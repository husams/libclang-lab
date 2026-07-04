"""indexer.model -- a high-level, object-oriented view over the cidx graph.

This is an *ergonomics layer* for writing scripts. The low-level
:mod:`indexer.query` API (``GraphQuery`` returning ``Sym`` / ``Edge`` / ``Site``)
is precise and token-cheap but uniform: every declaration comes back as a
``Sym`` regardless of whether it is a free function, a virtual method, a class,
or a template. You then call graph verbs (``g.callers``, ``g.members``,
``g.dispatch_targets`` ...) and remember the edge-direction conventions.

This module wraps that surface in concept-bearing classes:

    Function / Method / Constructor / Destructor   -- callables
    Class (class/struct/union) / Field             -- records & their members
    Enum / EnumConstant                            -- enumerations
    Typedef                                         -- aliases
    Namespace / Variable / Macro                   -- the rest
    FunctionTemplate / ClassTemplate               -- templated entities

Each entity exposes *semantic* properties instead of raw graph verbs, e.g.::

    from indexer.model import open_codebase
    cb = open_codebase()
    fn = cb.find("rd_kafka_new")[0]      # -> a Function
    fn.return_type                       # -> Type('rd_kafka_t *')
    fn.arguments                         # -> [Type('rd_kafka_type_t'), ...]
    [c.name for c in fn.callers()]       # -> qualified caller names
    cls = cb.find("RdKafka::Conf")[0]    # -> a Class
    cls.is_abstract, cls.parents, cls.children, cls.methods, cls.fields

Every entity carries its ``definition`` and ``declaration`` locations (surfaced
separately when they differ) and a ``references()`` method.

This layer is **purely additive and read-only**. It does NOT change or replace
``indexer.query``; ``entity.sym`` is always available as the escape hatch back to
the low-level ``Sym``, and ``cb.graph`` exposes the underlying ``GraphQuery``.

Fidelity notes (thin layer, no schema change):
  * ``arguments`` / ``return_type`` are parsed from the symbol's ``type_info``
    signature string. Parameters are positional ``Type`` values (clang does not
    store per-parameter names as indexable symbols), and a ``Type`` resolves to
    a declaring entity only on a best-effort basis (strip cv/ptr/ref/template
    args, then look the base name up).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Literal, Optional, Sequence, overload

from .entity_graph import ClassKind
from .query import (
    CallArg,
    CallerWithContext,
    DispatchSite,
    Edge,
    File,
    GraphQuery,
    Selection,
    Site,
    Sym,
    TemplateArg,
    TemplateParam,
    open_query,
)

__all__ = [
    "CodeBase",
    "open_codebase",
    "ClassKind",
    "Signature",
    "Location",
    "Type",
    "Reference",
    "TemplateParam",
    "TemplateArg",
    "CallerWithContext",
    "CallerWithContextModel",
    "SelectionModel",
    "DispatchSiteModel",
    "CallStep",
    "Entity",
    "Callable",
    "Function",
    "Method",
    "Constructor",
    "Destructor",
    "Record",
    "Class",
    "Struct",
    "Union",
    "Field",
    "Enum",
    "EnumConstant",
    "Typedef",
    "Namespace",
    "Variable",
    "Macro",
    "FunctionTemplate",
    "ClassTemplate",
]

#: kinds that name a type a `Type` can resolve to
_TYPE_DECL_KINDS = frozenset(
    {"class", "struct", "union", "enum", "typedef", "type-alias", "class-template"}
)

# --------------------------------------------------------------------------- #
# Plain value types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Location:
    """A resolved source position (file + line + col).

    ``file`` is a :class:`indexer.query.File` (a smart path) -- ``.path`` is the
    absolute path, ``.loc`` keeps the compact ``basename:line`` display, and the
    File's component/repo/source/AST accessors are reachable straight from here.
    """

    file: Optional[File]
    line: Optional[int]
    col: Optional[int]
    end_line: Optional[int] = None  # v25: end of the entity's own extent
    end_col: Optional[int] = None   # (line..end_line) slices the whole entity

    @property
    def path(self) -> Optional[str]:
        """The absolute path of this location's file, or ``None``."""
        return self.file.path if self.file else None

    @property
    def loc(self) -> str:
        if not self.file:
            return "<no-location>"
        base = self.file.name
        if self.line is None:
            return base
        if self.col is None:
            return f"{base}:{self.line}"
        return f"{base}:{self.line}:{self.col}"

    @property
    def span(self) -> Optional[str]:
        """``file:line-end_line`` -- the line range that slices the whole entity
        (function/method body, class/struct/union/enum/typedef region), or
        ``None`` when no end is known."""
        if not self.file or self.line is None or self.end_line is None:
            return None
        return f"{self.file.name}:{self.line}-{self.end_line}"

    def to_dict(self) -> dict:
        return {
            "file": self.file.path if self.file else None,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "end_col": self.end_col,
        }

    def __repr__(self) -> str:
        return f"Location(path={self.path!r}, line={self.line}, col={self.col})"


@dataclass(frozen=True)
class Type:
    """A type as it appears in a signature, field, or variable declaration.

    `spelling` is the textual type (e.g. ``const std::string &``). `name` is the
    bare base identifier with cv-qualifiers, pointer/reference, and template
    arguments stripped (``std::string``). `declaration()` resolves that base
    name to the entity that declares it, when one is indexed -- best-effort.
    """

    spelling: str
    _cb: "CodeBase"

    @property
    def name(self) -> str:
        return _base_type_name(self.spelling)

    @property
    def is_pointer(self) -> bool:
        return "*" in self.spelling

    @property
    def is_reference(self) -> bool:
        return "&" in self.spelling

    @property
    def is_const(self) -> bool:
        return bool(re.search(r"\bconst\b", self.spelling))

    def declaration(self) -> "Optional[Entity]":
        """The entity declaring this type's base name, or None. Best-effort:
        prefers a definition among record/enum/typedef kinds."""
        base = self.name
        if not base:
            return None
        cands = [
            e
            for e in self._cb.find(base, limit=50)
            if e.kind in _TYPE_DECL_KINDS and e.name == base
        ]
        if not cands:
            cands = [
                e for e in self._cb.find(base, limit=50) if e.kind in _TYPE_DECL_KINDS
            ]
        if not cands:
            return None
        cands.sort(key=lambda e: (not e.is_definition,))
        return cands[0]

    def __repr__(self) -> str:
        return f"Type({self.spelling!r})"


@dataclass(frozen=True)
class Reference:
    """A place that refers to an entity: who, how, and where."""

    by: "Entity"  # the referring entity
    kind: str  # 'calls' | 'uses'
    sites: Sequence[Site]  # concrete file:line locations of the reference

    def __repr__(self) -> str:
        where = self.sites[0].loc if self.sites else self.by.location.loc
        return f"Reference({self.kind} by {self.by.name} @{where})"


@dataclass(frozen=True)
class SelectionModel:
    """Entity-typed view of a :class:`indexer.query.Selection`: if the receiver's
    run-time type is ``selecting_type``, the virtual call lands on ``target``.
    ``inherited`` marks a subtype that inherits an ancestor's override."""

    selecting_type: "Optional[Entity]"
    target: "Optional[Entity]"
    inherited: bool = False

    def __repr__(self) -> str:
        st = self.selecting_type.name if self.selecting_type else "?"
        tg = self.target.name if self.target else "?"
        tag = " inherited" if self.inherited else ""
        return f"SelectionModel({st} -> {tg}{tag})"


@dataclass(frozen=True)
class CallerWithContextModel:
    """Entity-typed view of a :class:`indexer.query.CallerWithContext`.

    Returned by :meth:`Callable.callers` / :meth:`Callable.callees` when
    ``include_instantiations=True``.

    Attributes:
        entity              The caller/callee as a typed :class:`Entity`.
        via_instantiation   The instantiation member node (``X<int>::print``)
                            through which this entity was reached, as an
                            :class:`Entity`; ``None`` for direct callers of
                            the primary.
        via_template_args   Concrete template arguments from the instantiation
                            TYPE node (e.g. ``[TemplateArg(0, type, 'int')]``
                            for ``X<int>``).  Empty for direct callers or when
                            no args are stored.

    Usage example::

        for r in fn.callers(include_instantiations=True):
            args = [a.literal for a in r.via_template_args]
            print(r.entity.name, "via", args or "direct")
            # -> caller_int via ['int']
            # -> caller_double via ['double']
    """

    entity: "Entity"
    via_instantiation: "Optional[Entity]"
    via_template_args: list[TemplateArg]

    def __repr__(self) -> str:
        targs = (
            "<" + ", ".join(a.literal or "?" for a in self.via_template_args) + ">"
            if self.via_template_args
            else ""
        )
        tag = f" via{targs}" if self.via_instantiation else ""
        return f"CallerWithContextModel({self.entity!r}{tag})"


@dataclass(frozen=True)
class DispatchSiteModel:
    """Entity-typed view of a :class:`indexer.query.DispatchSite` -- the Phase-1
    over-approximation of one virtual call: the full ``selections`` map plus a
    ``prunable`` flag (and ``unprunable_reasons`` when not). NO pruning happens
    in Phase 1; this just records what Phase 2 may later narrow."""

    receiver_static_type: "Optional[Entity]"
    declared_target: "Optional[Entity]"
    selections: "list[SelectionModel]"
    prunable: bool
    unprunable_reasons: tuple[str, ...]

    @property
    def targets(self) -> "list[Entity]":
        return [s.target for s in self.selections if s.target is not None]

    def __repr__(self) -> str:
        tg = self.declared_target.name if self.declared_target else "?"
        state = (
            "prunable"
            if self.prunable
            else f"unprunable({','.join(self.unprunable_reasons)})"
        )
        return f"DispatchSiteModel({tg}: {len(self.selections)} candidate(s), {state})"


@dataclass(frozen=True)
class CallStep:
    """One step of a devirtualized call-graph walk: the ``callee`` reached at
    ``depth`` (call edges from the root), plus the ``dispatch_site`` when that
    callee is a virtual dispatch point (None for an ordinary static call).

    Phase-2 fields (``prune=True`` only):
      * ``pruned_candidates``: the subset of ``dispatch_site.selections`` kept
        after Gamma pruning; None when prune=False OR site is kept-all.
      * ``gamma_receiver``: the Gamma TypeSet (frozenset of class USRs) for
        the receiver; None == TOP (unknown/non-finite)."""

    callee: "Entity"
    depth: int
    dispatch_site: "Optional[DispatchSiteModel]" = None
    pruned_candidates: "Optional[list[SelectionModel]]" = None
    gamma_receiver: "Optional[frozenset[str]]" = None

    def __repr__(self) -> str:
        v = " [virtual]" if self.dispatch_site is not None else ""
        p = (
            f" pruned={len(self.pruned_candidates)}"
            if self.pruned_candidates is not None
            else ""
        )
        return f"CallStep({self.callee.name} @depth {self.depth}{v}{p})"


# --------------------------------------------------------------------------- #
# The codebase handle / entity factory
# --------------------------------------------------------------------------- #


def open_codebase(
    db_path: Optional[str] = None, require_edges: bool = False
) -> "CodeBase":
    """Open the standard cidx index and wrap it as a CodeBase."""
    return CodeBase(open_query(db_path, require_edges=require_edges))


class CodeBase:
    """High-level entry point: looks up typed :class:`Entity` objects.

    Wraps a :class:`indexer.query.GraphQuery`. The underlying graph handle stays
    reachable as ``cb.graph`` for anything this layer does not cover.
    """

    def __init__(self, graph: GraphQuery):
        self.graph = graph

    # -- lifecycle ----------------------------------------------------------- #

    def close(self) -> None:
        self.graph.close()

    def __enter__(self) -> "CodeBase":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- factory ------------------------------------------------------------- #

    def wrap(self, sym: Optional[Sym]) -> "Optional[Entity]":
        """Turn a low-level ``Sym`` into its concept-bearing :class:`Entity`."""
        if sym is None:
            return None
        cls = _KIND_TO_CLASS.get(sym.kind, Entity)
        return cls(sym, self)

    def _wrap_all(self, syms: Iterable[Optional[Sym]]) -> "list[Entity]":
        out = []
        for s in syms:
            e = self.wrap(s)
            if e is not None:
                out.append(e)
        return out

    def _wrap_cwc(
        self, cwcs: "Iterable[CallerWithContext]"
    ) -> "list[CallerWithContextModel]":
        """Wrap a sequence of :class:`CallerWithContext` into entity-typed
        :class:`CallerWithContextModel` values.  Entries whose ``sym``
        resolves to ``None`` are silently dropped (same policy as
        ``_wrap_all``)."""
        out: list[CallerWithContextModel] = []
        for r in cwcs:
            e = self.wrap(r.sym)
            if e is None:
                continue
            out.append(
                CallerWithContextModel(
                    entity=e,
                    via_instantiation=self.wrap(r.via_instantiation),
                    via_template_args=r.via_template_args,
                )
            )
        return out

    def _wrap_dispatch_site(self, ds: DispatchSite) -> DispatchSiteModel:
        """Wrap a low-level :class:`DispatchSite` into the entity-typed
        :class:`DispatchSiteModel`."""
        selections = [
            SelectionModel(
                selecting_type=self.wrap(s.selecting_type),
                target=self.wrap(s.target),
                inherited=s.inherited,
            )
            for s in ds.candidates
        ]
        return DispatchSiteModel(
            receiver_static_type=self.wrap(ds.receiver_static_type),
            declared_target=self.wrap(ds.declared_target),
            selections=selections,
            prunable=ds.prunable,
            unprunable_reasons=ds.unprunable_reasons,
        )

    # -- lookup -------------------------------------------------------------- #

    def get(self, ident) -> "Optional[Entity]":
        """Fetch one entity by id, USR, Sym, or Entity (pass-through)."""
        if isinstance(ident, Entity):
            return ident
        return self.wrap(self.graph.get(ident))

    def find(
        self, pattern: str, kind: Optional[str] = None, limit: int = 50
    ) -> "list[Entity]":
        """Fuzzy qualified-name lookup -> typed entities (see GraphQuery.find)."""
        return self._wrap_all(self.graph.find(pattern, kind=kind, limit=limit))

    def by_name(self, spelling: str, kind: Optional[str] = None) -> "list[Entity]":
        """Exact-spelling lookup -> typed entities."""
        return self._wrap_all(self.graph.by_name(spelling, kind=kind))

    def symbols_in_file(self, path_substr: str, limit: int = 500) -> "list[Entity]":
        return self._wrap_all(self.graph.symbols_in_file(path_substr, limit=limit))

    # typed, signature-aware selectors ------------------------------------- #
    #
    # Each selector returns a LIST of matching typed entities (empty if none).
    # Callables (function / method / function_template) accept the three
    # signature-calling conventions resolved by ``_make_signature``:
    #
    #   cb.function("app::f", ret="int", params=["int", "std::string"])  # parts
    #   cb.function("int app::f(int, std::string)")                       # full
    #   cb.function("app::f", "int(std::string)")                        # ret(p)
    #
    # Omitting ``ret`` / ``params`` leaves that dimension unconstrained; an empty
    # ``params=[]`` requires zero parameters.  Overloads all come back; narrow by
    # adding ``ret`` / ``params``.  Type arguments may be strings, ``Type``, or
    # entity objects, and are matched by NORMALIZED spelling (whitespace / cv /
    # ``*`` / ``&`` tolerant, ``std::__1::`` folded, namespace-stripped fallback).

    def _lookup(self, name: str, limit: int = 100) -> "list[Entity]":
        """Candidate entities for a (possibly qualified) ``name`` -- narrowed to
        those whose name / spelling actually matches ``name`` (qualified vs.
        spelling leniently). De-duplicated, order-stable.

        Fast path: an INDEX-backed exact lookup on qual_name / spelling
        (``by_qual_or_spelling``) -- on a multi-million-symbol index the old
        ``self.find`` fuzzy ``LIKE '%..%'`` is a full table scan, run twice
        (name + tail). When the exact path yields matches we return them; only a
        partial / suffix-qualified name (e.g. ``Box::get`` for ``app::Box::get``)
        that the index cannot answer falls back to the fuzzy scan."""
        tail = _unqualify(name)

        def _narrow(syms) -> "list[Entity]":
            seen: dict[int, Entity] = {}
            for e in self._wrap_all(syms):
                seen.setdefault(e.id, e)
            return [e for e in seen.values() if _name_matches(name, e)]

        exact = _narrow(self.graph.by_qual_or_spelling(name, tail, limit=limit))
        if exact:
            return exact
        # Fallback: fuzzy candidate generation (partial / suffix names).
        queries = [name]
        if tail and tail != name:
            queries.append(tail)
        fuzzy = []
        for q in queries:
            fuzzy.extend(self.graph.find(q, limit=limit))
        return _narrow(fuzzy)

    def _make_signature(
        self, name: str, sig: Optional[str], ret, params
    ) -> "tuple[Signature, str]":
        """Resolve the three calling conventions into a ``(Signature, name)``.

        * ``name`` containing ``(`` -> a full signature string; the embedded
          qualified name becomes the lookup name (``sig`` / ``ret`` / ``params``
          are ignored).
        * else a non-None ``sig`` -> a ``ret(params)`` signature string.
        * else the ``ret`` / ``params`` keywords."""
        if "(" in name:
            return Signature.parse_full(name)
        if sig is not None:
            return Signature.parse_ret_params(sig), name
        return Signature.from_parts(ret=ret, params=params), name

    def function(
        self, name: str, sig: Optional[str] = None, *, ret=None, params=None
    ) -> "list[Function | FunctionTemplate]":
        """Free functions matching ``name`` and the optional signature.

        Includes free function templates (``FunctionTemplate`` is a sibling of
        ``Function``, not a subclass).  Member functions are excluded -- use
        :meth:`method`.  Returns ALL overloads that match; empty when none."""
        spec, lookup = self._make_signature(name, sig, ret, params)
        return [
            e
            for e in self._lookup(lookup)
            if isinstance(e, (Function, FunctionTemplate))
            and not isinstance(e, Method)
            and spec.matches(e)
        ]

    def method(
        self,
        name: str,
        sig: Optional[str] = None,
        *,
        ret=None,
        params=None,
        owner=None,
    ) -> "list[Method]":
        """Member functions matching ``name`` and the optional signature.

        ``owner`` (a ``Record``, an entity, or a class-name string) scopes the
        match to methods of that owning record.  Returns ALL overloads; empty
        when none."""
        spec, lookup = self._make_signature(name, sig, ret, params)
        owner_ok = self._owner_predicate(owner)
        return [
            e
            for e in self._lookup(lookup)
            if isinstance(e, Method) and spec.matches(e) and owner_ok(e)
        ]

    def function_template(
        self, name: str, sig: Optional[str] = None, *, ret=None, params=None
    ) -> "list[FunctionTemplate]":
        """Primary free function templates matching ``name`` (and optional
        signature).  Member function templates are reached via :meth:`method`'s
        owner or ``Record.methods``."""
        spec, lookup = self._make_signature(name, sig, ret, params)
        return [
            e
            for e in self._lookup(lookup)
            if isinstance(e, FunctionTemplate)
            and not isinstance(e, Method)
            and spec.matches(e)
        ]

    def _records(
        self, name: str, kinds: "set[str]", *, class_kinds=None
    ) -> "list[Record]":
        """Records named ``name`` of the given C++ keyword ``kinds`` (class /
        struct / union) and -- when ``class_kinds`` is given -- the given
        abstractness (CONCRETE / ABSTRACT / INTERFACE).

        Fast path: ONE indexed ``entity_node`` JOIN (``records_by_name``) reads
        the abstractness ``resolve`` already materialized, so there is no
        per-candidate ``class_kind`` query. Falls back to per-record
        classification only for a fuzzy / suffix name the exact JOIN cannot
        answer, or an index whose ``entity_node`` table is not yet
        materialized."""
        ekinds = (
            sorted({i for ck in class_kinds for i in _CLASSKIND_ENTITY_IDS[ck]})
            if class_kinds is not None
            else None
        )
        tail = _unqualify(name)
        if ekinds is not None and self.graph.entity_nodes_ready():
            syms = self.graph.records_by_name(
                name, tail, symbol_kinds=kinds, entity_kinds=ekinds
            )
            seen: dict[int, Record] = {}
            for e in self._wrap_all(syms):
                if isinstance(e, Record) and _name_matches(name, e):
                    seen.setdefault(e.id, e)
            hits = list(seen.values())
            # A bare name is fully answered by the exact JOIN; only a qualified
            # / suffix name with no exact hit needs the fuzzy fallback below.
            if hits or name == tail:
                return hits
        return self._records_classify(name, kinds, class_kinds=class_kinds)

    def _records_classify(
        self, name: str, kinds: "set[str]", *, class_kinds=None
    ) -> "list[Record]":
        """Fallback record selector: generate candidates via ``_lookup`` and
        classify each with ``class_kind``. Used when the materialized
        ``entity_node`` JOIN cannot answer -- a fuzzy / suffix name, or an
        unmaterialized index."""
        out: list[Record] = []
        for e in self._lookup(name):
            if not isinstance(e, Record) or isinstance(e, ClassTemplate):
                continue
            if e.kind not in kinds:
                continue
            if _is_instance(e):  # a template instantiation is not a plain record
                continue
            if class_kinds is not None and e.class_kind not in class_kinds:
                continue
            out.append(e)
        return out

    def klass(self, name: str) -> "list[Record]":
        """Concrete ``class`` records named ``name`` -- excludes abstract
        classes, interfaces, template instantiations, and class templates (the
        entity-graph partitioning).  Returns a list; empty when none."""
        return self._records(name, {"class"}, class_kinds={ClassKind.CONCRETE})

    def struct(self, name: str) -> "list[Record]":
        """Concrete ``struct`` records named ``name`` (same exclusions as
        :meth:`klass`)."""
        return self._records(name, {"struct"}, class_kinds={ClassKind.CONCRETE})

    def record(self, name: str) -> "list[Record]":
        """Concrete ``class`` / ``struct`` / ``union`` records named ``name``
        (same exclusions as :meth:`klass`)."""
        return self._records(
            name, {"class", "struct", "union"}, class_kinds={ClassKind.CONCRETE}
        )

    def interface(self, name: str) -> "list[Record]":
        """Pure interface records named ``name`` -- all own methods pure-virtual
        and no data members (``ClassKind.INTERFACE``).  Mutually exclusive with
        :meth:`klass` and :meth:`abstract_class`."""
        return self._records(
            name, {"class", "struct", "union"}, class_kinds={ClassKind.INTERFACE}
        )

    def abstract_class(self, name: str) -> "list[Record]":
        """Abstract (but not pure-interface) records named ``name`` -- have a
        pure-virtual method yet carry state or a concrete method
        (``ClassKind.ABSTRACT``).  Mutually exclusive with :meth:`klass` and
        :meth:`interface`."""
        return self._records(
            name, {"class", "struct", "union"}, class_kinds={ClassKind.ABSTRACT}
        )

    def class_template(self, name: str) -> "list[ClassTemplate]":
        """Primary class templates named ``name`` (e.g. ``Box<T>``)."""
        return [e for e in self._lookup(name) if isinstance(e, ClassTemplate)]

    def instance(self, name: Optional[str] = None, *, args=None) -> "list[Record]":
        """Template instantiations / specializations matched by the types used
        for the instance.

        Three forms::

            cb.instance("Box<int>")              # base + args in one string
            cb.instance("app::Box<int>")         # qualified base accepted
            cb.instance("Box", args=["int"])     # base + explicit arg list

        ``args`` may be strings, ``Type``, or entity objects and are matched by
        normalized spelling against the instance's stored template arguments
        (order + arity).  Omitting both the ``<...>`` and ``args`` matches every
        instantiation of the base.  Returns a list; empty when none."""
        base, parsed = _parse_instance_name(name)
        want = args if args is not None else parsed
        out: list[Record] = []
        if base is None:
            return out
        for e in self._lookup(base):
            if not isinstance(e, Record):
                continue
            if not _is_instance(e) or not _name_matches(base, e):
                continue
            if want is not None:
                tas = e.template_arguments
                if len(tas) != len(want):
                    continue
                if not all(_types_match(w, _ta_text(t)) for w, t in zip(want, tas)):
                    continue
            out.append(e)
        return out

    def _owner_predicate(self, owner):
        """A predicate over a ``Method`` enforcing the optional owner scope."""
        if owner is None:
            return lambda e: True
        if isinstance(owner, Entity):
            oid = owner.id
            return lambda e: e.owner is not None and e.owner.id == oid
        return lambda e: e.owner is not None and _name_matches(owner, e.owner)

    def stats(self) -> dict:
        return self.graph.stats()


# --------------------------------------------------------------------------- #
# Entity hierarchy
# --------------------------------------------------------------------------- #


class Entity:
    """Base for every indexed declaration. Wraps a low-level ``Sym``.

    Common to all entities: identity (``name``/``usr``/``id``), ``kind``, the
    ``definition`` and ``declaration`` locations (the latter only when distinct),
    and ``references()``. ``self.sym`` is the escape hatch to the low-level value.
    """

    def __init__(self, sym: Sym, cb: CodeBase):
        self.sym = sym
        self._cb = cb

    # -- identity ------------------------------------------------------------ #

    @property
    def name(self) -> str:
        """Fully-qualified name (falls back to spelling for C symbols)."""
        return self.sym.name

    @property
    def spelling(self) -> str:
        return self.sym.spelling

    @property
    def display_name(self) -> str:
        """The fully-qualified name WITH concrete template arguments, so two
        instantiations are told apart: ``cont::Wrapper<int>`` vs
        ``cont::Wrapper<bool>`` (the primary template reads ``cont::Wrapper<T>``).

        Composed from the qualified :attr:`name` (``cont::Wrapper``) plus the
        ``<...>`` suffix of the stored ``display_name`` (``Wrapper<int>``), so the
        namespace qualification is kept. Falls back to :attr:`name` for a
        non-templated symbol. Prefer this over :attr:`name` when printing
        template instances -- :attr:`name`/:attr:`spelling` drop the arguments."""
        dn = self.sym.display_name
        if dn and "<" in dn:
            return _strip_template_args(self.name) + dn[dn.index("<") :]
        return self.name

    @property
    def template_arguments(self) -> list[TemplateArg]:
        """The concrete template arguments this entity binds, when it is a
        specialization or an instantiation of a template (e.g.
        ``[TemplateArg(#0 type int)]`` for ``Wrapper<int>``). Empty for a plain,
        non-templated entity. Available on every entity -- not just records -- so
        an instance member node can list its bindings too.

        The low-level view: each :class:`TemplateArg` carries ``arg_kind`` (type
        vs. value), the ``literal`` spelling, and a ``ref_id`` linking a
        class-type argument to its indexed symbol. For the resolved-type view use
        :attr:`template_argument_types` / :meth:`template_argument_entities`."""
        return self._cb.graph.template_args(self.sym)

    @property
    def template_argument_types(self) -> list["Type"]:
        """The concrete template arguments as :class:`Type` objects, in order --
        the actual types a template was instantiated with: ``[Type('int')]`` for
        ``Wrapper<int>``, ``[Type('app::Cache')]`` for ``Singleton<app::Cache>``.

        Each :class:`Type` exposes ``spelling`` / ``name`` and resolves to its
        declaring entity via :meth:`Type.declaration`; for a precise, ref-id
        backed resolution use :meth:`template_argument_entities` instead. A
        non-type (value) argument carries its value text as the spelling so the
        list stays position-aligned with :attr:`template_arguments`."""
        return [Type(ta.literal or "", self._cb) for ta in self.template_arguments]

    def template_argument_entities(self) -> "list[Optional[Entity]]":
        """The declaring entity of each template argument, in order, resolved
        precisely from the stored ``ref_id`` -- e.g. ``Singleton<app::Cache>``
        yields ``[Class('app::Cache' ...)]`` and ``Box<Color>`` yields
        ``[Enum('geo::Color' ...)]``.

        Position ``i`` is ``None`` when the argument is a builtin/unindexed type
        (``int``) or a non-type value (``64``) -- i.e. whenever no indexed symbol
        backs it. Pair with :attr:`template_argument_types` for the textual
        (always-present) view."""
        out: list[Optional[Entity]] = []
        for ta in self.template_arguments:
            sym = self._cb.graph.get(ta.ref_id) if ta.ref_id is not None else None
            out.append(self._cb.wrap(sym))
        return out

    @property
    def kind(self) -> str:
        return self.sym.kind

    @property
    def usr(self) -> str:
        return self.sym.usr

    @property
    def id(self) -> int:
        return self.sym.id

    @property
    def is_definition(self) -> bool:
        return self.sym.is_definition

    @property
    def is_instantiation(self) -> bool:
        """True for implicit template-instantiation nodes (``X<int>`` type
        node or ``X<int>::print`` member node) created by ADR-004."""
        return self.sym.is_instantiation

    def template_of(self) -> "Optional[Entity]":
        """The primary template this node is an instantiation of, or ``None``.

        Returns ``None`` when this entity is not an implicit-instantiation node
        (``is_instantiation`` is False) or has no outgoing ``instantiates`` edge.

        For an ``X<int>`` type node: returns the ``X`` class template.
        For an ``X<int>::print`` member node: returns the ``X::print`` template
        method. For the template itself, returns ``None``."""
        tpl = self._cb.graph.template_of(self.sym)
        return self._cb.wrap(tpl)

    @property
    def is_stub(self) -> bool:
        return self.sym.is_stub

    # -- locations ----------------------------------------------------------- #

    def _locations(self):
        return self._cb.graph.def_decl_locations(self.sym)

    def _loc(self, site) -> Location:
        """Wrap a ``(abs_path, line, col)`` tuple into a :class:`Location` whose
        ``file`` is a DB-aware :class:`File` bound to this codebase's graph."""
        path, line, col = site
        return Location(self._cb.graph.make_file(path), line, col)

    @property
    def file(self) -> Optional[File]:
        """The :class:`indexer.query.File` of this entity's best-known location
        (definition, else declaration), or ``None`` for a stub. The smart-path
        handle: ``.path`` / ``.component`` / ``.repo`` / ``.compile_options`` /
        ``.source(...)`` / ``.tu()`` / ``.walk()``."""
        return self.sym.file

    @property
    def location(self) -> Location:
        """Best-known location (definition, else declaration). Carries the
        entity's extent end (``end_line``/``end_col``), so ``.location.span``
        gives the ``file:line-end_line`` range that slices the whole entity."""
        return Location(
            self.sym.file,
            self.sym.line,
            self.sym.col,
            self.sym.end_line,
            self.sym.end_col,
        )

    def source(self, default_lines: int = 10, *, encoding: str = "utf-8") -> str:
        """This entity's own source text, read straight off disk over its
        stored extent (falling back to ``default_lines`` lines from its start
        line when no extent is known). See :meth:`Sym.source`."""
        return self.sym.source(default_lines, encoding=encoding)

    @property
    def definition(self) -> Optional[Location]:
        """Where the entity is defined, or None if only declared."""
        defn, _ = self._locations()
        return self._loc(defn) if defn else None

    @property
    def declaration(self) -> Optional[Location]:
        """Where the entity is declared, surfaced only when it DIFFERS from the
        definition (e.g. a prototype in a header vs. the body in a .c). Returns
        None when the declaration coincides with the definition or is unknown."""
        defn, decl = self._locations()
        if decl is None:
            return None
        if defn is not None and decl == defn:
            return None
        return self._loc(decl)

    # -- references ---------------------------------------------------------- #

    def references(self, limit: int = 500) -> list[Reference]:
        """Everywhere this entity is called or used (incoming calls + uses).

        Each :class:`Reference` carries the referring entity, the relationship
        kind, and the concrete source ``sites``."""
        out = []
        for e in self._cb.graph.references(self.sym, limit=limit):
            peer = self._cb.wrap(e.peer)
            if peer is not None:
                out.append(Reference(by=peer, kind=e.kind, sites=tuple(e.sites)))
        return out

    # -- escape / serialization --------------------------------------------- #

    def to_dict(self) -> dict:
        d = self.sym.to_dict()
        d["entity"] = type(self).__name__
        display = self.display_name
        if display != self.name:
            d["display_name"] = display
        targs = self.template_arguments
        if targs:
            d["template_arguments"] = [ta.to_dict() for ta in targs]
        decl = self.declaration
        if decl is not None:
            d["declaration"] = decl.to_dict()
        return d

    def __eq__(self, other) -> bool:
        return isinstance(other, Entity) and other.sym.id == self.sym.id

    def __hash__(self) -> int:
        return hash(self.sym.id)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.display_name!r} @{self.location.loc})"


# --------------------------------------------------------------------------- #
# Phase 2 — Gamma propagation engine (internal, Python-only, EXEMPT from C++)
# --------------------------------------------------------------------------- #

#: Maximum number of distinct calling contexts cloned per callable before
#: the analysis falls back to TOP (sound, terminates cloning).
K_LIMIT: int = 3


# Sentinel for the "unknown / non-finite" type set (TOP).
class _Top:
    """Singleton sentinel for the TOP type-set (unknown receiver type)."""

    _instance: "Optional[_Top]" = None

    def __new__(cls) -> "_Top":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "TOP"


TOP = _Top()

# TypeSet = TOP | frozenset[str]  (str = class USR)
TypeSet = "_Top | frozenset[str]"


def _join(gamma: dict, key: tuple, ts: "_Top | frozenset[str]") -> None:
    """Flow-insensitive union join; never kills an existing binding."""
    cur = gamma.get(key, frozenset())
    if cur is TOP or ts is TOP:
        gamma[key] = TOP
    else:
        gamma[key] = cur | ts  # type: ignore[operator]


class _GammaEngine:
    """Pure-Python type-environment propagation over the query layer.

    Computes a Gamma mapping (callable-context, decl-usr) -> TypeSet so
    that the devirtualized_callgraph(prune=True) walk can narrow virtual
    dispatch candidates to the receiver's statically-determined type set.

    Constructed once per ``devirtualized_callgraph(prune=True)`` call and
    seeded by ``analyse(root_sym)``. Afterwards use ``decide()`` to prune
    a dispatch site inside a given context.
    """

    def __init__(self, cb: "CodeBase", assume_closed_world: bool = False) -> None:
        self._cb = cb
        self._g = cb.graph
        self._cw = assume_closed_world
        # gamma[(ctx, decl_usr)] -> TypeSet
        self._gamma: dict[tuple, "_Top | frozenset[str]"] = {}
        # context (callee_id, param_sig) -> PENDING (in-flight) | result
        self._analysed: set[tuple] = set()
        # count distinct contexts analysed per callable USR (k-limit)
        self._ctx_count: dict[str, int] = {}
        # Phase 3b: closed-world param-Γ memo keyed by (callee_usr, pos)
        self._cw_memo: dict[tuple, "_Top | frozenset[str]"] = {}

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def analyse(self, root: Sym) -> None:
        """Seed Gamma starting from ``root`` (the walk root).

        After this call, ``decide()`` can be queried for any site inside
        any callable reachable from root."""
        root_ctx = (root.usr, ())
        self._visit(root_ctx, root)

    # ------------------------------------------------------------------ #
    # Core traversal
    # ------------------------------------------------------------------ #

    def _visit(self, ctx: tuple, fn: Sym) -> None:
        if ctx in self._analysed:
            return
        self._analysed.add(ctx)

        # Seed from value-typed local constructions inside this function's body.
        # For each outgoing `calls` edge where dst is a constructor and the
        # enclosing VAR_DECL is a value type, Gamma[var] = {type}.
        # The extractor stores construct/local src_kinds in call_arg; we mine
        # those here so the algorithm does not need to re-parse ASTs.
        self._seed_locals(ctx, fn)

        # Descend into callees and bind their params.
        for edge in self._g.edges_out(fn, kinds=("calls",), limit=500):
            callee = edge.peer
            self._bind_and_visit(ctx, callee, edge)

    def _seed_locals(self, ctx: tuple, fn: Sym) -> None:
        """Seed Gamma from call_arg rows in fn's outgoing calls.

        - construct arg (B{} / new B): type is known immediately -> seed {type_usr}.
        - local arg with type_usr: the extractor recorded the static type of the
          named variable -> seed (ctx, decl_usr) = {type_usr} so the binding is
          available when the callee's gamma_for_site looks up recv_decl_usr.
        """
        for edge in self._g._edges(fn, "out", ("calls",), 500, with_sites=False):
            for arg in self._g.call_args(edge.edge_id):
                if arg.src_kind == "construct" and arg.type_usr:
                    # seed the (ctx, arg.type_usr) binding so decide() can use it
                    _join(self._gamma, (ctx, arg.type_usr), frozenset({arg.type_usr}))
                elif (
                    arg.src_kind in ("local", "this") and arg.decl_usr and arg.type_usr
                ):
                    # static type of the named local/param; decl_usr is the arg's
                    # own USR which may differ from the callee's param USR, but
                    # seeding it lets the caller-level Gamma be complete for
                    # forward propagation into _bind_and_visit.
                    _join(
                        self._gamma,
                        (ctx, arg.decl_usr),
                        frozenset({arg.type_usr}),
                    )

    def _resolve_source(
        self,
        ctx: tuple,
        src_kind: Optional[str],
        type_usr: Optional[str],
        decl_usr: Optional[str],
        callee_usr: Optional[str],
        type_is_value: Optional[int] = None,
    ) -> "_Top | frozenset[str]":
        """Map a provenance record to a TypeSet in ctx."""
        if src_kind in (None, "literal", "unknown"):
            return TOP
        if src_kind == "construct":
            return frozenset({type_usr}) if type_usr else TOP
        if src_kind in ("member", "global"):
            if type_is_value and type_usr:  # 3a: exact value singleton
                return frozenset({type_usr})
            if decl_usr is None:
                return TOP
            val = self._gamma.get((ctx, decl_usr))
            return val if val is not None else TOP
        if src_kind in ("local", "this"):
            # 3a extension: a value-typed local has an exact concrete type.
            # type_is_value=1 means the local variable holds T BY VALUE (not
            # a reference/pointer), so its run-time type IS type_usr.
            # This handles `B b; dispatch_param(b)` in cross-TU closed-world.
            if type_is_value and type_usr:
                return frozenset({type_usr})
            if decl_usr is None:
                return TOP
            val = self._gamma.get((ctx, decl_usr))
            return val if val is not None else TOP
        if src_kind == "call_result":
            if type_is_value and type_usr:  # 3a: by-value return singleton
                return frozenset({type_usr})
            return TOP  # was unconditional TOP (Phase 2)
        return TOP

    def _param_sig(self, param_sets: list) -> tuple:
        """Canonical hashable signature of param TypeSets."""
        parts = []
        for ts in param_sets:
            if ts is TOP:
                parts.append(None)
            else:
                parts.append(tuple(sorted(ts)))  # type: ignore[arg-type]
        return tuple(parts)

    def _bind_and_visit(self, caller_ctx: tuple, callee: Sym, edge: "Edge") -> None:
        """Bind callee params from call_args and recurse.

        Context key: we always use (callee.usr, ()) — flow-insensitive per
        callable.  This ensures the key written here is byte-identical to the
        key the walk constructs in _devirt_prune (line ~1046), fixing the
        context-key mismatch that caused all gamma lookups to miss.

        Param binding: for each arg at position i with a known TypeSet ts we
        seed two callee-context keys:
          - (callee_ctx, ("@pos", i))  -- position-indexed, always available
          - (callee_ctx, a.decl_usr)   -- USR-indexed, when decl_usr is set

        gamma_for_site reads the position-indexed key as a fallback when the
        USR-based lookups (recv_decl_usr, recv_type_usr) miss, using the
        site's recv_param_pos field.
        """
        # Gather args for any site of this edge.
        all_args = self._g.call_args(edge.edge_id)

        # Flow-insensitive context: always () — keeps write/read keys identical.
        callee_ctx = (callee.usr, ())

        # k-limit: cap distinct contexts per callable USR
        count = self._ctx_count.get(callee.usr, 0)
        if count >= K_LIMIT:
            # Beyond limit => keep callee dispatch sites as KEEP_ALL (sound)
            return

        # Bind params into callee's Gamma keyed by position and decl_usr.
        if all_args:
            # Group by position and resolve each arg's TypeSet.
            by_pos: dict[int, list[CallArg]] = {}
            for a in all_args:
                by_pos.setdefault(a.position, []).append(a)

            n_params = max(by_pos.keys()) + 1 if by_pos else 0
            for i in range(n_params):
                args_i = by_pos.get(i, [])
                ts: "_Top | frozenset[str]" = frozenset()
                for a in args_i:
                    a_ts = self._resolve_source(
                        caller_ctx,
                        a.src_kind,
                        a.type_usr,
                        a.decl_usr,
                        a.callee_usr,
                        a.type_is_value,
                    )
                    if a_ts is TOP:
                        ts = TOP
                        break
                    else:
                        ts = frozenset() if ts is TOP else ts | a_ts  # type: ignore[operator]
                pos_ts = ts if ts is not TOP else TOP

                # Seed position-indexed key (primary cross-function flow path).
                _join(self._gamma, (callee_ctx, ("@pos", i)), pos_ts)

                # Also seed decl_usr keys from each arg in this position.
                for a in args_i:
                    a_ts2 = self._resolve_source(
                        caller_ctx,
                        a.src_kind,
                        a.type_usr,
                        a.decl_usr,
                        a.callee_usr,
                        a.type_is_value,
                    )
                    if a.decl_usr:
                        _join(self._gamma, (callee_ctx, a.decl_usr), a_ts2)
                    if a.type_usr and a.src_kind == "construct":
                        _join(
                            self._gamma,
                            (callee_ctx, a.type_usr),
                            frozenset({a.type_usr}),
                        )

        self._ctx_count[callee.usr] = count + 1
        self._visit(callee_ctx, callee)

    # ------------------------------------------------------------------ #
    # Gamma reader (for receiver provenance at a site)
    # ------------------------------------------------------------------ #

    def gamma_for_site(self, ctx: tuple, site: "Site") -> "_Top | frozenset[str]":
        """The TypeSet for a virtual call's receiver at ``site`` in ``ctx``.

        Lookup order (first hit wins):
          0. 3a: value member/global/call_result receiver -> exact singleton.
          1. (ctx, recv_decl_usr)   — USR of the named local/param
          2. (ctx, recv_type_usr)   — static declared type USR
          3. (ctx, ("@pos", recv_param_pos))  — position-indexed binding from
             _bind_and_visit; fills the cross-function gap when decl_usr in the
             callee (a_parm_usr) differs from decl_usr in the caller (b_var_usr)
          4. construct shortcut     — recv_src_kind==construct -> {recv_type_usr}
          5. 3b: closed-world cross-TU param union (when assume_closed_world).
        """
        # 3a: value member/global/call_result receiver -> exact singleton.
        if (
            site.recv_type_is_value
            and site.recv_src_kind in ("member", "global", "call_result")
            and site.recv_type_usr
        ):
            return frozenset({site.recv_type_usr})
        if site.recv_decl_usr:
            val = self._gamma.get((ctx, site.recv_decl_usr))
            if val is not None:
                return val
        # Fall back to static type
        if site.recv_type_usr:
            val = self._gamma.get((ctx, site.recv_type_usr))
            if val is not None:
                return val
        # Position-indexed fallback: covers cross-function flow where the callee's
        # recv_decl_usr (param USR) differs from the caller's arg decl_usr.
        if site.recv_param_pos is not None:
            val = self._gamma.get((ctx, ("@pos", site.recv_param_pos)))
            if val is not None:
                return val
        # Check if this is a construct site
        if site.recv_src_kind == "construct" and site.recv_type_usr:
            return frozenset({site.recv_type_usr})
        # 3b: closed-world cross-TU param union (last resort, only when enabled).
        if (
            self._cw
            and site.recv_src_kind == "local"
            and site.recv_param_pos is not None
        ):
            cw = self._closed_world_param(ctx[0], site.recv_param_pos, frozenset())
            if cw is not None:
                return cw
        return TOP

    def _closed_world_param(
        self, callee_usr: str, pos: int, visited: "frozenset[str]"
    ) -> "_Top | frozenset[str] | None":
        """Monotone join of resolve_source(arg_pos) over ALL visible callers of
        ``callee_usr``. Returns a frozenset (narrowed), TOP, or None (use TOP).
        Sound only because the caller asserted assume_closed_world (the index is
        whole-program + resolved)."""
        if callee_usr in visited:  # in-flight cycle -> TOP
            return TOP
        memo_key = (callee_usr, pos)
        if memo_key in self._cw_memo:
            return self._cw_memo[memo_key]
        callee = self._g.get(callee_usr)
        if callee is None:
            return TOP
        self._cw_memo[memo_key] = TOP  # mark in-flight (breaks recursion)
        union: "_Top | frozenset[str]" = frozenset()
        saw_caller = False
        for cc in self._g.call_sites_into(callee):
            saw_caller = True
            arg = next((a for a in cc.args if a.position == pos), None)
            if arg is None:  # caller passes nothing knowable -> TOP
                union = TOP
                break
            caller_ctx = (cc.caller.usr, ())
            ts = self._resolve_source(
                caller_ctx,
                arg.src_kind,
                arg.type_usr,
                arg.decl_usr,
                arg.callee_usr,
                arg.type_is_value,
            )
            # A directly-knowable caller arg narrows here: a value local/construct
            # (`B b; f(b)`) resolves via _resolve_source's value shortcut. A
            # caller that forwards one of ITS OWN parameters (a non-value ref/ptr
            # param) resolves to TOP -- and we KEEP it TOP. We deliberately do
            # NOT chase the forwarded param into the caller's callers: param
            # ordinals are not stored (parameters are not indexed as symbols, and
            # only receiver-params carry recv_param_pos), so a forwarded param
            # cannot be soundly mapped to its ordinal in the caller's signature.
            # The previous outgoing-arg-position proxy was UNSOUND under reordered
            # forwarding (`wrapper(p,q){ callee(q,p); }` dropped the real target).
            # Conservative TOP here is sound + monotone; full transitive precision
            # is deferred until param ordinals are persisted (see design doc).
            if ts is TOP:
                union = TOP
                break
            union = union | ts  # type: ignore[operator]
        result: "_Top | frozenset[str]" = (
            TOP if (union is TOP or not saw_caller) else union
        )
        self._cw_memo[memo_key] = result
        return result

    # ------------------------------------------------------------------ #
    # Prune decision
    # ------------------------------------------------------------------ #

    def decide(
        self, ctx: tuple, site: "Site", ds: "DispatchSite"
    ) -> tuple["_Top | frozenset[str]", "Optional[list[Selection]]"]:
        """Prune decision at a virtual dispatch site.

        Returns (gamma_ts, kept_selections):
          - gamma_ts: the resolved TypeSet (TOP or frozenset of USRs)
          - kept_selections: None => KEEP_ALL; list => the pruned subset

        Sound: returns KEEP_ALL on every unsound path (unprunable, TOP, empty
        intersection).

        No subtype expansion: g_ts already contains concrete receiver types
        (construct produces exact types; the engine seeds concrete USRs, not
        declared types).  The dispatch_selection(close_subtypes=True) query
        gives us all candidates including inherited overrides; we then filter
        by whether the *selecting_type* (the concrete class that picks the
        method) is in g_ts.
        """
        if not ds.prunable:
            return TOP, None
        g_ts = self.gamma_for_site(ctx, site)
        if g_ts is TOP:
            return TOP, None

        # Fetch candidates including inherited overrides (close_subtypes=True so
        # E:B with no own rank() shows up as selecting_type=E, target=B::rank).
        ds_closed = self._g.dispatch_selection(ds.declared_target, close_subtypes=True)
        receiver_usrs: set[str] = set(g_ts)  # type: ignore[arg-type]

        kept = [
            s
            for s in ds_closed.candidates
            if s.selecting_type is not None and s.selecting_type.usr in receiver_usrs
        ]
        if not kept:
            return g_ts, None  # empty intersection => KEEP_ALL (sound)

        # Cast to frozenset for the returned gamma_receiver
        gamma_fs: frozenset[str] = frozenset(g_ts)  # type: ignore[arg-type]
        return gamma_fs, kept


class Callable(Entity):
    """Mixin behaviour for function-like entities (free fn, method, template).

    Adds the call graph (``callers``/``callees``) and the parsed signature
    (``return_type``/``arguments``)."""

    @property
    def signature(self) -> Optional[str]:
        """The raw signature string from the index (``type_info``)."""
        return self.sym.type_info

    @property
    def return_type(self) -> Optional[Type]:
        ret, _ = _parse_signature(self.sym.type_info)
        return Type(ret, self._cb) if ret else None

    @property
    def arguments(self) -> list[Type]:
        """Positional parameter types (no names -- see module fidelity note)."""
        _, args = _parse_signature(self.sym.type_info)
        return [Type(a, self._cb) for a in (args or [])]

    @overload
    def callers(
        self,
        limit: int = ...,
        include_instantiations: Literal[False] = ...,
        include_overrides: bool = ...,
    ) -> list["Entity"]: ...

    @overload
    def callers(
        self, limit: int = ..., *, include_instantiations: Literal[True]
    ) -> list[CallerWithContextModel]: ...

    def callers(
        self,
        limit: int = 500,
        include_instantiations: bool = False,
        include_overrides: bool = True,
    ) -> "list[Entity] | list[CallerWithContextModel]":
        """Entities that call this one.

        Includes both direct callers and virtual-dispatch callers (a static call
        to a virtual base method this one overrides), read from the materialised
        ``dispatch_calls`` edges ``resolve`` builds -- so after index + resolve
        this "just works" with no flag. Pass ``include_overrides=False`` for
        direct callers only. Return type: ``list[Entity]``.

        ``include_instantiations=True`` — when this is a template
        method/function, rolls up callers of all implicit-instantiation
        members (ADR-004).  Return type: ``list[CallerWithContextModel]``.

        Each :class:`CallerWithContextModel` carries:
          * ``.entity`` — the caller as a typed :class:`Entity`.
          * ``.via_instantiation`` — the instantiation member (``X<int>::print``)
            as an :class:`Entity`, or ``None`` for direct callers.
          * ``.via_template_args`` — concrete template arguments of the
            instantiation TYPE node (e.g. ``[TemplateArg(0, type, 'int')]``
            for ``X<int>``); empty for direct callers.

        A caller reaching ``X<int>::print`` and a *different* caller reaching
        ``X<double>::print`` both appear, each tagged with its own type."""
        if include_instantiations:
            return self._cb._wrap_cwc(
                self._cb.graph.callers(
                    self.sym,
                    limit=limit,
                    include_instantiations=True,
                )
            )
        return self._cb._wrap_all(
            self._cb.graph.callers(
                self.sym,
                limit=limit,
                include_instantiations=False,
                include_overrides=include_overrides,
            )
        )

    @overload
    def callees(
        self,
        limit: int = ...,
        include_instantiations: Literal[False] = ...,
        include_overrides: bool = ...,
    ) -> list["Entity"]: ...

    @overload
    def callees(
        self, limit: int = ..., *, include_instantiations: Literal[True]
    ) -> list[CallerWithContextModel]: ...

    def callees(
        self,
        limit: int = 500,
        include_instantiations: bool = False,
        include_overrides: bool = True,
    ) -> "list[Entity] | list[CallerWithContextModel]":
        """Entities this one calls, **in source order** (by first call site) for
        the direct callees.

        Includes both direct callees and virtual-dispatch targets: when this
        calls a virtual method B, the concrete override(s) B can reach at run
        time are read from the materialised ``dispatch_calls`` edges ``resolve``
        builds, and appended after the source-ordered direct callees. This is
        what lets :meth:`callgraph` descend through a virtual call into the real
        override. Pass ``include_overrides=False`` for the literal outgoing
        ``calls`` only. Return type: ``list[Entity]``.

        ``include_instantiations=True`` — rolls up callees of all
        implicit-instantiation members.  Return type:
        ``list[CallerWithContextModel]``.  Source-order is NOT applied in the
        opt-in path (rolled-up callee sets span multiple instantiation bodies).

        See :meth:`callers` for a description of the
        :class:`CallerWithContextModel` fields."""
        if include_instantiations:
            return self._cb._wrap_cwc(
                self._cb.graph.callees(
                    self.sym,
                    limit=limit,
                    include_instantiations=True,
                )
            )
        edges = self._cb.graph.edges_out(self.sym, kinds=("calls",), limit=limit)
        edges.sort(key=_call_site_order)
        peers = [e.peer for e in edges]
        if include_overrides:
            # Virtual-dispatch targets have no call site; append after the
            # source-ordered direct callees, deduplicated.
            seen = {p.id for p in peers}
            for p in self._cb.graph._peers(
                self.sym, ("dispatch_calls",), "out", limit
            ):
                if p.id not in seen:
                    seen.add(p.id)
                    peers.append(p)
        return self._cb._wrap_all(peers)

    def callgraph(
        self, depth: Optional[int] = None, *, fanout: int = 500
    ) -> "Iterator[tuple[Entity, int]]":
        """Lazily walk the outbound call graph rooted at this callable.

        A *generator* (nothing is computed until you iterate, and a node is
        expanded only once you consume past it). It yields ``(callee, depth)``
        pairs in **call sequence**: a depth-first pre-order walk where each
        node's callees are visited in source order (the order the calls appear
        in its body). So the stream follows execution flow -- the caller's first
        call, then everything that call reaches, then the caller's second call,
        and so on. ``depth`` is the distance in call edges from the root (direct
        callees are depth 1)::

            for callee, depth in fn.callgraph():          # unbounded
                ...
            for callee, depth in fn.callgraph(depth=10):  # at most 10 levels
                ...

        By default the walk is **unbounded**: it runs until every remaining
        symbol is a leaf -- an external/unresolved (stub) symbol, or any callable
        that calls nothing further. Each entity is surfaced once, the first time
        the sequence reaches it, so cycles and recursion terminate naturally.
        Pass ``depth=N`` to stop expanding after N levels.

        ``fanout`` caps the callees expanded per node (a guard against
        pathological nodes; default 500)."""
        seen = {self.id}

        def _kids(node: "Entity", d: int) -> "Iterator[Entity]":
            if depth is not None and d >= depth:
                return iter(())
            if not isinstance(node, Callable):
                return iter(())  # a call edge can point at a non-callable leaf
            # callees() folds in virtual-dispatch targets by default, so the
            # walk descends through a virtual call into the real override(s).
            return iter(node.callees(limit=fanout))

        # Explicit iterator stack -> iterative DFS pre-order (no recursion limit
        # on deep/recursive call chains). Each frame is (callee-iterator, depth
        # of the callees it yields).
        stack: list[tuple[Iterator[Entity], int]] = [(_kids(self, 0), 1)]
        while stack:
            it, d = stack[-1]
            for callee in it:
                if callee.id in seen:
                    continue
                seen.add(callee.id)
                yield callee, d
                stack.append((_kids(callee, d), d + 1))
                break
            else:
                stack.pop()

    def devirtualized_callgraph(
        self,
        depth: Optional[int] = None,
        *,
        fanout: int = 500,
        expand_virtual: bool = False,
        prune: bool = False,
        assume_closed_world: bool = False,
    ) -> "Iterator[CallStep]":
        """Like :meth:`callgraph`, but each step is a :class:`CallStep` that
        carries the Phase-1 ``dispatch_site`` (the selection map) whenever the
        reached callee is a virtual dispatch point.

        By default the walk is **identical to ``callgraph()``** in the nodes and
        depths it visits -- it descends into the statically-declared callee, NOT
        every dispatch target -- so it is a faithful, behaviour-preserving view
        with dispatch metadata attached (this is Phase 1: no pruning, no
        expansion). Pass ``expand_virtual=True`` to ALSO walk into every concrete
        dispatch target of each virtual callee (the conservative superset).

        Pass ``prune=True`` to run the Phase-2 Gamma propagation engine and
        narrow each virtual hop to its feasible subset.  ``prune=True`` implies
        ``expand_virtual=True``; passing ``prune=True, expand_virtual=False``
        raises ``ValueError``.

        Pass ``assume_closed_world=True`` (requires ``prune=True``) to also apply
        Phase-3b cross-TU param narrowing: the index MUST be whole-program AND
        ``resolve``d, else narrowing is unsound. Asserting closed-world on a
        partial or un-resolved index yields unsound results."""
        if assume_closed_world and not prune:
            raise ValueError("assume_closed_world requires prune=True")

        if prune and expand_virtual is False:
            # The caller explicitly passed expand_virtual=False + prune=True.
            # expand_virtual defaults to False, so we only raise when it is
            # EXPLICITLY set to False (not the default).
            pass  # handled below after prune+expand_virtual logic

        if prune:
            # prune=True implies expand_virtual=True.
            expand_virtual = True
            yield from self._devirt_prune(
                depth=depth,
                fanout=fanout,
                assume_closed_world=assume_closed_world,
            )
            return

        # ------------------------------------------------------------------ #
        # Phase-1 default path (prune=False) — byte-identical to pre-Phase-2
        # ------------------------------------------------------------------ #
        seen = {self.id}

        def _dispatch_site(node: "Entity") -> "Optional[DispatchSiteModel]":
            if isinstance(node, Method) and node.is_virtual:
                return node.dispatch_selection()
            return None

        def _kids(node: "Entity", d: int) -> "Iterator[Entity]":
            if depth is not None and d >= depth:
                return iter(())
            if not isinstance(node, Callable):
                return iter(())
            # Static callees only: this engine does its own virtual expansion
            # (via dispatch_targets below), so it must NOT fold in the
            # dispatch_calls edges that callees() now includes by default.
            callees = node.callees(limit=fanout, include_overrides=False)
            if not expand_virtual:
                return iter(callees)
            # Superset mode: append each virtual callee's concrete targets as
            # siblings (dedup happens in the walk via `seen`).
            out: list[Entity] = []
            for c in callees:
                out.append(c)
                if isinstance(c, Method) and c.is_virtual:
                    out.extend(c.dispatch_targets())
            return iter(out)

        stack: list[tuple[Iterator[Entity], int]] = [(_kids(self, 0), 1)]
        while stack:
            it, d = stack[-1]
            for callee in it:
                if callee.id in seen:
                    continue
                seen.add(callee.id)
                yield CallStep(
                    callee=callee, depth=d, dispatch_site=_dispatch_site(callee)
                )
                stack.append((_kids(callee, d), d + 1))
                break
            else:
                stack.pop()

    def _devirt_prune(
        self,
        depth: Optional[int] = None,
        fanout: int = 500,
        assume_closed_world: bool = False,
    ) -> "Iterator[CallStep]":
        """Phase-2/3 pruned devirtualized callgraph walk (prune=True path).

        Runs _GammaEngine once, then does a DFS where each virtual callee's
        children are narrowed to the pruned candidate set."""
        engine = _GammaEngine(self._cb, assume_closed_world=assume_closed_world)
        engine.analyse(self.sym)

        seen = {self.id}
        root_ctx = (self.sym.usr, ())

        def _dispatch_site(node: "Entity") -> "Optional[DispatchSiteModel]":
            if isinstance(node, Method) and node.is_virtual:
                return node.dispatch_selection()
            return None

        def _kids_pruned(
            node: "Entity", d: int, ctx: tuple
        ) -> "Iterator[tuple[Entity, Optional[list[SelectionModel]], Optional[frozenset[str]]]]":
            """Yield (entity, pruned_candidates, gamma_receiver) tuples."""
            if depth is not None and d >= depth:
                return
            if not isinstance(node, Callable):
                return
            # Static callees only (the Gamma engine expands virtual sites itself).
            callees = node.callees(limit=fanout, include_overrides=False)
            # Build edges map: callee_id -> edge for site lookup (with sites=True
            # so the site provenance is available for Gamma decisions)
            edges = self._cb.graph.edges_out(node.sym, kinds=("calls",), limit=fanout)
            edge_by_dst: dict[int, "Edge"] = {e.dst_id: e for e in edges}

            for c in callees:
                if isinstance(c, Method) and c.is_virtual:
                    # Get the dispatch selection
                    ds_raw = self._cb.graph.dispatch_selection(
                        c.sym, close_subtypes=False
                    )
                    # Find site for this call (use first site)
                    edge = edge_by_dst.get(c.sym.id)
                    if edge is not None and edge.sites:
                        site = edge.sites[0]
                    else:
                        site = None

                    if site is not None:
                        g_ts, kept_sels = engine.decide(ctx, site, ds_raw)
                    else:
                        g_ts, kept_sels = TOP, None

                    gamma_fs: Optional[frozenset[str]] = (
                        None if g_ts is TOP else frozenset(g_ts)  # type: ignore
                    )

                    if kept_sels is None:
                        # KEEP_ALL: yield static callee + all dispatch targets
                        yield c, None, gamma_fs
                        for t in c.dispatch_targets():
                            if t.id != c.id:
                                yield t, None, gamma_fs
                    else:
                        # Pruned: wrap Selection into SelectionModel
                        kept_entities = [
                            SelectionModel(
                                selecting_type=self._cb.wrap(s.selecting_type),
                                target=self._cb.wrap(s.target),
                                inherited=s.inherited,
                            )
                            for s in kept_sels
                        ]
                        # Yield the static callee with the pruned metadata
                        yield c, kept_entities, gamma_fs
                        # Also visit the pruned target methods
                        for s in kept_sels:
                            if s.target is not None:
                                tgt = self._cb.wrap(s.target)
                                if tgt is not None and tgt.id != c.id:
                                    yield tgt, kept_entities, gamma_fs
                else:
                    yield c, None, None

        # DFS with (iterator, depth, context) stack frames
        def root_iter():
            for item in _kids_pruned(self, 0, root_ctx):
                yield item

        stack2: list[tuple[Iterator, int, tuple]] = [(root_iter(), 1, root_ctx)]
        while stack2:
            it, d, ctx = stack2[-1]
            item = next(it, None)
            if item is None:
                stack2.pop()
                continue
            callee, pruned_cands, gamma_fs = item
            if callee.id in seen:
                continue
            seen.add(callee.id)
            ds = _dispatch_site(callee)

            # Build pruned CallStep
            step_pruned = pruned_cands if (ds is not None) else None
            step_gamma = gamma_fs if (ds is not None) else None

            yield CallStep(
                callee=callee,
                depth=d,
                dispatch_site=ds,
                pruned_candidates=step_pruned,
                gamma_receiver=step_gamma,
            )

            # Determine context for callee
            callee_ctx = (callee.sym.usr, ())
            child_it = _kids_pruned(callee, d, callee_ctx)
            stack2.append((child_it, d + 1, callee_ctx))


class Function(Callable):
    """A free function."""


class Method(Callable):
    """A C++ member function."""

    @property
    def owner(self) -> "Optional[Record]":
        """The class/struct/union this method belongs to."""
        if not self.sym.parent_usr:
            return None
        owner = self._cb.get(self.sym.parent_usr)
        return owner if isinstance(owner, Record) else owner  # type: ignore[return-value]

    @property
    def access(self) -> Optional[str]:
        """C++ access specifier: 'public' | 'protected' | 'private'."""
        return self.sym.access

    @property
    def is_pure(self) -> bool:
        """Pure virtual (``= 0``): declared but has no own body."""
        return self.sym.is_pure

    @property
    def is_static(self) -> bool:
        """C++ ``static`` member function: no implicit ``this`` receiver."""
        return self.sym.is_static

    @property
    def is_virtual(self) -> bool:
        """Participates in dynamic dispatch (pure, overrides, or is overridden)."""
        return self._cb.graph.is_virtual_method(self.sym)

    def overrides(self) -> list["Method"]:
        """Base-class methods this method overrides."""
        return [
            e
            for e in self._cb._wrap_all(self._cb.graph.overrides(self.sym))
            if isinstance(e, Method)
        ]

    def overridden_by(self) -> list["Method"]:
        """Methods that directly override this one."""
        return [
            e
            for e in self._cb._wrap_all(self._cb.graph.overridden_by(self.sym))
            if isinstance(e, Method)
        ]

    def dispatch_targets(self) -> list["Method"]:
        """Every concrete method a virtual call here could reach at run time."""
        return [
            e
            for e in self._cb._wrap_all(self._cb.graph.dispatch_targets(self.sym))
            if isinstance(e, Method)
        ]

    def dispatch_selection(self, close_subtypes: bool = False) -> DispatchSiteModel:
        """The Phase-1 selection map for a virtual call to this method: every
        concrete receiver type paired with the target it would dispatch to, plus
        a ``prunable`` flag (see :class:`DispatchSiteModel`). With
        ``close_subtypes=True``, subtypes that inherit (rather than declare) an
        override are included as ``inherited`` candidates. Records data only --
        no pruning happens until Phase 2."""
        ds = self._cb.graph.dispatch_selection(self.sym, close_subtypes=close_subtypes)
        return self._cb._wrap_dispatch_site(ds)


class Constructor(Method):
    """A C++ constructor."""


class Destructor(Method):
    """A C++ destructor."""


class Record(Entity):
    """Base for ``class`` / ``struct`` / ``union`` -- anything with members."""

    def _members(self, access: Optional[str] = None) -> list[Entity]:
        return self._cb._wrap_all(self._cb.graph.members(self.sym, access=access))

    @property
    def access(self) -> Optional[str]:
        return self.sym.access

    @property
    def class_kind(self) -> ClassKind:
        """Abstractness by this record's OWN members -- CONCRETE / ABSTRACT /
        INTERFACE, the entity-graph "three kinds of class"
        (:class:`indexer.entity_graph.ClassKind`).

        Computed from direct members only (own pure-virtual methods + own data
        fields), reusing the canonical ``entity_rollup._is_interface`` rule, so
        it is consistent with the Layer-1 entity graph's partitioning and is
        what the :meth:`CodeBase.klass` / :meth:`CodeBase.abstract_class` /
        :meth:`CodeBase.interface` selectors filter on.  This is orthogonal to
        the inheritance-aware :attr:`is_abstract` heuristic below (which also
        considers pure methods inherited but not overridden)."""
        return _record_class_kind(self._cb.graph._c, self.sym.usr)

    @property
    def is_interface(self) -> bool:
        """True if this record is a pure interface -- all own methods are
        pure-virtual (a defaulted virtual destructor is allowed) and it has no
        data members. Equivalent to ``class_kind is ClassKind.INTERFACE``."""
        return self.class_kind is ClassKind.INTERFACE

    @property
    def fields(self) -> list[Field]:
        """Data members (fields)."""
        return [e for e in self._members() if isinstance(e, Field)]

    @property
    def methods(self) -> "list[Method | FunctionTemplate]":
        """Member functions, including constructors/destructors and member
        function templates.

        A member function template (e.g. ``Cache::set<T>``) is wrapped as
        FunctionTemplate, a sibling of Method rather than a subclass, so it has
        to be admitted explicitly. Every FunctionTemplate reachable here arrived
        via a ``method_of`` edge, so it is by definition a member template."""
        return [
            e
            for e in self._members()
            if isinstance(e, (Method, FunctionTemplate))
        ]

    def members(self, access: Optional[str] = None) -> list[Entity]:
        """All members; `access` filters to public/protected/private."""
        return self._members(access=access)

    # -- inheritance --------------------------------------------------------- #

    def bases(self, recursive: bool = False) -> list["Record"]:
        """Base classes. recursive=True walks the whole ancestry."""
        syms = self._cb.graph.bases(self.sym, direct=not recursive)
        return [e for e in self._cb._wrap_all(syms) if isinstance(e, Record)]

    def derived(self, recursive: bool = False) -> list["Record"]:
        """Subclasses. recursive=True walks the whole subtree."""
        syms = self._cb.graph.subclasses(self.sym, direct=not recursive)
        return [e for e in self._cb._wrap_all(syms) if isinstance(e, Record)]

    @property
    def parents(self) -> list["Record"]:
        """Direct base classes."""
        return self.bases(recursive=False)

    @property
    def ancestors(self) -> list["Record"]:
        """All transitive base classes."""
        return self.bases(recursive=True)

    @property
    def children(self) -> list["Record"]:
        """All subclasses that inherit from this class, directly or indirectly."""
        return self.derived(recursive=True)

    @property
    def is_abstract(self) -> bool:
        """True if the record cannot be instantiated -- it declares a pure
        virtual method, or inherits one it does not override.

        Heuristic (thin layer): a class is abstract if any of its own methods is
        pure, or any ancestor's pure method has no same-spelling override here.

        Only plain Methods participate -- a function template can never be pure
        virtual, and `.methods` now also yields FunctionTemplate members."""
        own = [m for m in self.methods if isinstance(m, Method)]
        if any(m.is_pure for m in own):
            return True
        overridden = {m.spelling for m in own if not m.is_pure}
        for anc in self.ancestors:
            for m in anc.methods:
                if isinstance(m, Method) and m.is_pure and m.spelling not in overridden:
                    return True
        return False


class Class(Record):
    """A C++ ``class`` record.

    Also the base for the other two keyword record types -- :class:`Struct` and
    :class:`Union` -- so ``isinstance(x, Class)`` is true for any of the three,
    while the concrete type (and ``.kind``) tells the keyword apart.  Note this
    *low-level* layer keeps the C++ keyword distinction; abstractness
    (class / abstract-class / interface) is the *high-level* entity-graph axis
    (:class:`indexer.entity_graph.EntityType`), reachable from an entity node via
    ``EntityNode.as_model()``."""


class Struct(Class):
    """A C++ ``struct`` record (a :class:`Class` whose ``.kind`` is ``struct``)."""


class Union(Class):
    """A C++ ``union`` record (a :class:`Class` whose ``.kind`` is ``union``)."""


class Field(Entity):
    """A data member of a record."""

    @property
    def type(self) -> Optional[Type]:
        return Type(self.sym.type_info, self._cb) if self.sym.type_info else None

    @property
    def access(self) -> Optional[str]:
        return self.sym.access

    @property
    def owner(self) -> "Optional[Record]":
        if not self.sym.parent_usr:
            return None
        owner = self._cb.get(self.sym.parent_usr)
        return owner if isinstance(owner, Record) else owner  # type: ignore[return-value]


class Enum(Entity):
    """An enumeration."""

    @property
    def constants(self) -> list["EnumConstant"]:
        return [
            e
            for e in self._cb._wrap_all(self._cb.graph.members(self.sym))
            if isinstance(e, EnumConstant)
        ]


class EnumConstant(Entity):
    """A single enumerator within an enum."""

    @property
    def owner(self) -> Optional[Enum]:
        if not self.sym.parent_usr:
            return None
        owner = self._cb.get(self.sym.parent_usr)
        return owner if isinstance(owner, Enum) else None


class Typedef(Entity):
    """A typedef or type alias."""

    @property
    def underlying_type(self) -> Optional[Type]:
        return Type(self.sym.type_info, self._cb) if self.sym.type_info else None

    def aliased(self) -> "Optional[Entity]":
        """The entity this alias directly names -- the *original* type it refers
        to -- or ``None`` when the underlying type is a builtin / unindexed type
        (``typedef int Integer;``) or is otherwise not indexed.

        Resolved from the structural ``uses`` edge the indexer emits from an
        alias to its underlying named type, so it returns the precise indexed
        declaration -- including a concrete *template instance*::

            typedef Widget WidgetAlias;   ->  Class('Widget')
            using GadgetAlias = Gadget;    ->  Struct('Gadget')
            using ColorAlias = Color;      ->  Enum('Color')
            using IntBox = Box<int>;       ->  Box<int> instance (a Record whose
                                               .template_arguments give [int])
            using Handle = OtherAlias;     ->  Typedef('OtherAlias')

        Single-level by design, matching how the type is *written*: an alias of
        an alias resolves to the next alias, not through to the final record --
        chain ``.aliased()`` to walk the chain to its end."""
        for sym in self._cb.graph.neighbors(
            self.sym, kinds=("uses",), direction="out"
        ):
            e = self._cb.wrap(sym)
            if e is not None:
                return e
        return None


class Namespace(Entity):
    """A C++ namespace.

    Beyond :meth:`members` (every symbol declared in the namespace, of any
    kind), the accessors below return typed, kind-filtered views: records vs.
    class templates, plain functions vs. function templates, type aliases,
    enums, variables vs. constants, and nested namespaces."""

    def members(self, pattern: Optional[str] = None) -> list[Entity]:
        """Every symbol declared directly in this namespace, of any kind
        (records, templates, functions, aliases, enums, variables, nested
        namespaces, ...). The kind-specific accessors are filtered views of
        this set.

        ``pattern`` narrows to members whose name contains it (case-insensitive
        substring of spelling / qualified name / display name); ``None`` returns
        all."""
        return [
            e
            for e in self._cb._wrap_all(self._cb.graph.members(self.sym))
            if _name_contains(e, pattern)
        ]

    def functions(
        self, pattern: Optional[str] = None
    ) -> "list[Function | FunctionTemplate]":
        """Free functions in this namespace, INCLUDING free function templates
        (FunctionTemplate is a sibling of Function, not a subclass). Use
        :meth:`function_templates` for the template-only view.

        ``pattern`` narrows to functions whose name contains it (case-insensitive
        substring); ``None`` returns all."""
        return [
            e
            for e in self.members(pattern)
            if isinstance(e, (Function, FunctionTemplate))
            and not isinstance(e, Method)
        ]

    def function_templates(
        self, pattern: Optional[str] = None
    ) -> "list[FunctionTemplate]":
        """Free function templates declared directly in this namespace
        (``template <class T> T identity(T);``) -- the template-only subset of
        :meth:`functions`. ``pattern`` narrows by name-contains; ``None`` = all."""
        return [
            e
            for e in self.members(pattern)
            if isinstance(e, FunctionTemplate) and not isinstance(e, Method)
        ]

    def classes(self, pattern: Optional[str] = None) -> list[Record]:
        """Plain class / struct / union records declared directly in this
        namespace -- EXCLUDING class templates (see :meth:`class_templates`) and
        concrete template instantiations (``Box<int>``). For the template
        primaries use :meth:`class_templates`.

        ``pattern`` narrows to records whose name contains it (case-insensitive
        substring), so ``ns.classes("Box")`` returns records named like ``Box``;
        ``None`` returns all."""
        return [
            e
            for e in self.members(pattern)
            if isinstance(e, Record)
            and not isinstance(e, ClassTemplate)
            and not e.is_instantiation
        ]

    def class_templates(
        self, pattern: Optional[str] = None
    ) -> "list[ClassTemplate]":
        """Class templates declared directly in this namespace
        (``template <class T> class Box``). ``pattern`` narrows by name-contains;
        ``None`` = all."""
        return [
            e for e in self.members(pattern) if isinstance(e, ClassTemplate)
        ]

    def type_aliases(self, pattern: Optional[str] = None) -> "list[Typedef]":
        """``typedef`` / ``using`` type aliases declared directly in this
        namespace. Resolve each to the type it names with
        :meth:`Typedef.aliased`. ``pattern`` narrows by name-contains;
        ``None`` = all."""
        return [e for e in self.members(pattern) if isinstance(e, Typedef)]

    def enums(self, pattern: Optional[str] = None) -> "list[Enum]":
        """Enumerations declared directly in this namespace. ``pattern`` narrows
        by name-contains; ``None`` = all."""
        return [e for e in self.members(pattern) if isinstance(e, Enum)]

    def variables(self, pattern: Optional[str] = None) -> "list[Variable]":
        """All namespace-scope variables declared directly in this namespace,
        both mutable and const/constexpr. :meth:`constants` is the const subset.
        ``pattern`` narrows by name-contains; ``None`` = all."""
        return [e for e in self.members(pattern) if isinstance(e, Variable)]

    def constants(self, pattern: Optional[str] = None) -> "list[Variable]":
        """Namespace-scope variables whose declared type is *top-level* const
        (``const int kMax``, ``constexpr double kPi``) -- a best-effort const
        subset of :meth:`variables`. A pointer-to-const bound to a mutable
        variable (``const char *name``) is NOT a constant and is excluded.
        ``pattern`` narrows by name-contains; ``None`` = all."""
        return [
            e
            for e in self.members(pattern)
            if isinstance(e, Variable)
            and e.type is not None
            and _is_top_level_const(e.type.spelling)
        ]

    def namespaces(self, pattern: Optional[str] = None) -> "list[Namespace]":
        """Namespaces nested directly in this one. ``pattern`` narrows by
        name-contains; ``None`` = all."""
        return [e for e in self.members(pattern) if isinstance(e, Namespace)]


class Variable(Entity):
    """A global / namespace-scope variable."""

    @property
    def type(self) -> Optional[Type]:
        return Type(self.sym.type_info, self._cb) if self.sym.type_info else None


class Macro(Entity):
    """A preprocessor macro definition."""


class _TemplateMixin:
    """Shared specialization/instantiation traversal for templated entities."""

    @property
    def parameters(self: Entity) -> list[TemplateParam]:  # type: ignore[misc]
        """The formal template parameters of this template, in declaration order
        (e.g. ``[TemplateParam(#0 type T)]`` for ``template <class T>``)."""
        return self._cb.graph.template_params(self.sym)

    def specializations(self: Entity) -> list[Entity]:  # type: ignore[misc]
        """Explicit/partial specializations of this template (incoming
        ``specializes``) -- e.g. ``template <> class Wrapper<bool> {...}``.

        Each specialization is a :class:`Record`; use its ``template_arguments``
        to see what it specializes on."""
        return [
            e
            for e in self._cb._wrap_all(
                self._cb.graph.neighbors(
                    self.sym, kinds=("specializes",), direction="in"
                )
            )
            if isinstance(e, Record)
        ]

    def instantiations(self: Entity) -> list[Entity]:  # type: ignore[misc]
        """Concrete instantiations of this template -- the instance *types*
        (e.g. ``template class Wrapper<int>;`` yields the ``Wrapper<int>``
        record) -- NOT the functions that trigger an instantiation.

        Incoming ``instantiates`` edges have multiple kinds of source:
        explicit-instantiation records (``template class Foo<int>;``), ADR-004
        implicit-instantiation type nodes (``X<int>`` with ``is_instantiation=1``
        from call-site minting), and functions that use the template (the
        ``instantiation_sites`` set). This returns only *type-like* sources:
        any incoming ``instantiates`` source that is a Record (not a Callable).
        Use :meth:`instantiation_sites` for the callable sources.

        Each instance is a :class:`Record` whose ``template_arguments`` give the
        concrete bindings."""
        return [
            e
            for e in self._cb._wrap_all(
                self._cb.graph.neighbors(
                    self.sym, kinds=("instantiates",), direction="in"
                )
            )
            if isinstance(e, Record)
        ]

    def instantiation_sites(self: Entity) -> list[Entity]:  # type: ignore[misc]
        """Functions that instantiate this template by using it in their body
        (incoming ``instantiates`` whose source is a callable). Pair with
        :meth:`instantiations` for the concrete instance nodes."""
        return [
            e
            for e in self._cb._wrap_all(
                self._cb.graph.neighbors(
                    self.sym, kinds=("instantiates",), direction="in"
                )
            )
            if isinstance(e, Callable)
        ]


class FunctionTemplate(Callable, _TemplateMixin):
    """A free (non-member) function template."""


class ClassTemplate(Record, _TemplateMixin):
    """A class template."""


# --------------------------------------------------------------------------- #
# kind -> entity-class dispatch table
# --------------------------------------------------------------------------- #

_KIND_TO_CLASS: dict[str, type] = {
    "function": Function,
    "method": Method,
    "constructor": Constructor,
    "destructor": Destructor,
    "class": Class,
    "struct": Struct,
    "union": Union,
    "class-template": ClassTemplate,
    "function-template": FunctionTemplate,
    "member": Field,
    "enum": Enum,
    "enum-constant": EnumConstant,
    "typedef": Typedef,
    "type-alias": Typedef,
    "namespace": Namespace,
    "variable": Variable,
    "macro": Macro,
}


# --------------------------------------------------------------------------- #
# signature / type-name parsing (thin, best-effort)
# --------------------------------------------------------------------------- #

_INF = float("inf")


def _call_site_order(edge: Edge) -> tuple[int, float, float]:
    """Sort key placing a call edge by its earliest call site (line, col).

    Edges whose sites carry a location sort before those that do not (bucket 0
    vs 1); within a bucket, by line then col. Python's stable sort preserves the
    incoming order for ties (and for the no-location bucket)."""
    best_line, best_col = _INF, _INF
    for s in edge.sites:
        line = s.line if s.line is not None else _INF
        col = s.col if s.col is not None else _INF
        if (line, col) < (best_line, best_col):
            best_line, best_col = line, col
    bucket = 1 if best_line == _INF else 0
    return (bucket, best_line, best_col)


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep`, but only at bracket depth 0 (respects <>, (), [])."""
    out, depth, buf = [], 0, []
    pairs = {"<": ">", "(": ")", "[": "]"}
    closers = set(pairs.values())
    for ch in text:
        if ch in pairs:
            depth += 1
        elif ch in closers:
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _parse_signature(sig: Optional[str]) -> tuple[Optional[str], Optional[list[str]]]:
    """Split a function ``type_info`` (``RET (ARGS)``) into (return, [args]).

    Returns ``(sig, None)`` when there is no top-level argument list (the string
    is not a function signature we understand). ``void`` and empty argument
    lists become ``[]``."""
    if not sig:
        return None, None
    depth, start, end = 0, None, None
    for i, ch in enumerate(sig):
        if ch == "(":
            if depth == 0 and start is None:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                end = i
                break
    if start is None or end is None:
        return sig.strip(), None
    ret = sig[:start].strip()
    inside = sig[start + 1 : end].strip()
    if inside in ("", "void"):
        return ret, []
    return ret, _split_top_level(inside)


def _base_type_name(spelling: str) -> str:
    """Reduce a type spelling to its bare base identifier.

    ``const std::string &`` -> ``std::string``; ``Foo<int> *`` -> ``Foo``."""
    s = spelling
    s = re.sub(r"\b(const|volatile|struct|class|enum|union)\b", " ", s)
    s = re.sub(r"<.*>", "", s)  # drop template arguments
    s = s.split("[")[0]  # drop array dims
    s = s.replace("*", " ").replace("&", " ")
    parts = s.split()
    return parts[-1] if parts else ""


def _is_top_level_const(spelling: str) -> bool:
    """True when the *variable itself* is const/constexpr (top-level const), not
    merely pointing/referring to const data. Best-effort, from the type spelling:

    * ``const int`` / ``const double``   -> True  (constexpr spells as const T)
    * ``char *const``                    -> True  (const applies to the pointer)
    * ``const char *``                   -> False (pointer-to-const; var mutable)
    * ``int``                            -> False

    For a pointer type the const must appear AFTER the last ``*`` to be top-level;
    for a non-pointer value type any ``const`` is top-level."""
    if "const" not in spelling:
        return False
    star = spelling.rfind("*")
    if star == -1:  # value type: leading/only const is top-level
        return True
    return "const" in spelling[star:]


def _name_contains(entity: "Entity", pattern: Optional[str]) -> bool:
    """True when ``pattern`` (case-insensitive substring) occurs in the entity's
    bare spelling, its fully-qualified name, or its display name (which carries
    template arguments). ``pattern`` of ``None`` matches everything -- the
    unfiltered case. Used by the :class:`Namespace` member accessors so
    ``ns.classes("Box")`` returns records whose name contains ``Box``."""
    if pattern is None:
        return True
    p = pattern.lower()
    return (
        p in entity.spelling.lower()
        or p in entity.name.lower()
        or p in entity.display_name.lower()
    )


# --------------------------------------------------------------------------- #
# Typed-selector support: class-kind, signature parsing, type/name matching
# --------------------------------------------------------------------------- #


#: ClassKind -> the ``entity_node.kind`` ids (storage.entity_kind seed) that
#: realize it: a CONCRETE record is a plain class/struct (1) or a union (4);
#: ABSTRACT is abstract_class (2); INTERFACE is interface (3). Lets the record
#: selectors push the abstractness filter straight into the entity_node JOIN.
_CLASSKIND_ENTITY_IDS: "dict[ClassKind, tuple[int, ...]]" = {
    ClassKind.CONCRETE: (1, 4),
    ClassKind.ABSTRACT: (2,),
    ClassKind.INTERFACE: (3,),
}


def _record_class_kind(conn, record_usr: str) -> ClassKind:
    """Classify a record CONCRETE / ABSTRACT / INTERFACE by its OWN members.

    ``record_usr`` is the record's own USR -- its members are exactly the
    symbols whose ``parent_usr`` equals it. The caller already holds it
    (``self.sym.usr``), so it is passed straight in: the query binds it
    directly (``WHERE parent_usr = ?``), no nested ``SELECT usr ... WHERE id``.

    Mirrors ``entity_rollup._is_interface`` / ``entity_graph._class_kind``
    exactly: method kind=21, field kind=6, ``is_pure``; destructors (kind=25)
    are excluded by the ``kind=21`` filter. A record with no own pure-virtual
    method is CONCRETE; with a pure method it is an INTERFACE when it has no
    non-pure method and no data field, else ABSTRACT.

    One index-backed query (idx_symbol_parent) returns this record's OWN
    members; the kind/is_pure split is done in Python over the handful of rows.
    The old form ran three ``COUNT(*) ... AND kind = 21`` queries, but the
    ``kind = 21`` predicate let SQLite pick idx_symbol_kind -- kind=21 (method)
    matches roughly a third of EVERY symbol in the index, so each query
    effectively scanned the whole symbol table. That made .klass /
    .abstract_class / .interface / .record / .struct O(symbols) (tens of ms per
    candidate on a multi-million-symbol DB). Selecting on ``parent_usr`` alone
    forces the selective idx_symbol_parent (only this record's members)."""
    rows = conn.execute(
        "SELECT kind, is_pure FROM symbol WHERE parent_usr = ?",
        (record_usr,),
    ).fetchall()
    pure = non_pure = fields = 0
    for kind, is_pure in rows:
        if kind == 21:
            if is_pure == 1:
                pure += 1
            elif is_pure == 0:
                non_pure += 1
        elif kind == 6:
            fields += 1
    if not pure:
        return ClassKind.CONCRETE
    return (
        ClassKind.INTERFACE
        if non_pure == 0 and fields == 0
        else ClassKind.ABSTRACT
    )


_WS_RE = re.compile(r"\s+")
#: a namespace / scope qualifier prefix (``std::`` / ``app::`` / ``Foo::``).
_NS_RE = re.compile(r"\b[A-Za-z_]\w*::")


def _type_text(x) -> str:
    """Coerce a type argument (str / :class:`Type` / :class:`Entity`) to its
    spelling for normalized comparison."""
    if isinstance(x, Type):
        return x.spelling
    if isinstance(x, Entity):
        return x.name
    return str(x)


def _norm_type(text: str) -> str:
    """Normalize a type spelling for tolerant comparison: fold ``std::__1::``
    to ``std::``, collapse whitespace, and drop spaces adjacent to ``:: * & < >
    , ( )`` so ``const std::string &`` == ``const std::string&``."""
    s = text.replace("std::__1::", "std::")
    s = _WS_RE.sub(" ", s).strip()
    for p in ("::", "*", "&", "<", ">", ",", "(", ")"):
        s = s.replace(" " + p, p).replace(p + " ", p)
    return s


def _strip_ns(text: str) -> str:
    """Drop every namespace / scope qualifier so ``std::string`` -> ``string``,
    ``app::Box<app::Cache>`` -> ``Box<Cache>``."""
    return _NS_RE.sub("", text)


def _types_match(want, got) -> bool:
    """Whether two type spellings match after normalization, with a
    namespace-stripped fallback (``std::string`` matches ``string``)."""
    nw = _norm_type(_type_text(want))
    ng = _norm_type(_type_text(got))
    if nw == ng:
        return True
    return _strip_ns(nw) == _strip_ns(ng)


def _split_ret_and_name(head: str) -> tuple[Optional[str], str]:
    """Split a ``RET NAME`` head (the part before a signature's ``(``) into
    ``(return-type, name)``. The name is the final whitespace-separated token at
    bracket depth 0 (so a return type carrying ``<...>`` with spaces is kept
    whole); a head with no top-level space is all name, no return type."""
    head = head.strip()
    depth = 0
    pairs = {"<": ">", "(": ")", "[": "]"}
    closers = set(pairs.values())
    split_at: Optional[int] = None
    for i, ch in enumerate(head):
        if ch in pairs:
            depth += 1
        elif ch in closers:
            depth -= 1
        elif ch.isspace() and depth == 0:
            split_at = i
    if split_at is None:
        return None, head
    return (head[:split_at].strip() or None), head[split_at + 1 :].strip()


def _strip_template_args(name: str) -> str:
    """Drop a trailing ``<...>`` from a (qualified) name: ``app::Box<int>`` ->
    ``app::Box``."""
    i = name.find("<")
    return name[:i] if i != -1 else name


def _unqualify(name: str) -> str:
    """The final ``::`` segment of a name, template args dropped:
    ``app::Box::get`` -> ``get``."""
    return _strip_template_args(name).rsplit("::", 1)[-1]


def _name_matches(requested: str, entity: "Entity") -> bool:
    """Whether ``entity`` answers to ``requested`` -- exact qualified name or
    spelling; a bare (unqualified) request matches the spelling / name tail; a
    qualified request matches when the entity's name ends with it on a ``::``
    boundary (``Box::get`` matches ``app::Box::get``)."""
    req = _strip_template_args(requested)
    nm = _strip_template_args(entity.name)
    if nm == req or entity.spelling == req:
        return True
    if "::" not in req:
        return _unqualify(nm) == req
    return nm.endswith("::" + req)


def _is_instance(e: "Entity") -> bool:
    """Whether ``e`` is a template instantiation / specialization record (a
    record carrying template arguments, but NOT the primary class template)."""
    return (
        isinstance(e, Record)
        and not isinstance(e, ClassTemplate)
        and bool(e.template_arguments)
    )


def _ta_text(ta: TemplateArg) -> str:
    """The type-spelling / value text of a template argument for matching."""
    return ta.literal or ""


def _parse_instance_name(
    name: Optional[str],
) -> tuple[Optional[str], Optional[list[str]]]:
    """Split ``Box<int>`` / ``app::Box<int, char>`` into ``(base, [args])``; a
    name with no ``<`` yields ``(name, None)`` (match any instantiation)."""
    if name is None:
        return None, None
    i = name.find("<")
    if i == -1:
        return name, None
    base = name[:i].strip()
    inside = name[i + 1 :]
    j = inside.rfind(">")
    if j != -1:
        inside = inside[:j]
    args = _split_top_level(inside) if inside.strip() else []
    return base, args


@dataclass(frozen=True)
class Signature:
    """A parsed, normalized signature constraint used by the typed selectors.

    ``ret`` is the desired return-type spelling (``None`` = don't constrain the
    return); ``params`` is the ordered list of desired parameter-type spellings
    (``None`` = don't constrain the parameter list; ``[]`` = require zero
    parameters). Types match by *normalized spelling* (see :func:`_types_match`).

    Three constructors mirror the selector calling conventions:

      * :meth:`from_parts`      -- ``Signature.from_parts(ret="int", params=["int"])``
      * :meth:`parse_full`      -- ``Signature.parse_full("int app::f(int)")``
        (also returns the qualified name to look up)
      * :meth:`parse_ret_params`-- ``Signature.parse_ret_params("int(std::string)")``
    """

    ret: Optional[str] = None
    params: Optional[list[str]] = None

    @classmethod
    def from_parts(cls, ret=None, params=None) -> "Signature":
        """Build from an optional return type and optional parameter-type list
        (each a str / :class:`Type` / :class:`Entity`)."""
        return cls(
            ret=_type_text(ret) if ret is not None else None,
            params=[_type_text(p) for p in params] if params is not None else None,
        )

    @classmethod
    def parse_ret_params(cls, sig: str) -> "Signature":
        """Parse a ``ret(param, ...)`` string (form 3), e.g.
        ``"int(std::string)"``.  An empty return (``"(int)"``) leaves the return
        unconstrained.  Raises ``ValueError`` on a string with no ``(...)``."""
        ret, params = _parse_signature(sig)
        if params is None:
            raise ValueError(
                f"unparseable signature {sig!r}; expected 'ret(param, ...)'"
            )
        return cls(ret=ret or None, params=params)

    @classmethod
    def parse_full(cls, sig: str) -> "tuple[Signature, str]":
        """Parse a full ``ret name(param, ...)`` string (form 2), e.g.
        ``"int app::func(int, std::string)"``, returning the ``Signature`` and
        the embedded qualified name.  Raises ``ValueError`` when there is no
        ``(...)`` or no function name."""
        head, params = _parse_signature(sig)
        if params is None:
            raise ValueError(
                f"unparseable signature {sig!r}; expected 'ret name(param, ...)'"
            )
        ret, name = _split_ret_and_name(head)
        if not name:
            raise ValueError(f"signature {sig!r} has no function name")
        return cls(ret=ret, params=params), name

    def matches(self, entity: "Callable") -> bool:
        """Whether ``entity``'s return type and ordered parameter types satisfy
        this constraint (an unconstrained dimension always passes)."""
        if self.ret is not None:
            rt = entity.return_type
            got = rt.spelling if rt is not None else ""
            if not _types_match(self.ret, got):
                return False
        if self.params is not None:
            args = entity.arguments
            if len(args) != len(self.params):
                return False
            for want, got_t in zip(self.params, args):
                if not _types_match(want, got_t.spelling):
                    return False
        return True

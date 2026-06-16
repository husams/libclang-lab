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
from typing import Iterable, Optional, Sequence

from .query import GraphQuery, Site, Sym, open_query

__all__ = [
    "CodeBase", "open_codebase", "Location", "Type", "Reference",
    "Entity", "Callable", "Function", "Method", "Constructor", "Destructor",
    "Record", "Class", "Field", "Enum", "EnumConstant", "Typedef",
    "Namespace", "Variable", "Macro", "FunctionTemplate", "ClassTemplate",
]

#: kinds that name a type a `Type` can resolve to
_TYPE_DECL_KINDS = frozenset({"class", "struct", "union", "enum", "typedef",
                              "type-alias", "class-template"})


# --------------------------------------------------------------------------- #
# Plain value types
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Location:
    """A resolved source position (file + line + col)."""
    file: Optional[str]
    line: Optional[int]
    col: Optional[int]

    @property
    def loc(self) -> str:
        if not self.file:
            return "<no-location>"
        import os
        base = os.path.basename(self.file)
        return f"{base}:{self.line}" if self.line else base

    def to_dict(self) -> dict:
        return {"file": self.file, "line": self.line, "col": self.col}

    def __repr__(self) -> str:
        return f"Location({self.loc})"


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
        cands = [e for e in self._cb.find(base, limit=50)
                 if e.kind in _TYPE_DECL_KINDS and e.name == base]
        if not cands:
            cands = [e for e in self._cb.find(base, limit=50)
                     if e.kind in _TYPE_DECL_KINDS]
        if not cands:
            return None
        cands.sort(key=lambda e: (not e.is_definition,))
        return cands[0]

    def __repr__(self) -> str:
        return f"Type({self.spelling!r})"


@dataclass(frozen=True)
class Reference:
    """A place that refers to an entity: who, how, and where."""
    by: "Entity"            # the referring entity
    kind: str               # 'calls' | 'uses'
    sites: Sequence[Site]   # concrete file:line locations of the reference

    def __repr__(self) -> str:
        where = self.sites[0].loc if self.sites else self.by.location.loc
        return f"Reference({self.kind} by {self.by.name} @{where})"


# --------------------------------------------------------------------------- #
# The codebase handle / entity factory
# --------------------------------------------------------------------------- #

def open_codebase(db_path: Optional[str] = None,
                  require_edges: bool = False) -> "CodeBase":
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

    # -- lookup -------------------------------------------------------------- #

    def get(self, ident) -> "Optional[Entity]":
        """Fetch one entity by id, USR, Sym, or Entity (pass-through)."""
        if isinstance(ident, Entity):
            return ident
        return self.wrap(self.graph.get(ident))

    def find(self, pattern: str, kind: Optional[str] = None,
             limit: int = 50) -> "list[Entity]":
        """Fuzzy qualified-name lookup -> typed entities (see GraphQuery.find)."""
        return self._wrap_all(self.graph.find(pattern, kind=kind, limit=limit))

    def by_name(self, spelling: str, kind: Optional[str] = None) -> "list[Entity]":
        """Exact-spelling lookup -> typed entities."""
        return self._wrap_all(self.graph.by_name(spelling, kind=kind))

    def symbols_in_file(self, path_substr: str, limit: int = 500) -> "list[Entity]":
        return self._wrap_all(self.graph.symbols_in_file(path_substr, limit=limit))

    # convenience single-kind lookups -------------------------------------- #

    def function(self, name: str) -> "Optional[Function]":
        """The first free function matching `name`, or None."""
        hits = [e for e in self.find(name) if isinstance(e, Function)
                and not isinstance(e, Method)]
        return hits[0] if hits else None

    def klass(self, name: str) -> "Optional[Record]":
        """The first class/struct/union matching `name`, or None."""
        hits = [e for e in self.find(name) if isinstance(e, Record)]
        return hits[0] if hits else None

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
    def is_stub(self) -> bool:
        return self.sym.is_stub

    # -- locations ----------------------------------------------------------- #

    def _locations(self):
        return self._cb.graph.def_decl_locations(self.sym)

    @property
    def location(self) -> Location:
        """Best-known location (definition, else declaration)."""
        return Location(self.sym.file, self.sym.line, self.sym.col)

    @property
    def definition(self) -> Optional[Location]:
        """Where the entity is defined, or None if only declared."""
        defn, _ = self._locations()
        return Location(*defn) if defn else None

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
        return Location(*decl)

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
        decl = self.declaration
        if decl is not None:
            d["declaration"] = decl.to_dict()
        return d

    def __eq__(self, other) -> bool:
        return isinstance(other, Entity) and other.sym.id == self.sym.id

    def __hash__(self) -> int:
        return hash(self.sym.id)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r} @{self.location.loc})"


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

    def callers(self, limit: int = 500) -> list["Entity"]:
        """Entities that call this one."""
        return self._cb._wrap_all(self._cb.graph.callers(self.sym, limit=limit))

    def callees(self, limit: int = 500) -> list["Entity"]:
        """Entities this one calls."""
        return self._cb._wrap_all(self._cb.graph.callees(self.sym, limit=limit))


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
    def is_virtual(self) -> bool:
        """Participates in dynamic dispatch (pure, overrides, or is overridden)."""
        return self._cb.graph.is_virtual_method(self.sym)

    def overrides(self) -> list["Method"]:
        """Base-class methods this method overrides."""
        return [e for e in self._cb._wrap_all(self._cb.graph.overrides(self.sym))
                if isinstance(e, Method)]

    def overridden_by(self) -> list["Method"]:
        """Methods that directly override this one."""
        return [e for e in
                self._cb._wrap_all(self._cb.graph.overridden_by(self.sym))
                if isinstance(e, Method)]

    def dispatch_targets(self) -> list["Method"]:
        """Every concrete method a virtual call here could reach at run time."""
        return [e for e in
                self._cb._wrap_all(self._cb.graph.dispatch_targets(self.sym))
                if isinstance(e, Method)]


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
    def fields(self) -> list[Field]:
        """Data members (fields)."""
        return [e for e in self._members() if isinstance(e, Field)]

    @property
    def methods(self) -> list[Method]:
        """Member functions, including constructors/destructors."""
        return [e for e in self._members() if isinstance(e, Method)]

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
        pure, or any ancestor's pure method has no same-spelling override here."""
        own = self.methods
        if any(m.is_pure for m in own):
            return True
        overridden = {m.spelling for m in own if not m.is_pure}
        for anc in self.ancestors:
            for m in anc.methods:
                if m.is_pure and m.spelling not in overridden:
                    return True
        return False


class Class(Record):
    """A class, struct, or union (kind disambiguates via ``.kind``)."""


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
        return [e for e in self._cb._wrap_all(self._cb.graph.members(self.sym))
                if isinstance(e, EnumConstant)]


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


class Namespace(Entity):
    """A C++ namespace."""

    def members(self) -> list[Entity]:
        return self._cb._wrap_all(self._cb.graph.members(self.sym))

    @property
    def functions(self) -> list[Function]:
        return [e for e in self.members()
                if isinstance(e, Function) and not isinstance(e, Method)]

    @property
    def classes(self) -> list[Record]:
        return [e for e in self.members() if isinstance(e, Record)]


class Variable(Entity):
    """A global / namespace-scope variable."""

    @property
    def type(self) -> Optional[Type]:
        return Type(self.sym.type_info, self._cb) if self.sym.type_info else None


class Macro(Entity):
    """A preprocessor macro definition."""


class _TemplateMixin:
    """Shared specialization/instantiation traversal for templated entities."""

    def specializations(self: Entity) -> list[Entity]:  # type: ignore[misc]
        """Explicit/partial specializations of this template (incoming
        ``specializes``)."""
        return self._cb._wrap_all(
            self._cb.graph.neighbors(self.sym, kinds=("specializes",),
                                     direction="in"))

    def instantiations(self: Entity) -> list[Entity]:  # type: ignore[misc]
        """Concrete instantiations of this template (incoming ``instantiates``)."""
        return self._cb._wrap_all(
            self._cb.graph.neighbors(self.sym, kinds=("instantiates",),
                                     direction="in"))


class FunctionTemplate(Callable, _TemplateMixin):
    """A function template."""


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
    "struct": Class,
    "union": Class,
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
    inside = sig[start + 1:end].strip()
    if inside in ("", "void"):
        return ret, []
    return ret, _split_top_level(inside)


def _base_type_name(spelling: str) -> str:
    """Reduce a type spelling to its bare base identifier.

    ``const std::string &`` -> ``std::string``; ``Foo<int> *`` -> ``Foo``."""
    s = spelling
    s = re.sub(r"\b(const|volatile|struct|class|enum|union)\b", " ", s)
    s = re.sub(r"<.*>", "", s)           # drop template arguments
    s = s.split("[")[0]                  # drop array dims
    s = s.replace("*", " ").replace("&", " ")
    parts = s.split()
    return parts[-1] if parts else ""

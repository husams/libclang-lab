"""High-level Python API over the Layer-1 *entity graph* (``entity_edge``).

The low-level :mod:`indexer.query` ``GraphQuery`` exposes the *symbol* graph --
every declaration plus the fine-grained ``calls``/``refers``/... edges between
them. On top of that, ``cidx resolve`` materialises a coarser **design-entity
graph** in the ``entity_edge`` table: one node per design entity (a record --
class / struct / union / their templates) and one edge per UML/ER-style
relation between two entities (``generalizes``, ``composes``, ``uses``, ...).

This module is the OO reader for *that* graph. It is the sibling of
:mod:`indexer.model` (which wraps the symbol graph) and is, like ``model``,
**Python-only by design** -- it never round-trips through the C++ port.

Three concepts, three classes:

* :class:`EdgeKind` -- *the kind of an edge*. An ``IntEnum`` whose members are
  the 11 ``entity_edge_kind`` rows (``GENERALIZES`` ... ``INSTANTIATES``),
  carrying display metadata (forward/inverse verb, structural-vs-behavioural
  category).
* :class:`EntityKind` -- *the type of an entity*. The flavour of a node:
  ``CLASS`` / ``STRUCT`` / ``UNION`` / ``CLASS_TEMPLATE`` / ``OTHER``.
* :class:`EntityNode` -- a node, with navigation methods (``bases()``,
  ``derived()``, ``out_edges()``, ``neighbors()``, ``walk()`` ...).
* :class:`EntityEdge` -- a single materialised edge, decoding the integer
  columns (multiplicity / access / create-form) into readable enums.

Open one with :func:`open_entity_graph` and navigate::

    eg = open_entity_graph()
    widget = eg.find("Widget")[0]
    for base in widget.bases():            # generalizes (transitive optional)
        print(widget.name, "is-a", base.name)
    for e in widget.uses():                # behavioural deps
        print(e)                           # Widget --uses--> Logger
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from itertools import islice
from typing import Callable, Iterator, Optional

from .query import GraphQuery, Sym, open_query

#: A re-runnable source of nodes: a zero-arg thunk yielding a *fresh* iterator
#: each call.  Keeping the query as a thunk (not a consumed generator) is what
#: makes :class:`EntityQuery` both lazy and reusable.
_NodeSrc = Callable[[], Iterator["EntityNode"]]
_EdgeSrc = Callable[[], Iterator["EntityEdge"]]

__all__ = [
    "EdgeKind",
    "EntityKind",
    "EntityType",
    "Multiplicity",
    "Access",
    "CreateForm",
    "EntityNode",
    "ClassNode",
    "AbstractClassNode",
    "InterfaceNode",
    "UnionNode",
    "EnumNode",
    "ClassTemplateNode",
    "AbstractClassTemplateNode",
    "InterfaceTemplateNode",
    "EntityEdge",
    "EntityQuery",
    "EntityGraph",
    "open_entity_graph",
    "ClassKind",
    # typed query seeds
    "Seed",
    "Klass",
    "AbstractClass",
    "Struct",
    "Record",
    "ClassTemplate",
    "Instance",
    "Interface",
]


# --------------------------------------------------------------------------- #
# Kind of an edge
# --------------------------------------------------------------------------- #


class EdgeKind(IntEnum):
    """The kind of an entity edge -- the 11 ``entity_edge_kind`` rows.

    The integer value is the on-disk ``entity_edge.kind``; the name matches
    ``entity_edge_kind.name``. Extra display metadata (the forward and inverse
    verb, and the structural-vs-behavioural category) hangs off each member.
    """

    GENERALIZES = 1
    IMPLEMENTS = 2
    SPECIALIZES = 3
    COMPOSES = 4
    AGGREGATES = 5
    ASSOCIATES = 6
    CREATES = 7
    USES = 8
    DESTROYS = 9
    BEFRIENDS = 10
    INSTANTIATES = 11

    @property
    def verb(self) -> str:
        """Forward reading: ``src <verb> dst`` (lowercase enum name)."""
        return self.name.lower()

    @property
    def inverse_verb(self) -> str:
        """How ``dst`` reads the edge back to ``src``."""
        return _INVERSE_VERB[self]

    @property
    def is_structural(self) -> bool:
        """Structural (UML class-diagram) relations vs behavioural ones.

        Structural = generalizes / implements / specializes / composes /
        aggregates / associates / befriends / instantiates. Behavioural =
        creates / uses / destroys (a member's body acts on another entity).
        """
        return self not in _BEHAVIOURAL

    @classmethod
    def from_name(cls, name: str) -> "EdgeKind":
        """Look a kind up by its ``entity_edge_kind`` name (case-insensitive)."""
        try:
            return cls[name.upper()]
        except KeyError as exc:
            raise ValueError(f"unknown entity edge kind {name!r}") from exc


_INVERSE_VERB: dict[EdgeKind, str] = {
    EdgeKind.GENERALIZES: "is base of",
    EdgeKind.IMPLEMENTS: "is interface of",
    EdgeKind.SPECIALIZES: "is generalized by",
    EdgeKind.COMPOSES: "is part of",
    EdgeKind.AGGREGATES: "is held by",
    EdgeKind.ASSOCIATES: "is associated with",
    EdgeKind.CREATES: "is created by",
    EdgeKind.USES: "is used by",
    EdgeKind.DESTROYS: "is destroyed by",
    EdgeKind.BEFRIENDS: "is friend of",
    EdgeKind.INSTANTIATES: "is instantiated by",
}

_BEHAVIOURAL: frozenset[EdgeKind] = frozenset(
    {EdgeKind.CREATES, EdgeKind.USES, EdgeKind.DESTROYS}
)


# --------------------------------------------------------------------------- #
# Type of an entity
# --------------------------------------------------------------------------- #


class EntityKind(IntEnum):
    """The type of an entity node -- the flavour of the underlying record."""

    CLASS = 1
    STRUCT = 2
    UNION = 3
    CLASS_TEMPLATE = 4
    OTHER = 0

    @classmethod
    def from_symbol_kind(cls, sym_kind: str) -> "EntityKind":
        """Map a ``Sym.kind`` string to an entity type."""
        return _SYM_KIND_TO_ENTITY.get(sym_kind, cls.OTHER)


_SYM_KIND_TO_ENTITY: dict[str, EntityKind] = {
    "class": EntityKind.CLASS,
    "struct": EntityKind.STRUCT,
    "union": EntityKind.UNION,
    "class-template": EntityKind.CLASS_TEMPLATE,
    "class_template": EntityKind.CLASS_TEMPLATE,
}


class ClassKind(IntEnum):
    """The abstractness of a record -- the "three kinds of class".

    Orthogonal to :class:`EntityKind` (which says class / struct / union /
    template): a ``struct`` can be an :attr:`INTERFACE`, a class template can be
    :attr:`ABSTRACT`.  Computed from the record's members, reusing the canonical
    ``entity_rollup._is_interface`` definition:

    * :attr:`CONCRETE`  -- no pure-virtual methods; can be instantiated.
    * :attr:`ABSTRACT`  -- has >=1 pure-virtual method AND carries state or a
      concrete method (so it is not a pure interface).
    * :attr:`INTERFACE` -- all methods pure-virtual (a defaulted virtual
      destructor is allowed) AND no data members AND >=1 pure method.
    """

    CONCRETE = 0
    ABSTRACT = 1
    INTERFACE = 2

    @property
    def label(self) -> str:
        return self.name.lower()


class EntityType(IntEnum):
    """The *materialized design type* of an entity node -- the ``entity_node.kind``
    column, written at ``cidx resolve`` (the ``entity_kind`` seed rows).

    This is the high-abstraction (UML) type: a record is classified by
    ABSTRACTNESS into :attr:`CLASS` / :attr:`ABSTRACT_CLASS` / :attr:`INTERFACE`
    (and the same split for class templates); :attr:`UNION` / :attr:`ENUM` keep
    their own type.  The C++ keyword (class vs struct) is deliberately NOT
    distinguished here -- that lives at the low-level symbol layer
    (:class:`indexer.model.Struct` / :class:`indexer.model.Union`), reachable via
    :meth:`EntityNode.as_model`.

    It drives which :class:`EntityNode` subclass wraps a node (so e.g. an
    interface gets :meth:`InterfaceNode.implemented_by` but not
    :meth:`ClassNode.implements`).  Orthogonal axis :attr:`class_kind` projects
    back onto the three-valued :class:`ClassKind`.
    """

    OTHER = 0
    CLASS = 1
    ABSTRACT_CLASS = 2
    INTERFACE = 3
    UNION = 4
    ENUM = 5
    CLASS_TEMPLATE = 6
    ABSTRACT_CLASS_TEMPLATE = 7
    INTERFACE_TEMPLATE = 8

    @property
    def label(self) -> str:
        return self.name.lower()

    @property
    def is_template(self) -> bool:
        return self in (
            EntityType.CLASS_TEMPLATE,
            EntityType.ABSTRACT_CLASS_TEMPLATE,
            EntityType.INTERFACE_TEMPLATE,
        )

    @property
    def class_kind(self) -> "ClassKind":
        """Project onto the abstractness axis (CONCRETE / ABSTRACT / INTERFACE).
        UNION / ENUM / OTHER are CONCRETE (they have no pure-virtual methods)."""
        if self in (EntityType.INTERFACE, EntityType.INTERFACE_TEMPLATE):
            return ClassKind.INTERFACE
        if self in (EntityType.ABSTRACT_CLASS, EntityType.ABSTRACT_CLASS_TEMPLATE):
            return ClassKind.ABSTRACT
        return ClassKind.CONCRETE


# --------------------------------------------------------------------------- #
# Typed query seeds
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Seed:
    """A typed query seed: a name plus a kind filter.

    Pass to :meth:`EntityGraph.query` for a readable, self-documenting start
    instead of a bare fuzzy string::

        g.query(ClassTemplate("Singleton"))            # only the primary template
        g.query(Instance("Singleton<app::Cache>"))     # only that instantiation
        g.query(Interface("Shape")).implemented_by()   # who realizes Shape

    The name is matched (exactly) against an entity's qualified name, spelling,
    OR display name (so ``Instance("Singleton<app::Cache>")`` matches by the
    template-argument-bearing display); if nothing matches exactly it falls back
    to a fuzzy lookup. The subclass then narrows by entity kind.

    :class:`EntityGraph` also exposes one-call shortcuts that wrap these:
    ``g.klass`` / ``g.struct`` / ``g.record`` / ``g.template`` / ``g.instance``
    / ``g.interface``.
    """

    name: str

    def admits(self, node: "EntityNode") -> bool:
        """Whether ``node`` passes this seed's kind filter (the base admits any
        entity; subclasses narrow)."""
        return True


@dataclass(frozen=True)
class Klass(Seed):
    """Seed: a plain *concrete* ``class`` named ``name`` -- no pure-virtual
    methods. Abstract classes (use :class:`AbstractClass`), interfaces (use
    :class:`Interface`), and template instantiations (use :class:`Instance`)
    are all excluded, so the three class kinds partition cleanly."""

    def admits(self, node: "EntityNode") -> bool:
        return (
            node.kind is EntityKind.CLASS
            and "<" not in node.display
            and node.class_kind is ClassKind.CONCRETE
        )


@dataclass(frozen=True)
class AbstractClass(Seed):
    """Seed: an *abstract* class named ``name`` -- has >=1 pure-virtual method
    but is not a pure interface (it carries state or a concrete method)."""

    def admits(self, node: "EntityNode") -> bool:
        return "<" not in node.display and node.class_kind is ClassKind.ABSTRACT


@dataclass(frozen=True)
class Struct(Seed):
    """Seed: a plain (non-instantiation) ``struct`` named ``name``."""

    def admits(self, node: "EntityNode") -> bool:
        return node.kind is EntityKind.STRUCT and "<" not in node.display


@dataclass(frozen=True)
class Record(Seed):
    """Seed: any plain record (class / struct / union) named ``name`` -- not a
    template instantiation (use :class:`Instance`) nor a primary template."""

    def admits(self, node: "EntityNode") -> bool:
        return (
            node.kind in (EntityKind.CLASS, EntityKind.STRUCT, EntityKind.UNION)
            and "<" not in node.display
        )


@dataclass(frozen=True)
class ClassTemplate(Seed):
    """Seed: a primary class template named ``name`` (e.g. ``Singleton<T>``)."""

    def admits(self, node: "EntityNode") -> bool:
        return node.kind is EntityKind.CLASS_TEMPLATE


@dataclass(frozen=True)
class Instance(Seed):
    """Seed: a concrete template instantiation / specialization (e.g.
    ``Singleton<app::Cache>``) -- a record carrying template arguments, NOT the
    primary template."""

    def admits(self, node: "EntityNode") -> bool:
        return node.kind is not EntityKind.CLASS_TEMPLATE and "<" in node.display


@dataclass(frozen=True)
class Interface(Seed):
    """Seed: a pure interface named ``name`` -- all methods pure-virtual, no
    data members (a ``class`` or ``struct``). Uses the structural definition,
    so it matches even an interface nothing implements yet."""

    def admits(self, node: "EntityNode") -> bool:
        return node.is_interface


# --------------------------------------------------------------------------- #
# Decoded edge attributes
# --------------------------------------------------------------------------- #


class Multiplicity(IntEnum):
    """``entity_edge.multiplicity`` -- cardinality of a structural relation."""

    ONE = 1  # exactly one
    OPTIONAL = 2  # 0..1
    MANY = 3  # 0..*
    N = 4  # fixed N (array member)

    @property
    def label(self) -> str:
        return {1: "1", 2: "0..1", 3: "0..*", 4: "N"}[self.value]


class Access(IntEnum):
    """``entity_edge.access`` -- C++ access of the relating member/base."""

    PUBLIC = 0
    PROTECTED = 1
    PRIVATE = 2

    @property
    def label(self) -> str:
        return self.name.lower()


class CreateForm(IntEnum):
    """``entity_edge.create_form`` -- how a creates/destroys edge arose."""

    CTOR_CALL = 1
    RETURN = 2
    VALUE = 3
    TEMP = 4
    HEAP = 5
    FACTORY = 6
    COPY = 7
    MOVE = 8

    @property
    def label(self) -> str:
        return self.name.lower()


# --------------------------------------------------------------------------- #
# An edge
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EntityEdge:
    """One materialised ``entity_edge`` row, as a typed object.

    ``src`` / ``dst`` are :class:`EntityNode`; ``kind`` is an :class:`EdgeKind`.
    The integer attribute columns are decoded lazily into enums via the
    properties below (``multiplicity`` / ``access`` / ``create_form``).
    """

    src: "EntityNode"
    dst: "EntityNode"
    kind: EdgeKind
    count: int
    via_member_id: Optional[int]
    _multiplicity: int
    _access: int
    is_virtual: bool
    _create_form: Optional[int]
    partial: bool
    _graph: "EntityGraph"

    @property
    def multiplicity(self) -> Multiplicity:
        return Multiplicity(self._multiplicity)

    @property
    def access(self) -> Access:
        return Access(self._access)

    @property
    def create_form(self) -> Optional[CreateForm]:
        """Decoded create/destroy form, or ``None`` for other kinds."""
        return CreateForm(self._create_form) if self._create_form else None

    @property
    def via_member(self) -> "Optional[EntityNode]":
        """The member/field that carries the relation (composes/uses/...), if any."""
        if self.via_member_id is None:
            return None
        return self._graph.entity(self.via_member_id, _any_symbol=True)

    def to_dict(self) -> dict:
        d: dict = {
            "src": self.src.display,
            "kind": self.kind.verb,
            "dst": self.dst.display,
            "count": self.count,
        }
        if self.kind.is_structural and self.multiplicity is not Multiplicity.ONE:
            d["multiplicity"] = self.multiplicity.label
        if self.access is not Access.PUBLIC:
            d["access"] = self.access.label
        if self.is_virtual:
            d["virtual"] = True
        if self.create_form is not None:
            d["form"] = self.create_form.label
        if self.via_member_id is not None:
            d["via"] = self.via_member_id
        if self.partial:
            d["partial"] = True
        return d

    def __repr__(self) -> str:
        extra = []
        if self.kind.is_structural and self.multiplicity is not Multiplicity.ONE:
            extra.append(self.multiplicity.label)
        if self.access is not Access.PUBLIC:
            extra.append(self.access.label)
        if self.is_virtual:
            extra.append("virtual")
        if self.create_form is not None:
            extra.append(self.create_form.label)
        tag = f" [{', '.join(extra)}]" if extra else ""
        return f"{self.src.display} --{self.kind.verb}--> {self.dst.display}{tag}"


# --------------------------------------------------------------------------- #
# A node
# --------------------------------------------------------------------------- #


class EntityNode:
    """A design entity (a record) -- a node in the entity graph.

    Wraps the underlying :class:`indexer.query.Sym` and offers navigation over
    ``entity_edge``. Equality / hashing is by symbol id, so nodes are usable as
    dict keys and set members.
    """

    __slots__ = ("_sym", "_graph")

    def __init__(self, sym: Sym, graph: "EntityGraph") -> None:
        self._sym = sym
        self._graph = graph

    # -- identity ----------------------------------------------------------- #

    @property
    def id(self) -> int:
        return self._sym.id

    @property
    def name(self) -> str:
        return self._sym.name

    @property
    def display(self) -> str:
        """Qualified name *including* template arguments for specializations
        and templates, e.g. ``app::Singleton<app::Cache>`` (specialization) or
        ``app::Singleton<T>`` (primary). Falls back to :attr:`name` (the bare
        qualified name) for non-templates.

        :attr:`name` keeps the un-parameterised qualified name (``app::Cache``);
        ``display`` is what reprs and edge dumps use so a specialization is not
        indistinguishable from its primary template.
        """
        dn = self._graph._display_name(self._sym.id)
        if dn and "<" in dn:
            return self._sym.name + dn[dn.index("<"):]
        return self._sym.name

    @property
    def entity_type(self) -> "EntityType":
        """The materialized design type (CLASS / ABSTRACT_CLASS / INTERFACE /
        UNION / ENUM / *_TEMPLATE / OTHER) -- read from the ``entity_node`` table
        when the index has been ``resolve``d, else derived from the symbol's
        members (so it is correct either way).  This is what selects the node's
        Python subclass (:class:`ClassNode` / :class:`InterfaceNode` / ...)."""
        return self._graph._entity_type(self._sym.id)

    @property
    def class_kind(self) -> "ClassKind":
        """Abstractness of this record: CONCRETE / ABSTRACT / INTERFACE -- the
        "three kinds of class" (orthogonal to :attr:`kind`). A non-record entity
        is CONCRETE (it has no pure-virtual methods)."""
        return self.entity_type.class_kind

    @property
    def is_abstract(self) -> bool:
        """True if this record has >=1 pure-virtual method (cannot be
        instantiated). Interfaces are abstract too -- see :attr:`is_interface`."""
        return self.class_kind is not ClassKind.CONCRETE

    @property
    def is_interface(self) -> bool:
        """True if this record is a pure interface (all methods pure-virtual,
        no data members)."""
        return self.class_kind is ClassKind.INTERFACE

    @property
    def spelling(self) -> str:
        return self._sym.spelling

    @property
    def usr(self) -> str:
        return self._sym.usr

    @property
    def kind(self) -> EntityKind:
        """The entity *type* (CLASS / STRUCT / ...)."""
        return EntityKind.from_symbol_kind(self._sym.kind)

    @property
    def symbol_kind(self) -> str:
        """The raw underlying symbol kind string."""
        return self._sym.kind

    @property
    def component(self) -> Optional[str]:
        return self._sym.component

    @property
    def location(self) -> Optional[str]:
        return self._sym.loc if self._sym.file else None

    @property
    def sym(self) -> Sym:
        """Escape hatch to the low-level symbol."""
        return self._sym

    # -- bridge to the low-level (model) layer ------------------------------ #

    def as_model(self):
        """Drop from this high-level entity down to the low-level
        :mod:`indexer.model` entity wrapping the SAME symbol -- the codebase
        graph, where the C++ keyword (``class`` / ``struct`` / ``union``) is
        distinguished and call-graph verbs (``callers`` / ``members`` / ...)
        live.  Returns ``None`` if the symbol has no model wrapper.

        The intended flow: search/navigate cheaply at the UML level, then
        ``as_model()`` to work against the full symbol graph::

            iface = eg.interface("Shape").first()
            for impl in iface.implemented_by():
                cls = impl.as_model()        # -> a model.Class / model.Struct
                cls.methods, cls.bases(), ...
        """
        return self._graph.model.get(self._sym.usr)

    # -- edges -------------------------------------------------------------- #

    def out_edges(self, kind: Optional[EdgeKind] = None) -> Iterator[EntityEdge]:
        """Edges where this node is the source (``self --kind--> ?``). Lazy."""
        return self._graph.edges(src=self, kind=kind)

    def in_edges(self, kind: Optional[EdgeKind] = None) -> Iterator[EntityEdge]:
        """Edges where this node is the target (``? --kind--> self``). Lazy."""
        return self._graph.edges(dst=self, kind=kind)

    def neighbors(
        self,
        kind: Optional[EdgeKind] = None,
        direction: str = "out",
    ) -> Iterator["EntityNode"]:
        """Distinct adjacent entities, streamed. ``direction`` out / in / both.

        Deduplicates as it yields (a node reached twice is emitted once), so the
        only state held is a set of ids -- never a materialised node list.
        """
        seen: set[int] = set()
        if direction in ("out", "both"):
            for e in self.out_edges(kind):
                if e.dst.id not in seen:
                    seen.add(e.dst.id)
                    yield e.dst
        if direction in ("in", "both"):
            for e in self.in_edges(kind):
                if e.src.id not in seen:
                    seen.add(e.src.id)
                    yield e.src

    # -- structural shortcuts ---------------------------------------------- #

    def bases(self, *, transitive: bool = False) -> Iterator["EntityNode"]:
        """Entities this one generalizes-to (direct base classes). Lazy."""
        if not transitive:
            for e in self.out_edges(EdgeKind.GENERALIZES):
                yield e.dst
        else:
            yield from self.walk(EdgeKind.GENERALIZES, direction="out")

    def derived(self, *, transitive: bool = False) -> Iterator["EntityNode"]:
        """Entities that generalize to this one (direct subclasses). Lazy."""
        if not transitive:
            for e in self.in_edges(EdgeKind.GENERALIZES):
                yield e.src
        else:
            yield from self.walk(EdgeKind.GENERALIZES, direction="in")

    def parts(self) -> Iterator[EntityEdge]:
        """Composition edges (strong ownership: ``composes``). Lazy."""
        return self.out_edges(EdgeKind.COMPOSES)

    def uses(self) -> Iterator[EntityEdge]:
        """Behavioural-dependency edges (``uses``). Lazy."""
        return self.out_edges(EdgeKind.USES)

    def creates(self) -> Iterator[EntityEdge]:
        return self.out_edges(EdgeKind.CREATES)

    def friends(self) -> Iterator["EntityNode"]:
        for e in self.out_edges(EdgeKind.BEFRIENDS):
            yield e.dst

    # -- template shortcuts ------------------------------------------------- #

    def instances(self) -> Iterator["EntityNode"]:
        """Template specializations/instantiations OF this primary template
        (``instantiates``, in). E.g. ``Singleton<T>`` -> ``Singleton<Cache>``,
        ``Singleton<Registry>``, ... Lazy."""
        for e in self.in_edges(EdgeKind.INSTANTIATES):
            yield e.src

    def primary_template(self) -> "Optional[EntityNode]":
        """The primary template this specialization instantiates
        (``instantiates``, out), or ``None`` if this node is not a
        specialization. E.g. ``Singleton<Cache>`` -> ``Singleton<T>``."""
        for e in self.out_edges(EdgeKind.INSTANTIATES):
            return e.dst
        return None

    # -- traversal ---------------------------------------------------------- #

    def walk(
        self,
        kind: EdgeKind,
        direction: str = "out",
        *,
        max_depth: Optional[int] = None,
    ) -> Iterator["EntityNode"]:
        """Transitive closure following ``kind`` edges (BFS, excludes self).

        Streams each entity the first time it is reached, so a consumer that
        stops early stops the traversal. Cycle-safe (a ``seen`` set of ids).
        ``direction`` is out (follow src->dst) or in (dst->src).
        """
        seen: set[int] = set()
        frontier: list[tuple["EntityNode", int]] = [(self, 0)]
        while frontier:
            node, depth = frontier.pop(0)
            if max_depth is not None and depth >= max_depth:
                continue
            for n in node.neighbors(kind, direction):
                if n.id != self.id and n.id not in seen:
                    seen.add(n.id)
                    yield n
                    frontier.append((n, depth + 1))

    # -- fluent query ------------------------------------------------------- #

    def query(self) -> "EntityQuery":
        """Start a fluent relational query seeded with this node.

        ``leaf.query().derived(transitive=True).uses().names()`` etc.
        """
        return EntityQuery(self._graph, lambda: iter((self,)))

    # -- dunder ------------------------------------------------------------- #

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind.name.lower(),
            "entity_type": self.entity_type.label,
            "class_kind": self.class_kind.label,
            "component": self.component,
            "location": self.location,
        }

    def __eq__(self, other) -> bool:
        return isinstance(other, EntityNode) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return f"<{self.entity_type.label} {self.display}>"


# --------------------------------------------------------------------------- #
# Entity-node subclasses -- one per materialized design type.
#
# `EntityGraph.entity()` dispatches on the stored entity_node.kind so a node
# wraps as exactly the type whose methods make sense for it: a concrete class
# can `implements()` an interface/abstract base, an interface/abstract class is
# `implemented_by()` concrete classes, an interface exposes its `operations()`.
# The C++ keyword (class vs struct) is NOT modelled here -- `as_model()` bridges
# down to indexer.model where Class / Struct / Union are distinct.
# --------------------------------------------------------------------------- #


class _Implementable:
    """Mixin for entity types that *require* implementation -- abstract classes
    and interfaces.  Both are realized/extended by other entities, so both carry
    :meth:`implemented_by`."""

    __slots__ = ()

    def implemented_by(self: "EntityNode", *, concrete_only: bool = False):  # type: ignore[misc]
        """Entities that implement / realize this one -- the incoming
        ``generalizes`` (a subclass extends an abstract base) and ``implements``
        (a class realizes an interface) edges.  ``concrete_only=True`` keeps only
        instantiable implementors (drops further abstract/interface subtypes).
        Lazy, de-duplicated."""
        seen: set[int] = set()
        for ek in (EdgeKind.GENERALIZES, EdgeKind.IMPLEMENTS):
            for e in self.in_edges(ek):
                src = e.src
                if src.id in seen:
                    continue
                if concrete_only and src.entity_type.class_kind is not ClassKind.CONCRETE:
                    continue
                seen.add(src.id)
                yield src

    def operations(self: "EntityNode"):  # type: ignore[misc]
        """The pure-virtual methods this entity declares -- its required
        contract.  Returns the low-level method :class:`indexer.query.Sym` rows
        (own members only)."""
        return self._graph._own_methods(self._sym.id, pure_only=True)


class ClassNode(EntityNode):
    """A concrete (instantiable) class / struct entity."""

    __slots__ = ()

    #: A concrete class can be instantiated (no unimplemented pure-virtuals).
    is_instantiable = True

    def implements(self):
        """The abstract classes / interfaces this concrete class satisfies --
        its non-concrete supertypes via outgoing ``generalizes`` (abstract base)
        and ``implements`` (interface).  Lazy, de-duplicated."""
        seen: set[int] = set()
        for ek in (EdgeKind.GENERALIZES, EdgeKind.IMPLEMENTS):
            for e in self.out_edges(ek):
                dst = e.dst
                if dst.id in seen:
                    continue
                if dst.entity_type.class_kind is ClassKind.CONCRETE:
                    continue
                seen.add(dst.id)
                yield dst


class AbstractClassNode(_Implementable, EntityNode):
    """An abstract class entity -- has >=1 pure-virtual method but also carries
    state or a concrete method, so it is not a pure interface.  Cannot be
    instantiated; is :meth:`implemented_by` concrete subclasses."""

    __slots__ = ()

    is_instantiable = False

    def pure_methods(self):
        """This class's own pure-virtual methods (the ones a subclass must
        override).  Alias of :meth:`operations` read as "still abstract here"."""
        return self.operations()


class InterfaceNode(_Implementable, EntityNode):
    """A pure interface entity -- all operations pure-virtual, no data members.
    Is :meth:`implemented_by` the classes that realize it; :meth:`operations`
    lists its contract."""

    __slots__ = ()

    is_instantiable = False


class UnionNode(EntityNode):
    """A union entity (never abstract)."""

    __slots__ = ()
    is_instantiable = True


class EnumNode(EntityNode):
    """An enum entity."""

    __slots__ = ()


class ClassTemplateNode(ClassNode):
    """A concrete class-template entity (primary template, e.g. ``Box<T>``)."""

    __slots__ = ()


class AbstractClassTemplateNode(AbstractClassNode):
    """An abstract class-template entity."""

    __slots__ = ()


class InterfaceTemplateNode(InterfaceNode):
    """A pure-interface class-template entity."""

    __slots__ = ()


#: Materialized design type -> the EntityNode subclass that wraps it.
_ETYPE_TO_NODE: dict["EntityType", type] = {}


def _init_etype_dispatch() -> None:
    _ETYPE_TO_NODE.update(
        {
            EntityType.CLASS: ClassNode,
            EntityType.ABSTRACT_CLASS: AbstractClassNode,
            EntityType.INTERFACE: InterfaceNode,
            EntityType.UNION: UnionNode,
            EntityType.ENUM: EnumNode,
            EntityType.CLASS_TEMPLATE: ClassTemplateNode,
            EntityType.ABSTRACT_CLASS_TEMPLATE: AbstractClassTemplateNode,
            EntityType.INTERFACE_TEMPLATE: InterfaceTemplateNode,
            EntityType.OTHER: EntityNode,
        }
    )


_init_etype_dispatch()


# --------------------------------------------------------------------------- #
# The graph
# --------------------------------------------------------------------------- #


_EDGE_COLS = (
    "src_id, dst_id, kind, count, via_member_id, "
    "multiplicity, access, is_virtual, create_form, partial"
)


class EntityGraph:
    """OO reader over the Layer-1 ``entity_edge`` graph.

    Wraps a :class:`indexer.query.GraphQuery` (sharing its read-only sqlite
    connection for symbol look-ups) and queries ``entity_edge`` directly. Use
    :func:`open_entity_graph` for the common "open the standard index" path.
    """

    def __init__(self, graph: GraphQuery) -> None:
        self._q = graph
        self._c = graph._c  # shared read-only connection
        self._node_cache: dict[int, EntityNode] = {}
        self._dname_cache: dict[int, Optional[str]] = {}
        self._ckind_cache: dict[int, "ClassKind"] = {}
        self._etype_cache: dict[int, "EntityType"] = {}
        self._model = None  # lazily-built model.CodeBase over the same connection

    def _display_name(self, sym_id: int) -> Optional[str]:
        """The symbol's ``display_name`` column (carries template arguments,
        e.g. ``Singleton<app::Cache>``). Cached; ``None`` when absent."""
        if sym_id not in self._dname_cache:
            row = self._c.execute(
                "SELECT display_name FROM symbol WHERE id = ?", (sym_id,)
            ).fetchone()
            self._dname_cache[sym_id] = row[0] if row else None
        return self._dname_cache[sym_id]

    def _class_kind(self, sym_id: int) -> ClassKind:
        """Classify a record as CONCRETE / ABSTRACT / INTERFACE by inspecting
        its members. Mirrors ``entity_rollup._is_interface`` exactly (method
        kind=21, field kind=6, ``is_pure``; destructors are kind=25, so the
        ``kind=21`` filters already exclude them). Cached."""
        if sym_id in self._ckind_cache:
            return self._ckind_cache[sym_id]
        usr_sub = "(SELECT usr FROM symbol WHERE id = ?)"
        pure = self._c.execute(
            f"SELECT COUNT(*) FROM symbol WHERE parent_usr = {usr_sub} "
            "AND kind = 21 AND is_pure = 1",
            (sym_id,),
        ).fetchone()[0]
        if not pure:
            kind = ClassKind.CONCRETE
        else:
            non_pure = self._c.execute(
                f"SELECT COUNT(*) FROM symbol WHERE parent_usr = {usr_sub} "
                "AND kind = 21 AND is_pure = 0",
                (sym_id,),
            ).fetchone()[0]
            fields = self._c.execute(
                f"SELECT COUNT(*) FROM symbol WHERE parent_usr = {usr_sub} "
                "AND kind = 6",
                (sym_id,),
            ).fetchone()[0]
            kind = (
                ClassKind.INTERFACE
                if non_pure == 0 and fields == 0
                else ClassKind.ABSTRACT
            )
        self._ckind_cache[sym_id] = kind
        return kind

    def _entity_type(self, sym_id: int) -> "EntityType":
        """The materialized design type of a node.

        Reads the ``entity_node`` table (populated at ``cidx resolve``); when the
        row is absent -- an un-resolved index, or a non-entity symbol -- derives
        it from the symbol kind + member classification, so the answer matches
        the materializer either way. Cached."""
        if sym_id in self._etype_cache:
            return self._etype_cache[sym_id]
        row = self._c.execute(
            "SELECT kind FROM entity_node WHERE id = ?", (sym_id,)
        ).fetchone()
        et = EntityType(row[0]) if row is not None else self._derive_entity_type(sym_id)
        self._etype_cache[sym_id] = et
        return et

    def _derive_entity_type(self, sym_id: int) -> "EntityType":
        """Fallback classification mirroring entity_rollup._entity_kind_id, used
        when the index has not been resolved (no entity_node row)."""
        row = self._c.execute(
            "SELECT kind FROM symbol WHERE id = ?", (sym_id,)
        ).fetchone()
        sym_kind = row[0] if row else None
        if sym_kind == 5:  # enum
            return EntityType.ENUM
        if sym_kind == 3:  # union
            return EntityType.UNION
        if sym_kind not in (2, 4, 31):  # not struct/class/class-template
            return EntityType.OTHER
        is_template = sym_kind == 31
        ck = self._class_kind(sym_id)
        if ck is ClassKind.INTERFACE:
            return EntityType.INTERFACE_TEMPLATE if is_template else EntityType.INTERFACE
        if ck is ClassKind.ABSTRACT:
            return (
                EntityType.ABSTRACT_CLASS_TEMPLATE
                if is_template
                else EntityType.ABSTRACT_CLASS
            )
        return EntityType.CLASS_TEMPLATE if is_template else EntityType.CLASS

    def _own_methods(self, sym_id: int, *, pure_only: bool = False) -> list[Sym]:
        """The record's own member-function symbols (``method`` kind=21), in id
        order. ``pure_only`` keeps only pure-virtual declarations (the contract /
        still-abstract operations)."""
        clause = " AND is_pure = 1" if pure_only else ""
        rows = self._c.execute(
            "SELECT id FROM symbol "
            "WHERE parent_usr = (SELECT usr FROM symbol WHERE id = ?) "
            f"  AND kind = 21{clause} ORDER BY id",
            (sym_id,),
        ).fetchall()
        out: list[Sym] = []
        for r in rows:
            s = self._q.get(r[0])
            if s is not None:
                out.append(s)
        return out

    # -- bridge to the low-level (model) layer ------------------------------ #

    @property
    def model(self):
        """A :class:`indexer.model.CodeBase` over the SAME connection -- the
        low-level codebase graph. Built lazily and shared (its lifetime follows
        this graph's; do not close it independently)."""
        if self._model is None:
            from .model import CodeBase

            self._model = CodeBase(self._q)
        return self._model

    # -- lifecycle ---------------------------------------------------------- #

    def close(self) -> None:
        self._q.close()

    def __enter__(self) -> "EntityGraph":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- node access -------------------------------------------------------- #

    def entity(self, ident, *, _any_symbol: bool = False) -> Optional[EntityNode]:
        """Resolve an id / USR / Sym / EntityNode to an :class:`EntityNode`.

        Returns ``None`` if no such symbol exists. By default any record symbol
        is wrappable; ``_any_symbol`` lets internal callers wrap a via-member
        (e.g. a field) that is not itself a graph node.
        """
        if isinstance(ident, EntityNode):
            return ident
        key = ident.id if isinstance(ident, Sym) else ident
        if isinstance(key, int) and key in self._node_cache:
            return self._node_cache[key]
        sym = self._q.get(ident.sym if isinstance(ident, EntityNode) else ident)
        if sym is None:
            return None
        cls = _ETYPE_TO_NODE.get(self._entity_type(sym.id), EntityNode)
        node = cls(sym, self)
        self._node_cache[sym.id] = node
        return node

    def entities(self) -> Iterator[EntityNode]:
        """Every design entity, streamed in id order. Lazy.

        The union of the materialized ``entity_node`` rows (every classified
        record / enum / class-template -- present once the index is resolved)
        and the ``entity_edge`` endpoints (so an un-resolved index still surfaces
        edge-participating nodes). Rows stream off the sqlite cursor -- no full
        node list is built up front (consume into ``list(...)`` / ``sorted(...)``
        if you need one).
        """
        cur = self._c.execute(
            "SELECT DISTINCT id FROM ("
            "  SELECT id FROM entity_node "
            "  UNION SELECT src_id AS id FROM entity_edge "
            "  UNION SELECT dst_id AS id FROM entity_edge"
            ") ORDER BY id"
        )
        for r in cur:
            node = self.entity(r[0])
            if node is not None:
                yield node

    def _in_graph(self, sym_id: int) -> bool:
        """Whether a symbol participates in the design graph: it has a
        materialized ``entity_node`` row OR appears as an ``entity_edge``
        endpoint. Exactly the membership the ``entities()`` union expresses, but
        evaluated as three indexed point lookups (entity_node PK,
        idx_entity_edge_src/dst) instead of materializing every entity."""
        row = self._c.execute(
            "SELECT 1 WHERE EXISTS (SELECT 1 FROM entity_node WHERE id = :i) "
            "   OR EXISTS (SELECT 1 FROM entity_edge WHERE src_id = :i) "
            "   OR EXISTS (SELECT 1 FROM entity_edge WHERE dst_id = :i)",
            {"i": sym_id},
        ).fetchone()
        return row is not None

    def find(self, pattern: str, limit: int = 50) -> list[EntityNode]:
        """Fuzzy qualified-name lookup, filtered to entities in the graph.

        Checks graph membership per candidate (at most ``limit`` indexed point
        lookups) instead of materializing the whole entity set first -- the old
        ``{n.id for n in self.entities()}`` walked all 270k+ entities on every
        call. Same result set + order (``self._q.find`` order is preserved)."""
        out: list[EntityNode] = []
        for sym in self._q.find(pattern, limit=limit):
            if self._in_graph(sym.id):
                node = self.entity(sym)
                if node is not None:
                    out.append(node)
        return out

    # -- fluent query ------------------------------------------------------- #

    def query(self, *start) -> "EntityQuery":
        """Start a fluent relational query from one or more seed entities.

        Each ``start`` may be an :class:`EntityNode`, a :class:`Sym`, a symbol
        id, or a name/pattern string (exact name preferred, else fuzzy
        :meth:`find`).  Pass several to seed a union; pass none to seed *every*
        entity in the graph.  Chain relation steps and finish with a terminal::

            eg.query("Shape").derived().names()          # who inherits Shape
            eg.query("OrderService").uses().names()       # what it uses
            eg.query("Shape").derived(transitive=True).uses().nodes()

        The query is lazy and reusable: seeds are resolved freshly every time a
        terminal consumes the query, so nothing is materialised up front.
        """
        if start:
            nodes_src: "_NodeSrc" = lambda: iter(self._resolve_seeds(start))
        else:
            nodes_src = self.entities
        return EntityQuery(self, nodes_src)

    # -- typed seeds (readable, kind-filtered query starts) ----------------- #

    def klass(self, name: str) -> "EntityQuery":
        """Query seeded with the non-template class(es) named ``name``."""
        return self.query(Klass(name))

    def struct(self, name: str) -> "EntityQuery":
        """Query seeded with the struct(s) named ``name``."""
        return self.query(Struct(name))

    def record(self, name: str) -> "EntityQuery":
        """Query seeded with any record (class/struct/union) named ``name``."""
        return self.query(Record(name))

    def template(self, name: str) -> "EntityQuery":
        """Query seeded with the primary class template(s) named ``name``
        (e.g. ``g.template("Singleton")`` -> ``Singleton<T>``)."""
        return self.query(ClassTemplate(name))

    def instance(self, name: str) -> "EntityQuery":
        """Query seeded with a concrete template instantiation, matched by its
        display name (e.g. ``g.instance("Singleton<app::Cache>")``)."""
        return self.query(Instance(name))

    def abstract_class(self, name: str) -> "EntityQuery":
        """Query seeded with the abstract class(es) named ``name`` -- have a
        pure-virtual method but are not pure interfaces."""
        return self.query(AbstractClass(name))

    def interface(self, name: str) -> "EntityQuery":
        """Query seeded with the pure interface(s) named ``name`` (all methods
        pure-virtual, no data members; e.g. ``g.interface("Shape")``)."""
        return self.query(Interface(name))

    def _resolve_seeds(self, start) -> list[EntityNode]:
        seen: dict[int, EntityNode] = {}
        for item in start:
            for node in self._seeds_for(item):
                seen.setdefault(node.id, node)
        return list(seen.values())

    def _named(self, name: str) -> list[EntityNode]:
        """Entities whose qualified name, spelling, display name, OR unqualified
        ``spelling<args>`` form equals ``name``; falls back to fuzzy
        :meth:`find` when none match exactly. The extra forms let a seed match a
        specialization by either ``Singleton<app::Cache>`` (unqualified) or
        ``app::Singleton<app::Cache>`` (qualified display)."""
        out: list[EntityNode] = []
        for n in self.entities():
            cands = {n.name, n.spelling, n.display}
            d = n.display
            if "<" in d:
                cands.add(n.spelling + d[d.index("<"):])  # unqualified + targs
            if name in cands:
                out.append(n)
        return out if out else self.find(name)

    def _seeds_for(self, item) -> list[EntityNode]:
        if isinstance(item, Seed):
            return [n for n in self._named(item.name) if item.admits(n)]
        if isinstance(item, str):
            exact = [n for n in self.entities() if n.name == item]
            return exact if exact else self.find(item)
        node = self.entity(item)
        return [node] if node is not None else []

    # -- edge access -------------------------------------------------------- #

    def edges(
        self,
        kind: Optional[EdgeKind] = None,
        src=None,
        dst=None,
    ) -> Iterator[EntityEdge]:
        """Materialised edges, optionally filtered by kind / src / dst. Lazy.

        Rows stream off the cursor in (src_id, kind, dst_id) order -- a stable,
        deterministic order for dumps -- without building a Python list up
        front.  (Ordering is by id, not by name as before; sort the result
        yourself if you need name order.)
        """
        wheres: list[str] = []
        params: list = []
        if src is not None:
            wheres.append("src_id = ?")
            params.append(self._id_of(src))
        if dst is not None:
            wheres.append("dst_id = ?")
            params.append(self._id_of(dst))
        if kind is not None:
            wheres.append("kind = ?")
            params.append(int(kind))
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        cur = self._c.execute(
            f"SELECT {_EDGE_COLS} FROM entity_edge {where_sql} "
            "ORDER BY src_id, kind, dst_id",
            params,
        )
        for r in cur:
            edge = self._edge(r)
            if edge is not None:
                yield edge

    def by_kind(self, kind: EdgeKind) -> Iterator[EntityEdge]:
        return self.edges(kind=kind)

    def kinds(self) -> list[EdgeKind]:
        """Edge kinds actually present in this graph (with >=1 edge)."""
        rows = self._c.execute(
            "SELECT DISTINCT kind FROM entity_edge ORDER BY kind"
        ).fetchall()
        return [EdgeKind(r[0]) for r in rows]

    def stats(self) -> dict:
        """Counts: total edges, per-kind breakdown, distinct entity count."""
        per_kind = {
            EdgeKind(r[0]).verb: r[1]
            for r in self._c.execute(
                "SELECT kind, COUNT(*) FROM entity_edge GROUP BY kind ORDER BY kind"
            ).fetchall()
        }
        total = self._c.execute("SELECT COUNT(*) FROM entity_edge").fetchone()[0]
        # Count distinct entity ids in SQL instead of materializing every node
        # (the old `sum(1 for _ in self.entities())` ran a get() per entity).
        n_entities = self._c.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT id FROM entity_node "
            "  UNION SELECT src_id AS id FROM entity_edge "
            "  UNION SELECT dst_id AS id FROM entity_edge"
            ")"
        ).fetchone()[0]
        return {
            "entities": n_entities,
            "edges": total,
            "by_kind": per_kind,
        }

    def __iter__(self) -> Iterator[EntityEdge]:
        return self.edges()

    # -- internals ---------------------------------------------------------- #

    def _id_of(self, ident) -> int:
        if isinstance(ident, EntityNode):
            return ident.id
        if isinstance(ident, Sym):
            return ident.id
        if isinstance(ident, int):
            return ident
        node = self.entity(ident)
        if node is None:
            raise KeyError(f"no entity {ident!r}")
        return node.id

    def _edge(self, r) -> Optional[EntityEdge]:
        src = self.entity(r["src_id"])
        dst = self.entity(r["dst_id"])
        if src is None or dst is None:
            return None
        return EntityEdge(
            src=src,
            dst=dst,
            kind=EdgeKind(r["kind"]),
            count=r["count"],
            via_member_id=r["via_member_id"],
            _multiplicity=r["multiplicity"],
            _access=r["access"],
            is_virtual=bool(r["is_virtual"]),
            _create_form=r["create_form"],
            partial=bool(r["partial"]),
            _graph=self,
        )


# --------------------------------------------------------------------------- #
# A fluent relational query
# --------------------------------------------------------------------------- #


_DIRECTIONS = ("out", "in", "both")


class EntityQuery:
    """A lazy, reusable, chainable relational query over the entity graph.

    A query holds a re-runnable *source* of nodes (a thunk), NOT a materialised
    list -- so building a chain allocates nothing, intermediate steps never
    realise a full node list, and a terminal that stops early (``first`` /
    ``any``) stops the whole upstream pipeline.  Because the source is a thunk
    rather than a consumed generator, the query is also **reusable**: every
    terminal re-runs the pipeline from the seeds, so the same query object can
    be iterated repeatedly and shared as a starting point.

    Begin with :meth:`EntityGraph.query` (or :meth:`EntityNode.query`), chain
    relation steps, and finish with a terminal.  *Streaming* terminals --
    :meth:`nodes`, :meth:`edges`, iteration -- yield lazily; *aggregate*
    terminals -- :meth:`names`, :meth:`to_dict`, :meth:`count`, :meth:`first` --
    consume the stream and return a concrete value.

    A *relation step* moves from the current nodes to their neighbours across
    one :class:`EdgeKind`, in a direction:

    * ``"out"``  -- follow ``src --kind--> dst`` (this node is the *source*)
    * ``"in"``   -- follow it backwards (this node is the *target*)
    * ``"both"`` -- either orientation

    Worked answers to the motivating questions::

        eg.query("Shape").derived().names()            # classes that inherit Shape
        eg.query("OrderService").uses().names()         # classes used by OrderService
        eg.query("Shape").derived(transitive=True) \\
          .uses().of_kind(EntityKind.CLASS).names()     # what Shape's subtree uses
        eg.query("Logger").used_by().names()            # who uses Logger
    """

    __slots__ = ("_g", "_nodes_src", "_edges_src")

    def __init__(
        self,
        graph: "EntityGraph",
        nodes_src: _NodeSrc,
        edges_src: Optional[_EdgeSrc] = None,
    ) -> None:
        self._g = graph
        #: re-runnable thunk -> fresh iterator of DISTINCT nodes
        self._nodes_src = nodes_src
        #: re-runnable thunk -> fresh iterator of the LAST step's edges
        self._edges_src: _EdgeSrc = edges_src or (lambda: iter(()))

    # -- the one general step ---------------------------------------------- #

    def relation(
        self,
        kind,
        direction: str = "out",
        *,
        transitive: bool = False,
        max_depth: Optional[int] = None,
    ) -> "EntityQuery":
        """Follow ``kind`` edges from the current nodes (lazy).

        ``kind`` is an :class:`EdgeKind` or its name (e.g. ``"uses"``).
        ``direction`` is ``out`` / ``in`` / ``both``.  With ``transitive=True``
        the step is the cycle-safe transitive closure (and :meth:`edges` is
        then empty -- a closure has no single connecting edge set).

        Nothing runs until a terminal consumes the result; node de-duplication
        happens while streaming (only a ``seen`` set of ids is held).
        """
        if isinstance(kind, str):
            kind = EdgeKind.from_name(kind)
        if direction not in _DIRECTIONS:
            raise ValueError(
                f"direction must be one of {_DIRECTIONS}, got {direction!r}"
            )
        prev = self._nodes_src

        if transitive:
            def nodes_src() -> Iterator[EntityNode]:
                seen: set[int] = set()
                for n in prev():
                    for m in n.walk(kind, direction=direction, max_depth=max_depth):
                        if m.id not in seen:
                            seen.add(m.id)
                            yield m
            return EntityQuery(self._g, nodes_src)

        def nodes_src() -> Iterator[EntityNode]:
            seen: set[int] = set()
            for n in prev():
                if direction in ("out", "both"):
                    for e in n.out_edges(kind):
                        if e.dst.id not in seen:
                            seen.add(e.dst.id)
                            yield e.dst
                if direction in ("in", "both"):
                    for e in n.in_edges(kind):
                        if e.src.id not in seen:
                            seen.add(e.src.id)
                            yield e.src

        def edges_src() -> Iterator[EntityEdge]:
            for n in prev():
                if direction in ("out", "both"):
                    yield from n.out_edges(kind)
                if direction in ("in", "both"):
                    yield from n.in_edges(kind)

        return EntityQuery(self._g, nodes_src, edges_src)

    #: ``then`` reads better as a continuation: ``...derived().then(USES)``.
    then = relation
    step = relation

    # -- named convenience steps ------------------------------------------- #

    def bases(self, *, transitive: bool = False) -> "EntityQuery":
        """Base classes of the current nodes (``generalizes``, out)."""
        return self.relation(EdgeKind.GENERALIZES, "out", transitive=transitive)

    def derived(self, *, transitive: bool = False) -> "EntityQuery":
        """Subclasses of the current nodes (``generalizes``, in)."""
        return self.relation(EdgeKind.GENERALIZES, "in", transitive=transitive)

    def implements(self) -> "EntityQuery":
        """Interfaces the current nodes implement (``implements``, out)."""
        return self.relation(EdgeKind.IMPLEMENTS, "out")

    def implementors(self) -> "EntityQuery":
        """Classes that implement the current interfaces (``implements``, in)."""
        return self.relation(EdgeKind.IMPLEMENTS, "in")

    #: ``interface.implemented_by()`` reads more naturally than ``implementors``.
    implemented_by = implementors

    def uses(self) -> "EntityQuery":
        """Entities the current nodes use (``uses``, out)."""
        return self.relation(EdgeKind.USES, "out")

    def used_by(self) -> "EntityQuery":
        """Entities that use the current nodes (``uses``, in)."""
        return self.relation(EdgeKind.USES, "in")

    def composes(self) -> "EntityQuery":
        """Parts the current nodes own by value (``composes``, out)."""
        return self.relation(EdgeKind.COMPOSES, "out")

    def composed_in(self) -> "EntityQuery":
        """Owners that compose the current nodes (``composes``, in)."""
        return self.relation(EdgeKind.COMPOSES, "in")

    def creates(self) -> "EntityQuery":
        """Entities the current nodes construct (``creates``, out)."""
        return self.relation(EdgeKind.CREATES, "out")

    def created_by(self) -> "EntityQuery":
        """Entities that construct the current nodes (``creates``, in)."""
        return self.relation(EdgeKind.CREATES, "in")

    def friends(self) -> "EntityQuery":
        """Entities the current nodes befriend (``befriends``, out)."""
        return self.relation(EdgeKind.BEFRIENDS, "out")

    def befriended_by(self) -> "EntityQuery":
        """Entities that befriend the current nodes (``befriends``, in)."""
        return self.relation(EdgeKind.BEFRIENDS, "in")

    def instantiates(self) -> "EntityQuery":
        """Primary templates the current specializations instantiate
        (``instantiates``, out). E.g. ``Singleton<Cache>`` -> ``Singleton<T>``."""
        return self.relation(EdgeKind.INSTANTIATES, "out")

    def instances(self) -> "EntityQuery":
        """Specializations/instantiations OF the current primary templates
        (``instantiates``, in). E.g. ``Singleton<T>`` -> ``Singleton<Cache>``."""
        return self.relation(EdgeKind.INSTANTIATES, "in")

    def specializes(self) -> "EntityQuery":
        """The more-general template the current partial specializations
        specialize (``specializes``, out)."""
        return self.relation(EdgeKind.SPECIALIZES, "out")

    def specialized_by(self) -> "EntityQuery":
        """Partial specializations of the current templates (``specializes``, in)."""
        return self.relation(EdgeKind.SPECIALIZES, "in")

    def aggregates(self) -> "EntityQuery":
        """Parts the current nodes hold by reference (``aggregates``, out)."""
        return self.relation(EdgeKind.AGGREGATES, "out")

    def aggregated_in(self) -> "EntityQuery":
        """Owners that aggregate the current nodes (``aggregates``, in)."""
        return self.relation(EdgeKind.AGGREGATES, "in")

    def associates(self) -> "EntityQuery":
        """Entities the current nodes associate with (``associates``, out)."""
        return self.relation(EdgeKind.ASSOCIATES, "out")

    def associated_with(self) -> "EntityQuery":
        """Entities that associate with the current nodes (``associates``, in)."""
        return self.relation(EdgeKind.ASSOCIATES, "in")

    def destroys(self) -> "EntityQuery":
        """Entities the current nodes destroy (``destroys``, out)."""
        return self.relation(EdgeKind.DESTROYS, "out")

    def destroyed_by(self) -> "EntityQuery":
        """Entities that destroy the current nodes (``destroys``, in)."""
        return self.relation(EdgeKind.DESTROYS, "in")

    # -- filters (narrow the current node set) ----------------------------- #

    def where(self, predicate) -> "EntityQuery":
        """Keep only nodes for which ``predicate(node)`` is truthy (lazy)."""
        prev = self._nodes_src
        return EntityQuery(
            self._g,
            lambda: (n for n in prev() if predicate(n)),
            self._edges_src,
        )

    def of_kind(self, *entity_kinds: EntityKind) -> "EntityQuery":
        """Keep only nodes whose entity *type* is one of ``entity_kinds``."""
        wanted = set(entity_kinds)
        return self.where(lambda n: n.kind in wanted)

    def of_class_kind(self, *class_kinds: "ClassKind") -> "EntityQuery":
        """Keep only records whose abstractness is one of ``class_kinds``."""
        wanted = set(class_kinds)
        return self.where(lambda n: n.class_kind in wanted)

    def interfaces(self) -> "EntityQuery":
        """Keep only pure interfaces (``ClassKind.INTERFACE``)."""
        return self.where(lambda n: n.is_interface)

    def abstract(self) -> "EntityQuery":
        """Keep only abstract records -- pure-virtual present (interfaces too)."""
        return self.where(lambda n: n.is_abstract)

    def concrete(self) -> "EntityQuery":
        """Keep only concrete records (no pure-virtual methods)."""
        return self.where(lambda n: n.class_kind is ClassKind.CONCRETE)

    def named(self, substring: str) -> "EntityQuery":
        """Keep only nodes whose name contains ``substring`` (case-insensitive)."""
        needle = substring.lower()
        return self.where(lambda n: needle in n.name.lower())

    def exclude(self, *others) -> "EntityQuery":
        """Drop the given entities from the set (lazy).

        Each argument is resolved like a query seed (node / id / USR / name or
        pattern), so ``.exclude("Circle")`` works as readily as passing a node.
        The (small) exclusion set is resolved once, when the step is built.
        """
        drop = frozenset(n.id for n in self._g._resolve_seeds(others))
        prev = self._nodes_src
        return EntityQuery(
            self._g,
            lambda: (n for n in prev() if n.id not in drop),
            self._edges_src,
        )

    # -- terminals --------------------------------------------------------- #
    # Streaming terminals (nodes / edges / iteration) yield lazily; aggregate
    # terminals (names / to_dict / count / first) consume the stream.

    def nodes(self) -> Iterator[EntityNode]:
        """Stream the current entities (de-duplicated). Lazy generator."""
        return self._nodes_src()

    def names(self) -> list[str]:
        """Names of the current entities, sorted (an explicit aggregate).

        Bare qualified names (``app::Singleton``); use :meth:`displays` to keep
        template arguments (``app::Singleton<app::Cache>``)."""
        return sorted(n.name for n in self._nodes_src())

    def displays(self) -> list[str]:
        """Display names of the current entities, sorted -- includes template
        arguments so a specialization is distinct from its primary template."""
        return sorted(n.display for n in self._nodes_src())

    def edges(self) -> Iterator[EntityEdge]:
        """Stream the edges produced by the most recent (non-transitive) step."""
        return self._edges_src()

    def first(self) -> Optional[EntityNode]:
        """First entity, or ``None`` -- short-circuits the pipeline."""
        return next(self._nodes_src(), None)

    def count(self) -> int:
        """Number of entities (consumes the stream)."""
        return sum(1 for _ in self._nodes_src())

    def to_dict(self) -> list[dict]:
        """JSON-ready node dicts for the current set (an explicit aggregate)."""
        return [n.to_dict() for n in self._nodes_src()]

    def __iter__(self) -> Iterator[EntityNode]:
        return self._nodes_src()

    def __len__(self) -> int:
        return self.count()

    def __bool__(self) -> bool:
        return next(self._nodes_src(), None) is not None

    def __repr__(self) -> str:
        head = list(islice(self._nodes_src(), 6))
        names = ", ".join(n.name for n in head[:5])
        more = "" if len(head) <= 5 else ", +…"
        return f"<EntityQuery [{names}{more}]>"


def open_entity_graph(
    db_path: Optional[str] = None, require_edges: bool = False
) -> EntityGraph:
    """Open the standard cidx index and wrap its entity graph."""
    return EntityGraph(open_query(db_path, require_edges=require_edges))

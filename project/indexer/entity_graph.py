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
from typing import Iterator, Optional

from .query import GraphQuery, Sym, open_query

__all__ = [
    "EdgeKind",
    "EntityKind",
    "Multiplicity",
    "Access",
    "CreateForm",
    "EntityNode",
    "EntityEdge",
    "EntityGraph",
    "open_entity_graph",
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
            "src": self.src.name,
            "kind": self.kind.verb,
            "dst": self.dst.name,
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
        return f"{self.src.name} --{self.kind.verb}--> {self.dst.name}{tag}"


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

    # -- edges -------------------------------------------------------------- #

    def out_edges(self, kind: Optional[EdgeKind] = None) -> list[EntityEdge]:
        """Edges where this node is the source (``self --kind--> ?``)."""
        return self._graph.edges(src=self, kind=kind)

    def in_edges(self, kind: Optional[EdgeKind] = None) -> list[EntityEdge]:
        """Edges where this node is the target (``? --kind--> self``)."""
        return self._graph.edges(dst=self, kind=kind)

    def neighbors(
        self,
        kind: Optional[EdgeKind] = None,
        direction: str = "out",
    ) -> list["EntityNode"]:
        """Distinct adjacent entities. ``direction`` is out / in / both."""
        seen: dict[int, EntityNode] = {}
        if direction in ("out", "both"):
            for e in self.out_edges(kind):
                seen.setdefault(e.dst.id, e.dst)
        if direction in ("in", "both"):
            for e in self.in_edges(kind):
                seen.setdefault(e.src.id, e.src)
        return list(seen.values())

    # -- structural shortcuts ---------------------------------------------- #

    def bases(self, *, transitive: bool = False) -> list["EntityNode"]:
        """Entities this one generalizes-to (direct base classes)."""
        if not transitive:
            return [e.dst for e in self.out_edges(EdgeKind.GENERALIZES)]
        return self.walk(EdgeKind.GENERALIZES, direction="out")

    def derived(self, *, transitive: bool = False) -> list["EntityNode"]:
        """Entities that generalize to this one (direct subclasses)."""
        if not transitive:
            return [e.src for e in self.in_edges(EdgeKind.GENERALIZES)]
        return self.walk(EdgeKind.GENERALIZES, direction="in")

    def parts(self) -> list[EntityEdge]:
        """Composition edges (strong ownership: ``composes``)."""
        return self.out_edges(EdgeKind.COMPOSES)

    def uses(self) -> list[EntityEdge]:
        """Behavioural-dependency edges (``uses``)."""
        return self.out_edges(EdgeKind.USES)

    def creates(self) -> list[EntityEdge]:
        return self.out_edges(EdgeKind.CREATES)

    def friends(self) -> list["EntityNode"]:
        return [e.dst for e in self.out_edges(EdgeKind.BEFRIENDS)]

    # -- traversal ---------------------------------------------------------- #

    def walk(
        self,
        kind: EdgeKind,
        direction: str = "out",
        *,
        max_depth: Optional[int] = None,
    ) -> list["EntityNode"]:
        """Transitive closure following ``kind`` edges (BFS, excludes self).

        Cycle-safe. ``direction`` is out (follow src->dst) or in (dst->src).
        """
        seen: dict[int, EntityNode] = {}
        frontier: list[tuple[EntityNode, int]] = [(self, 0)]
        while frontier:
            node, depth = frontier.pop(0)
            if max_depth is not None and depth >= max_depth:
                continue
            nxt = node.neighbors(kind, direction)
            for n in nxt:
                if n.id != self.id and n.id not in seen:
                    seen[n.id] = n
                    frontier.append((n, depth + 1))
        return list(seen.values())

    # -- dunder ------------------------------------------------------------- #

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind.name.lower(),
            "component": self.component,
            "location": self.location,
        }

    def __eq__(self, other) -> bool:
        return isinstance(other, EntityNode) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return f"<{self.kind.name.lower()} {self.name}>"


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
        node = EntityNode(sym, self)
        self._node_cache[sym.id] = node
        return node

    def entities(self) -> list[EntityNode]:
        """All entities that participate in at least one edge (sorted by name)."""
        rows = self._c.execute(
            "SELECT DISTINCT id FROM ("
            "  SELECT src_id AS id FROM entity_edge "
            "  UNION SELECT dst_id AS id FROM entity_edge"
            ")"
        ).fetchall()
        nodes = [self.entity(r[0]) for r in rows]
        nodes = [n for n in nodes if n is not None]
        nodes.sort(key=lambda n: n.name)
        return nodes

    def find(self, pattern: str, limit: int = 50) -> list[EntityNode]:
        """Fuzzy qualified-name lookup, filtered to entities in the graph."""
        in_graph = {n.id for n in self.entities()}
        out: list[EntityNode] = []
        for sym in self._q.find(pattern, limit=limit):
            if sym.id in in_graph:
                node = self.entity(sym)
                if node is not None:
                    out.append(node)
        return out

    # -- edge access -------------------------------------------------------- #

    def edges(
        self,
        kind: Optional[EdgeKind] = None,
        src=None,
        dst=None,
    ) -> list[EntityEdge]:
        """Materialised edges, optionally filtered by kind / src / dst.

        Sorted by (src name, kind, dst name) for stable, readable dumps.
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
        rows = self._c.execute(
            f"SELECT {_EDGE_COLS} FROM entity_edge {where_sql}", params
        ).fetchall()
        edges = [self._edge(r) for r in rows]
        edges = [e for e in edges if e is not None]
        edges.sort(key=lambda e: (e.src.name, int(e.kind), e.dst.name))
        return edges

    def by_kind(self, kind: EdgeKind) -> list[EntityEdge]:
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
        return {
            "entities": len(self.entities()),
            "edges": total,
            "by_kind": per_kind,
        }

    def __iter__(self) -> Iterator[EntityEdge]:
        return iter(self.edges())

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


def open_entity_graph(
    db_path: Optional[str] = None, require_edges: bool = False
) -> EntityGraph:
    """Open the standard cidx index and wrap its entity graph."""
    return EntityGraph(open_query(db_path, require_edges=require_edges))

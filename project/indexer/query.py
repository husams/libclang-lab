"""indexer.query -- read-only, ground-the-LLM graph API over a cidx index.db.

This module lets an agent (or human) REASON over a large code graph without
loading source files into context. You ask graph questions -- who calls X, what
does X call, X's class hierarchy, the real run-time targets of a virtual call --
and get back compact dataclasses that always carry a resolved `file:line` so the
answer can be grounded.

It is strictly read-only: the database is opened with `?mode=ro`, no schema is
created or migrated, and no write methods exist. It depends only on the standard
library (sqlite3), matching storage.py's zero-dependency style.

The schema it reads is cidx schema v7 (see storage.py):

    component / directory / file   location of every symbol (abs path is rebuilt
                                   by joining component.path / directory.path / name)
    symbol                         one decl/def, keyed by clang USR
    edge (+ edge_kind, edge_site)  typed relationships between symbols

Edge kinds (edge.kind -> edge_kind.name):
    1 calls   2 inherits   3 contains   4 specializes   5 instantiates
    6 overrides   7 uses   8 field_of   9 method_of

Edge-direction conventions (the gotchas the traversals get right):
    calls       src=caller, dst=callee.   callers(X) = inbound;  callees(X) = outbound.
    overrides   src=derived, dst=base.    overridden_by(base) = inbound `overrides`.
    inherits    src=derived, dst=base.    subclasses(base) = inbound.
    field_of /  src=member,  dst=record.  a record's members are INbound, while
    method_of                             `contains` is OUTbound (scope->child);
                                          members() unions both.

Quick start:

    from indexer.query import GraphQuery, open_query
    g = open_query()                       # standard DB (INDEXER_CACHE/index.db)
    fn = g.find("rd_kafka_new")[0]         # fuzzy lookup
    for s in g.callers(fn):                # who calls it (inbound `calls`)
        print(s)
    for t in g.dispatch_targets(method):   # virtual method -> run-time targets
        print(t)
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

# edge_kind.id <-> name -- seeded identically by the indexer (storage.py). We
# hardcode to avoid a query and to validate any DB that disagrees.
EDGE_KINDS = {
    "calls": 1, "inherits": 2, "contains": 3, "specializes": 4,
    "instantiates": 5, "overrides": 6, "uses": 7, "field_of": 8, "method_of": 9,
}
EDGE_NAMES = {v: k for k, v in EDGE_KINDS.items()}

_CACHE_ENV = "INDEXER_CACHE"
_DEFAULT_CACHE = "~/.cache/cidx"
_INDEX_NAME = "index.db"


class NoIndexError(FileNotFoundError):
    """No index database at the requested path."""


class NoEdgesError(RuntimeError):
    """The index has no graph edges (indexed with --no-graph, or never resolved).

    Graph queries are meaningless without edges, so they must not silently fall
    back to another database -- they raise this instead.
    """


def default_db_path() -> str:
    """The standard cidx index path: $INDEXER_CACHE/index.db else ~/.cache/cidx/index.db.

    Mirrors indexer.cli.index_path() so the library and the CLI agree on the one
    canonical location.
    """
    cache = os.environ.get(_CACHE_ENV) or _DEFAULT_CACHE
    return os.path.join(os.path.expanduser(cache), _INDEX_NAME)


def open_query(db_path: Optional[str] = None,
               require_edges: bool = False) -> "GraphQuery":
    """Open the standard cidx index read-only. `db_path` overrides discovery."""
    return GraphQuery(db_path or default_db_path(), require_edges=require_edges)


# --------------------------------------------------------------------------- #
# Compact value types -- terse __repr__ so dumping a list stays token-cheap.
# Each carries the resolved file path + line so a caller can ground its claims.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Sym:
    """A symbol (declaration/definition). `name` is the qualified name."""
    id: int
    usr: str
    spelling: str
    name: str                 # qual_name, else spelling
    kind: str
    type_info: Optional[str]
    is_definition: bool
    is_pure: bool             # C++ pure-virtual (= 0): no own body exists
    access: Optional[str]     # public/protected/private (C++)
    parent_usr: Optional[str]
    resolved: bool
    component: Optional[str]
    file: Optional[str]       # abs path of best-known location, or None (stub)
    line: Optional[int]
    col: Optional[int]

    @property
    def loc(self) -> str:
        if not self.file:
            return "<no-location>"
        base = os.path.basename(self.file)
        return f"{base}:{self.line}" if self.line else base

    @property
    def is_stub(self) -> bool:
        """A minted placeholder for a target that was never indexed (storage
        mints these with spelling='' and resolved=0)."""
        return self.spelling == "" and not self.resolved

    def to_dict(self) -> dict[str, Any]:
        """Stable JSON-serializable view. Identical-by-spec to the C++ port."""
        return {
            "id": self.id,
            "usr": self.usr,
            "spelling": self.spelling,
            "qual_name": self.name,
            "kind": self.kind,
            "type_info": self.type_info,
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "is_definition": self.is_definition,
            "is_pure": self.is_pure,
            "is_stub": self.is_stub,
        }

    def __repr__(self) -> str:
        nm = self.name or self.usr
        tag = " stub" if self.is_stub else ""
        return f"Sym(#{self.id} {self.kind} {nm} @{self.loc}{tag})"


@dataclass(frozen=True)
class Edge:
    """A typed relationship. `peer` is the symbol at the other end."""
    edge_id: int
    kind: str                 # edge_kind name
    src_id: int
    dst_id: int
    peer: Sym                 # the neighbor reached by following this edge
    count: int                # call/use multiplicity
    base_access: Optional[int]
    is_virtual: Optional[int]

    def to_dict(self, sites: Optional[Sequence["Site"]] = None) -> dict[str, Any]:
        """Stable JSON view: the peer symbol's fields, plus edge metadata.

        The result is the *peer* (id/usr/qual_name/kind/file/line) augmented with
        the edge `kind`, `count`, and -- when provided -- `sites[]`. This is the
        shape the cidx-graph skill consumes for callers/callees/refs/neighbors.
        """
        d = self.peer.to_dict()
        d["edge_kind"] = self.kind
        d["count"] = self.count
        if self.base_access is not None:
            d["base_access"] = self.base_access
        if self.is_virtual is not None:
            d["is_virtual"] = bool(self.is_virtual)
        d["sites"] = [s.to_dict() for s in sites] if sites is not None else []
        return d

    def __repr__(self) -> str:
        extra = f" x{self.count}" if self.count and self.count != 1 else ""
        return f"Edge({self.kind}{extra} -> {self.peer!r})"


@dataclass(frozen=True)
class Site:
    """A concrete source location where an edge occurs -- the grounding."""
    file: Optional[str]
    line: Optional[int]
    col: Optional[int]
    conditional: bool         # inside an #if / template that may not compile
    args_sig: Optional[str]

    @property
    def loc(self) -> str:
        if not self.file:
            return "<no-location>"
        base = os.path.basename(self.file)
        return f"{base}:{self.line}:{self.col}" if self.line else base

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "conditional": self.conditional,
            "args_sig": self.args_sig,
        }

    def __repr__(self) -> str:
        c = " (conditional)" if self.conditional else ""
        return f"Site({self.loc}{c})"


# --------------------------------------------------------------------------- #
# The query handle.
# --------------------------------------------------------------------------- #

_SYM_COLS = (
    "s.id, s.usr, s.spelling, s.qual_name, s.kind, s.type_info, "
    "s.file_id, s.line, s.col, s.decl_file_id, s.decl_line, s.decl_col, "
    "s.is_definition, s.is_pure, s.access, s.parent_usr, s.resolved"
)


class GraphQuery:
    """Read-only handle on a cidx index database.

    Every method returns compact `Sym` / `Edge` / `Site` values or lists of them.
    Traversals are bounded by `limit`/`depth` -- never unbounded -- so a query
    over a million-edge graph still hands back a small, reasonable result.

    Construct from a path (opened read-only) or wrap an existing sqlite3
    connection with `GraphQuery.from_connection(conn)` (used by tests that seed
    an in-memory DB and by code that already holds a Storage connection).
    """

    def __init__(self, db_path: str, *, require_edges: bool = False):
        if not os.path.exists(db_path):
            raise NoIndexError(
                f"no cidx index at {db_path!r}. Build one with:\n"
                "    cd <repo> && cidx add-source --path . && cidx import "
                "--db <build> && cidx index && cidx resolve\n"
                "or pass --db PATH / set $INDEXER_CACHE."
            )
        # Read-only: file:...?mode=ro guards against accidental writes.
        uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        self._c = sqlite3.connect(uri, uri=True)
        self._c.row_factory = sqlite3.Row
        self.db_path = db_path
        self._owns_conn = True
        self._file_cache: Optional[dict[int, tuple[str, Optional[str]]]] = None
        self._resolved: Optional[bool] = None
        if require_edges:
            self.require_edges()

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection,
                        db_path: str = "<connection>") -> "GraphQuery":
        """Wrap an already-open sqlite3 connection (does not take ownership)."""
        self = cls.__new__(cls)
        conn.row_factory = sqlite3.Row
        self._c = conn
        self.db_path = db_path
        self._owns_conn = False
        self._file_cache = None
        self._resolved = None
        return self

    # -- lifecycle ----------------------------------------------------------- #

    def close(self) -> None:
        if self._owns_conn:
            self._c.close()

    def __enter__(self) -> "GraphQuery":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- guards -------------------------------------------------------------- #

    def edge_count(self) -> int:
        """Total number of edges. 0 means the graph layer is empty."""
        return self._c.execute("SELECT COUNT(*) FROM edge").fetchone()[0]

    def require_edges(self) -> None:
        """Raise NoEdgesError unless the index has at least one edge.

        The standard-DB discipline: a graph query against an edge-less index is a
        hard error, never a silent fall-back to another database.
        """
        if self.edge_count() == 0:
            raise NoEdgesError(
                f"index {self.db_path!r} has no graph edges -- it was built with "
                "`cidx index --no-graph`, or the graph was cleared. Re-run "
                "`cidx index` (without --no-graph) then `cidx resolve`."
            )

    def _is_resolved(self) -> bool:
        """True once `cidx resolve` has rolled up edge counts (meta flag set).

        When unset, edge.count is not authoritative, so multiplicity falls back
        to COUNT(edge_site)."""
        if self._resolved is None:
            row = self._c.execute(
                "SELECT value FROM meta WHERE key = 'graph_resolved_at'"
            ).fetchone()
            self._resolved = bool(row and row[0])
        return self._resolved

    # -- internal: file path / Sym construction ------------------------------ #

    def _files(self) -> dict[int, tuple[str, Optional[str]]]:
        """{file_id: (abs_path, component_name)} -- loaded once, cached."""
        if self._file_cache is None:
            cache: dict[int, tuple[str, Optional[str]]] = {}
            for r in self._c.execute(
                "SELECT f.id AS fid, c.name AS cname, c.path AS root, "
                "       d.path AS rel, f.name AS name "
                "FROM file f JOIN directory d ON d.id = f.directory_id "
                "JOIN component c ON c.id = d.component_id"
            ):
                path = (os.path.join(r["root"], r["rel"], r["name"])
                        if r["rel"] else os.path.join(r["root"], r["name"]))
                cache[r["fid"]] = (path, r["cname"])
            self._file_cache = cache
        return self._file_cache

    def _sym(self, r: sqlite3.Row) -> Sym:
        files = self._files()
        fid, line, col = r["file_id"], r["line"], r["col"]
        if fid is None:                       # decl-only: fall back to decl site
            fid, line, col = r["decl_file_id"], r["decl_line"], r["decl_col"]
        path, comp = files.get(fid, (None, None)) if fid is not None else (None, None)
        return Sym(
            id=r["id"], usr=r["usr"], spelling=r["spelling"],
            name=r["qual_name"] or r["spelling"], kind=r["kind"],
            type_info=r["type_info"], is_definition=bool(r["is_definition"]),
            is_pure=bool(r["is_pure"]), access=r["access"],
            parent_usr=r["parent_usr"], resolved=bool(r["resolved"]),
            component=comp, file=path, line=line, col=col,
        )

    @staticmethod
    def _resolve_id(sym) -> int:
        return sym.id if isinstance(sym, Sym) else int(sym)

    def _kind_ids(self, kinds: Optional[Iterable[str]]) -> Optional[list[int]]:
        if kinds is None:
            return None
        out = []
        for k in kinds:
            if k not in EDGE_KINDS:
                raise ValueError(
                    f"unknown edge kind {k!r}; valid: {sorted(EDGE_KINDS)}")
            out.append(EDGE_KINDS[k])
        return out

    # ===================================================================== #
    # 1. LOOKUP SYMBOLS
    # ===================================================================== #

    def get(self, ident) -> Optional[Sym]:
        """Fetch one symbol by integer id, USR string, or pass-through Sym."""
        if isinstance(ident, Sym):
            return ident
        col = "id" if isinstance(ident, int) else "usr"
        r = self._c.execute(
            f"SELECT {_SYM_COLS} FROM symbol s WHERE s.{col} = ?", (ident,)
        ).fetchone()
        return self._sym(r) if r else None

    def find(self, pattern: str, kind: Optional[str] = None,
             limit: int = 50) -> list[Sym]:
        """Fuzzy lookup by qualified name. '::'-separated segments must appear in
        order: find('conf::set') matches 'RdKafka::ConfImpl::set'. Shortest names
        first (the closest matches). `kind` filters by symbol.kind.

        Mirrors storage.search_symbols, but over COALESCE(qual_name, spelling) so
        C symbols (no qual_name) are still found."""
        like = "%" + "%".join(
            seg.replace("%", r"\%").replace("_", r"\_")
            for seg in pattern.split("::") if seg
        ) + "%"
        sql = (f"SELECT {_SYM_COLS} FROM symbol s "
               r"WHERE COALESCE(s.qual_name, s.spelling) LIKE ? ESCAPE '\'")
        args: list = [like]
        if kind:
            sql += " AND s.kind = ?"
            args.append(kind)
        sql += (" ORDER BY LENGTH(COALESCE(s.qual_name, s.spelling)), "
                "COALESCE(s.qual_name, s.spelling) LIMIT ?")
        args.append(limit)
        return [self._sym(r) for r in self._c.execute(sql, args)]

    def by_name(self, spelling: str, kind: Optional[str] = None) -> list[Sym]:
        """Exact-spelling lookup (overloads/statics yield several rows)."""
        sql = f"SELECT {_SYM_COLS} FROM symbol s WHERE s.spelling = ?"
        args: list = [spelling]
        if kind:
            sql += " AND s.kind = ?"
            args.append(kind)
        sql += " ORDER BY s.usr"
        return [self._sym(r) for r in self._c.execute(sql, args)]

    def symbols_in_file(self, path_substr: str, limit: int = 500) -> list[Sym]:
        """Symbols whose definition file path contains `path_substr`. Useful to
        enumerate a file's API without opening it."""
        ids = [fid for fid, (p, _) in self._files().items() if path_substr in p]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return [self._sym(r) for r in self._c.execute(
            f"SELECT {_SYM_COLS} FROM symbol s WHERE s.file_id IN ({q}) "
            f"ORDER BY s.line, s.col LIMIT ?", (*ids, limit))]

    # ===================================================================== #
    # 2. LOOKUP REFERENCES
    # ===================================================================== #

    def edges_in(self, sym, kinds: Optional[Sequence[str]] = None,
                 limit: int = 500) -> list[Edge]:
        """Incoming edges: who points AT this symbol (peer = the source)."""
        return self._edges(sym, "in", kinds, limit)

    def edges_out(self, sym, kinds: Optional[Sequence[str]] = None,
                  limit: int = 500) -> list[Edge]:
        """Outgoing edges: what this symbol points to (peer = the destination)."""
        return self._edges(sym, "out", kinds, limit)

    def _edges(self, sym, direction: str, kinds, limit: int) -> list[Edge]:
        sid = self._resolve_id(sym)
        kids = self._kind_ids(kinds)
        if direction == "in":
            mine, peer = "dst_id", "src_id"
        elif direction == "out":
            mine, peer = "src_id", "dst_id"
        else:
            raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
        # count: edge.count is authoritative only after `cidx resolve`; otherwise
        # fall back to COUNT(edge_site) for true multiplicity.
        if self._is_resolved():
            count_expr = "e.count"
        else:
            count_expr = ("(SELECT COUNT(*) FROM edge_site es "
                          "WHERE es.edge_id = e.id)")
        # alias e.kind -> ekind so it does not collide with symbol.kind (sqlite3.Row
        # returns the FIRST column on a name clash, which would mislabel the peer).
        sql = (f"SELECT e.id AS eid, e.src_id, e.dst_id, e.kind AS ekind, "
               f"{count_expr} AS ecount, e.count AS rawcount, "
               f"e.base_access, e.is_virtual, {_SYM_COLS} "
               f"FROM edge e JOIN symbol s ON s.id = e.{peer} "
               f"WHERE e.{mine} = ?")
        args: list = [sid]
        if kids:
            sql += f" AND e.kind IN ({','.join('?' * len(kids))})"
            args.extend(kids)
        sql += " ORDER BY ecount DESC, e.kind LIMIT ?"
        args.append(limit)
        out = []
        for r in self._c.execute(sql, args):
            cnt = r["ecount"]
            if not cnt:                       # no sites recorded -> at least 1
                cnt = r["rawcount"] or 1
            out.append(Edge(
                edge_id=r["eid"], kind=EDGE_NAMES[r["ekind"]],
                src_id=r["src_id"], dst_id=r["dst_id"], peer=self._sym(r),
                count=cnt, base_access=r["base_access"],
                is_virtual=r["is_virtual"],
            ))
        return out

    def references(self, sym, limit: int = 500) -> list[Edge]:
        """All incoming `calls` + `uses` edges -- "who references this symbol".
        Each Edge.peer is the referrer; Edge.count is how many times; follow with
        sites() for exact file:line locations."""
        return self.edges_in(sym, kinds=("calls", "uses"), limit=limit)

    def callers(self, sym, limit: int = 500) -> list[Sym]:
        """Symbols that call `sym` (incoming `calls`)."""
        return [e.peer for e in self.edges_in(sym, ("calls",), limit)]

    def callees(self, sym, limit: int = 500) -> list[Sym]:
        """Symbols that `sym` calls (outgoing `calls`)."""
        return [e.peer for e in self.edges_out(sym, ("calls",), limit)]

    def sites(self, edge, limit: int = 200) -> list[Site]:
        """Concrete source locations for an edge (the file:line grounding).
        Accepts an Edge or a raw edge_id."""
        eid = edge.edge_id if isinstance(edge, Edge) else int(edge)
        files = self._files()
        out = []
        for r in self._c.execute(
            "SELECT file_id, line, col, conditional, args_sig "
            "FROM edge_site WHERE edge_id = ? ORDER BY file_id, line, col LIMIT ?",
            (eid, limit),
        ):
            p = files.get(r["file_id"], (None, None))[0] if r["file_id"] else None
            out.append(Site(file=p, line=r["line"], col=r["col"],
                            conditional=bool(r["conditional"]),
                            args_sig=r["args_sig"]))
        return out

    # ===================================================================== #
    # 3. NAVIGATION (walk the graph)
    # ===================================================================== #

    def neighbors(self, sym, kinds: Optional[Sequence[str]] = None,
                  direction: str = "out", limit: int = 500) -> list[Sym]:
        """One hop. direction='out'|'in'. Returns the peer symbols."""
        edges = self._edges(sym, direction, kinds, limit)
        return [e.peer for e in edges]

    def walk(self, start, kinds: Sequence[str], direction: str = "out",
             depth: int = 3, max_nodes: int = 500) -> "Traversal":
        """Bounded BFS from `start` over edges of `kinds` in one `direction`.

        Returns a Traversal recording each reached symbol with its minimum depth
        and the parent it was first reached from -- so you can reconstruct paths
        without re-querying. Bounded by `depth` and `max_nodes`."""
        start_sym = self.get(start)
        if start_sym is None:
            return Traversal({}, {}, {})
        seen: dict[int, Sym] = {start_sym.id: start_sym}
        level: dict[int, int] = {start_sym.id: 0}
        parent: dict[int, Optional[int]] = {start_sym.id: None}
        frontier = [start_sym.id]
        for d in range(1, depth + 1):
            nxt = []
            for nid in frontier:
                for e in self._edges(nid, direction, kinds, limit=max_nodes):
                    if e.peer.id not in seen:
                        seen[e.peer.id] = e.peer
                        level[e.peer.id] = d
                        parent[e.peer.id] = nid
                        nxt.append(e.peer.id)
                        if len(seen) >= max_nodes:
                            return Traversal(seen, level, parent)
            if not nxt:
                break
            frontier = nxt
        return Traversal(seen, level, parent)

    def reaches(self, src, dst, kinds: Sequence[str] = ("calls",),
                direction: str = "out", max_depth: int = 8) -> Optional[list[Sym]]:
        """Shortest path of `kinds` edges from `src` to `dst`, or None.

        Answers "can A reach B?" (e.g. does this entrypoint ever call that sink)
        and returns the actual chain for grounding."""
        s, t = self.get(src), self.get(dst)
        if s is None or t is None:
            return None
        if s.id == t.id:
            return [s]
        seen = {s.id}
        parent: dict[int, int] = {}
        frontier = [s.id]
        for _ in range(max_depth):
            nxt = []
            for nid in frontier:
                for peer in self.neighbors(nid, kinds, direction):
                    if peer.id in seen:
                        continue
                    seen.add(peer.id)
                    parent[peer.id] = nid
                    if peer.id == t.id:
                        chain = [t.id]
                        while chain[-1] in parent:
                            chain.append(parent[chain[-1]])
                        return [x for x in (self.get(i) for i in reversed(chain))
                                if x is not None]
                    nxt.append(peer.id)
            if not nxt:
                break
            frontier = nxt
        return None

    # -- class hierarchy (inherits) ----------------------------------------- #

    def bases(self, sym, direct: bool = True) -> list[Sym]:
        """Base classes of `sym` (outgoing `inherits`). direct=False walks up the
        whole hierarchy."""
        if direct:
            return self.neighbors(sym, ("inherits",), "out")
        return [s for s in self.walk(sym, ("inherits",), "out", depth=16).nodes
                if s.id != self._resolve_id(sym)]

    def subclasses(self, sym, direct: bool = True) -> list[Sym]:
        """Derived classes of `sym` (incoming `inherits`). direct=False walks the
        whole subtree."""
        if direct:
            return self.neighbors(sym, ("inherits",), "in")
        return [s for s in self.walk(sym, ("inherits",), "in", depth=16).nodes
                if s.id != self._resolve_id(sym)]

    def members(self, sym) -> list[Sym]:
        """Members of a record/namespace.

        `contains` points scope->child (outbound: namespace members, nested
        types), but `field_of`/`method_of` point member->record (so a record's
        fields and methods are INbound). This unions both so you get the full
        member set regardless of edge direction."""
        out = self.neighbors(sym, ("contains",), "out")
        inn = self.neighbors(sym, ("field_of", "method_of"), "in")
        seen, merged = set(), []
        for s in out + inn:
            if s.id not in seen:
                seen.add(s.id)
                merged.append(s)
        return merged

    # ===================================================================== #
    # 4. DYNAMIC DISPATCH
    # ===================================================================== #

    def overrides(self, method) -> list[Sym]:
        """Base methods that `method` overrides (outgoing `overrides`)."""
        return self.neighbors(method, ("overrides",), "out")

    def overridden_by(self, method) -> list[Sym]:
        """Methods that directly override `method` (incoming `overrides`)."""
        return self.neighbors(method, ("overrides",), "in")

    def is_virtual_method(self, method) -> bool:
        """True if `method` participates in dynamic dispatch -- it is pure, it
        overrides something, or something overrides it."""
        m = self.get(method)
        if m is None:
            return False
        if m.is_pure:
            return True
        return bool(self.overridden_by(m) or self.overrides(m))

    def dispatch_targets(self, method) -> list[Sym]:
        """All concrete methods a virtual call to `method` could land on at run
        time: `method` itself (unless pure-virtual, which has no body) plus every
        method that overrides it, transitively down the class hierarchy.

        This is the core dynamic-dispatch resolver: a single `calls` edge to a
        virtual method understates reality -- the real callee set is this."""
        root = self.get(method)
        if root is None:
            return []
        targets: dict[int, Sym] = {}
        if not root.is_pure:
            targets[root.id] = root
        # BFS down the override chain (incoming `overrides`).
        seen = {root.id}
        frontier = [root.id]
        while frontier:
            nxt = []
            for nid in frontier:
                for d in self.overridden_by(nid):
                    if d.id in seen:
                        continue
                    seen.add(d.id)
                    if not d.is_pure:
                        targets[d.id] = d
                    nxt.append(d.id)
            frontier = nxt
        return list(targets.values())

    def virtual_callees(self, fn) -> list[Sym]:
        """Callees of `fn` that are virtual -- the dispatch points inside it.
        Pair with dispatch_targets() to expand each into its real target set."""
        return [c for c in self.callees(fn) if self.is_virtual_method(c)]

    # ===================================================================== #
    # Introspection
    # ===================================================================== #

    def stats(self) -> dict[str, Any]:
        """Counts that tell you how complete the index is before you trust it."""
        one = lambda s: self._c.execute(s).fetchone()[0]  # noqa: E731
        by_edge = {EDGE_NAMES[r["kind"]]: r["n"] for r in self._c.execute(
            "SELECT kind, COUNT(*) AS n FROM edge GROUP BY kind")}
        return {
            "db": self.db_path,
            "components": one("SELECT COUNT(*) FROM component"),
            "files_indexed": one("SELECT COUNT(*) FROM file WHERE indexed = 1"),
            "symbols": one("SELECT COUNT(*) FROM symbol"),
            "stubs": one("SELECT COUNT(*) FROM symbol WHERE spelling = '' "
                         "AND resolved = 0"),
            "edges": one("SELECT COUNT(*) FROM edge"),
            "edges_by_kind": by_edge,
            "resolved_at": (lambda r: r[0] if r else None)(self._c.execute(
                "SELECT value FROM meta WHERE key = 'graph_resolved_at'").fetchone()),
        }


@dataclass
class Traversal:
    """Result of GraphQuery.walk(): reached symbols + how they were reached."""
    nodes_by_id: dict[int, Sym]
    depth_by_id: dict[int, int]
    parent_by_id: Optional[dict[int, Optional[int]]] = None

    @property
    def nodes(self) -> list[Sym]:
        """All reached symbols, shallowest first."""
        return sorted(self.nodes_by_id.values(),
                      key=lambda s: (self.depth_by_id.get(s.id, 0), s.name))

    def path_to(self, ident) -> list[Sym]:
        """Reconstruct the discovery path from start to a reached symbol."""
        if self.parent_by_id is None:
            raise ValueError("this Traversal did not record parents")
        sid = ident.id if isinstance(ident, Sym) else int(ident)
        if sid not in self.nodes_by_id:
            return []
        chain = [sid]
        while True:
            par = self.parent_by_id.get(chain[-1])
            if par is None:
                break
            chain.append(par)
        return [self.nodes_by_id[i] for i in reversed(chain)]

    def __len__(self) -> int:
        return len(self.nodes_by_id)

    def __repr__(self) -> str:
        return (f"Traversal({len(self.nodes_by_id)} nodes, "
                f"max_depth={max(self.depth_by_id.values(), default=0)})")

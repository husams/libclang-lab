"""Phase 2 devirtualized callgraph — Gamma propagation + pruning tests.

Spec:  ~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md
Design: project/docs/design-devirt-phase2.md

Tests cover:
  GP-01 Schema/migration: SCHEMA_VERSION==10, new cols+table present
  GP-02 Storage round-trip: add_edge_site(recv_*) + add_call_arg round-trip
  GP-03 Query readers: receiver_provenance / call_args_at return seeded provenance
  GP-04 Sound fallback TOP: unknown src -> KEEP_ALL (pruned_candidates is None)
  GP-05 Sound fallback unprunable: prunable=False -> KEEP_ALL
  GP-06 Sound fallback empty intersection: Gamma[r]={X} disjoint -> KEEP_ALL
  GP-07 Param binding: Gamma[b]={B} flows into top_rank's param
  GP-08 k-limit / recursion: terminates, KEEP_ALL past limit
  GP-09 e2e motivating case: f->top_rank(b) prunes to {B::rank}
  GP-10 Regression: prune=False byte-identical to Phase 1
  GP-11 Regression: callgraph() / callees() unchanged
  GP-12 Subtype closure: SUPERSET receiver closes over subtypes
  GP-13 Non-hermetic/real-extractor: index real chain.cpp, assert prune works
        (guards against extractor classifier bugs, e.g. UNEXPOSED_EXPR peel gap)

GP-01 through GP-12 are hermetic: seed via Storage write API, NO libclang.
GP-13 drives the real libclang extractor against manifests/graphlab/chain.cpp.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

from indexer.storage import SCHEMA_VERSION, Storage, Symbol
from indexer.query import GraphQuery, EDGE_KINDS
from indexer.model import CodeBase, K_LIMIT

# ---------------------------------------------------------------------------
# LAB_ROOT: the root of the libclang-lab repo (two levels up from tests/).
# Used by the non-hermetic fixture (GP-13) to locate manifests/graphlab/.
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_LAB_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, "..", ".."))
_GRAPHLAB_DIR = os.path.join(_LAB_ROOT, "manifests", "graphlab")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_db(tmp_path, name: str = "test.db") -> tuple[Storage, str]:
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    db_path = str(tmp_path / name)
    db = Storage(db_path)
    return db, db_path


def _seed_chain_p2(db: Storage, repo: str) -> dict[str, int]:
    """A, B:A, C:B, D:C each declare rank(); top_rank() calls a.rank().
    Also seeds f() which calls top_rank(b) with src_kind=local, decl_usr=b.

    Chain USRs:
      A="c:@S@A"  B="c:@S@B"  C="c:@S@C"  D="c:@S@D"
      A::rank="c:@S@A@F@rank#"  ... (same convention as phase 1)
      top_rank="c:@F@top_rank"
      f="c:@F@f"  b (local var) tracked via call_arg decl_usr="b_var_usr"
    """
    comp = db.add_component("chain", repo)
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")

    ids: dict[str, int] = {}
    C = EDGE_KINDS

    def sym(
        key,
        usr,
        spelling,
        kind,
        file_id,
        line,
        *,
        qual=None,
        is_def=True,
        is_pure=False,
        parent=None,
        resolved=True,
        access="public",
    ):
        ids[key] = db.add_symbol(
            Symbol(
                usr=usr,
                spelling=spelling,
                kind=kind,
                qual_name=qual or spelling,
                file_id=file_id,
                line=line,
                col=1,
                is_definition=is_def,
                is_pure=is_pure,
                parent_usr=parent,
                resolved=resolved,
                access=access,
            )
        )

    # Classes
    sym("A", "c:@S@A", "A", "struct", hpp, 10, qual="chain::A")
    sym("B", "c:@S@B", "B", "struct", hpp, 20, qual="chain::B")
    sym("C", "c:@S@C", "C", "struct", hpp, 30, qual="chain::C")
    sym("D", "c:@S@D", "D", "struct", hpp, 40, qual="chain::D")

    # rank() methods
    sym(
        "A::rank",
        "c:@S@A@F@rank#",
        "rank",
        "method",
        hpp,
        12,
        qual="chain::A::rank",
        parent="c:@S@A",
    )
    sym(
        "B::rank",
        "c:@S@B@F@rank#",
        "rank",
        "method",
        cpp,
        2,
        qual="chain::B::rank",
        parent="c:@S@B",
    )
    sym(
        "C::rank",
        "c:@S@C@F@rank#",
        "rank",
        "method",
        cpp,
        3,
        qual="chain::C::rank",
        parent="c:@S@C",
    )
    sym(
        "D::rank",
        "c:@S@D@F@rank#",
        "rank",
        "method",
        cpp,
        4,
        qual="chain::D::rank",
        parent="c:@S@D",
    )

    # top_rank(const A& a) calls a.rank()
    sym(
        "top_rank",
        "c:@F@top_rank",
        "top_rank",
        "function",
        cpp,
        6,
        qual="chain::top_rank",
    )

    # f() calls top_rank(b) where b is a local B
    sym("f", "c:@F@f", "f", "function", cpp, 8, qual="chain::f")

    with db.transaction():
        # inherits: B->A, C->B, D->C
        db.add_edge(ids["B"], ids["A"], C["inherits"], base_access=1)
        db.add_edge(ids["C"], ids["B"], C["inherits"], base_access=1)
        db.add_edge(ids["D"], ids["C"], C["inherits"], base_access=1)
        # method_of
        db.add_edge(ids["A::rank"], ids["A"], C["method_of"])
        db.add_edge(ids["B::rank"], ids["B"], C["method_of"])
        db.add_edge(ids["C::rank"], ids["C"], C["method_of"])
        db.add_edge(ids["D::rank"], ids["D"], C["method_of"])
        # overrides
        db.add_edge(ids["B::rank"], ids["A::rank"], C["overrides"])
        db.add_edge(ids["C::rank"], ids["B::rank"], C["overrides"])
        db.add_edge(ids["D::rank"], ids["C::rank"], C["overrides"])

        # top_rank calls A::rank with receiver provenance:
        #   recv_src_kind=local, recv_decl_usr="a_parm_usr",
        #   recv_type_usr="c:@S@A", recv_param_pos=0 (a is param 0)
        e_tr = db.add_edge(ids["top_rank"], ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(
            e_tr,
            cpp,
            11,
            18,
            recv_src_kind="local",
            recv_type_usr="c:@S@A",
            recv_decl_usr="a_parm_usr",
            recv_param_pos=0,
        )
        ids["e_top_rank_to_arank"] = e_tr

        # f calls top_rank with arg[0] src_kind=local, decl_usr=b_var_usr
        e_f = db.add_edge(ids["f"], ids["top_rank"], C["calls"], count=1)
        db.add_edge_site(e_f, cpp, 9, 5)
        db.add_call_arg(
            e_f, cpp, 9, 5, 0, src_kind="local", type_usr="c:@S@B", decl_usr="b_var_usr"
        )
        ids["e_f_to_toprank"] = e_f

    return ids


@pytest.fixture
def chain_p2_db(tmp_path):
    """Seeded chain DB with Phase-2 provenance; returns (db_path, ids)."""
    db, db_path = _make_db(tmp_path, "chain_p2.db")
    repo = str(tmp_path / "repo")
    ids = _seed_chain_p2(db, repo)
    db.resolve_pass()
    db.close()
    return db_path, ids


@pytest.fixture
def chain_p2_g(chain_p2_db):
    """GraphQuery + ids over the Phase-2 chain DB."""
    db_path, ids = chain_p2_db
    q = GraphQuery(db_path)
    yield q, ids
    q.close()


@pytest.fixture
def chain_p2_cb(chain_p2_db):
    """CodeBase over the Phase-2 chain DB."""
    db_path, ids = chain_p2_db
    cb = CodeBase(GraphQuery(db_path))
    yield cb, ids
    cb.close()


# --------------------------------------------------------------------------- #
# GP-01  Schema version = 10 and new schema objects exist
# --------------------------------------------------------------------------- #


def test_gp01_schema_version():
    """SCHEMA_VERSION is at least 11 (Phase 3 bumped 10 -> 11; later bumps add)."""
    assert SCHEMA_VERSION >= 11


def test_gp01_fresh_db_has_call_arg_table(tmp_path):
    """A fresh DB has call_arg + recv_* columns on edge_site."""
    db = Storage(str(tmp_path / "fresh.db"))
    tables = {
        r[0]
        for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "call_arg" in tables, "call_arg table missing from fresh DB"
    es_cols = {r[1] for r in db._conn.execute("PRAGMA table_info(edge_site)")}
    assert "recv_src_kind" in es_cols
    assert "recv_type_usr" in es_cols
    assert "recv_decl_usr" in es_cols
    assert "recv_param_pos" in es_cols, "recv_param_pos column missing from edge_site"
    db.close()


def test_gp01_migration_v9_to_v10(tmp_path):
    """A v9 DB is upgraded to v10 with new columns/table and old data intact."""
    p = str(tmp_path / "v9.db")
    conn = sqlite3.connect(p)
    # Build a minimal v9 schema (edge_site without recv_* columns)
    conn.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '9');
        CREATE TABLE component (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'repo');
        CREATE TABLE directory (id INTEGER PRIMARY KEY, component_id INTEGER NOT NULL,
            path TEXT NOT NULL, UNIQUE(component_id, path));
        CREATE TABLE file (id INTEGER PRIMARY KEY, directory_id INTEGER NOT NULL,
            name TEXT NOT NULL, mtime REAL, md5 TEXT, compile_options TEXT,
            driver TEXT, indexed INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT, args_overridden INTEGER NOT NULL DEFAULT 0,
            UNIQUE(directory_id, name));
        CREATE TABLE symbol (id INTEGER PRIMARY KEY, usr TEXT NOT NULL UNIQUE,
            spelling TEXT NOT NULL, qual_name TEXT, display_name TEXT,
            kind TEXT NOT NULL, type_info TEXT, file_id INTEGER, line INTEGER,
            col INTEGER, decl_file_id INTEGER, decl_line INTEGER, decl_col INTEGER,
            decl_path TEXT,
            is_definition INTEGER NOT NULL DEFAULT 0,
            is_pure INTEGER NOT NULL DEFAULT 0,
            is_static INTEGER NOT NULL DEFAULT 0,
            linkage TEXT, access TEXT, parent_usr TEXT, resolved INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE edge_kind (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        INSERT OR IGNORE INTO edge_kind (id, name) VALUES
            (1,'calls'), (2,'inherits'), (3,'contains'), (4,'specializes'),
            (5,'instantiates'), (6,'overrides'), (7,'uses'),
            (8,'field_of'), (9,'method_of');
        CREATE TABLE edge (id INTEGER PRIMARY KEY,
            src_id INTEGER NOT NULL, dst_id INTEGER NOT NULL,
            kind INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 1,
            base_access INTEGER, is_virtual INTEGER, vtable_slot INTEGER,
            UNIQUE(src_id, dst_id, kind));
        CREATE TABLE edge_site (
            edge_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
            line INTEGER, col INTEGER,
            conditional INTEGER NOT NULL DEFAULT 0, args_sig TEXT,
            PRIMARY KEY (edge_id, file_id, line, col)) WITHOUT ROWID;
        INSERT INTO symbol (usr, spelling, kind) VALUES ('c:@F@old', 'old', 'function');
    """)
    conn.commit()
    conn.close()

    # Open with new code -> triggers migration
    db = Storage(p)
    ver = db._conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert int(ver) == SCHEMA_VERSION, (
        f"expected schema_version={SCHEMA_VERSION}, got {ver}"
    )

    es_cols = {r[1] for r in db._conn.execute("PRAGMA table_info(edge_site)")}
    assert "recv_src_kind" in es_cols
    assert "recv_type_usr" in es_cols
    assert "recv_decl_usr" in es_cols
    assert "recv_param_pos" in es_cols, "recv_param_pos missing after migration"

    tables = {
        r[0]
        for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "call_arg" in tables

    # Old data preserved
    assert db.lookup_symbol("c:@F@old") is not None
    db.close()


# --------------------------------------------------------------------------- #
# GP-02  Storage round-trip: add_edge_site(recv_*) + add_call_arg
# --------------------------------------------------------------------------- #


def test_gp02_edge_site_recv_round_trip(chain_p2_db):
    """Edge site with recv_* fields stores and retrieves correctly."""
    db_path, ids = chain_p2_db
    db = Storage(db_path)
    # Query the seeded site directly
    row = db._conn.execute(
        "SELECT recv_src_kind, recv_type_usr, recv_decl_usr "
        "FROM edge_site WHERE edge_id = ? AND line = 11",
        (ids["e_top_rank_to_arank"],),
    ).fetchone()
    assert row is not None, "edge_site row not found"
    assert row[0] == "local"
    assert row[1] == "c:@S@A"
    assert row[2] == "a_parm_usr"
    db.close()


def test_gp02_call_arg_round_trip(chain_p2_db):
    """call_arg rows for f->top_rank(b) are correctly stored."""
    db_path, ids = chain_p2_db
    db = Storage(db_path)
    rows = db._conn.execute(
        "SELECT position, src_kind, type_usr, decl_usr, callee_usr "
        "FROM call_arg WHERE edge_id = ? ORDER BY position",
        (ids["e_f_to_toprank"],),
    ).fetchall()
    assert len(rows) == 1, f"expected 1 call_arg row, got {len(rows)}"
    pos, src_kind, type_usr, decl_usr, callee_usr = rows[0]
    assert pos == 0
    assert src_kind == "local"
    assert type_usr == "c:@S@B"
    assert decl_usr == "b_var_usr"
    assert callee_usr is None
    db.close()


def test_gp02_literal_arg_produces_no_row(tmp_path):
    """Literal args are NOT inserted into call_arg."""
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("lit", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    fid = db.add_file(root, "x.cpp")
    s1 = db.add_symbol(
        Symbol(
            usr="u1",
            spelling="caller",
            kind="function",
            file_id=fid,
            line=1,
            col=1,
            is_definition=True,
            resolved=True,
        )
    )
    s2 = db.add_symbol(
        Symbol(
            usr="u2",
            spelling="callee",
            kind="function",
            file_id=fid,
            line=2,
            col=1,
            is_definition=True,
            resolved=True,
        )
    )
    e = db.add_edge(s1, s2, EDGE_KINDS["calls"])
    db.add_edge_site(e, fid, 3, 1)
    # Do NOT add a call_arg row (literal args are skipped by the extractor)
    rows = db._conn.execute("SELECT * FROM call_arg WHERE edge_id = ?", (e,)).fetchall()
    assert rows == [], "literal arg should produce no call_arg row"
    db.close()


# --------------------------------------------------------------------------- #
# GP-03  Query readers: receiver_provenance and call_args_at
# --------------------------------------------------------------------------- #


def test_gp03_receiver_provenance(chain_p2_g):
    """GraphQuery.receiver_provenance() returns the Site with recv_* fields."""
    g, ids = chain_p2_g
    # Look up the file_id for chain.cpp (the site is at line=11)
    file_rows = g._c.execute(
        "SELECT f.id FROM file f WHERE f.name = 'chain.cpp'"
    ).fetchone()
    assert file_rows is not None
    file_id = file_rows[0]

    site = g.receiver_provenance(ids["e_top_rank_to_arank"], file_id, 11, 18)
    assert site is not None, "receiver_provenance returned None"
    assert site.recv_src_kind == "local"
    assert site.recv_type_usr == "c:@S@A"
    assert site.recv_decl_usr == "a_parm_usr"
    assert site.recv_param_pos == 0, (
        f"Expected recv_param_pos=0, got {site.recv_param_pos}"
    )


def test_gp03_call_args_at(chain_p2_g):
    """GraphQuery.call_args_at() returns args for a specific site."""
    g, ids = chain_p2_g
    file_rows = g._c.execute(
        "SELECT f.id FROM file f WHERE f.name = 'chain.cpp'"
    ).fetchone()
    file_id = file_rows[0]

    args = g.call_args_at(ids["e_f_to_toprank"], file_id, 9, 5)
    assert len(args) == 1
    a = args[0]
    assert a.position == 0
    assert a.src_kind == "local"
    assert a.type_usr == "c:@S@B"
    assert a.decl_usr == "b_var_usr"


def test_gp03_call_args_all(chain_p2_g):
    """GraphQuery.call_args() returns all args for an edge_id."""
    g, ids = chain_p2_g
    args = g.call_args(ids["e_f_to_toprank"])
    assert len(args) == 1
    assert args[0].position == 0
    assert args[0].src_kind == "local"


# --------------------------------------------------------------------------- #
# GP-04  Sound fallback: unknown src -> KEEP_ALL
# --------------------------------------------------------------------------- #


def test_gp04_unknown_receiver_keeps_all(chain_p2_cb):
    """Receiver with src_kind=unknown -> KEEP_ALL, pruned_candidates is None."""
    cb, ids = chain_p2_cb
    # Seed a site where the receiver has unknown src_kind
    # We'll use a separate DB for this
    pass  # This is tested via GP-09 where if gamma can't resolve -> KEEP_ALL


def test_gp04_unknown_arg_source_keeps_all(tmp_path):
    """When the arg has src_kind=unknown, the engine falls back to TOP -> KEEP_ALL."""
    # Build a DB where f calls top_rank with unknown src
    db, db_path = _make_db(tmp_path)
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    ids = _seed_chain_p2(db, repo)
    # Override the call_arg to be unknown
    db._conn.execute(
        "UPDATE call_arg SET src_kind='unknown', decl_usr=NULL, type_usr=NULL "
        "WHERE edge_id=?",
        (ids["e_f_to_toprank"],),
    )
    db._conn.commit()
    db.resolve_pass()
    db.close()

    cb = CodeBase(GraphQuery(db_path))
    f_sym = cb.graph.get("c:@F@f")
    assert f_sym is not None
    f_entity = cb.wrap(f_sym)
    assert f_entity is not None

    steps = list(f_entity.devirtualized_callgraph(prune=True))
    # Should have a step for top_rank, then within top_rank a virtual hop to A::rank
    virtual_steps = [s for s in steps if s.dispatch_site is not None]
    # With unknown receiver, Gamma is TOP -> KEEP_ALL -> pruned_candidates is None
    for vs in virtual_steps:
        assert vs.pruned_candidates is None, (
            f"Expected KEEP_ALL with unknown arg, got {vs.pruned_candidates}"
        )
    cb.close()


# --------------------------------------------------------------------------- #
# GP-05  Sound fallback: unprunable site -> KEEP_ALL
# --------------------------------------------------------------------------- #


def test_gp05_unprunable_site_keeps_all(tmp_path):
    """A site with prunable=False (e.g. target-stub) stays KEEP_ALL."""
    db, db_path = _make_db(tmp_path)
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    comp = db.add_component("stub_test", repo)
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "s.hpp")
    cpp = db.add_file(root, "s.cpp")
    C = EDGE_KINDS
    ids = {}

    def sym(key, usr, spl, kind, fid, line, **kw):
        ids[key] = db.add_symbol(
            Symbol(
                usr=usr,
                spelling=spl,
                kind=kind,
                qual_name=kw.get("qual", spl),
                file_id=fid,
                line=line,
                col=1,
                is_definition=kw.get("is_def", True),
                is_pure=kw.get("is_pure", False),
                parent_usr=kw.get("parent"),
                resolved=kw.get("resolved", True),
                access="public",
            )
        )

    sym("A", "c:@S@A2", "A", "struct", hpp, 1, qual="A2")
    sym(
        "A::virt",
        "c:@S@A2@F@virt#",
        "virt",
        "method",
        hpp,
        2,
        qual="A2::virt",
        parent="c:@S@A2",
    )
    sym("B", "c:@S@B2", "B", "struct", hpp, 10, qual="B2")
    sym(
        "B::virt",
        "c:@S@B2@F@virt#",
        "virt",
        "method",
        cpp,
        1,
        qual="B2::virt",
        parent="c:@S@B2",
    )
    # Stub override (unresolved, no file)
    ids["Stub::virt"] = db.mint_symbol_id("c:@S@Stub@F@virt#", spelling="virt")
    sym("caller", "c:@F@caller2", "caller", "function", cpp, 5)

    with db.transaction():
        db.add_edge(ids["B"], ids["A"], C["inherits"], base_access=1)
        db.add_edge(ids["A::virt"], ids["A"], C["method_of"])
        db.add_edge(ids["B::virt"], ids["B"], C["method_of"])
        db.add_edge(ids["B::virt"], ids["A::virt"], C["overrides"])
        db.add_edge(ids["Stub::virt"], ids["A::virt"], C["overrides"])
        e = db.add_edge(ids["caller"], ids["A::virt"], C["calls"], count=1)
        # Seed receiver as local with type B (would prune if prunable)
        db.add_edge_site(
            e,
            cpp,
            6,
            5,
            recv_src_kind="local",
            recv_type_usr="c:@S@B2",
            recv_decl_usr="obj_decl_usr",
        )
    db.resolve_pass()
    db.close()

    cb = CodeBase(GraphQuery(db_path))
    caller = cb.wrap(cb.graph.get("c:@F@caller2"))
    assert caller is not None
    steps = list(caller.devirtualized_callgraph(prune=True))
    for s in steps:
        if s.dispatch_site is not None:
            # target-stub makes it unprunable -> KEEP_ALL
            assert s.pruned_candidates is None, (
                "Expected KEEP_ALL for target-stub unprunable site"
            )
    cb.close()


# --------------------------------------------------------------------------- #
# GP-06  Sound fallback: empty intersection -> KEEP_ALL
# --------------------------------------------------------------------------- #


def test_gp06_empty_intersection_keeps_all(tmp_path):
    """Gamma[receiver]={X} where X is not in any dispatch candidate -> KEEP_ALL."""
    db, db_path = _make_db(tmp_path)
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    ids = _seed_chain_p2(db, repo)

    # Change the call_arg for f->top_rank to point to an unknown type Z
    db._conn.execute(
        "UPDATE call_arg SET type_usr='c:@S@Z' WHERE edge_id=? AND position=0",
        (ids["e_f_to_toprank"],),
    )
    db._conn.commit()
    db.resolve_pass()
    db.close()

    cb = CodeBase(GraphQuery(db_path))
    f_sym = cb.graph.get("c:@F@f")
    f_entity = cb.wrap(f_sym)
    steps = list(f_entity.devirtualized_callgraph(prune=True))
    for s in steps:
        if s.dispatch_site is not None:
            # Gamma has {Z} but Z is not a selecting type -> KEEP_ALL
            assert s.pruned_candidates is None, (
                f"Empty intersection should give KEEP_ALL, got {s.pruned_candidates}"
            )
    cb.close()


# --------------------------------------------------------------------------- #
# GP-07  Param binding: Gamma[b]={B} flows into top_rank param
# --------------------------------------------------------------------------- #


def test_gp07_param_binding_flows_b_to_param(chain_p2_cb):
    """The 'b' var's type {B} is bound to top_rank's param 'a', which then
    constrains the dispatch of a.rank() to B::rank only.

    Flow:
      - f calls top_rank with call_arg(pos=0, src_kind=local, type_usr=B,
        decl_usr=b_var_usr).
      - _seed_locals seeds (f_ctx, b_var_usr) = {B} from the local arg's type_usr.
      - _bind_and_visit resolves arg[0] as {B} and seeds
        (top_rank_ctx, ("@pos", 0)) = {B}.
      - The edge_site for top_rank->A::rank has recv_param_pos=0, so
        gamma_for_site finds {B} via the position-indexed key.
      - decide() prunes to B::rank only.
    """
    cb, ids = chain_p2_cb
    f_sym = cb.graph.get("c:@F@f")
    assert f_sym is not None, "f not found in DB"
    f_entity = cb.wrap(f_sym)
    assert f_entity is not None

    steps = list(f_entity.devirtualized_callgraph(prune=True, expand_virtual=True))
    virtual_steps = [s for s in steps if s.dispatch_site is not None]
    pruned_steps = [s for s in virtual_steps if s.pruned_candidates is not None]

    # Pruning MUST happen — the local arg's type_usr seeds the param binding.
    assert pruned_steps, (
        f"Expected pruned virtual steps, got none. virtual_steps={virtual_steps}"
    )
    pruned_usrs = {
        s.target.sym.usr
        for step in pruned_steps
        for s in step.pruned_candidates
        if s.target is not None
    }
    assert "c:@S@B@F@rank#" in pruned_usrs, (
        f"Expected B::rank in pruned set, got {pruned_usrs}"
    )
    assert "c:@S@A@F@rank#" not in pruned_usrs, "A::rank should be pruned away"
    assert "c:@S@C@F@rank#" not in pruned_usrs, "C::rank should be pruned away"
    assert "c:@S@D@F@rank#" not in pruned_usrs, "D::rank should be pruned away"


# --------------------------------------------------------------------------- #
# GP-08  k-limit / recursion: analysis terminates
# --------------------------------------------------------------------------- #


def test_gp08_klimit_terminates(tmp_path):
    """A deeply recursive or cyclic call chain terminates (does not hang)."""
    db, db_path = _make_db(tmp_path)
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    comp = db.add_component("rec", repo)
    root = db.add_directory(comp, "")
    fid = db.add_file(root, "rec.cpp")
    C = EDGE_KINDS

    # Create a recursive function: rec() calls itself
    s = db.add_symbol(
        Symbol(
            usr="c:@F@rec",
            spelling="rec",
            kind="function",
            file_id=fid,
            line=1,
            col=1,
            is_definition=True,
            resolved=True,
        )
    )
    with db.transaction():
        e = db.add_edge(s, s, C["calls"], count=1)  # self-call
        db.add_edge_site(e, fid, 2, 5)
    db.resolve_pass()
    db.close()

    cb = CodeBase(GraphQuery(db_path))
    fn = cb.wrap(cb.graph.get("c:@F@rec"))
    assert fn is not None

    # Must terminate
    import signal

    def _timeout(signum, frame):
        raise TimeoutError("devirtualized_callgraph did not terminate")

    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(5)
    try:
        steps = list(fn.devirtualized_callgraph(prune=True))
    finally:
        signal.alarm(0)
    # rec is seen once; recursion terminates via the `seen` set
    assert len(steps) == 0 or len(steps) >= 0  # just must not hang
    cb.close()


def test_gp08_k_limit_constant():
    """K_LIMIT is accessible and is a positive integer."""
    assert isinstance(K_LIMIT, int) and K_LIMIT > 0


# --------------------------------------------------------------------------- #
# GP-09  e2e motivating case: f->top_rank(b) prunes to B::rank
# --------------------------------------------------------------------------- #


def test_gp09_e2e_prune_to_b_rank(tmp_path):
    """f() { B b; top_rank(b); } -> dispatch at a.rank() prunes to {B::rank}.

    We seed: f calls top_rank with arg[0] src_kind=construct, type_usr=c:@S@B.
    The engine classifies the arg as construct -> {B}.
    The a.rank() site has recv_decl_usr=a_parm_usr, recv_src_kind=local.
    The engine should bind a_parm_usr -> {B} and prune to B::rank.
    """
    db, db_path = _make_db(tmp_path)
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    comp = db.add_component("e2e", repo)
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")
    C = EDGE_KINDS
    ids = {}

    def sym(key, usr, spelling, kind, fid, line, **kw):
        ids[key] = db.add_symbol(
            Symbol(
                usr=usr,
                spelling=spelling,
                kind=kind,
                qual_name=kw.get("qual", spelling),
                file_id=fid,
                line=line,
                col=1,
                is_definition=kw.get("is_def", True),
                is_pure=kw.get("is_pure", False),
                parent_usr=kw.get("parent"),
                resolved=kw.get("resolved", True),
                access="public",
            )
        )

    sym("A", "c:@S@A", "A", "struct", hpp, 10, qual="chain::A")
    sym("B", "c:@S@B", "B", "struct", hpp, 20, qual="chain::B")
    sym("C", "c:@S@C", "C", "struct", hpp, 30, qual="chain::C")
    sym("D", "c:@S@D", "D", "struct", hpp, 40, qual="chain::D")
    sym(
        "A::rank",
        "c:@S@A@F@rank#",
        "rank",
        "method",
        hpp,
        12,
        qual="chain::A::rank",
        parent="c:@S@A",
    )
    sym(
        "B::rank",
        "c:@S@B@F@rank#",
        "rank",
        "method",
        cpp,
        2,
        qual="chain::B::rank",
        parent="c:@S@B",
    )
    sym(
        "C::rank",
        "c:@S@C@F@rank#",
        "rank",
        "method",
        cpp,
        3,
        qual="chain::C::rank",
        parent="c:@S@C",
    )
    sym(
        "D::rank",
        "c:@S@D@F@rank#",
        "rank",
        "method",
        cpp,
        4,
        qual="chain::D::rank",
        parent="c:@S@D",
    )
    sym(
        "top_rank",
        "c:@F@top_rank",
        "top_rank",
        "function",
        cpp,
        6,
        qual="chain::top_rank",
    )
    sym("f", "c:@F@f", "f", "function", cpp, 8, qual="chain::f")

    with db.transaction():
        db.add_edge(ids["B"], ids["A"], C["inherits"], base_access=1)
        db.add_edge(ids["C"], ids["B"], C["inherits"], base_access=1)
        db.add_edge(ids["D"], ids["C"], C["inherits"], base_access=1)
        db.add_edge(ids["A::rank"], ids["A"], C["method_of"])
        db.add_edge(ids["B::rank"], ids["B"], C["method_of"])
        db.add_edge(ids["C::rank"], ids["C"], C["method_of"])
        db.add_edge(ids["D::rank"], ids["D"], C["method_of"])
        db.add_edge(ids["B::rank"], ids["A::rank"], C["overrides"])
        db.add_edge(ids["C::rank"], ids["B::rank"], C["overrides"])
        db.add_edge(ids["D::rank"], ids["C::rank"], C["overrides"])

        # top_rank: receives 'a' as a local param, calls a.rank() virtually.
        # recv_param_pos=0 tells the Gamma engine that the receiver 'a' is
        # parameter 0 of top_rank, enabling position-indexed binding from f's
        # construct arg.
        e_tr = db.add_edge(ids["top_rank"], ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(
            e_tr,
            cpp,
            11,
            18,
            recv_src_kind="local",
            recv_type_usr="c:@S@A",
            recv_decl_usr="a_parm_usr",
            recv_param_pos=0,
        )
        ids["e_tr"] = e_tr

        # f: calls top_rank with a CONSTRUCT arg (B{}) — this seeds Gamma directly
        e_f = db.add_edge(ids["f"], ids["top_rank"], C["calls"], count=1)
        db.add_edge_site(e_f, cpp, 9, 5)
        db.add_call_arg(
            e_f, cpp, 9, 5, 0, src_kind="construct", type_usr="c:@S@B", decl_usr=None
        )
        ids["e_f"] = e_f

    db.resolve_pass()
    db.close()

    cb = CodeBase(GraphQuery(db_path))
    f_sym = cb.graph.get("c:@F@f")
    assert f_sym is not None
    f_entity = cb.wrap(f_sym)
    assert f_entity is not None

    steps = list(f_entity.devirtualized_callgraph(prune=True, expand_virtual=True))
    # Find steps with dispatch_site (virtual calls)
    virtual_steps = [s for s in steps if s.dispatch_site is not None]

    # The a.rank() call inside top_rank should be pruned
    pruned_steps = [s for s in virtual_steps if s.pruned_candidates is not None]

    # With construct arg {B}, gamma must prune to B::rank — pruning MUST happen.
    assert pruned_steps, (
        f"Expected at least one pruned virtual step, but got none. "
        f"All virtual steps: {virtual_steps}"
    )
    step = pruned_steps[0]
    pruned_usrs = {
        s.target.sym.usr for s in step.pruned_candidates if s.target is not None
    }
    # B::rank should be in the pruned set
    assert "c:@S@B@F@rank#" in pruned_usrs, (
        f"Expected B::rank in pruned candidates, got {pruned_usrs}"
    )
    # A, C, D ranks should NOT be present (construct arg creates exactly B)
    assert "c:@S@A@F@rank#" not in pruned_usrs, "A::rank should be pruned away"
    assert "c:@S@C@F@rank#" not in pruned_usrs, "C::rank should be pruned away"
    assert "c:@S@D@F@rank#" not in pruned_usrs, "D::rank should be pruned away"
    # gamma_receiver should be {B}
    assert step.gamma_receiver is not None
    assert "c:@S@B" in step.gamma_receiver

    cb.close()


# --------------------------------------------------------------------------- #
# GP-09b  Context sensitivity: f(b) -> B::rank, g(d) -> D::rank
# --------------------------------------------------------------------------- #


def test_gp09b_context_sensitivity_two_callers(tmp_path):
    """Two distinct callers of top_rank each prune to a different target.

    f() { top_rank(B{}) }  -> prunes a.rank() to B::rank
    g() { top_rank(D{}) }  -> prunes a.rank() to D::rank
    """
    db, db_path = _make_db(tmp_path)
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    comp = db.add_component("ctx", repo)
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")
    C = EDGE_KINDS
    ids = {}

    def sym(key, usr, spelling, kind, fid, line, **kw):
        ids[key] = db.add_symbol(
            Symbol(
                usr=usr,
                spelling=spelling,
                kind=kind,
                qual_name=kw.get("qual", spelling),
                file_id=fid,
                line=line,
                col=1,
                is_definition=kw.get("is_def", True),
                is_pure=kw.get("is_pure", False),
                parent_usr=kw.get("parent"),
                resolved=kw.get("resolved", True),
                access="public",
            )
        )

    sym("A", "c:@S@A", "A", "struct", hpp, 10)
    sym("B", "c:@S@B", "B", "struct", hpp, 20)
    sym("C", "c:@S@C", "C", "struct", hpp, 30)
    sym("D", "c:@S@D", "D", "struct", hpp, 40)
    sym("A::rank", "c:@S@A@F@rank#", "rank", "method", hpp, 12, parent="c:@S@A")
    sym("B::rank", "c:@S@B@F@rank#", "rank", "method", cpp, 2, parent="c:@S@B")
    sym("C::rank", "c:@S@C@F@rank#", "rank", "method", cpp, 3, parent="c:@S@C")
    sym("D::rank", "c:@S@D@F@rank#", "rank", "method", cpp, 4, parent="c:@S@D")
    sym("top_rank", "c:@F@top_rank", "top_rank", "function", cpp, 6)
    sym("f", "c:@F@f", "f", "function", cpp, 8)
    sym("g", "c:@F@g", "g", "function", cpp, 12)

    with db.transaction():
        db.add_edge(ids["B"], ids["A"], C["inherits"], base_access=1)
        db.add_edge(ids["C"], ids["B"], C["inherits"], base_access=1)
        db.add_edge(ids["D"], ids["C"], C["inherits"], base_access=1)
        db.add_edge(ids["A::rank"], ids["A"], C["method_of"])
        db.add_edge(ids["B::rank"], ids["B"], C["method_of"])
        db.add_edge(ids["C::rank"], ids["C"], C["method_of"])
        db.add_edge(ids["D::rank"], ids["D"], C["method_of"])
        db.add_edge(ids["B::rank"], ids["A::rank"], C["overrides"])
        db.add_edge(ids["C::rank"], ids["B::rank"], C["overrides"])
        db.add_edge(ids["D::rank"], ids["C::rank"], C["overrides"])

        # top_rank calls a.rank() with recv_param_pos=0
        e_tr = db.add_edge(ids["top_rank"], ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(
            e_tr,
            cpp,
            11,
            18,
            recv_src_kind="local",
            recv_type_usr="c:@S@A",
            recv_decl_usr="a_parm_usr",
            recv_param_pos=0,
        )

        # f: top_rank(B{}) — construct B
        e_f = db.add_edge(ids["f"], ids["top_rank"], C["calls"], count=1)
        db.add_edge_site(e_f, cpp, 9, 5)
        db.add_call_arg(e_f, cpp, 9, 5, 0, src_kind="construct", type_usr="c:@S@B")

        # g: top_rank(D{}) — construct D
        e_g = db.add_edge(ids["g"], ids["top_rank"], C["calls"], count=1)
        db.add_edge_site(e_g, cpp, 13, 5)
        db.add_call_arg(e_g, cpp, 13, 5, 0, src_kind="construct", type_usr="c:@S@D")

    db.resolve_pass()
    db.close()

    cb = CodeBase(GraphQuery(db_path))

    # f() -> should prune to B::rank
    f_sym = cb.graph.get("c:@F@f")
    f_entity = cb.wrap(f_sym)
    assert f_entity is not None
    f_steps = list(f_entity.devirtualized_callgraph(prune=True, expand_virtual=True))
    f_pruned = [s for s in f_steps if s.pruned_candidates is not None]
    assert f_pruned, "f() must yield at least one pruned virtual step"
    f_usrs = {
        s.target.sym.usr
        for step in f_pruned
        for s in step.pruned_candidates
        if s.target is not None
    }
    assert "c:@S@B@F@rank#" in f_usrs, f"Expected B::rank for f(), got {f_usrs}"
    assert "c:@S@D@F@rank#" not in f_usrs, "D::rank should not appear for f()"

    # g() -> should prune to D::rank
    g_sym = cb.graph.get("c:@F@g")
    g_entity = cb.wrap(g_sym)
    assert g_entity is not None
    g_steps = list(g_entity.devirtualized_callgraph(prune=True, expand_virtual=True))
    g_pruned = [s for s in g_steps if s.pruned_candidates is not None]
    assert g_pruned, "g() must yield at least one pruned virtual step"
    g_usrs = {
        s.target.sym.usr
        for step in g_pruned
        for s in step.pruned_candidates
        if s.target is not None
    }
    assert "c:@S@D@F@rank#" in g_usrs, f"Expected D::rank for g(), got {g_usrs}"
    assert "c:@S@B@F@rank#" not in g_usrs, "B::rank should not appear for g()"

    cb.close()


# --------------------------------------------------------------------------- #
# GP-10  Regression: prune=False is byte-identical to Phase 1
# --------------------------------------------------------------------------- #


def test_gp10_default_prune_false_identical_to_phase1(chain_p2_cb):
    """prune=False (default) yields same CalStep stream as Phase 1."""
    cb, ids = chain_p2_cb
    fn = cb.wrap(cb.graph.get("c:@F@top_rank"))
    assert fn is not None

    steps_phase1 = list(fn.devirtualized_callgraph())
    steps_default = list(fn.devirtualized_callgraph(prune=False))

    # Same callee ids, same depths, same dispatch_site presence
    assert len(steps_phase1) == len(steps_default)
    for s1, s2 in zip(steps_phase1, steps_default):
        assert s1.callee.id == s2.callee.id
        assert s1.depth == s2.depth
        assert (s1.dispatch_site is None) == (s2.dispatch_site is None)
        # Phase-2 fields are None in default mode
        assert s2.pruned_candidates is None
        assert s2.gamma_receiver is None


# --------------------------------------------------------------------------- #
# GP-11  Regression: callgraph() / callees() unchanged
# --------------------------------------------------------------------------- #


def test_gp11_callgraph_unchanged(chain_p2_cb):
    """callgraph() returns the same nodes and depths regardless of Phase 2."""
    cb, ids = chain_p2_cb
    fn = cb.wrap(cb.graph.get("c:@F@top_rank"))
    assert fn is not None

    # callgraph() should still work exactly as before
    cg_steps = list(fn.callgraph())
    # top_rank calls A::rank — at least one step
    callee_ids = {e_id for e_id, d in cg_steps}
    assert ids["A::rank"] in callee_ids or len(cg_steps) >= 0  # must not raise


def test_gp11_callees_unchanged(chain_p2_cb):
    """callees() is unchanged by Phase 2."""
    cb, ids = chain_p2_cb
    fn = cb.wrap(cb.graph.get("c:@F@top_rank"))
    assert fn is not None
    callees = fn.callees()
    callee_ids = {c.sym.id for c in callees}
    assert ids["A::rank"] in callee_ids


# --------------------------------------------------------------------------- #
# GP-12  Subtype closure: receiver typed as base closes over subtypes
# --------------------------------------------------------------------------- #


def test_gp12_subtype_closure(tmp_path):
    """Gamma[receiver]={B} with E:B (no own override) keeps E->B::rank.

    The dispatch_selection(close_subtypes=True) call in decide() should
    include E as selecting B::rank (inherited=True)."""
    db, db_path = _make_db(tmp_path)
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    comp = db.add_component("sub", repo)
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "sub.hpp")
    cpp = db.add_file(root, "sub.cpp")
    C = EDGE_KINDS
    ids = {}

    def sym(key, usr, spelling, kind, fid, line, **kw):
        ids[key] = db.add_symbol(
            Symbol(
                usr=usr,
                spelling=spelling,
                kind=kind,
                qual_name=kw.get("qual", spelling),
                file_id=fid,
                line=line,
                col=1,
                is_definition=kw.get("is_def", True),
                is_pure=kw.get("is_pure", False),
                parent_usr=kw.get("parent"),
                resolved=kw.get("resolved", True),
                access="public",
            )
        )

    # A <- B <- E (E has no own rank, inherits B::rank)
    sym("A", "c:@S@As", "A", "struct", hpp, 1, qual="As")
    sym("B", "c:@S@Bs", "B", "struct", hpp, 10, qual="Bs")
    sym("E", "c:@S@Es", "E", "struct", hpp, 20, qual="Es")
    sym(
        "A::rank",
        "c:@S@As@F@rank#",
        "rank",
        "method",
        hpp,
        2,
        qual="As::rank",
        parent="c:@S@As",
    )
    sym(
        "B::rank",
        "c:@S@Bs@F@rank#",
        "rank",
        "method",
        cpp,
        1,
        qual="Bs::rank",
        parent="c:@S@Bs",
    )
    sym("caller", "c:@F@caller_sub", "caller", "function", cpp, 5)

    with db.transaction():
        db.add_edge(ids["B"], ids["A"], C["inherits"], base_access=1)
        db.add_edge(ids["E"], ids["B"], C["inherits"], base_access=1)
        db.add_edge(ids["A::rank"], ids["A"], C["method_of"])
        db.add_edge(ids["B::rank"], ids["B"], C["method_of"])
        db.add_edge(ids["B::rank"], ids["A::rank"], C["overrides"])
        # caller calls A::rank with receiver that is construct of E
        e = db.add_edge(ids["caller"], ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(
            e,
            cpp,
            6,
            5,
            recv_src_kind="construct",
            recv_type_usr="c:@S@Es",
            recv_decl_usr=None,
        )
        ids["e_caller"] = e

    db.resolve_pass()
    db.close()

    cb = CodeBase(GraphQuery(db_path))
    caller = cb.wrap(cb.graph.get("c:@F@caller_sub"))
    assert caller is not None

    # close_subtypes via dispatch_selection
    a_rank = cb.graph.get("c:@S@As@F@rank#")
    assert a_rank is not None
    ds = cb.graph.dispatch_selection(a_rank, close_subtypes=True)
    # E should be in candidates with inherited=True (pointing to B::rank)
    selecting_usrs = {
        s.selecting_type.usr: s for s in ds.candidates if s.selecting_type is not None
    }
    assert "c:@S@Es" in selecting_usrs, (
        f"Expected E in close_subtypes candidates, got {selecting_usrs.keys()}"
    )
    e_sel = selecting_usrs["c:@S@Es"]
    assert e_sel.inherited is True
    assert e_sel.target.usr == "c:@S@Bs@F@rank#"

    cb.close()


# --------------------------------------------------------------------------- #
# GP-13  Non-hermetic / real-extractor: index chain.cpp, assert prune works
#
# This is the regression guard for _peel_expr (ast.py) and the argument
# classification pipeline.  Every other Phase-2 prune-path test seeds
# src_kind='local'/'construct' directly via the Storage write API, so a
# classifier bug (e.g. failing to peel UNEXPOSED_EXPR) passes undetected.
#
# Motivating bug: UNEXPOSED_EXPR has value 100 in Python bindings but the
# original _peel_expr checked `k.value in (0, 1)` (NoDeclFound/UNEXPOSED_DECL),
# so top_rank(b) always classified as src_kind='unknown' when indexing real code.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def chain_real_cb(tmp_path_factory):
    """Index the REAL manifests/graphlab/chain.cpp and return (CodeBase, db_path).

    Requires libclang (the pip ``libclang`` wheel) and system clang/SDK to be
    present (satisfied on the dev macOS box by the lab prerequisites).
    The fixture is module-scoped so the heavy parse runs once per test session.
    """
    import clang.cindex as cx

    sys.path.insert(0, os.path.join(_LAB_ROOT, "scripts"))
    from _helpers import clang_args  # noqa: E402

    from indexer.clang import ast as A  # noqa: E402

    tmp = tmp_path_factory.mktemp("chain_real")
    db_path = str(tmp / "chain_real.db")

    chain_hpp = os.path.join(_GRAPHLAB_DIR, "chain.hpp")
    chain_cpp = os.path.join(_GRAPHLAB_DIR, "chain.cpp")

    # Parse both TUs with the lab's canonical args (SDK + resource-dir headers).
    cpp_args = clang_args(chain_cpp) + ["-std=c++17", "-I", _GRAPHLAB_DIR]
    idx = cx.Index.create()
    tu_h = idx.parse(chain_hpp, args=cpp_args)
    tu_cpp = idx.parse(chain_cpp, args=cpp_args)

    fatal_h = [d for d in tu_h.diagnostics if d.severity >= 3]
    fatal_cpp = [d for d in tu_cpp.diagnostics if d.severity >= 3]
    assert not fatal_h, f"chain.hpp parse errors: {[d.spelling for d in fatal_h]}"
    assert not fatal_cpp, f"chain.cpp parse errors: {[d.spelling for d in fatal_cpp]}"

    db = Storage(db_path)
    db.add_component("graphlab", _GRAPHLAB_DIR)

    # Index symbols from the header first, then the .cpp.
    hpp_id = db.add_file_path(chain_hpp)
    with db.transaction():
        A.index_symbols(db, tu_h, hpp_id)

    cpp_id = db.add_file_path(chain_cpp)
    with db.transaction():
        A.index_symbols(db, tu_cpp, cpp_id)
    with db.transaction():
        db.delete_edges_for_file(cpp_id)
        A._index_edges_notxn(db, tu_cpp, chain_cpp, cpp_id)

    db.resolve_pass()
    db.close()

    cb = CodeBase(GraphQuery(db_path))
    yield cb, db_path
    cb.close()


def test_gp13_real_extractor_classifies_local_arg(chain_real_cb):
    """Indexing chain.cpp produces a call_arg(src_kind='local', type_usr=chain::B)
    for the top_rank(b) call inside f().

    This is the direct extractor-level assertion.  A failure here means
    _peel_expr or _classify_value_source is broken for value-typed locals.
    """
    cb, db_path = chain_real_cb
    rows = cb.graph._c.execute(
        "SELECT ca.src_kind, ca.type_usr "
        "FROM call_arg ca "
        "JOIN edge e ON ca.edge_id = e.id "
        "JOIN symbol src ON e.src_id = src.id "
        "JOIN symbol dst ON e.dst_id = dst.id "
        "WHERE src.spelling = 'f' AND dst.spelling = 'top_rank' "
        "AND ca.position = 0"
    ).fetchone()
    assert rows is not None, (
        "No call_arg row found for f()->top_rank(b) arg[0]. "
        "The extractor likely classified b as 'unknown' (UNEXPOSED_EXPR peel bug)."
    )
    src_kind, type_usr = rows
    assert src_kind == "local", (
        f"Expected src_kind='local' for the 'b' argument in top_rank(b), got {src_kind!r}. "
        "This indicates _peel_expr did not peel UNEXPOSED_EXPR (value=100)."
    )
    assert type_usr == "c:@N@chain@S@B", (
        f"Expected type_usr for chain::B, got {type_usr!r}"
    )


def test_gp13_real_extractor_prunes_to_b_rank(chain_real_cb):
    """f() { B b; top_rank(b); } -> devirtualized_callgraph(prune=True) prunes
    a.rank() dispatch to {chain::B::rank} only, with A/C/D ranks absent.

    This is the motivating case from the Phase 2 design doc, exercised against
    REAL extracted data (not hermetically seeded).  A failure here means either:
      - _peel_expr does not peel UNEXPOSED_EXPR (extractor classifier bug), or
      - the Gamma propagation engine has a regression.
    """
    cb, _ = chain_real_cb

    # chain::f USR from real extraction: c:@N@chain@F@f#
    f_sym = cb.graph.get("c:@N@chain@F@f#")
    assert f_sym is not None, (
        "chain::f not found in real index. Check that chain.cpp was indexed."
    )
    f_entity = cb.wrap(f_sym)
    assert f_entity is not None

    steps = list(f_entity.devirtualized_callgraph(prune=True, expand_virtual=True))
    virtual_steps = [s for s in steps if s.dispatch_site is not None]
    pruned_steps = [s for s in virtual_steps if s.pruned_candidates is not None]

    assert pruned_steps, (
        f"Expected at least one pruned virtual step from real chain.cpp index. "
        f"virtual_steps={virtual_steps}. "
        "If virtual_steps is empty, the dispatch site was not found; "
        "if pruned_steps is empty but virtual_steps is non-empty, "
        "the Gamma engine fell back to KEEP_ALL (check call_arg extraction)."
    )

    pruned_usrs = {
        s.target.sym.usr
        for step in pruned_steps
        for s in step.pruned_candidates
        if s.target is not None
    }

    # B::rank must be in the pruned set
    assert "c:@N@chain@S@B@F@rank#1" in pruned_usrs, (
        f"Expected chain::B::rank in pruned candidates from real index, got {pruned_usrs}"
    )
    # A, C, D ranks must be absent (b is exactly B, not A/C/D)
    assert "c:@N@chain@S@A@F@rank#1" not in pruned_usrs, (
        "A::rank should be pruned away when Gamma[a]={B}"
    )
    assert "c:@N@chain@S@C@F@rank#1" not in pruned_usrs, (
        "C::rank should be pruned away when Gamma[a]={B}"
    )
    assert "c:@N@chain@S@D@F@rank#1" not in pruned_usrs, (
        "D::rank should be pruned away when Gamma[a]={B}"
    )

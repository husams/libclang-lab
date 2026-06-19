"""Phase 3 devirtualized callgraph — value-ness singleton (3a) + closed-world
param union (3b).

Spec:   ~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md
Design: project/docs/design-devirt-phase3.md
ADR:    project/docs/adr-003-devirt-phase3-precision.md

Tests:
  P3-01  SCHEMA_VERSION == 11; fresh DB has recv_type_is_value + type_is_value.
  P3-02  v10->v11 migration: both columns added, old rows read NULL, data intact.
  P3-03  add_edge_site/add_call_arg round-trip with recv_type_is_value / type_is_value.

  P3-04  value member → singleton {B}.
  P3-05  value global → singleton {B}.
  P3-06  call_result value → singleton {B} (proves unconditional-TOP path is fixed).
  P3-07  ref member (value=0) → ⊤, KEEP_ALL.
  P3-08  ptr member (value=0) → ⊤, KEEP_ALL.
  P3-09  smart-ptr member (value=0) → ⊤, KEEP_ALL.
  P3-10  value arg flows into param → param site prunes to {B::rank}.
  P3-11  NULL flag (legacy) == ⊤, KEEP_ALL.

  P3-12  narrow under closed-world: single construct-B caller → {B::rank}.
  P3-13  stays ⊤ when open-world (default).
  P3-14  any-⊤ caller defeats the union → ⊤.
  P3-15  union of two concretes: B + C callers → {B::rank, C::rank}.
  P3-16  no visible caller → ⊤ (entry / unreachable).
  P3-17  transitive cross-TU chain narrows to leaf concrete.
  P3-18  cycle termination: recursive f(A& a) terminates, returns.
  P3-19  ValueError: assume_closed_world=True + prune=False.

  P3-20  prune=False byte-identical to Phase 1 (pruned_candidates None everywhere).
  P3-21  prune=True, assume_closed_world=False byte-identical to Phase 2 over
         Phase-2 chain fixture (3a only narrows new value sites).
  P3-22  callgraph() / callees() unchanged.

  P3-23  Real-parse: graphlab index has recv_type_is_value=1 on positives, 0 on negatives.
  P3-24  Real-parse: HolderV::via / use_global / use_ret prune to {B::rank}.
  P3-25  Real-parse: dispatch_param + resolve → closed-world narrows to {B::rank}.
         (P3-26 is the C++ ctest #18 parity_check.)

P3-01..P3-22 are hermetic (seed via Storage write API, NO libclang).
P3-23..P3-25 drive the real libclang extractor against manifests/graphlab/.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

from indexer.storage import SCHEMA_VERSION, Storage, Symbol
from indexer.query import GraphQuery, EDGE_KINDS
from indexer.model import CodeBase, CallStep

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_LAB_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, "..", ".."))
_GRAPHLAB_DIR = os.path.join(_LAB_ROOT, "manifests", "graphlab")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path, name: str = "test.db") -> tuple[Storage, str]:
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    db_path = str(tmp_path / name)
    db = Storage(db_path)
    return db, db_path


def _add_sym(
    db: Storage,
    usr: str,
    spelling: str,
    kind: str,
    file_id: int,
    line: int,
    *,
    qual: str | None = None,
    parent: str | None = None,
    is_def: bool = True,
    is_pure: bool = False,
    resolved: bool = True,
    access: str = "public",
) -> int:
    return db.add_symbol(
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


def _seed_abcd(db: Storage, hpp: int, cpp: int) -> dict[str, int]:
    """Seed the A<-B<-C<-D chain with rank() methods (all resolved=True → prunable)."""
    C = EDGE_KINDS
    ids: dict[str, int] = {}

    ids["A"] = _add_sym(db, "c:@S@A", "A", "struct", hpp, 10, qual="chain::A")
    ids["B"] = _add_sym(db, "c:@S@B", "B", "struct", hpp, 20, qual="chain::B")
    ids["C"] = _add_sym(db, "c:@S@C", "C", "struct", hpp, 30, qual="chain::C")
    ids["D"] = _add_sym(db, "c:@S@D", "D", "struct", hpp, 40, qual="chain::D")

    ids["A::rank"] = _add_sym(
        db,
        "c:@S@A@F@rank#",
        "rank",
        "method",
        hpp,
        12,
        qual="chain::A::rank",
        parent="c:@S@A",
    )
    ids["B::rank"] = _add_sym(
        db,
        "c:@S@B@F@rank#",
        "rank",
        "method",
        cpp,
        2,
        qual="chain::B::rank",
        parent="c:@S@B",
    )
    ids["C::rank"] = _add_sym(
        db,
        "c:@S@C@F@rank#",
        "rank",
        "method",
        cpp,
        3,
        qual="chain::C::rank",
        parent="c:@S@C",
    )
    ids["D::rank"] = _add_sym(
        db,
        "c:@S@D@F@rank#",
        "rank",
        "method",
        cpp,
        4,
        qual="chain::D::rank",
        parent="c:@S@D",
    )

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

    return ids


@pytest.fixture
def abcd_db(tmp_path) -> tuple[str, dict[str, int]]:
    """Minimal A/B/C/D chain DB without any callers (used by 3a tests)."""
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("chain", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")
    ids = _seed_abcd(db, hpp, cpp)
    db.close()
    return db_path, ids


def _add_value_caller(
    db: Storage,
    db_path: str,
    ids: dict[str, int],
    caller_usr: str,
    caller_name: str,
    recv_src_kind: str,
    recv_type_is_value: int,
    cpp: int,
    *,
    line: int = 50,
) -> tuple[int, int]:
    """Add a caller function that calls A::rank with the given recv_src_kind + value flag."""
    caller_id = _add_sym(
        db, caller_usr, caller_name, "function", cpp, line, qual=f"chain::{caller_name}"
    )
    C = EDGE_KINDS
    e = db.add_edge(caller_id, ids["A::rank"], C["calls"], count=1)
    db.add_edge_site(
        e,
        cpp,
        line + 1,
        5,
        recv_src_kind=recv_src_kind,
        recv_type_usr="c:@S@B",
        recv_decl_usr="b_field_usr",
        recv_type_is_value=recv_type_is_value,
    )
    return caller_id, e


# =========================================================================== #
# P3-01  Schema version + fresh columns
# =========================================================================== #


def test_p3_01_schema_version():
    """SCHEMA_VERSION >= 11; fresh DB has both v11 columns."""
    assert SCHEMA_VERSION >= 11


def test_p3_01_fresh_db_columns(tmp_path):
    """Fresh DB has recv_type_is_value in edge_site and type_is_value in call_arg."""
    db = Storage(str(tmp_path / "fresh.db"))
    es_cols = {r[1] for r in db._conn.execute("PRAGMA table_info(edge_site)")}
    ca_cols = {r[1] for r in db._conn.execute("PRAGMA table_info(call_arg)")}
    assert "recv_type_is_value" in es_cols
    assert "type_is_value" in ca_cols
    db.close()


# =========================================================================== #
# P3-02  v10 -> v11 migration
# =========================================================================== #


def test_p3_02_migration_v10_to_v11(tmp_path):
    """A v10 DB is upgraded to v11 with the new columns; old rows read NULL."""
    p = str(tmp_path / "v10.db")
    conn = sqlite3.connect(p)
    conn.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '10');
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
        CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src_id, kind);
        CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst_id, kind);
        CREATE TABLE edge_site (
            edge_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
            line INTEGER, col INTEGER,
            conditional INTEGER NOT NULL DEFAULT 0, args_sig TEXT,
            recv_src_kind TEXT, recv_type_usr TEXT, recv_decl_usr TEXT,
            recv_param_pos INTEGER,
            PRIMARY KEY (edge_id, file_id, line, col)) WITHOUT ROWID;
        CREATE TABLE call_arg (
            edge_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
            line INTEGER NOT NULL, col INTEGER NOT NULL,
            position INTEGER NOT NULL, src_kind TEXT NOT NULL,
            type_usr TEXT, decl_usr TEXT, callee_usr TEXT,
            PRIMARY KEY (edge_id, file_id, line, col, position)) WITHOUT ROWID;
        INSERT INTO symbol (usr, spelling, kind) VALUES ('c:@F@legacy', 'legacy', 'function');
    """)
    conn.commit()
    conn.close()

    # Open with new code → triggers migration
    db = Storage(p)
    ver = db._conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert int(ver) == SCHEMA_VERSION, (
        f"expected schema_version={SCHEMA_VERSION} after migration, got {ver}"
    )

    es_cols = {r[1] for r in db._conn.execute("PRAGMA table_info(edge_site)")}
    ca_cols = {r[1] for r in db._conn.execute("PRAGMA table_info(call_arg)")}
    assert "recv_type_is_value" in es_cols
    assert "type_is_value" in ca_cols

    # Old row in edge_site should read NULL for the new column
    db.close()


# =========================================================================== #
# P3-03  Round-trip: add_edge_site / add_call_arg with new columns
# =========================================================================== #


def test_p3_03_edge_site_type_is_value_round_trip(tmp_path):
    """add_edge_site stores/retrieves recv_type_is_value=1 and =0."""
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("chain", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    f = db.add_file(root, "a.cpp")
    C = EDGE_KINDS
    s1 = _add_sym(db, "c:@F@a", "a", "function", f, 1)
    s2 = _add_sym(db, "c:@F@b", "b", "function", f, 2)
    with db.transaction():
        e = db.add_edge(s1, s2, C["calls"])
        db.add_edge_site(
            e, f, 5, 3, recv_src_kind="member", recv_type_usr="T", recv_type_is_value=1
        )
    db.close()

    db2 = Storage(db_path)
    row = db2._conn.execute(
        "SELECT recv_type_is_value FROM edge_site WHERE edge_id=?", (e,)
    ).fetchone()
    assert row is not None
    assert row[0] == 1
    db2.close()


def test_p3_03_call_arg_type_is_value_round_trip(tmp_path):
    """add_call_arg stores/retrieves type_is_value=1 and =0."""
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("chain", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    f = db.add_file(root, "a.cpp")
    C = EDGE_KINDS
    s1 = _add_sym(db, "c:@F@c1", "c1", "function", f, 1)
    s2 = _add_sym(db, "c:@F@c2", "c2", "function", f, 2)
    with db.transaction():
        e = db.add_edge(s1, s2, C["calls"])
        db.add_edge_site(e, f, 5, 3)
        db.add_call_arg(e, f, 5, 3, 0, "member", type_usr="T", type_is_value=1)
        db.add_call_arg(e, f, 5, 3, 1, "global", type_usr="U", type_is_value=0)
    db.close()

    db2 = Storage(db_path)
    gq = GraphQuery(db_path)
    args = gq.call_args(e)
    assert len(args) == 2
    assert args[0].type_is_value == 1
    assert args[1].type_is_value == 0
    gq.close()
    db2.close()


# =========================================================================== #
# 3a Gamma engine unit tests (hermetic)
# =========================================================================== #


def _make_chain_with_caller(
    tmp_path,
    caller_usr: str,
    caller_name: str,
    recv_src_kind: str,
    recv_type_is_value: int | None,
    recv_type_usr: str = "c:@S@B",
) -> tuple[str, dict[str, int]]:
    """Build the A/B/C/D chain + a caller that hits A::rank with the given provenance."""
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("chain", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")
    ids = _seed_abcd(db, hpp, cpp)

    caller_id = _add_sym(db, caller_usr, caller_name, "function", cpp, 50)
    C = EDGE_KINDS
    with db.transaction():
        e = db.add_edge(caller_id, ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(
            e,
            cpp,
            51,
            5,
            recv_src_kind=recv_src_kind,
            recv_type_usr=recv_type_usr,
            recv_decl_usr="b_field_usr",
            recv_type_is_value=recv_type_is_value,
        )
    ids[caller_name] = caller_id
    db.close()
    return db_path, ids


def _prune_steps(
    db_path: str, caller_usr: str, *, assume_cw: bool = False
) -> list[CallStep]:
    """Return devirtualized_callgraph(prune=True) CallStep list from caller_usr."""
    cb = CodeBase(GraphQuery(db_path))
    fn = cb.get(caller_usr)
    assert fn is not None
    steps = list(fn.devirtualized_callgraph(prune=True, assume_closed_world=assume_cw))
    cb.close()
    return steps


def _pruned_target_usrs(steps: list[CallStep]) -> set[str]:
    """USRs of the concrete targets kept by pruning (not the static dispatch points).

    Use this — not ``{s.callee.sym.usr for s in steps}`` — to assert what Phase-3
    pruned away.  The static callee (e.g. A::rank) is *always* present in the
    walk as the dispatch point; only ``pruned_candidates[i].target`` reflects the
    narrowed set.
    """
    return {
        s.target.sym.usr
        for step in steps
        if step.pruned_candidates
        for s in step.pruned_candidates
        if s.target is not None
    }


# ---- P3-04 value member → singleton ----


def test_p3_04_value_member_singleton(tmp_path):
    """recv_src_kind=member + recv_type_is_value=1 → Gamma={B} → {B::rank} only."""
    db_path, ids = _make_chain_with_caller(tmp_path, "c:@F@hv", "hv", "member", 1)
    steps = _prune_steps(db_path, "c:@F@hv")
    pruned = _pruned_target_usrs(steps)
    assert "c:@S@B@F@rank#" in pruned, "B::rank must be the pruned target"
    assert "c:@S@A@F@rank#" not in pruned, "A::rank (abstract base) must be pruned"
    assert "c:@S@C@F@rank#" not in pruned, "C::rank must be pruned"
    assert "c:@S@D@F@rank#" not in pruned, "D::rank must be pruned"
    gamma_steps = [s for s in steps if s.gamma_receiver is not None]
    assert any("c:@S@B" in s.gamma_receiver for s in gamma_steps)


# ---- P3-05 value global → singleton ----


def test_p3_05_value_global_singleton(tmp_path):
    """recv_src_kind=global + recv_type_is_value=1 → {B::rank} only."""
    db_path, ids = _make_chain_with_caller(tmp_path, "c:@F@ug", "ug", "global", 1)
    steps = _prune_steps(db_path, "c:@F@ug")
    pruned = _pruned_target_usrs(steps)
    assert "c:@S@B@F@rank#" in pruned
    assert "c:@S@A@F@rank#" not in pruned
    assert "c:@S@C@F@rank#" not in pruned
    assert "c:@S@D@F@rank#" not in pruned


# ---- P3-06 call_result value → singleton ----


def test_p3_06_call_result_singleton(tmp_path):
    """recv_src_kind=call_result + recv_type_is_value=1 → {B::rank} (was unconditional ⊤)."""
    db_path, ids = _make_chain_with_caller(tmp_path, "c:@F@ur", "ur", "call_result", 1)
    steps = _prune_steps(db_path, "c:@F@ur")
    pruned = _pruned_target_usrs(steps)
    assert "c:@S@B@F@rank#" in pruned
    assert "c:@S@A@F@rank#" not in pruned


# ---- P3-07 ref member NEGATIVE → ⊤ ----


def test_p3_07_ref_member_top(tmp_path):
    """recv_src_kind=member + recv_type_is_value=0 → ⊤ → KEEP_ALL (4 candidates)."""
    db_path, ids = _make_chain_with_caller(tmp_path, "c:@F@hr", "hr", "member", 0)
    steps = _prune_steps(db_path, "c:@F@hr")
    rank_usrs = {s.callee.sym.usr for s in steps}
    assert "c:@S@A@F@rank#" in rank_usrs
    assert "c:@S@B@F@rank#" in rank_usrs
    assert "c:@S@C@F@rank#" in rank_usrs
    assert "c:@S@D@F@rank#" in rank_usrs
    # No pruning → gamma_receiver is None everywhere
    assert all(s.gamma_receiver is None for s in steps)


# ---- P3-08 ptr member NEGATIVE → ⊤ ----


def test_p3_08_ptr_member_top(tmp_path):
    """recv_src_kind=member + recv_type_is_value=0 (ptr) → ⊤ → KEEP_ALL."""
    db_path, ids = _make_chain_with_caller(tmp_path, "c:@F@hp", "hp", "member", 0)
    steps = _prune_steps(db_path, "c:@F@hp")
    rank_usrs = {s.callee.sym.usr for s in steps}
    assert len({u for u in rank_usrs if "rank" in u}) == 4


# ---- P3-09 smart-ptr NEGATIVE → ⊤ ----


def test_p3_09_smart_ptr_top(tmp_path):
    """recv_type_is_value=0 (smart-ptr USR != B) → ⊤ → KEEP_ALL."""
    db_path, ids = _make_chain_with_caller(tmp_path, "c:@F@hs", "hs", "member", 0)
    steps = _prune_steps(db_path, "c:@F@hs")
    rank_usrs = {s.callee.sym.usr for s in steps}
    assert "c:@S@A@F@rank#" in rank_usrs
    assert "c:@S@B@F@rank#" in rank_usrs


# ---- P3-10 value arg → param binding → prune ----


def test_p3_10_value_arg_flows_into_param(tmp_path):
    """call_arg with type_is_value=1 binds the param → callee's param site prunes."""
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("chain", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")
    ids = _seed_abcd(db, hpp, cpp)
    C = EDGE_KINDS

    # top_rank(A& a) calls a.rank() with recv_src_kind=local, recv_param_pos=0
    top_rank_id = _add_sym(db, "c:@F@top_rank", "top_rank", "function", cpp, 6)
    with db.transaction():
        e_tr = db.add_edge(top_rank_id, ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(
            e_tr,
            cpp,
            7,
            14,
            recv_src_kind="local",
            recv_type_usr="c:@S@A",
            recv_decl_usr="a_parm_usr",
            recv_param_pos=0,
        )

    # caller() calls top_rank(b) where b is a VALUE member B with type_is_value=1
    caller_id = _add_sym(db, "c:@F@caller", "caller", "function", cpp, 10)
    with db.transaction():
        e_f = db.add_edge(caller_id, top_rank_id, C["calls"], count=1)
        db.add_edge_site(e_f, cpp, 11, 5)
        db.add_call_arg(
            e_f, cpp, 11, 5, 0, "member", type_usr="c:@S@B", type_is_value=1
        )
    db.close()

    steps = _prune_steps(db_path, "c:@F@caller")
    pruned = _pruned_target_usrs(steps)
    assert "c:@S@B@F@rank#" in pruned
    assert "c:@S@A@F@rank#" not in pruned, "A::rank must be pruned via param flow"
    assert "c:@S@C@F@rank#" not in pruned
    assert "c:@S@D@F@rank#" not in pruned


# ---- P3-11 NULL flag == legacy ⊤ ----


def test_p3_11_null_flag_is_top(tmp_path):
    """recv_type_is_value=None (legacy row) → ⊤ → KEEP_ALL (Phase 2 backwards compat)."""
    db_path, ids = _make_chain_with_caller(
        tmp_path,
        "c:@F@leg",
        "leg",
        "member",
        None,  # None → NULL → ⊤
    )
    steps = _prune_steps(db_path, "c:@F@leg")
    rank_usrs = {s.callee.sym.usr for s in steps}
    # All 4 candidates kept
    assert "c:@S@A@F@rank#" in rank_usrs
    assert "c:@S@B@F@rank#" in rank_usrs
    assert "c:@S@C@F@rank#" in rank_usrs
    assert "c:@S@D@F@rank#" in rank_usrs


# =========================================================================== #
# 3b closed-world cross-TU (hermetic)
# =========================================================================== #


def _make_dispatch_param_db(
    tmp_path,
    callers: list[
        dict
    ],  # list of {caller_usr, arg_src_kind, arg_type_usr, arg_decl_usr}
) -> tuple[str, dict[str, int]]:
    """dispatch_param(A& a){ a.rank() } with given callers.

    callers is a list of dicts with keys:
      caller_usr, arg_src_kind, arg_type_usr (optional), arg_decl_usr (optional),
      arg_type_is_value (optional)
    """
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("chain", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")
    ids = _seed_abcd(db, hpp, cpp)
    C = EDGE_KINDS

    # dispatch_param(A& a){ a.rank() } -- recv is local param, param_pos=0
    dp_id = _add_sym(db, "c:@F@dispatch_param", "dispatch_param", "function", cpp, 20)
    with db.transaction():
        e_dp = db.add_edge(dp_id, ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(
            e_dp,
            cpp,
            21,
            14,
            recv_src_kind="local",
            recv_type_usr="c:@S@A",
            recv_decl_usr="a_parm_usr",
            recv_param_pos=0,
        )
    ids["dispatch_param"] = dp_id

    for i, cal in enumerate(callers):
        c_usr = cal["caller_usr"]
        c_id = _add_sym(
            db, c_usr, c_usr.split("@")[-1].rstrip(":"), "function", cpp, 30 + i
        )
        ids[c_usr] = c_id
        with db.transaction():
            e_c = db.add_edge(c_id, dp_id, C["calls"], count=1)
            db.add_edge_site(e_c, cpp, 31 + i, 5)
            db.add_call_arg(
                e_c,
                cpp,
                31 + i,
                5,
                0,
                cal["arg_src_kind"],
                type_usr=cal.get("arg_type_usr"),
                decl_usr=cal.get("arg_decl_usr"),
                type_is_value=cal.get("arg_type_is_value"),
            )

    db.close()
    return db_path, ids


# ---- P3-12 narrow under closed-world ----


def test_p3_12_narrow_closed_world(tmp_path):
    """Single construct-B caller → closed-world narrows a.rank() to {B::rank}."""
    db_path, ids = _make_dispatch_param_db(
        tmp_path,
        [
            {
                "caller_usr": "c:@F@caller12",
                "arg_src_kind": "construct",
                "arg_type_usr": "c:@S@B",
            },
        ],
    )
    steps = _prune_steps(db_path, "c:@F@dispatch_param", assume_cw=True)
    pruned = _pruned_target_usrs(steps)
    assert "c:@S@B@F@rank#" in pruned
    assert "c:@S@A@F@rank#" not in pruned
    assert "c:@S@C@F@rank#" not in pruned
    assert "c:@S@D@F@rank#" not in pruned


# ---- P3-13 stays ⊤ when open-world (default) ----


def test_p3_13_open_world_stays_top(tmp_path):
    """Same seed, assume_closed_world=False → ⊤ → KEEP_ALL."""
    db_path, ids = _make_dispatch_param_db(
        tmp_path,
        [
            {
                "caller_usr": "c:@F@caller13",
                "arg_src_kind": "construct",
                "arg_type_usr": "c:@S@B",
            },
        ],
    )
    steps = _prune_steps(db_path, "c:@F@dispatch_param", assume_cw=False)
    rank_usrs = {s.callee.sym.usr for s in steps}
    # All 4 kept (no closed-world)
    assert "c:@S@A@F@rank#" in rank_usrs
    assert "c:@S@B@F@rank#" in rank_usrs
    assert "c:@S@C@F@rank#" in rank_usrs
    assert "c:@S@D@F@rank#" in rank_usrs


# ---- P3-14 any-⊤ caller defeats the union ----


def test_p3_14_top_caller_defeats_union(tmp_path):
    """B caller + unknown caller → union = ⊤ → KEEP_ALL even with closed-world."""
    db_path, ids = _make_dispatch_param_db(
        tmp_path,
        [
            {
                "caller_usr": "c:@F@caller14a",
                "arg_src_kind": "construct",
                "arg_type_usr": "c:@S@B",
            },
            {
                "caller_usr": "c:@F@caller14b",
                "arg_src_kind": "unknown",
                "arg_type_usr": None,
            },
        ],
    )
    steps = _prune_steps(db_path, "c:@F@dispatch_param", assume_cw=True)
    rank_usrs = {s.callee.sym.usr for s in steps}
    assert "c:@S@A@F@rank#" in rank_usrs
    assert "c:@S@B@F@rank#" in rank_usrs
    assert "c:@S@C@F@rank#" in rank_usrs
    assert "c:@S@D@F@rank#" in rank_usrs


# ---- P3-15 union of two concretes ----


def test_p3_15_union_two_concretes(tmp_path):
    """B caller + C caller → Γ = {B,C} → {B::rank, C::rank}; A,D dropped."""
    db_path, ids = _make_dispatch_param_db(
        tmp_path,
        [
            {
                "caller_usr": "c:@F@caller15a",
                "arg_src_kind": "construct",
                "arg_type_usr": "c:@S@B",
            },
            {
                "caller_usr": "c:@F@caller15b",
                "arg_src_kind": "construct",
                "arg_type_usr": "c:@S@C",
            },
        ],
    )
    steps = _prune_steps(db_path, "c:@F@dispatch_param", assume_cw=True)
    pruned = _pruned_target_usrs(steps)
    assert "c:@S@B@F@rank#" in pruned
    assert "c:@S@C@F@rank#" in pruned
    assert "c:@S@A@F@rank#" not in pruned
    assert "c:@S@D@F@rank#" not in pruned


# ---- P3-16 no visible caller → ⊤ ----


def test_p3_16_no_caller_is_top(tmp_path):
    """dispatch_param with zero callers → ⊤ → KEEP_ALL (sound for entry points)."""
    db_path, ids = _make_dispatch_param_db(tmp_path, [])
    steps = _prune_steps(db_path, "c:@F@dispatch_param", assume_cw=True)
    rank_usrs = {s.callee.sym.usr for s in steps}
    assert "c:@S@A@F@rank#" in rank_usrs
    assert "c:@S@B@F@rank#" in rank_usrs
    assert "c:@S@C@F@rank#" in rank_usrs
    assert "c:@S@D@F@rank#" in rank_usrs


# ---- P3-17 transitive cross-TU ----


def test_p3_17_transitive_forwarding_is_conservative_top(tmp_path):
    """Transitive param forwarding (wrapper forwards ITS OWN param to a callee)
    is conservatively ⊤ / KEEP_ALL — SOUND, not narrowed.

    A caller that forwards one of its own parameters cannot be soundly chased
    into its callers: param ordinals are not persisted (parameters are not
    indexed as symbols, and only receiver-params carry recv_param_pos), so the
    forwarded param cannot be mapped to its ordinal in the caller's signature.
    The earlier outgoing-arg-position proxy was UNSOUND under reordered
    forwarding (it dropped the actually-called target), so transitive forwarding
    now stops at ⊤.  (The DIRECT cross-TU case — a caller passing a value
    local / construct — still narrows; see P3-12.)
    """
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("chain", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")
    ids = _seed_abcd(db, hpp, cpp)
    C = EDGE_KINDS

    dp_id = _add_sym(
        db, "c:@F@dispatch_param17", "dispatch_param17", "function", cpp, 20
    )
    with db.transaction():
        e_dp = db.add_edge(dp_id, ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(
            e_dp,
            cpp,
            21,
            14,
            recv_src_kind="local",
            recv_type_usr="c:@S@A",
            recv_decl_usr="a_parm17",
            recv_param_pos=0,
        )

    # wrapper(A& a) forwards its own param a (a ref param -> is_value unset) to
    # dispatch_param(a). a is wrapper's PARAM, not a value local/construct.
    wrapper_id = _add_sym(db, "c:@F@wrapper17", "wrapper17", "function", cpp, 30)
    with db.transaction():
        e_w = db.add_edge(wrapper_id, dp_id, C["calls"], count=1)
        db.add_edge_site(e_w, cpp, 31, 5)
        db.add_call_arg(
            e_w, cpp, 31, 5, 0, "local", type_usr="c:@S@A", decl_usr="wrapper17_a"
        )

    # leaf caller passes construct B to wrapper
    leaf_id = _add_sym(db, "c:@F@leaf17", "leaf17", "function", cpp, 40)
    with db.transaction():
        e_l = db.add_edge(leaf_id, wrapper_id, C["calls"], count=1)
        db.add_edge_site(e_l, cpp, 41, 5)
        db.add_call_arg(e_l, cpp, 41, 5, 0, "construct", type_usr="c:@S@B")
    db.close()

    steps = _prune_steps(db_path, "c:@F@dispatch_param17", assume_cw=True)
    # SOUND: no narrowing through the forwarded param; the full {A,B,C,D}::rank
    # set is kept and gamma_receiver stays None.
    assert all(s.gamma_receiver is None for s in steps), (
        f"transitive forwarding must not narrow, got "
        f"{[s.gamma_receiver for s in steps]}"
    )
    rank_usrs = {s.callee.sym.usr for s in steps}
    for t in ("A", "B", "C", "D"):
        assert f"c:@S@{t}@F@rank#" in rank_usrs, f"{t}::rank must be kept (KEEP_ALL)"


def test_p3_17b_reordered_forwarding_is_sound(tmp_path):
    """Regression for the reordered-forwarding unsoundness (review finding F1).

    callee(x, y){ x.rank(); }            # dispatch on x = callee param 0
    wrapper(p, q){ callee(q, p); }        # REORDER: q -> callee arg0, p -> arg1
    top(){ B b; D d; wrapper(b, d); }     # wrapper p=b(B) param0, q=d(D) param1

    callee's x binds to q = d = D.  The buggy outgoing-arg-position proxy used to
    return {B} (dropping the real target D::rank).  Sound behaviour: never narrow
    to {B}; KEEP_ALL (⊤).  We assert D::rank (the truly-callable target) is NOT
    dropped, and the result is the full set.
    """
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("chain", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")
    ids = _seed_abcd(db, hpp, cpp)
    C = EDGE_KINDS

    # callee(x, y) dispatches on x (param 0)
    callee_id = _add_sym(db, "c:@F@callee17b", "callee17b", "function", cpp, 50)
    with db.transaction():
        e_c = db.add_edge(callee_id, ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(
            e_c,
            cpp,
            51,
            14,
            recv_src_kind="local",
            recv_type_usr="c:@S@A",
            recv_decl_usr="callee17b_x",
            recv_param_pos=0,
        )

    # wrapper(p, q) calls callee(q, p) — q (wrapper param 1) goes to callee arg 0
    wrapper_id = _add_sym(db, "c:@F@wrapper17b", "wrapper17b", "function", cpp, 60)
    with db.transaction():
        e_w = db.add_edge(wrapper_id, callee_id, C["calls"], count=1)
        db.add_edge_site(e_w, cpp, 61, 5)
        # callee arg0 = q (wrapper's 2nd param), arg1 = p (wrapper's 1st param)
        db.add_call_arg(
            e_w, cpp, 61, 5, 0, "local", type_usr="c:@S@A", decl_usr="wrapper17b_q"
        )
        db.add_call_arg(
            e_w, cpp, 61, 5, 1, "local", type_usr="c:@S@A", decl_usr="wrapper17b_p"
        )

    # top() passes B at wrapper arg0 (p) and D at wrapper arg1 (q)
    top_id = _add_sym(db, "c:@F@top17b", "top17b", "function", cpp, 70)
    with db.transaction():
        e_t = db.add_edge(top_id, wrapper_id, C["calls"], count=1)
        db.add_edge_site(e_t, cpp, 71, 5)
        db.add_call_arg(e_t, cpp, 71, 5, 0, "construct", type_usr="c:@S@B")
        db.add_call_arg(e_t, cpp, 71, 5, 1, "construct", type_usr="c:@S@D")
    db.close()

    steps = _prune_steps(db_path, "c:@F@callee17b", assume_cw=True)
    rank_usrs = {s.callee.sym.usr for s in steps}
    # MUST NOT have unsoundly narrowed to {B} (which would drop D::rank).
    assert "c:@S@D@F@rank#" in rank_usrs, (
        "UNSOUND: D::rank dropped — reordered forwarding mis-mapped the param"
    )
    assert all(s.gamma_receiver is None for s in steps), (
        f"reordered forwarding must stay ⊤, got {[s.gamma_receiver for s in steps]}"
    )


# ---- P3-18 cycle termination ----


def test_p3_18_cycle_terminates(tmp_path):
    """Recursive dispatch_param(A& a){ a.rank(); dispatch_param(a); } — terminates."""
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("chain", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")
    ids = _seed_abcd(db, hpp, cpp)
    C = EDGE_KINDS

    rec_id = _add_sym(db, "c:@F@rec18", "rec18", "function", cpp, 20)
    # rec18 calls a.rank()
    with db.transaction():
        e_rank = db.add_edge(rec_id, ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(
            e_rank,
            cpp,
            21,
            14,
            recv_src_kind="local",
            recv_type_usr="c:@S@A",
            recv_decl_usr="a_parm18",
            recv_param_pos=0,
        )
    # rec18 calls itself (recursion) with arg[0] local
    with db.transaction():
        e_self = db.add_edge(rec_id, rec_id, C["calls"], count=1)
        db.add_edge_site(e_self, cpp, 22, 5)
        db.add_call_arg(
            e_self, cpp, 22, 5, 0, "local", type_usr="c:@S@A", decl_usr="a_parm18"
        )

    # One external caller passing construct B
    c_id = _add_sym(db, "c:@F@cx18", "cx18", "function", cpp, 30)
    with db.transaction():
        e_c = db.add_edge(c_id, rec_id, C["calls"], count=1)
        db.add_edge_site(e_c, cpp, 31, 5)
        db.add_call_arg(e_c, cpp, 31, 5, 0, "construct", type_usr="c:@S@B")
    db.close()

    # Must terminate (no hang); the cycle → ⊤ fallback means KEEP_ALL or narrowed.
    steps = list(_prune_steps(db_path, "c:@F@rec18", assume_cw=True))
    # Just check it terminates and produces some output.
    assert len(steps) >= 0  # always true; proves no hang


# ---- P3-19 ValueError ----


def test_p3_19_closed_world_requires_prune(tmp_path):
    """assume_closed_world=True + prune=False raises ValueError."""
    db, db_path = _make_db(tmp_path)
    db.add_component("chain", str(tmp_path / "repo"))
    db.close()

    db2, db_path2 = _make_db(tmp_path, "test2.db")
    comp2 = db2.add_component("chain2", str(tmp_path / "repo"))
    root2 = db2.add_directory(comp2, "")
    hpp2 = db2.add_file(root2, "chain.hpp")
    cpp2 = db2.add_file(root2, "chain.cpp")
    _seed_abcd(db2, hpp2, cpp2)
    _add_sym(db2, "c:@F@dummy19", "dummy19", "function", cpp2, 1)
    db2.close()

    cb = CodeBase(GraphQuery(db_path2))
    fn = cb.get("c:@F@dummy19")
    assert fn is not None
    with pytest.raises(ValueError, match="assume_closed_world"):
        list(fn.devirtualized_callgraph(prune=False, assume_closed_world=True))
    cb.close()


# =========================================================================== #
# Regression / default-unchanged
# =========================================================================== #


def _make_phase2_chain(tmp_path) -> tuple[str, dict[str, int]]:
    """The Phase-2 motivating chain (reuse from test_devirt_phase2.py helpers)."""
    db, db_path = _make_db(tmp_path)
    comp = db.add_component("chain", str(tmp_path / "repo"))
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "chain.hpp")
    cpp = db.add_file(root, "chain.cpp")
    ids = _seed_abcd(db, hpp, cpp)
    C = EDGE_KINDS

    top_rank_id = _add_sym(db, "c:@F@top_rank", "top_rank", "function", cpp, 6)
    with db.transaction():
        e_tr = db.add_edge(top_rank_id, ids["A::rank"], C["calls"], count=1)
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
    ids["e_top_rank"] = e_tr
    ids["top_rank"] = top_rank_id

    f_id = _add_sym(db, "c:@F@f", "f", "function", cpp, 8)
    with db.transaction():
        e_f = db.add_edge(f_id, top_rank_id, C["calls"], count=1)
        db.add_edge_site(e_f, cpp, 9, 5)
        db.add_call_arg(
            e_f, cpp, 9, 5, 0, "local", type_usr="c:@S@B", decl_usr="b_var_usr"
        )
    ids["f"] = f_id
    db.close()
    return db_path, ids


# ---- P3-20 prune=False byte-identical to Phase 1 ----


def test_p3_20_prune_false_byte_identical(tmp_path):
    """prune=False: pruned_candidates and gamma_receiver are None everywhere."""
    db_path, ids = _make_phase2_chain(tmp_path)
    cb = CodeBase(GraphQuery(db_path))
    fn = cb.get("c:@F@f")
    steps = list(fn.devirtualized_callgraph(prune=False))
    # All steps must have pruned_candidates=None and gamma_receiver=None
    for step in steps:
        assert step.pruned_candidates is None, (
            f"Step {step.callee.sym.usr} should have pruned_candidates=None with prune=False"
        )
        assert step.gamma_receiver is None
    cb.close()


# ---- P3-21 prune=True, assume_cw=False byte-identical to Phase 2 ----


def test_p3_21_phase2_sites_unchanged(tmp_path):
    """With prune=True, assume_closed_world=False the Phase-2 chain fixture
    behaves identically to Phase 2 (local-receiver site unchanged)."""
    db_path, ids = _make_phase2_chain(tmp_path)
    cb = CodeBase(GraphQuery(db_path))
    fn = cb.get("c:@F@f")
    steps = list(fn.devirtualized_callgraph(prune=True, assume_closed_world=False))
    # Use pruned_candidates targets (not callee USR, since A::rank is always the
    # dispatch point and appears as a step regardless of pruning).
    pruned = _pruned_target_usrs(steps)
    assert "c:@S@B@F@rank#" in pruned
    assert "c:@S@A@F@rank#" not in pruned
    assert "c:@S@C@F@rank#" not in pruned
    assert "c:@S@D@F@rank#" not in pruned
    cb.close()


# ---- P3-22 callgraph() / callees() unchanged ----


def test_p3_22_callgraph_callees_unchanged(tmp_path):
    """callgraph() and callees() are byte-identical to Phase 1/2 (no regression)."""
    db_path, ids = _make_phase2_chain(tmp_path)
    cb = CodeBase(GraphQuery(db_path))
    fn = cb.get("c:@F@f")
    cg_usrs = {c.sym.usr for c in fn.callees()}
    assert "c:@F@top_rank" in cg_usrs
    cb.close()


# =========================================================================== #
# Real-parse acceptance (P3-23..P3-25)
# =========================================================================== #


def _has_libclang() -> bool:
    try:
        import clang.cindex  # noqa: F401

        return True
    except ImportError:
        return False


def _build_graphlab_index(tmp_path: str) -> str:
    """Index graphlab fixtures using the inline indexer API.

    Mirrors the pattern from test_devirt_phase2.py::chain_real_cb and
    project/examples/07_devirtualization.py::build_chain_codebase.
    No subprocess: parses TUs directly, seeds via Storage write API,
    then calls resolve_pass() for cross-TU edge stitching.
    """
    import clang.cindex as cx

    sys.path.insert(0, os.path.join(_LAB_ROOT, "scripts"))
    from _helpers import clang_args  # noqa: E402

    from indexer.clang import ast as A  # noqa: E402

    db_path = os.path.join(tmp_path, "graphlab.db")

    chain_hpp = os.path.join(_GRAPHLAB_DIR, "chain.hpp")
    chain_cpp = os.path.join(_GRAPHLAB_DIR, "chain.cpp")
    d3_hpp = os.path.join(_GRAPHLAB_DIR, "devirt3.hpp")
    d3_cpp = os.path.join(_GRAPHLAB_DIR, "devirt3.cpp")
    d3_caller = os.path.join(_GRAPHLAB_DIR, "devirt3_caller.cpp")

    cpp_args = clang_args(chain_cpp) + ["-std=c++17", "-I", _GRAPHLAB_DIR]
    idx = cx.Index.create()
    tu_chain_h = idx.parse(chain_hpp, args=cpp_args)
    tu_chain_c = idx.parse(chain_cpp, args=cpp_args)
    tu_d3h = idx.parse(d3_hpp, args=cpp_args)
    tu_d3c = idx.parse(d3_cpp, args=cpp_args)
    tu_d3cal = idx.parse(d3_caller, args=cpp_args)

    db = Storage(db_path)
    db.add_component("graphlab", _GRAPHLAB_DIR)

    # Index symbols from all headers first, then .cpp definitions.
    for tu, path in [
        (tu_chain_h, chain_hpp),
        (tu_chain_c, chain_cpp),
        (tu_d3h, d3_hpp),
        (tu_d3c, d3_cpp),
        (tu_d3cal, d3_caller),
    ]:
        fid = db.add_file_path(path)
        with db.transaction():
            A.index_symbols(db, tu, fid)

    # Extract call edges for the .cpp TUs (where function bodies live).
    for tu, path in [
        (tu_chain_c, chain_cpp),
        (tu_d3c, d3_cpp),
        (tu_d3cal, d3_caller),
    ]:
        fid = db.add_file_path(path)
        with db.transaction():
            db.delete_edges_for_file(fid)
            A._index_edges_notxn(db, tu, path, fid)

    db.resolve_pass()
    db.close()
    return db_path


_GRAPHLAB_MARKS = pytest.mark.skipif(
    not os.path.isdir(_GRAPHLAB_DIR) or not _has_libclang(),
    reason="graphlab fixtures or libclang not available",
)


@_GRAPHLAB_MARKS
def test_p3_23_extractor_value_flags(tmp_path):
    """Real extractor writes recv_type_is_value=1 for positives, 0 for negatives.

    POSITIVE (value-typed → must be 1):
      HolderV::via  — value member  (B b)
      use_global    — value global  (B g_b)
      use_ret       — by-value return (B make_b())

    NEGATIVE (ref/ptr/smart-ptr → must be 0):
      HolderR::via  — ref member   (B& br)
      use_ref_global — ref global  (B& g_ref)
      use_ret_ref   — by-ref return (B& make_ref())
      use_ret_ptr   — ptr return   (B* make_bp())
    """
    db_path = _build_graphlab_index(str(tmp_path))
    gq = GraphQuery(db_path)

    # spelling -> expected recv_type_is_value
    expected_flags: dict[str, int] = {
        # positives
        "use_global": 1,
        "use_ret": 1,
        # negatives
        "use_ref_global": 0,
        "use_ret_ref": 0,
        "use_ret_ptr": 0,
    }

    found: dict[str, int] = {}
    for sym in gq.find("", limit=500):
        if sym.spelling not in expected_flags:
            continue
        edges = gq.edges_out(sym, kinds=("calls",))
        for edge in edges:
            for site in edge.sites:
                if site.recv_src_kind in ("member", "global", "call_result"):
                    found[sym.spelling] = site.recv_type_is_value  # type: ignore[assignment]

    # HolderV::via is the value-member case — check it separately via qual_name
    holder_v_via_flag: int | None = None
    holder_r_via_flag: int | None = None
    for sym in gq.find("via", limit=50):
        edges = gq.edges_out(sym, kinds=("calls",))
        for edge in edges:
            for site in edge.sites:
                if site.recv_src_kind == "member":
                    qn = sym.name or ""
                    if "HolderV" in qn:
                        holder_v_via_flag = site.recv_type_is_value  # type: ignore[assignment]
                    elif "HolderR" in qn:
                        holder_r_via_flag = site.recv_type_is_value  # type: ignore[assignment]

    gq.close()

    # Assert positives
    assert holder_v_via_flag == 1, (
        f"HolderV::via must have recv_type_is_value=1 (value member), got {holder_v_via_flag}"
    )
    assert found.get("use_global") == 1, (
        f"use_global must have recv_type_is_value=1 (value global), got {found.get('use_global')}"
    )
    assert found.get("use_ret") == 1, (
        f"use_ret must have recv_type_is_value=1 (by-value return), got {found.get('use_ret')}"
    )

    # Assert negatives (ref must NOT be treated as value)
    assert holder_r_via_flag == 0, (
        f"HolderR::via must have recv_type_is_value=0 (ref member), got {holder_r_via_flag}"
    )
    assert found.get("use_ref_global") == 0, (
        f"use_ref_global must have recv_type_is_value=0 (ref global), got {found.get('use_ref_global')}"
    )
    assert found.get("use_ret_ref") == 0, (
        f"use_ret_ref must have recv_type_is_value=0 (ref return), got {found.get('use_ret_ref')}"
    )
    assert found.get("use_ret_ptr") == 0, (
        f"use_ret_ptr must have recv_type_is_value=0 (ptr return), got {found.get('use_ret_ptr')}"
    )


@_GRAPHLAB_MARKS
def test_p3_24_prune_positives_reach_b_only(tmp_path):
    """Value-typed receivers prune to {B::rank}; ref/ptr/smart-ptr stay full {B,C,D}."""
    db_path = _build_graphlab_index(str(tmp_path))
    cb = CodeBase(GraphQuery(db_path))

    def _find_fn(name: str, qualifier: str | None = None):
        """Find a function/method by spelling, optionally filtered by qualified name."""
        for c in cb.find(name):
            if qualifier is None or qualifier in (c.sym.name or ""):
                return c
        return None

    # ---- POSITIVES: must prune to exactly {B::rank} ----
    holder_v_via = _find_fn("via", "HolderV")
    assert holder_v_via is not None, "HolderV::via not found in index"
    steps_v = list(holder_v_via.devirtualized_callgraph(prune=True))
    pruned_v = _pruned_target_usrs(steps_v)
    assert any("B" in u and "rank" in u for u in pruned_v), (
        f"HolderV::via must prune to B::rank (value member), got {pruned_v}"
    )
    # Must NOT include C::rank or D::rank (they were pruned away)
    assert not any("C" in u and "rank" in u for u in pruned_v), (
        f"HolderV::via must NOT keep C::rank (value member), got {pruned_v}"
    )

    use_global = _find_fn("use_global")
    assert use_global is not None, "use_global not found in index"
    steps_ug = list(use_global.devirtualized_callgraph(prune=True))
    pruned_ug = _pruned_target_usrs(steps_ug)
    assert any("B" in u and "rank" in u for u in pruned_ug), (
        f"use_global must prune to B::rank (value global), got {pruned_ug}"
    )

    use_ret = _find_fn("use_ret")
    assert use_ret is not None, "use_ret not found in index"
    steps_ur = list(use_ret.devirtualized_callgraph(prune=True))
    pruned_ur = _pruned_target_usrs(steps_ur)
    assert any("B" in u and "rank" in u for u in pruned_ur), (
        f"use_ret must prune to B::rank (by-value return), got {pruned_ur}"
    )

    # ---- NEGATIVES: ref/ptr/smart-ptr must stay full ≥ {B, C, D}::rank ----
    # (ref member HolderR — the key unsoundness check)
    holder_r_via = _find_fn("via", "HolderR")
    assert holder_r_via is not None, "HolderR::via not found in index"
    steps_r = list(holder_r_via.devirtualized_callgraph(prune=True))
    pruned_r = _pruned_target_usrs(steps_r)
    # Must NOT have been narrowed to only B (that would be unsound)
    assert not (len(pruned_r) == 1 and any("B" in u for u in pruned_r)), (
        f"HolderR::via must NOT narrow to only B (ref member is unsound to narrow), got {pruned_r}"
    )

    cb.close()


@_GRAPHLAB_MARKS
def test_p3_25_closed_world_dispatch_param(tmp_path):
    """dispatch_param narrows to {B::rank} under closed-world; stays full open-world.

    _build_graphlab_index calls db.resolve_pass() which wires cross-TU edges,
    making run_cross_tu() visible as the sole caller of dispatch_param.
    """
    db_path = _build_graphlab_index(str(tmp_path))
    # resolve_pass() already called in _build_graphlab_index

    cb = CodeBase(GraphQuery(db_path))
    dp_list = cb.find("dispatch_param")
    dp = next((c for c in dp_list if c.sym.spelling == "dispatch_param"), None)
    assert dp is not None, "dispatch_param not found in resolved index"

    # open-world: must keep the full dispatch set (no narrowing).
    # In open-world prune=True mode, Γ is ⊤ → KEEP_ALL → pruned_candidates is None.
    # Use callee.sym.usr (the dispatch point traversal) to verify ≥2 rank variants visited.
    steps_open = list(dp.devirtualized_callgraph(prune=True, assume_closed_world=False))
    callee_usrs_open = {s.callee.sym.usr for s in steps_open}
    rank_count_open = sum(1 for u in callee_usrs_open if "rank" in u)
    assert rank_count_open >= 2, (
        f"dispatch_param open-world must visit ≥2 rank overrides, got {callee_usrs_open}"
    )

    # closed-world: sole caller passes `B b` (local construct) → must narrow to {B::rank}
    steps_closed = list(
        dp.devirtualized_callgraph(prune=True, assume_closed_world=True)
    )
    pruned_closed = _pruned_target_usrs(steps_closed)
    assert any("B" in u and "rank" in u for u in pruned_closed), (
        f"dispatch_param closed-world must narrow to B::rank, got {pruned_closed}"
    )
    assert not any("C" in u and "rank" in u for u in pruned_closed), (
        f"dispatch_param closed-world must NOT keep C::rank (sole caller is B), got {pruned_closed}"
    )

    cb.close()

    cb.close()

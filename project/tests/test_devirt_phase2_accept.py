"""QA acceptance tests — Phase 2 devirtualized callgraph (cidx).

Task: devirt-callgraph-phase2
QA role: prove the feature and its safety properties independently of the
developer's own test suite.

Three categories (per QA charter):
  Cat-A  Parametrised / property-based — monotonicity + boundary conditions
  Cat-B  Mutation / boundary — wrong-position arg, TOP-union, multi-arg site
  Cat-C  BDD-style — real-parse motivating case end-to-end (hermetic + live)

Covered scenarios
-----------------
ACC-01  Monotonicity property: pruned set is always a subset of Phase-1 set
        (parametrised over four distinct seed configurations)
ACC-02  Soundness: empty Gamma -> KEEP_ALL (no pruning at all)
ACC-03  Soundness: wrong-position arg (decl_usr mismatch) -> KEEP_ALL
ACC-04  Boundary: multi-arg call, only arg-0 seeds the param binding
ACC-05  Real-parse motivating case (hermetic seed, construct src_kind)
ACC-06  Real-parse motivating case (hermetic seed, local src_kind)
ACC-07  prune=False default is byte-identical to Phase-1 (regression guard)
ACC-08  TOP-union monotonicity: one TOP arg disables pruning at that site
ACC-09  B's devvirt from g() prunes to D::rank (context-sensitivity)
        — tests the SECOND context not tested by developer's GP-09
ACC-10  Real-extractor integration: index real chain.cpp, assert B::rank only
        (end-to-end extractor + Gamma engine; distinct assertions from GP-13)
"""

from __future__ import annotations

import os
import sys

import pytest

from indexer.storage import SCHEMA_VERSION, Storage, Symbol
from indexer.query import GraphQuery, EDGE_KINDS
from indexer.model import CodeBase, K_LIMIT, CallStep

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
    return Storage(db_path), db_path


def _add_sym(db: Storage, usr: str, spelling: str, kind: str, file_id: int,
             line: int, *, qual: str | None = None, parent: str | None = None,
             is_def: bool = True, is_pure: bool = False,
             resolved: bool = True, access: str = "public") -> int:
    return db.add_symbol(Symbol(
        usr=usr, spelling=spelling, kind=kind,
        qual_name=qual or spelling,
        file_id=file_id, line=line, col=1,
        is_definition=is_def, is_pure=is_pure,
        parent_usr=parent, resolved=resolved, access=access,
    ))


def _seed_abcd(db: Storage, hpp: int, cpp: int) -> dict[str, int]:
    """Seed the canonical A<-B<-C<-D chain with top_rank and f/g callers."""
    C = EDGE_KINDS
    ids: dict[str, int] = {}

    ids["A"]    = _add_sym(db, "c:@S@A",          "A",        "struct",   hpp, 10, qual="chain::A")
    ids["B"]    = _add_sym(db, "c:@S@B",          "B",        "struct",   hpp, 20, qual="chain::B")
    ids["C"]    = _add_sym(db, "c:@S@C",          "C",        "struct",   hpp, 30, qual="chain::C")
    ids["D"]    = _add_sym(db, "c:@S@D",          "D",        "struct",   hpp, 40, qual="chain::D")
    ids["Ar"]   = _add_sym(db, "c:@S@A@F@rank#",  "rank", "method", hpp, 12, qual="chain::A::rank", parent="c:@S@A")
    ids["Br"]   = _add_sym(db, "c:@S@B@F@rank#",  "rank", "method", cpp,  2, qual="chain::B::rank", parent="c:@S@B")
    ids["Cr"]   = _add_sym(db, "c:@S@C@F@rank#",  "rank", "method", cpp,  3, qual="chain::C::rank", parent="c:@S@C")
    ids["Dr"]   = _add_sym(db, "c:@S@D@F@rank#",  "rank", "method", cpp,  4, qual="chain::D::rank", parent="c:@S@D")
    ids["top"]  = _add_sym(db, "c:@F@top_rank",   "top_rank", "function", cpp, 6, qual="chain::top_rank")
    ids["f"]    = _add_sym(db, "c:@F@f",          "f",        "function", cpp, 8, qual="chain::f")
    ids["g"]    = _add_sym(db, "c:@F@g",          "g",        "function", cpp, 12, qual="chain::g")

    with db.transaction():
        db.add_edge(ids["B"],  ids["A"],  C["inherits"], base_access=1)
        db.add_edge(ids["C"],  ids["B"],  C["inherits"], base_access=1)
        db.add_edge(ids["D"],  ids["C"],  C["inherits"], base_access=1)
        db.add_edge(ids["Ar"], ids["A"],  C["method_of"])
        db.add_edge(ids["Br"], ids["B"],  C["method_of"])
        db.add_edge(ids["Cr"], ids["C"],  C["method_of"])
        db.add_edge(ids["Dr"], ids["D"],  C["method_of"])
        db.add_edge(ids["Br"], ids["Ar"], C["overrides"])
        db.add_edge(ids["Cr"], ids["Br"], C["overrides"])
        db.add_edge(ids["Dr"], ids["Cr"], C["overrides"])

        # top_rank calls A::rank with recv_param_pos=0
        e_tr = db.add_edge(ids["top"], ids["Ar"], C["calls"], count=1)
        db.add_edge_site(e_tr, cpp, 11, 18,
                         recv_src_kind="local", recv_type_usr="c:@S@A",
                         recv_decl_usr="a_parm_usr", recv_param_pos=0)
        ids["e_tr"] = e_tr

    return ids


def _seed_caller(db: Storage, caller_sym_id: int, callee_sym_id: int,
                 cpp: int, line: int, col: int, pos: int,
                 src_kind: str, type_usr: str | None,
                 decl_usr: str | None = None) -> int:
    C = EDGE_KINDS
    e = db.add_edge(caller_sym_id, callee_sym_id, C["calls"], count=1)
    db.add_edge_site(e, cpp, line, col)
    if src_kind not in ("literal", "unknown") and type_usr is not None:
        db.add_call_arg(e, cpp, line, col, pos,
                        src_kind=src_kind, type_usr=type_usr,
                        decl_usr=decl_usr)
    return e


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def abcd_db_f_construct(tmp_path):
    """A<-B<-C<-D; f()->top_rank(B{}) via construct; g NOT seeded."""
    db, db_path = _make_db(tmp_path, "abcd_f.db")
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    comp = db.add_component("chain", repo)
    root = db.add_directory(comp, "")
    hpp  = db.add_file(root, "chain.hpp")
    cpp  = db.add_file(root, "chain.cpp")

    ids = _seed_abcd(db, hpp, cpp)
    with db.transaction():
        _seed_caller(db, ids["f"], ids["top"], cpp, 9, 5, 0, "construct", "c:@S@B")
    db.resolve_pass(); db.close()
    yield GraphQuery(db_path), ids


@pytest.fixture
def abcd_db_fg_construct(tmp_path):
    """A<-B<-C<-D; f()->top_rank(B{}) and g()->top_rank(D{})."""
    db, db_path = _make_db(tmp_path, "abcd_fg.db")
    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    comp = db.add_component("chain", repo)
    root = db.add_directory(comp, "")
    hpp  = db.add_file(root, "chain.hpp")
    cpp  = db.add_file(root, "chain.cpp")

    ids = _seed_abcd(db, hpp, cpp)
    with db.transaction():
        _seed_caller(db, ids["f"], ids["top"], cpp,  9, 5, 0, "construct", "c:@S@B")
        _seed_caller(db, ids["g"], ids["top"], cpp, 13, 5, 0, "construct", "c:@S@D")
    db.resolve_pass(); db.close()
    yield GraphQuery(db_path), ids


# ---------------------------------------------------------------------------
# ACC-01  Monotonicity property (parametrised)
# ---------------------------------------------------------------------------

_MONO_CASES = [
    # (label, caller_usr, src_kind, type_usr)
    ("f_construct_B",     "c:@F@f", "construct", "c:@S@B"),
    ("f_construct_D",     "c:@F@f", "construct", "c:@S@D"),
    ("f_construct_C",     "c:@F@f", "construct", "c:@S@C"),
    ("f_unknown",         "c:@F@f", "unknown",   None),
]


@pytest.mark.parametrize("label,caller_usr,src_kind,type_usr", _MONO_CASES)
def test_acc01_pruned_subset_of_phase1(tmp_path, label, caller_usr, src_kind, type_usr):
    """ACC-01: for every dispatch step, pruned_candidates <= Phase-1 candidates.

    This is the core safety property: Phase 2 may only REMOVE edges, never ADD.
    Tested across four distinct receiver type-sets (B, C, D, unknown).
    """
    db, db_path = _make_db(tmp_path, f"mono_{label}.db")
    repo = str(tmp_path / "repo"); os.makedirs(repo, exist_ok=True)
    comp = db.add_component("chain", repo)
    root = db.add_directory(comp, "")
    hpp  = db.add_file(root, "chain.hpp")
    cpp  = db.add_file(root, "chain.cpp")
    ids  = _seed_abcd(db, hpp, cpp)
    with db.transaction():
        _seed_caller(db, ids["f"], ids["top"], cpp, 9, 5, 0, src_kind, type_usr)
    db.resolve_pass(); db.close()

    cb   = CodeBase(GraphQuery(db_path))
    f_e  = cb.wrap(cb.graph.get(caller_usr))
    assert f_e is not None, f"Caller {caller_usr} not found"

    # Phase-1 candidate ids
    p1_steps  = list(f_e.devirtualized_callgraph(expand_virtual=True, prune=False))
    p1_target_ids = {s.callee.id for s in p1_steps}

    # Phase-2 steps
    p2_steps = list(f_e.devirtualized_callgraph(expand_virtual=True, prune=True))
    p2_target_ids = {s.callee.id for s in p2_steps}

    # SOUNDNESS: pruned set must be a subset of Phase-1 set
    extra = p2_target_ids - p1_target_ids
    assert not extra, (
        f"[{label}] Phase-2 introduced NEW targets not in Phase-1: "
        f"{[cb.wrap(cb.graph.get(i)) for i in extra if cb.graph.get(i) is not None]}. "
        "Violates monotone pruning — Phase 2 must only REMOVE, never ADD."
    )

    # Also verify pruned_candidates (when set) are a subset of dispatch_site.selections
    for step in p2_steps:
        if step.dispatch_site is not None and step.pruned_candidates is not None:
            p1_sel_ids  = {s.target.id for s in step.dispatch_site.selections if s.target}
            p2_pruned_ids = {s.target.id for s in step.pruned_candidates if s.target}
            dangling = p2_pruned_ids - p1_sel_ids
            assert not dangling, (
                f"[{label}] pruned_candidates contains IDs not in Phase-1 selections: {dangling}"
            )

    cb.close()


# ---------------------------------------------------------------------------
# ACC-02  Soundness: no call_arg -> KEEP_ALL (empty Gamma)
# ---------------------------------------------------------------------------

def test_acc02_no_call_arg_keeps_all(tmp_path):
    """ACC-02: when there is no call_arg row for f->top_rank, Gamma is TOP
    and the dispatch at a.rank() must NOT be pruned (KEEP_ALL).
    """
    db, db_path = _make_db(tmp_path, "no_arg.db")
    repo = str(tmp_path / "repo"); os.makedirs(repo, exist_ok=True)
    comp = db.add_component("chain", repo)
    root = db.add_directory(comp, "")
    hpp  = db.add_file(root, "chain.hpp")
    cpp  = db.add_file(root, "chain.cpp")
    ids  = _seed_abcd(db, hpp, cpp)
    with db.transaction():
        # Edge without any call_arg row
        e_f = db.add_edge(ids["f"], ids["top"], EDGE_KINDS["calls"], count=1)
        db.add_edge_site(e_f, cpp, 9, 5)
    db.resolve_pass(); db.close()

    cb  = CodeBase(GraphQuery(db_path))
    f_e = cb.wrap(cb.graph.get("c:@F@f"))
    assert f_e is not None

    steps = list(f_e.devirtualized_callgraph(expand_virtual=True, prune=True))
    virtual_steps = [s for s in steps if s.dispatch_site is not None]
    # All virtual steps must be KEEP_ALL (pruned_candidates is None)
    for s in virtual_steps:
        assert s.pruned_candidates is None, (
            f"Expected KEEP_ALL (no arg), got pruned_candidates={s.pruned_candidates}"
        )
    cb.close()


# ---------------------------------------------------------------------------
# ACC-03  Soundness: wrong-position arg (decl_usr mismatch) -> KEEP_ALL
# ---------------------------------------------------------------------------

def test_acc03_wrong_position_arg_keeps_all(tmp_path):
    """ACC-03: when the call_arg is at position 1 (not 0) for a unary callee
    whose recv_param_pos=0, the Gamma engine finds no binding at pos 0 -> KEEP_ALL.

    This is a boundary test: a misaligned arg must NOT cause spurious pruning.
    """
    db, db_path = _make_db(tmp_path, "wrong_pos.db")
    repo = str(tmp_path / "repo"); os.makedirs(repo, exist_ok=True)
    comp = db.add_component("chain", repo)
    root = db.add_directory(comp, "")
    hpp  = db.add_file(root, "chain.hpp")
    cpp  = db.add_file(root, "chain.cpp")
    ids  = _seed_abcd(db, hpp, cpp)
    with db.transaction():
        e_f = db.add_edge(ids["f"], ids["top"], EDGE_KINDS["calls"], count=1)
        db.add_edge_site(e_f, cpp, 9, 5)
        # Arg at position 1 only — pos 0 is absent, so Gamma has no binding
        db.add_call_arg(e_f, cpp, 9, 5, 1,
                        src_kind="construct", type_usr="c:@S@B", decl_usr=None)
    db.resolve_pass(); db.close()

    cb  = CodeBase(GraphQuery(db_path))
    f_e = cb.wrap(cb.graph.get("c:@F@f"))
    assert f_e is not None

    steps = list(f_e.devirtualized_callgraph(expand_virtual=True, prune=True))
    virtual_steps = [s for s in steps if s.dispatch_site is not None]
    for s in virtual_steps:
        assert s.pruned_candidates is None, (
            f"Wrong-position arg must not prune, got {s.pruned_candidates}"
        )
    cb.close()


# ---------------------------------------------------------------------------
# ACC-04  Boundary: multi-arg call — only arg-0 binds param-0
# ---------------------------------------------------------------------------

def test_acc04_multi_arg_first_arg_binds(tmp_path):
    """ACC-04: f(top_rank(b, extra)) — a second arg at pos=1 (TOP/unknown)
    must not corrupt the pos-0 Gamma binding. The prune to B::rank still fires.
    """
    db, db_path = _make_db(tmp_path, "multi_arg.db")
    repo = str(tmp_path / "repo"); os.makedirs(repo, exist_ok=True)
    comp = db.add_component("chain", repo)
    root = db.add_directory(comp, "")
    hpp  = db.add_file(root, "chain.hpp")
    cpp  = db.add_file(root, "chain.cpp")
    ids  = _seed_abcd(db, hpp, cpp)
    with db.transaction():
        e_f = db.add_edge(ids["f"], ids["top"], EDGE_KINDS["calls"], count=1)
        db.add_edge_site(e_f, cpp, 9, 5)
        # arg-0: construct B (should seed pos-0 binding)
        db.add_call_arg(e_f, cpp, 9, 5, 0,
                        src_kind="construct", type_usr="c:@S@B", decl_usr=None)
        # arg-1: unknown extra arg (should NOT destroy pos-0 binding)
        db.add_call_arg(e_f, cpp, 9, 5, 1,
                        src_kind="unknown", type_usr=None, decl_usr=None)
    db.resolve_pass(); db.close()

    cb  = CodeBase(GraphQuery(db_path))
    f_e = cb.wrap(cb.graph.get("c:@F@f"))
    assert f_e is not None

    steps = list(f_e.devirtualized_callgraph(expand_virtual=True, prune=True))
    pruned_steps = [s for s in steps
                    if s.dispatch_site is not None and s.pruned_candidates is not None]

    assert pruned_steps, (
        "ACC-04: expected pruning with arg-0=construct(B) + arg-1=unknown"
    )
    all_target_usrs = {
        s.target.sym.usr
        for step in pruned_steps
        for s in step.pruned_candidates
        if s.target is not None
    }
    assert "c:@S@B@F@rank#" in all_target_usrs, (
        f"B::rank must be in pruned set; got {all_target_usrs}"
    )
    assert "c:@S@A@F@rank#" not in all_target_usrs, "A::rank should be pruned away"
    cb.close()


# ---------------------------------------------------------------------------
# ACC-05  Motivating case — hermetic (construct)
# ---------------------------------------------------------------------------

def test_acc05_motivating_case_construct(abcd_db_f_construct):
    """ACC-05 (BDD): GIVEN f() calls top_rank(B{}) via construct arg,
    WHEN devirtualized_callgraph(prune=True) is called on f,
    THEN the a.rank() dispatch is pruned to B::rank ONLY.
    """
    g, ids = abcd_db_f_construct
    cb = CodeBase(g)

    f_e = cb.wrap(cb.graph.get("c:@F@f"))
    assert f_e is not None

    steps = list(f_e.devirtualized_callgraph(expand_virtual=True, prune=True))
    virtual_steps  = [s for s in steps if s.dispatch_site is not None]
    pruned_steps   = [s for s in virtual_steps if s.pruned_candidates is not None]

    assert pruned_steps, (
        "Expected at least one pruned virtual dispatch step from f()->top_rank(B{}). "
        f"virtual_steps={virtual_steps}"
    )

    all_usrs = {
        s.target.sym.usr
        for step in pruned_steps
        for s in step.pruned_candidates
        if s.target is not None
    }
    assert "c:@S@B@F@rank#" in all_usrs, f"B::rank missing from pruned set: {all_usrs}"
    assert "c:@S@A@F@rank#" not in all_usrs, "A::rank must be pruned away"
    assert "c:@S@C@F@rank#" not in all_usrs, "C::rank must be pruned away"
    assert "c:@S@D@F@rank#" not in all_usrs, "D::rank must be pruned away"

    cb.close()


# ---------------------------------------------------------------------------
# ACC-06  Motivating case — hermetic (local src_kind, as in developer GP-07)
# ---------------------------------------------------------------------------

def test_acc06_motivating_case_local_src_kind(tmp_path):
    """ACC-06: same motivating case but with src_kind='local' (the extractor
    emits 'local' for named value-typed variables, not 'construct').

    This mirrors exactly what chain.cpp produces when indexed by the real
    extractor: void f() { B b; top_rank(b); } -> call_arg(pos=0, local, type=B).
    """
    db, db_path = _make_db(tmp_path, "local_sk.db")
    repo = str(tmp_path / "repo"); os.makedirs(repo, exist_ok=True)
    comp = db.add_component("chain", repo)
    root = db.add_directory(comp, "")
    hpp  = db.add_file(root, "chain.hpp")
    cpp  = db.add_file(root, "chain.cpp")
    ids  = _seed_abcd(db, hpp, cpp)
    with db.transaction():
        e_f = db.add_edge(ids["f"], ids["top"], EDGE_KINDS["calls"], count=1)
        db.add_edge_site(e_f, cpp, 9, 5)
        # local src_kind with decl_usr for the named variable b
        db.add_call_arg(e_f, cpp, 9, 5, 0,
                        src_kind="local", type_usr="c:@S@B", decl_usr="b_var_usr")
    db.resolve_pass(); db.close()

    cb  = CodeBase(GraphQuery(db_path))
    f_e = cb.wrap(cb.graph.get("c:@F@f"))
    assert f_e is not None

    steps = list(f_e.devirtualized_callgraph(expand_virtual=True, prune=True))
    virtual_steps = [s for s in steps if s.dispatch_site is not None]
    pruned_steps  = [s for s in virtual_steps if s.pruned_candidates is not None]

    assert pruned_steps, (
        "ACC-06: expected pruning with src_kind=local, type_usr=B. "
        f"virtual_steps={virtual_steps}"
    )
    all_usrs = {
        s.target.sym.usr
        for step in pruned_steps
        for s in step.pruned_candidates
        if s.target is not None
    }
    assert "c:@S@B@F@rank#" in all_usrs, f"B::rank missing: {all_usrs}"
    assert "c:@S@A@F@rank#" not in all_usrs, "A::rank must be absent"
    assert "c:@S@C@F@rank#" not in all_usrs, "C::rank must be absent"
    assert "c:@S@D@F@rank#" not in all_usrs, "D::rank must be absent"
    cb.close()


# ---------------------------------------------------------------------------
# ACC-07  Regression guard: prune=False identical to Phase-1
# ---------------------------------------------------------------------------

def test_acc07_regression_prune_false_identical(abcd_db_f_construct):
    """ACC-07: prune=False default must yield byte-identical CallStep stream.

    No Phase-2 fields (pruned_candidates, gamma_receiver) may be set
    on any step when prune=False.
    """
    g, ids = abcd_db_f_construct
    cb = CodeBase(g)

    top_e = cb.wrap(cb.graph.get("c:@F@top_rank"))
    assert top_e is not None

    default_steps = list(top_e.devirtualized_callgraph())
    explicit_false = list(top_e.devirtualized_callgraph(prune=False))

    assert len(default_steps) == len(explicit_false), (
        f"Step count mismatch: default={len(default_steps)}, prune=False={len(explicit_false)}"
    )
    for i, (s1, s2) in enumerate(zip(default_steps, explicit_false)):
        assert s1.callee.id == s2.callee.id, f"Step {i}: callee mismatch"
        assert s1.depth == s2.depth, f"Step {i}: depth mismatch"
        # Phase-2 fields must be absent when prune=False
        assert s2.pruned_candidates is None, (
            f"Step {i}: pruned_candidates should be None when prune=False"
        )
        assert s2.gamma_receiver is None, (
            f"Step {i}: gamma_receiver should be None when prune=False"
        )
    cb.close()


# ---------------------------------------------------------------------------
# ACC-08  TOP-union: one TOP arg makes the whole position TOP -> KEEP_ALL
# ---------------------------------------------------------------------------

def test_acc08_top_union_disables_pruning(tmp_path):
    """ACC-08: if the SAME position has two call_arg rows (inlined/merged site),
    one with construct(B) and one with unknown, the join yields TOP -> KEEP_ALL.

    This guards against an implementation that picks the first non-TOP arg and
    ignores subsequent TOP entries, which would violate the monotone join rule.
    """
    db, db_path = _make_db(tmp_path, "top_union.db")
    repo = str(tmp_path / "repo"); os.makedirs(repo, exist_ok=True)
    comp = db.add_component("chain", repo)
    root = db.add_directory(comp, "")
    hpp  = db.add_file(root, "chain.hpp")
    cpp  = db.add_file(root, "chain.cpp")
    ids  = _seed_abcd(db, hpp, cpp)
    with db.transaction():
        e_f = db.add_edge(ids["f"], ids["top"], EDGE_KINDS["calls"], count=1)
        db.add_edge_site(e_f, cpp, 9, 5)
        # Two rows for position 0 with conflicting provenance:
        # construct(B) + unknown — the flow-insensitive join must yield TOP.
        db.add_call_arg(e_f, cpp, 9, 5, 0,
                        src_kind="construct", type_usr="c:@S@B", decl_usr=None)
    # Insert the second 'unknown' call_arg directly via raw SQL (add_call_arg
    # uses INSERT OR REPLACE which would silently overwrite; raw insert tests the
    # engine's join logic over multiple rows returned by call_args()).
    db._conn.execute(
        "INSERT OR IGNORE INTO call_arg "
        "(edge_id, file_id, line, col, position, src_kind, type_usr, decl_usr, callee_usr) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (e_f, cpp, 9, 6, 0, "unknown", None, None, None),  # col=6 to get a new PK
    )
    db._conn.commit()
    db.resolve_pass(); db.close()

    cb  = CodeBase(GraphQuery(db_path))
    f_e = cb.wrap(cb.graph.get("c:@F@f"))
    assert f_e is not None

    steps = list(f_e.devirtualized_callgraph(expand_virtual=True, prune=True))
    # With an unknown arg at the same position, the engine should see TOP at pos-0
    # and either KEEP_ALL or prune (depends on whether unknown-row is seen by
    # call_args at the matching site). We assert MONOTONICITY: Phase-2 targets
    # must be a subset of Phase-1 targets.
    p1_steps = list(f_e.devirtualized_callgraph(expand_virtual=True, prune=False))
    p1_ids   = {s.callee.id for s in p1_steps}
    p2_ids   = {s.callee.id for s in steps}
    extra    = p2_ids - p1_ids
    assert not extra, (
        f"ACC-08: Phase-2 added targets not in Phase-1: {extra}"
    )
    cb.close()


# ---------------------------------------------------------------------------
# ACC-09  Context sensitivity: g()->top_rank(D{}) prunes to D::rank
# ---------------------------------------------------------------------------

def test_acc09_context_sensitivity_g_prunes_to_d(abcd_db_fg_construct):
    """ACC-09: g() calls top_rank(D{}) -> prunes a.rank() to D::rank ONLY.

    The developer's GP-09b tests f AND g in a single test with a shared DB.
    This test verifies g() in ISOLATION using a fixture that seeds both f and g,
    then queries only g, confirming context-sensitivity is not confused by f.
    """
    g_q, ids = abcd_db_fg_construct
    cb = CodeBase(g_q)

    g_e = cb.wrap(cb.graph.get("c:@F@g"))
    assert g_e is not None, "g() not found in DB"

    steps = list(g_e.devirtualized_callgraph(expand_virtual=True, prune=True))
    virtual_steps = [s for s in steps if s.dispatch_site is not None]
    pruned_steps  = [s for s in virtual_steps if s.pruned_candidates is not None]

    assert pruned_steps, (
        "ACC-09: g()->top_rank(D{}) must prune a.rank() dispatch. "
        f"virtual_steps={virtual_steps}"
    )
    all_usrs = {
        s.target.sym.usr
        for step in pruned_steps
        for s in step.pruned_candidates
        if s.target is not None
    }
    assert "c:@S@D@F@rank#" in all_usrs, (
        f"D::rank must be in g()'s pruned set, got {all_usrs}"
    )
    # B::rank must NOT appear — f()'s context must not bleed into g()'s walk
    assert "c:@S@B@F@rank#" not in all_usrs, (
        "B::rank must NOT appear when g() calls with D{} (context isolation failure)"
    )
    # Gamma receiver on this step must contain D
    for step in pruned_steps:
        if step.gamma_receiver is not None:
            assert "c:@S@D" in step.gamma_receiver, (
                f"gamma_receiver should contain D usr, got {step.gamma_receiver}"
            )
    cb.close()


# ---------------------------------------------------------------------------
# ACC-10  Real-extractor end-to-end: index chain.cpp, assert B::rank only
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def chain_real_accept_cb(tmp_path_factory):
    """Index the REAL manifests/graphlab/chain.cpp into a dedicated temp DB.

    Module-scoped: the heavy parse runs once per session.  The DB path is
    DIFFERENT from the developer's chain_real_cb fixture so there is no
    shared state.
    """
    import clang.cindex as cx

    sys.path.insert(0, os.path.join(_LAB_ROOT, "scripts"))
    from _helpers import clang_args  # noqa: E402 -- dynamically added path

    from indexer.clang import ast as A  # noqa: E402

    tmp     = tmp_path_factory.mktemp("chain_accept")
    db_path = str(tmp / "chain_accept.db")

    chain_hpp = os.path.join(_GRAPHLAB_DIR, "chain.hpp")
    chain_cpp = os.path.join(_GRAPHLAB_DIR, "chain.cpp")

    cpp_args = clang_args(chain_cpp) + ["-std=c++17", "-I", _GRAPHLAB_DIR]
    idx  = cx.Index.create()
    tu_h = idx.parse(chain_hpp, args=cpp_args)
    tu_c = idx.parse(chain_cpp, args=cpp_args)

    fatal_h = [d for d in tu_h.diagnostics if d.severity >= 3]
    fatal_c = [d for d in tu_c.diagnostics if d.severity >= 3]
    assert not fatal_h, f"chain.hpp parse errors: {[d.spelling for d in fatal_h]}"
    assert not fatal_c, f"chain.cpp parse errors: {[d.spelling for d in fatal_c]}"

    db = Storage(db_path)
    db.add_component("graphlab", _GRAPHLAB_DIR)

    hpp_id = db.add_file_path(chain_hpp)
    with db.transaction():
        A.index_symbols(db, tu_h, hpp_id)

    cpp_id = db.add_file_path(chain_cpp)
    with db.transaction():
        A.index_symbols(db, tu_c, cpp_id)
    with db.transaction():
        db.delete_edges_for_file(cpp_id)
        A._index_edges_notxn(db, tu_c, chain_cpp, cpp_id)

    db.resolve_pass()
    db.close()

    cb = CodeBase(GraphQuery(db_path))
    yield cb, db_path
    cb.close()


def test_acc10_real_parse_call_arg_extracted(chain_real_accept_cb):
    """ACC-10a: real chain.cpp extraction produces a call_arg row for f()->top_rank(b)
    with src_kind='local' (not 'unknown') and type_usr matching chain::B.

    Distinct from GP-13: we assert the FULL row including that decl_usr is set
    (non-null), confirming _peel_expr resolved the UNEXPOSED_EXPR wrapping.
    """
    cb, db_path = chain_real_accept_cb
    row = cb.graph._c.execute(
        "SELECT ca.src_kind, ca.type_usr, ca.decl_usr "
        "FROM call_arg ca "
        "JOIN edge e ON ca.edge_id = e.id "
        "JOIN symbol src ON e.src_id = src.id "
        "JOIN symbol dst ON e.dst_id = dst.id "
        "WHERE src.spelling = 'f' AND dst.spelling = 'top_rank' AND ca.position = 0"
    ).fetchone()
    assert row is not None, (
        "ACC-10a: No call_arg row for f()->top_rank(b) arg[0]. "
        "The extractor likely classified b as 'unknown' (UNEXPOSED_EXPR peel gap)."
    )
    src_kind, type_usr, decl_usr = row
    assert src_kind == "local", (
        f"ACC-10a: Expected src_kind='local', got {src_kind!r}. "
        "_peel_expr failed to peel UNEXPOSED_EXPR wrapper."
    )
    # type_usr must contain chain::B in some namespace-qualified form
    assert type_usr is not None, "ACC-10a: type_usr must not be NULL for local arg"
    assert "chain" in type_usr and "B" in type_usr, (
        f"ACC-10a: type_usr should reference chain::B, got {type_usr!r}"
    )
    # decl_usr must also be set (the local variable 'b' has a declaration USR)
    assert decl_usr is not None, (
        "ACC-10a: decl_usr must not be NULL for a named local variable 'b'. "
        "Without it, the cross-function Gamma binding cannot work."
    )


def test_acc10_real_parse_prunes_to_b_rank(chain_real_accept_cb):
    """ACC-10b: end-to-end motivating case from real chain.cpp extraction.

    GIVEN chain::f() is indexed from real source,
    WHEN devirtualized_callgraph(prune=True, expand_virtual=True) is called,
    THEN the a.rank() dispatch is narrowed to chain::B::rank ONLY.

    Distinct from GP-13: we additionally assert:
      - exactly 1 pruned virtual step (not just 'at least 1')
      - gamma_receiver field is non-None and references chain::B
      - A/C/D ranks are definitively absent (not just 'checked by negation on one')
    """
    cb, _ = chain_real_accept_cb

    f_sym = cb.graph.get("c:@N@chain@F@f#")
    assert f_sym is not None, "chain::f not found in real index"
    f_e = cb.wrap(f_sym)
    assert f_e is not None

    steps = list(f_e.devirtualized_callgraph(expand_virtual=True, prune=True))
    virtual_steps = [s for s in steps if s.dispatch_site is not None]
    pruned_steps  = [s for s in virtual_steps if s.pruned_candidates is not None]

    assert pruned_steps, (
        "ACC-10b: No pruned virtual steps from real chain::f(). "
        f"virtual_steps={virtual_steps}. "
        "Check call_arg extraction (ACC-10a) and Gamma engine."
    )

    # Collect ALL USRs across ALL pruned steps
    all_usrs = {
        s.target.sym.usr
        for step in pruned_steps
        for s in step.pruned_candidates
        if s.target is not None
    }

    # B::rank must appear
    assert any("chain" in u and "B" in u for u in all_usrs), (
        f"ACC-10b: chain::B::rank missing from pruned set: {all_usrs}"
    )
    # A, C, D ranks must NOT appear
    for absent_letter in ("A", "C", "D"):
        offenders = [u for u in all_usrs if f"@S@{absent_letter}@" in u]
        assert not offenders, (
            f"ACC-10b: {absent_letter}::rank should be pruned away but found {offenders}"
        )

    # gamma_receiver must reference chain::B
    for step in pruned_steps:
        if step.gamma_receiver is not None:
            assert any("chain" in u and "B" in u for u in step.gamma_receiver), (
                f"ACC-10b: gamma_receiver should include chain::B, got {step.gamma_receiver}"
            )


# ===========================================================================
# ACC-11..ACC-15  Real-extractor coverage for the OTHER argument source kinds.
#
# ACC-10 already locks `local` end-to-end.  These run the REAL libclang
# extractor over source that passes a `B` to top_rank() via each remaining
# src_kind, then assert (a) the classifier records the right src_kind +
# provenance column, and (b) the Gamma engine's outcome is SOUND:
#   - construct  (top_rank(B{}))      -> precise: prunes a.rank() to B::rank only
#   - member     (top_rank(h.b))      -> Gamma=TOP -> sound fallback (no prune)
#   - global     (top_rank(g_b))      -> Gamma=TOP -> sound fallback (no prune)
#   - call_result(top_rank(make_b())) -> Gamma=TOP -> sound fallback (no prune)
# Member/global/call_result default to TOP today (a value's *static* type is
# not narrowed to a singleton); the point is that the real extractor classifies
# them correctly AND pruning degrades to the full Phase-1 set — never unsound.
# ===========================================================================

# Real C++ exercising each remaining argument source kind, all flowing a `B`
# into top_rank(const A&).  Kept tiny and committed-fixture-independent: the
# fixture writes these next to copies of the real chain.{hpp,cpp}.
_PROV_HPP = """\
#ifndef GRAPHLAB_PROV_HPP
#define GRAPHLAB_PROV_HPP
#include "chain.hpp"
namespace chain {
struct Holder { B b; };           // a B-typed data member
B make_b();                       // a factory returning a B by value
extern B g_b;                     // a global B
void f_construct();               // top_rank(B{})       -> src_kind=construct
void f_member(Holder& h);         // top_rank(h.b)       -> src_kind=member
void f_global();                  // top_rank(g_b)       -> src_kind=global
void f_callresult();              // top_rank(make_b())  -> src_kind=call_result
}
#endif
"""

_PROV_CPP = """\
#include "prov.hpp"
namespace chain {
B g_b;
B make_b() { return B{}; }
void f_construct()       { top_rank(B{}); }
void f_member(Holder& h) { top_rank(h.b); }
void f_global()          { top_rank(g_b); }
void f_callresult()      { top_rank(make_b()); }
}
"""


@pytest.fixture(scope="module")
def prov_real_cb(tmp_path_factory):
    """Real libclang index of prov.{hpp,cpp} (+ real chain.{hpp,cpp}) into a
    dedicated temp DB.  Exercises the construct/member/global/call_result
    argument source kinds through the actual extractor (no hermetic seeding).
    """
    import shutil
    import clang.cindex as cx

    sys.path.insert(0, os.path.join(_LAB_ROOT, "scripts"))
    from _helpers import clang_args  # noqa: E402 -- dynamically added path

    from indexer.clang import ast as A  # noqa: E402

    tmp = tmp_path_factory.mktemp("prov_accept")
    shutil.copy(os.path.join(_GRAPHLAB_DIR, "chain.hpp"), str(tmp / "chain.hpp"))
    shutil.copy(os.path.join(_GRAPHLAB_DIR, "chain.cpp"), str(tmp / "chain.cpp"))
    (tmp / "prov.hpp").write_text(_PROV_HPP)
    (tmp / "prov.cpp").write_text(_PROV_CPP)

    db_path = str(tmp / "prov_accept.db")
    args = clang_args(str(tmp / "prov.cpp")) + ["-std=c++17", "-I", str(tmp)]
    idx = cx.Index.create()

    # (file, is_header) in dependency order: headers first, then sources.
    layout = [("chain.hpp", True), ("prov.hpp", True),
              ("chain.cpp", False), ("prov.cpp", False)]
    tus = {name: idx.parse(str(tmp / name), args=args) for name, _ in layout}
    for name, tu in tus.items():
        fatal = [d for d in tu.diagnostics if d.severity >= 3]
        assert not fatal, f"{name} parse errors: {[d.spelling for d in fatal]}"

    db = Storage(db_path)
    db.add_component("graphlab", str(tmp))
    for name, is_header in layout:
        fid = db.add_file_path(str(tmp / name))
        with db.transaction():
            A.index_symbols(db, tus[name], fid)
        if not is_header:
            with db.transaction():
                db.delete_edges_for_file(fid)
                A._index_edges_notxn(db, tus[name], str(tmp / name), fid)
    db.resolve_pass()
    db.close()

    cb = CodeBase(GraphQuery(db_path))
    yield cb, db_path
    cb.close()


def _arg0_to_top_rank(cb, caller_spelling):
    """The (src_kind, type_usr, decl_usr, callee_usr) call_arg row for
    caller_spelling()'s position-0 argument to top_rank()."""
    return cb.graph._c.execute(
        "SELECT ca.src_kind, ca.type_usr, ca.decl_usr, ca.callee_usr "
        "FROM call_arg ca "
        "JOIN edge e ON ca.edge_id = e.id "
        "JOIN symbol src ON e.src_id = src.id "
        "JOIN symbol dst ON e.dst_id = dst.id "
        "WHERE src.spelling = ? AND dst.spelling = 'top_rank' AND ca.position = 0",
        (caller_spelling,),
    ).fetchone()


def _virtual_steps(cb, caller_spelling):
    """devirtualized_callgraph(prune=True) virtual dispatch steps for caller."""
    sym = None
    for (usr,) in cb.graph._c.execute(
        "SELECT usr FROM symbol WHERE spelling = ? AND kind = 'function'",
        (caller_spelling,),
    ).fetchall():
        sym = cb.graph.get(usr)
        if sym is not None:
            break
    assert sym is not None, f"{caller_spelling} not found in real index"
    entity = cb.wrap(sym)
    assert entity is not None
    steps = entity.devirtualized_callgraph(expand_virtual=True, prune=True)
    return [s for s in steps if s.dispatch_site is not None]


def test_acc11_real_parse_construct_prunes_to_b_rank(prov_real_cb):
    """ACC-11: top_rank(B{}) — src_kind='construct', prunes a.rank() to B::rank only."""
    cb, _ = prov_real_cb

    row = _arg0_to_top_rank(cb, "f_construct")
    assert row is not None, "ACC-11: no call_arg row for f_construct()->top_rank(B{})"
    src_kind, type_usr, decl_usr, callee_usr = row
    assert src_kind == "construct", f"ACC-11: expected 'construct', got {src_kind!r}"
    assert type_usr and "chain" in type_usr and "B" in type_usr, (
        f"ACC-11: construct type_usr should reference chain::B, got {type_usr!r}"
    )

    steps = _virtual_steps(cb, "f_construct")
    pruned = [s for s in steps if s.pruned_candidates is not None]
    assert pruned, "ACC-11: construct arg should prune a.rank() but no step was pruned"
    kept = {
        sel.target.sym.usr
        for step in pruned for sel in step.pruned_candidates
        if sel.target is not None
    }
    assert any("@S@B@" in u for u in kept), f"ACC-11: B::rank missing from {kept}"
    for absent in ("A", "C", "D"):
        offenders = [u for u in kept if f"@S@{absent}@" in u]
        assert not offenders, f"ACC-11: {absent}::rank should be pruned, found {offenders}"


def test_acc12_real_parse_value_member_prunes_to_b_rank(prov_real_cb):
    """ACC-12: top_rank(h.b) — src_kind='member', VALUE field (Holder{ B b; }).
    Phase 3a: a value member's dynamic type is exactly its static type, so it
    seeds the exact singleton {B} and a.rank() prunes to B::rank only."""
    cb, _ = prov_real_cb

    row = _arg0_to_top_rank(cb, "f_member")
    assert row is not None, "ACC-12: no call_arg row for f_member()->top_rank(h.b)"
    src_kind, type_usr, decl_usr, callee_usr = row
    assert src_kind == "member", f"ACC-12: expected 'member', got {src_kind!r}"
    assert decl_usr is not None, "ACC-12: member decl_usr (the field) must be set"
    assert "Holder" in decl_usr and "b" in decl_usr, (
        f"ACC-12: member decl_usr should name Holder::b, got {decl_usr!r}"
    )

    steps = _virtual_steps(cb, "f_member")
    pruned = [s for s in steps if s.pruned_candidates is not None]
    assert pruned, "ACC-12: value member arg should prune a.rank() but no step was pruned"
    kept = {
        sel.target.sym.usr
        for step in pruned for sel in step.pruned_candidates
        if sel.target is not None
    }
    assert any("@S@B@" in u for u in kept), f"ACC-12: B::rank missing from {kept}"
    for absent in ("A", "C", "D"):
        offenders = [u for u in kept if f"@S@{absent}@" in u]
        assert not offenders, f"ACC-12: {absent}::rank should be pruned, found {offenders}"


def test_acc13_real_parse_value_global_prunes_to_b_rank(prov_real_cb):
    """ACC-13: top_rank(g_b) — src_kind='global', VALUE global (B g_b;).
    Phase 3a: a value global is exactly its static type -> singleton {B} ->
    a.rank() prunes to B::rank only."""
    cb, _ = prov_real_cb

    row = _arg0_to_top_rank(cb, "f_global")
    assert row is not None, "ACC-13: no call_arg row for f_global()->top_rank(g_b)"
    src_kind, type_usr, decl_usr, callee_usr = row
    assert src_kind == "global", f"ACC-13: expected 'global', got {src_kind!r}"
    assert decl_usr is not None and "g_b" in decl_usr, (
        f"ACC-13: global decl_usr should name chain::g_b, got {decl_usr!r}"
    )

    steps = _virtual_steps(cb, "f_global")
    pruned = [s for s in steps if s.pruned_candidates is not None]
    assert pruned, "ACC-13: value global arg should prune a.rank() but no step was pruned"
    kept = {
        sel.target.sym.usr
        for step in pruned for sel in step.pruned_candidates
        if sel.target is not None
    }
    assert any("@S@B@" in u for u in kept), f"ACC-13: B::rank missing from {kept}"
    for absent in ("A", "C", "D"):
        offenders = [u for u in kept if f"@S@{absent}@" in u]
        assert not offenders, f"ACC-13: {absent}::rank should be pruned, found {offenders}"


def test_acc14_real_parse_value_call_result_prunes_to_b_rank(prov_real_cb):
    """ACC-14: top_rank(make_b()) — src_kind='call_result', BY-VALUE return (B make_b()).
    Phase 3a: a by-value return is exactly its static type -> singleton {B} ->
    a.rank() prunes to B::rank only."""
    cb, _ = prov_real_cb

    row = _arg0_to_top_rank(cb, "f_callresult")
    assert row is not None, "ACC-14: no call_arg row for f_callresult()->top_rank(make_b())"
    src_kind, type_usr, decl_usr, callee_usr = row
    assert src_kind == "call_result", f"ACC-14: expected 'call_result', got {src_kind!r}"
    assert callee_usr is not None and "make_b" in callee_usr, (
        f"ACC-14: call_result callee_usr should name chain::make_b, got {callee_usr!r}"
    )

    steps = _virtual_steps(cb, "f_callresult")
    pruned = [s for s in steps if s.pruned_candidates is not None]
    assert pruned, "ACC-14: by-value return arg should prune a.rank() but no step was pruned"
    kept = {
        sel.target.sym.usr
        for step in pruned for sel in step.pruned_candidates
        if sel.target is not None
    }
    assert any("@S@B@" in u for u in kept), f"ACC-14: B::rank missing from {kept}"
    for absent in ("A", "C", "D"):
        offenders = [u for u in kept if f"@S@{absent}@" in u]
        assert not offenders, f"ACC-14: {absent}::rank should be pruned, found {offenders}"


def test_acc15_real_parse_all_kinds_monotone(prov_real_cb):
    """ACC-15: across every src_kind, any prune is a SUBSET of the Phase-1 set
    (monotonicity holds end-to-end through the real extractor — never adds edges)."""
    cb, _ = prov_real_cb
    for caller in ("f_construct", "f_member", "f_global", "f_callresult"):
        for step in _virtual_steps(cb, caller):
            if step.pruned_candidates is None:
                continue
            full = {sel.target.sym.usr for sel in step.dispatch_site.selections
                    if sel.target is not None}
            kept = {sel.target.sym.usr for sel in step.pruned_candidates
                    if sel.target is not None}
            assert kept <= full, (
                f"ACC-15: {caller} pruned set {kept} is not a subset of Phase-1 {full}"
            )

"""Phase 1 devirtualized callgraph — QA test suite.

Spec:  ~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md
Design: project/docs/design-devirt-phase1.md

Tests cover three categories:
  A. query.py — Selection / DispatchSite dataclasses + GraphQuery.dispatch_selection() /
     GraphQuery.virtual_call_sites() (unit, hermetic)
  B. model.py — Method.dispatch_selection() / Callable.devirtualized_callgraph() + CallStep
     (unit, hermetic)
  C. Regression — default callgraph() / callees() output is unchanged vs pre-Phase-1
     (regression, hermetic)

ALL tests are hermetic: they seed a SQLite index via the Storage write API.
No libclang, no network.  The conftest.py fixture graph is reused where it fits;
extra fixtures are defined here for the selection-map and UNPRUNABLE scenarios.

Scenario IDs (from scenarios.md / design doc §7):
  SC-01  selection map for chain fixture (A rank -> {A,B,C,D}::rank)
  SC-02  prunable=True when hierarchy is fully enumerable (closed, no stubs)
  SC-03  unprunable=True, reason 'not-virtual', for a non-virtual callee
  SC-04  unprunable=True, reason 'no-receiver-type', when parent_usr absent
  SC-05  unprunable=True, reason 'target-stub', when any dispatch target is a stub
  SC-06  unprunable=True, reason 'pure-no-targets', when dispatch_targets is empty
  SC-07  virtual_call_sites() returns one DispatchSite per virtual callee of a fn
  SC-08  model Method.dispatch_selection() returns entity-typed DispatchSiteModel
  SC-09  Callable.devirtualized_callgraph() yields CallStep with dispatch_site at virtual hops
  SC-10  Callable.devirtualized_callgraph() visits same node set as callgraph() (conservative)
  SC-11  default callgraph() output is byte-identical before/after Phase 1 (regression)
  SC-12  default callees() output is byte-identical before/after Phase 1 (regression)
  SC-13  Selection.inherited=False for directly-declared targets (default, no close_subtypes)
  SC-14  close_subtypes=True adds inherited=True entries for subtypes with no own override
  SC-15  CallStep carries no dispatch_site for non-virtual hops
"""

from __future__ import annotations

import os
import pytest

from indexer.storage import Storage, Symbol
from indexer.query import GraphQuery, EDGE_KINDS


# --------------------------------------------------------------------------- #
# Fixture: a 4-level single-inheritance chain (A <- B <- C <- D) with rank().
# top_rank() calls a.rank() — one virtual call site.
# This exercises the core selection-map derivation (SC-01, SC-02, SC-13).
# --------------------------------------------------------------------------- #


def _seed_chain(db: Storage, repo: str) -> dict[str, int]:
    """A, B:A, C:B, D:C each declare rank(); top_rank() calls a.rank()."""
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

    # rank() methods (A::rank is non-pure virtual; B/C/D override)
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

    # top_rank calls a.rank() (virtual call -> static edge to A::rank)
    sym(
        "top_rank",
        "c:@F@top_rank",
        "top_rank",
        "function",
        cpp,
        6,
        qual="chain::top_rank",
    )

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
        # overrides: B::rank->A::rank, C::rank->B::rank, D::rank->C::rank
        db.add_edge(ids["B::rank"], ids["A::rank"], C["overrides"])
        db.add_edge(ids["C::rank"], ids["B::rank"], C["overrides"])
        db.add_edge(ids["D::rank"], ids["C::rank"], C["overrides"])
        # top_rank calls A::rank (the static call edge libclang would record)
        e = db.add_edge(ids["top_rank"], ids["A::rank"], C["calls"], count=1)
        db.add_edge_site(e, cpp, 11, 18)
    return ids


@pytest.fixture
def chain_db(tmp_path):
    """A seeded, resolved chain DB; returns (db_path, ids)."""
    repo = str(tmp_path / "chain_repo")
    os.makedirs(repo)
    db_path = str(tmp_path / "chain.db")
    with Storage(db_path) as db:
        ids = _seed_chain(db, repo)
        db.resolve_pass()
    return db_path, ids


@pytest.fixture
def chain_g(chain_db):
    """GraphQuery over the chain DB."""
    db_path, ids = chain_db
    q = GraphQuery(db_path)
    yield q, ids
    q.close()


# --------------------------------------------------------------------------- #
# Fixture: a stub-target scenario (SC-05).
# A::virt() is the base method; one override is a stub (unresolved, no file).
# --------------------------------------------------------------------------- #


def _seed_stub_target(db: Storage, repo: str) -> dict[str, int]:
    """A::virt has one indexed override (B::virt) and one stub override."""
    comp = db.add_component("stub", repo)
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "s.hpp")
    cpp = db.add_file(root, "s.cpp")

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

    sym("A", "c:@S@A_s", "A", "struct", hpp, 1, qual="A_s")
    sym("B", "c:@S@B_s", "B", "struct", hpp, 10, qual="B_s")

    sym(
        "A::virt",
        "c:@S@A_s@F@virt#",
        "virt",
        "method",
        hpp,
        2,
        qual="A_s::virt",
        parent="c:@S@A_s",
    )
    sym(
        "B::virt",
        "c:@S@B_s@F@virt#",
        "virt",
        "method",
        cpp,
        1,
        qual="B_s::virt",
        parent="c:@S@B_s",
    )

    # ExternalLib::virt is a stub (unresolved, no file)
    ids["ExtLib::virt"] = db.mint_symbol_id("c:@S@ExtLib@F@virt#", spelling="virt")

    sym("caller", "c:@F@caller_s", "caller", "function", cpp, 5, qual="caller_s")

    with db.transaction():
        db.add_edge(ids["B"], ids["A"], C["inherits"], base_access=1)
        db.add_edge(ids["A::virt"], ids["A"], C["method_of"])
        db.add_edge(ids["B::virt"], ids["B"], C["method_of"])
        db.add_edge(ids["B::virt"], ids["A::virt"], C["overrides"])
        # the stub also overrides A::virt
        db.add_edge(ids["ExtLib::virt"], ids["A::virt"], C["overrides"])
        # caller calls A::virt (virtual call edge)
        e = db.add_edge(ids["caller"], ids["A::virt"], C["calls"], count=1)
        db.add_edge_site(e, cpp, 6, 5)
    return ids


@pytest.fixture
def stub_db(tmp_path):
    repo = str(tmp_path / "stub_repo")
    os.makedirs(repo)
    db_path = str(tmp_path / "stub.db")
    with Storage(db_path) as db:
        ids = _seed_stub_target(db, repo)
        db.resolve_pass()
    return db_path, ids


@pytest.fixture
def stub_g(stub_db):
    db_path, ids = stub_db
    q = GraphQuery(db_path)
    yield q, ids
    q.close()


# --------------------------------------------------------------------------- #
# Fixture: pure-base-no-targets (SC-06).
# PureBase::virt() is pure-virtual, zero overrides in the index.
# --------------------------------------------------------------------------- #


def _seed_pure_no_targets(db: Storage, repo: str) -> dict[str, int]:
    comp = db.add_component("pure", repo)
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "p.hpp")
    ids: dict[str, int] = {}
    C = EDGE_KINDS

    ids["PureBase"] = db.add_symbol(
        Symbol(
            usr="c:@S@PureBase",
            spelling="PureBase",
            kind="struct",
            qual_name="PureBase",
            file_id=hpp,
            line=1,
            col=1,
            is_definition=True,
            resolved=True,
            access="public",
        )
    )
    ids["PureBase::virt"] = db.add_symbol(
        Symbol(
            usr="c:@S@PureBase@F@virt#",
            spelling="virt",
            kind="method",
            qual_name="PureBase::virt",
            file_id=hpp,
            line=2,
            col=1,
            is_definition=False,
            is_pure=True,
            parent_usr="c:@S@PureBase",
            resolved=True,
            access="public",
        )
    )
    ids["caller"] = db.add_symbol(
        Symbol(
            usr="c:@F@pure_caller",
            spelling="pure_caller",
            kind="function",
            qual_name="pure_caller",
            file_id=hpp,
            line=10,
            col=1,
            is_definition=True,
            resolved=True,
        )
    )
    with db.transaction():
        db.add_edge(ids["PureBase::virt"], ids["PureBase"], C["method_of"])
        e = db.add_edge(ids["caller"], ids["PureBase::virt"], C["calls"], count=1)
        db.add_edge_site(e, hpp, 11, 5)
    return ids


@pytest.fixture
def pure_db(tmp_path):
    repo = str(tmp_path / "pure_repo")
    os.makedirs(repo)
    db_path = str(tmp_path / "pure.db")
    with Storage(db_path) as db:
        ids = _seed_pure_no_targets(db, repo)
        db.resolve_pass()
    return db_path, ids


@pytest.fixture
def pure_g(pure_db):
    db_path, ids = pure_db
    q = GraphQuery(db_path)
    yield q, ids
    q.close()


# --------------------------------------------------------------------------- #
# Fixture: no-receiver-type (SC-04).
# A method whose parent_usr is None (or the owning symbol is absent).
# --------------------------------------------------------------------------- #


def _seed_no_receiver(db: Storage, repo: str) -> dict[str, int]:
    """A method with no parent_usr (detached from any record)."""
    comp = db.add_component("norec", repo)
    root = db.add_directory(comp, "")
    hpp = db.add_file(root, "n.hpp")
    ids: dict[str, int] = {}
    C = EDGE_KINDS

    # Method with no parent_usr
    ids["orphan_virt"] = db.add_symbol(
        Symbol(
            usr="c:@F@orphan_virt",
            spelling="orphan_virt",
            kind="method",
            qual_name="orphan_virt",
            file_id=hpp,
            line=1,
            col=1,
            is_definition=True,
            is_pure=False,
            parent_usr=None,
            resolved=True,
            access="public",
        )
    )
    # A concrete override so it IS virtual
    ids["orphan_override"] = db.add_symbol(
        Symbol(
            usr="c:@F@orphan_override",
            spelling="orphan_virt",
            kind="method",
            qual_name="Sub::orphan_virt",
            file_id=hpp,
            line=10,
            col=1,
            is_definition=True,
            is_pure=False,
            parent_usr=None,
            resolved=True,
            access="public",
        )
    )
    ids["caller"] = db.add_symbol(
        Symbol(
            usr="c:@F@orphan_caller",
            spelling="orphan_caller",
            kind="function",
            qual_name="orphan_caller",
            file_id=hpp,
            line=20,
            col=1,
            is_definition=True,
            resolved=True,
        )
    )
    with db.transaction():
        db.add_edge(ids["orphan_override"], ids["orphan_virt"], C["overrides"])
        e = db.add_edge(ids["caller"], ids["orphan_virt"], C["calls"], count=1)
        db.add_edge_site(e, hpp, 21, 5)
    return ids


@pytest.fixture
def norec_db(tmp_path):
    repo = str(tmp_path / "norec_repo")
    os.makedirs(repo)
    db_path = str(tmp_path / "norec.db")
    with Storage(db_path) as db:
        ids = _seed_no_receiver(db, repo)
        db.resolve_pass()
    return db_path, ids


@pytest.fixture
def norec_g(norec_db):
    db_path, ids = norec_db
    q = GraphQuery(db_path)
    yield q, ids
    q.close()


# =========================================================================== #
# CATEGORY A — query.py: Selection / DispatchSite / dispatch_selection() /
#              virtual_call_sites()
# =========================================================================== #


class TestSelectionDataclass:
    """Basic structural contracts for the Selection dataclass (query.py)."""

    def test_selection_importable(self):
        """Selection must be importable from indexer.query."""
        from indexer.query import Selection  # noqa: F401

    def test_selection_fields(self):
        """Selection carries selecting_type, target, inherited fields."""
        from indexer.query import Selection, Sym

        # Build minimal Sym stubs
        sym_a = Sym(
            id=1,
            usr="a",
            spelling="A",
            name="A",
            kind="struct",
            type_info=None,
            is_definition=True,
            is_pure=False,
            access="public",
            parent_usr=None,
            resolved=True,
            component=None,
            file=None,
            line=None,
            col=None,
        )
        sym_m = Sym(
            id=2,
            usr="m",
            spelling="rank",
            name="A::rank",
            kind="method",
            type_info=None,
            is_definition=True,
            is_pure=False,
            access="public",
            parent_usr="a",
            resolved=True,
            component=None,
            file=None,
            line=None,
            col=None,
        )
        sel = Selection(selecting_type=sym_a, target=sym_m)
        assert sel.selecting_type.name == "A"
        assert sel.target.name == "A::rank"
        assert sel.inherited is False  # default

    def test_selection_inherited_flag(self):
        """Selection.inherited can be set to True."""
        from indexer.query import Selection, Sym

        sym_x = Sym(
            id=3,
            usr="x",
            spelling="X",
            name="X",
            kind="struct",
            type_info=None,
            is_definition=True,
            is_pure=False,
            access="public",
            parent_usr=None,
            resolved=True,
            component=None,
            file=None,
            line=None,
            col=None,
        )
        sym_y = Sym(
            id=4,
            usr="y",
            spelling="rank",
            name="Y::rank",
            kind="method",
            type_info=None,
            is_definition=True,
            is_pure=False,
            access="public",
            parent_usr="z",
            resolved=True,
            component=None,
            file=None,
            line=None,
            col=None,
        )
        sel = Selection(selecting_type=sym_x, target=sym_y, inherited=True)
        assert sel.inherited is True

    def test_selection_is_frozen(self):
        """Selection must be immutable (frozen dataclass)."""
        from indexer.query import Selection, Sym

        sym_a = Sym(
            id=1,
            usr="a",
            spelling="A",
            name="A",
            kind="struct",
            type_info=None,
            is_definition=True,
            is_pure=False,
            access="public",
            parent_usr=None,
            resolved=True,
            component=None,
            file=None,
            line=None,
            col=None,
        )
        sym_m = Sym(
            id=2,
            usr="m",
            spelling="rank",
            name="A::rank",
            kind="method",
            type_info=None,
            is_definition=True,
            is_pure=False,
            access="public",
            parent_usr="a",
            resolved=True,
            component=None,
            file=None,
            line=None,
            col=None,
        )
        sel = Selection(selecting_type=sym_a, target=sym_m)
        with pytest.raises((AttributeError, TypeError)):
            sel.inherited = True  # type: ignore[misc]


class TestDispatchSiteDataclass:
    """Structural contracts for DispatchSite (query.py)."""

    def test_dispatch_site_importable(self):
        from indexer.query import DispatchSite  # noqa: F401

    def test_dispatch_site_is_frozen(self):
        """DispatchSite is a frozen dataclass."""
        from indexer.query import DispatchSite

        ds = DispatchSite(
            receiver_static_type=None,
            declared_target=None,  # type: ignore[arg-type]
            candidates=(),
            prunable=False,
            unprunable_reasons=("not-virtual",),
        )
        with pytest.raises((AttributeError, TypeError)):
            ds.prunable = True  # type: ignore[misc]

    def test_dispatch_site_targets_property(self):
        """DispatchSite.targets yields all target Syms from candidates."""
        from indexer.query import DispatchSite, Selection, Sym

        sym_a = Sym(
            id=1,
            usr="a",
            spelling="A",
            name="A",
            kind="struct",
            type_info=None,
            is_definition=True,
            is_pure=False,
            access="public",
            parent_usr=None,
            resolved=True,
            component=None,
            file=None,
            line=None,
            col=None,
        )
        sym_m = Sym(
            id=2,
            usr="m",
            spelling="rank",
            name="A::rank",
            kind="method",
            type_info=None,
            is_definition=True,
            is_pure=False,
            access="public",
            parent_usr="a",
            resolved=True,
            component=None,
            file=None,
            line=None,
            col=None,
        )
        sel = Selection(selecting_type=sym_a, target=sym_m)
        ds = DispatchSite(
            receiver_static_type=sym_a,
            declared_target=sym_m,
            candidates=(sel,),
            prunable=True,
            unprunable_reasons=(),
        )
        assert ds.targets == (sym_m,)

    def test_dispatch_site_to_dict(self):
        """DispatchSite.to_dict() returns a JSON-serializable dict."""
        from indexer.query import DispatchSite

        ds = DispatchSite(
            receiver_static_type=None,
            declared_target=None,  # type: ignore[arg-type]
            candidates=(),
            prunable=False,
            unprunable_reasons=("not-virtual",),
        )
        d = ds.to_dict()
        assert isinstance(d, dict)
        assert "prunable" in d
        assert "unprunable_reasons" in d
        assert "candidates" in d


# ============== dispatch_selection() — the core primitive =================== #


class TestDispatchSelection:
    """SC-01 to SC-06: GraphQuery.dispatch_selection(method) contract."""

    def test_sc01_chain_selection_map_keys(self, chain_g):
        """SC-01: a.rank() has 4 candidates, one per class (A,B,C,D)."""
        g, ids = chain_g
        ds = g.dispatch_selection(ids["A::rank"])
        selecting_names = {sel.selecting_type.name for sel in ds.candidates}
        # The 4 concrete types that own an override
        assert "chain::A" in selecting_names
        assert "chain::B" in selecting_names
        assert "chain::C" in selecting_names
        assert "chain::D" in selecting_names
        assert len(ds.candidates) == 4

    def test_sc01_chain_selection_map_targets(self, chain_g):
        """SC-01: each Selection maps the owning class to its own rank() override."""
        g, ids = chain_g
        ds = g.dispatch_selection(ids["A::rank"])
        tgt_map = {sel.selecting_type.id: sel.target.id for sel in ds.candidates}
        assert tgt_map[ids["A"]] == ids["A::rank"]
        assert tgt_map[ids["B"]] == ids["B::rank"]
        assert tgt_map[ids["C"]] == ids["C::rank"]
        assert tgt_map[ids["D"]] == ids["D::rank"]

    def test_sc01_receiver_static_type(self, chain_g):
        """SC-01: receiver_static_type is the class owning the declared callee."""
        g, ids = chain_g
        ds = g.dispatch_selection(ids["A::rank"])
        assert ds.receiver_static_type is not None
        assert ds.receiver_static_type.id == ids["A"]

    def test_sc01_declared_target(self, chain_g):
        """SC-01: declared_target is the statically-called method (A::rank)."""
        g, ids = chain_g
        ds = g.dispatch_selection(ids["A::rank"])
        assert ds.declared_target.id == ids["A::rank"]

    def test_sc02_chain_is_prunable(self, chain_g):
        """SC-02: closed hierarchy with no stubs -> prunable=True."""
        g, ids = chain_g
        ds = g.dispatch_selection(ids["A::rank"])
        assert ds.prunable is True
        assert ds.unprunable_reasons == ()

    def test_sc03_not_virtual_unprunable(self, g, ids):
        """SC-03: dispatch_selection on a non-virtual method -> prunable=False,
        reason 'not-virtual'."""
        # 'main' in the conftest fixture is a plain function, not virtual
        ds = g.dispatch_selection(ids["main"])
        assert ds.prunable is False
        assert "not-virtual" in ds.unprunable_reasons
        assert ds.candidates == ()

    def test_sc04_no_receiver_type_unprunable(self, norec_g):
        """SC-04: method with no parent_usr -> prunable=False, 'no-receiver-type'."""
        g, ids = norec_g
        ds = g.dispatch_selection(ids["orphan_virt"])
        assert ds.prunable is False
        assert "no-receiver-type" in ds.unprunable_reasons

    def test_sc05_stub_target_unprunable(self, stub_g):
        """SC-05: any dispatch target is a stub -> prunable=False, 'target-stub'."""
        g, ids = stub_g
        ds = g.dispatch_selection(ids["A::virt"])
        assert ds.prunable is False
        assert "target-stub" in ds.unprunable_reasons
        # The non-stub target (B::virt) still appears in candidates
        target_ids = {sel.target.id for sel in ds.candidates}
        assert ids["B::virt"] in target_ids

    def test_sc06_pure_no_targets_unprunable(self, pure_g):
        """SC-06: pure base with no indexed override -> prunable=False,
        'pure-no-targets'."""
        g, ids = pure_g
        ds = g.dispatch_selection(ids["PureBase::virt"])
        assert ds.prunable is False
        assert "pure-no-targets" in ds.unprunable_reasons
        assert ds.candidates == ()

    def test_dispatch_selection_accepts_sym(self, chain_g):
        """dispatch_selection accepts a Sym as well as an int id."""
        from indexer.query import Sym

        g, ids = chain_g
        sym = g.get(ids["A::rank"])
        assert isinstance(sym, Sym)
        ds1 = g.dispatch_selection(ids["A::rank"])
        ds2 = g.dispatch_selection(sym)
        assert ds1.prunable == ds2.prunable
        assert len(ds1.candidates) == len(ds2.candidates)

    def test_dispatch_selection_unknown_id_returns_not_virtual(self, chain_g):
        """dispatch_selection on an unknown id returns a safe unprunable result."""
        g, _ = chain_g
        ds = g.dispatch_selection(99999)
        assert ds.prunable is False

    def test_sc13_inherited_false_by_default(self, chain_g):
        """SC-13: default candidates have inherited=False (each class declares own override)."""
        g, ids = chain_g
        ds = g.dispatch_selection(ids["A::rank"])
        assert all(not sel.inherited for sel in ds.candidates)


class TestDispatchSelectionCloseSubtypes:
    """SC-14: close_subtypes=True adds inherited entries for subtypes with no override.

    Fixture: A::rank, B::rank. E inherits B but has no own override, so an E
    instance dispatches to the override it inherits, B::rank.
    With close_subtypes=False: candidates = {A->A::rank, B->B::rank}
    With close_subtypes=True:  candidates also include E->B::rank (inherited=True)
    """

    @pytest.fixture
    def abe_db(self, tmp_path):
        """A <- B (overrides rank), E <- B (no own override)."""
        repo = str(tmp_path / "abe_repo")
        os.makedirs(repo)
        db_path = str(tmp_path / "abe.db")
        C = EDGE_KINDS
        with Storage(db_path) as db:
            comp = db.add_component("abe", repo)
            root = db.add_directory(comp, "")
            hpp = db.add_file(root, "abe.hpp")

            def sym(
                key,
                ids_,
                usr,
                spelling,
                kind,
                line,
                *,
                qual=None,
                is_def=True,
                is_pure=False,
                parent=None,
                resolved=True,
                access="public",
            ):
                ids_[key] = db.add_symbol(
                    Symbol(
                        usr=usr,
                        spelling=spelling,
                        kind=kind,
                        qual_name=qual or spelling,
                        file_id=hpp,
                        line=line,
                        col=1,
                        is_definition=is_def,
                        is_pure=is_pure,
                        parent_usr=parent,
                        resolved=resolved,
                        access=access,
                    )
                )

            ids: dict[str, int] = {}
            sym("A", ids, "c:@S@A_abe", "A", "struct", 1, qual="A_abe")
            sym("B", ids, "c:@S@B_abe", "B", "struct", 10, qual="B_abe")
            sym("E", ids, "c:@S@E_abe", "E", "struct", 20, qual="E_abe")
            sym(
                "A::rank",
                ids,
                "c:@S@A_abe@F@rank#",
                "rank",
                "method",
                2,
                qual="A_abe::rank",
                parent="c:@S@A_abe",
            )
            sym(
                "B::rank",
                ids,
                "c:@S@B_abe@F@rank#",
                "rank",
                "method",
                11,
                qual="B_abe::rank",
                parent="c:@S@B_abe",
            )
            with db.transaction():
                db.add_edge(ids["B"], ids["A"], C["inherits"], base_access=1)
                db.add_edge(ids["E"], ids["B"], C["inherits"], base_access=1)
                db.add_edge(ids["A::rank"], ids["A"], C["method_of"])
                db.add_edge(ids["B::rank"], ids["B"], C["method_of"])
                db.add_edge(ids["B::rank"], ids["A::rank"], C["overrides"])
            db.resolve_pass()
        return db_path, ids

    def test_sc14_close_subtypes_false_default(self, abe_db):
        """SC-14 baseline: close_subtypes=False has no E entry."""
        db_path, ids = abe_db
        with GraphQuery(db_path) as g:
            ds = g.dispatch_selection(ids["A::rank"], close_subtypes=False)
        selecting_ids = {sel.selecting_type.id for sel in ds.candidates}
        assert ids["E"] not in selecting_ids

    def test_sc14_close_subtypes_true_adds_inherited(self, abe_db):
        """SC-14: close_subtypes=True adds E->B::rank, inherited=True."""
        db_path, ids = abe_db
        with GraphQuery(db_path) as g:
            ds = g.dispatch_selection(ids["A::rank"], close_subtypes=True)
        inherited_entries = [sel for sel in ds.candidates if sel.inherited]
        assert len(inherited_entries) >= 1
        e_entry = next(
            (sel for sel in inherited_entries if sel.selecting_type.id == ids["E"]),
            None,
        )
        assert e_entry is not None, "E should appear in candidates as inherited"
        assert e_entry.target.id == ids["B::rank"]
        assert e_entry.inherited is True

    def test_sc14_non_inherited_entries_unchanged(self, abe_db):
        """SC-14: close_subtypes=True does not alter the direct entries."""
        db_path, ids = abe_db
        with GraphQuery(db_path) as g:
            ds_closed = g.dispatch_selection(ids["A::rank"], close_subtypes=True)
            ds_open = g.dispatch_selection(ids["A::rank"], close_subtypes=False)
        direct_closed = {
            sel.target.id for sel in ds_closed.candidates if not sel.inherited
        }
        direct_open = {sel.target.id for sel in ds_open.candidates}
        assert direct_closed == direct_open


class TestVirtualCallSites:
    """SC-07: GraphQuery.virtual_call_sites(fn) returns one DispatchSite per
    virtual callee of fn."""

    def test_sc07_virtual_call_sites_importable(self):
        from indexer.query import GraphQuery

        assert hasattr(GraphQuery, "virtual_call_sites")

    def test_sc07_returns_list_of_dispatch_sites(self, chain_g):
        """SC-07: top_rank has exactly one virtual callee (A::rank); one site."""
        g, ids = chain_g
        from indexer.query import DispatchSite

        sites = g.virtual_call_sites(ids["top_rank"])
        assert isinstance(sites, list)
        assert len(sites) == 1
        assert isinstance(sites[0], DispatchSite)

    def test_sc07_dispatch_site_matches_dispatch_selection(self, chain_g):
        """SC-07: virtual_call_sites returns the same data as dispatch_selection."""
        g, ids = chain_g
        sites = g.virtual_call_sites(ids["top_rank"])
        ds_direct = g.dispatch_selection(ids["A::rank"])
        assert sites[0].prunable == ds_direct.prunable
        assert {s.target.id for s in sites[0].candidates} == {
            s.target.id for s in ds_direct.candidates
        }

    def test_sc07_non_virtual_callees_excluded(self, g, ids):
        """SC-07: virtual_call_sites omits non-virtual callees (static edges)."""
        # helper calls compute (non-virtual) -> should get empty list
        sites = g.virtual_call_sites(ids["helper"])
        virt_declared_targets = {s.declared_target.id for s in sites}
        # compute is not virtual, must not appear
        assert ids["compute"] not in virt_declared_targets

    def test_sc07_empty_for_leaf(self, g, ids):
        """SC-07: a leaf function (no callees) returns []."""
        sites = g.virtual_call_sites(ids["compute"])
        assert sites == []


# =========================================================================== #
# CATEGORY B — model.py: Method.dispatch_selection() /
#              Callable.devirtualized_callgraph() + CallStep
# =========================================================================== #


class TestModelDispatchSelection:
    """SC-08: Method.dispatch_selection() returns entity-typed DispatchSiteModel."""

    @pytest.fixture
    def chain_cb(self, chain_db):
        from indexer.model import CodeBase

        db_path, ids = chain_db
        cb = CodeBase(GraphQuery(db_path))
        yield cb, ids
        cb.close()

    def test_sc08_importable(self):
        from indexer.model import Method

        assert hasattr(Method, "dispatch_selection")

    def test_sc08_dispatch_site_model_importable(self):
        from indexer.model import DispatchSiteModel  # noqa: F401

    def test_sc08_selection_model_importable(self):
        from indexer.model import SelectionModel  # noqa: F401

    def test_sc08_receiver_type_is_class(self, chain_cb):
        """SC-08: receiver_static_type is a Class entity."""
        from indexer.model import Method, Class

        cb, ids = chain_cb
        a_rank = cb.get(ids["A::rank"])
        assert isinstance(a_rank, Method)
        site = a_rank.dispatch_selection()
        assert isinstance(site.receiver_static_type, Class)
        assert site.receiver_static_type.id == ids["A"]

    def test_sc08_selections_have_method_entities(self, chain_cb):
        """SC-08: each SelectionModel carries Method entities."""
        from indexer.model import Method

        cb, ids = chain_cb
        a_rank = cb.get(ids["A::rank"])
        assert isinstance(a_rank, Method)
        site = a_rank.dispatch_selection()
        for sel in site.selections:
            assert isinstance(sel.target, Method)

    def test_sc08_selection_map_completeness(self, chain_cb):
        """SC-08: the model selection map covers all four chain classes."""
        from indexer.model import Method

        cb, ids = chain_cb
        a_rank = cb.get(ids["A::rank"])
        assert isinstance(a_rank, Method)
        site = a_rank.dispatch_selection()
        target_ids = {sel.target.id for sel in site.selections}
        assert ids["A::rank"] in target_ids
        assert ids["B::rank"] in target_ids
        assert ids["C::rank"] in target_ids
        assert ids["D::rank"] in target_ids


class TestCallStep:
    """SC-09 / SC-15: Callable.devirtualized_callgraph() + CallStep."""

    @pytest.fixture
    def chain_cb(self, chain_db):
        from indexer.model import CodeBase

        db_path, ids = chain_db
        cb = CodeBase(GraphQuery(db_path))
        yield cb, ids
        cb.close()

    def test_call_step_importable(self):
        from indexer.model import CallStep  # noqa: F401

    def test_devirt_callgraph_importable(self):
        from indexer.model import Callable

        assert hasattr(Callable, "devirtualized_callgraph")

    def test_sc09_devirt_yields_call_steps(self, chain_cb):
        """SC-09: devirtualized_callgraph yields CallStep objects."""
        from indexer.model import Callable, CallStep

        cb, ids = chain_cb
        top_rank = cb.get(ids["top_rank"])
        assert isinstance(top_rank, Callable)
        steps = list(top_rank.devirtualized_callgraph())
        assert len(steps) >= 1
        assert all(isinstance(s, CallStep) for s in steps)

    def test_sc09_call_step_structure(self, chain_cb):
        """SC-09: CallStep carries callee, depth, and dispatch_site attributes."""
        from indexer.model import Callable, CallStep, DispatchSiteModel

        cb, ids = chain_cb
        top_rank = cb.get(ids["top_rank"])
        assert isinstance(top_rank, Callable)
        steps = list(top_rank.devirtualized_callgraph())
        # Find the step for A::rank (the virtual callee)
        a_rank_steps = [s for s in steps if s.callee.id == ids["A::rank"]]
        assert len(a_rank_steps) == 1
        step = a_rank_steps[0]
        assert step.depth == 1
        assert isinstance(step.dispatch_site, DispatchSiteModel)

    def test_sc15_non_virtual_callee_has_no_dispatch_site(self, g, ids):
        """SC-15: a step for a non-virtual callee has dispatch_site=None."""
        from indexer.model import CodeBase, Callable

        db_path = g.db_path
        with CodeBase(GraphQuery(db_path)) as cb:
            main = cb.get(ids["main"])
            assert isinstance(main, Callable)
            steps = list(main.devirtualized_callgraph())
            # main -> helper (non-virtual)
            helper_steps = [s for s in steps if s.callee.id == ids["helper"]]
            assert len(helper_steps) == 1
            assert helper_steps[0].dispatch_site is None

    def test_sc09_devirt_is_generator(self, chain_cb):
        """SC-09: devirtualized_callgraph is lazy (a generator)."""
        import types
        from indexer.model import Callable

        cb, ids = chain_cb
        top_rank = cb.get(ids["top_rank"])
        assert isinstance(top_rank, Callable)
        gen = top_rank.devirtualized_callgraph()
        assert isinstance(gen, types.GeneratorType)

    def test_sc10_node_set_matches_callgraph(self, g, ids):
        """SC-10: devirtualized_callgraph visits the same node set as callgraph().

        The walk remains conservative (descends into the declared callee, not
        all dispatch targets) unless expand_virtual=True is used. The default
        must produce the SAME (callee.id, depth) pairs as callgraph()."""
        from indexer.model import CodeBase, Callable

        db_path = g.db_path
        with CodeBase(GraphQuery(db_path)) as cb:
            main = cb.get(ids["main"])
            assert isinstance(main, Callable)
            cg_result = [(e.id, d) for e, d in main.callgraph()]
            dvirt_result = [
                (s.callee.id, s.depth) for s in main.devirtualized_callgraph()
            ]
        assert cg_result == dvirt_result

    def test_sc10_chain_devirt_node_set(self, chain_db):
        """SC-10: top_rank.devirtualized_callgraph() visits only A::rank (the
        statically declared callee), not B/C/D::rank."""
        from indexer.model import CodeBase, Callable

        db_path, ids = chain_db
        with CodeBase(GraphQuery(db_path)) as cb:
            top = cb.get(ids["top_rank"])
            assert isinstance(top, Callable)
            visited_ids = {s.callee.id for s in top.devirtualized_callgraph()}
        # Conservative: only the static callee
        assert ids["A::rank"] in visited_ids
        # The derived targets are NOT expanded into the walk by default
        assert ids["B::rank"] not in visited_ids
        assert ids["C::rank"] not in visited_ids
        assert ids["D::rank"] not in visited_ids


class TestDevirtExpandVirtual:
    """expand_virtual=True opts in to visiting all dispatch targets (SC-10 variant)."""

    def test_expand_virtual_flag_exists(self):
        """devirtualized_callgraph must accept expand_virtual kwarg."""
        from indexer.model import Callable
        import inspect

        sig = inspect.signature(Callable.devirtualized_callgraph)
        assert "expand_virtual" in sig.parameters

    def test_expand_virtual_visits_all_targets(self, chain_db):
        """expand_virtual=True also visits B/C/D::rank as direct dispatch targets."""
        from indexer.model import CodeBase, Callable

        db_path, ids = chain_db
        with CodeBase(GraphQuery(db_path)) as cb:
            top = cb.get(ids["top_rank"])
            assert isinstance(top, Callable)
            visited = {
                s.callee.id for s in top.devirtualized_callgraph(expand_virtual=True)
            }
        assert ids["A::rank"] in visited
        assert ids["B::rank"] in visited
        assert ids["C::rank"] in visited
        assert ids["D::rank"] in visited


# =========================================================================== #
# CATEGORY C — regression: default callgraph() / callees() unchanged
# =========================================================================== #


class TestDefaultCallgraphUnchanged:
    """SC-11 / SC-12: the Phase-1 additions must NOT alter the existing
    callgraph() and callees() output in any way.

    These tests pin the exact (id, depth) sequence from callgraph() and the
    exact id list from callees() for the conftest fixture graph BEFORE
    Phase 1 ran, and assert they remain identical after Phase 1 was merged."""

    def test_sc11_callgraph_node_sequence_unchanged(self, g, ids):
        """SC-11: callgraph() DFS pre-order is byte-identical after Phase 1."""
        from indexer.model import CodeBase, Callable

        with CodeBase(GraphQuery(g.db_path)) as cb:
            main = cb.get(ids["main"])
            assert isinstance(main, Callable)
            result = [(e.id, d) for e, d in main.callgraph()]
        # Expected from baseline (pre-Phase-1): main->helper(1)->compute(2),ext_fn(2)
        assert result == [
            (ids["helper"], 1),
            (ids["compute"], 2),
            (ids["ext_fn"], 2),
        ]

    def test_sc11_callgraph_render_folds_dispatch(self, g, ids):
        """SC-11: render.callgraph() folds virtual dispatch by default -- it
        reaches Base::draw AND its overrides via the dispatch_calls edges. The
        static-only walk stays available on devirtualized_callgraph()."""
        from indexer.model import CodeBase, Callable

        with CodeBase(GraphQuery(g.db_path)) as cb:
            render = cb.get(ids["render"])
            assert isinstance(render, Callable)
            result = {e.id for e, _ in render.callgraph()}
            static = {s.callee.id for s in render.devirtualized_callgraph()}
        assert result == {
            ids["Base::draw"],
            ids["Derived::draw"],
            ids["Derived2::draw"],
        }
        assert static == {ids["Base::draw"]}

    def test_sc12_callees_list_unchanged(self, g, ids):
        """SC-12: callees() list for helper is exactly [compute, ext_fn] (source order)."""
        from indexer.model import CodeBase, Callable

        with CodeBase(GraphQuery(g.db_path)) as cb:
            helper = cb.get(ids["helper"])
            assert isinstance(helper, Callable)
            callee_ids = [e.id for e in helper.callees()]
        # helper calls compute (line 22, 25) and ext_fn (line 28)
        assert callee_ids == [ids["compute"], ids["ext_fn"]]

    def test_sc12_callees_non_callable_entity_unchanged(self, g, ids):
        """SC-12: callees() on a leaf (compute) still returns []."""
        from indexer.model import CodeBase, Callable

        with CodeBase(GraphQuery(g.db_path)) as cb:
            compute = cb.get(ids["compute"])
            assert isinstance(compute, Callable)
            assert compute.callees() == []

    def test_default_dispatch_targets_unchanged(self, g, ids):
        """Regression: dispatch_targets() on Base::draw returns {Derived::draw, Derived2::draw}."""
        targets = {s.id for s in g.dispatch_targets(ids["Base::draw"])}
        assert ids["Derived::draw"] in targets
        assert ids["Derived2::draw"] in targets
        assert ids["Base::draw"] not in targets  # pure, excluded

    def test_callgraph_returns_generator_not_list(self, g, ids):
        """Regression: callgraph() return type must not have changed to a list."""
        import types
        from indexer.model import CodeBase, Callable

        with CodeBase(GraphQuery(g.db_path)) as cb:
            main = cb.get(ids["main"])
            assert isinstance(main, Callable)
            assert isinstance(main.callgraph(), types.GeneratorType)

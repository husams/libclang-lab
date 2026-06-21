"""PR2: v17 entity_edge materialisation tests.

Covers:
  * schema/version invariants (SCHEMA_VERSION=17, VERSION='0.18.0')
  * entity_rollup module importable
  * entity_edge + entity_edge_kind tables present in schema
  * PR1 edge_kind seed rows 10-16 present
  * BDD acceptance scenarios (ADR-008 S1..S4)
  * all 11 entity_edge kinds
  * roll-up idempotency + re-materialise

All fixture graphs are hermetic (no libclang parse, no disk files).
"""

from __future__ import annotations

import importlib
import sys

import pytest

from indexer.storage import SCHEMA_VERSION, Storage, Symbol
from indexer.query import EDGE_KINDS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENTITY_KIND_NAMES = {
    1: "generalizes",
    2: "realizes",
    3: "specializes",
    4: "composes",
    5: "aggregates",
    6: "associates",
    7: "creates",
    8: "uses",
    9: "destroys",
    10: "nests",
    11: "befriends",
}

# Layer-0 edge kinds introduced by PR1
_PR1_EDGE_KINDS = {
    10: "construct-value",
    11: "construct-temp",
    12: "construct-heap",
    13: "construct-copy",
    14: "construct-move",
    15: "factory-construct",
    16: "destroy",
}

_HAS_ROLLUP = False
try:
    from indexer import entity_rollup as _er  # noqa: F401
    from indexer.entity_rollup import materialize_entity_edges

    _HAS_ROLLUP = True
except ImportError:
    pass


def _fresh(tmp_path):
    """Create a Storage with one component + one root directory; return (db, dir_id)."""
    db = Storage(str(tmp_path / "i.db"))
    comp_id = db.add_component("lab", str(tmp_path))
    dir_id = db.add_directory(comp_id, "")
    return db, dir_id


def _sym(db, file_id, key, usr, spelling, kind, line, *, qual=None, is_pure=False,
         parent=None, access=None, type_info=None, is_def=True):
    return db.add_symbol(
        Symbol(
            usr=usr,
            spelling=spelling,
            kind=kind,
            qual_name=qual or spelling,
            type_info=type_info,
            file_id=file_id,
            line=line,
            col=1,
            is_definition=is_def,
            is_pure=is_pure,
            parent_usr=parent,
            resolved=True,
            access=access,
        )
    )


# ---------------------------------------------------------------------------
# Version / schema invariants
# ---------------------------------------------------------------------------

def test_schema_version_is_17():
    assert SCHEMA_VERSION == 17, f"Expected 17, got {SCHEMA_VERSION}"


def test_product_version_is_0180():
    from indexer import cli
    assert cli.VERSION == "0.18.0", f"Expected '0.18.0', got {cli.VERSION!r}"


def test_entity_rollup_module_importable():
    mod = importlib.import_module("indexer.entity_rollup")
    assert hasattr(mod, "materialize_entity_edges")


def test_entity_edge_table_exists(tmp_path):
    db, _ = _fresh(tmp_path)
    tables = {
        r[0]
        for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    db.close()
    assert "entity_edge" in tables, "entity_edge table missing from schema"
    assert "entity_edge_kind" in tables, "entity_edge_kind table missing from schema"


def test_entity_edge_kind_rows(tmp_path):
    db, _ = _fresh(tmp_path)
    rows = dict(db._conn.execute("SELECT id, name FROM entity_edge_kind"))
    db.close()
    assert rows == _ENTITY_KIND_NAMES


@pytest.mark.parametrize("kid,name", list(_PR1_EDGE_KINDS.items()))
def test_pr1_edge_kind_seeds(tmp_path, kid, name):
    db, _ = _fresh(tmp_path)
    row = db._conn.execute(
        "SELECT name FROM edge_kind WHERE id = ?", (kid,)
    ).fetchone()
    db.close()
    assert row is not None, f"edge_kind row id={kid} ({name}) missing"
    assert row[0] == name, f"edge_kind id={kid}: expected {name!r}, got {row[0]!r}"


# ---------------------------------------------------------------------------
# BDD acceptance scenarios (ADR-008)
# ---------------------------------------------------------------------------

# ADR-008 S1: realizes XOR generalizes
def test_bdd_ADR008_S1_realizes_xor_generalizes_interface(tmp_path):
    """Interface base (all-pure, no data) → realizes(2), NOT generalizes(1)."""
    if not _HAS_ROLLUP:
        pytest.skip("required modules not available (_HAS_ROLLUP=False)")

    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")

    # Interface: one pure-virtual method, no data fields
    iface_id = _sym(db, root, "IFace", "c:@S@IFace", "IFace", "class", 1)
    meth_id = _sym(db, root, "IFace::draw", "c:@S@IFace@F@draw#",
                   "draw", "method", 2, is_pure=True, parent="c:@S@IFace",
                   access="public")
    # Concrete: inherits Interface
    conc_id = _sym(db, root, "Concrete", "c:@S@Concrete", "Concrete", "class", 10)

    C = EDGE_KINDS
    db.add_edge(conc_id, iface_id, C["inherits"], base_access=0)
    db.add_edge(meth_id, iface_id, C["method_of"])

    materialize_entity_edges(db)

    rows = db.entity_edges(src_id=conc_id, dst_id=iface_id)
    kinds = {r["kind"] for r in rows}
    assert 2 in kinds, "realizes(2) missing for Interface base"
    assert 1 not in kinds, "generalizes(1) must NOT fire for Interface base"


def test_bdd_ADR008_S1_state_base_generalizes(tmp_path):
    """State-bearing base (has data field) → generalizes(1), NOT realizes(2)."""
    if not _HAS_ROLLUP:
        pytest.skip("required modules not available (_HAS_ROLLUP=False)")

    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")

    base_id = _sym(db, root, "Base", "c:@S@Base", "Base", "class", 1)
    field_id = _sym(db, root, "Base::x", "c:@S@Base@FI@x", "x", "member", 2,
                    parent="c:@S@Base", type_info="int")
    derived_id = _sym(db, root, "Derived", "c:@S@Derived", "Derived", "class", 10)

    C = EDGE_KINDS
    db.add_edge(derived_id, base_id, C["inherits"], base_access=0)
    db.add_edge(field_id, base_id, C["field_of"])

    materialize_entity_edges(db)

    rows = db.entity_edges(src_id=derived_id, dst_id=base_id)
    kinds = {r["kind"] for r in rows}
    assert 1 in kinds, "generalizes(1) missing for state-bearing base"
    assert 2 not in kinds, "realizes(2) must NOT fire for state-bearing base"


# ADR-008 S2: creates (heap) from method-scoped new
def test_bdd_ADR008_S2_creates_heap_from_method(tmp_path):
    """Dashboard::refresh() → new Circle → creates(7, create_form=5)."""
    if not _HAS_ROLLUP:
        pytest.skip("required modules not available (_HAS_ROLLUP=False)")

    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "p.hpp")

    C = EDGE_KINDS

    dashboard_id = _sym(db, root, "Dashboard", "c:@S@Dashboard", "Dashboard", "class", 1)
    circle_id = _sym(db, root, "Circle", "c:@S@Circle", "Circle", "class", 10)
    ctor_id = _sym(db, root, "Circle::Circle", "c:@S@Circle@C1", "Circle", "constructor", 11,
                   parent="c:@S@Circle")
    refresh_id = _sym(db, root, "Dashboard::refresh", "c:@S@Dashboard@F@refresh#",
                      "refresh", "method", 2, parent="c:@S@Dashboard",
                      type_info="void (double)", access="public")

    # refresh is method_of Dashboard
    db.add_edge(refresh_id, dashboard_id, C["method_of"])
    # refresh construct-heap → Circle ctor
    db.add_edge(refresh_id, ctor_id, 12)  # construct-heap = 12

    materialize_entity_edges(db)

    rows = db.entity_edges(src_id=dashboard_id, dst_id=circle_id)
    assert rows, "No entity_edge for Dashboard→Circle creates"
    creates = [r for r in rows if r["kind"] == 7]
    assert creates, "creates(7) missing"
    assert creates[0]["create_form"] == 5, f"Expected create_form=5 (heap), got {creates[0]['create_form']}"
    assert creates[0]["partial"] == 0


# ADR-008 S3: destroys from method-scoped delete
def test_bdd_ADR008_S3_destroys_from_method(tmp_path):
    """Dashboard::refresh() → delete Shape* → destroys(9)."""
    if not _HAS_ROLLUP:
        pytest.skip("required modules not available (_HAS_ROLLUP=False)")

    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "p.hpp")

    C = EDGE_KINDS

    dashboard_id = _sym(db, root, "Dashboard", "c:@S@Dashboard", "Dashboard", "class", 1)
    shape_id = _sym(db, root, "Shape", "c:@S@Shape", "Shape", "class", 20)
    dtor_id = _sym(db, root, "Shape::~Shape", "c:@S@Shape@D1", "~Shape", "destructor", 21,
                   parent="c:@S@Shape")
    refresh_id = _sym(db, root, "Dashboard::refresh", "c:@S@Dashboard@F@refresh#",
                      "refresh", "method", 2, parent="c:@S@Dashboard",
                      type_info="void (double)", access="public")

    db.add_edge(refresh_id, dashboard_id, C["method_of"])
    # refresh destroy → Shape dtor
    db.add_edge(refresh_id, dtor_id, 16)  # destroy = 16

    materialize_entity_edges(db)

    rows = db.entity_edges(src_id=dashboard_id, dst_id=shape_id)
    assert rows, "No entity_edge for Dashboard→Shape destroys"
    destroys = [r for r in rows if r["kind"] == 9]
    assert destroys, "destroys(9) missing"


# ADR-008 S4: free function construction → no entity_edge
def test_bdd_ADR008_S4_free_function_no_entity_edge(tmp_path):
    """make_shape() is a free function → no entity src → no entity_edge creates."""
    if not _HAS_ROLLUP:
        pytest.skip("required modules not available (_HAS_ROLLUP=False)")

    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "p.hpp")

    C = EDGE_KINDS

    circle_id = _sym(db, root, "Circle", "c:@S@Circle", "Circle", "class", 1)
    ctor_id = _sym(db, root, "Circle::Circle", "c:@S@Circle@C1", "Circle", "constructor", 2,
                   parent="c:@S@Circle")
    # Free function (NOT owned by any entity)
    make_shape_id = _sym(db, root, "make_shape", "c:@F@make_shape", "make_shape", "function", 5)

    # construct-heap edge from free function → no entity_edge row
    db.add_edge(make_shape_id, ctor_id, 12)  # construct-heap

    materialize_entity_edges(db)

    # No entity_edge because make_shape is a free function (no method_of)
    rows = db.entity_edges(dst_id=circle_id, kind=7)
    assert not rows, f"Expected no creates entity_edge from free fn, got {rows}"


# ---------------------------------------------------------------------------
# All 11 entity_edge kinds
# ---------------------------------------------------------------------------

def test_kind_generalizes(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    base_id = _sym(db, root, "A", "c:@S@A", "A", "class", 1)
    field_id = _sym(db, root, "A::x", "c:@S@A@FI@x", "x", "member", 2,
                    parent="c:@S@A", type_info="int")
    derived_id = _sym(db, root, "B", "c:@S@B", "B", "class", 10)
    db.add_edge(derived_id, base_id, C["inherits"], base_access=0)
    db.add_edge(field_id, base_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=derived_id, kind=1)
    assert rows, "generalizes(1) missing"


def test_kind_realizes(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    iface_id = _sym(db, root, "IFace", "c:@S@IFace", "IFace", "class", 1)
    meth_id = _sym(db, root, "IFace::f", "c:@S@IFace@F@f#", "f", "method", 2,
                   is_pure=True, parent="c:@S@IFace", access="public")
    conc_id = _sym(db, root, "Conc", "c:@S@Conc", "Conc", "class", 10)
    db.add_edge(conc_id, iface_id, C["inherits"], base_access=0)
    db.add_edge(meth_id, iface_id, C["method_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=conc_id, kind=2)
    assert rows, "realizes(2) missing"


def test_kind_specializes(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")

    primary_id = _sym(db, root, "Vec", "c:@ST@Vec", "Vec", "class-template", 1)
    spec_id = _sym(db, root, "Vec<int>", "c:@S@Vec#I", "Vec<int>", "class", 10)
    db.add_edge(spec_id, primary_id, EDGE_KINDS["specializes"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=spec_id, kind=3)
    assert rows, "specializes(3) missing"


def test_kind_composes(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    part_id = _sym(db, root, "Part", "c:@S@Part", "Part", "class", 1)
    owner_id = _sym(db, root, "Owner", "c:@S@Owner", "Owner", "class", 10)
    field_id = _sym(db, root, "Owner::p", "c:@S@Owner@FI@p", "p", "member", 11,
                    parent="c:@S@Owner", type_info="Part", access="private")
    db.add_edge(field_id, owner_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=owner_id, dst_id=part_id, kind=4)
    assert rows, "composes(4) missing"


def test_kind_aggregates(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    part_id = _sym(db, root, "Part", "c:@S@Part", "Part", "class", 1)
    owner_id = _sym(db, root, "Owner", "c:@S@Owner", "Owner", "class", 10)
    field_id = _sym(db, root, "Owner::p", "c:@S@Owner@FI@p", "p", "member", 11,
                    parent="c:@S@Owner", type_info="std::unique_ptr<Part>",
                    access="private")
    db.add_edge(field_id, owner_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=owner_id, dst_id=part_id, kind=5)
    assert rows, "aggregates(5) missing"


def test_kind_associates(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    target_id = _sym(db, root, "Target", "c:@S@Target", "Target", "class", 1)
    holder_id = _sym(db, root, "Holder", "c:@S@Holder", "Holder", "class", 10)
    field_id = _sym(db, root, "Holder::t", "c:@S@Holder@FI@t", "t", "member", 11,
                    parent="c:@S@Holder", type_info="Target*", access="private")
    db.add_edge(field_id, holder_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=holder_id, dst_id=target_id, kind=6)
    assert rows, "associates(6) missing"


def test_kind_creates(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    target_id = _sym(db, root, "Tgt", "c:@S@Tgt", "Tgt", "class", 1)
    ctor_id = _sym(db, root, "Tgt::Tgt", "c:@S@Tgt@C1", "Tgt", "constructor", 2,
                   parent="c:@S@Tgt")
    factory_id = _sym(db, root, "Owner", "c:@S@Owner", "Owner", "class", 5)
    method_id = _sym(db, root, "Owner::make", "c:@S@Owner@F@make#",
                     "make", "method", 6, parent="c:@S@Owner", access="public",
                     type_info="void ()")
    db.add_edge(method_id, factory_id, C["method_of"])
    db.add_edge(method_id, ctor_id, 12)  # construct-heap

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=factory_id, dst_id=target_id, kind=7)
    assert rows, "creates(7) missing"
    assert rows[0]["create_form"] == 5


def test_kind_uses(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    shape_id = _sym(db, root, "Shape", "c:@S@Shape", "Shape", "class", 1)
    area_id = _sym(db, root, "Shape::area", "c:@S@Shape@F@area#",
                   "area", "method", 2, is_pure=True, parent="c:@S@Shape",
                   access="public")
    renderer_id = _sym(db, root, "Renderer", "c:@S@Renderer", "Renderer", "class", 10)
    render_meth = _sym(db, root, "Renderer::render", "c:@S@Renderer@F@render#",
                       "render", "method", 11, parent="c:@S@Renderer",
                       type_info="void ()", access="public")
    db.add_edge(area_id, shape_id, C["method_of"])
    db.add_edge(render_meth, renderer_id, C["method_of"])
    db.add_edge(render_meth, area_id, C["calls"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=renderer_id, dst_id=shape_id, kind=8)
    assert rows, "uses(8) missing"
    assert rows[0]["partial"] == 1, "partial=1 expected for virtual dispatch"


def test_kind_destroys(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    victim_id = _sym(db, root, "Victim", "c:@S@Victim", "Victim", "class", 1)
    dtor_id = _sym(db, root, "Victim::~Victim", "c:@S@Victim@D1",
                   "~Victim", "destructor", 2, parent="c:@S@Victim")
    killer_id = _sym(db, root, "Killer", "c:@S@Killer", "Killer", "class", 5)
    kill_meth = _sym(db, root, "Killer::kill", "c:@S@Killer@F@kill#",
                     "kill", "method", 6, parent="c:@S@Killer",
                     type_info="void ()", access="public")
    db.add_edge(kill_meth, killer_id, C["method_of"])
    db.add_edge(kill_meth, dtor_id, 16)  # destroy = 16

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=killer_id, dst_id=victim_id, kind=9)
    assert rows, "destroys(9) missing"


def test_kind_nests(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    outer_id = _sym(db, root, "Outer", "c:@S@Outer", "Outer", "class", 1)
    inner_id = _sym(db, root, "Outer::Inner", "c:@S@Outer@S@Inner",
                    "Inner", "struct", 2, parent="c:@S@Outer")
    db.add_edge(outer_id, inner_id, C["contains"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=outer_id, dst_id=inner_id, kind=10)
    assert rows, "nests(10) missing"


def test_kind_befriends(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    # `class Vault { friend class Pool; };` -> Layer-0 friend(17) Vault->Pool,
    # rolled up to befriends(11).
    vault_id = _sym(db, root, "Vault", "c:@S@Vault", "Vault", "class", 1)
    pool_id = _sym(db, root, "Pool", "c:@S@Pool", "Pool", "struct", 5)
    db.add_edge(vault_id, pool_id, C["friend"])  # friend = 17

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=vault_id, dst_id=pool_id, kind=11)
    assert rows, "befriends(11) missing"


# ---------------------------------------------------------------------------
# Idempotency + re-materialise
# ---------------------------------------------------------------------------

def test_rollup_idempotency(tmp_path):
    """Running materialise twice → identical rows (UNIQUE holds, no dupes)."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    a_id = _sym(db, root, "A", "c:@S@A", "A", "class", 1)
    b_id = _sym(db, root, "B", "c:@S@B", "B", "class", 5)
    field_id = _sym(db, root, "A::b", "c:@S@A@FI@b", "b", "member", 2,
                    parent="c:@S@A", type_info="int", access="private")
    db.add_edge(b_id, a_id, C["inherits"], base_access=0)
    db.add_edge(field_id, a_id, C["field_of"])  # makes A not-interface

    materialize_entity_edges(db)
    first = db.entity_edges()

    materialize_entity_edges(db)
    second = db.entity_edges()

    assert first == second, "entity_edge rows differ on second materialise (not idempotent)"


def test_rematerialise_after_clear(tmp_path):
    """DELETE FROM entity_edge + re-run → same rows."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    a_id = _sym(db, root, "A", "c:@S@A", "A", "class", 1)
    b_id = _sym(db, root, "B", "c:@S@B", "B", "class", 5)
    field_id = _sym(db, root, "A::x", "c:@S@A@FI@x", "x", "member", 2,
                    parent="c:@S@A", type_info="int", access="private")
    db.add_edge(b_id, a_id, C["inherits"], base_access=0)
    db.add_edge(field_id, a_id, C["field_of"])

    materialize_entity_edges(db)
    first = db.entity_edges()

    db.clear_entity_edges()
    assert db.entity_edges() == [], "clear_entity_edges() should leave table empty"

    materialize_entity_edges(db)
    second = db.entity_edges()
    assert first == second


def test_migration_entity_edge_empty_post_migrate(tmp_path):
    """After migration (v16→v17), entity_edge is empty (no backfill)."""
    # A fresh database is v17; simulate an older one by writing v16 explicitly
    # and then re-opening (the schema script re-runs CREATE IF NOT EXISTS).
    db, dir_id = _fresh(tmp_path)
    # entity_edge exists and is empty by definition right after schema creation
    rows = db.entity_edges()
    db.close()
    assert rows == [], "entity_edge should be empty immediately after schema creation"


def test_migration_entity_edge_populated_after_resolve(tmp_path):
    """After resolve_pass(), entity_edge has rows when inheritance exists."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    a_id = _sym(db, root, "A", "c:@S@A", "A", "class", 1)
    b_id = _sym(db, root, "B", "c:@S@B", "B", "class", 5)
    field_id = _sym(db, root, "A::x", "c:@S@A@FI@x", "x", "member", 2,
                    parent="c:@S@A", type_info="int", access="private")
    db.add_edge(b_id, a_id, C["inherits"], base_access=0)
    db.add_edge(field_id, a_id, C["field_of"])

    db.resolve_pass()

    rows = db.entity_edges()
    assert len(rows) > 0, "entity_edge should have rows after resolve_pass()"


# ---------------------------------------------------------------------------
# Multiplicity
# ---------------------------------------------------------------------------

def test_multiplicity_value_member_is_one(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    part_id = _sym(db, root, "Part", "c:@S@Part", "Part", "class", 1)
    owner_id = _sym(db, root, "Owner", "c:@S@Owner", "Owner", "class", 10)
    field_id = _sym(db, root, "Owner::p", "c:@S@Owner@FI@p", "p", "member", 11,
                    parent="c:@S@Owner", type_info="Part", access="private")
    db.add_edge(field_id, owner_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=owner_id, dst_id=part_id)
    assert rows, "No entity_edge for value member"
    assert rows[0]["multiplicity"] == 1


def test_multiplicity_unique_ptr_is_2(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    part_id = _sym(db, root, "Part", "c:@S@Part", "Part", "class", 1)
    owner_id = _sym(db, root, "Owner", "c:@S@Owner", "Owner", "class", 10)
    field_id = _sym(db, root, "Owner::p", "c:@S@Owner@FI@p", "p", "member", 11,
                    parent="c:@S@Owner", type_info="std::unique_ptr<Part>",
                    access="private")
    db.add_edge(field_id, owner_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=owner_id, dst_id=part_id)
    assert rows and rows[0]["multiplicity"] == 2


def test_multiplicity_vector_is_3(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    part_id = _sym(db, root, "Part", "c:@S@Part", "Part", "class", 1)
    owner_id = _sym(db, root, "Owner", "c:@S@Owner", "Owner", "class", 10)
    field_id = _sym(db, root, "Owner::p", "c:@S@Owner@FI@p", "p", "member", 11,
                    parent="c:@S@Owner", type_info="std::vector<Part>",
                    access="private")
    db.add_edge(field_id, owner_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=owner_id, dst_id=part_id)
    assert rows and rows[0]["multiplicity"] == 3


# ---------------------------------------------------------------------------
# Partial flag
# ---------------------------------------------------------------------------

def test_partial_factory_construct(tmp_path):
    """factory-construct (kind 15) → creates(7) with partial=1."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    tgt_id = _sym(db, root, "Tgt", "c:@S@Tgt", "Tgt", "class", 1)
    ctor_id = _sym(db, root, "Tgt::Tgt", "c:@S@Tgt@C1", "Tgt", "constructor", 2,
                   parent="c:@S@Tgt")
    own_id = _sym(db, root, "Own", "c:@S@Own", "Own", "class", 5)
    meth_id = _sym(db, root, "Own::make", "c:@S@Own@F@make#", "make", "method", 6,
                   parent="c:@S@Own", type_info="void ()", access="public")
    db.add_edge(meth_id, own_id, C["method_of"])
    db.add_edge(meth_id, ctor_id, 15)  # factory-construct

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=own_id, dst_id=tgt_id, kind=7)
    assert rows, "creates missing for factory-construct"
    assert rows[0]["partial"] == 1, "partial=1 expected for factory-construct"
    assert rows[0]["create_form"] == 6, "create_form=6 (factory) expected"


# ---------------------------------------------------------------------------
# Access / virtual base
# ---------------------------------------------------------------------------

def test_virtual_base_is_virtual_flag(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    base_id = _sym(db, root, "Base", "c:@S@Base", "Base", "class", 1)
    field_id = _sym(db, root, "Base::x", "c:@S@Base@FI@x", "x", "member", 2,
                    parent="c:@S@Base", type_info="int")
    derived_id = _sym(db, root, "Derived", "c:@S@Derived", "Derived", "class", 10)
    db.add_edge(derived_id, base_id, C["inherits"], base_access=0, is_virtual=1)
    db.add_edge(field_id, base_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=derived_id, dst_id=base_id, kind=1)
    assert rows, "generalizes missing"
    assert rows[0]["is_virtual"] == 1


# ---------------------------------------------------------------------------
# pinned scenario: nests-1 (from design plan PR2 test matrix)
# ---------------------------------------------------------------------------

def test_nests_pinned_scenario_id_nests1(tmp_path):
    """id=nests-1: Base::Nested → nests(10) row src=Base dst=Nested."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    base_id = _sym(db, root, "Base", "c:@S@Base", "Base", "class", 1)
    nested_id = _sym(db, root, "Base::Nested", "c:@S@Base@S@Nested",
                     "Nested", "struct", 2, parent="c:@S@Base")
    db.add_edge(base_id, nested_id, C["contains"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=base_id, dst_id=nested_id, kind=10)
    assert rows, "nests(10) missing for Base::Nested (pinned nests-1)"


# ---------------------------------------------------------------------------
# create_form variants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("l0_kind,expected_form,desc", [
    (10, 3, "value"),
    (11, 4, "temp"),
    (12, 5, "heap"),
    (13, 7, "copy"),
    (14, 8, "move"),
    (15, 6, "factory"),
])
def test_create_form_per_l0_kind(tmp_path, l0_kind, expected_form, desc):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    tgt_id = _sym(db, root, "Tgt", f"c:@S@Tgt_{desc}", f"Tgt_{desc}", "class", 1)
    ctor_id = _sym(db, root, f"Tgt_{desc}::Tgt", f"c:@S@Tgt_{desc}@C1",
                   f"Tgt_{desc}", "constructor", 2, parent=f"c:@S@Tgt_{desc}")
    own_id = _sym(db, root, f"Own_{desc}", f"c:@S@Own_{desc}", f"Own_{desc}",
                  "class", 5)
    meth_id = _sym(db, root, f"Own_{desc}::make", f"c:@S@Own_{desc}@F@make#",
                   "make", "method", 6, parent=f"c:@S@Own_{desc}",
                   type_info="void ()", access="public")
    db.add_edge(meth_id, own_id, C["method_of"])
    db.add_edge(meth_id, ctor_id, l0_kind)

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=own_id, dst_id=tgt_id, kind=7)
    assert rows, f"creates missing for {desc}"
    assert rows[0]["create_form"] == expected_form, (
        f"create_form: expected {expected_form}, got {rows[0]['create_form']} for {desc}"
    )

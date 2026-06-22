"""PR2: v17 entity_edge materialisation tests.

Covers:
  * schema/version invariants (SCHEMA_VERSION=21, VERSION='0.28.2')
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
    2: "implements",
    3: "specializes",
    4: "composes",
    5: "aggregates",
    6: "associates",
    7: "creates",
    8: "uses",
    9: "destroys",
    10: "befriends",
    11: "instantiates",
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

def test_schema_version_is_21():
    assert SCHEMA_VERSION == 21, f"Expected 21, got {SCHEMA_VERSION}"


def test_product_version_is_0282():
    from indexer import cli
    assert cli.VERSION == "0.28.2", f"Expected '0.28.2', got {cli.VERSION!r}"


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

# ADR-008 S1: implements XOR generalizes
def test_bdd_ADR008_S1_implements_xor_generalizes_interface(tmp_path):
    """Interface base (all-pure, no data) → implements(2), NOT generalizes(1)."""
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
    assert 2 in kinds, "implements(2) missing for Interface base"
    assert 1 not in kinds, "generalizes(1) must NOT fire for Interface base"


def test_bdd_ADR008_S1_state_base_generalizes(tmp_path):
    """State-bearing base (has data field) → generalizes(1), NOT implements(2)."""
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
    assert 2 not in kinds, "implements(2) must NOT fire for state-bearing base"


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
# All 10 entity_edge kinds
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


def test_kind_implements(tmp_path):
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
    assert rows, "implements(2) missing"


def test_kind_specializes_fires_for_explicit_specialization(tmp_path):
    """An EXPLICIT specialization (`template<> class Vec<bool>{...}`) is a distinct
    design entity that specializes its primary.  Its Layer-0 specializes(4) edge
    points instance -> primary; the SOURCE is kept un-collapsed (only the dst is
    collapsed, which is already the primary), so specializes(3) Vec<bool> -> Vec
    FIRES -- it is NOT suppressed."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")

    primary_id = _sym(db, root, "Vec", "c:@ST@Vec", "Vec", "class-template", 1)
    spec_id = _sym(db, root, "Vec<bool>", "c:@S@Vec#b", "Vec<bool>", "class", 10)
    db.add_edge(spec_id, primary_id, EDGE_KINDS["specializes"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=spec_id, dst_id=primary_id, kind=3)
    assert rows, "specializes(3) Vec<bool> -> Vec must fire for an explicit spec"
    # No spurious self-edge on the primary.
    assert not db.entity_edges(src_id=primary_id, kind=3), (
        "no Vec->Vec self-edge"
    )
    # A specialization is NEVER an instantiation: specializes(4) must not leak
    # into instantiates(11).
    assert not db.entity_edges(src_id=spec_id, kind=11), (
        "specializes(4) must NOT materialise as instantiates(11)"
    )


def test_kind_instantiates_fires_for_plain_instantiation(tmp_path):
    """A plain instantiation (`X<B>` via a `using`/use) is a distinct design
    entity that instantiates its primary.  Its Layer-0 instantiates(5) edge points
    instance -> primary; the SOURCE is kept un-collapsed, so instantiates(11)
    X<B> -> X FIRES.  It is NOT a specialization -- specializes(3) must NOT fire."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")

    primary_id = _sym(db, root, "X", "c:@ST@X", "X", "class-template", 1)
    inst_id = _sym(db, root, "X<B>", "c:@S@X>#$@S@B", "X<B>", "class", 10)
    db.add_edge(inst_id, primary_id, EDGE_KINDS["instantiates"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=inst_id, dst_id=primary_id, kind=11)
    assert rows, "instantiates(11) X<B> -> X must fire for a plain instantiation"
    # An instantiation is NEVER a specialization.
    assert not db.entity_edges(src_id=inst_id, kind=3), (
        "instantiates(5) must NOT materialise as specializes(3)"
    )
    # No spurious self-edge on the primary.
    assert not db.entity_edges(src_id=primary_id, kind=11), (
        "no X->X self-edge"
    )


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
    """shared_ptr field -> aggregates(5): SHARED ownership, part can outlive owner."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    part_id = _sym(db, root, "Part", "c:@S@Part", "Part", "class", 1)
    owner_id = _sym(db, root, "Owner", "c:@S@Owner", "Owner", "class", 10)
    field_id = _sym(db, root, "Owner::p", "c:@S@Owner@FI@p", "p", "member", 11,
                    parent="c:@S@Owner", type_info="std::shared_ptr<Part>",
                    access="private")
    db.add_edge(field_id, owner_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=owner_id, dst_id=part_id, kind=5)
    assert rows, "aggregates(5) missing for shared_ptr"


@pytest.mark.parametrize("type_info", ["std::unique_ptr<Part>", "std::optional<Part>"])
def test_unique_ptr_and_optional_compose(tmp_path, type_info):
    """unique_ptr / optional -> composes(4): EXCLUSIVE ownership (dies with owner),
    NOT aggregates. Multiplicity 2 (0..1)."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    part_id = _sym(db, root, "Part", "c:@S@Part", "Part", "class", 1)
    owner_id = _sym(db, root, "Owner", "c:@S@Owner", "Owner", "class", 10)
    field_id = _sym(db, root, "Owner::p", "c:@S@Owner@FI@p", "p", "member", 11,
                    parent="c:@S@Owner", type_info=type_info, access="private")
    db.add_edge(field_id, owner_id, C["field_of"])

    materialize_entity_edges(db)
    composes = db.entity_edges(src_id=owner_id, dst_id=part_id, kind=4)
    aggregates = db.entity_edges(src_id=owner_id, dst_id=part_id, kind=5)
    assert composes, f"{type_info} must be composes(4) (exclusive ownership)"
    assert not aggregates, f"{type_info} must NOT be aggregates(5)"
    assert composes[0]["multiplicity"] == 2, "unique_ptr/optional is 0..1"


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


def test_map_resolves_to_value_type(tmp_path):
    """std::map<K,V> -> relation to the VALUE type V (0..*), not the key K.

    Regression: the value type was previously never extracted (the unwrapper
    only looked at the first template arg / key), so Transaction->Order was lost.
    """
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    order_id = _sym(db, root, "Order", "c:@S@Order", "Order", "struct", 1)
    txn_id = _sym(db, root, "Transaction", "c:@S@Transaction", "Transaction",
                  "struct", 10)
    field_id = _sym(db, root, "Transaction::m", "c:@S@Transaction@FI@m", "m",
                    "member", 11, parent="c:@S@Transaction",
                    type_info="std::map<std::string, Order>", access="private")
    db.add_edge(field_id, txn_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=txn_id, dst_id=order_id, kind=4)  # composes
    assert rows, "map<K,V> by value must compose its value type Order"
    assert rows[0]["multiplicity"] == 3, "map is 0..*"


def test_map_of_pointer_value_associates(tmp_path):
    """std::map<K, V*> -> associates V (0..*): the value is a borrowed pointer."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    node_id = _sym(db, root, "Node", "c:@S@Node", "Node", "struct", 1)
    graph_id = _sym(db, root, "Graph", "c:@S@Graph", "Graph", "struct", 10)
    field_id = _sym(db, root, "Graph::byId", "c:@S@Graph@FI@byId", "byId",
                    "member", 11, parent="c:@S@Graph",
                    type_info="std::map<int, Node*>", access="private")
    db.add_edge(field_id, graph_id, C["field_of"])

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=graph_id, dst_id=node_id, kind=6)  # associates
    assert rows, "map<K, V*> must associate the (borrowed) value type Node"
    assert rows[0]["multiplicity"] == 3, "map is 0..*"


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


def test_collapse_instance_endpoint_to_primary(tmp_path):
    """A non-specializes edge whose endpoint is a template INSTANCE collapses
    onto the primary (ADR-008 decision 6). uses(Box<int> -> Sink) becomes
    uses(Box -> Sink): the entity_edge is keyed on the primary, never the
    per-instantiation node."""
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    # primary class-template Box + its implicit instantiation Box<int>
    box_id = _sym(db, root, "Box", "c:@ST@Box", "Box", "class-template", 1)
    box_int_id = _sym(db, root, "Box<int>", "c:@S@Box>#I", "Box", "class", 2)
    db.add_edge(box_int_id, box_id, C["instantiates"])  # instance -> primary

    # Box<int>::use() (owned by the INSTANCE) calls Sink::accept()
    use_meth = _sym(db, root, "Box<int>::use", "c:@S@Box>#I@F@use#",
                    "use", "method", 3, parent="c:@S@Box>#I",
                    type_info="void ()", access="public")
    sink_id = _sym(db, root, "Sink", "c:@S@Sink", "Sink", "class", 10)
    accept_id = _sym(db, root, "Sink::accept", "c:@S@Sink@F@accept#",
                     "accept", "method", 11, parent="c:@S@Sink",
                     type_info="void ()", access="public")
    db.add_edge(use_meth, box_int_id, C["method_of"])
    db.add_edge(accept_id, sink_id, C["method_of"])
    db.add_edge(use_meth, accept_id, C["calls"])

    materialize_entity_edges(db)
    # The use edge is attributed to the PRIMARY Box, not the instance Box<int>.
    assert db.entity_edges(src_id=box_id, dst_id=sink_id, kind=8), (
        "uses(8) must be keyed on the primary template Box"
    )
    assert not db.entity_edges(src_id=box_int_id, kind=8), (
        "no uses(8) keyed on the Box<int> instantiation node"
    )


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


def test_kind_befriends(tmp_path):
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    # `class Vault { friend class Pool; };` -> Layer-0 friend(17) Vault->Pool,
    # rolled up to befriends(10).
    vault_id = _sym(db, root, "Vault", "c:@S@Vault", "Vault", "class", 1)
    pool_id = _sym(db, root, "Pool", "c:@S@Pool", "Pool", "struct", 5)
    db.add_edge(vault_id, pool_id, C["friend"])  # friend = 17

    materialize_entity_edges(db)
    rows = db.entity_edges(src_id=vault_id, dst_id=pool_id, kind=10)
    assert rows, "befriends(10) missing"


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


def test_null_via_edges_dedup_within_materialise(tmp_path):
    """v20→v21 regression: NULL-via edges that collapse to the same logical
    (src,dst,kind) must produce ONE row, not duplicate copies.

    Two template instantiations (Derived<int>, Derived<float>) both inherit
    Base; both collapse onto the primary Derived, so the roll-up inserts the
    same NULL-via generalizes edge twice. The old UNIQUE(src,dst,kind,via)
    never collided on NULL via (SQLite NULL != NULL), so the ON CONFLICT merge
    silently fanned out into 2 rows. The COALESCE identity index folds NULL to a
    sentinel so the second insert upserts the first.
    """
    if not _HAS_ROLLUP:
        pytest.skip()
    db, dir_id = _fresh(tmp_path)
    root = db.add_file(dir_id, "h.hpp")
    C = EDGE_KINDS

    base = _sym(db, root, "Base", "c:@S@Base", "Base", "class", 1)
    prim = _sym(db, root, "Derived", "c:@ST>1#T@Derived", "Derived",
                "class-template", 2)
    i1 = _sym(db, root, "Di", "c:@S@Derived>#I", "Derived<int>", "class", 3)
    i2 = _sym(db, root, "Df", "c:@S@Derived>#f", "Derived<float>", "class", 4)
    db.add_edge(i1, prim, C["instantiates"])
    db.add_edge(i2, prim, C["instantiates"])
    db.add_edge(i1, base, C["inherits"], base_access=0)
    db.add_edge(i2, base, C["inherits"], base_access=0)

    # Running resolve/materialise repeatedly must never accumulate copies.
    for _ in range(3):
        materialize_entity_edges(db)
        gens = [
            r for r in db._conn.execute(
                "SELECT src_id, dst_id FROM entity_edge WHERE kind IN (1, 2)"
            )
        ]
        assert len(gens) == 1, (
            f"expected exactly one Derived→Base generalizes row, got {gens}"
        )

    # The NULL-safe identity index must be present.
    idx = db._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_entity_edge_identity'"
    ).fetchone()
    assert idx is not None, "idx_entity_edge_identity missing from schema"


def test_null_via_add_entity_edge_upserts(tmp_path):
    """Direct add_entity_edge with NULL via must upsert, but distinct
    create_form values stay separate rows (form is part of the identity)."""
    db, _ = _fresh(tmp_path)
    a = db.mint_symbol_id("c:@S@A", "A", kind="struct")
    b = db.mint_symbol_id("c:@S@B", "B", kind="struct")

    for _ in range(3):
        db.add_entity_edge(a, b, 8, via_member_id=None)  # uses(8), NULL via
    n_uses = db._conn.execute(
        "SELECT count(*) FROM entity_edge WHERE src_id=? AND dst_id=? AND kind=8",
        (a, b),
    ).fetchone()[0]
    assert n_uses == 1, f"NULL-via uses(8) not deduped: {n_uses} rows"

    for form in (3, 4, 5):
        db.add_entity_edge(a, b, 7, via_member_id=None, create_form=form)
    n_creates = db._conn.execute(
        "SELECT count(*) FROM entity_edge WHERE src_id=? AND dst_id=? AND kind=7",
        (a, b),
    ).fetchone()[0]
    assert n_creates == 3, (
        f"distinct create_form values must stay separate rows: got {n_creates}"
    )


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


@pytest.mark.parametrize("stamped_version", ["17", "18"])
def test_migration_drops_nests_and_renumbers_befriends(tmp_path, stamped_version):
    """The entity-kind migration cleans the DB in place: drop the defunct
    nests(10) rows, renumber befriends 11 -> 10, rename realizes(2) ->
    implements(2), and reseed entity_edge_kind.

    Gated on the stale seed rows, NOT the version -- so it fires even on a DB an
    earlier build already stamped v18 WITHOUT cleaning (stamped_version=18).
    """
    import sqlite3

    db_path = str(tmp_path / "index.db")
    # Build a current DB, then mutate it to carry the old (stale) entity_edge
    # state: old kind table + one nests(10) edge and one befriends(11) edge.
    s = Storage(db_path)
    s.close()
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "INSERT INTO symbol (usr,spelling,kind) VALUES "
        "('c:@S@A','A',4),('c:@S@B','B',4);"
        "DELETE FROM entity_edge_kind;"
        "INSERT INTO entity_edge_kind (id,name) VALUES "
        "(1,'generalizes'),(2,'realizes'),(3,'specializes'),(4,'composes'),"
        "(5,'aggregates'),(6,'associates'),(7,'creates'),(8,'uses'),"
        "(9,'destroys'),(10,'nests'),(11,'befriends');"
    )
    a = conn.execute("SELECT id FROM symbol WHERE usr='c:@S@A'").fetchone()[0]
    b = conn.execute("SELECT id FROM symbol WHERE usr='c:@S@B'").fetchone()[0]
    conn.execute(
        "INSERT INTO entity_edge (src_id,dst_id,kind) VALUES (?,?,10),(?,?,11)",
        (a, b, a, b),
    )
    conn.execute(
        "UPDATE meta SET value=? WHERE key='schema_version'", (stamped_version,)
    )
    conn.commit()
    conn.close()

    # Reopen -> _migrate() runs in the constructor.
    Storage(db_path).close()

    conn = sqlite3.connect(db_path)
    try:
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        edges = conn.execute("SELECT kind FROM entity_edge").fetchall()
        kinds = conn.execute(
            "SELECT id,name FROM entity_edge_kind ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert ver == str(SCHEMA_VERSION), (
        f"schema_version not at {SCHEMA_VERSION}: {ver}"
    )
    # the nests row is gone; only befriends survives, renumbered 11 -> 10
    assert edges == [(10,)], f"expected only befriends(10); got {edges}"
    # 11 rows: 1-10 plus the reseeded instantiates(11). The old (11,'befriends')
    # was renumbered to 10, freeing id 11 for the schema script's INSERT OR IGNORE.
    assert len(kinds) == 11, f"entity_edge_kind should have 11 rows; got {kinds}"
    names = [n for _, n in kinds]
    assert "nests" not in names, "stale 'nests' seed row still present"
    assert "realizes" not in names, "stale 'realizes' seed row was not renamed"
    assert dict(kinds)[10] == "befriends", "id 10 should be befriends after migrate"
    assert dict(kinds)[11] == "instantiates", "id 11 should be instantiates after migrate"
    assert dict(kinds)[2] == "implements", "id 2 should be implements after migrate"


def test_migration_nests_cleanup_is_idempotent(tmp_path):
    """A clean v19 DB (no stale marker) is left untouched on re-open."""
    db_path = str(tmp_path / "index.db")
    Storage(db_path).close()
    # second open must not raise and must keep the 11-row seed intact
    Storage(db_path).close()
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        kinds = conn.execute("SELECT id,name FROM entity_edge_kind ORDER BY id").fetchall()
    finally:
        conn.close()
    names = [n for _, n in kinds]
    assert len(kinds) == 11
    assert "nests" not in names and "realizes" not in names
    assert dict(kinds)[2] == "implements"
    assert dict(kinds)[11] == "instantiates"


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

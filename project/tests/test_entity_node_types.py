"""Tests for the materialized entity-node *type* and its EntityNode subclasses.

The Layer-1 entity graph classifies every record / enum / class-template into a
design type (``entity_node.kind``, written at resolve): class / abstract_class /
interface / union / enum + the three class-template variants.  ``entity()`` then
wraps a node as the matching subclass so the type-specific verbs are available:

  * a concrete ``ClassNode``      -> ``implements()`` (its abstract/interface bases)
  * an ``AbstractClassNode``      -> ``implemented_by()`` + ``pure_methods()``
  * an ``InterfaceNode``          -> ``implemented_by()`` + ``operations()``

and ``EntityNode.as_model()`` bridges down to the low-level model layer (where
the C++ keyword class / struct / union is distinct).

Builds a REAL index, materialises the entity graph, and asserts against it; one
test deliberately SKIPS materialise to prove the read-time fallback agrees.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from indexer.storage import Storage  # noqa: E402
from indexer.query import GraphQuery  # noqa: E402
from indexer.clang import ast as A  # noqa: E402
from indexer.clang import util as U  # noqa: E402
from indexer.entity_graph import (  # noqa: E402
    EntityGraph,
    EntityType,
    ClassKind,
    ClassNode,
    AbstractClassNode,
    InterfaceNode,
    UnionNode,
    EnumNode,
    ClassTemplateNode,
    InterfaceTemplateNode,
)
from indexer.entity_rollup import materialize_entity_edges  # noqa: E402
from indexer import model as M  # noqa: E402


# An interface (Drawable: all-pure + virtual dtor), an abstract class (Shape:
# pure area() + state + concrete fn), a concrete class (Circle : Shape), a
# concrete struct (Point), a union (Value), an enum (Color), a concrete class
# template (Box<T>) and a pure-interface class template (ISink<T>).
SOURCE = """
namespace app {

struct Drawable {                       // interface
    virtual void draw() const = 0;
    virtual ~Drawable() = default;
};

class Shape : public Drawable {         // abstract (pure + state + concrete fn)
    int id_;
public:
    virtual double area() const = 0;
    int id() const { return id_; }
};

class Circle : public Shape {           // concrete
    double r;
public:
    void draw() const override {}
    double area() const override { return r; }
};

struct Point { int x, y; };             // concrete struct
union Value { int i; float f; };        // union
enum Color { RED, GREEN, BLUE };        // enum

template <class T>
struct Box { T value; T get() const { return value; } };   // concrete template

template <class T>
struct ISink {                          // interface template
    virtual void put(T) = 0;
    virtual ~ISink() = default;
};

}  // namespace app
"""


def _build(tmp: str, *, resolve: bool) -> EntityGraph:
    path = os.path.join(tmp, "fixture.cpp")
    with open(path, "w") as fh:
        fh.write(SOURCE)
    tu = U.parse(path, args=["-std=c++17"], check=False)
    fatal = [d for d in tu.diagnostics if d.severity >= 3]
    assert not fatal, "; ".join(d.spelling for d in fatal)

    db = Storage(os.path.join(tmp, "i.db"))
    db.add_component("t", tmp)
    file_id = db.add_file_path(path)
    with db.transaction():
        A.index_symbols(db, tu, file_id)
    with db.transaction():
        db.delete_edges_for_file(file_id)
        A._index_edges_notxn(db, tu, path, file_id)
    if resolve:
        materialize_entity_edges(db)
    return EntityGraph(GraphQuery.from_connection(db._conn))


@pytest.fixture
def eg():
    with tempfile.TemporaryDirectory() as tmp:
        graph = _build(tmp, resolve=True)
        yield graph
        graph.close()


def _one(eg: EntityGraph, name: str):
    hits = eg.find("app::" + name)
    assert hits, f"no entity {name}"
    return hits[0]


# --------------------------------------------------------------------------- #
# Materialized type column
# --------------------------------------------------------------------------- #


def test_entity_node_table_populated(eg):
    rows = eg._c.execute("SELECT COUNT(*) FROM entity_node").fetchone()[0]
    assert rows >= 8  # Drawable Shape Circle Point Value Color Box ISink


def test_materialized_entity_types(eg):
    et = lambda n: _one(eg, n).entity_type
    assert et("Drawable") is EntityType.INTERFACE
    assert et("Shape") is EntityType.ABSTRACT_CLASS
    assert et("Circle") is EntityType.CLASS
    assert et("Point") is EntityType.CLASS  # struct keyword collapses to CLASS here
    assert et("Value") is EntityType.UNION
    assert et("Color") is EntityType.ENUM
    assert et("Box") is EntityType.CLASS_TEMPLATE
    assert et("ISink") is EntityType.INTERFACE_TEMPLATE


def test_entity_type_projects_to_class_kind(eg):
    assert _one(eg, "Drawable").class_kind is ClassKind.INTERFACE
    assert _one(eg, "Shape").class_kind is ClassKind.ABSTRACT
    assert _one(eg, "Circle").class_kind is ClassKind.CONCRETE
    assert _one(eg, "Box").class_kind is ClassKind.CONCRETE


# --------------------------------------------------------------------------- #
# Subclass dispatch -- the node IS the type
# --------------------------------------------------------------------------- #


def test_node_subclass_dispatch(eg):
    assert isinstance(_one(eg, "Circle"), ClassNode)
    assert isinstance(_one(eg, "Shape"), AbstractClassNode)
    assert isinstance(_one(eg, "Drawable"), InterfaceNode)
    assert isinstance(_one(eg, "Value"), UnionNode)
    assert isinstance(_one(eg, "Color"), EnumNode)
    assert isinstance(_one(eg, "Box"), ClassTemplateNode)
    assert isinstance(_one(eg, "ISink"), InterfaceTemplateNode)
    # the three abstractness types are mutually exclusive
    assert not isinstance(_one(eg, "Circle"), (AbstractClassNode, InterfaceNode))
    assert not isinstance(_one(eg, "Shape"), InterfaceNode)


def test_repr_uses_entity_type(eg):
    assert repr(_one(eg, "Drawable")) == "<interface app::Drawable>"
    assert repr(_one(eg, "Shape")) == "<abstract_class app::Shape>"


# --------------------------------------------------------------------------- #
# Distinct methods: implements / implemented_by / operations
# --------------------------------------------------------------------------- #


def test_concrete_class_implements(eg):
    circle = _one(eg, "Circle")
    # direct non-concrete supertype: Shape (abstract)
    assert sorted(n.name for n in circle.implements()) == ["app::Shape"]
    # a concrete class has no implemented_by / operations verbs
    assert not hasattr(circle, "implemented_by")
    assert not hasattr(circle, "operations")
    assert circle.is_instantiable is True


def test_abstract_and_interface_implemented_by(eg):
    drawable = _one(eg, "Drawable")
    shape = _one(eg, "Shape")
    # Drawable is realized directly by Shape (implements edge)
    assert "app::Shape" in [n.name for n in drawable.implemented_by()]
    # Shape is extended directly by Circle (generalizes edge)
    assert "app::Circle" in [n.name for n in shape.implemented_by()]
    # concrete_only drops the abstract Shape from Drawable's implementors
    assert "app::Shape" not in [
        n.name for n in drawable.implemented_by(concrete_only=True)
    ]
    # neither carries the concrete-only verb
    assert not hasattr(drawable, "implements")
    assert not hasattr(shape, "implements")
    assert drawable.is_instantiable is False and shape.is_instantiable is False


def test_interface_operations_and_abstract_pure_methods(eg):
    ops = [s.spelling for s in _one(eg, "Drawable").operations()]
    assert "draw" in ops  # the pure contract method (dtor excluded)
    assert all(s != "~Drawable" for s in ops)
    pures = [s.spelling for s in _one(eg, "Shape").pure_methods()]
    assert "area" in pures and "id" not in pures  # id() is concrete


# --------------------------------------------------------------------------- #
# Bridge down to the low-level model layer
# --------------------------------------------------------------------------- #


def test_as_model_bridges_to_keyword_types(eg):
    circle = _one(eg, "Circle").as_model()
    assert isinstance(circle, M.Class) and circle.kind == "class"
    point = _one(eg, "Point").as_model()
    assert isinstance(point, M.Struct) and point.kind == "struct"
    value = _one(eg, "Value").as_model()
    assert isinstance(value, M.Union) and value.kind == "union"
    # the bridge reaches the symbol graph -- members are queryable
    assert "draw" in {m.spelling for m in circle.methods}


# --------------------------------------------------------------------------- #
# Read-time fallback (un-resolved index) agrees with the materialized table
# --------------------------------------------------------------------------- #


def test_v21_to_v22_migration_backfills_without_reindex():
    """An upgraded index (entity_node table absent) gets its design types filled
    in on the next open -- a pure-DB classification of existing symbols, no
    re-parse / re-index / resolve."""
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        graph = _build(tmp, resolve=True)
        graph.close()
        db_path = os.path.join(tmp, "i.db")
        # Simulate a v21 index: drop the v22 tables and roll the version back.
        c = sqlite3.connect(db_path)
        c.execute("DROP TABLE entity_node")
        c.execute("DROP TABLE entity_kind")
        c.execute("UPDATE meta SET value = '21' WHERE key = 'schema_version'")
        c.commit()
        c.close()

        # Reopen via Storage -> migration backfills entity_node (no reindex).
        db = Storage(db_path)
        try:
            ver = db._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            assert ver == "25"  # migration now lands on the current schema
            n = db._conn.execute("SELECT COUNT(*) FROM entity_node").fetchone()[0]
            assert n >= 8
            eg = EntityGraph(GraphQuery.from_connection(db._conn))
            assert _one(eg, "Drawable").entity_type is EntityType.INTERFACE
            assert _one(eg, "Shape").entity_type is EntityType.ABSTRACT_CLASS
            assert _one(eg, "Value").entity_type is EntityType.UNION
        finally:
            db.close()


def test_unresolved_fallback_matches_materialized():
    with tempfile.TemporaryDirectory() as tmp:
        unresolved = _build(tmp, resolve=False)
        # entity_node is empty -> entity_type derives from members instead
        assert unresolved._c.execute(
            "SELECT COUNT(*) FROM entity_node"
        ).fetchone()[0] == 0
        for name, want in [
            ("Drawable", EntityType.INTERFACE),
            ("Shape", EntityType.ABSTRACT_CLASS),
            ("Circle", EntityType.CLASS),
            ("Value", EntityType.UNION),
            ("Color", EntityType.ENUM),
            ("ISink", EntityType.INTERFACE_TEMPLATE),
        ]:
            hits = unresolved.find("app::" + name)
            # un-resolved: nodes only reachable via find over symbols, not edges
            node = hits[0] if hits else unresolved.entity(
                unresolved._q.by_name(name)[0]
            )
            assert node.entity_type is want, name
        unresolved.close()

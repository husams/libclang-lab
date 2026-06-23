"""Tests for the fluent relational query layer (EntityGraph.query / EntityQuery).

Builds a REAL index from an inline C++ TU, materializes the Layer-1
entity_edge graph, then drives EntityQuery to answer the motivating questions
('which classes inherit X', 'which classes are used by X') plus chaining,
transitive closure, direction, and filters.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.storage import Storage  # noqa: E402
from indexer.query import GraphQuery  # noqa: E402
from indexer.clang import ast as A  # noqa: E402
from indexer.clang import util as U  # noqa: E402
from indexer.entity_graph import EntityGraph, EntityKind, EdgeKind  # noqa: E402

try:
    from indexer.entity_rollup import materialize_entity_edges
    _HAS_ROLLUP = True
except Exception:  # pragma: no cover
    _HAS_ROLLUP = False


# Shape <- Circle, Square (two subclasses; Shape carries state + a concrete
# method, so the relation is `generalizes`, not `implements`).  Renderer uses
# Shape (calls a method on it in a body).  Circle uses Logger.  Square composes
# a Color by value.  No namespace -> entity names are unqualified.
SOURCE = """
struct Logger { void log(); };
struct Color { int rgb; };

struct Shape {
    int z_order;                              // state -> not a pure interface
    void describe();                          // concrete method
    virtual void draw();
};

struct Circle : Shape {
    Logger *logger;
    void draw() { logger->log(); }            // Circle uses Logger
};

struct Square : Shape {
    Color color;                              // Square composes Color
    void draw() {}
};

struct Renderer {
    void render(Shape &s) { s.draw(); }       // Renderer uses Shape
};
"""


@pytest.fixture
def eg():
    if not _HAS_ROLLUP:
        pytest.skip("entity_rollup not present")
    with tempfile.TemporaryDirectory() as tmp:
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
        materialize_entity_edges(db)

        graph = EntityGraph(GraphQuery.from_connection(db._conn))
        yield graph
        db.close()


# -- the two motivating questions ---------------------------------------- #

def test_classes_that_inherit_x(eg):
    """List classes that inherit from Shape."""
    assert eg.query("Shape").derived().names() == ["Circle", "Square"]


def test_classes_used_by_x(eg):
    """Which entities are used by Renderer -> Shape (behavioural use)."""
    assert eg.query("Renderer").uses().names() == ["Shape"]


def test_who_uses_x_inverse(eg):
    """Inverse direction: who uses Logger -> Circle."""
    assert eg.query("Logger").used_by().names() == ["Circle"]


# -- direction, transitivity, chaining ----------------------------------- #

def test_bases_direction(eg):
    assert eg.query("Circle").bases().names() == ["Shape"]


def test_transitive_derived_closure(eg):
    """Both subclasses appear in the (here 1-deep) closure."""
    assert eg.query("Shape").derived(transitive=True).names() == ["Circle", "Square"]


def test_chaining_relations(eg):
    """Entities used by anything that inherits Shape -> {Logger} (Circle uses it)."""
    used = eg.query("Shape").derived().uses().names()
    assert used == ["Logger"]


def test_then_alias_matches_relation(eg):
    a = eg.query("Shape").derived().then(EdgeKind.USES, "out").names()
    b = eg.query("Shape").derived().relation("uses", "out").names()
    assert a == b == ["Logger"]


def test_composition_step(eg):
    assert eg.query("Square").composes().names() == ["Color"]


# -- filters -------------------------------------------------------------- #

def test_of_kind_filter(eg):
    """Filter the derived set to structs only (all are structs here)."""
    q = eg.query("Shape").derived().of_kind(EntityKind.STRUCT)
    assert q.names() == ["Circle", "Square"]
    assert eg.query("Shape").derived().of_kind(EntityKind.UNION).names() == []


def test_named_filter(eg):
    assert eg.query("Shape").derived().named("ircl").names() == ["Circle"]


def test_exclude_filter(eg):
    assert eg.query("Shape").derived().exclude("Circle").names() == ["Square"]


# -- seeds, terminals, immutability -------------------------------------- #

def test_multi_seed_union(eg):
    """Seed with two entities; union their bases."""
    bases = eg.query("Circle", "Square").bases().names()
    assert bases == ["Shape"]


def test_node_query_entrypoint(eg):
    circle = eg.find("Circle")[0]
    assert circle.query().bases().names() == ["Shape"]


def test_edges_terminal_carries_step_edges(eg):
    edges = list(eg.query("Renderer").uses().edges())
    assert len(edges) == 1
    assert edges[0].kind is EdgeKind.USES
    assert edges[0].src.name == "Renderer" and edges[0].dst.name == "Shape"


def test_query_is_immutable_and_reusable(eg):
    base = eg.query("Shape")
    assert base.derived().names() == ["Circle", "Square"]
    # base is unchanged: a fresh step yields the same result
    assert base.derived().names() == ["Circle", "Square"]
    assert base.names() == ["Shape"]


def test_empty_seed_queries_all_entities(eg):
    """query() with no args seeds every entity in the graph."""
    everything = eg.query().nodes()
    assert {n.name for n in everything} >= {"Shape", "Circle", "Square", "Renderer"}


def test_streaming_terminals_are_generators(eg):
    """nodes()/edges() and the low-level scans are lazy generators, not lists."""
    import types
    assert isinstance(eg.query("Shape").derived().nodes(), types.GeneratorType)
    assert isinstance(eg.query("Renderer").uses().edges(), types.GeneratorType)
    assert isinstance(eg.entities(), types.GeneratorType)
    assert isinstance(eg.edges(), types.GeneratorType)
    assert isinstance(eg.find("Shape")[0].bases(), types.GeneratorType)


def test_first_short_circuits_the_pipeline(eg):
    """first() pulls exactly one node through the chain (lazy, no full build)."""
    seen: list[str] = []
    q = eg.query("Shape").derived().where(lambda n: seen.append(n.name) or True)
    assert q.first() is not None
    assert len(seen) == 1, f"expected 1 node pulled, saw {seen}"


def test_query_reusable_across_terminals(eg):
    """A query is a re-runnable thunk: terminals can be called repeatedly."""
    q = eg.query("Shape").derived()
    assert sorted(n.name for n in q.nodes()) == ["Circle", "Square"]  # consume once
    assert sorted(n.name for n in q.nodes()) == ["Circle", "Square"]  # re-run, fresh
    assert q.count() == 2 and q.names() == ["Circle", "Square"]


def test_terminals(eg):
    q = eg.query("Shape").derived()
    assert q.count() == 2
    assert len(q) == 2
    assert bool(q) is True
    assert q.first().name == "Circle"
    assert {d["name"] for d in q.to_dict()} == {"Circle", "Square"}
    assert eg.query("Color").derived().first() is None

"""Tests for the high-level entity-graph API (indexer.entity_graph).

Builds a REAL index from an inline C++ TU (inheritance + composition + a
behavioural use + a friend), materializes the Layer-1 entity_edge graph, then
drives the OO reader: EdgeKind / EntityKind classes, EntityNode navigation
(bases/derived/walk/neighbors), edge filtering, and attribute decoding.
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
    EntityNode,
    EntityEdge,
    EdgeKind,
    EntityKind,
    Access,
    Multiplicity,
    ClassKind,
    ClassTemplate,
    Interface,
)

try:
    from indexer.entity_rollup import materialize_entity_edges

    _HAS_ROLLUP = True
except Exception:  # pragma: no cover
    _HAS_ROLLUP = False


# A 3-deep base chain (Base <- Mid <- Leaf); a Holder that composes a Part by
# value (composes, multiplicity 1), holds a Leaf* (associates, 0..1), and calls
# a method on Leaf in a body (uses); and a Vault that befriends Holder.
SOURCE = """
namespace app {

struct Base { int b; };
struct Mid : Base { int m; };
struct Leaf : Mid { int l; void tick(); };

struct Part { int p; };

struct Holder {
    Part part;                          // composes (value member)
    Leaf *link;                         // associates (pointer member, 0..1)
    void poke(Leaf &x) { x.tick(); }    // uses Leaf (method-body call)
};

class Vault {
    friend struct Holder;               // befriends
    int secret;
};

}  // namespace app
"""


@pytest.fixture
def eg():
    """A real index over SOURCE, wrapped as an EntityGraph (read-only)."""
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


# --------------------------------------------------------------------------- #
# Kind classes (the "kind of edge" / "type of entity" objects)
# --------------------------------------------------------------------------- #


def test_edge_kind_metadata():
    assert EdgeKind.GENERALIZES.value == 1
    assert EdgeKind.GENERALIZES.verb == "generalizes"
    assert EdgeKind.GENERALIZES.inverse_verb == "is base of"
    assert EdgeKind.GENERALIZES.is_structural
    assert not EdgeKind.USES.is_structural  # behavioural
    assert EdgeKind.from_name("uses") is EdgeKind.USES
    assert EdgeKind.from_name("USES") is EdgeKind.USES
    with pytest.raises(ValueError):
        EdgeKind.from_name("nope")


def test_entity_kind_mapping():
    assert EntityKind.from_symbol_kind("class") is EntityKind.CLASS
    assert EntityKind.from_symbol_kind("struct") is EntityKind.STRUCT
    assert EntityKind.from_symbol_kind("class-template") is EntityKind.CLASS_TEMPLATE
    assert EntityKind.from_symbol_kind("function") is EntityKind.OTHER


# --------------------------------------------------------------------------- #
# Graph-level access
# --------------------------------------------------------------------------- #


def test_stats_and_kinds(eg):
    st = eg.stats()
    assert st["edges"] >= 6
    assert "generalizes" in st["by_kind"]
    assert "composes" in st["by_kind"]
    present = eg.kinds()
    assert EdgeKind.GENERALIZES in present
    assert EdgeKind.COMPOSES in present
    assert EdgeKind.USES in present


def test_entities_and_find(eg):
    names = {n.name for n in eg.entities()}
    assert {"app::Base", "app::Mid", "app::Leaf", "app::Holder"} <= names
    found = eg.find("Holder")
    assert found and found[0].name == "app::Holder"
    assert isinstance(found[0], EntityNode)
    assert found[0].kind is EntityKind.STRUCT


def test_unknown_entity_is_none(eg):
    assert eg.entity(10_000_000) is None


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #


def test_bases_direct_and_transitive(eg):
    leaf = eg.find("app::Leaf")[0]
    assert [n.name for n in leaf.bases()] == ["app::Mid"]
    assert [n.name for n in leaf.bases(transitive=True)] == ["app::Mid", "app::Base"]


def test_derived_direct_and_transitive(eg):
    base = eg.find("app::Base")[0]
    assert [n.name for n in base.derived()] == ["app::Mid"]
    assert {n.name for n in base.derived(transitive=True)} == {"app::Mid", "app::Leaf"}


def test_neighbors_direction(eg):
    mid = eg.find("app::Mid")[0]
    out = {n.name for n in mid.neighbors(EdgeKind.GENERALIZES, "out")}
    inn = {n.name for n in mid.neighbors(EdgeKind.GENERALIZES, "in")}
    both = {n.name for n in mid.neighbors(EdgeKind.GENERALIZES, "both")}
    assert out == {"app::Base"}
    assert inn == {"app::Leaf"}
    assert both == {"app::Base", "app::Leaf"}


# --------------------------------------------------------------------------- #
# Edges and attribute decoding
# --------------------------------------------------------------------------- #


def test_composes_edge_decoded(eg):
    holder = eg.find("app::Holder")[0]
    parts = list(holder.parts())
    assert parts, "expected a composes edge Holder->Part"
    e = parts[0]
    assert isinstance(e, EntityEdge)
    assert e.kind is EdgeKind.COMPOSES
    assert e.dst.name == "app::Part"
    assert e.multiplicity is Multiplicity.ONE
    assert e.access is Access.PUBLIC
    # composes goes through the `part` field
    assert e.via_member is not None
    assert e.via_member.spelling == "part"


def test_uses_edge(eg):
    holder = eg.find("app::Holder")[0]
    used = {e.dst.name for e in holder.uses()}
    assert "app::Leaf" in used


def test_befriends_edge(eg):
    vault = eg.find("app::Vault")[0]
    friends = {n.name for n in vault.friends()}
    assert "app::Holder" in friends


def test_edge_filtering_and_repr(eg):
    gen = eg.by_kind(EdgeKind.GENERALIZES)
    assert all(e.kind is EdgeKind.GENERALIZES for e in gen)
    # filter by src
    leaf = eg.find("app::Leaf")[0]
    leaf_gen = list(eg.edges(kind=EdgeKind.GENERALIZES, src=leaf))
    assert len(leaf_gen) == 1
    assert "generalizes" in repr(leaf_gen[0])
    d = leaf_gen[0].to_dict()
    assert d["src"] == "app::Leaf" and d["dst"] == "app::Mid"


# --------------------------------------------------------------------------- #
# Template-specialization display: a node must render with its template
# arguments so a specialization is distinguishable from its primary template.
# --------------------------------------------------------------------------- #

_CRTP_SOURCE = """
namespace app {
template <class Derived>
class Singleton {
public:
    static Derived& instance() { static Derived d; return d; }
protected:
    Singleton() = default;
private:
    int count_;
};
class Cache : public Singleton<Cache> {
    friend class Singleton<Cache>;
    Cache() = default;
public:
    void put(int k, int v);
    int  get(int k);
};

// A pure interface + a realizer, for the Interface() seed.
struct Drawable {
    virtual void draw() = 0;
    virtual ~Drawable() = default;
};
struct Sprite : Drawable {
    void draw() override {}
};
}  // namespace app
"""


@pytest.fixture
def crtp_eg():
    """A real index over a CRTP singleton (Cache : Singleton<Cache>)."""
    if not _HAS_ROLLUP:
        pytest.skip("entity_rollup not present")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "crtp.cpp")
        with open(path, "w") as fh:
            fh.write(_CRTP_SOURCE)
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


def test_specialization_display_carries_template_args(crtp_eg):
    """display() shows template arguments; name() stays the bare qual_name."""
    cache = crtp_eg.find("app::Cache")[0]
    spec = cache.bases().__next__()  # the Singleton<Cache> specialization

    # The specialization is distinguishable from the primary template.
    assert spec.display == "app::Singleton<app::Cache>"
    assert spec.name == "app::Singleton"  # bare qual_name unchanged
    assert "Singleton<app::Cache>" in repr(spec)

    # Cache (non-template) is unaffected: display == name.
    assert cache.display == cache.name == "app::Cache"


def test_specialization_chain_repr_and_dict(crtp_eg):
    """The full Cache --generalizes--> Singleton<Cache> --instantiates-->
    Singleton<T> chain renders the specialization with its arguments."""
    cache = crtp_eg.find("app::Cache")[0]
    gen = list(cache.out_edges(EdgeKind.GENERALIZES))[0]
    assert (
        repr(gen)
        == "app::Cache --generalizes--> app::Singleton<app::Cache> [protected]"
    )
    assert gen.to_dict()["dst"] == "app::Singleton<app::Cache>"

    inst = list(gen.dst.out_edges(EdgeKind.INSTANTIATES))[0]
    assert inst.to_dict() == {
        "src": "app::Singleton<app::Cache>",
        "kind": "instantiates",
        "dst": "app::Singleton<Derived>",  # primary's param is named Derived here
        "count": inst.count,
    }


def test_template_instances_and_primary(crtp_eg):
    """instances() (instantiates, in) and primary_template() (instantiates, out)."""
    primary = crtp_eg.find("Singleton")[0]
    # find returns primary template first; make sure we have the CLASS_TEMPLATE
    primary = next(
        n for n in crtp_eg.find("Singleton") if n.kind is EntityKind.CLASS_TEMPLATE
    )
    spec = crtp_eg.entity(next(primary.instances()).id)

    assert [n.display for n in primary.instances()] == ["app::Singleton<app::Cache>"]
    assert spec.primary_template().kind is EntityKind.CLASS_TEMPLATE
    assert primary.primary_template() is None  # the primary instantiates nothing


def test_which_classes_are_singletons_declarative(crtp_eg):
    """The motivating query: classes that ARE singletons = subclasses of any
    instantiation of the Singleton primary template."""
    primary = next(
        n for n in crtp_eg.find("Singleton") if n.kind is EntityKind.CLASS_TEMPLATE
    )
    assert primary.query().instances().displays() == ["app::Singleton<app::Cache>"]
    assert primary.query().instances().derived().displays() == ["app::Cache"]


def test_query_relations_round_trip(crtp_eg):
    """New fluent steps invert correctly (instances <-> instantiates)."""
    spec = next(
        n
        for n in crtp_eg.find("Singleton")
        if n.kind is EntityKind.CLASS  # the Singleton<Cache> specialization
    )
    # specialization --instantiates--> primary --instances--> specialization
    assert spec.query().instantiates().instances().displays() == [
        "app::Singleton<app::Cache>"
    ]


# --------------------------------------------------------------------------- #
# Typed query seeds: g.template / g.klass / g.instance / g.interface and the
# selector-object form g.query(ClassTemplate(...)).
# --------------------------------------------------------------------------- #


def test_typed_seed_template_and_klass(crtp_eg):
    # template() seeds only the primary class template.
    assert crtp_eg.template("Singleton").displays() == ["app::Singleton<Derived>"]
    # klass() seeds a plain class, excluding the Singleton<Cache> instantiation.
    assert crtp_eg.klass("Cache").displays() == ["app::Cache"]
    assert crtp_eg.klass("Singleton").displays() == []  # only a template/instance


def test_typed_seed_instance_matches_by_display(crtp_eg):
    # instance() matches the instantiation by its (unqualified) display string.
    assert crtp_eg.instance("Singleton<app::Cache>").displays() == [
        "app::Singleton<app::Cache>"
    ]
    # ...and the qualified display works too.
    assert crtp_eg.instance("app::Singleton<app::Cache>").displays() == [
        "app::Singleton<app::Cache>"
    ]


def test_typed_seed_declarative_singletons(crtp_eg):
    # The motivating query reads cleanly with a typed seed.
    assert crtp_eg.template("Singleton").instances().derived().displays() == [
        "app::Cache"
    ]
    # Selector-object form via query() is equivalent.
    assert crtp_eg.query(ClassTemplate("Singleton")).instances().derived().displays() == [
        "app::Cache"
    ]


def test_typed_seed_interface_implemented_by(crtp_eg):
    # interface() seeds a record realized by others; implemented_by() finds them.
    assert crtp_eg.interface("Drawable").implemented_by().displays() == ["app::Sprite"]
    # query(Interface(...)) is the selector-object equivalent.
    assert crtp_eg.query(Interface("Drawable")).implemented_by().displays() == [
        "app::Sprite"
    ]


# --------------------------------------------------------------------------- #
# The three kinds of class: concrete / abstract / interface (ClassKind).
# --------------------------------------------------------------------------- #

_CLASSKINDS_SOURCE = """
namespace app {
struct Shape {                              // INTERFACE: all pure, no fields
    virtual double area() const = 0;
    virtual ~Shape() = default;
};
class Widget {                              // ABSTRACT: pure method + a field
public:
    virtual void draw() = 0;
private:
    int z = 0;
};
class Button : public Widget {              // CONCRETE: overrides draw
public:
    void draw() override {}
};
struct Circle : Shape {                     // CONCRETE: overrides area
    double area() const override { return 3.14; }
};
}  // namespace app
"""


@pytest.fixture
def kinds_eg():
    if not _HAS_ROLLUP:
        pytest.skip("entity_rollup not present")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "kinds.cpp")
        with open(path, "w") as fh:
            fh.write(_CLASSKINDS_SOURCE)
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


def test_class_kind_classification(kinds_eg):
    def ck(name):
        return kinds_eg.find("app::" + name)[0].class_kind

    assert ck("Shape") is ClassKind.INTERFACE
    assert ck("Widget") is ClassKind.ABSTRACT
    assert ck("Button") is ClassKind.CONCRETE
    assert ck("Circle") is ClassKind.CONCRETE


def test_is_abstract_is_interface_flags(kinds_eg):
    shape = kinds_eg.find("app::Shape")[0]
    widget = kinds_eg.find("app::Widget")[0]
    button = kinds_eg.find("app::Button")[0]
    assert shape.is_interface and shape.is_abstract
    assert widget.is_abstract and not widget.is_interface  # abstract != interface
    assert not button.is_abstract and not button.is_interface


def test_typed_seeds_partition_the_three_class_kinds(kinds_eg):
    # interface() / abstract_class() / klass() are mutually exclusive.
    assert kinds_eg.interface("Shape").displays() == ["app::Shape"]
    assert kinds_eg.abstract_class("Widget").displays() == ["app::Widget"]
    assert kinds_eg.klass("Button").displays() == ["app::Button"]
    # Widget is abstract -> NOT matched by klass(); Shape is an interface ->
    # NOT matched by abstract_class().
    assert kinds_eg.klass("Widget").displays() == []
    assert kinds_eg.abstract_class("Shape").displays() == []


def test_query_class_kind_filters(kinds_eg):
    assert kinds_eg.query().interfaces().displays() == ["app::Shape"]
    assert kinds_eg.query().abstract().displays() == ["app::Shape", "app::Widget"]
    assert kinds_eg.query().concrete().displays() == ["app::Button", "app::Circle"]
    assert kinds_eg.query().of_class_kind(ClassKind.ABSTRACT).displays() == [
        "app::Widget"
    ]


def test_class_kind_in_to_dict(kinds_eg):
    d = kinds_eg.find("app::Shape")[0].to_dict()
    assert d["class_kind"] == "interface" and d["kind"] == "struct"

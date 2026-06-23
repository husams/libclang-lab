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

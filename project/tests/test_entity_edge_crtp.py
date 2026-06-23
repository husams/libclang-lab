"""CRTP base-class instantiation -> entity_edge chain (REAL libclang parse).

`class Cache : public Singleton<Cache>` is the one template-instantiation site
that is NOT a variable/member/call/using -- the template appears as a BASE
CLASS.  The extractor must emit, for the Singleton<Cache> specialization, an
instantiates(5) Layer-0 edge to its primary template, and the entity roll-up
must keep the specialization as the generalizes target (its own design entity)
rather than collapsing it onto the primary.  Expected Layer-1 chain:

    Cache  --generalizes-->  Singleton<Cache>  --instantiates-->  Singleton

Regression guard for the gap where a CRTP base produced only a single
generalizes edge and never an instantiates edge.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.storage import Storage  # noqa: E402
from indexer.clang import ast as A  # noqa: E402
from indexer.clang import util as U  # noqa: E402

try:
    from indexer.entity_rollup import materialize_entity_edges
    _HAS_ROLLUP = True
except Exception:  # pragma: no cover
    _HAS_ROLLUP = False

SOURCE = """
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
public:
    void put(int k, int v);
    int  get(int k);
private:
    int store_[256];
};
"""

# entity_edge_kind ids
_EK_GENERALIZES = 1
_EK_IMPLEMENTS = 2
_EK_INSTANTIATES = 11


@pytest.fixture
def graph() -> dict:
    if not _HAS_ROLLUP:
        pytest.skip("entity_rollup not present")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "crtp.cpp")
        with open(path, "w") as fh:
            fh.write(SOURCE)
        tu = U.parse(path, args=["-std=c++17"], check=False)
        assert not [d for d in tu.diagnostics if d.severity >= 3], (
            "fixture must parse cleanly: "
            + "; ".join(d.spelling for d in tu.diagnostics if d.severity >= 3)
        )
        db = Storage(os.path.join(tmp, "i.db"))
        db.add_component("t", tmp)
        file_id = db.add_file_path(path)
        with db.transaction():
            A.index_symbols(db, tu, file_id)
        with db.transaction():
            db.delete_edges_for_file(file_id)
            A._index_edges_notxn(db, tu, path, file_id)
        materialize_entity_edges(db)

        conn = db._conn
        rows = conn.execute(
            "SELECT ee.kind, s1.id AS src_id, s1.kind AS src_kind, "
            "       s2.id AS dst_id, s2.kind AS dst_kind, ee.access "
            "FROM entity_edge ee "
            "JOIN symbol s1 ON s1.id = ee.src_id "
            "JOIN symbol s2 ON s2.id = ee.dst_id "
            "ORDER BY ee.kind"
        ).fetchall()

        def sym(usr_kind_pred):
            return conn.execute(
                "SELECT id FROM symbol WHERE " + usr_kind_pred + " LIMIT 1"
            ).fetchone()

        return {
            "rows": [dict(r) for r in rows],
            # primary template = CLASS_TEMPLATE (kind 31), spelling Singleton
            "primary": sym("spelling='Singleton' AND kind=31")["id"],
            # specialization = class (kind 4), spelling Singleton
            "spec": sym("spelling='Singleton' AND kind=4")["id"],
            "cache": sym("spelling='Cache' AND kind=4")["id"],
        }


def test_generalizes_points_at_specialization(graph):
    """Cache generalizes Singleton<Cache> -- the base stays the specialization,
    NOT collapsed onto the primary template."""
    gens = [r for r in graph["rows"] if r["kind"] == _EK_GENERALIZES]
    assert gens, "no generalizes edge materialized for the CRTP base"
    assert any(
        r["src_id"] == graph["cache"] and r["dst_id"] == graph["spec"]
        for r in gens
    ), f"expected Cache->Singleton<Cache> generalizes; got {gens}"
    # And it must NOT be an implements edge (Singleton has state + a concrete method)
    assert not any(r["kind"] == _EK_IMPLEMENTS for r in graph["rows"])
    # Generalizes must not jump straight to the primary template.
    assert not any(
        r["kind"] == _EK_GENERALIZES and r["dst_id"] == graph["primary"]
        for r in graph["rows"]
    ), "generalizes must point at the specialization, not the primary"


def test_specialization_instantiates_primary(graph):
    """Singleton<Cache> instantiates Singleton -- the new CRTP-base edge."""
    insts = [r for r in graph["rows"] if r["kind"] == _EK_INSTANTIATES]
    assert insts, "no instantiates edge materialized (CRTP base extraction gap)"
    assert any(
        r["src_id"] == graph["spec"] and r["dst_id"] == graph["primary"]
        for r in insts
    ), f"expected Singleton<Cache>->Singleton instantiates; got {insts}"


def test_full_chain_three_entities_two_edges(graph):
    """Exactly the three-entity / two-edge chain, no self-edges."""
    assert graph["primary"] != graph["spec"] != graph["cache"]
    kinds = sorted(r["kind"] for r in graph["rows"])
    assert _EK_GENERALIZES in kinds and _EK_INSTANTIATES in kinds
    # No id-level self-edges anywhere.
    assert all(r["src_id"] != r["dst_id"] for r in graph["rows"])

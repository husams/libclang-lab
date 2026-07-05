"""Namespace canonicalization (v26 / product 0.45.0).

Drives the REAL libclang extraction + resolve path over a namespace that is
REOPENED across two translation units, asserting the four capabilities:

  1. one canonical Namespace whose members aggregate across every reopening,
     reachable by the exact getter CodeBase.namespace(name);
  2. Entity.declaration_sites() lists every reopen site (v26 decl_site);
  3. Entity.references() lists every qualified/using use site (namespace uses
     edges, kind 7);
  4. the namespace is a first-class entity_node with DIRECT `declares` entity
     edges -- and ABC is NOT the same as ABC::XXX (content is not recursive).
"""

from __future__ import annotations

import os
import tempfile

import pytest

from indexer.storage import Storage
from indexer.clang import ast as A
from indexer.clang import util as U
from indexer.query import GraphQuery
from indexer.model import CodeBase, Namespace


# demo is REOPENED in both files; demo::inner is a nested namespace (distinct
# entity). file_b both reopens demo AND references it via a qualifier.
FILE_A = r"""
namespace demo {
    struct Point { int x; int y; };
    enum Kind { A, B };
    int helper(int v) { return v; }
    namespace inner {
        struct Deep { int d; };
    }
}
"""

FILE_B = r"""
namespace demo {                 // reopened here
    struct Widget { int w; };
    int gizmo(int n) { return n; }
    int use_widget() {
        demo::Widget x{5};       // qualified use of demo  -> uses edge
        return x.w + demo::gizmo(1);
    }
}
"""


@pytest.fixture
def cb():
    """Index FILE_A + FILE_B for real, resolve, and yield a CodeBase."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "i.db")
        db = Storage(db_path)
        db.add_component("t", tmp)
        for name, src in (("a.cpp", FILE_A), ("b.cpp", FILE_B)):
            path = os.path.join(tmp, name)
            with open(path, "w") as fh:
                fh.write(src)
            tu = U.parse(path, args=["-std=c++17"], check=False)
            fatal = [d.spelling for d in tu.diagnostics if d.severity >= 3]
            assert not fatal, f"{name} must parse cleanly: " + "; ".join(fatal)
            file_id = db.add_file_path(path)
            with db.transaction():
                A.index_symbols(db, tu, file_id)
            with db.transaction():
                db.delete_edges_for_file(file_id)
                A._index_edges_notxn(db, tu, path, file_id)
        db.resolve_pass()
        db.close()

        c = CodeBase(GraphQuery(db_path))
        try:
            yield c
        finally:
            c.close()


# --------------------------------------------------------------------------- #
# 1. canonical getter + cross-TU member aggregation
# --------------------------------------------------------------------------- #


def test_namespace_getter_returns_one_canonical(cb):
    ns = cb.namespace("demo")
    assert isinstance(ns, Namespace)
    assert ns.name == "demo"


def test_getter_is_exact_not_fuzzy(cb):
    # demo::inner shares the "demo" prefix but must NOT come back from the
    # exact getter -- ABC and ABC::XXX are distinct entities.
    assert cb.namespace("demo").id != cb.namespace("demo::inner").id
    assert cb.namespace("nope") is None


def test_members_aggregate_across_reopenings(cb):
    ns = cb.namespace("demo")
    names = sorted(m.spelling for m in ns.members())
    # Point/Kind/helper/inner from file A, Widget/use_point from file B --
    # unified onto the one canonical namespace despite two TUs.
    for expected in ("Point", "Kind", "helper", "Widget", "use_widget", "inner"):
        assert expected in names, f"{expected} missing from {names}"


def test_content_is_not_recursive(cb):
    # demo's direct members include the nested namespace `inner` but NOT
    # inner's own member `Deep`.
    direct = {m.spelling for m in cb.namespace("demo").members()}
    assert "inner" in direct
    assert "Deep" not in direct
    # Deep lives under demo::inner, reached only by navigating in.
    assert "Deep" in {m.spelling for m in cb.namespace("demo::inner").members()}


# --------------------------------------------------------------------------- #
# 2. declaration_sites: every reopening
# --------------------------------------------------------------------------- #


def test_declaration_sites_list_every_reopening(cb):
    sites = cb.namespace("demo").declaration_sites()
    files = {os.path.basename(s.file.path) for s in sites if s.file}
    assert files == {"a.cpp", "b.cpp"}, f"expected both TUs, got {files}"


# --------------------------------------------------------------------------- #
# 3. references: qualified / using uses
# --------------------------------------------------------------------------- #


def test_references_include_qualified_uses(cb):
    refs = cb.namespace("demo").references()
    # use_widget() qualifies demo:: twice (demo::Widget, demo::gizmo).
    users = {r.by.spelling for r in refs}
    assert "use_widget" in users, f"expected use_widget among {users}"
    sites = [s.loc for r in refs for s in r.sites]
    assert sites, "references() must carry concrete sites"


# --------------------------------------------------------------------------- #
# 4. entity-node rollup: namespace node + DIRECT declares edges
# --------------------------------------------------------------------------- #


def test_namespace_is_entity_node_with_declares(cb):
    conn = cb.graph._c
    demo = cb.namespace("demo")
    # entity_node row, kind 9 = namespace
    row = conn.execute(
        "SELECT kind FROM entity_node WHERE id = ?", (demo.id,)
    ).fetchone()
    assert row is not None and row[0] == 9

    # declares (kind 12) targets: demo directly declares Point/Kind/Widget/inner
    # (records/enum/nested namespace) -- NOT demo::inner::Deep.
    declared = {
        r[0]
        for r in conn.execute(
            "SELECT dst.spelling FROM entity_edge ee "
            "JOIN symbol dst ON dst.id = ee.dst_id "
            "WHERE ee.kind = 12 AND ee.src_id = ?",
            (demo.id,),
        )
    }
    assert {"Point", "Kind", "Widget", "inner"} <= declared, declared
    assert "Deep" not in declared


def test_declares_is_direct_only(cb):
    # demo::inner declares Deep; demo does NOT (non-recursive containment).
    conn = cb.graph._c
    inner = cb.namespace("demo::inner")
    inner_declares = {
        r[0]
        for r in conn.execute(
            "SELECT dst.spelling FROM entity_edge ee "
            "JOIN symbol dst ON dst.id = ee.dst_id "
            "WHERE ee.kind = 12 AND ee.src_id = ?",
            (inner.id,),
        )
    }
    assert "Deep" in inner_declares

"""End-to-end entity_edge test: REAL libclang parse -> Layer-0 extraction ->
materialize_entity_edges -> assert the rolled-up Layer-1 rows.

Unlike test_entity_edge_pr2.py (which injects synthetic Layer-0 edges), this
test drives the ACTUAL ast.py extraction path on a real C++ TU, so it locks the
full pipeline: the PR1 construct/destroy/friend handlers in clang/ast.py must
emit the Layer-0 form edges (10-16 + friend 17) that the roll-up reads. It is
the regression guard for the failure mode where the schema + roll-up exist but
the extraction is not wired (entity_edge would then stay empty for these kinds).
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

import clang.cindex as cx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from _helpers import clang_args  # noqa: E402

from indexer.storage import Storage  # noqa: E402
from indexer.clang import ast as A  # noqa: E402

try:
    from indexer.entity_rollup import materialize_entity_edges
    _HAS_ROLLUP = True
except Exception:  # pragma: no cover
    _HAS_ROLLUP = False

# All construction/destruction forms inside class methods (so they roll up to
# the enclosing record), plus a nested record and a friend declaration. No
# <memory> dependency: the factory form rolls up from free functions only and
# is intentionally excluded here.
SOURCE = """
namespace gl {

struct Widget {
    int value;
    Widget() = default;
    explicit Widget(int v) : value(v) {}
    Widget(const Widget &) = default;
    Widget(Widget &&) = default;
    ~Widget() = default;
};

struct Pool {
    void make_value(int x)  { Widget w(x); (void)w; }
    void make_temp(int x)   { (void)Widget(x); }
    void make_copy(const Widget &s) { Widget c(s); (void)c; }
    void make_move(Widget s) { Widget m(static_cast<Widget &&>(s)); (void)m; }
    void make_and_destroy(int x) { Widget *p = new Widget(x); delete p; }
};

struct Outer {
    struct Inner { int depth; };
    Inner inner;
};

class Vault {
    friend class Pool;
    int secret;
};

}  // namespace gl
"""


@pytest.fixture
def edges() -> dict:
    """Index the inline TU for real, materialize, and return {(kind): rows}."""
    if not _HAS_ROLLUP:
        pytest.skip("entity_rollup not present")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fixture.cpp")
        with open(path, "w") as fh:
            fh.write(SOURCE)
        tu = cx.Index.create().parse(path, args=clang_args(path) + ["-std=c++17"])
        assert not [d for d in tu.diagnostics if d.severity >= 3], (
            "fixture source must parse cleanly"
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

        def by_kind(kind: int):
            return conn.execute(
                "SELECT s1.spelling AS src, s2.spelling AS dst, "
                "       ee.create_form, ee.partial "
                "FROM entity_edge ee "
                "JOIN symbol s1 ON s1.id = ee.src_id "
                "JOIN symbol s2 ON s2.id = ee.dst_id "
                "WHERE ee.kind = ? ORDER BY ee.create_form",
                (kind,),
            ).fetchall()

        return {
            "creates": by_kind(7),
            "destroys": by_kind(9),
            "nests": by_kind(10),
            "befriends": by_kind(11),
        }


def test_creates_all_forms_from_methods(edges):
    """Pool methods construct Widget by value/temp/heap/copy/move -> creates(7)."""
    rows = edges["creates"]
    assert rows, "no creates(7) rows materialized -- PR1 extraction not wired?"
    assert all(r["src"] == "Pool" and r["dst"] == "Widget" for r in rows)
    forms = {r["create_form"] for r in rows}
    # value=3, temp=4, heap=5, copy=7, move=8 (factory=6 excluded: free fn only)
    for f in (3, 4, 5, 7, 8):
        assert f in forms, f"create_form {f} missing (got {sorted(forms)})"


def test_destroys_from_method(edges):
    """Pool::make_and_destroy `delete p` -> destroys(9) Pool->Widget."""
    rows = edges["destroys"]
    assert any(r["src"] == "Pool" and r["dst"] == "Widget" for r in rows), (
        "destroys(9) Pool->Widget missing"
    )


def test_nests_record_in_record(edges):
    """Outer::Inner -> nests(10) Outer->Inner."""
    rows = edges["nests"]
    assert any(r["src"] == "Outer" and r["dst"] == "Inner" for r in rows), (
        "nests(10) Outer->Inner missing"
    )


def test_befriends_friend_decl(edges):
    """`class Vault { friend class Pool; }` -> befriends(11) Vault->Pool."""
    rows = edges["befriends"]
    assert any(r["src"] == "Vault" and r["dst"] == "Pool" for r in rows), (
        "befriends(11) Vault->Pool missing -- FRIEND_DECL extraction not wired?"
    )

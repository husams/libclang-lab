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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from indexer.storage import Storage  # noqa: E402
from indexer.clang import ast as A  # noqa: E402
from indexer.clang import util as U  # noqa: E402

try:
    from indexer.entity_rollup import materialize_entity_edges
    _HAS_ROLLUP = True
except Exception:  # pragma: no cover
    _HAS_ROLLUP = False

# All construction/destruction forms inside class methods (so they roll up to
# the enclosing record), plus a method-scoped factory (make_owned ->
# make_unique<Widget>, create_form=6), a nested record and a friend declaration.
SOURCE = """
#include <memory>

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
    std::unique_ptr<Widget> make_owned(int x) { return std::make_unique<Widget>(x); }
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
        # Use the indexer's own parse (C++-correct -isystem ordering) so the
        # std::make_unique factory site in <memory> resolves cleanly -- the lab's
        # simple clang_args() puts the clang builtin -I before libc++ and trips the
        # <cstddef> ordering gotcha for C++ TUs.
        tu = U.parse(path, args=["-std=c++17"], check=False)
        assert not [d for d in tu.diagnostics if d.severity >= 3], (
            "fixture source must parse cleanly: "
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
            "befriends": by_kind(10),
        }


def test_creates_all_forms_from_methods(edges):
    """Pool methods construct Widget by value/temp/heap/copy/move/factory -> creates(7)."""
    rows = edges["creates"]
    assert rows, "no creates(7) rows materialized -- PR1 extraction not wired?"
    assert all(r["src"] == "Pool" and r["dst"] == "Widget" for r in rows)
    forms = {r["create_form"] for r in rows}
    # value=3, temp=4, heap=5, factory=6, copy=7, move=8
    for f in (3, 4, 5, 6, 7, 8):
        assert f in forms, f"create_form {f} missing (got {sorted(forms)})"


def test_creates_factory_form_from_method(edges):
    """Pool::make_owned -> make_unique<Widget> rolls up creates(7, form=6, partial=1).

    BUG 2 regression guard: the factory form (15 -> create_form 6) is only
    rolled up from a METHOD owner. graphlab's free-function make_unique/make_shared
    sites have no owner record, so without a method-scoped factory site this path
    was never exercised end-to-end.
    """
    rows = [r for r in edges["creates"] if r["create_form"] == 6]
    assert rows, "creates(7, create_form=6) factory roll-up missing from a method"
    assert all(r["src"] == "Pool" and r["dst"] == "Widget" for r in rows)
    assert all(r["partial"] == 1 for r in rows), "factory creates must be partial=1"


def test_destroys_from_method(edges):
    """Pool::make_and_destroy `delete p` -> destroys(9) Pool->Widget."""
    rows = edges["destroys"]
    assert any(r["src"] == "Pool" and r["dst"] == "Widget" for r in rows), (
        "destroys(9) Pool->Widget missing"
    )


def test_befriends_friend_decl(edges):
    """`class Vault { friend class Pool; }` -> befriends(10) Vault->Pool."""
    rows = edges["befriends"]
    assert any(r["src"] == "Vault" and r["dst"] == "Pool" for r in rows), (
        "befriends(10) Vault->Pool missing -- FRIEND_DECL extraction not wired?"
    )


# ---------------------------------------------------------------------------
# Stage 1: instantiates(11) vs specializes(3) on a REAL parse
# ---------------------------------------------------------------------------

# An explicit instantiation (`template class X<int>;`) is an INSTANCE that
# `instantiates` its primary; an explicit specialization (`template<> class
# X<bool>{...}`) is a separate body that `specializes` its primary. The two are
# mutually exclusive and BOTH keep the instance/spec as its own design entity
# (the source is never collapsed onto the primary).
_TMPL_SOURCE = """
namespace tg {

template <typename T>
class Box {
    T value_;
public:
    explicit Box(T v) : value_(v) {}
    const T& get() const { return value_; }
};

// EXPLICIT SPECIALIZATION: a separate body -> specializes(3) Box<bool> -> Box.
template <>
class Box<bool> {
    bool flag_;
public:
    explicit Box(bool v) : flag_(v) {}
    bool get() const { return flag_; }
};

// EXPLICIT INSTANTIATION: a concrete instance -> instantiates(11) Box<int> -> Box.
template class Box<int>;

}  // namespace tg
"""


@pytest.fixture
def tmpl_edges() -> dict:
    """Index the template TU for real, materialize, and return rows by kind."""
    if not _HAS_ROLLUP:
        pytest.skip("entity_rollup not present")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "tmpl.cpp")
        with open(path, "w") as fh:
            fh.write(_TMPL_SOURCE)
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

        def by_kind(kind: int):
            return conn.execute(
                "SELECT s1.display_name AS src, s2.display_name AS dst "
                "FROM entity_edge ee "
                "JOIN symbol s1 ON s1.id = ee.src_id "
                "JOIN symbol s2 ON s2.id = ee.dst_id "
                "WHERE ee.kind = ? ORDER BY src",
                (kind,),
            ).fetchall()

        return {"specializes": by_kind(3), "instantiates": by_kind(11)}


def test_instantiates_fires_for_explicit_instantiation(tmpl_edges):
    """`template class Box<int>;` -> instantiates(11) Box<int> -> Box<T>."""
    rows = tmpl_edges["instantiates"]
    assert any(r["src"] == "Box<int>" and r["dst"] == "Box<T>" for r in rows), (
        f"instantiates(11) Box<int> -> Box<T> missing; got {[(r['src'], r['dst']) for r in rows]}"
    )
    # An instantiation is NEVER recorded as a specialization.
    assert not any(r["src"] == "Box<int>" for r in tmpl_edges["specializes"]), (
        "Box<int> must NOT appear as a specializes(3) source"
    )


def test_specializes_fires_for_explicit_specialization(tmpl_edges):
    """`template<> class Box<bool>{...}` -> specializes(3) Box<bool> -> Box<T>."""
    rows = tmpl_edges["specializes"]
    assert any(r["src"] == "Box<bool>" and r["dst"] == "Box<T>" for r in rows), (
        f"specializes(3) Box<bool> -> Box<T> missing; got {[(r['src'], r['dst']) for r in rows]}"
    )
    # A specialization is NEVER recorded as an instantiation.
    assert not any(r["src"] == "Box<bool>" for r in tmpl_edges["instantiates"]), (
        "Box<bool> must NOT appear as an instantiates(11) source"
    )

"""Integration test for dependent-call recovery inside template bodies.

Drives the REAL libclang extractor (like test_type_uses.py): parses a small C++
source and asserts that a call to a DEPENDENT/overloaded name inside a template
method body earns a `calls` edge to the primary template, even though
`CALL_EXPR.referenced` is None for such a call.

This is the regression guard for indexer.clang.ast._recover_overloaded_callee:
without it, a template method like `Stack<T>::summary()` that calls a function
template `combine<T>()` records no callee at all.
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


SOURCE = """
namespace nn {

template <class T> T combine(T a, T b) { return a + b; }
template <class T> int describe(const T&) { return 0; }

template <class T>
struct Stack {
    T data_[4];
    int n_ = 0;
    // dependent calls inside a template body: combine<T> + describe<T>
    int summary() const {
        T acc = data_[0];
        for (int i = 1; i < n_; ++i) acc = combine(acc, data_[i]);
        return describe(acc);
    }
};

// ambiguous overload set must NOT be linked (no wrong guess)
int over(int);
double over(double);
template <class T> int caller(T v) { return (int)over(v); }

}  // namespace nn
"""


@pytest.fixture
def edges():
    """(calls_map, db) where calls_map[src_qual] = {dst_qual, ...}."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.cpp")
        with open(path, "w") as fh:
            fh.write(SOURCE)
        tu = cx.Index.create().parse(path, args=clang_args(path) + ["-std=c++17"])
        assert not [d for d in tu.diagnostics if d.severity >= 3], \
            "fixture source must parse cleanly"

        db = Storage(os.path.join(tmp, "i.db"))
        db.add_component("t", tmp)
        file_id = db.add_file_path(path)
        with db.transaction():
            A.index_symbols(db, tu, file_id)
        with db.transaction():
            db.delete_edges_for_file(file_id)
            A._index_edges_notxn(db, tu, path, file_id)

        rows = db._conn.execute(
            "SELECT a.qual_name, b.qual_name FROM edge e "
            "JOIN symbol a ON e.src_id = a.id "
            "JOIN symbol b ON e.dst_id = b.id "
            "WHERE e.kind = 1").fetchall()        # kind 1 = calls
        out: dict[str, set[str]] = {}
        for src, dst in rows:
            out.setdefault(src, set()).add(dst)
        yield out


def test_template_method_calls_function_template(edges):
    # Stack<T>::summary -> combine (recovered from the dependent CALL_EXPR)
    assert "nn::combine" in edges.get("nn::Stack::summary", set())


def test_template_method_calls_second_function_template(edges):
    # Stack<T>::summary -> describe
    assert "nn::describe" in edges.get("nn::Stack::summary", set())


def test_ambiguous_overload_is_not_linked(edges):
    # `over(v)` in caller<T> is an overload SET (int/double) -> must not guess.
    assert "nn::over" not in edges.get("nn::caller", set())

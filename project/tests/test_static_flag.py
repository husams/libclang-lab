"""Integration test for the v12 `is_static` symbol flag.

Drives the REAL libclang extractor over a small C++ source and asserts that a
`static` member function is recorded with is_static=1 while instance methods and
free functions are 0. A file-scope `static` free function stays is_static=0 (its
static-ness is reflected by linkage='internal', not by this method-only flag).

Covers the same gap in both the storage round-trip and the model layer.
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
from indexer.query import GraphQuery  # noqa: E402
from indexer.model import CodeBase, Method  # noqa: E402


SOURCE = """
namespace ns {
  struct Widget {
    static int make(int x);   // static member function -> is_static
    int area() const;         // instance method        -> not static
  };
  int Widget::make(int x) { return x; }
  int Widget::area() const { return 0; }
  int free_fn(int x) { return x; }                 // external free function
  static int hidden_fn(int x) { return x; }        // internal-linkage free fn
}
"""


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "w.cpp")
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
        yield db._conn.execute("PRAGMA database_list").fetchone()[2]


def _by_spelling(conn, spelling):
    # kind is stored as a CXCursorKind int (v16); join symbol_kind for the name.
    return conn.execute(
        "SELECT s.is_static, sk.name, s.linkage FROM symbol s "
        "JOIN symbol_kind sk ON sk.id = s.kind WHERE s.spelling = ?",
        (spelling,),
    ).fetchone()


def test_static_member_method_is_flagged(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    row = _by_spelling(conn, "make")
    assert row is not None and row[1] == "method"
    assert row[0] == 1, "static member function must record is_static=1"


def test_instance_method_is_not_static(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    row = _by_spelling(conn, "area")
    assert row is not None and row[1] == "method"
    assert row[0] == 0, "instance method must record is_static=0"


def test_free_functions_are_not_static(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    extern = _by_spelling(conn, "free_fn")
    hidden = _by_spelling(conn, "hidden_fn")
    assert extern is not None and extern[0] == 0 and extern[1] == "function"
    # file-scope `static` free fn: is_static=0, but internal linkage marks it
    assert hidden is not None and hidden[0] == 0 and hidden[1] == "function"
    assert hidden[2] == "internal", "file-scope static -> linkage='internal'"


def test_model_layer_exposes_is_static(db_path):
    with GraphQuery(db_path) as g:
        cb = CodeBase(g)
        widget = cb.klass("Widget")
        assert widget is not None
        methods = {m.spelling: m for m in widget.methods}
        assert isinstance(methods["make"], Method)
        assert methods["make"].is_static is True
        assert methods["area"].is_static is False


def test_sym_to_dict_carries_is_static(db_path):
    with GraphQuery(db_path) as g:
        make = g.by_name("make", kind="method")[0]
        assert make.is_static is True
        assert make.to_dict()["is_static"] is True

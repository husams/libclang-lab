"""Regression tests for member-function-template graph edges.

Two indexer bugs, both specific to MEMBER function templates, fixed together:

1. A call to a member function template inside a (dependent) template body
   (`box.put(k, v)` in `roundtrip<T>`) has `CALL_EXPR.referenced is None`;
   overload-recovery finds the right cursor, but libclang emits an INCONSISTENT
   USR for it (parameter types collapse, e.g. `const std::string&` -> `I`), so a
   USR lookup misses. The recovered path now falls back to (qualified name +
   kind) and links when the match is unambiguous -> the `calls` edge appears
   (so callers/refs are non-empty).

2. A member function template (cursor kind FUNCTION_TEMPLATE) never hit the
   CXX_METHOD `method_of` block, so it lacked a `method_of` edge and was invisible
   to method-oriented queries. It now gets `method_of`.

The cross-header case (3) also guards the index_headers TWO-PASS ordering: the
header declaring `roundtrip` is included BEFORE the header declaring `Box::put`,
so the target symbol only exists if all header symbols are minted before any
header's edges are extracted.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from _helpers import clang_args  # noqa: E402

from indexer.storage import Storage  # noqa: E402
from indexer.clang import ast as A  # noqa: E402


# Single translation unit: a member function template called from a function
# template, plus the std::string parameter that triggers the USR mismatch.
SINGLE = """
#include <string>
namespace mm {

class Box {
    int n_ = 0;
public:
    template <class T> void put(const std::string& k, T v) {
        n_ += static_cast<int>(sizeof(v)) + static_cast<int>(k.size());
    }
    template <class T> T get(const std::string&) const { return T(); }
};

template <class T>
T roundtrip(Box& b, const std::string& k, T v) {
    b.put(k, v);          // -> Box::put  (dependent, recovered, USR mismatch)
    return b.get<T>(k);   // -> Box::get
}

}  // namespace mm
"""


def _edges_for_source(tmp, src):
    path = os.path.join(tmp, "t.cpp")
    with open(path, "w") as fh:
        fh.write(src)
    # Use the indexer's own parse helper (same arg handling as real indexing) so
    # libc++ header search paths resolve for the <string> include.
    tu = A.parse(path, clang_args(path) + ["-std=c++17"])
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
    return db


def _edge_map(db, kind):
    rows = db._conn.execute(
        "SELECT a.qual_name, b.qual_name FROM edge e "
        "JOIN symbol a ON e.src_id = a.id "
        "JOIN symbol b ON e.dst_id = b.id "
        "WHERE e.kind = ?",
        (kind,),
    ).fetchall()
    out: dict[str, set[str]] = {}
    for src, dst in rows:
        out.setdefault(src, set()).add(dst)
    return out


@pytest.fixture
def single_db():
    with tempfile.TemporaryDirectory() as tmp:
        yield _edges_for_source(tmp, SINGLE)


def test_calls_into_member_template(single_db):
    calls = _edge_map(single_db, 1)  # kind 1 = calls
    assert "mm::Box::put" in calls.get("mm::roundtrip", set())
    assert "mm::Box::get" in calls.get("mm::roundtrip", set())


def test_member_template_has_method_of(single_db):
    method_of = _edge_map(single_db, 9)  # kind 9 = method_of
    assert "mm::Box" in method_of.get("mm::Box::put", set())
    assert "mm::Box" in method_of.get("mm::Box::get", set())


# --- cross-header ordering (index_headers two-pass) ------------------------- #

B_HPP = """
#ifndef MM_B_HPP
#define MM_B_HPP
#include <string>
namespace mm {
class Box {
    int n_ = 0;
public:
    template <class T> void put(const std::string& k, T v) {
        n_ += static_cast<int>(sizeof(v)) + static_cast<int>(k.size());
    }
    template <class T> T get(const std::string&) const { return T(); }
};
}  // namespace mm
#endif
"""

# a.hpp is included BEFORE b.hpp (it includes b.hpp), yet references Box::put.
A_HPP = """
#ifndef MM_A_HPP
#define MM_A_HPP
#include "b.hpp"
namespace mm {
template <class T>
T roundtrip(Box& b, const std::string& k, T v) {
    b.put(k, v);
    return b.get<T>(k);
}
}  // namespace mm
#endif
"""

MAIN = """
#include "a.hpp"
namespace mm {
int use() {
    Box b;
    return roundtrip<int>(b, "x", 5);
}
}  // namespace mm
"""


def test_cross_header_member_template_call_ordering():
    """roundtrip (a.hpp, included first) -> Box::put (b.hpp, included later).

    Guards the index_headers two-pass: edges for a.hpp are extracted only after
    b.hpp's symbols exist, so the recovered call to Box::put resolves.
    """
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in (("b.hpp", B_HPP), ("a.hpp", A_HPP), ("main.cpp", MAIN)):
            with open(os.path.join(tmp, name), "w") as fh:
                fh.write(body)
        main = os.path.join(tmp, "main.cpp")
        db = Storage(os.path.join(tmp, "i.db"))
        db.add_component("proj", tmp)
        file_id = db.add_file_path(main)
        A.index_source(db, main, clang_args(main) + ["-std=c++17"], file_id)

        calls = _edge_map(db, 1)
        assert "mm::Box::put" in calls.get("mm::roundtrip", set()), (
            "cross-header member-template call must be linked (two-pass ordering)"
        )
        method_of = _edge_map(db, 9)
        assert "mm::Box" in method_of.get("mm::Box::put", set())

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


# --- cross-TU wrong-order indexing (USR-keyed stub + backfill) -------------- #
#
# A dependent call to a member function template whose DEFINING TU is indexed
# AFTER the consuming TU must still link, because the callee USR is TU-invariant:
# the consuming TU mints a USR-keyed stub, and the later index of the cache TU
# backfills the same USR. Covers BOTH the single-candidate recovered path (`get`)
# and the multi-candidate overloaded path (`set`, two overloads).

CACHE_HPP = """
#ifndef MM_CACHE_HPP
#define MM_CACHE_HPP
#include <string>
namespace mm {
class Cache {
    int n_ = 0;
public:
    template <class T> void set(const std::string& k, T v) { n_ += (int)sizeof(v); }
    template <class T> void set(const std::string& k, const T* p) { n_ += (int)k.size(); }
    template <class T> T get(const std::string&) const { return T(); }
};
}  // namespace mm
#endif
"""

CACHE_CPP = '#include "cache.hpp"\n'

USE_HPP = """
#ifndef MM_USE_HPP
#define MM_USE_HPP
#include "cache.hpp"
namespace mm {
template <class T>
T cache_roundtrip(Cache& c, const std::string& k, T v) {
    c.set(k, v);          // overloaded -> multi-candidate dependent call
    return c.get<T>(k);   // single-candidate dependent call
}
}  // namespace mm
#endif
"""

USE_CPP = """
#include "use.hpp"
namespace mm {
int exercise(Cache& c) { return cache_roundtrip<int>(c, "k", 1); }
}  // namespace mm
"""


def test_cross_tu_wrong_order_stub_backfill():
    """Index the consuming TU BEFORE the cache TU; the call must still resolve.

    The cache header lives in a separate, not-yet-registered directory, so it is
    UNOWNED when `use.cpp` is indexed -- Cache::set/get are absent from the DB.
    The dependent calls must mint USR-keyed stubs; indexing `cache.cpp` later
    backfills the same USRs, so the edges become resolved without re-indexing the
    consuming TU.
    """
    with tempfile.TemporaryDirectory() as tmp:
        use_dir = os.path.join(tmp, "use")
        lib_dir = os.path.join(tmp, "lib")
        os.makedirs(use_dir)
        os.makedirs(lib_dir)
        for d, name, body in (
            (lib_dir, "cache.hpp", CACHE_HPP),
            (lib_dir, "cache.cpp", CACHE_CPP),
            (use_dir, "use.hpp", USE_HPP),
            (use_dir, "use.cpp", USE_CPP),
        ):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(body)
        use_cpp = os.path.join(use_dir, "use.cpp")
        cache_cpp = os.path.join(lib_dir, "cache.cpp")
        inc = ["-I" + use_dir, "-I" + lib_dir, "-std=c++17"]

        db = Storage(os.path.join(tmp, "i.db"))
        # Only the use/ directory is a registered component: cache.hpp under lib/
        # is unowned at use.cpp index time.
        db.add_component("use", use_dir)
        use_fid = db.add_file_path(use_cpp)
        A.index_source(db, use_cpp, clang_args(use_cpp) + inc, use_fid)

        # Before the cache TU is indexed, the calls already exist as edges to
        # (unresolved) USR-keyed stubs -- order-independence depends on this.
        calls = _edge_map(db, 1)
        roundtrip_calls = calls.get("mm::cache_roundtrip", set())
        assert "mm::Cache::set" in roundtrip_calls, "overloaded dependent call stub"
        assert "mm::Cache::get" in roundtrip_calls, "single dependent call stub"
        pre = db._conn.execute(
            "SELECT COUNT(*) FROM symbol "
            "WHERE qual_name = 'mm::Cache::set' AND resolved = 0"
        ).fetchone()[0]
        assert pre == 2, "both set overloads present as unresolved stubs"

        # Now index the defining TU: same USRs backfill the stubs to resolved.
        db.add_component("lib", lib_dir)
        cache_fid = db.add_file_path(cache_cpp)
        A.index_source(db, cache_cpp, clang_args(cache_cpp) + ["-I" + lib_dir, "-std=c++17"], cache_fid)
        db.resolve_pass()

        calls = _edge_map(db, 1)
        roundtrip_calls = calls.get("mm::cache_roundtrip", set())
        assert "mm::Cache::set" in roundtrip_calls
        assert "mm::Cache::get" in roundtrip_calls
        unresolved = db._conn.execute(
            "SELECT COUNT(*) FROM symbol "
            "WHERE qual_name IN ('mm::Cache::set', 'mm::Cache::get') AND resolved = 0"
        ).fetchone()[0]
        assert unresolved == 0, "stubs backfilled to resolved after cache TU indexed"

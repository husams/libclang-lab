"""v27 multi-definition (per-backend redefinition) — end-to-end.

A library declares a method (and a static member var) and leaves them undefined;
two "servers" each re-implement them in their own file. cidx keys `symbol` by
USR, so both bodies collapse to one node -- these tests prove the `definition` /
`def_edge` / `possible_call` tables + `symbol.multi_def` keep every backend body,
its own calls, and the "possible call" fan-out.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexer.storage import Storage  # noqa: E402
from indexer.clang import ast as A  # noqa: E402
from indexer.query import GraphQuery  # noqa: E402

_HEADER = """
#pragma once
struct Context {
    void reg();              // declared here, defined per-backend
    void run() { reg(); }    // inline caller -> reg()
    static int count;        // static member var, redefined per-backend
};
"""
_HELPERS = """
#pragma once
void helper_a();
void helper_b();
int seed_a();
int seed_b();
"""
_SERVER1 = """
#include "context.hpp"
#include "helpers.hpp"
void Context::reg() { helper_a(); }
int Context::count = seed_a();
"""
_SERVER2 = """
#include "context.hpp"
#include "helpers.hpp"
void Context::reg() { helper_b(); }
int Context::count = seed_b();
"""


@pytest.fixture(scope="module")
def indexed():
    """Index both servers into one DB, resolve, hand back a GraphQuery + raw conn."""
    with tempfile.TemporaryDirectory() as tmp:
        def w(name, src):
            p = os.path.join(tmp, name)
            with open(p, "w") as fh:
                fh.write(src)
            return p

        w("context.hpp", _HEADER)
        w("helpers.hpp", _HELPERS)
        s1 = w("server1.cpp", _SERVER1)
        s2 = w("server2.cpp", _SERVER2)

        db = Storage(os.path.join(tmp, "i.db"))
        db.add_component("t", tmp)
        for path in (s1, s2):
            fid = db.add_file_path(path)
            A.index_source(db, path, ["-std=c++17"], fid)
            db.mark_file_indexed(fid)
        db.resolve_pass()
        db.close()

        g = GraphQuery(os.path.join(tmp, "i.db"))
        yield g


def _by_spelling(g, spelling):
    hits = [s for s in g.by_name(spelling)]
    assert hits, f"no symbol spelled {spelling!r}"
    return hits[0]


def test_multi_def_counts_both_backends(indexed):
    g = indexed
    reg = _by_spelling(g, "reg")
    count = _by_spelling(g, "count")
    assert reg.multi_def == 2 and reg.is_redefined
    assert count.multi_def == 2 and count.is_redefined
    # a single-definition symbol is NOT flagged
    run = _by_spelling(g, "run")
    assert run.multi_def == 1 and not run.is_redefined


def test_redefined_lists_method_and_static_var(indexed):
    names = {s.spelling for s in indexed.redefined()}
    assert "reg" in names
    assert "count" in names  # static member var counts too
    assert "run" not in names  # single body


def test_definitions_returns_both_bodies(indexed):
    reg = _by_spelling(indexed, "reg")
    defs = indexed.definitions(reg)
    files = sorted(d.file.name for d in defs if d.file)
    assert files == ["server1.cpp", "server2.cpp"]


def test_def_edges_are_per_backend(indexed):
    """Each backend body keeps its OWN callee -- not merged onto one node."""
    reg = _by_spelling(indexed, "reg")
    conn = indexed._c
    rows = conn.execute(
        "SELECT f.name AS file, d.spelling AS callee "
        "FROM def_edge de "
        "JOIN definition df ON df.id = de.src_def_id "
        "JOIN file f ON f.id = df.file_id "
        "JOIN symbol d ON d.id = de.dst_id "
        "WHERE df.symbol_id = ? AND de.kind = 1 "
        "ORDER BY f.name",
        (reg.id,),
    ).fetchall()
    got = {(r["file"], r["callee"]) for r in rows}
    assert ("server1.cpp", "helper_a") in got
    assert ("server2.cpp", "helper_b") in got


def test_possible_call_fans_out_to_every_body(indexed):
    """run() calls reg(); reg() has two bodies -> both are possible targets."""
    run = _by_spelling(indexed, "run")
    targets = indexed.possible_callees(run)
    files = sorted(d.file.name for d in targets if d.file)
    assert files == ["server1.cpp", "server2.cpp"]
    assert all(d.sym.spelling == "reg" for d in targets)


def test_static_var_initializer_edges_are_per_backend(indexed):
    """`int Context::count = seed_x()` records the init call per backend."""
    count = _by_spelling(indexed, "count")
    conn = indexed._c
    rows = conn.execute(
        "SELECT f.name AS file, d.spelling AS callee "
        "FROM def_edge de "
        "JOIN definition df ON df.id = de.src_def_id "
        "JOIN file f ON f.id = df.file_id "
        "JOIN symbol d ON d.id = de.dst_id "
        "WHERE df.symbol_id = ? ",
        (count.id,),
    ).fetchall()
    got = {(r["file"], r["callee"]) for r in rows}
    assert ("server1.cpp", "seed_a") in got
    assert ("server2.cpp", "seed_b") in got


def test_static_var_init_text_per_backend(indexed):
    """v28: each backend body carries its own initializer source text."""
    count = _by_spelling(indexed, "count")
    by_file = {
        d.file.name: d.init_text
        for d in indexed.definitions(count)
        if d.file
    }
    assert by_file["server1.cpp"] == "seed_a()"
    assert by_file["server2.cpp"] == "seed_b()"


def test_static_var_init_edges_are_uses_not_calls(indexed):
    """v28: a variable's initializer references are USES (kind 7), not calls."""
    count = _by_spelling(indexed, "count")
    kinds = {
        r["kind"]
        for r in indexed._c.execute(
            "SELECT de.kind FROM def_edge de "
            "JOIN definition df ON df.id = de.src_def_id "
            "WHERE df.symbol_id = ?",
            (count.id,),
        )
    }
    assert kinds == {7}  # uses only -- variables do not call


def test_reindex_one_backend_preserves_the_other(indexed):
    """Re-indexing server2 must NOT wipe server1's def_edge (the flip-flop bug
    the index-time capture defends against)."""
    reg = _by_spelling(indexed, "reg")
    before = indexed.definitions(reg)
    assert len(before) == 2  # still both after the module-scoped index+resolve

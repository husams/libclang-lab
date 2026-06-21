"""v16: symbol.kind stored as a CXCursorKind integer + kind metadata tables.

Asserts:
  * symbol.kind is persisted as the libclang CXCursorKind int, while the read
    side (Storage / GraphQuery / stats) still presents the string name.
  * the `symbol_kind` and `edge_kind` metadata tables enumerate every kind with
    its integer code + string name, and the symbol_kind ids match the CABI.
  * a fresh schema has no CHECK on symbol.kind and no FK on edge.kind.
"""

from __future__ import annotations

import os
import sys

import clang.cindex as cx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from indexer.storage import (  # noqa: E402
    SYMBOL_KIND_IDS,
    SYMBOL_KIND_NAMES,
    Storage,
    Symbol,
)
from indexer.query import EDGE_KINDS, GraphQuery  # noqa: E402


def _fresh(tmp_path):
    db = Storage(str(tmp_path / "i.db"))
    db.add_component("c", str(tmp_path))
    return db


def test_symbol_kind_table_matches_cabi(tmp_path):
    """symbol_kind rows == SYMBOL_KIND_IDS, and each id IS the CXCursorKind value."""
    db = _fresh(tmp_path)
    rows = dict(db._conn.execute("SELECT name, id FROM symbol_kind"))
    db.close()
    assert rows == SYMBOL_KIND_IDS
    # Spot-check the mapping against libclang directly.
    assert SYMBOL_KIND_IDS["method"] == cx.CursorKind.CXX_METHOD.value == 21
    assert SYMBOL_KIND_IDS["struct"] == cx.CursorKind.STRUCT_DECL.value == 2
    assert SYMBOL_KIND_IDS["macro"] == cx.CursorKind.MACRO_DEFINITION.value == 501


def test_edge_kind_table_matches_map(tmp_path):
    db = _fresh(tmp_path)
    rows = dict(db._conn.execute("SELECT name, id FROM edge_kind"))
    db.close()
    assert rows == EDGE_KINDS


def test_kind_stored_as_int_read_as_name(tmp_path):
    db = _fresh(tmp_path)
    fid = db.add_file_path(str(tmp_path / "x.c"))
    db.add_symbol(
        Symbol(usr="c:@F@f", spelling="f", kind="function", file_id=fid, line=1, col=1)
    )
    # On disk: the CXCursorKind int.
    raw = db._conn.execute("SELECT kind, typeof(kind) FROM symbol").fetchone()
    assert raw[0] == SYMBOL_KIND_IDS["function"] == 8
    assert raw[1] == "integer"
    # Read side: the string name, unchanged.
    assert db.lookup_symbol("c:@F@f").kind == "function"
    assert db.stats()["symbols_by_kind"] == {"function": 1}
    db.close()


def test_kind_filter_round_trips(tmp_path):
    db = _fresh(tmp_path)
    fid = db.add_file_path(str(tmp_path / "x.c"))
    db.add_symbol(Symbol(usr="c:@F@f", spelling="s", kind="function", file_id=fid))
    db.add_symbol(Symbol(usr="c:@S@S", spelling="s", kind="struct", file_id=fid))
    assert len(db.lookup_symbols_by_name("s", kind="function")) == 1
    assert len(db.lookup_symbols_by_name("s", kind="struct")) == 1
    assert db.lookup_symbols_by_name("s", kind="not-a-kind") == []  # unknown -> none
    path = str(tmp_path / "i.db")
    db.close()
    with GraphQuery(path) as g:
        assert [s.kind for s in g.by_name("s", kind="function")] == ["function"]
        assert {s.kind for s in g.by_name("s")} == {"function", "struct"}


def test_fresh_schema_drops_check_and_edge_fk(tmp_path):
    db = _fresh(tmp_path)
    sql = {
        r[0]: r[1]
        for r in db._conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN ('symbol','edge')"
        )
    }
    db.close()
    assert "CHECK" not in sql["symbol"].upper().split("KIND", 1)[1].split(",")[0]
    assert "REFERENCES edge_kind" not in sql["edge"]


def test_inverse_map_is_consistent():
    assert SYMBOL_KIND_NAMES == {v: k for k, v in SYMBOL_KIND_IDS.items()}
    assert len(SYMBOL_KIND_IDS) == 17

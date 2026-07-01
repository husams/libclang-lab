"""Integration test for the v25 symbol extent-end columns (`end_line`/`end_col`).

Drives the REAL libclang extractor and asserts that every symbol records the END
of its own extent alongside the start, so an agent can slice `(line..end_line)`
to read the whole entity without scanning the file:

  * functions / methods -> the DEFINITION body (up to the closing brace);
  * class / struct / union / enum -> the full declaration region (like a class);
  * typedef -> the extent of the declaration.

Also covers the decl->def supersession invariant: `end_line`/`end_col` move in
lockstep with `line`/`col` when a `.c` definition supersedes a header prototype,
and the query-layer `Sym` surfaces the pair via `to_dict()` and `.span`.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from _helpers import clang_args  # noqa: E402

from indexer.storage import Storage  # noqa: E402
from indexer.clang import ast as A  # noqa: E402
from indexer.clang.util import parse as clang_parse  # noqa: E402
from indexer.query import GraphQuery  # noqa: E402


HEADER = """\
struct Point { int x; int y; };

enum Color { RED, GREEN, BLUE };

int add(int a, int b);
"""

SOURCE = """\
#include "shapes_ext.h"

int add(int a, int b) {
    int s = a + b;
    return s;
}
"""


@pytest.fixture
def db_path():
    """Index the header (prototype) THEN the .c (definition) into one DB so the
    upsert supersession path is exercised, and yield the DB path."""
    with tempfile.TemporaryDirectory() as tmp:
        hdr = os.path.join(tmp, "shapes_ext.h")
        src = os.path.join(tmp, "shapes_ext.c")
        with open(hdr, "w") as fh:
            fh.write(HEADER)
        with open(src, "w") as fh:
            fh.write(SOURCE)
        db = Storage(os.path.join(tmp, "i.db"))
        db.add_component("t", tmp)
        with db.transaction():
            for path in (hdr, src):
                tu = clang_parse(path, clang_args())
                assert not [d for d in tu.diagnostics if d.severity >= 4], (
                    f"fixture {path} must parse without fatals"
                )
                A.index_symbols(db, tu, db.add_file_path(path))
        yield db._conn.execute("PRAGMA database_list").fetchone()[2]


def _row(conn, spelling):
    return conn.execute(
        "SELECT line, col, end_line, end_col, decl_line, is_definition "
        "FROM symbol WHERE spelling = ? ORDER BY is_definition DESC LIMIT 1",
        (spelling,),
    ).fetchone()


def test_function_end_is_definition_closing_brace(db_path):
    conn = sqlite3.connect(db_path)
    line, _col, end_line, _end_col, decl_line, is_def = _row(conn, "add")
    # The definition (in the .c, lines 3-6) supersedes the header prototype:
    # line/col point at the def, end_line at its closing brace, decl_line at the
    # header prototype. end MUST be past the start (a multi-line body).
    assert is_def == 1
    assert line == 3 and end_line == 6, (line, end_line)
    assert decl_line == 5, "header prototype site is preserved in decl_line"


def test_struct_end_spans_the_declaration_region(db_path):
    conn = sqlite3.connect(db_path)
    line, _c, end_line, _ec, _dl, _d = _row(conn, "Point")
    assert line == 1 and end_line == 1, "single-line struct spans its one line"


def test_enum_end_spans_the_full_region_like_a_class(db_path):
    conn = sqlite3.connect(db_path)
    line, _c, end_line, _ec, _dl, _d = _row(conn, "Color")
    # enum Color occupies header line 3 in full -- the whole `enum { ... }` region.
    assert line == 3 and end_line == 3, (line, end_line)


def test_every_indexed_symbol_has_an_end(db_path):
    conn = sqlite3.connect(db_path)
    total, with_end = conn.execute(
        "SELECT COUNT(*), SUM(end_line IS NOT NULL) FROM symbol "
        "WHERE file_id IS NOT NULL"
    ).fetchone()
    assert total > 0 and with_end == total, "located symbols all carry an end"


def test_query_sym_exposes_end_and_span(db_path):
    with GraphQuery(db_path) as g:
        sym = g.find("add")[0]
        assert sym.end_line is not None and sym.end_col is not None
        d = sym.to_dict()
        assert d["line"] == sym.line and d["end_line"] == sym.end_line
        assert d["end_col"] == sym.end_col
        # span slices the whole entity: file:line-end_line
        assert sym.span == f"{sym.file.name}:{sym.line}-{sym.end_line}"

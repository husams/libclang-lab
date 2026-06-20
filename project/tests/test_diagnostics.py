"""Tests for v15 per-file parse diagnostics (errors/warnings).

Covers the storage layer (replace/get/counts, wholesale refresh, locationless
rows), the CLI display helpers, and the `list files` indicator + `show file`
diagnostics section. Hermetic: diagnostics are injected via the storage API so
no real libclang parse is needed.
"""

from __future__ import annotations

import os

from indexer import cli
from indexer.cli import _diag_flag, _diag_summary
from indexer.storage import Storage


def run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def _use_cache(monkeypatch, db_path):
    """Point the standard index path ($INDEXER_CACHE/index.db) at the fixture
    DB, so `list`/`show` (no --db override) read it."""
    monkeypatch.setenv("INDEXER_CACHE", os.path.dirname(db_path))


def _first_file_id(db_path: str) -> int:
    with Storage(db_path) as db:
        rows = db.list_files()
        fid = rows[0][0].id
        assert fid is not None
        return fid


# -- storage layer ------------------------------------------------------------


def test_replace_get_roundtrip_in_tu_order(index_db):
    fid = _first_file_id(index_db)
    with Storage(index_db) as db:
        db.replace_diagnostics(
            fid,
            [
                {"severity": 2, "spelling": "unused 'x'",
                 "file_path": "/r/a.c", "line": 3, "col": 5},
                {"severity": 3, "spelling": "implicit decl",
                 "file_path": "/r/a.c", "line": 7, "col": 1},
            ],
        )
        diags = db.get_diagnostics(fid)
        assert [d.severity for d in diags] == [2, 3]  # insertion (TU) order
        ids = [d.id for d in diags]
        assert ids == sorted(ids)  # ids follow TU order
        assert diags[0].spelling == "unused 'x'"
        assert diags[0].line == 3 and diags[0].col == 5


def test_locationless_diagnostic_stores_nulls(index_db):
    fid = _first_file_id(index_db)
    with Storage(index_db) as db:
        db.replace_diagnostics(
            fid,
            [{"severity": 2, "spelling": "linker input unused",
              "file_path": None, "line": None, "col": None}],
        )
        (d,) = db.get_diagnostics(fid)
        assert d.file_path is None and d.line is None and d.col is None


def test_replace_is_wholesale_refresh(index_db):
    fid = _first_file_id(index_db)
    with Storage(index_db) as db:
        db.replace_diagnostics(
            fid, [{"severity": 3, "spelling": "old", "file_path": None,
                   "line": None, "col": None}]
        )
        # Re-index of a now-clean file drops the stale rows.
        db.replace_diagnostics(fid, [])
        assert db.get_diagnostics(fid) == []
        assert db.diagnostic_counts() == {}


def test_diagnostic_counts_group_by_severity(index_db):
    fid = _first_file_id(index_db)
    with Storage(index_db) as db:
        db.replace_diagnostics(
            fid,
            [
                {"severity": 2, "spelling": "w1", "file_path": None,
                 "line": None, "col": None},
                {"severity": 2, "spelling": "w2", "file_path": None,
                 "line": None, "col": None},
                {"severity": 3, "spelling": "e1", "file_path": None,
                 "line": None, "col": None},
            ],
        )
        assert db.diagnostic_counts() == {fid: {2: 2, 3: 1}}


def test_diagnostics_cascade_on_file_delete(index_db):
    fid = _first_file_id(index_db)
    with Storage(index_db) as db:
        db.replace_diagnostics(
            fid, [{"severity": 3, "spelling": "e", "file_path": None,
                   "line": None, "col": None}]
        )
        db.delete_file(fid)
        assert db.diagnostic_counts() == {}


# -- display helpers ----------------------------------------------------------


def test_diag_flag():
    assert _diag_flag({}) == "-"
    assert _diag_flag({2: 3}) == "3W"
    assert _diag_flag({3: 2}) == "2E"
    assert _diag_flag({2: 3, 3: 2}) == "2E3W"
    assert _diag_flag({4: 1, 3: 1}) == "2E"  # fatal folds into E


def test_diag_summary():
    assert _diag_summary({2: 1}) == "1 warning(s)"
    assert _diag_summary({2: 1, 3: 2}) == "2 error(s), 1 warning(s)"
    assert _diag_summary({4: 1}) == "1 fatal(s)"


# -- CLI display --------------------------------------------------------------


def test_list_files_shows_indicator(index_db, capsys, monkeypatch):
    fid = _first_file_id(index_db)
    with Storage(index_db) as db:
        db.replace_diagnostics(
            fid,
            [{"severity": 2, "spelling": "w", "file_path": None,
              "line": None, "col": None}],
        )
    _use_cache(monkeypatch, index_db)
    rc, out, _ = run(["list", "files"], capsys)
    assert rc == 0
    # The flagged file shows '1W'; clean files show '-'.
    flagged = [ln for ln in out.splitlines() if f"{fid:>4}  " in ln]
    assert flagged and "1W" in flagged[0]
    assert any(" -  " in ln for ln in out.splitlines())  # a clean file


def test_show_file_lists_diagnostics(index_db, capsys, monkeypatch):
    fid = _first_file_id(index_db)
    with Storage(index_db) as db:
        db.replace_diagnostics(
            fid,
            [
                {"severity": 3, "spelling": "bad thing",
                 "file_path": "/r/a.c", "line": 10, "col": 2},
                {"severity": 2, "spelling": "minor thing",
                 "file_path": "/r/a.c", "line": 12, "col": 1},
            ],
        )
    _use_cache(monkeypatch, index_db)
    rc, out, _ = run(["show", "file", str(fid)], capsys)
    assert rc == 0
    assert "diagnostics  1 error(s), 1 warning(s)" in out
    assert "  error   /r/a.c:10:2: bad thing" in out
    assert "  warning /r/a.c:12:1: minor thing" in out


def test_show_file_clean_has_no_diagnostics_line(index_db, capsys, monkeypatch):
    fid = _first_file_id(index_db)
    _use_cache(monkeypatch, index_db)
    rc, out, _ = run(["show", "file", str(fid)], capsys)
    assert rc == 0
    assert "diagnostics" not in out

"""Tests for the `cidx set ...` CLI subcommand (indexer.cli).

Drives cli.main(argv) with --db pointing at the seeded fixture DB and asserts
the file `indexed`/pending flag flips without touching symbols. Hermetic.
"""

from __future__ import annotations

from indexer import cli
from indexer.storage import Storage


def run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def _indexed(db_path):
    """{basename: indexed_bool} for every file in the DB."""
    with Storage(db_path) as db:
        return {ap.rsplit("/", 1)[-1]: bool(f.indexed)
                for f, ap in db.list_files()}


def _symbol_count(db_path):
    with Storage(db_path) as db:
        return db.stats()["symbols"]


def test_set_pending_true_marks_all_files(index_db, capsys):
    before = _symbol_count(index_db)
    rc, out, _ = run(
        ["set", "pending=True", "--component", "lab", "--db", index_db], capsys)
    assert rc == 0
    assert all(v is False for v in _indexed(index_db).values())
    # symbols are NOT deleted by `set`
    assert _symbol_count(index_db) == before
    assert "set pending=True" in out


def test_set_pending_false_one_file(index_db, capsys):
    # First mark everything pending, then clear just one file.
    run(["set", "pending=True", "--component", "lab", "--db", index_db], capsys)
    rc, _, _ = run(
        ["set", "pending=False", "--component", "lab", "--file", "main.c",
         "--db", index_db], capsys)
    assert rc == 0
    state = _indexed(index_db)
    assert state["main.c"] is True
    assert state["lib.c"] is False


def test_set_indexed_alias(index_db, capsys):
    rc, _, _ = run(
        ["set", "indexed=true", "--component", "lab", "--db", index_db], capsys)
    assert rc == 0
    assert all(v is True for v in _indexed(index_db).values())


def test_set_spaced_assignment(index_db, capsys):
    # 'pending = True' as three argv tokens must parse identically.
    rc, _, _ = run(
        ["set", "pending", "=", "True", "--component", "lab", "--db", index_db],
        capsys)
    assert rc == 0
    assert all(v is False for v in _indexed(index_db).values())


def test_set_dry_run_changes_nothing(index_db, capsys):
    before = _indexed(index_db)
    rc, out, _ = run(
        ["set", "indexed=True", "--component", "lab", "--dry-run",
         "--db", index_db], capsys)
    assert rc == 0
    assert "would set" in out
    assert _indexed(index_db) == before


def test_set_unknown_field(index_db, capsys):
    rc, _, err = run(
        ["set", "frobnicate=True", "--component", "lab", "--db", index_db],
        capsys)
    assert rc == 1
    assert "unknown field" in err


def test_set_bad_bool(index_db, capsys):
    rc, _, err = run(
        ["set", "pending=maybe", "--component", "lab", "--db", index_db], capsys)
    assert rc == 1
    assert "boolean" in err


def test_set_unknown_component(index_db, capsys):
    rc, _, err = run(
        ["set", "pending=True", "--component", "nope", "--db", index_db], capsys)
    assert rc == 1
    assert "no component named" in err


def test_set_no_file_match(index_db, capsys):
    rc, _, err = run(
        ["set", "pending=True", "--component", "lab", "--file", "ghost.c",
         "--db", index_db], capsys)
    assert rc == 1
    assert "no files match" in err

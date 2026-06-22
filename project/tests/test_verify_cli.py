"""Tests for `cidx verify` (v0.29.0) -- checks that component roots (incl.
version) and files exist on disk.

Hermetic: builds a real on-disk tree under tmp_path so the existence checks are
meaningful, then drives cli.main directly. The output format is asserted because
it is part of the Python<->C++ byte-identical parity contract.
"""

from __future__ import annotations

import os

import pytest

from indexer import cli
from indexer.storage import Storage


def run(argv, capsys):
    rc = cli.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


@pytest.fixture
def tree(tmp_path):
    """A DB whose components/files map onto a real tmp tree.

    - good      : dir exists, one real file           -> ok
    - gone      : dir absent                           -> MISSING
    - vergood   : versioned dir 1.0.0 exists           -> ok
    - verbad    : base exists but version dir 9.9.9 absent -> VER-MISS
    """
    root = tmp_path
    good = root / "good"
    good.mkdir()
    (good / "a.c").write_text("int a;\n")

    verbase = root / "verbase"
    verbase.mkdir()
    vergood = root / "verlib" / "1.0.0"
    vergood.mkdir(parents=True)

    db_path = str(root / "v.db")
    with Storage(db_path) as db:
        db.add_component("good", str(good))
        db.add_file_path(str(good / "a.c"))
        db.add_component("gone", str(root / "gone"))
        db.add_component("vergood", str(root / "verlib"), version="1.0.0")
        db.add_component("verbad", str(verbase), version="9.9.9")
    return db_path, str(good)


def test_verify_reports_each_status(tree, capsys):
    db_path, good = tree
    rc, out, err = run(["verify", "--db", db_path], capsys)
    assert rc == 1  # at least one component missing
    assert f"component  ok        good  {good}" in out
    assert "component  MISSING   gone  " in out
    assert "component  ok        vergood  " in out
    assert "component  VER-MISS  verbad  " in out
    # Summary lines.
    assert "components: 2 ok, 1 missing, 1 version-mismatch" in out
    assert "files: 1 ok, 0 missing" in out


def test_verify_ok_file_hidden_without_all(tree, capsys):
    db_path, _ = tree
    _, out, _ = run(["verify", "--db", db_path], capsys)
    assert "file  ok" not in out  # OK files suppressed by default


def test_verify_all_lists_ok_files(tree, capsys):
    db_path, good = tree
    _, out, _ = run(["verify", "--db", db_path, "--all"], capsys)
    assert f"file  ok        {os.path.join(good, 'a.c')}" in out


def test_verify_missing_file_listed_and_nonzero(tree, capsys):
    db_path, good = tree
    os.remove(os.path.join(good, "a.c"))  # the indexed file now vanishes
    rc, out, _ = run(["verify", "--db", db_path], capsys)
    assert rc == 1
    assert f"file  MISSING   {os.path.join(good, 'a.c')}" in out
    assert "files: 0 ok, 1 missing" in out


def test_verify_scoped_clean_component_exits_zero(tree, capsys):
    db_path, _ = tree
    rc, out, _ = run(["verify", "--db", db_path, "-c", "good"], capsys)
    assert rc == 0
    assert "components: 1 ok, 0 missing, 0 version-mismatch" in out
    assert "files: 1 ok, 0 missing" in out
    # Scoped: other components are not listed.
    assert "gone" not in out


def test_verify_unknown_component_errors(tree, capsys):
    db_path, _ = tree
    rc, _, err = run(["verify", "--db", db_path, "-c", "nope"], capsys)
    assert rc == 1
    assert "error: no component named 'nope'" in err

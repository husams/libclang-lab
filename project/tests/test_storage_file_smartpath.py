"""Tests for the storage-layer File *smart path* (indexer.storage.File).

A File handed back by Storage.get_file / get_file_by_id / files / list_files is
bound to its Storage, mirroring the read-only indexer.query.File but -- because
storage is the writable layer -- able to really index() and resolve() itself.

These tests write a real on-disk C source so source()/tu()/walk()/index() run
end-to-end through libclang (like the other parse-backed tests in this suite).
"""

from __future__ import annotations

import os

import pytest

from indexer.storage import File, Storage
from indexer.utils.hashing import md5_of

SRC = """\
int add(int a, int b) {
    return a + b;
}

int twice(int x) {
    return add(x, x);
}
"""


@pytest.fixture
def bound_file(tmp_path):
    """A Storage-bound File over a real on-disk calc.c (not yet indexed)."""
    src = tmp_path / "calc.c"
    src.write_text(SRC)
    db_path = str(tmp_path / "index.db")
    db = Storage(db_path)
    comp = db.add_component("lab", str(tmp_path))
    root = db.add_directory(comp, "")
    fid = db.add_file(
        root, "calc.c", md5=md5_of(str(src)), compile_options=["-std=c11"]
    )
    f = db.get_file(str(src))
    assert f is not None and f.id == fid
    yield db, f, str(src)
    db.close()


# -- binding / identity ----------------------------------------------------- #


def test_unbound_file_raises_clearly():
    f = File(directory_id=1, name="x.c")
    with pytest.raises(RuntimeError, match="not bound to a Storage"):
        _ = f.abspath
    with pytest.raises(RuntimeError, match="not bound to a Storage"):
        _ = f.component


def test_get_file_binds(bound_file):
    _, f, src = bound_file
    assert f._storage is not None
    assert os.path.realpath(f.abspath) == os.path.realpath(src)
    assert f.name == "calc.c"


def test_existing_fields_preserved(bound_file):
    _, f, _src = bound_file
    assert f.compile_options == ["-std=c11"]
    assert f.indexed is False
    assert f.args_overridden is False
    assert f.md5 is not None


def test_files_and_list_files_bind(bound_file):
    db, _f, src = bound_file
    (f1, p1), = db.files()
    assert f1._storage is db and os.path.realpath(p1) == os.path.realpath(src)
    (f2, _p2), = db.list_files(name="calc")
    assert f2._storage is db and f2.abspath  # abspath resolves on a bound row


def test_get_file_by_id_binds(bound_file):
    db, f, _src = bound_file
    g = db.get_file_by_id(f.id)
    assert g is not None and g._storage is db and g.abspath == f.abspath


# -- component / repo (#6) -------------------------------------------------- #


def test_component_and_repo(bound_file):
    db, f, _src = bound_file
    assert f.component is not None and f.component.name == "lab"
    # ungrouped component -> no repository
    assert f.repo is None


# -- source region (#1) ----------------------------------------------------- #


def test_source_single_line(bound_file):
    _, f, _src = bound_file
    assert f.source((1, 1), (1, 11)) == "int add(int"


def test_source_multi_line(bound_file):
    _, f, _src = bound_file
    assert f.source((5, 1), (6, 20)) == "int twice(int x) {\n    return add(x, x)"


def test_source_past_eof_is_empty(bound_file):
    _, f, _src = bound_file
    assert f.source((999, 1), (999, 5)) == ""


def test_source_bad_range_raises(bound_file):
    _, f, _src = bound_file
    with pytest.raises(ValueError):
        f.source((3, 5), (3, 1))


# -- index (#4) ------------------------------------------------------------- #


def test_index_then_symbols_and_state(bound_file):
    db, f, _src = bound_file
    res = f.index()
    assert res["symbols"] == 2
    assert f.indexed is True and f.mtime is not None
    # persisted on the row too
    assert db.get_file_by_id(f.id).indexed is True


def test_index_reparses_each_call(bound_file):
    _, f, _src = bound_file
    f.index()
    # index() always does the real job (no short-circuit); symbols already exist
    # by USR so the *newly stored* count is 0, but it parsed + ran again.
    again = f.index()
    assert "symbols" in again


def test_index_unbound_or_idless_raises():
    f = File(directory_id=1, name="x.c")
    with pytest.raises(RuntimeError):
        f.index()


# -- symbols (#2) ----------------------------------------------------------- #


def test_symbols_in_file(bound_file):
    _, f, _src = bound_file
    f.index()
    got = sorted((s.spelling, s.kind) for s in f.symbols())
    assert got == [("add", "function"), ("twice", "function")]
    assert [s.spelling for s in f.symbols(limit=1)] == ["add"]


def test_symbols_empty_before_index(bound_file):
    _, f, _src = bound_file
    assert f.symbols() == []


# -- tu + walk with caching (#3) -------------------------------------------- #


def test_tu_is_memoized(bound_file):
    _, f, _src = bound_file
    tu1 = f.tu()
    assert tu1 is not None
    assert f.tu() is tu1  # cache=True returns the SAME object
    assert os.path.basename(tu1.spelling) == "calc.c"


def test_tu_cache_false_reparses(bound_file):
    _, f, _src = bound_file
    tu1 = f.tu()
    tu2 = f.tu(cache=False)
    assert tu2 is not None and tu2 is not tu1


def test_walk_yields_cursors(bound_file):
    _, f, _src = bound_file
    names = {c.spelling for c in f.walk()}
    assert {"add", "twice"} <= names


def test_index_invalidates_tu_memo(bound_file):
    _, f, _src = bound_file
    tu1 = f.tu()
    f.index()
    assert f.tu() is not tu1  # memo dropped after a (re)index


# -- resolve (#5) ----------------------------------------------------------- #


def test_resolve_runs_global_pass(bound_file):
    db, f, _src = bound_file
    f.index()
    stubs, cross = f.resolve()
    assert isinstance(stubs, int) and isinstance(cross, int)
    row = db._conn.execute(
        "SELECT value FROM meta WHERE key = 'graph_resolved_at'"
    ).fetchone()
    assert row is not None  # resolve_pass stamped the meta flag

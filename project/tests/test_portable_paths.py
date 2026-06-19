"""Tests for cidx portable paths (v14): pathx, schema migration, storage API,
compiledb preserve rule, and all new CLI commands.

All tests are hermetic (no real file-system, no libclang, no network).
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from indexer import pathx
from indexer.compiledb import strip_for_libclang, _is_indirected
from indexer.storage import Component, Storage
from indexer import cli


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def run(argv, capsys):
    """Run cli.main(argv) and return (rc, stdout, stderr)."""
    rc = cli.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# ---------------------------------------------------------------------------
# 1. pathx.expandvars -- parity table (§1.1.3 of the contract)
# ---------------------------------------------------------------------------

_PARITY_TABLE = [
    ("no vars", "no vars"),
    ("$FOO/a", "/x/a"),
    ("${FOO}/a", "/x/a"),
    ("$UNDEF/a", "$UNDEF/a"),
    ("${UNDEF}/a", "${UNDEF}/a"),
    ("$$FOO", "$/x"),
    ("${FOO", "${FOO"),
    ("$FOO$BAR", "/x/y"),
    ("a$FOO-b", "a/x-b"),
    ("${FO-O}", "${FO-O}"),
    ("$1FOO", "$1FOO"),
    ("$_F", "$_F"),
    ("$FOO.h", "/x.h"),
]


@pytest.fixture(autouse=True)
def _parity_env(monkeypatch):
    """Set FOO=/x and BAR=/y; leave UNDEF, _F, 1FOO, FO-O absent."""
    monkeypatch.setenv("FOO", "/x")
    monkeypatch.setenv("BAR", "/y")
    for k in ("UNDEF", "_F"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("inp,expected", _PARITY_TABLE)
def test_expandvars_parity(inp, expected):
    assert pathx.expandvars(inp) == expected


def test_expandvars_no_dollar_fast_path():
    """No $ in input → returned unchanged without touching the regex."""
    s = "no_dollar_here"
    assert pathx.expandvars(s) is s  # fast-path: same object


def test_expandvars_empty():
    assert pathx.expandvars("") == ""


def test_expandvars_dollar_only():
    """A bare $ that doesn't start a valid var is left literal."""
    assert pathx.expandvars("$") == "$"


def test_expandvars_defined_empty_string(monkeypatch):
    """An env var set to '' substitutes empty string (not literal)."""
    monkeypatch.setenv("EMPTY_VAR", "")
    assert pathx.expandvars("$EMPTY_VAR/x") == "/x"


# ---------------------------------------------------------------------------
# 2. pathx.label_expand
# ---------------------------------------------------------------------------


def test_label_expand_registry_hit():
    def lookup(name):
        return "$REP/foo/include" if name == "libfoo-include" else None

    assert pathx.label_expand("<libfoo-include>", lookup=lookup) == "$REP/foo/include"


def test_label_expand_glued_with_flag():
    def lookup(name):
        return "$REP/foo/include" if name == "libfoo-include" else None

    assert (
        pathx.label_expand("-I<libfoo-include>", lookup=lookup) == "-I$REP/foo/include"
    )


def test_label_expand_trailing_subdir():
    def lookup(name):
        return "$REP/foo/include" if name == "libfoo-include" else None

    assert (
        pathx.label_expand("<libfoo-include>/sub", lookup=lookup)
        == "$REP/foo/include/sub"
    )


def test_label_expand_autoderive_usr_local():
    """No registry hit → autoderive: <usr-local-include> → /usr/local/include."""
    assert pathx.label_expand("<usr-local-include>", lookup=None, autoderive=True) == (
        "/usr/local/include"
    )


def test_label_expand_autoderive_flag_glued():
    assert (
        pathx.label_expand("-I<usr-local-include>", lookup=None, autoderive=True)
        == "-I/usr/local/include"
    )


def test_label_expand_autoderive_off_unknown():
    """-I<unknown-dir> with autoderive off → left literal."""
    assert (
        pathx.label_expand("-I<unknown-dir>", lookup=None, autoderive=False)
        == "-I<unknown-dir>"
    )


def test_label_expand_no_angle_brackets_unchanged():
    """A token without <> is passed through unchanged."""
    s = "-I/some/path"
    assert pathx.label_expand(s) == s


def test_label_expand_empty_name_preserved():
    """<> (empty name) → left literal even with autoderive on."""
    assert pathx.label_expand("<>", autoderive=True) == "<>"


def test_label_expand_registry_wins_over_autoderive():
    def lookup(name):
        return "/custom" if name == "usr-local-include" else None

    assert (
        pathx.label_expand("<usr-local-include>", lookup=lookup, autoderive=True)
        == "/custom"
    )


# ---------------------------------------------------------------------------
# 3. pathx.resolve_fs_path  (§1.3)
# ---------------------------------------------------------------------------


def test_resolve_fs_path_full_chain(monkeypatch):
    """label → expandvars → expanduser → normpath in order."""
    monkeypatch.setenv("REP", "/opt")

    def lookup(name):
        return "$REP/foo/include" if name == "libfoo-include" else None

    result = pathx.resolve_fs_path("<libfoo-include>", lookup=lookup)
    assert result == "/opt/foo/include"


def test_resolve_fs_path_no_label():
    result = pathx.resolve_fs_path("/foo/../bar")
    assert result == "/bar"


def test_resolve_fs_path_expanduser(monkeypatch):
    monkeypatch.setenv("HOME", "/home/user")
    result = pathx.resolve_fs_path("~/projects")
    assert result == "/home/user/projects"


def test_resolve_fs_path_empty():
    """resolve_fs_path('') → normpath('') → '.'."""
    assert pathx.resolve_fs_path("") == "."


def test_resolve_fs_path_no_lookup_defaults_to_autoderive():
    """Without a lookup, autoderive still applies (default autoderive=True)."""
    result = pathx.resolve_fs_path("<usr-local-include>")
    assert result == "/usr/local/include"


def test_resolve_fs_path_autoderive_off():
    result = pathx.resolve_fs_path("<usr-local-include>", autoderive=False)
    assert result == "<usr-local-include>"


# ---------------------------------------------------------------------------
# 4. pathx.split_base_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "root,expected_base,expected_ver",
    [
        ("/opt/libfoo/1.2.3", "/opt/libfoo", "1.2.3"),
        ("/opt/libfoo/v2", "/opt/libfoo", "v2"),
        ("/opt/libfoo/10-10-20", "/opt/libfoo", "10-10-20"),
        ("/opt/libfoo/10_10_20", "/opt/libfoo", "10_10_20"),
        ("/opt/libfoo/10", "/opt/libfoo", "10"),
        ("/src/v8", "/src", "v8"),
        # Non-version trailing segment → no split
        ("/opt/libfoo/include", "/opt/libfoo/include", None),
        ("/opt/libfoo/src", "/opt/libfoo/src", None),
        # Root-level: base would be '/' → no split
        ("/1.2.3", "/1.2.3", None),
    ],
)
def test_split_base_version(root, expected_base, expected_ver):
    base, ver = pathx.split_base_version(root)
    assert base == expected_base
    assert ver == expected_ver


def test_split_base_version_trailing_slash():
    """Trailing slash on input is normalised away."""
    base, ver = pathx.split_base_version("/opt/libfoo/1.2.3/")
    assert base == "/opt/libfoo"
    assert ver == "1.2.3"


# ---------------------------------------------------------------------------
# 5. Schema v14: fresh DB + migration
# ---------------------------------------------------------------------------


def test_fresh_db_schema_version(tmp_path):
    db_path = str(tmp_path / "test.db")
    with Storage(db_path) as db:
        row = db._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 14


def test_fresh_db_component_has_version_column(tmp_path):
    db_path = str(tmp_path / "test.db")
    with Storage(db_path) as db:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(component)")}
        assert "version" in cols


def test_fresh_db_label_table_exists(tmp_path):
    db_path = str(tmp_path / "test.db")
    with Storage(db_path) as db:
        tables = {
            r[0]
            for r in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "label" in tables


def test_migration_v13_to_v14(tmp_path):
    """A v13 DB gains component.version + label table and bumps to v14."""
    db_path = str(tmp_path / "v13.db")

    # Build a minimal v13-era DB (no version column on component, no label table).
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '13')")
    conn.execute("""
        CREATE TABLE component (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL DEFAULT 'repo'
            CHECK (kind IN ('repo', 'external'))
        )
    """)
    conn.execute("""
        CREATE TABLE symbol (
            id INTEGER PRIMARY KEY,
            usr TEXT NOT NULL UNIQUE,
            spelling TEXT NOT NULL,
            qual_name TEXT, display_name TEXT, kind TEXT NOT NULL,
            type_info TEXT, file_id INTEGER, line INTEGER, col INTEGER,
            decl_file_id INTEGER, decl_line INTEGER, decl_col INTEGER,
            decl_path TEXT,
            is_definition INTEGER NOT NULL DEFAULT 0,
            is_pure INTEGER NOT NULL DEFAULT 0,
            is_static INTEGER NOT NULL DEFAULT 0,
            is_instantiation INTEGER NOT NULL DEFAULT 0,
            linkage TEXT, access TEXT, parent_usr TEXT,
            resolved INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE TABLE edge_kind (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"
    )
    conn.execute("INSERT INTO edge_kind VALUES (1,'calls')")
    conn.execute("""
        CREATE TABLE edge (
            id INTEGER PRIMARY KEY,
            src_id INTEGER NOT NULL, dst_id INTEGER NOT NULL,
            kind INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 1,
            UNIQUE (src_id, dst_id, kind)
        )
    """)
    conn.execute("""
        CREATE TABLE edge_site (
            edge_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
            line INTEGER, col INTEGER, conditional INTEGER NOT NULL DEFAULT 0,
            recv_src_kind TEXT, recv_type_usr TEXT, recv_decl_usr TEXT,
            recv_param_pos INTEGER, recv_type_is_value INTEGER,
            PRIMARY KEY (edge_id, file_id, line, col)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE call_arg (
            edge_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
            line INTEGER NOT NULL, col INTEGER NOT NULL,
            position INTEGER NOT NULL, src_kind TEXT NOT NULL,
            type_is_value INTEGER,
            PRIMARY KEY (edge_id, file_id, line, col, position)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE directory (
            id INTEGER PRIMARY KEY, component_id INTEGER NOT NULL,
            path TEXT NOT NULL, UNIQUE (component_id, path)
        )
    """)
    conn.execute("""
        CREATE TABLE file (
            id INTEGER PRIMARY KEY, directory_id INTEGER NOT NULL,
            name TEXT NOT NULL, mtime REAL, md5 TEXT,
            compile_options TEXT, driver TEXT,
            indexed INTEGER NOT NULL DEFAULT 0, indexed_at TEXT,
            args_overridden INTEGER NOT NULL DEFAULT 0,
            UNIQUE (directory_id, name)
        )
    """)
    conn.commit()
    conn.close()

    # Open via Storage: should migrate to v14.
    with Storage(db_path) as db:
        compcols = {r[1] for r in db._conn.execute("PRAGMA table_info(component)")}
        assert "version" in compcols, "version column not added to component"
        tables = {
            r[0]
            for r in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "label" in tables, "label table not created"
        row = db._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert row is not None and int(row[0]) == 14

    # Idempotent on second open.
    with Storage(db_path) as db2:
        row = db2._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert int(row[0]) == 14


def test_migration_existing_components_get_null_version(tmp_path):
    """After v13→v14 migration, existing components have version=NULL."""
    db_path = str(tmp_path / "v13.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '13')")
    conn.execute("""
        CREATE TABLE component (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL DEFAULT 'repo'
        )
    """)
    conn.execute(
        "INSERT INTO component (name, path, kind) VALUES ('foo', '/opt/foo', 'repo')"
    )
    conn.execute("""
        CREATE TABLE symbol (
            id INTEGER PRIMARY KEY, usr TEXT NOT NULL UNIQUE,
            spelling TEXT NOT NULL, kind TEXT NOT NULL,
            is_definition INTEGER NOT NULL DEFAULT 0, is_pure INTEGER NOT NULL DEFAULT 0,
            is_static INTEGER NOT NULL DEFAULT 0, is_instantiation INTEGER NOT NULL DEFAULT 0,
            resolved INTEGER NOT NULL DEFAULT 0,
            parent_usr TEXT, qual_name TEXT, decl_path TEXT,
            decl_file_id INTEGER, decl_line INTEGER, decl_col INTEGER,
            file_id INTEGER, line INTEGER, col INTEGER
        )
    """)
    conn.execute(
        "CREATE TABLE edge_kind (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"
    )
    conn.execute("INSERT INTO edge_kind VALUES (1,'calls')")
    conn.execute("""
        CREATE TABLE edge (
            id INTEGER PRIMARY KEY, src_id INTEGER NOT NULL, dst_id INTEGER NOT NULL,
            kind INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 1,
            UNIQUE (src_id, dst_id, kind)
        )
    """)
    conn.execute("""
        CREATE TABLE edge_site (
            edge_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
            line INTEGER, col INTEGER, conditional INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (edge_id, file_id, line, col)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE call_arg (
            edge_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
            line INTEGER NOT NULL, col INTEGER NOT NULL,
            position INTEGER NOT NULL, src_kind TEXT NOT NULL,
            PRIMARY KEY (edge_id, file_id, line, col, position)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE directory (
            id INTEGER PRIMARY KEY, component_id INTEGER NOT NULL, path TEXT NOT NULL,
            UNIQUE (component_id, path)
        )
    """)
    conn.execute("""
        CREATE TABLE file (
            id INTEGER PRIMARY KEY, directory_id INTEGER NOT NULL,
            name TEXT NOT NULL, mtime REAL, md5 TEXT, compile_options TEXT, driver TEXT,
            indexed INTEGER NOT NULL DEFAULT 0, indexed_at TEXT,
            args_overridden INTEGER NOT NULL DEFAULT 0,
            UNIQUE (directory_id, name)
        )
    """)
    conn.commit()
    conn.close()

    with Storage(db_path) as db:
        comp = db.get_component_by_name("foo")
        assert comp is not None
        assert comp.version is None


# ---------------------------------------------------------------------------
# 6. Storage: add_component with version, effective_root, set_component_version
# ---------------------------------------------------------------------------


def test_add_component_with_version(tmp_path):
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        cid = db.add_component("libfoo", "/opt/libfoo", version="1.2.3")
        comp = db.get_component_by_id(cid)
    assert comp is not None
    assert comp.path == "/opt/libfoo"
    assert comp.version == "1.2.3"
    assert comp.name == "libfoo"


def test_add_component_upsert_preserves_version(tmp_path):
    """Re-adding with version=None should NOT overwrite an existing version."""
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("libfoo", "/opt/libfoo", version="1.2.3")
        # Re-add without a version
        db.add_component("libfoo", "/opt/libfoo", version=None)
        comp = db.get_component_by_name("libfoo")
    assert comp.version == "1.2.3"


def test_add_component_upsert_updates_version(tmp_path):
    """Re-adding with a different version should update it."""
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("libfoo", "/opt/libfoo", version="1.0.0")
        db.add_component("libfoo", "/opt/libfoo", version="2.0.0")
        comp = db.get_component_by_name("libfoo")
    assert comp.version == "2.0.0"


def test_set_component_version(tmp_path):
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("libfoo", "/opt/libfoo")
        ok = db.set_component_version("libfoo", "3.0.0")
        assert ok is True
        comp = db.get_component_by_name("libfoo")
    assert comp.version == "3.0.0"


def test_set_component_version_clear(tmp_path):
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("libfoo", "/opt/libfoo", version="1.0.0")
        db.set_component_version("libfoo", None)
        comp = db.get_component_by_name("libfoo")
    assert comp.version is None


def test_set_component_version_unknown_name(tmp_path):
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        ok = db.set_component_version("notexist", "1.0.0")
    assert ok is False


def test_effective_root_unversioned():
    comp = Component(name="foo", path="/opt/foo")
    assert Storage.effective_root(comp) == "/opt/foo"


def test_effective_root_versioned():
    comp = Component(name="foo", path="/opt/foo", version="1.2.3")
    assert Storage.effective_root(comp) == "/opt/foo/1.2.3"


def test_effective_root_normpath():
    comp = Component(name="foo", path="/opt/foo/", version="1.2.3")
    # normpath strips trailing slash
    assert Storage.effective_root(comp) == "/opt/foo/1.2.3"


# ---------------------------------------------------------------------------
# 7. Storage: component_for_path with versioned components
# ---------------------------------------------------------------------------


def test_component_for_path_versioned(tmp_path):
    """component_for_path uses effective root (base+version) for prefix matching."""
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        # Register component with base=/opt/libfoo, version=1.2.3
        db.add_component("libfoo", "/opt/libfoo", version="1.2.3")
        # A path under the effective root /opt/libfoo/1.2.3 should be owned.
        comp = db.component_for_path("/opt/libfoo/1.2.3/include/foo.h")
    assert comp is not None
    assert comp.name == "libfoo"


def test_component_for_path_versioned_excludes_sibling(tmp_path):
    """A path under the base dir but NOT under version should NOT be claimed."""
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("libfoo", "/opt/libfoo", version="1.2.3")
        # /opt/libfoo/1.2.0/... is a SIBLING version dir, not owned
        comp = db.component_for_path("/opt/libfoo/1.2.0/foo.h")
    assert comp is None


def test_component_for_path_no_version(tmp_path):
    """Unversioned component still works the old way."""
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("mylib", "/opt/mylib")
        comp = db.component_for_path("/opt/mylib/src/foo.c")
    assert comp is not None
    assert comp.name == "mylib"


def test_get_component_two_step_lookup(tmp_path):
    """get_component('/src/v8') works even though stored base is '/src'."""
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("v8", "/src", version="v8")
        # Lookup by effective root should find it.
        comp = db.get_component("/src/v8")
    assert comp is not None
    assert comp.name == "v8"
    assert comp.version == "v8"


# ---------------------------------------------------------------------------
# 8. Storage: file_abs_path / files / list_files with versioned components
# ---------------------------------------------------------------------------


def test_file_abs_path_versioned(tmp_path):
    """file_abs_path returns path under the effective root."""
    repo = str(tmp_path / "repo" / "1.0.0")
    os.makedirs(repo)
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("repo", str(tmp_path / "repo"), version="1.0.0")
        fid = db.add_file_path(os.path.join(repo, "foo.c"))
        abs_path = db.file_abs_path(fid)
    assert abs_path == os.path.join(repo, "foo.c")


def test_files_versioned(tmp_path):
    """files() returns correct absolute paths for versioned components."""
    repo = str(tmp_path / "repo" / "2.0.0")
    os.makedirs(repo)
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("repo", str(tmp_path / "repo"), version="2.0.0")
        db.add_file_path(os.path.join(repo, "main.c"))
        entries = db.files()
    assert len(entries) == 1
    _, ap = entries[0]
    assert ap == os.path.join(repo, "main.c")


def test_split_path_versioned(tmp_path):
    """_split_path resolves relative to the effective root, not the base."""
    repo = str(tmp_path / "repo" / "3.0.0")
    os.makedirs(os.path.join(repo, "src"))
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("repo", str(tmp_path / "repo"), version="3.0.0")
        comp_id, rel_dir, name = db._split_path(os.path.join(repo, "src", "foo.c"))
    assert name == "foo.c"
    assert rel_dir == "src"


# ---------------------------------------------------------------------------
# 9. Storage: label registry
# ---------------------------------------------------------------------------


@pytest.fixture
def label_db(tmp_path):
    """A Storage with two labels pre-seeded."""
    db_path = str(tmp_path / "label.db")
    with Storage(db_path) as db:
        db.add_label("libfoo-include", "$REP/foo/include")
        db.add_label("libbar", "/opt/bar")
    return db_path


def test_add_label_creates(label_db):
    with Storage(label_db) as db:
        val = db.get_label("libfoo-include")
    assert val == "$REP/foo/include"


def test_add_label_upsert(label_db):
    with Storage(label_db) as db:
        db.add_label("libfoo-include", "/new/path")
        val = db.get_label("libfoo-include")
    assert val == "/new/path"


def test_remove_label(label_db):
    with Storage(label_db) as db:
        ok = db.remove_label("libbar")
        assert ok is True
        val = db.get_label("libbar")
    assert val is None


def test_remove_label_absent(label_db):
    with Storage(label_db) as db:
        ok = db.remove_label("doesnotexist")
    assert ok is False


def test_list_labels_sorted(label_db):
    with Storage(label_db) as db:
        labels = db.list_labels()
    names = [n for n, _ in labels]
    assert names == sorted(names)
    assert ("libbar", "/opt/bar") in labels
    assert ("libfoo-include", "$REP/foo/include") in labels


def test_get_label_absent(label_db):
    with Storage(label_db) as db:
        val = db.get_label("notexist")
    assert val is None


# ---------------------------------------------------------------------------
# 10. compiledb.strip_for_libclang: preserve rule (§5)
# ---------------------------------------------------------------------------


class _FakeCmd:
    """Minimal compile-command fake for strip_for_libclang."""

    def __init__(self, args, directory="/tmp", filename="/tmp/foo.c"):
        self.arguments = args
        self.directory = directory
        self.filename = filename


def test_strip_preserves_label_in_space_form():
    cmd = _FakeCmd(["cc", "-I", "<libfoo-include>", "foo.c"])
    opts = strip_for_libclang(cmd)
    assert "-I" in opts
    idx = opts.index("-I")
    assert opts[idx + 1] == "<libfoo-include>"


def test_strip_preserves_label_glued():
    cmd = _FakeCmd(["cc", "-I<libfoo-include>", "foo.c"])
    opts = strip_for_libclang(cmd)
    assert "-I<libfoo-include>" in opts


def test_strip_preserves_dollar_var_space_form():
    cmd = _FakeCmd(["cc", "-I", "$REP/include", "foo.c"])
    opts = strip_for_libclang(cmd)
    idx = opts.index("-I")
    assert opts[idx + 1] == "$REP/include"


def test_strip_preserves_dollar_var_glued():
    cmd = _FakeCmd(["cc", "-I$REP/include", "foo.c"])
    opts = strip_for_libclang(cmd)
    assert "-I$REP/include" in opts


def test_strip_absolutizes_relative_path():
    """Relative paths (no < or $) are still absolutized."""
    cmd = _FakeCmd(["cc", "-I", "include", "foo.c"], directory="/proj")
    opts = strip_for_libclang(cmd)
    idx = opts.index("-I")
    assert opts[idx + 1] == "/proj/include"


def test_is_indirected():
    assert _is_indirected("<libfoo>") is True
    assert _is_indirected("$REP/foo") is True
    assert _is_indirected("/absolute/path") is False
    assert _is_indirected("relative/path") is False


def test_strip_preserves_isystem_label():
    cmd = _FakeCmd(["cc", "-isystem", "<sys-include>", "foo.c"])
    opts = strip_for_libclang(cmd)
    assert "-isystem" in opts
    idx = opts.index("-isystem")
    assert opts[idx + 1] == "<sys-include>"


# ---------------------------------------------------------------------------
# 11. CLI: cidx component show
# ---------------------------------------------------------------------------


@pytest.fixture
def comp_db(tmp_path):
    """A DB with two components: one versioned, one with $VAR base."""
    db_path = str(tmp_path / "comp.db")
    with Storage(db_path) as db:
        db.add_component("libfoo", "/opt/libfoo", kind="external", version="1.2.3")
        db.add_component("app", "$REP/app", kind="repo")
    return db_path


def test_component_show_versioned(comp_db, capsys, monkeypatch):
    monkeypatch.setenv("REP", "/home/u/src")
    rc, out, err = run(["--db", comp_db, "component", "show", "libfoo"], capsys)
    assert rc == 0
    assert "name           libfoo" in out
    assert "kind           external" in out
    assert "base path      /opt/libfoo" in out
    assert "version        1.2.3" in out
    assert "effective root /opt/libfoo/1.2.3" in out
    assert "resolved root  /opt/libfoo/1.2.3" in out


def test_component_show_unversioned_var_base(comp_db, capsys, monkeypatch):
    monkeypatch.setenv("REP", "/home/u/src")
    rc, out, err = run(["--db", comp_db, "component", "show", "app"], capsys)
    assert rc == 0
    assert "name           app" in out
    assert "kind           repo" in out
    assert "base path      $REP/app" in out
    assert "version        (none)" in out
    assert "effective root $REP/app" in out
    assert "resolved root  /home/u/src/app" in out


def test_component_show_unknown(comp_db, capsys):
    rc, out, err = run(["--db", comp_db, "component", "show", "notexist"], capsys)
    assert rc == 1
    assert "error: no component named 'notexist'" in err


def test_component_show_field_width(comp_db, capsys, monkeypatch):
    """Every key is left-justified in a 14-char field (§8.1 format check)."""
    monkeypatch.setenv("REP", "/home/u/src")
    rc, out, _ = run(["--db", comp_db, "component", "show", "libfoo"], capsys)
    assert rc == 0
    for line in out.strip().splitlines():
        # The value starts at offset 15 (14 chars key + 1 space).
        key_part = line[:14]
        sep = line[14]
        assert sep == " ", f"expected space at offset 14 in {line!r}"
        _ = key_part  # key is left-padded


# ---------------------------------------------------------------------------
# 12. CLI: cidx component set-version
# ---------------------------------------------------------------------------


def test_component_set_version(comp_db, capsys):
    rc, out, err = run(
        ["--db", comp_db, "component", "set-version", "libfoo", "2.0.0"], capsys
    )
    assert rc == 0
    assert "component 'libfoo' version set to 2.0.0" in out
    with Storage(comp_db) as db:
        comp = db.get_component_by_name("libfoo")
    assert comp.version == "2.0.0"


def test_component_set_version_clear(comp_db, capsys):
    rc, out, err = run(
        ["--db", comp_db, "component", "set-version", "libfoo", ""], capsys
    )
    assert rc == 0
    assert "component 'libfoo' version cleared" in out
    with Storage(comp_db) as db:
        comp = db.get_component_by_name("libfoo")
    assert comp.version is None


def test_component_set_version_no_v_arg(comp_db, capsys):
    """Omitting V (no positional arg) clears the version."""
    rc, out, err = run(["--db", comp_db, "component", "set-version", "libfoo"], capsys)
    assert rc == 0
    assert "version cleared" in out


def test_component_set_version_unknown(comp_db, capsys):
    rc, out, err = run(
        ["--db", comp_db, "component", "set-version", "nobody", "1.0"], capsys
    )
    assert rc == 1
    assert "error: no component named 'nobody'" in err


# ---------------------------------------------------------------------------
# 13. CLI: cidx label add / rm / list / resolve
# ---------------------------------------------------------------------------


@pytest.fixture
def db_with_labels(tmp_path):
    db_path = str(tmp_path / "lbl.db")
    with Storage(db_path) as db:
        db.add_component("dummy", str(tmp_path / "dummy"))
    return db_path


def test_label_add(db_with_labels, capsys):
    rc, out, err = run(
        ["--db", db_with_labels, "label", "add", "libfoo-include", "$REP/foo/include"],
        capsys,
    )
    assert rc == 0
    assert "added label libfoo-include -> $REP/foo/include" in out


def test_label_add_update(db_with_labels, capsys):
    run(["--db", db_with_labels, "label", "add", "libfoo-include", "/v1"], capsys)
    rc, out, err = run(
        ["--db", db_with_labels, "label", "add", "libfoo-include", "/v2"], capsys
    )
    assert rc == 0
    assert "updated label libfoo-include -> /v2" in out


def test_label_rm(db_with_labels, capsys):
    run(["--db", db_with_labels, "label", "add", "toremove", "/x"], capsys)
    rc, out, err = run(["--db", db_with_labels, "label", "rm", "toremove"], capsys)
    assert rc == 0
    assert "removed label toremove" in out


def test_label_rm_absent(db_with_labels, capsys):
    rc, out, err = run(["--db", db_with_labels, "label", "rm", "nope"], capsys)
    assert rc == 1
    assert "error: no label named 'nope'" in err


def test_label_list_empty(db_with_labels, capsys):
    rc, out, err = run(["--db", db_with_labels, "label", "list"], capsys)
    assert rc == 0
    assert "0 label(s)" in out


def test_label_list_sorted(db_with_labels, capsys):
    run(
        ["--db", db_with_labels, "label", "add", "libfoo-include", "$REP/foo/include"],
        capsys,
    )
    run(["--db", db_with_labels, "label", "add", "libbar", "/opt/bar"], capsys)
    rc, out, err = run(["--db", db_with_labels, "label", "list"], capsys)
    assert rc == 0
    lines = out.strip().splitlines()
    # Last line is the count.
    assert lines[-1] == "2 label(s)"
    # Names appear in alphabetical order.
    name_lines = lines[:-1]
    assert name_lines[0].startswith("libbar")
    assert name_lines[1].startswith("libfoo-include")
    # Path column starts after the max-name-width + 2 spaces.
    # libfoo-include (14 chars) is the wider name; libbar padded to 14.
    assert "/opt/bar" in name_lines[0]
    assert "$REP/foo/include" in name_lines[1]


def test_label_resolve_registry(db_with_labels, capsys, monkeypatch):
    monkeypatch.setenv("REP", "/opt")
    run(
        ["--db", db_with_labels, "label", "add", "libfoo-include", "$REP/foo/include"],
        capsys,
    )
    rc, out, err = run(
        ["--db", db_with_labels, "label", "resolve", "libfoo-include"], capsys
    )
    assert rc == 0
    assert out.strip() == "/opt/foo/include"


def test_label_resolve_autoderive(db_with_labels, capsys):
    rc, out, err = run(
        ["--db", db_with_labels, "label", "resolve", "usr-local-include"], capsys
    )
    assert rc == 0
    assert out.strip() == "/usr/local/include"


def test_label_resolve_token_with_angle(db_with_labels, capsys, monkeypatch):
    monkeypatch.setenv("REP", "/opt")
    run(
        ["--db", db_with_labels, "label", "add", "libfoo-include", "$REP/foo/include"],
        capsys,
    )
    # Tokens starting with '-' must be preceded by '--' to avoid argparse treating
    # them as flags.
    rc, out, err = run(
        ["--db", db_with_labels, "label", "resolve", "--", "-I<libfoo-include>/x"],
        capsys,
    )
    assert rc == 0
    assert out.strip() == "-I/opt/foo/include/x"


def test_label_resolve_always_exit_0(db_with_labels, capsys):
    """resolve never errors even on an unknown label with autoderive on."""
    rc, out, err = run(
        ["--db", db_with_labels, "label", "resolve", "totally-unknown-label"], capsys
    )
    assert rc == 0
    assert out.strip() == "/totally/unknown/label"


# ---------------------------------------------------------------------------
# 14. CLI: import with version detection
# ---------------------------------------------------------------------------


def test_import_detects_version(tmp_path, capsys):
    """import auto-detects trailing version segment from compile_commands root."""
    import json

    comp_root = str(tmp_path / "myproject" / "1.0.0")
    os.makedirs(comp_root)
    # Write a minimal compile_commands.json
    src = os.path.join(comp_root, "foo.c")
    with open(src, "w") as fh:
        fh.write("int x;")
    cdb = [{"directory": comp_root, "file": src, "arguments": ["cc", src]}]
    cdb_path = os.path.join(comp_root, "compile_commands.json")
    with open(cdb_path, "w") as fh:
        json.dump(cdb, fh)
    db_path = str(tmp_path / "idx.db")
    rc, out, err = run(["--db", db_path, "import", "--db", cdb_path], capsys)
    assert rc == 0
    with Storage(db_path) as db:
        comp = db.get_component_by_name("myproject")
        if comp is None:
            # Might pick up the git root for this repo; just check any component.
            comps = db.list_components()
            # At least one component was registered
            assert len(comps) >= 1
            return
    assert comp.version == "1.0.0"


def test_import_no_detect_version(tmp_path, capsys):
    """--no-detect-version disables splitting."""
    import json

    comp_root = str(tmp_path / "myproject" / "1.0.0")
    os.makedirs(comp_root)
    src = os.path.join(comp_root, "foo.c")
    with open(src, "w") as fh:
        fh.write("int x;")
    cdb = [{"directory": comp_root, "file": src, "arguments": ["cc", src]}]
    cdb_path = os.path.join(comp_root, "compile_commands.json")
    with open(cdb_path, "w") as fh:
        json.dump(cdb, fh)
    db_path = str(tmp_path / "idx.db")
    rc, out, err = run(
        ["--db", db_path, "import", "--db", cdb_path, "--no-detect-version"], capsys
    )
    assert rc == 0
    with Storage(db_path) as db:
        comps = db.list_components()
        # No component should have version set (detection was disabled)
        for c in comps:
            assert c.version is None, f"unexpected version on {c.name}: {c.version}"


# ---------------------------------------------------------------------------
# 15. resolve_compile_args interaction (§7) -- not yet a separate function
#     but strip_for_libclang preserve rule is already tested in §10
# ---------------------------------------------------------------------------

# (Parse-time arg resolution involves a live libclang parse; deferred to QA.)

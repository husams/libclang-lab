"""`cidx migrate` — deliberate in-place schema upgrade (no re-index)."""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from indexer.cli import main  # noqa: E402
from indexer.storage import SCHEMA_VERSION  # noqa: E402


def _build_v15(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version','15');
        CREATE TABLE component (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'repo', version TEXT);
        CREATE TABLE directory (id INTEGER PRIMARY KEY, component_id INTEGER, path TEXT);
        CREATE TABLE file (id INTEGER PRIMARY KEY, directory_id INTEGER, name TEXT NOT NULL,
            mtime REAL, md5 TEXT, compile_options TEXT, driver TEXT,
            indexed INTEGER NOT NULL DEFAULT 0, indexed_at TEXT,
            args_overridden INTEGER NOT NULL DEFAULT 0, UNIQUE(directory_id,name));
        CREATE TABLE symbol (id INTEGER PRIMARY KEY, usr TEXT NOT NULL UNIQUE,
            spelling TEXT NOT NULL, qual_name TEXT, display_name TEXT,
            kind TEXT NOT NULL CHECK (kind IN ('class','struct','function','method','macro')),
            type_info TEXT, file_id INTEGER, line INTEGER, col INTEGER,
            decl_file_id INTEGER, decl_line INTEGER, decl_col INTEGER, decl_path TEXT,
            is_definition INTEGER NOT NULL DEFAULT 0, is_pure INTEGER NOT NULL DEFAULT 0,
            is_static INTEGER NOT NULL DEFAULT 0, is_instantiation INTEGER NOT NULL DEFAULT 0,
            linkage TEXT, access TEXT, parent_usr TEXT, resolved INTEGER NOT NULL DEFAULT 0);
        INSERT INTO symbol (usr,spelling,kind) VALUES
            ('c:@F@f','f','function'), ('c:@S@S','S','struct');
        """
    )
    c.commit()
    c.close()


def _ver(path: str) -> int:
    con = sqlite3.connect(path)
    try:
        return int(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
    finally:
        con.close()


def test_migrate_upgrades_v15_in_place(tmp_path, capsys):
    db = str(tmp_path / "v15.db")
    _build_v15(db)
    assert _ver(db) == 15

    rc = main(["migrate", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"v15 -> v{SCHEMA_VERSION}" in out
    assert _ver(db) == SCHEMA_VERSION

    # kind is now the CXCursorKind int, symbol_kind table seeded, rows preserved.
    con = sqlite3.connect(db)
    rows = dict(con.execute("SELECT usr, kind FROM symbol"))
    types = {r[0] for r in con.execute("SELECT DISTINCT typeof(kind) FROM symbol")}
    has_meta = con.execute("SELECT COUNT(*) FROM symbol_kind").fetchone()[0]
    con.close()
    assert types == {"integer"}
    assert rows == {"c:@F@f": 8, "c:@S@S": 2}  # function=8, struct=2
    assert has_meta == 17


def test_migrate_is_idempotent(tmp_path, capsys):
    db = str(tmp_path / "v15.db")
    _build_v15(db)
    main(["migrate", "--db", db])
    capsys.readouterr()
    rc = main(["migrate", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "already at schema" in out


def test_migrate_missing_db_errors(tmp_path, capsys):
    rc = main(["migrate", "--db", str(tmp_path / "nope.db")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no index database" in err

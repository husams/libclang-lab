"""indexer.storage -- SQLite persistence layer for the cidx symbol index.

Schema (all stdlib sqlite3, no dependencies):

    component   one indexed code base (a git repo) or an external library
    directory   a directory, path relative to its component root
    file        a source/header file inside a directory; tracks indexing state
    symbol      one declaration/definition, keyed by its clang USR (unique)

A symbol's location is (file_id, line, col); the absolute path is recovered by
joining component.path / directory.path / file.name, so moving a repo only
requires updating one component row.

Usage:
    with Storage(".cidx/index.db") as db:
        comp_id = db.add_component("librdkafka", "/path/to/librdkafka")
        dir_id  = db.add_directory(comp_id, "src")
        file_id = db.add_file(dir_id, "rdkafka.c", mtime=1718000000.0)
        db.add_symbol(Symbol(usr="c:@F@rd_kafka_new", spelling="rd_kafka_new",
                             kind="function", file_id=file_id, line=42, col=1))
        sym = db.lookup_symbol("c:@F@rd_kafka_new")
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, fields
from typing import Any, Optional

SCHEMA_VERSION = 6

#: Allowed values for symbol.kind. Superset of the cidx brief: the core C/C++
#: declaration kinds plus the ones any real walk over a TU produces.
SYMBOL_KINDS = frozenset({
    "class",
    "struct",
    "union",
    "function",
    "method",
    "member",            # data member / field
    "constructor",
    "destructor",
    "enum",
    "enum-constant",
    "typedef",
    "type-alias",
    "class-template",
    "function-template",
    "variable",
    "namespace",
    "macro",
})

_SCHEMA = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS component (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    path  TEXT NOT NULL UNIQUE,         -- repo root (where .git lives) or
                                        -- header root for an external library
    kind  TEXT NOT NULL DEFAULT 'repo'
          CHECK (kind IN ('repo', 'external'))
);

CREATE TABLE IF NOT EXISTS directory (
    id           INTEGER PRIMARY KEY,
    component_id INTEGER NOT NULL REFERENCES component(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,         -- relative to component.path ('' = root)
    UNIQUE (component_id, path)
);

CREATE TABLE IF NOT EXISTS file (
    id              INTEGER PRIMARY KEY,
    directory_id    INTEGER NOT NULL REFERENCES directory(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    mtime           REAL,               -- source mtime at index time
    md5             TEXT,               -- content hash at import time
    compile_options TEXT,               -- JSON list of stripped parse args
    driver          TEXT,               -- argv[0] of the compile command; its
                                        -- system include paths are replicated
                                        -- at parse time (custom toolchains)
    indexed         INTEGER NOT NULL DEFAULT 0,
    indexed_at      TEXT,               -- ISO timestamp of last successful index
    UNIQUE (directory_id, name)
);

CREATE TABLE IF NOT EXISTS symbol (
    id           INTEGER PRIMARY KEY,
    usr          TEXT NOT NULL UNIQUE,  -- clang Unified Symbol Resolution
    spelling     TEXT NOT NULL,
    qual_name    TEXT,                  -- fully qualified, e.g. 'RdKafka::ConfImpl::set'
    display_name TEXT,                  -- spelling + signature, e.g. 'multiply(int, int)'
    kind         TEXT NOT NULL CHECK (kind IN ({", ".join(repr(k) for k in sorted(SYMBOL_KINDS))})),
    type_info    TEXT,                  -- cursor.type.spelling
    file_id      INTEGER REFERENCES file(id) ON DELETE SET NULL,
    line         INTEGER,                     -- definition site once seen,
    col          INTEGER,                     -- else the declaration site
    decl_file_id INTEGER REFERENCES file(id) ON DELETE SET NULL,
    decl_line    INTEGER,                     -- declaration site (e.g. the .h
    decl_col     INTEGER,                     -- prototype); NULL if none seen
    is_definition INTEGER NOT NULL DEFAULT 0,
    is_pure      INTEGER NOT NULL DEFAULT 0,  -- C++: pure virtual ('= 0'), so
                                              -- no definition can ever exist
    linkage      TEXT,                  -- 'external' | 'internal' | 'no-linkage' | ...
    access       TEXT,                  -- C++: 'public' | 'protected' | 'private'
    parent_usr   TEXT,                  -- semantic parent (class/namespace) USR
    resolved     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_symbol_spelling ON symbol(spelling);
CREATE INDEX IF NOT EXISTS idx_symbol_qual     ON symbol(qual_name);
CREATE INDEX IF NOT EXISTS idx_symbol_file     ON symbol(file_id);
CREATE INDEX IF NOT EXISTS idx_symbol_parent   ON symbol(parent_usr);
CREATE INDEX IF NOT EXISTS idx_symbol_kind     ON symbol(kind);

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');
"""


@dataclass
class Component:
    name: str
    path: str
    kind: str = "repo"
    id: Optional[int] = None


@dataclass
class Directory:
    component_id: int
    path: str
    id: Optional[int] = None


@dataclass
class File:
    directory_id: int
    name: str
    mtime: Optional[float] = None
    md5: Optional[str] = None
    compile_options: Optional[list[str]] = None
    driver: Optional[str] = None
    indexed: bool = False
    indexed_at: Optional[str] = None
    id: Optional[int] = None


@dataclass
class Symbol:
    usr: str
    spelling: str
    kind: str
    qual_name: Optional[str] = None
    display_name: Optional[str] = None
    type_info: Optional[str] = None
    file_id: Optional[int] = None
    line: Optional[int] = None
    col: Optional[int] = None
    decl_file_id: Optional[int] = None
    decl_line: Optional[int] = None
    decl_col: Optional[int] = None
    is_definition: bool = False
    is_pure: bool = False
    linkage: Optional[str] = None
    access: Optional[str] = None
    parent_usr: Optional[str] = None
    resolved: bool = False
    id: Optional[int] = None


def _row_to(cls, row: Optional[sqlite3.Row]) -> Any:
    if row is None:
        return None
    kwargs = {f.name: row[f.name] for f in fields(cls)}
    if cls is Symbol:
        kwargs["is_definition"] = bool(kwargs["is_definition"])
        kwargs["is_pure"] = bool(kwargs["is_pure"])
        kwargs["resolved"] = bool(kwargs["resolved"])
    if cls is File:
        kwargs["indexed"] = bool(kwargs["indexed"])
        if kwargs["compile_options"] is not None:
            kwargs["compile_options"] = json.loads(kwargs["compile_options"])
    return cls(**kwargs)


class Storage:
    """All access to the index database goes through this class.

    Every public mutator commits; wrap bulk work in `with db.transaction():`
    to batch commits (row-at-a-time autocommit is the classic 100x slowdown).
    """

    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()             # before _SCHEMA: its indexes need new columns
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._in_txn = False

    def _migrate(self) -> None:
        """In-place upgrade of a database created by an older schema version.

        v2 -> v3: adds symbol.qual_name and backfills it by walking the stored
        parent_usr chains (the longest chain per symbol is the full path).
        v3 -> v4: adds symbol.decl_file_id/decl_line/decl_col. For rows that
        are still declaration-only the stored location IS the declaration, so
        it is copied over; definition rows get their decl site on reindex.
        v5 -> v6: adds file.driver (compile-command argv[0]); backfilled on
        the next `import`.
        """
        tables = {r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "symbol" not in tables:
            return                  # fresh database: _SCHEMA creates everything
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(symbol)")}
        changed = False
        if "qual_name" not in cols:
            self._conn.execute("ALTER TABLE symbol ADD COLUMN qual_name TEXT")
            self._conn.execute("""
                WITH RECURSIVE chain(id, parent_usr, qual) AS (
                    SELECT id, parent_usr, spelling FROM symbol
                    UNION ALL
                    SELECT c.id, p.parent_usr,
                           CASE WHEN p.spelling = '' THEN c.qual
                                ELSE p.spelling || '::' || c.qual END
                    FROM chain c JOIN symbol p ON p.usr = c.parent_usr
                )
                UPDATE symbol SET qual_name = (
                    SELECT qual FROM chain WHERE chain.id = symbol.id
                    ORDER BY LENGTH(qual) DESC LIMIT 1
                )
            """)
            changed = True
        if "decl_file_id" not in cols:
            self._conn.execute(
                "ALTER TABLE symbol ADD COLUMN decl_file_id INTEGER "
                "REFERENCES file(id) ON DELETE SET NULL")
            self._conn.execute("ALTER TABLE symbol ADD COLUMN decl_line INTEGER")
            self._conn.execute("ALTER TABLE symbol ADD COLUMN decl_col INTEGER")
            self._conn.execute(
                "UPDATE symbol SET decl_file_id = file_id, decl_line = line, "
                "decl_col = col WHERE is_definition = 0")
            changed = True
        if "is_pure" not in cols:
            # No backfill possible from stored data -- reindex to populate.
            self._conn.execute(
                "ALTER TABLE symbol ADD COLUMN is_pure INTEGER NOT NULL DEFAULT 0")
            changed = True
        fcols = {r[1] for r in self._conn.execute("PRAGMA table_info(file)")}
        if "file" in tables and "driver" not in fcols:
            # No backfill possible from stored data -- re-import to populate.
            self._conn.execute("ALTER TABLE file ADD COLUMN driver TEXT")
            changed = True
        if changed:
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def transaction(self):
        """Context manager batching many mutations into one commit."""
        return _Transaction(self)

    def _commit(self) -> None:
        if not self._in_txn:
            self._conn.commit()

    # -- components ----------------------------------------------------------

    def add_component(self, name: str, path: str, kind: str = "repo") -> int:
        """Insert a component; idempotent on path. Returns the component id."""
        path = os.path.abspath(path)
        cur = self._conn.execute(
            "INSERT INTO component (name, path, kind) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET name = excluded.name, kind = excluded.kind "
            "RETURNING id",
            (name, path, kind),
        )
        cid = cur.fetchone()["id"]
        self._commit()
        return cid

    def get_component_by_name(self, name: str) -> Optional[Component]:
        row = self._conn.execute(
            "SELECT * FROM component WHERE name = ?", (name,)
        ).fetchone()
        return _row_to(Component, row)

    def get_component(self, path: str) -> Optional[Component]:
        row = self._conn.execute(
            "SELECT * FROM component WHERE path = ?", (os.path.abspath(path),)
        ).fetchone()
        return _row_to(Component, row)

    def get_component_by_id(self, component_id: int) -> Optional[Component]:
        row = self._conn.execute(
            "SELECT * FROM component WHERE id = ?", (component_id,)
        ).fetchone()
        return _row_to(Component, row)

    def component_for_path(self, abs_path: str) -> Optional[Component]:
        """Longest-prefix match: which component owns this absolute path?"""
        abs_path = os.path.abspath(abs_path)
        best = None
        for row in self._conn.execute("SELECT * FROM component"):
            root = row["path"].rstrip(os.sep)
            if abs_path == root or abs_path.startswith(root + os.sep):
                if best is None or len(root) > len(best["path"]):
                    best = row
        return _row_to(Component, best)

    @staticmethod
    def _fuzzy_like(text: str) -> str:
        """LIKE pattern for fzf-style fuzzy matching (use with ESCAPE '\\').

        Every non-space character of `text` must appear in the column, in
        order: 'shp' matches 'shapes.c'. LIKE is case-insensitive for ASCII.
        """
        chars = [c.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
                 for c in text if not c.isspace()]
        return "%" + "%".join(chars) + "%"

    def list_components(self, name: Optional[str] = None,
                        kind: Optional[str] = None) -> list[Component]:
        """All components, optionally fuzzy-filtered by name and/or kind."""
        sql = "SELECT * FROM component"
        where, args = [], []
        if name:
            where.append(r"name LIKE ? ESCAPE '\'")
            args.append(self._fuzzy_like(name))
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY name, path"
        return [_row_to(Component, r) for r in self._conn.execute(sql, args)]

    # -- directories ---------------------------------------------------------

    def add_directory(self, component_id: int, path: str) -> int:
        """Insert a directory (path relative to its component); idempotent."""
        path = os.path.normpath(path) if path else "."
        if path == ".":
            path = ""
        cur = self._conn.execute(
            "INSERT INTO directory (component_id, path) VALUES (?, ?) "
            "ON CONFLICT(component_id, path) DO UPDATE SET path = excluded.path "
            "RETURNING id",
            (component_id, path),
        )
        did = cur.fetchone()["id"]
        self._commit()
        return did

    def get_directory(self, component_id: int, path: str) -> Optional[Directory]:
        row = self._conn.execute(
            "SELECT * FROM directory WHERE component_id = ? AND path = ?",
            (component_id, os.path.normpath(path) if path not in ("", ".") else ""),
        ).fetchone()
        return _row_to(Directory, row)

    def list_directories(self, component_id: Optional[int] = None,
                         name: Optional[str] = None
                         ) -> list[tuple[Directory, str]]:
        """(Directory, component name) pairs, optionally scoped to one
        component and/or fuzzy-filtered on the relative directory path."""
        sql = ("SELECT d.*, c.name AS comp_name "
               "FROM directory d JOIN component c ON c.id = d.component_id")
        where, args = [], []
        if component_id is not None:
            where.append("d.component_id = ?")
            args.append(component_id)
        if name:
            where.append(r"d.path LIKE ? ESCAPE '\'")
            args.append(self._fuzzy_like(name))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY c.name, d.path"
        return [(_row_to(Directory, r), r["comp_name"])
                for r in self._conn.execute(sql, args)]

    def get_directory_by_id(self, directory_id: int) -> Optional[Directory]:
        row = self._conn.execute(
            "SELECT * FROM directory WHERE id = ?", (directory_id,)
        ).fetchone()
        return _row_to(Directory, row)

    @staticmethod
    def _dir_scope_sql(dir_path: str, args: list) -> str:
        """WHERE fragment matching a directory and its whole subtree."""
        rel = os.path.normpath(dir_path)
        if rel in (".", ""):
            rel = ""
        esc = rel.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        # '' is the component root: its subtree is every directory.
        args.extend([rel, esc + os.sep + "%" if rel else "%"])
        return r"(d.path = ? OR d.path LIKE ? ESCAPE '\')"

    # -- files ----------------------------------------------------------------

    def add_file(self, directory_id: int, name: str,
                 mtime: Optional[float] = None,
                 md5: Optional[str] = None,
                 compile_options: Optional[list[str]] = None,
                 driver: Optional[str] = None) -> int:
        """Insert a file row; idempotent on (directory, name). Returns file id.

        Re-adding with a *different* md5 resets the indexed flag (the content
        changed, so the stored symbols are stale).
        """
        opts = json.dumps(compile_options) if compile_options is not None else None
        cur = self._conn.execute(
            "INSERT INTO file (directory_id, name, mtime, md5, compile_options, driver) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(directory_id, name) DO UPDATE SET "
            "  mtime           = COALESCE(excluded.mtime, file.mtime), "
            "  compile_options = COALESCE(excluded.compile_options, file.compile_options), "
            "  driver          = COALESCE(excluded.driver, file.driver), "
            "  indexed         = CASE WHEN excluded.md5 IS NOT NULL "
            "                          AND excluded.md5 IS NOT file.md5 "
            "                         THEN 0 ELSE file.indexed END, "
            "  md5             = COALESCE(excluded.md5, file.md5) "
            "RETURNING id",
            (directory_id, name, mtime, md5, opts, driver),
        )
        fid = cur.fetchone()["id"]
        self._commit()
        return fid

    def add_file_path(self, abs_path: str, mtime: Optional[float] = None,
                      md5: Optional[str] = None,
                      compile_options: Optional[list[str]] = None,
                      driver: Optional[str] = None) -> int:
        """Convenience: register an absolute path, creating the directory row.

        The owning component must already exist (add_component first).
        """
        comp_id, rel_dir, name = self._split_path(abs_path)
        dir_id = self.add_directory(comp_id, rel_dir)
        return self.add_file(dir_id, name, mtime=mtime, md5=md5,
                             compile_options=compile_options, driver=driver)

    def get_file(self, abs_path: str) -> Optional[File]:
        """File row for an absolute path, or None."""
        try:
            comp_id, rel_dir, name = self._split_path(abs_path)
        except KeyError:
            return None
        row = self._conn.execute(
            "SELECT f.* FROM file f JOIN directory d ON d.id = f.directory_id "
            "WHERE d.component_id = ? AND d.path = ? AND f.name = ?",
            (comp_id, rel_dir, name),
        ).fetchone()
        return _row_to(File, row)

    def get_file_by_id(self, file_id: int) -> Optional[File]:
        row = self._conn.execute(
            "SELECT * FROM file WHERE id = ?", (file_id,)
        ).fetchone()
        return _row_to(File, row)

    def files(self) -> list[tuple[File, str]]:
        """Every file row with its reconstructed absolute path, sorted by path."""
        rows = self._conn.execute(
            "SELECT f.*, c.path AS root, d.path AS rel "
            "FROM file f JOIN directory d ON d.id = f.directory_id "
            "JOIN component c ON c.id = d.component_id "
            "ORDER BY c.path, d.path, f.name"
        ).fetchall()
        out = []
        for row in rows:
            abs_path = os.path.join(row["root"], row["rel"], row["name"]) \
                if row["rel"] else os.path.join(row["root"], row["name"])
            out.append((_row_to(File, row), abs_path))
        return out

    def list_files(self, component_id: Optional[int] = None,
                   dir_path: Optional[str] = None,
                   name: Optional[str] = None,
                   indexed: Optional[bool] = None) -> list[tuple[File, str]]:
        """Like files(), with optional filters: component, directory subtree,
        fuzzy file name, and indexed state."""
        sql = ("SELECT f.*, c.path AS root, d.path AS rel "
               "FROM file f JOIN directory d ON d.id = f.directory_id "
               "JOIN component c ON c.id = d.component_id")
        where, args = [], []
        if component_id is not None:
            where.append("d.component_id = ?")
            args.append(component_id)
        if dir_path is not None:
            where.append(self._dir_scope_sql(dir_path, args))
        if name:
            where.append(r"f.name LIKE ? ESCAPE '\'")
            args.append(self._fuzzy_like(name))
        if indexed is not None:
            where.append("f.indexed = ?")
            args.append(int(indexed))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY c.path, d.path, f.name"
        out = []
        for row in self._conn.execute(sql, args):
            abs_path = os.path.join(row["root"], row["rel"], row["name"]) \
                if row["rel"] else os.path.join(row["root"], row["name"])
            out.append((_row_to(File, row), abs_path))
        return out

    def mark_file_indexed(self, file_id: int, mtime: Optional[float] = None) -> None:
        self._conn.execute(
            "UPDATE file SET indexed = 1, indexed_at = datetime('now'), "
            "  mtime = COALESCE(?, mtime) WHERE id = ?",
            (mtime, file_id),
        )
        self._commit()

    def is_file_indexed(self, abs_path: str, mtime: Optional[float] = None,
                        md5: Optional[str] = None) -> bool:
        """True if the file has been indexed (and is not stale, if mtime/md5 given).

        `mtime`/`md5` describe the file's *current* state: pass either to also
        treat a changed file as NOT indexed (incremental reindex).
        """
        f = self.get_file(abs_path)
        if f is None or not f.indexed:
            return False
        if mtime is not None and (f.mtime is None or f.mtime < mtime):
            return False
        if md5 is not None and f.md5 != md5:
            return False
        return True

    def file_abs_path(self, file_id: int) -> Optional[str]:
        """component.path / directory.path / file.name for a file id."""
        row = self._conn.execute(
            "SELECT c.path AS root, d.path AS rel, f.name AS name "
            "FROM file f JOIN directory d ON d.id = f.directory_id "
            "JOIN component c ON c.id = d.component_id WHERE f.id = ?",
            (file_id,),
        ).fetchone()
        if row is None:
            return None
        return os.path.join(row["root"], row["rel"], row["name"]) if row["rel"] \
            else os.path.join(row["root"], row["name"])

    def _split_path(self, abs_path: str) -> tuple[int, str, str]:
        """Absolute path -> (component_id, relative dir, file name)."""
        abs_path = os.path.abspath(abs_path)
        comp = self.component_for_path(abs_path)
        if comp is None or comp.id is None:
            raise KeyError(f"no component owns {abs_path} (add_component first)")
        rel = os.path.relpath(abs_path, comp.path)
        rel_dir, name = os.path.split(rel)
        if rel_dir == ".":
            rel_dir = ""
        return comp.id, rel_dir, name

    # -- symbols ---------------------------------------------------------------

    _SYMBOL_COLS = ("usr", "spelling", "qual_name", "display_name", "kind",
                    "type_info", "file_id", "line", "col",
                    "decl_file_id", "decl_line", "decl_col",
                    "is_definition", "is_pure", "linkage", "access",
                    "parent_usr", "resolved")

    def add_symbol(self, sym: Symbol) -> int:
        """Insert or upsert a symbol keyed by USR. Returns the symbol id.

        A definition always wins over a previously stored declaration; a
        declaration never downgrades a stored definition's location.
        """
        if sym.kind not in SYMBOL_KINDS:
            raise ValueError(f"unknown symbol kind {sym.kind!r}")
        vals = tuple(getattr(sym, c) for c in self._SYMBOL_COLS)
        cur = self._conn.execute(
            f"INSERT INTO symbol ({', '.join(self._SYMBOL_COLS)}) "
            f"VALUES ({', '.join('?' * len(self._SYMBOL_COLS))}) "
            "ON CONFLICT(usr) DO UPDATE SET "
            "  spelling      = excluded.spelling, "
            "  qual_name     = COALESCE(excluded.qual_name, symbol.qual_name), "
            "  display_name  = COALESCE(excluded.display_name, symbol.display_name), "
            "  kind          = excluded.kind, "
            "  type_info     = COALESCE(excluded.type_info, symbol.type_info), "
            "  file_id       = CASE WHEN excluded.is_definition >= symbol.is_definition "
            "                       THEN excluded.file_id ELSE symbol.file_id END, "
            "  line          = CASE WHEN excluded.is_definition >= symbol.is_definition "
            "                       THEN excluded.line ELSE symbol.line END, "
            "  col           = CASE WHEN excluded.is_definition >= symbol.is_definition "
            "                       THEN excluded.col ELSE symbol.col END, "
            "  decl_file_id  = COALESCE(excluded.decl_file_id, symbol.decl_file_id), "
            "  decl_line     = COALESCE(excluded.decl_line, symbol.decl_line), "
            "  decl_col      = COALESCE(excluded.decl_col, symbol.decl_col), "
            "  is_definition = MAX(excluded.is_definition, symbol.is_definition), "
            "  is_pure       = MAX(excluded.is_pure, symbol.is_pure), "
            "  linkage       = COALESCE(excluded.linkage, symbol.linkage), "
            "  access        = COALESCE(excluded.access, symbol.access), "
            "  parent_usr    = COALESCE(excluded.parent_usr, symbol.parent_usr), "
            "  resolved      = MAX(excluded.resolved, symbol.resolved) "
            "RETURNING id",
            vals,
        )
        sid = cur.fetchone()["id"]
        self._commit()
        return sid

    def update_symbol(self, usr: str, **values: Any) -> bool:
        """Update named columns of the symbol with this USR. Returns False if absent."""
        bad = set(values) - set(self._SYMBOL_COLS)
        if bad:
            raise ValueError(f"unknown symbol column(s): {sorted(bad)}")
        if "kind" in values and values["kind"] not in SYMBOL_KINDS:
            raise ValueError(f"unknown symbol kind {values['kind']!r}")
        if not values:
            return self.lookup_symbol(usr) is not None
        sets = ", ".join(f"{c} = ?" for c in values)
        cur = self._conn.execute(
            f"UPDATE symbol SET {sets} WHERE usr = ?", (*values.values(), usr)
        )
        self._commit()
        return cur.rowcount > 0

    def lookup_symbol(self, usr: str) -> Optional[Symbol]:
        row = self._conn.execute(
            "SELECT * FROM symbol WHERE usr = ?", (usr,)
        ).fetchone()
        return _row_to(Symbol, row)

    def lookup_symbol_by_id(self, symbol_id: int) -> Optional[Symbol]:
        row = self._conn.execute(
            "SELECT * FROM symbol WHERE id = ?", (symbol_id,)
        ).fetchone()
        return _row_to(Symbol, row)

    def lookup_symbols_by_name(self, spelling: str,
                               kind: Optional[str] = None) -> list[Symbol]:
        """All symbols with this spelling (overloads / statics give several rows)."""
        sql = "SELECT * FROM symbol WHERE spelling = ?"
        args: list[Any] = [spelling]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY usr"
        return [_row_to(Symbol, r) for r in self._conn.execute(sql, args)]

    def search_symbols(self, pattern: str,
                       kind: Optional[str] = None) -> list[Symbol]:
        """Fuzzy match against the qualified name (case-insensitive).

        Each '::'-separated segment of `pattern` must appear, in order, as a
        substring of qual_name: 'conf::set' matches 'RdKafka::ConfImpl::set'.
        """
        like = "%" + "%".join(
            seg.replace("%", r"\%").replace("_", r"\_")
            for seg in pattern.split("::") if seg
        ) + "%"
        sql = r"SELECT * FROM symbol WHERE qual_name LIKE ? ESCAPE '\'"
        args: list[Any] = [like]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY LENGTH(qual_name), qual_name"
        return [_row_to(Symbol, r) for r in self._conn.execute(sql, args)]

    def list_symbols(self, component_id: Optional[int] = None,
                     dir_path: Optional[str] = None,
                     file_id: Optional[int] = None,
                     name: Optional[str] = None,
                     kind: Optional[str] = None) -> list[Symbol]:
        """Symbols filtered by location and/or name.

        Location scoping (component / directory subtree / file) matches a
        symbol if EITHER its definition site or its declaration site falls
        inside the scope -- so listing a header shows the prototypes whose
        definitions live in a .c file. `name` is a free-text fuzzy match
        against the qualified name (spelling when no qual_name is stored).
        """
        sql = "SELECT s.* FROM symbol s"
        where, args = [], []
        if component_id is not None or dir_path is not None:
            scope, scope_args = ["d.component_id = ?"], [component_id]
            if component_id is None:        # dir filter across all components
                scope, scope_args = [], []
            if dir_path is not None:
                scope.append(self._dir_scope_sql(dir_path, scope_args))
            where.append(
                "EXISTS (SELECT 1 FROM file f "
                "JOIN directory d ON d.id = f.directory_id "
                "WHERE f.id IN (s.file_id, s.decl_file_id) AND "
                + " AND ".join(scope) + ")")
            args.extend(scope_args)
        if file_id is not None:
            where.append("(s.file_id = ? OR s.decl_file_id = ?)")
            args.extend([file_id, file_id])
        if name:
            where.append(
                r"COALESCE(s.qual_name, s.spelling) LIKE ? ESCAPE '\'")
            args.append(self._fuzzy_like(name))
        if kind is not None:
            where.append("s.kind = ?")
            args.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (" ORDER BY LENGTH(COALESCE(s.qual_name, s.spelling)),"
                " COALESCE(s.qual_name, s.spelling)")
        return [_row_to(Symbol, r) for r in self._conn.execute(sql, args)]

    def symbols_in_file(self, file_id: int) -> list[Symbol]:
        return [_row_to(Symbol, r) for r in self._conn.execute(
            "SELECT * FROM symbol WHERE file_id = ? ORDER BY line, col", (file_id,)
        )]

    def unresolved_symbols(self) -> list[Symbol]:
        return [_row_to(Symbol, r) for r in self._conn.execute(
            "SELECT * FROM symbol WHERE resolved = 0 ORDER BY usr"
        )]

    # -- stats -----------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        one = lambda sql: self._conn.execute(sql).fetchone()[0]  # noqa: E731
        by_kind = {r["kind"]: r["n"] for r in self._conn.execute(
            "SELECT kind, COUNT(*) AS n FROM symbol GROUP BY kind ORDER BY kind"
        )}
        return {
            "components": one("SELECT COUNT(*) FROM component"),
            "directories": one("SELECT COUNT(*) FROM directory"),
            "files": one("SELECT COUNT(*) FROM file"),
            "files_indexed": one("SELECT COUNT(*) FROM file WHERE indexed = 1"),
            "symbols": one("SELECT COUNT(*) FROM symbol"),
            "symbols_unresolved": one("SELECT COUNT(*) FROM symbol WHERE resolved = 0"),
            "symbols_by_kind": by_kind,
        }


class _Transaction:
    def __init__(self, db: Storage):
        self._db = db

    def __enter__(self):
        self._db._in_txn = True
        return self._db

    def __exit__(self, exc_type, exc, tb):
        self._db._in_txn = False
        if exc_type is None:
            self._db._conn.commit()
        else:
            self._db._conn.rollback()
        return False

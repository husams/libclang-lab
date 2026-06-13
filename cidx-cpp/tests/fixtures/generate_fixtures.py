#!/usr/bin/env python3
"""One-off generator for the committed migration fixture DBs (S02 test plan).

Writes v2.db / v3.db / v4.db / v5.db next to this script, each mirroring the
historical schema layout described in spec/01-technical-analysis.md §3.3:

    v2  symbol without qual_name / decl_* / is_pure; file without driver
    v3  + symbol.qual_name
    v4  + symbol.decl_file_id / decl_line / decl_col
    v5  + symbol.is_pure
    v6  + file.driver  (current — created by the C++ Storage itself)

Run once by the developer (stdlib sqlite3 only), commit the DBs alongside this
script. Being Python-written, the fixtures double as a cross-tool-open proof
for the C++ port. CI never runs this script.

    python3 generate_fixtures.py
"""

import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))

COMMON = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE component (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    path  TEXT NOT NULL UNIQUE,
    kind  TEXT NOT NULL DEFAULT 'repo'
          CHECK (kind IN ('repo', 'external'))
);

CREATE TABLE directory (
    id           INTEGER PRIMARY KEY,
    component_id INTEGER NOT NULL REFERENCES component(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    UNIQUE (component_id, path)
);
"""

FILE_V2 = """
CREATE TABLE file (
    id              INTEGER PRIMARY KEY,
    directory_id    INTEGER NOT NULL REFERENCES directory(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    mtime           REAL,
    md5             TEXT,
    compile_options TEXT,
    indexed         INTEGER NOT NULL DEFAULT 0,
    indexed_at      TEXT,
    UNIQUE (directory_id, name)
);
"""

# symbol column sets per historical version (id first, shared tail).
SYM_HEAD = "id INTEGER PRIMARY KEY, usr TEXT NOT NULL UNIQUE, spelling TEXT NOT NULL"
SYM_QUAL = "qual_name TEXT"
SYM_MID = ("display_name TEXT, kind TEXT NOT NULL, type_info TEXT, "
           "file_id INTEGER REFERENCES file(id) ON DELETE SET NULL, "
           "line INTEGER, col INTEGER")
SYM_DECL = ("decl_file_id INTEGER REFERENCES file(id) ON DELETE SET NULL, "
            "decl_line INTEGER, decl_col INTEGER")
SYM_DEF = "is_definition INTEGER NOT NULL DEFAULT 0"
SYM_PURE = "is_pure INTEGER NOT NULL DEFAULT 0"
SYM_TAIL = "linkage TEXT, access TEXT, parent_usr TEXT, resolved INTEGER NOT NULL DEFAULT 0"

# Seed rows shared by every fixture. The parent_usr chains exercise the
# v2->v3 recursive-CTE qual_name backfill (longest chain wins; an empty
# parent spelling — the anonymous namespace — is skipped).
COMPONENTS = [(1, "myrepo", "/data/myrepo", "repo")]
DIRECTORIES = [(1, 1, ""), (2, 1, "src")]
FILES = [(1, 2, "a.c", 100.0, "aaa", '["-I."]', 1, "2024-01-01 00:00:00")]

# (id, usr, spelling, qual_name, kind, file_id, line, col,
#  decl_file_id, decl_line, decl_col, is_definition, is_pure, parent_usr,
#  resolved)
SYMBOLS = [
    (1, "c:@N@rk", "rk", "rk", "namespace",
     None, None, None, None, None, None, 1, 0, None, 1),
    (2, "c:@N@rk@S@Conf", "Conf", "rk::Conf", "class",
     None, None, None, None, None, None, 1, 0, "c:@N@rk", 1),
    (3, "c:@N@rk@S@Conf@F@set", "set", "rk::Conf::set", "method",
     1, 3, 5, 1, 3, 5, 0, 1, "c:@N@rk@S@Conf", 0),
    (4, "c:@aN", "", "", "namespace",
     None, None, None, None, None, None, 1, 0, None, 1),
    (5, "c:@aN@F@hidden", "hidden", "hidden", "function",
     1, 7, 1, 1, 7, 1, 0, 0, "c:@aN", 0),
    (6, "c:@F@main", "main", "main", "function",
     1, 10, 1, None, None, None, 1, 0, None, 1),
]


def build(version: int) -> None:
    path = os.path.join(HERE, f"v{version}.db")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(COMMON)
    conn.executescript(FILE_V2)

    parts = [SYM_HEAD]
    if version >= 3:
        parts.append(SYM_QUAL)
    parts.append(SYM_MID)
    if version >= 4:
        parts.append(SYM_DECL)
    parts.append(SYM_DEF)
    if version >= 5:
        parts.append(SYM_PURE)
    parts.append(SYM_TAIL)
    conn.execute(f"CREATE TABLE symbol ({', '.join(parts)})")

    conn.executemany("INSERT INTO component VALUES (?, ?, ?, ?)", COMPONENTS)
    conn.executemany("INSERT INTO directory VALUES (?, ?, ?)", DIRECTORIES)
    conn.executemany("INSERT INTO file VALUES (?, ?, ?, ?, ?, ?, ?, ?)", FILES)

    for (sid, usr, spelling, qual, kind, fid, line, col,
         dfid, dline, dcol, is_def, is_pure, parent, resolved) in SYMBOLS:
        cols = ["id", "usr", "spelling"]
        vals = [sid, usr, spelling]
        if version >= 3:
            cols.append("qual_name")
            vals.append(qual)
        cols += ["display_name", "kind", "type_info", "file_id", "line", "col"]
        vals += [None, kind, None, fid, line, col]
        if version >= 4:
            cols += ["decl_file_id", "decl_line", "decl_col"]
            vals += [dfid if is_def == 0 else None,
                     dline if is_def == 0 else None,
                     dcol if is_def == 0 else None]
        cols.append("is_definition")
        vals.append(is_def)
        if version >= 5:
            cols.append("is_pure")
            vals.append(is_pure)
        cols += ["linkage", "access", "parent_usr", "resolved"]
        vals += [None, None, parent, resolved]
        conn.execute(
            f"INSERT INTO symbol ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(vals))})", vals)

    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)",
                 (str(version),))
    conn.commit()
    conn.close()
    print(f"wrote {path}")


if __name__ == "__main__":
    for v in (2, 3, 4, 5):
        build(v)

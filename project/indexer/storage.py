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
from collections.abc import Sequence
from typing import Any, Optional

from indexer import pathx as _pathx

SCHEMA_VERSION = 22

#: symbol.kind name -> the integer it is stored as on disk (v16+). The integer
#: IS libclang's `CXCursorKind` enum value, so a stored kind matches the C API
#: 1:1 (e.g. CXCursor_CXXMethod == 21). Storing the small int instead of the
#: name keeps the symbol table compact; the `symbol_kind` table (and the inverse
#: map below) recover the string for display. Mirrors clang/ast.py:_KIND_MAP.
SYMBOL_KIND_IDS = {
    "struct": 2,             # CXCursor_StructDecl
    "union": 3,              # CXCursor_UnionDecl
    "class": 4,              # CXCursor_ClassDecl
    "enum": 5,               # CXCursor_EnumDecl
    "member": 6,             # CXCursor_FieldDecl
    "enum-constant": 7,      # CXCursor_EnumConstantDecl
    "function": 8,           # CXCursor_FunctionDecl
    "variable": 9,           # CXCursor_VarDecl
    "typedef": 20,           # CXCursor_TypedefDecl
    "method": 21,            # CXCursor_CXXMethod
    "namespace": 22,         # CXCursor_Namespace
    "constructor": 24,       # CXCursor_Constructor
    "destructor": 25,        # CXCursor_Destructor
    "function-template": 30,  # CXCursor_FunctionTemplate
    "class-template": 31,    # CXCursor_ClassTemplate
    "type-alias": 36,        # CXCursor_TypeAliasDecl
    "macro": 501,            # CXCursor_MacroDefinition
}
#: stored integer -> symbol.kind name (display / read-side recovery).
SYMBOL_KIND_NAMES = {v: k for k, v in SYMBOL_KIND_IDS.items()}

#: Allowed values for symbol.kind. Superset of the cidx brief: the core C/C++
#: declaration kinds plus the ones any real walk over a TU produces.
SYMBOL_KINDS = frozenset(SYMBOL_KIND_IDS)

_SCHEMA = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS component (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    path    TEXT NOT NULL UNIQUE,         -- base path (no version segment)
    kind    TEXT NOT NULL DEFAULT 'repo'
            CHECK (kind IN ('repo', 'external')),
    version TEXT                          -- v14: nullable; NULL = unversioned
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
    args_overridden INTEGER NOT NULL DEFAULT 0,  -- compile_options/driver were
                                        -- edited by `cidx file`; a re-import
                                        -- (without --force) must NOT clobber them
    UNIQUE (directory_id, name)
);

CREATE TABLE IF NOT EXISTS symbol (
    id           INTEGER PRIMARY KEY,
    usr          TEXT NOT NULL UNIQUE,  -- clang Unified Symbol Resolution
    spelling     TEXT NOT NULL,
    qual_name    TEXT,                  -- fully qualified, e.g. 'RdKafka::ConfImpl::set'
    display_name TEXT,                  -- spelling + signature, e.g. 'multiply(int, int)'
    kind         INTEGER NOT NULL,     -- CXCursorKind value; see symbol_kind table
                                       -- (v16: was TEXT name; now stored as int)
    type_info    TEXT,                  -- cursor.type.spelling
    file_id      INTEGER REFERENCES file(id) ON DELETE SET NULL,
    line         INTEGER,                     -- definition site once seen,
    col          INTEGER,                     -- else the declaration site
    decl_file_id INTEGER REFERENCES file(id) ON DELETE SET NULL,
    decl_line    INTEGER,                     -- declaration site (e.g. the .h
    decl_col     INTEGER,                     -- prototype); NULL if none seen
    decl_path    TEXT,                         -- raw decl path for a target in an
                                              -- UNREGISTERED file (system/stdlib
                                              -- header no component owns): the AST
                                              -- has the location but there is no
                                              -- file row to point decl_file_id at,
                                              -- so the stub keeps the path here
    is_definition INTEGER NOT NULL DEFAULT 0,
    is_pure      INTEGER NOT NULL DEFAULT 0,  -- C++: pure virtual ('= 0'), so
                                              -- no definition can ever exist
    is_static    INTEGER NOT NULL DEFAULT 0,  -- v12: C++ static member function
                                              -- (clang_CXXMethod_isStatic). Free
                                              -- functions/non-methods are 0; a
                                              -- file-scope `static` free function
                                              -- is captured by linkage='internal'
    is_instantiation INTEGER NOT NULL DEFAULT 0,  -- v13: implicit template
                                              -- instantiation node (own USR,
                                              -- definition via `instantiates` edge)
    is_named_instance INTEGER NOT NULL DEFAULT 0, -- v20: a template instance
                                              -- minted from a NAMED `using`/
                                              -- typedef alias (X<B> from
                                              -- `using Y = X<B>`). Such instances
                                              -- carry their OWN composes/aggregates
                                              -- /associates (T->B substituted into
                                              -- the primary's members) instead of
                                              -- collapsing onto the primary. Plain
                                              -- call-site instantiation nodes
                                              -- (is_instantiation=1, this=0) stay
                                              -- collapsed (std::vector<Foo> etc.)
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

-- ---- v16: symbol-kind metadata (display only) ----------------------------
-- Maps the integer stored in symbol.kind (== libclang CXCursorKind) to its
-- string name. Purely for display/debugging -- no FK from symbol references it;
-- readers use the in-code SYMBOL_KIND_NAMES map.
CREATE TABLE IF NOT EXISTS symbol_kind (
    id   INTEGER PRIMARY KEY,    -- CXCursorKind value
    name TEXT NOT NULL UNIQUE
);
INSERT OR IGNORE INTO symbol_kind (id, name) VALUES
  {", ".join(f"({i},{k!r})" for k, i in sorted(SYMBOL_KIND_IDS.items(), key=lambda kv: kv[1]))};

-- ---- v7 graph layer (PLAN §2/§6) -----------------------------------------

-- edge.kind metadata (display only, like symbol_kind): no symbol/edge FK
-- references it -- readers use the in-code EDGE_NAMES map.
CREATE TABLE IF NOT EXISTS edge_kind (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
INSERT OR IGNORE INTO edge_kind (id, name) VALUES
  (1,'calls'), (2,'inherits'), (3,'contains'), (4,'specializes'),
  (5,'instantiates'), (6,'overrides'), (7,'uses'),
  (8,'field_of'), (9,'method_of'),
  (10,'construct-value'), (11,'construct-temp'), (12,'construct-heap'),
  (13,'construct-copy'), (14,'construct-move'),
  (15,'factory-construct'), (16,'destroy'), (17,'friend');

CREATE TABLE IF NOT EXISTS edge (
    id          INTEGER PRIMARY KEY,
    src_id      INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    dst_id      INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    kind        INTEGER NOT NULL,   -- edge_kind.id (no FK: faster inserts)
    count       INTEGER NOT NULL DEFAULT 1,
    base_access INTEGER,   -- inherits: public/protected/private of the base
    is_virtual  INTEGER,   -- inherits: virtual base
    vtable_slot INTEGER,   -- overrides: reserved (NULL today)
    UNIQUE (src_id, dst_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src_id, kind);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst_id, kind);

CREATE TABLE IF NOT EXISTS edge_site (
    edge_id      INTEGER NOT NULL REFERENCES edge(id) ON DELETE CASCADE,
    file_id      INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    line         INTEGER,
    col          INTEGER,
    conditional  INTEGER NOT NULL DEFAULT 0,
    args_sig     TEXT,
    recv_src_kind TEXT,
    recv_type_usr TEXT,
    recv_decl_usr TEXT,
    recv_param_pos INTEGER,
    recv_type_is_value INTEGER,          -- v11: receiver held by value (1) else 0/NULL
    PRIMARY KEY (edge_id, file_id, line, col)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS template_param (
    owner_id    INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    param_kind  INTEGER NOT NULL,  -- 1=type 2=non-type 3=template-template 4=pack
    name        TEXT,
    default_txt TEXT,
    PRIMARY KEY (owner_id, position)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS template_arg (
    owner_id  INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    arg_kind  INTEGER NOT NULL,  -- 1=type 2=non-type value 3=template-template 4=pack
    ref_id    INTEGER REFERENCES symbol(id) ON DELETE SET NULL,
    literal   TEXT,
    PRIMARY KEY (owner_id, position)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS call_arg (
    edge_id    INTEGER NOT NULL REFERENCES edge(id) ON DELETE CASCADE,
    file_id    INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    line       INTEGER NOT NULL,
    col        INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    src_kind   TEXT NOT NULL,
    type_usr   TEXT,
    decl_usr   TEXT,
    callee_usr TEXT,
    type_is_value INTEGER,               -- v11: arg held by value (1) else 0/NULL
    PRIMARY KEY (edge_id, file_id, line, col, position)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_call_arg_edge ON call_arg(edge_id);

CREATE TABLE IF NOT EXISTS label (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,   -- label key, e.g. 'libfoo-include'
    path TEXT NOT NULL           -- stored verbatim; may contain $VAR
);

-- ---- v15: per-file parse diagnostics (errors/warnings) ---------------------
-- Clang diagnostics (severity >= warning) emitted while parsing a TU, keyed by
-- the TU's file row. Refreshed wholesale on every (re)index of that file. A
-- diagnostic located in an #included header keeps its own file_path/line/col,
-- but is owned by the TU that surfaced it.
CREATE TABLE IF NOT EXISTS diagnostic (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    severity  INTEGER NOT NULL,   -- clang severity: 2=warning 3=error 4=fatal
    spelling  TEXT NOT NULL,      -- the diagnostic message
    file_path TEXT,               -- diagnostic location file (NULL if locationless)
    line      INTEGER,            -- NULL when locationless
    col       INTEGER             -- NULL when locationless
);
CREATE INDEX IF NOT EXISTS idx_diagnostic_file ON diagnostic(file_id);

-- ---- v17: Layer-1 entity-edge graph (UML/ER relations over record/enum symbols) --
-- Entity = a symbol whose kind is in {{class,struct,union,enum}}; no separate table.
-- All columns are INTEGER (zero text in the table itself). The 11 relation names
-- live only in entity_edge_kind (seed-only; no FK from entity_edge -- same pattern
-- as edge_kind). A NULL-safe unique identity index (see below) keeps one row
-- per logical edge so re-materialise = DELETE + re-run stays idempotent.
-- (Lexical nesting is a declaration-scope property of the symbol, not a relation,
--  so it is NOT an entity_edge kind.)

CREATE TABLE IF NOT EXISTS entity_edge_kind (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
INSERT OR IGNORE INTO entity_edge_kind (id, name) VALUES
  (1,'generalizes'), (2,'implements'), (3,'specializes'),
  (4,'composes'), (5,'aggregates'), (6,'associates'),
  (7,'creates'), (8,'uses'), (9,'destroys'),
  (10,'befriends'), (11,'instantiates');

CREATE TABLE IF NOT EXISTS entity_edge (
    src_id        INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    dst_id        INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    kind          INTEGER NOT NULL,   -- entity_edge_kind.id (no FK: seed-only)
    count         INTEGER NOT NULL DEFAULT 1,
    via_member_id INTEGER REFERENCES symbol(id) ON DELETE SET NULL,
    multiplicity  INTEGER NOT NULL DEFAULT 1,
                                      -- 1=one 2=0..1 3=0..* 4=N
    access        INTEGER NOT NULL DEFAULT 0,
                                      -- 0=public 1=protected 2=private
    is_virtual    INTEGER NOT NULL DEFAULT 0,  -- 1 = virtual base (generalizes)
    create_form   INTEGER,            -- creates/destroys only:
                                      -- 1=ctor_call 2=return 3=value 4=temp
                                      -- 5=heap 6=factory 7=copy 8=move
    partial       INTEGER NOT NULL DEFAULT 0   -- 1 = top-soundness flag
);
-- One row per logical entity edge.  A plain UNIQUE(...via_member_id) cannot
-- enforce this: SQLite treats NULL != NULL, so the very common NULL-via edges
-- (generalizes/specializes/uses/creates/...) would never collide and the
-- INSERT ... ON CONFLICT upserts in entity_rollup would silently fan out into
-- duplicate rows on every materialise.  A COALESCE expression index folds NULL
-- to a sentinel so the identity is NULL-safe; create_form is part of the key so
-- distinct creates/destroys forms (value/temp/heap/...) stay separate rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_edge_identity ON entity_edge(
    src_id, dst_id, kind,
    COALESCE(via_member_id, -1), COALESCE(create_form, -1)
);
CREATE INDEX IF NOT EXISTS idx_entity_edge_src  ON entity_edge(src_id, kind);
CREATE INDEX IF NOT EXISTS idx_entity_edge_dst  ON entity_edge(dst_id, kind);

-- ---- v22: entity-node type (Layer-1 design-entity classification) -----------
-- The *type of an entity node* in the UML/abstraction graph, materialised at
-- `cidx resolve` alongside entity_edge. Orthogonal to the C++ keyword (which
-- stays at the low-level symbol layer): a record is classified by ABSTRACTNESS
-- into class / abstract_class / interface (and the same split for class
-- templates); union / enum keep their own type. Same zero-TEXT/lookup-table
-- pattern as entity_edge_kind. Populated by entity_rollup.materialize_entity_nodes.
CREATE TABLE IF NOT EXISTS entity_kind (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
INSERT OR IGNORE INTO entity_kind (id, name) VALUES
  (0,'other'),
  (1,'class'), (2,'abstract_class'), (3,'interface'),
  (4,'union'), (5,'enum'),
  (6,'class_template'), (7,'abstract_class_template'), (8,'interface_template');

CREATE TABLE IF NOT EXISTS entity_node (
    id   INTEGER PRIMARY KEY REFERENCES symbol(id) ON DELETE CASCADE,
    kind INTEGER NOT NULL   -- entity_kind.id (no FK: seed-only)
);

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');
"""


@dataclass
class Component:
    name: str
    path: str
    kind: str = "repo"
    id: Optional[int] = None
    version: Optional[str] = None  # v14: nullable; NULL = unversioned


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
    args_overridden: bool = False
    id: Optional[int] = None


@dataclass
class Diagnostic:
    file_id: int
    severity: int  # clang: 2=warning, 3=error, 4=fatal
    spelling: str
    file_path: Optional[str] = None
    line: Optional[int] = None
    col: Optional[int] = None
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
    decl_path: Optional[str] = None  # raw decl path for an unregistered
    # (system/stdlib) target -- see schema
    is_definition: bool = False
    is_pure: bool = False
    is_static: bool = False
    is_instantiation: bool = False  # v13: implicit template-instantiation node
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
        # kind is stored as a CXCursorKind int (v16); present it as the name.
        kwargs["kind"] = SYMBOL_KIND_NAMES.get(kwargs["kind"], kwargs["kind"])
        kwargs["is_definition"] = bool(kwargs["is_definition"])
        kwargs["is_pure"] = bool(kwargs["is_pure"])
        kwargs["is_static"] = bool(kwargs["is_static"])
        kwargs["is_instantiation"] = bool(kwargs["is_instantiation"])
        kwargs["resolved"] = bool(kwargs["resolved"])
    if cls is File:
        kwargs["indexed"] = bool(kwargs["indexed"])
        kwargs["args_overridden"] = bool(kwargs["args_overridden"])
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
        self._needs_entity_node_backfill = False
        self._migrate()  # before _SCHEMA: its indexes need new columns
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._in_txn = False
        # v21 -> v22 one-time backfill: entity_node is a pure-DB classification
        # of existing symbols (no re-parse), so an upgraded index gets its design
        # types filled in immediately on open -- no `cidx index`/`resolve` needed.
        # (entity_node did not exist during _migrate; it does now, after _SCHEMA.)
        if self._needs_entity_node_backfill:
            from indexer.entity_rollup import _materialise_entity_nodes

            with self.transaction():
                _materialise_entity_nodes(self)

    def _migrate(self) -> None:
        """In-place upgrade of a database created by an older schema version.

        v2 -> v3: adds symbol.qual_name and backfills it by walking the stored
        parent_usr chains (the longest chain per symbol is the full path).
        v3 -> v4: adds symbol.decl_file_id/decl_line/decl_col. For rows that
        are still declaration-only the stored location IS the declaration, so
        it is copied over; definition rows get their decl site on reindex.
        v5 -> v6: adds file.driver (compile-command argv[0]); backfilled on
        the next `import`.
        v7 -> v8: adds file.args_overridden (0/1); marks files whose compile
        flags were hand-edited via `cidx file` so re-import does not clobber
        them. Defaults to 0; no backfill needed.
        """
        tables = {
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "symbol" not in tables:
            return  # fresh database: _SCHEMA creates everything
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
                "REFERENCES file(id) ON DELETE SET NULL"
            )
            self._conn.execute("ALTER TABLE symbol ADD COLUMN decl_line INTEGER")
            self._conn.execute("ALTER TABLE symbol ADD COLUMN decl_col INTEGER")
            self._conn.execute(
                "UPDATE symbol SET decl_file_id = file_id, decl_line = line, "
                "decl_col = col WHERE is_definition = 0"
            )
            changed = True
        if "is_pure" not in cols:
            # No backfill possible from stored data -- reindex to populate.
            self._conn.execute(
                "ALTER TABLE symbol ADD COLUMN is_pure INTEGER NOT NULL DEFAULT 0"
            )
            changed = True
        if "is_static" not in cols:
            # v11 -> v12: C++ static member function flag. No backfill possible
            # from stored data -- reindex to populate; old rows read as 0.
            self._conn.execute(
                "ALTER TABLE symbol ADD COLUMN is_static INTEGER NOT NULL DEFAULT 0"
            )
            changed = True
        if "is_instantiation" not in cols:
            # v12 -> v13: implicit template-instantiation node marker. No backfill
            # possible from stored data -- reindex to populate; old rows read as 0.
            self._conn.execute(
                "ALTER TABLE symbol ADD COLUMN is_instantiation INTEGER NOT NULL DEFAULT 0"
            )
            changed = True
        if "decl_path" not in cols:
            # v8 -> v9: raw decl path for stubs whose target lives in an
            # unregistered (system/stdlib) file. No backfill -- those rows had no
            # location to recover; a reindex repopulates it from the AST.
            self._conn.execute("ALTER TABLE symbol ADD COLUMN decl_path TEXT")
            changed = True
        # v15 -> v16: symbol.kind moves from a TEXT name to its CXCursorKind
        # integer (compact storage; symbol_kind table recovers the string). The
        # column type changes and the old CHECK constraint must go, so the table
        # is rebuilt in place with the kind values converted. Runs after the
        # column-add migrations above so the new table mirrors all columns.
        kind_type = next(
            (r[2] for r in self._conn.execute("PRAGMA table_info(symbol)")
             if r[1] == "kind"),
            "",
        )
        if (kind_type or "").upper() != "INTEGER":
            self._migrate_symbol_kind_to_int()
            changed = True
        # v19 -> v20: named-instance marker. A template instance minted from a
        # NAMED `using`/typedef alias (X<B>) carries its own composes/aggregates
        # /associates instead of collapsing onto the primary. No backfill -- a
        # reindex repopulates it; old rows read as 0. Re-read the column set here
        # because the v15->v16 rebuild above recreates `symbol` (without this
        # column), so the snapshot taken at the top of _migrate may be stale.
        cols2 = {r[1] for r in self._conn.execute("PRAGMA table_info(symbol)")}
        if "is_named_instance" not in cols2:
            self._conn.execute(
                "ALTER TABLE symbol ADD COLUMN is_named_instance INTEGER NOT NULL DEFAULT 0"
            )
            changed = True
        fcols = {r[1] for r in self._conn.execute("PRAGMA table_info(file)")}
        if "file" in tables and "driver" not in fcols:
            # No backfill possible from stored data -- re-import to populate.
            self._conn.execute("ALTER TABLE file ADD COLUMN driver TEXT")
            changed = True
        if "file" in tables and "args_overridden" not in fcols:
            # v7 -> v8: per-file flag override marker (`cidx file`). Existing
            # rows default to 0 (not overridden), so re-import behaves as before.
            self._conn.execute(
                "ALTER TABLE file ADD COLUMN args_overridden INTEGER NOT NULL DEFAULT 0"
            )
            changed = True
        # v9 -> v10: receiver provenance + per-argument provenance for virtual
        # dispatch.  No backfill -- reindex repopulates from the AST.
        escols = (
            {r[1] for r in self._conn.execute("PRAGMA table_info(edge_site)")}
            if "edge_site" in tables
            else set()
        )
        if "edge_site" in tables and "recv_src_kind" not in escols:
            self._conn.execute("ALTER TABLE edge_site ADD COLUMN recv_src_kind TEXT")
            self._conn.execute("ALTER TABLE edge_site ADD COLUMN recv_type_usr TEXT")
            self._conn.execute("ALTER TABLE edge_site ADD COLUMN recv_decl_usr TEXT")
            changed = True
        if "edge_site" in tables and "recv_param_pos" not in escols:
            self._conn.execute(
                "ALTER TABLE edge_site ADD COLUMN recv_param_pos INTEGER"
            )
            changed = True
        if "edge_site" in tables and "call_arg" not in tables:
            # The call_arg table itself is created by _SCHEMA (CREATE TABLE IF
            # NOT EXISTS), run after _migrate(), so the migration only needs to
            # flip changed to bump the version -- identical to the v6->v7 graph
            # tables pattern.
            changed = True
        # v10 -> v11: value-ness booleans for exact-singleton Gamma narrowing.
        # No backfill -- reindex repopulates; old rows read as NULL == not-value == TOP.
        if "edge_site" in tables and "recv_type_is_value" not in escols:
            self._conn.execute(
                "ALTER TABLE edge_site ADD COLUMN recv_type_is_value INTEGER"
            )
            changed = True
        cacols = (
            {r[1] for r in self._conn.execute("PRAGMA table_info(call_arg)")}
            if "call_arg" in tables
            else set()
        )
        if "call_arg" in tables and "type_is_value" not in cacols:
            self._conn.execute("ALTER TABLE call_arg ADD COLUMN type_is_value INTEGER")
            changed = True
        # v13 -> v14: component.version column + label table.
        compcols = {r[1] for r in self._conn.execute("PRAGMA table_info(component)")}
        if "component" in tables and "version" not in compcols:
            self._conn.execute("ALTER TABLE component ADD COLUMN version TEXT")
            # No backfill -- existing components get version = NULL.
            changed = True
        if "label" not in tables:
            # The schema script (run AFTER migrate) creates the table via
            # CREATE TABLE IF NOT EXISTS; the migration only needs to flip
            # changed so the schema_version meta is bumped.
            changed = True
        if "diagnostic" not in tables:
            # v14 -> v15: per-file parse diagnostics. Created by the schema
            # script (CREATE TABLE IF NOT EXISTS); no backfill possible -- a
            # reindex repopulates it from each TU's diagnostics.
            changed = True
        if "entity_edge" not in tables:
            # v16 -> v17: Layer-1 entity_edge + entity_edge_kind tables.
            # Created by the schema script (CREATE TABLE IF NOT EXISTS).
            # entity_edge is a derived, materialized table -- populate via
            # `cidx resolve`. No backfill on migration.
            changed = True
        else:
            # The `nests` entity_edge kind was removed (lexical nesting is a
            # symbol declaration-scope property, not a relation). Clean the DB in
            # place: drop the defunct nests rows (kind 10) and renumber befriends
            # 11 -> 10 to match the new contiguous seed. Order matters -- delete
            # the old kind-10 rows BEFORE renumbering 11 -> 10 so the two never
            # collide on UNIQUE(src,dst,kind,via). Also drop the stale
            # entity_edge_kind rows so _SCHEMA's INSERT OR IGNORE reseeds
            # (10,'befriends').
            #
            # Gate on the STALE DATA (a leftover 'nests' seed row), NOT the schema
            # version: an earlier build bumped schema_version to 18 WITHOUT
            # cleaning, so a version gate would skip those already-stamped DBs.
            # Idempotent -- after cleanup there is no 'nests' row, so it never
            # runs again.
            stale = self._conn.execute(
                "SELECT 1 FROM entity_edge_kind WHERE name = 'nests' LIMIT 1"
            ).fetchone()
            if stale is not None:
                self._conn.execute("DELETE FROM entity_edge WHERE kind = 10")
                self._conn.execute("UPDATE entity_edge SET kind = 10 WHERE kind = 11")
                self._conn.execute("DELETE FROM entity_edge_kind WHERE id IN (10, 11)")
                changed = True
            # Rename kind 2 'realizes' -> 'implements' (display name only; the
            # stored entity_edge.kind int is unchanged). Data-gated on the old
            # name so it fires regardless of schema_version; _SCHEMA's INSERT OR
            # IGNORE would otherwise leave the stale (2,'realizes') row in place.
            renamed = self._conn.execute(
                "SELECT 1 FROM entity_edge_kind WHERE id = 2 AND name = 'realizes'"
            ).fetchone()
            if renamed is not None:
                self._conn.execute(
                    "UPDATE entity_edge_kind SET name = 'implements' WHERE id = 2"
                )
                changed = True
            # v20 -> v21: NULL-safe entity_edge identity. The old table-level
            # UNIQUE(src,dst,kind,via_member_id) never collided on NULL-via rows
            # (SQLite NULL != NULL), so every materialise fanned NULL-via edges
            # out into duplicate copies. _SCHEMA now builds a COALESCE unique
            # index idx_entity_edge_identity; it would fail to create over a DB
            # that already carries those duplicates, so dedup in place first
            # (keep the lowest rowid per logical key). Gate on the index's
            # absence so it runs exactly once; entity_edge is derived, so `cidx
            # resolve` repopulates cleanly regardless.
            has_identity_idx = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' "
                "AND name = 'idx_entity_edge_identity'"
            ).fetchone()
            if has_identity_idx is None:
                self._conn.execute(
                    "DELETE FROM entity_edge WHERE rowid NOT IN ("
                    "  SELECT MIN(rowid) FROM entity_edge GROUP BY "
                    "    src_id, dst_id, kind, "
                    "    COALESCE(via_member_id, -1), COALESCE(create_form, -1))"
                )
                changed = True
        if "entity_node" not in tables:
            # v21 -> v22: entity_node + entity_kind tables (the entity's design
            # type). The table is created by the schema script (run after this);
            # because the type is a pure-DB classification of existing symbols,
            # __init__ backfills it right after _SCHEMA -- no re-index/resolve.
            self._needs_entity_node_backfill = True
            changed = True
        if "edge" not in tables:
            # v6 -> v7: graph layer. The schema script (run AFTER migrate) creates
            # the tables + indexes + seeds edge_kind; nothing to backfill from
            # stored data (edges are derived — re-run `cidx index`/`resolve`).
            changed = True
        elif not changed:
            # edge table exists: bump version only when stored version is OLDER
            # (future-schema DBs — version > SCHEMA_VERSION — are left untouched).
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None:
                v = row[0]
                if v and int(v) < SCHEMA_VERSION:
                    changed = True
        if changed:
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def _migrate_symbol_kind_to_int(self) -> None:
        """v15 -> v16: rebuild `symbol` with kind stored as its CXCursorKind int.

        SQLite cannot ALTER a column's type or drop the old `kind IN (...)` CHECK,
        so the table is recreated and the rows copied with the kind names mapped
        to integers. Foreign keys are disabled for the swap so dropping the old
        table does not cascade-delete edges (edge.src_id/dst_id keep the same ids
        the new rows carry). The schema script (run right after) recreates the
        symbol indexes via CREATE INDEX IF NOT EXISTS.
        """
        case = "CASE kind " + " ".join(
            f"WHEN {name!r} THEN {i}" for name, i in SYMBOL_KIND_IDS.items()
        ) + " ELSE kind END"
        self._conn.commit()  # close any open txn so the pragma below takes effect
        self._conn.execute("PRAGMA foreign_keys = OFF")
        self._conn.executescript(f"""
            CREATE TABLE symbol_new (
                id           INTEGER PRIMARY KEY,
                usr          TEXT NOT NULL UNIQUE,
                spelling     TEXT NOT NULL,
                qual_name    TEXT,
                display_name TEXT,
                kind         INTEGER NOT NULL,
                type_info    TEXT,
                file_id      INTEGER REFERENCES file(id) ON DELETE SET NULL,
                line         INTEGER,
                col          INTEGER,
                decl_file_id INTEGER REFERENCES file(id) ON DELETE SET NULL,
                decl_line    INTEGER,
                decl_col     INTEGER,
                decl_path    TEXT,
                is_definition INTEGER NOT NULL DEFAULT 0,
                is_pure      INTEGER NOT NULL DEFAULT 0,
                is_static    INTEGER NOT NULL DEFAULT 0,
                is_instantiation INTEGER NOT NULL DEFAULT 0,
                linkage      TEXT,
                access       TEXT,
                parent_usr   TEXT,
                resolved     INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO symbol_new
                SELECT id, usr, spelling, qual_name, display_name, {case},
                       type_info, file_id, line, col, decl_file_id, decl_line,
                       decl_col, decl_path, is_definition, is_pure, is_static,
                       is_instantiation, linkage, access, parent_usr, resolved
                FROM symbol;
            DROP TABLE symbol;
            ALTER TABLE symbol_new RENAME TO symbol;
        """)
        self._conn.commit()
        self._conn.execute("PRAGMA foreign_keys = ON")

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

    def add_component(
        self, name: str, path: str, kind: str = "repo", version: Optional[str] = None
    ) -> int:
        """Insert a component; idempotent on path. Returns the component id.

        On conflict (same path), updates name and kind; updates version only
        when the caller supplies a non-None value (COALESCE: a re-import that
        passes no version does NOT wipe an existing stored version).
        """
        # Preserve indirected (portable) paths verbatim; absolutize plain paths.
        if "$" not in path and "<" not in path:
            path = os.path.abspath(path)
        cur = self._conn.execute(
            "INSERT INTO component (name, path, kind, version) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  name    = excluded.name, "
            "  kind    = excluded.kind, "
            "  version = COALESCE(excluded.version, component.version) "
            "RETURNING id",
            (name, path, kind, version),
        )
        cid = cur.fetchone()["id"]
        self._commit()
        return cid

    def set_component_version(self, name: str, version: Optional[str]) -> bool:
        """Set (or clear when version=None) a component's version by name.

        Returns False when no component with that name exists.
        """
        cur = self._conn.execute(
            "UPDATE component SET version = ? WHERE name = ?", (version, name)
        )
        self._commit()
        return cur.rowcount > 0

    def set_component_effective_version(self, name: str, version: str) -> bool:
        """Set a component's EFFECTIVE version regardless of how the existing
        version is represented, non-destructively.

        Two representations exist (see component_alias_index):
          - version-as-property: the `version` column carries it, `path` has no
            trailing version segment  -> just UPDATE the column.
          - version-in-path: the version is the trailing segment of `path`
            (no `version` column)     -> rewrite that trailing segment in place
            and leave `version` NULL.

        Only applied when the name resolves to exactly ONE component row;
        multi-row names (duplicate/ambiguous) are left untouched and False is
        returned. The stored path is split (not the resolved one) so portable
        `<label>` / `$VAR` prefixes survive the rewrite.
        """
        rows = [c for c in self.list_components() if c.name == name and c.id is not None]
        if len(rows) != 1:
            return False
        comp = rows[0]
        base, seg = _pathx.split_base_version(comp.path)
        if seg is not None:
            # version embedded in the path: swap the trailing segment.
            new_path = os.path.normpath(os.path.join(base, version))
            if "$" not in new_path and "<" not in new_path:
                new_path = os.path.abspath(new_path)
            self._conn.execute(
                "UPDATE component SET path = ?, version = NULL WHERE id = ?",
                (new_path, comp.id),
            )
        else:
            self._conn.execute(
                "UPDATE component SET version = ? WHERE id = ?", (version, comp.id)
            )
        self._commit()
        return True

    @staticmethod
    def effective_root(comp: "Component") -> str:
        """Stored effective root (NOT resolved): version joined onto path.

        Returns normpath(join(path, version)) when versioned, else path.
        """
        if comp.version:
            return os.path.normpath(os.path.join(comp.path, comp.version))
        return comp.path

    # -- labels (v14) --------------------------------------------------------

    def add_label(self, name: str, path: str) -> int:
        """Upsert a label by name. Returns the label id."""
        cur = self._conn.execute(
            "INSERT INTO label (name, path) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET path = excluded.path "
            "RETURNING id",
            (name, path),
        )
        lid = cur.fetchone()["id"]
        self._commit()
        return lid

    def remove_label(self, name: str) -> bool:
        """Delete a label by name. Returns False if it did not exist."""
        cur = self._conn.execute("DELETE FROM label WHERE name = ?", (name,))
        self._commit()
        return cur.rowcount > 0

    def get_label(self, name: str) -> Optional[str]:
        """Return the stored path for a label, or None if absent."""
        row = self._conn.execute(
            "SELECT path FROM label WHERE name = ?", (name,)
        ).fetchone()
        return row["path"] if row else None

    def list_labels(self) -> list[tuple[str, str]]:
        """All labels sorted by name; returns (name, stored_path) pairs."""
        return [
            (r["name"], r["path"])
            for r in self._conn.execute("SELECT name, path FROM label ORDER BY name")
        ]

    def component_alias_index(
        self,
    ) -> dict[str, tuple[str, Optional[str], bool]]:
        """Version-agnostic component alias map: name -> (base, max_version,
        bumpable).

        For each component the resolved effective root is split into
        (base, version) by trailing-segment detection, so matching is done on
        the VERSION-STRIPPED base. Rows are grouped by name; a name is included
        only when all its rows share ONE base (conflicting bases = ambiguous,
        skipped). `max_version` is the numeric-max trailing version across the
        rows (None if none). `bumpable` is True only when the name has exactly
        one row whose stored `path` carries no embedded version segment (pure
        version-as-property) — so `set_component_version` is safe and
        non-destructive; otherwise version-bump-on-import is skipped for it.
        """
        by_name: dict[str, list[tuple[str, Optional[str], bool]]] = {}
        for c in self.list_components():
            eff = os.path.abspath(_pathx.resolve_fs_path(Storage.effective_root(c)))
            base, ver = _pathx.split_base_version(eff)
            _, path_ver = _pathx.split_base_version(
                os.path.abspath(_pathx.resolve_fs_path(c.path))
            )
            by_name.setdefault(c.name, []).append((base, ver, path_ver is None))
        out: dict[str, tuple[str, Optional[str], bool]] = {}
        for name, rows in by_name.items():
            bases = {b for b, _v, _p in rows}
            if len(bases) != 1:
                continue  # ambiguous: same name, different base dirs
            base = next(iter(bases))
            vers = [v for _b, v, _p in rows if v]
            maxver = max(vers, key=_pathx.version_key) if vers else None
            bumpable = len(rows) == 1 and rows[0][2]
            out[name] = (base, maxver, bumpable)
        return out

    def list_alias_pairs(self) -> list[tuple[str, str, bool]]:
        """Encode registry for include-path aliasing as (name, match_path,
        versioned) triples:

          - explicit labels -> (name, stored_path, False): exact match, no
            version handling.
          - components -> (name, version-stripped base, True): version-agnostic
            match; the version segment after the base is dropped at encode and
            re-injected at decode (`get_alias` -> base + highest version).

        Labels win on a name collision; component names with conflicting bases
        are skipped (ambiguous). Sorted by name; `build_label_map` re-sorts by
        resolved length for longest-match. Decode mirror = `get_alias`.
        """
        triples: list[tuple[str, str, bool]] = []
        label_names: set[str] = set()
        for name, stored in self.list_labels():
            triples.append((name, stored, False))
            label_names.add(name)
        for name, (base, _ver, _bump) in self.component_alias_index().items():
            if name not in label_names:
                triples.append((name, base, True))
        triples.sort(key=lambda t: t[0])
        return triples

    def get_alias(self, name: str) -> Optional[str]:
        """Decode an alias name: explicit label -> stored path; else a
        uniquely-based component -> its base joined with the highest known
        version (= effective root at the max version). None if neither applies
        (or an ambiguous duplicate-based component name). Mirror of the
        `list_alias_pairs` encode registry."""
        path = self.get_label(name)
        if path is not None:
            return path
        entry = self.component_alias_index().get(name)
        if entry is None:
            return None
        base, maxver, _bump = entry
        return os.path.join(base, maxver) if maxver else base

    def get_component_by_name(self, name: str) -> Optional[Component]:
        row = self._conn.execute(
            "SELECT * FROM component WHERE name = ?", (name,)
        ).fetchone()
        return _row_to(Component, row)

    def get_component(self, path: str) -> Optional[Component]:
        """Look up a component by root path.

        Two-step lookup for version-split safety (§4.4):
          1. Exact match on the stored BASE path.
          2. If that misses, match where effective_root(comp) == abspath(path).
        """
        abs_path = os.path.abspath(path)
        row = self._conn.execute(
            "SELECT * FROM component WHERE path = ?", (abs_path,)
        ).fetchone()
        if row is not None:
            return _row_to(Component, row)
        # Fallback: match effective root (handles the version-split case where the
        # user registered /src/v8 but the stored base is /src with version=v8).
        for row in self._conn.execute("SELECT * FROM component"):
            comp = _row_to(Component, row)
            eff = os.path.abspath(_pathx.resolve_fs_path(Storage.effective_root(comp)))
            if eff == abs_path:
                return comp
        return None

    def get_component_by_id(self, component_id: int) -> Optional[Component]:
        row = self._conn.execute(
            "SELECT * FROM component WHERE id = ?", (component_id,)
        ).fetchone()
        return _row_to(Component, row)

    def component_for_path(self, abs_path: str) -> Optional[Component]:
        """Longest-prefix match: which component owns this absolute path?

        Uses the effective root (base+version, resolved) for prefix matching,
        so a versioned component stored as (path=/opt/libfoo, version=1.2.3)
        correctly claims /opt/libfoo/1.2.3/include/... .
        """
        abs_path = os.path.abspath(abs_path)
        best: Optional[Component] = None
        best_root_len = -1
        for row in self._conn.execute("SELECT * FROM component"):
            comp = _row_to(Component, row)
            root = os.path.abspath(
                _pathx.resolve_fs_path(Storage.effective_root(comp))
            ).rstrip(os.sep)
            if abs_path == root or abs_path.startswith(root + os.sep):
                if len(root) > best_root_len:
                    best = comp
                    best_root_len = len(root)
        return best

    def delete_component(self, component_id: int) -> None:
        """Remove a component and everything derived from it.

        Directories and files vanish via ON DELETE CASCADE; symbols reference
        files with ON DELETE SET NULL, so symbols indexed from this component's
        files are deleted explicitly first -- otherwise they would linger as
        file-less orphans. Used by `import --force` to rebuild from scratch."""
        sub = (
            "SELECT f.id FROM file f "
            "JOIN directory d ON f.directory_id = d.id "
            "WHERE d.component_id = ?"
        )
        self._conn.execute(
            f"DELETE FROM symbol WHERE file_id IN ({sub}) OR decl_file_id IN ({sub})",
            (component_id, component_id),
        )
        self._conn.execute("DELETE FROM component WHERE id = ?", (component_id,))
        self._commit()

    def delete_directory(self, directory_id: int) -> None:
        """Remove a directory, its files (ON DELETE CASCADE), and the symbols
        indexed from those files (file_id/decl_file_id are ON DELETE SET NULL,
        so they are deleted explicitly to avoid file-less orphans)."""
        sub = "SELECT id FROM file WHERE directory_id = ?"
        self._conn.execute(
            f"DELETE FROM symbol WHERE file_id IN ({sub}) OR decl_file_id IN ({sub})",
            (directory_id, directory_id),
        )
        self._conn.execute("DELETE FROM directory WHERE id = ?", (directory_id,))
        self._commit()

    def delete_file(self, file_id: int) -> None:
        """Remove a file and the symbols indexed from it (referenced by
        file_id/decl_file_id with ON DELETE SET NULL, so deleted explicitly to
        avoid file-less orphans)."""
        self._conn.execute(
            "DELETE FROM symbol WHERE file_id = ? OR decl_file_id = ?",
            (file_id, file_id),
        )
        self._conn.execute("DELETE FROM file WHERE id = ?", (file_id,))
        self._commit()

    def delete_symbol(self, symbol_id: int) -> None:
        """Remove a single symbol row."""
        self._conn.execute("DELETE FROM symbol WHERE id = ?", (symbol_id,))
        self._commit()

    @staticmethod
    def _fuzzy_like(text: str) -> str:
        """LIKE pattern for fzf-style fuzzy matching (use with ESCAPE '\\').

        Every non-space character of `text` must appear in the column, in
        order: 'shp' matches 'shapes.c'. LIKE is case-insensitive for ASCII.
        """
        chars = [
            c.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            for c in text
            if not c.isspace()
        ]
        return "%" + "%".join(chars) + "%"

    def list_components(
        self, name: Optional[str] = None, kind: Optional[str] = None
    ) -> list[Component]:
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

    def list_directories(
        self, component_id: Optional[int] = None, name: Optional[str] = None
    ) -> list[tuple[Directory, str]]:
        """(Directory, component name) pairs, optionally scoped to one
        component and/or fuzzy-filtered on the relative directory path."""
        sql = (
            "SELECT d.*, c.name AS comp_name "
            "FROM directory d JOIN component c ON c.id = d.component_id"
        )
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
        return [
            (_row_to(Directory, r), r["comp_name"])
            for r in self._conn.execute(sql, args)
        ]

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

    def add_file(
        self,
        directory_id: int,
        name: str,
        mtime: Optional[float] = None,
        md5: Optional[str] = None,
        compile_options: Optional[list[str]] = None,
        driver: Optional[str] = None,
    ) -> int:
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
            "  compile_options = CASE WHEN file.args_overridden = 1 "
            "                         THEN file.compile_options "
            "                         ELSE COALESCE(excluded.compile_options, "
            "                                       file.compile_options) END, "
            "  driver          = CASE WHEN file.args_overridden = 1 "
            "                         THEN file.driver "
            "                         ELSE COALESCE(excluded.driver, "
            "                                       file.driver) END, "
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

    def add_file_path(
        self,
        abs_path: str,
        mtime: Optional[float] = None,
        md5: Optional[str] = None,
        compile_options: Optional[list[str]] = None,
        driver: Optional[str] = None,
    ) -> int:
        """Convenience: register an absolute path, creating the directory row.

        The owning component must already exist (add_component first).
        """
        comp_id, rel_dir, name = self._split_path(abs_path)
        dir_id = self.add_directory(comp_id, rel_dir)
        return self.add_file(
            dir_id,
            name,
            mtime=mtime,
            md5=md5,
            compile_options=compile_options,
            driver=driver,
        )

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
            "SELECT f.*, c.path AS root, c.version AS comp_version, d.path AS rel "
            "FROM file f JOIN directory d ON d.id = f.directory_id "
            "JOIN component c ON c.id = d.component_id "
            "ORDER BY c.path, d.path, f.name"
        ).fetchall()
        out = []
        for row in rows:
            comp_stub = Component(
                name="", path=row["root"], version=row["comp_version"]
            )
            eff = os.path.abspath(
                _pathx.resolve_fs_path(Storage.effective_root(comp_stub))
            )
            abs_path = (
                os.path.join(eff, row["rel"], row["name"])
                if row["rel"]
                else os.path.join(eff, row["name"])
            )
            out.append((_row_to(File, row), abs_path))
        return out

    def list_files(
        self,
        component_id: Optional[int] = None,
        dir_path: Optional[str] = None,
        name: Optional[str] = None,
        indexed: Optional[bool] = None,
    ) -> list[tuple[File, str]]:
        """Like files(), with optional filters: component, directory subtree,
        fuzzy file name, and indexed state."""
        sql = (
            "SELECT f.*, c.path AS root, c.version AS comp_version, d.path AS rel "
            "FROM file f JOIN directory d ON d.id = f.directory_id "
            "JOIN component c ON c.id = d.component_id"
        )
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
            comp_stub = Component(
                name="", path=row["root"], version=row["comp_version"]
            )
            eff = os.path.abspath(
                _pathx.resolve_fs_path(Storage.effective_root(comp_stub))
            )
            abs_path = (
                os.path.join(eff, row["rel"], row["name"])
                if row["rel"]
                else os.path.join(eff, row["name"])
            )
            out.append((_row_to(File, row), abs_path))
        return out

    def mark_file_indexed(self, file_id: int, mtime: Optional[float] = None) -> None:
        self._conn.execute(
            "UPDATE file SET indexed = 1, indexed_at = datetime('now'), "
            "  mtime = COALESCE(?, mtime) WHERE id = ?",
            (mtime, file_id),
        )
        self._commit()

    def set_file_indexed(self, file_id: int, indexed: bool) -> None:
        """Flip a file's indexed/pending flag in place; symbols are untouched.

        Setting indexed=0 marks the file pending so the next `index` re-parses
        it (regenerating graph edges) without losing its existing symbols."""
        self._conn.execute(
            "UPDATE file SET indexed = ? WHERE id = ?",
            (int(bool(indexed)), file_id),
        )
        self._commit()

    # -- diagnostics (v15) ---------------------------------------------------

    def replace_diagnostics(
        self, file_id: int, diags: Sequence[dict[str, Any]]
    ) -> None:
        """Replace the stored parse diagnostics for a file (TU) wholesale.

        Called on every (re)index so a now-clean file drops its stale rows.
        Each diag is a dict with severity/spelling/file_path/line/col; rows are
        inserted in the given order so their ids follow TU diagnostic order."""
        self._conn.execute("DELETE FROM diagnostic WHERE file_id = ?", (file_id,))
        for d in diags:
            self._conn.execute(
                "INSERT INTO diagnostic "
                "(file_id, severity, spelling, file_path, line, col) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    d["severity"],
                    d["spelling"],
                    d.get("file_path"),
                    d.get("line"),
                    d.get("col"),
                ),
            )
        self._commit()

    def get_diagnostics(self, file_id: int) -> list[Diagnostic]:
        """Stored parse diagnostics for a file, in insertion (TU) order."""
        rows = self._conn.execute(
            "SELECT * FROM diagnostic WHERE file_id = ? ORDER BY id", (file_id,)
        ).fetchall()
        return [_row_to(Diagnostic, r) for r in rows]

    def diagnostic_counts(self) -> dict[int, dict[int, int]]:
        """Per-file diagnostic counts grouped by severity: {file_id: {sev: n}}."""
        out: dict[int, dict[int, int]] = {}
        for fid, sev, n in self._conn.execute(
            "SELECT file_id, severity, COUNT(*) FROM diagnostic "
            "GROUP BY file_id, severity"
        ):
            out.setdefault(fid, {})[sev] = n
        return out

    def set_file_compile_options(
        self,
        file_id: int,
        options: list[str],
        driver: Optional[str] = None,
        update_driver: bool = False,
    ) -> None:
        """Replace a file's stored compile flags (and optionally its driver) and
        mark it args_overridden=1 so a later `import` (without --force) keeps the
        edit. Used by `cidx file -set-flag/-unset-flag/-import-args`."""
        opts = json.dumps(options)
        if update_driver:
            self._conn.execute(
                "UPDATE file SET compile_options = ?, driver = ?, "
                "args_overridden = 1 WHERE id = ?",
                (opts, driver, file_id),
            )
        else:
            self._conn.execute(
                "UPDATE file SET compile_options = ?, args_overridden = 1 WHERE id = ?",
                (opts, file_id),
            )
        self._commit()

    def update_file_compile_options(self, file_id: int, options: list[str]) -> None:
        """Replace a file's stored compile flags WITHOUT marking args_overridden.

        Used by `cidx realias`, which rewrites include paths to <label> tokens as
        a portability transform (not a manual edit) -- a later `import` should be
        free to re-strip + re-alias these files."""
        self._conn.execute(
            "UPDATE file SET compile_options = ? WHERE id = ?",
            (json.dumps(options), file_id),
        )
        self._commit()

    def is_file_indexed(
        self, abs_path: str, mtime: Optional[float] = None, md5: Optional[str] = None
    ) -> bool:
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
        """Reconstructed absolute path for a file id (uses effective root)."""
        row = self._conn.execute(
            "SELECT c.path AS root, c.version AS version, d.path AS rel, f.name AS name "
            "FROM file f JOIN directory d ON d.id = f.directory_id "
            "JOIN component c ON c.id = d.component_id WHERE f.id = ?",
            (file_id,),
        ).fetchone()
        if row is None:
            return None
        # Reconstruct via the effective root (base + version, resolved).
        comp_stub = Component(name="", path=row["root"], version=row["version"])
        eff = os.path.abspath(_pathx.resolve_fs_path(Storage.effective_root(comp_stub)))
        return (
            os.path.join(eff, row["rel"], row["name"])
            if row["rel"]
            else os.path.join(eff, row["name"])
        )

    def directory_abs_path(self, directory_id: int) -> Optional[str]:
        """Reconstructed absolute path for a directory id (uses effective root)."""
        row = self._conn.execute(
            "SELECT c.path AS root, c.version AS version, d.path AS rel FROM directory d "
            "JOIN component c ON c.id = d.component_id WHERE d.id = ?",
            (directory_id,),
        ).fetchone()
        if row is None:
            return None
        comp_stub = Component(name="", path=row["root"], version=row["version"])
        eff = os.path.abspath(_pathx.resolve_fs_path(Storage.effective_root(comp_stub)))
        return os.path.join(eff, row["rel"]) if row["rel"] else eff

    def _split_path(self, abs_path: str) -> tuple[int, str, str]:
        """Absolute path -> (component_id, relative dir, file name)."""
        abs_path = os.path.abspath(abs_path)
        comp = self.component_for_path(abs_path)
        if comp is None or comp.id is None:
            raise KeyError(f"no component owns {abs_path} (add_component first)")
        resolved_root = os.path.abspath(
            _pathx.resolve_fs_path(Storage.effective_root(comp))
        )
        rel = os.path.relpath(abs_path, resolved_root)
        rel_dir, name = os.path.split(rel)
        if rel_dir == ".":
            rel_dir = ""
        return comp.id, rel_dir, name

    # -- symbols ---------------------------------------------------------------

    _SYMBOL_COLS = (
        "usr",
        "spelling",
        "qual_name",
        "display_name",
        "kind",
        "type_info",
        "file_id",
        "line",
        "col",
        "decl_file_id",
        "decl_line",
        "decl_col",
        "decl_path",
        "is_definition",
        "is_pure",
        "is_static",
        "is_instantiation",
        "linkage",
        "access",
        "parent_usr",
        "resolved",
    )

    def add_symbol(self, sym: Symbol) -> int:
        """Insert or upsert a symbol keyed by USR. Returns the symbol id.

        A definition always wins over a previously stored declaration; a
        declaration never downgrades a stored definition's location.
        """
        if sym.kind not in SYMBOL_KINDS:
            raise ValueError(f"unknown symbol kind {sym.kind!r}")
        # kind is stored as its CXCursorKind integer (v16); convert on the way in.
        vals = tuple(
            SYMBOL_KIND_IDS[sym.kind] if c == "kind" else getattr(sym, c)
            for c in self._SYMBOL_COLS
        )
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
            "  is_definition    = MAX(excluded.is_definition, symbol.is_definition), "
            "  is_pure          = MAX(excluded.is_pure, symbol.is_pure), "
            "  is_static        = MAX(excluded.is_static, symbol.is_static), "
            "  is_instantiation = MAX(excluded.is_instantiation, symbol.is_instantiation), "
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
        if "kind" in values:
            if values["kind"] not in SYMBOL_KINDS:
                raise ValueError(f"unknown symbol kind {values['kind']!r}")
            values = {**values, "kind": SYMBOL_KIND_IDS[values["kind"]]}
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

    def lookup_symbols_by_name(
        self, spelling: str, kind: Optional[str] = None
    ) -> list[Symbol]:
        """All symbols with this spelling (overloads / statics give several rows)."""
        sql = "SELECT * FROM symbol WHERE spelling = ?"
        args: list[Any] = [spelling]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(SYMBOL_KIND_IDS.get(kind, -1))
        sql += " ORDER BY usr"
        return [_row_to(Symbol, r) for r in self._conn.execute(sql, args)]

    def lookup_symbols_by_qual_name(
        self, qual_name: str, kind: Optional[str] = None
    ) -> list[Symbol]:
        """All symbols with this fully-qualified name (overloads give several).

        Used to resolve a callee whose USR cannot be matched directly -- e.g. a
        member function template referenced in a dependent template body, where
        libclang emits an inconsistent USR (parameter types collapse). The
        qualified name + kind are stable, so an unambiguous (single) match
        recovers the target."""
        sql = "SELECT * FROM symbol WHERE qual_name = ?"
        args: list[Any] = [qual_name]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(SYMBOL_KIND_IDS.get(kind, -1))
        sql += " ORDER BY usr"
        return [_row_to(Symbol, r) for r in self._conn.execute(sql, args)]

    def search_symbols(self, pattern: str, kind: Optional[str] = None) -> list[Symbol]:
        """Fuzzy match against the qualified name (case-insensitive).

        Each '::'-separated segment of `pattern` must appear, in order, as a
        substring of qual_name: 'conf::set' matches 'RdKafka::ConfImpl::set'.
        """
        like = (
            "%"
            + "%".join(
                seg.replace("%", r"\%").replace("_", r"\_")
                for seg in pattern.split("::")
                if seg
            )
            + "%"
        )
        sql = r"SELECT * FROM symbol WHERE qual_name LIKE ? ESCAPE '\'"
        args: list[Any] = [like]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(SYMBOL_KIND_IDS.get(kind, -1))
        sql += " ORDER BY LENGTH(qual_name), qual_name"
        return [_row_to(Symbol, r) for r in self._conn.execute(sql, args)]

    def list_symbols(
        self,
        component_id: Optional[int] = None,
        dir_path: Optional[str] = None,
        file_id: Optional[int] = None,
        name: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> list[Symbol]:
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
            if component_id is None:  # dir filter across all components
                scope, scope_args = [], []
            if dir_path is not None:
                scope.append(self._dir_scope_sql(dir_path, scope_args))
            where.append(
                "EXISTS (SELECT 1 FROM file f "
                "JOIN directory d ON d.id = f.directory_id "
                "WHERE f.id IN (s.file_id, s.decl_file_id) AND "
                + " AND ".join(scope)
                + ")"
            )
            args.extend(scope_args)
        if file_id is not None:
            where.append("(s.file_id = ? OR s.decl_file_id = ?)")
            args.extend([file_id, file_id])
        if name:
            where.append(r"COALESCE(s.qual_name, s.spelling) LIKE ? ESCAPE '\'")
            args.append(self._fuzzy_like(name))
        if kind is not None:
            where.append("s.kind = ?")
            args.append(SYMBOL_KIND_IDS.get(kind, -1))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " ORDER BY LENGTH(COALESCE(s.qual_name, s.spelling)),"
            " COALESCE(s.qual_name, s.spelling)"
        )
        return [_row_to(Symbol, r) for r in self._conn.execute(sql, args)]

    def symbols_in_file(self, file_id: int) -> list[Symbol]:
        return [
            _row_to(Symbol, r)
            for r in self._conn.execute(
                "SELECT * FROM symbol WHERE file_id = ? ORDER BY line, col", (file_id,)
            )
        ]

    def unresolved_symbols(self) -> list[Symbol]:
        return [
            _row_to(Symbol, r)
            for r in self._conn.execute(
                "SELECT * FROM symbol WHERE resolved = 0 ORDER BY usr"
            )
        ]

    # -- graph (v7) ------------------------------------------------------------

    def mint_symbol_id(
        self,
        usr: str,
        spelling: str = "",
        qual_name: Optional[str] = None,
        display_name: Optional[str] = None,
        kind: str = "function",
        decl_file_id: Optional[int] = None,
        decl_line: Optional[int] = None,
        decl_col: Optional[int] = None,
        decl_path: Optional[str] = None,
        is_instantiation: bool = False,
        is_named_instance: bool = False,
    ) -> int:
        """Insert a stub row for `usr` (if absent), then SELECT its id.

        The callee/base/override/primary reference cursor is always in hand at
        the call site, so its name, kind AND declaration location travel with
        the USR: a stub is born NAMED, correctly typed, and -- when the
        reference cursor carries a source location in an indexed file -- LOCATED
        (e.g. a defaulted ctor anchored to its `struct` line). This matters for
        targets whose definition is never separately indexed (implicit/defaulted
        special members, implicit template instantiations) -- without a
        backfilling `add_symbol`, this mint is all the graph will ever have, so
        dropping libclang's location here is what made `chain::D::D` print
        `@<no-location>`. `decl_file_id` is None for targets in unregistered
        (e.g. system/stdlib) headers; for those the AST still carries a real
        source location, so the caller passes the raw path as `decl_path` (with
        decl_line/decl_col) and the stub stays located -- e.g. a libstdc++
        `__normal_iterator::operator*` resolves to `stl_iterator.h:1234` instead
        of `@<no-location>`. Only a target with no source location at all
        (implicit/builtin) is truly location-less.

        'function' is the fallback kind when the cursor kind is unknown; the
        real def's add_symbol upsert overwrites kind/spelling/location/resolved
        later. On a repeat mint we only UPGRADE an unnamed stub (empty spelling)
        -- name and kind together -- never clobber a real symbol's; the decl
        location (registered or raw path) is filled in only when still absent
        (COALESCE).

        `is_instantiation=True` marks implicit template-instantiation nodes
        (v13: both the X<int> type node and each X<int>::member node). The flag
        is set via MAX() so a later stub->instantiation promotion always upgrades
        but never downgrades.
        """
        self._conn.execute(
            "INSERT INTO symbol (usr, spelling, qual_name, display_name, kind, "
            "                    decl_file_id, decl_line, decl_col, decl_path, "
            "                    is_instantiation, is_named_instance, resolved) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(usr) DO UPDATE SET "
            "  kind             = CASE WHEN symbol.spelling = '' "
            "                          THEN excluded.kind ELSE symbol.kind END, "
            "  spelling         = CASE WHEN symbol.spelling = '' "
            "                          THEN excluded.spelling ELSE symbol.spelling END, "
            "  qual_name        = COALESCE(symbol.qual_name, excluded.qual_name), "
            "  display_name     = COALESCE(symbol.display_name, excluded.display_name), "
            "  decl_file_id     = COALESCE(symbol.decl_file_id, excluded.decl_file_id), "
            "  decl_line        = COALESCE(symbol.decl_line, excluded.decl_line), "
            "  decl_col         = COALESCE(symbol.decl_col, excluded.decl_col), "
            "  decl_path        = COALESCE(symbol.decl_path, excluded.decl_path), "
            "  is_instantiation = MAX(symbol.is_instantiation, excluded.is_instantiation), "
            "  is_named_instance = MAX(symbol.is_named_instance, excluded.is_named_instance)",
            (
                usr,
                spelling,
                qual_name or None,
                display_name or None,
                SYMBOL_KIND_IDS.get(kind, -1),  # kind stored as CXCursorKind int (v16)
                decl_file_id,
                decl_line,
                decl_col,
                decl_path or None,
                1 if is_instantiation else 0,
                1 if is_named_instance else 0,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM symbol WHERE usr = ?", (usr,)
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"mint_symbol_id: SELECT returned no row for usr={usr!r}"
            )
        return row["id"]

    def add_edge(
        self,
        src_id: int,
        dst_id: int,
        kind: int,
        count: int = 1,
        base_access: Optional[int] = None,
        is_virtual: Optional[int] = None,
        vtable_slot: Optional[int] = None,
    ) -> int:
        """Upsert an edge; returns the edge id."""
        cur = self._conn.execute(
            "INSERT INTO edge (src_id, dst_id, kind, count, base_access, is_virtual, "
            "                  vtable_slot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(src_id, dst_id, kind) DO UPDATE SET "
            "  count       = edge.count + excluded.count, "
            "  base_access = COALESCE(excluded.base_access, edge.base_access), "
            "  is_virtual  = COALESCE(excluded.is_virtual,  edge.is_virtual), "
            "  vtable_slot = COALESCE(excluded.vtable_slot, edge.vtable_slot) "
            "RETURNING id",
            (src_id, dst_id, kind, count, base_access, is_virtual, vtable_slot),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("add_edge: upsert returned no id")
        return row["id"]

    def add_edge_site(
        self,
        edge_id: int,
        file_id: Optional[int],
        line: Optional[int],
        col: Optional[int],
        conditional: int = 0,
        args_sig: Optional[str] = None,
        recv_src_kind: Optional[str] = None,
        recv_type_usr: Optional[str] = None,
        recv_decl_usr: Optional[str] = None,
        recv_param_pos: Optional[int] = None,
        recv_type_is_value: Optional[int] = None,
    ) -> None:
        """INSERT OR IGNORE an edge_site (PK collision = same site, harmless)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO edge_site "
            "(edge_id, file_id, line, col, conditional, args_sig, "
            " recv_src_kind, recv_type_usr, recv_decl_usr, recv_param_pos,"
            " recv_type_is_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                edge_id,
                file_id,
                line,
                col,
                conditional,
                args_sig,
                recv_src_kind,
                recv_type_usr,
                recv_decl_usr,
                recv_param_pos,
                recv_type_is_value,
            ),
        )

    def add_call_arg(
        self,
        edge_id: int,
        file_id: int,
        line: int,
        col: int,
        position: int,
        src_kind: str,
        type_usr: Optional[str] = None,
        decl_usr: Optional[str] = None,
        callee_usr: Optional[str] = None,
        type_is_value: Optional[int] = None,
    ) -> None:
        """INSERT OR IGNORE a call_arg row (PK collision = same arg, harmless)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO call_arg "
            "(edge_id, file_id, line, col, position, src_kind, "
            " type_usr, decl_usr, callee_usr, type_is_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                edge_id,
                file_id,
                line,
                col,
                position,
                src_kind,
                type_usr,
                decl_usr,
                callee_usr,
                type_is_value,
            ),
        )

    def add_template_param(
        self,
        owner_id: int,
        position: int,
        param_kind: int,
        name: Optional[str] = None,
        default_txt: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO template_param "
            "(owner_id, position, param_kind, name, default_txt) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_id, position, param_kind, name, default_txt),
        )

    def add_template_arg(
        self,
        owner_id: int,
        position: int,
        arg_kind: int,
        ref_id: Optional[int] = None,
        literal: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO template_arg "
            "(owner_id, position, arg_kind, ref_id, literal) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_id, position, arg_kind, ref_id, literal),
        )

    def delete_edges_for_file(self, file_id: int) -> None:
        """Remove edges whose src symbol was indexed from this file.

        Contains edges (kind=3) are excluded: they are declaration-level
        structural edges emitted once during header indexing. Namespace and
        record membership spans multiple TUs (the same namespace reopens in
        every .cpp that uses it), so deleting contains on each re-index would
        permanently erase edges emitted during the header-indexing pass.
        Contains edges are idempotent (UPSERT) and survive stale re-indexes.
        """
        self._conn.execute(
            "DELETE FROM edge WHERE kind != 3 AND src_id IN "
            "(SELECT id FROM symbol WHERE file_id = ?)",
            (file_id,),
        )
        self._commit()

    # -- entity_edge (v17) ------------------------------------------------------

    def add_entity_edge(
        self,
        src_id: int,
        dst_id: int,
        kind: int,
        count: int = 1,
        via_member_id: Optional[int] = None,
        multiplicity: int = 1,
        access: int = 0,
        is_virtual: int = 0,
        create_form: Optional[int] = None,
        partial: int = 0,
    ) -> None:
        """Upsert an entity_edge row (re-materialise safe via ON CONFLICT DO UPDATE)."""
        self._conn.execute(
            "INSERT INTO entity_edge "
            "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
            " access, is_virtual, create_form, partial) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(src_id, dst_id, kind, "
            "            COALESCE(via_member_id, -1), COALESCE(create_form, -1)) "
            "DO UPDATE SET "
            "  count       = excluded.count, "
            "  multiplicity = excluded.multiplicity, "
            "  access      = excluded.access, "
            "  is_virtual  = excluded.is_virtual, "
            "  create_form = COALESCE(excluded.create_form, entity_edge.create_form), "
            "  partial     = excluded.partial",
            (src_id, dst_id, kind, count, via_member_id, multiplicity,
             access, is_virtual, create_form, partial),
        )
        self._commit()

    def clear_entity_edges(self) -> None:
        """Delete all entity_edge rows (pre-step for idempotent re-materialise)."""
        self._conn.execute("DELETE FROM entity_edge")
        self._commit()

    def entity_edges(
        self,
        src_id: Optional[int] = None,
        dst_id: Optional[int] = None,
        kind: Optional[int] = None,
    ) -> list[dict]:
        """Return entity_edge rows as dicts, optionally filtered.

        All columns returned. Rows sorted by (src_id, kind, dst_id).
        """
        wheres: list[str] = []
        params: list[Any] = []
        if src_id is not None:
            wheres.append("src_id = ?")
            params.append(src_id)
        if dst_id is not None:
            wheres.append("dst_id = ?")
            params.append(dst_id)
        if kind is not None:
            wheres.append("kind = ?")
            params.append(kind)
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        rows = self._conn.execute(
            f"SELECT src_id, dst_id, kind, count, via_member_id, multiplicity, "
            f"access, is_virtual, create_form, partial "
            f"FROM entity_edge {where_sql} "
            f"ORDER BY src_id, kind, dst_id",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def rollup_edge_counts(self) -> None:
        """For calls (1) and uses (7): set count = COUNT(edge_site)."""
        self._conn.execute(
            "UPDATE edge SET count = ("
            "  SELECT COUNT(*) FROM edge_site WHERE edge_site.edge_id = edge.id"
            ") "
            "WHERE kind IN (1, 7)"
            "  AND EXISTS (SELECT 1 FROM edge_site WHERE edge_site.edge_id = edge.id)"
        )
        self._commit()

    def cross_repo_edges(self) -> list[tuple[int, int, int]]:
        """Return (src_id, dst_id, kind) for edges crossing component boundaries."""
        rows = self._conn.execute(
            "SELECT e.src_id, e.dst_id, e.kind FROM edge e "
            "JOIN symbol src ON src.id = e.src_id "
            "JOIN symbol dst ON dst.id = e.dst_id "
            "JOIN file sf ON sf.id = src.file_id "
            "JOIN directory sd ON sd.id = sf.directory_id "
            "JOIN file df ON df.id = dst.file_id "
            "JOIN directory dd ON dd.id = df.directory_id "
            "WHERE sd.component_id != dd.component_id"
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a meta row."""
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._commit()

    def resolve_pass(self) -> tuple[int, int]:
        """Roll up edge counts, materialise entity_edge, write graph_resolved_at meta.

        Returns (still_stub_count, cross_repo_edge_count).
        """
        from datetime import datetime, timezone
        from indexer.entity_rollup import materialize_entity_edges

        self.rollup_edge_counts()
        materialize_entity_edges(self)
        # A still-stub is a minted placeholder never backfilled by a real
        # symbol: resolved=0 with NO location (neither a definition nor a decl
        # site). NOT keyed on spelling -- stubs are now minted NAMED, so the
        # absence of any location is the robust signal (matches Sym.is_stub).
        row = self._conn.execute(
            "SELECT COUNT(*) FROM symbol "
            "WHERE resolved = 0 AND file_id IS NULL AND decl_file_id IS NULL"
        ).fetchone()
        stubs = row[0] if row else 0
        cross = self.cross_repo_edges()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.set_meta("graph_resolved_at", ts)
        return stubs, len(cross)

    # -- stats -----------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        one = lambda sql: self._conn.execute(sql).fetchone()[0]  # noqa: E731
        by_kind = {
            SYMBOL_KIND_NAMES.get(r["kind"], r["kind"]): r["n"]
            for r in self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM symbol GROUP BY kind ORDER BY kind"
            )
        }
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

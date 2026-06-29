// Port of indexer/storage.py. The schema text and the three upsert statements
// are copied character-for-character (design §4/§4.2); read queries keep the
// same WHERE/ORDER BY text but select explicit column lists so that rows from
// MIGRATED databases (where ALTER TABLE appended columns at the end) decode
// correctly without name-based row factories.
#include "storage/storage.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstring>
#include <exception>
#include <filesystem>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>

#include "compiledb/compiledb.hpp"
#include "util/errors.hpp"
#include "util/json_min.hpp"
#include "util/logger.hpp"
#include "util/pathutil.hpp"

namespace cidx {

namespace {

// Schema v6 — exact text from design §4 (= Python's expanded _SCHEMA, kinds
// in sorted order).
constexpr char kSchema[] = R"sql(
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- v23: a repository groups one or more components under one logical code base.
-- A repo can be checked out in several directories (git worktrees / separate
-- clones); each is a `clone` row and `active_clone_id` names the live one.
CREATE TABLE IF NOT EXISTS repository (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'repo'
                    CHECK (kind IN ('repo', 'external')),
    remote_url      TEXT,                 -- git origin URL when known
    active_clone_id INTEGER               -- -> clone.id (no FK: circular w/ clone)
);

CREATE TABLE IF NOT EXISTS clone (
    id            INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repository(id) ON DELETE CASCADE,
    path          TEXT NOT NULL UNIQUE,   -- absolute checkout/worktree root
    label         TEXT                    -- optional human label (branch/worktree)
);

CREATE TABLE IF NOT EXISTS component (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    path    TEXT NOT NULL UNIQUE,         -- base path (no version segment)
    kind    TEXT NOT NULL DEFAULT 'repo'
            CHECK (kind IN ('repo', 'external')),
    version TEXT,                         -- v14: nullable; NULL = unversioned
    repository_id INTEGER                 -- v23: -> repository.id; NULL = ungrouped
            REFERENCES repository(id) ON DELETE SET NULL
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
    is_named_instance INTEGER NOT NULL DEFAULT 0, -- v20: instance minted from a
                                              -- NAMED using/typedef alias (X<B>);
                                              -- carries its own composes/
                                              -- aggregates/associates (T->B)
                                              -- instead of collapsing onto the
                                              -- primary
    linkage      TEXT,                  -- 'external' | 'internal' | 'no-linkage' | ...
    access       TEXT,                  -- C++: 'public' | 'protected' | 'private'
    parent_usr   TEXT,                  -- semantic parent (class/namespace) USR
    resolved     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_symbol_spelling ON symbol(spelling);
CREATE INDEX IF NOT EXISTS idx_symbol_qual     ON symbol(qual_name);
-- NOCASE companions: let a case-insensitive prefix LIKE ('Foo%') on these
-- columns become a range SEARCH instead of a full scan (query.py find tier 2,
-- search_symbols). A BINARY index cannot serve a case-insensitive LIKE; a
-- NOCASE index can. Additive -- created on every open via this script, so an
-- existing index gains them with no reindex.
CREATE INDEX IF NOT EXISTS idx_symbol_spelling_nc ON symbol(spelling COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_symbol_qual_nc     ON symbol(qual_name COLLATE NOCASE);
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
  (2,'struct'), (3,'union'), (4,'class'), (5,'enum'), (6,'member'), (7,'enum-constant'), (8,'function'), (9,'variable'), (20,'typedef'), (21,'method'), (22,'namespace'), (24,'constructor'), (25,'destructor'), (30,'function-template'), (31,'class-template'), (36,'type-alias'), (501,'macro');

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
-- Entity = a symbol whose kind is in {class,struct,union,enum}; no separate table.
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
-- INSERT ... ON CONFLICT upserts in the materialise pass would silently fan out
-- into duplicate rows on every materialise.  A COALESCE expression index folds
-- NULL to a sentinel so the identity is NULL-safe; create_form is part of the
-- key so distinct creates/destroys forms (value/temp/heap/...) stay separate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_edge_identity ON entity_edge(
    src_id, dst_id, kind,
    COALESCE(via_member_id, -1), COALESCE(create_form, -1)
);
CREATE INDEX IF NOT EXISTS idx_entity_edge_src  ON entity_edge(src_id, kind);
CREATE INDEX IF NOT EXISTS idx_entity_edge_dst  ON entity_edge(dst_id, kind);

-- ---- v22: entity-node type (Layer-1 design-entity classification) -----------
-- The *type of an entity node* in the UML/abstraction graph, materialised at
-- `cidx resolve` alongside entity_edge. Mirrors storage.py byte-identically.
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

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '23');
)sql";

// v2 -> v3 qual_name backfill — verbatim from storage.py:231-244: the longest
// stored parent_usr chain per symbol is the full qualified path; empty parent
// spellings (anonymous scopes) are skipped.
constexpr char kQualNameBackfill[] = R"sql(
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
)sql";

constexpr std::array<std::string_view, 17> kSymbolKinds = {
    "class",          "struct",
    "union",          "function",
    "method",         "member",
    "constructor",    "destructor",
    "enum",           "enum-constant",
    "typedef",        "type-alias",
    "class-template", "function-template",
    "variable",       "namespace",
    "macro",
};

// symbol.kind name <-> stored integer (== libclang CXCursorKind). Mirrors
// storage.py SYMBOL_KIND_IDS / SYMBOL_KIND_NAMES. The metadata symbol_kind
// table is seeded with the same pairs (display only).
const std::map<std::string_view, int64_t> &symbol_kind_ids_map() {
  static const std::map<std::string_view, int64_t> m = {
      {"struct", 2},      {"union", 3},              {"class", 4},
      {"enum", 5},        {"member", 6},             {"enum-constant", 7},
      {"function", 8},    {"variable", 9},           {"typedef", 20},
      {"method", 21},     {"namespace", 22},         {"constructor", 24},
      {"destructor", 25}, {"function-template", 30}, {"class-template", 31},
      {"type-alias", 36}, {"macro", 501},
  };
  return m;
}

const std::map<int64_t, std::string> &symbol_kind_names_map() {
  static const std::map<int64_t, std::string> m = [] {
    std::map<int64_t, std::string> r;
    for (const auto &kv : symbol_kind_ids_map()) {
      r.emplace(kv.second, std::string(kv.first));
    }
    return r;
  }();
  return m;
}

// Python Storage._SYMBOL_COLS — insert/update order is load-bearing for the
// upsert statement and for update_symbol validation.
constexpr std::array<std::string_view, 21> kSymbolInsertCols = {
    "usr",            "spelling",      "qual_name",    "display_name",
    "kind",           "type_info",     "file_id",      "line",
    "col",            "decl_file_id",  "decl_line",    "decl_col",
    "decl_path",      "is_definition", "is_pure",      "is_static",
    "is_instantiation", "linkage",     "access",       "parent_usr",
    "resolved",
};

// Explicit SELECT lists (stable column positions even on migrated DBs).
// Column order mirrors _SYMBOL_COLS in storage.py; is_instantiation comes
// right after is_static (col index 16), then linkage/access/parent_usr/resolved
// (17-20), then decl_path appended last (21) -- append-at-end pattern for
// migrated DBs (ALTER TABLE appends; positional decode in symbol_from must match).
// v14: version appended at end (append-at-end discipline so migrated DBs
// whose ALTER added the column last decode positionally).
constexpr char kComponentCols[] =
    "id, name, path, kind, version, repository_id";
constexpr char kRepositoryCols[] =
    "id, name, kind, remote_url, active_clone_id";
constexpr char kCloneCols[] = "id, repository_id, path, label";
constexpr char kDirectoryCols[] = "id, component_id, path";
constexpr char kFileCols[] =
    "id, directory_id, name, mtime, md5, compile_options, driver, indexed, "
    "indexed_at, args_overridden";
constexpr char kSymbolCols[] =
    "id, usr, spelling, qual_name, display_name, kind, type_info, file_id, "
    "line, col, decl_file_id, decl_line, decl_col, is_definition, is_pure, "
    "is_static, linkage, access, parent_usr, resolved, decl_path, is_instantiation";
constexpr char kSymbolColsS[] =
    "s.id, s.usr, s.spelling, s.qual_name, s.display_name, s.kind, "
    "s.type_info, s.file_id, s.line, s.col, s.decl_file_id, s.decl_line, "
    "s.decl_col, s.is_definition, s.is_pure, s.is_static, s.linkage, s.access, "
    "s.parent_usr, s.resolved, s.decl_path, s.is_instantiation";

std::optional<int64_t> opt_int64(const SqliteStmt &st, int idx) {
  if (st.col_is_null(idx)) {
    return std::nullopt;
  }
  return st.col_int64(idx);
}

std::optional<double> opt_double(const SqliteStmt &st, int idx) {
  if (st.col_is_null(idx)) {
    return std::nullopt;
  }
  return st.col_double(idx);
}

std::optional<std::string> opt_text(const SqliteStmt &st, int idx) {
  if (st.col_is_null(idx)) {
    return std::nullopt;
  }
  return st.col_text(idx);
}

void bind_opt(SqliteStmt &st, int idx, const std::optional<int64_t> &v) {
  if (v) {
    st.bind(idx, *v);
  } else {
    st.bind_null(idx);
  }
}

void bind_opt(SqliteStmt &st, int idx, const std::optional<double> &v) {
  if (v) {
    st.bind(idx, *v);
  } else {
    st.bind_null(idx);
  }
}

void bind_opt(SqliteStmt &st, int idx, const std::optional<std::string> &v) {
  if (v) {
    st.bind(idx, std::string_view(*v));
  } else {
    st.bind_null(idx);
  }
}

Component component_from(const SqliteStmt &st) {
  Component c;
  c.id = st.col_int64(0);
  c.name = st.col_text(1);
  c.path = st.col_text(2);
  c.kind = st.col_text(3);
  // v14: version at column 4 (appended last; nullopt when NULL)
  c.version = opt_text(st, 4);
  // v23: repository_id at column 5 (SELECT * order; nullopt when NULL/ungrouped)
  c.repository_id = opt_int64(st, 5);
  return c;
}

Repository repository_from(const SqliteStmt &st) {
  Repository r;
  r.id = st.col_int64(0);
  r.name = st.col_text(1);
  r.kind = st.col_text(2);
  r.remote_url = opt_text(st, 3);
  r.active_clone_id = opt_int64(st, 4);
  return r;
}

Clone clone_from(const SqliteStmt &st) {
  Clone c;
  c.id = st.col_int64(0);
  c.repository_id = st.col_int64(1);
  c.path = st.col_text(2);
  c.label = opt_text(st, 3);
  return c;
}

Directory directory_from(const SqliteStmt &st) {
  Directory d;
  d.id = st.col_int64(0);
  d.component_id = st.col_int64(1);
  d.path = st.col_text(2);
  return d;
}

File file_from(const SqliteStmt &st) {
  File f;
  f.id = st.col_int64(0);
  f.directory_id = st.col_int64(1);
  f.name = st.col_text(2);
  f.mtime = opt_double(st, 3);
  f.md5 = opt_text(st, 4);
  if (!st.col_is_null(5)) {
    f.compile_options = json_min::decode_string_array(st.col_text(5));
  }
  f.driver = opt_text(st, 6);
  f.indexed = st.col_int64(7) != 0;
  f.indexed_at = opt_text(st, 8);
  f.args_overridden = st.col_int64(9) != 0;
  return f;
}

// symbol_from_offset: decode kSymbolColsS starting at column `off`.
// Called by symbol_from (off=0) and by A6 graph_edges (off=8, where 8 edge
// columns precede the symbol columns).
Symbol symbol_from_offset(const SqliteStmt &st, int off) {
  Symbol s;
  s.id = st.col_int64(off + 0);
  s.usr = st.col_text(off + 1);
  s.spelling = st.col_text(off + 2);
  s.qual_name = opt_text(st, off + 3);
  s.display_name = opt_text(st, off + 4);
  s.kind = symbol_kind_name(st.col_int64(off + 5)); // stored as CXCursorKind int
  s.type_info = opt_text(st, off + 6);
  s.file_id = opt_int64(st, off + 7);
  s.line = opt_int64(st, off + 8);
  s.col = opt_int64(st, off + 9);
  s.decl_file_id = opt_int64(st, off + 10);
  s.decl_line = opt_int64(st, off + 11);
  s.decl_col = opt_int64(st, off + 12);
  s.is_definition = st.col_int64(off + 13) != 0;
  s.is_pure = st.col_int64(off + 14) != 0;
  s.is_static = st.col_int64(off + 15) != 0;
  s.linkage = opt_text(st, off + 16);
  s.access = opt_text(st, off + 17);
  s.parent_usr = opt_text(st, off + 18);
  s.resolved = st.col_int64(off + 19) != 0;
  s.decl_path = opt_text(st, off + 20);
  s.is_instantiation = st.col_int64(off + 21) != 0;
  return s;
}

Symbol symbol_from(const SqliteStmt &st) {
  return symbol_from_offset(st, 0);
}

// Escape the LIKE metacharacters for use with ESCAPE '\' — order matters:
// backslash first.
std::string escape_like(const std::string &text) {
  std::string out;
  out.reserve(text.size());
  for (const char c : text) {
    if (c == '\\') {
      out += "\\\\";
    } else if (c == '%') {
      out += "\\%";
    } else if (c == '_') {
      out += "\\_";
    } else {
      out += c;
    }
  }
  return out;
}

std::string join_strings(const std::vector<std::string> &parts,
                         const std::string &sep) {
  std::string out;
  for (std::size_t i = 0; i < parts.size(); ++i) {
    if (i != 0) {
      out += sep;
    }
    out += parts[i];
  }
  return out;
}

// abs path = component.path / directory.path / file.name (rel may be '').
std::string reconstruct_path(const std::string &root, const std::string &rel,
                             const std::string &name) {
  if (rel.empty()) {
    return pathutil::join(root, name);
  }
  return pathutil::join(root, rel, name);
}

// mkdir -p the DB directory before opening (storage.py:202-203); :memory:
// passes through untouched.
std::string prepare_db_path(const std::string &path) {
  if (path != ":memory:") {
    const std::string dir = pathutil::dirname(pathutil::abspath(path));
    std::error_code ec;
    std::filesystem::create_directories(dir, ec);
    if (ec) {
      throw StorageError("cannot create database directory " + dir + ": " +
                         ec.message());
    }
  }
  return path;
}

} // namespace

bool is_symbol_kind(std::string_view kind) {
  return std::find(kSymbolKinds.begin(), kSymbolKinds.end(), kind) !=
         kSymbolKinds.end();
}

int64_t symbol_kind_id(std::string_view name) {
  const auto &m = symbol_kind_ids_map();
  const auto it = m.find(name);
  return it != m.end() ? it->second : -1; // unknown -> matches nothing (filters)
}

std::string symbol_kind_name(int64_t id) {
  const auto &m = symbol_kind_names_map();
  const auto it = m.find(id);
  return it != m.end() ? it->second : std::to_string(id);
}

// -- Transaction --------------------------------------------------------------

Transaction::Transaction(Storage &db)
    : db_(db), uncaught_on_entry_(std::uncaught_exceptions()) {
  if (db_.in_txn_) {
    throw StorageError("nested Storage::transaction() is not supported");
  }
  db_.db_.exec("BEGIN");
  db_.in_txn_ = true;
}

Transaction::~Transaction() {
  if (done_) {
    return;
  }
  // Destructor is ROLLBACK-only. Successful paths must call txn.commit()
  // explicitly so a failed COMMIT is not silently swallowed here (R2).
  // We only reach this branch during exception unwind (or forgotten commit).
  try {
    db_.db_.exec("ROLLBACK");
  } catch (...) {
    // Destructor must not throw; the connection rolls back on close anyway.
  }
  db_.in_txn_ = false;
}

void Transaction::commit() {
  if (done_) {
    return;
  }
  db_.db_.exec("COMMIT");
  db_.in_txn_ = false;
  done_ = true;
}

void Transaction::rollback() {
  if (done_) {
    return;
  }
  db_.db_.exec("ROLLBACK");
  db_.in_txn_ = false;
  done_ = true;
}

// -- Storage lifecycle
// ---------------------------------------------------------

// Defined below (materialise pass); forward-declared so the constructor can run
// the v21->v22 entity_node backfill right after the schema is created.
static void cpp_materialise_entity_nodes(cidx::SqliteDb &db);

Storage::Storage(const std::string &path) : db_(prepare_db_path(path)) {
  db_.exec("PRAGMA foreign_keys = ON");
  migrate(); // BEFORE the schema script: its indexes need migrated columns
             // (G19)
  db_.exec(kSchema);
  // v21 -> v22 one-time backfill: entity_node is a pure-DB classification of
  // existing symbols (no re-parse), so an upgraded index gets its design types
  // filled in immediately on open -- no re-index/resolve. (entity_node did not
  // exist during migrate(); it does now, after kSchema.) Mirrors storage.py.
  if (needs_entity_node_backfill_) {
    auto txn = transaction();
    cpp_materialise_entity_nodes(db_);
    txn.commit();
  }
}

void Storage::migrate() {
  std::vector<std::string> tables;
  {
    auto st =
        db_.prepare("SELECT name FROM sqlite_master WHERE type = 'table'");
    while (st.step()) {
      tables.push_back(st.col_text(0));
    }
  }
  const auto has_table = [&tables](const char *name) {
    return std::find(tables.begin(), tables.end(), name) != tables.end();
  };
  if (!has_table("symbol")) {
    return; // fresh database: the schema script creates everything
  }
  const auto table_columns = [this](const char *table) {
    std::vector<std::string> cols;
    auto st = db_.prepare(std::string("PRAGMA table_info(") + table + ")");
    while (st.step()) {
      cols.push_back(st.col_text(1));
    }
    return cols;
  };
  const auto has_col = [](const std::vector<std::string> &cols,
                          const char *name) {
    return std::find(cols.begin(), cols.end(), name) != cols.end();
  };

  const auto cols = table_columns("symbol");
  bool changed = false;
  if (!has_col(cols, "qual_name")) {
    db_.exec("ALTER TABLE symbol ADD COLUMN qual_name TEXT");
    db_.exec(kQualNameBackfill);
    changed = true;
  }
  if (!has_col(cols, "decl_file_id")) {
    db_.exec("ALTER TABLE symbol ADD COLUMN decl_file_id INTEGER "
             "REFERENCES file(id) ON DELETE SET NULL");
    db_.exec("ALTER TABLE symbol ADD COLUMN decl_line INTEGER");
    db_.exec("ALTER TABLE symbol ADD COLUMN decl_col INTEGER");
    db_.exec("UPDATE symbol SET decl_file_id = file_id, decl_line = line, "
             "decl_col = col WHERE is_definition = 0");
    changed = true;
  }
  if (!has_col(cols, "is_pure")) {
    // No backfill possible from stored data -- reindex to populate.
    db_.exec(
        "ALTER TABLE symbol ADD COLUMN is_pure INTEGER NOT NULL DEFAULT 0");
    changed = true;
  }
  if (!has_col(cols, "is_static")) {
    // v11 -> v12: C++ static member function flag. No backfill possible from
    // stored data -- reindex to populate; old rows read as 0.
    db_.exec(
        "ALTER TABLE symbol ADD COLUMN is_static INTEGER NOT NULL DEFAULT 0");
    changed = true;
  }
  if (!has_col(cols, "is_instantiation")) {
    // v12 -> v13: implicit template-instantiation node marker. No backfill
    // possible from stored data -- reindex to populate; old rows read as 0.
    db_.exec(
        "ALTER TABLE symbol ADD COLUMN is_instantiation INTEGER NOT NULL DEFAULT 0");
    changed = true;
  }
  if (!has_col(cols, "decl_path")) {
    // v8 -> v9: raw decl path for stubs whose target lives in an unregistered
    // (system/stdlib) file. No backfill -- those rows had no location to
    // recover; a reindex repopulates it from the AST.
    db_.exec("ALTER TABLE symbol ADD COLUMN decl_path TEXT");
    changed = true;
  }
  // v15 -> v16: symbol.kind moves from a TEXT name to its CXCursorKind integer
  // (compact storage; the symbol_kind table recovers the string). The column
  // type changes and the old CHECK must go, so the table is rebuilt in place
  // with the kind values converted. Runs after the column-add migrations above
  // so the new table mirrors all columns. Mirrors storage.py.
  {
    std::string kind_type;
    {
      // Read the kind column's declared type. Finalize this statement (close
      // the scope) BEFORE the rebuild -- an open read on `symbol` would make
      // DROP TABLE fail with "database table is locked".
      auto st = db_.prepare("PRAGMA table_info(symbol)");
      while (st.step()) {
        if (st.col_text(1) == "kind") {
          kind_type = st.col_text(2);
        }
      }
    }
    for (char &c : kind_type) {
      c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    }
    if (kind_type != "INTEGER") {
      int64_t nrows = 0;
      {
        auto cs = db_.prepare("SELECT COUNT(*) FROM symbol");
        if (cs.step()) {
          nrows = cs.col_int64(0);
        }
      }
      migrate_symbol_kind_to_int();
      changed = true;
    }
  }
  // v19 -> v20: named-instance marker. A template instance minted from a NAMED
  // using/typedef alias (X<B>) carries its own composes/aggregates/associates
  // instead of collapsing onto the primary. No backfill -- reindex repopulates;
  // old rows read as 0. Re-read columns: the v15->v16 rebuild above recreates
  // `symbol` (without this column), so the snapshot from the top is stale.
  {
    const auto cols2 = table_columns("symbol");
    if (!has_col(cols2, "is_named_instance")) {
      db_.exec("ALTER TABLE symbol ADD COLUMN is_named_instance INTEGER NOT NULL "
               "DEFAULT 0");
      changed = true;
    }
  }
  if (has_table("file")) {
    const auto fcols = table_columns("file");
    if (!has_col(fcols, "driver")) {
      // No backfill possible from stored data -- re-import to populate.
      db_.exec("ALTER TABLE file ADD COLUMN driver TEXT");
      changed = true;
    }
    // v7 -> v8: per-file flag override marker (`cidx file`). Existing rows
    // default to 0 (not overridden), so re-import behaves as before.
    if (!has_col(fcols, "args_overridden")) {
      db_.exec("ALTER TABLE file ADD COLUMN args_overridden INTEGER "
               "NOT NULL DEFAULT 0");
      changed = true;
    }
  }
  // v9 -> v10: receiver provenance + per-argument provenance for virtual
  // dispatch. No backfill -- reindex repopulates from the AST.
  if (has_table("edge_site")) {
    const auto escols = table_columns("edge_site");
    if (!has_col(escols, "recv_src_kind")) {
      db_.exec("ALTER TABLE edge_site ADD COLUMN recv_src_kind TEXT");
      db_.exec("ALTER TABLE edge_site ADD COLUMN recv_type_usr TEXT");
      db_.exec("ALTER TABLE edge_site ADD COLUMN recv_decl_usr TEXT");
      changed = true;
    }
    if (!has_col(escols, "recv_param_pos")) {
      db_.exec("ALTER TABLE edge_site ADD COLUMN recv_param_pos INTEGER");
      changed = true;
    }
    if (!has_table("call_arg")) {
      // The call_arg table itself is created by the schema script (CREATE
      // TABLE IF NOT EXISTS), run after migrate(), so we only flip changed
      // to bump the version -- identical to the v6->v7 graph tables pattern.
      changed = true;
    }
    // v10 -> v11: value-ness booleans for exact-singleton Gamma narrowing.
    // No backfill -- reindex repopulates; old rows read as NULL == not-value == TOP.
    if (!has_col(escols, "recv_type_is_value")) {
      db_.exec("ALTER TABLE edge_site ADD COLUMN recv_type_is_value INTEGER");
      changed = true;
    }
  }
  if (has_table("call_arg")) {
    const auto cacols = table_columns("call_arg");
    if (!has_col(cacols, "type_is_value")) {
      db_.exec("ALTER TABLE call_arg ADD COLUMN type_is_value INTEGER");
      changed = true;
    }
  }
  // v6 -> v7: the graph tables are created by the schema script (CREATE TABLE
  // IF NOT EXISTS + INSERT OR IGNORE edge_kind). No symbol/file ALTER is
  // needed, so detect the version from meta and bump it directly. Idempotent.
  if (!has_table("edge")) {
    changed = true; // tables will be created by the schema script
  } else {
    // edge table exists: bump version only when stored version is OLDER
    // (future-schema DBs — version > kSchemaVersion — are left untouched so
    // we do NOT downgrade them).
    auto st =
        db_.prepare("SELECT value FROM meta WHERE key = 'schema_version'");
    if (st.step()) {
      const std::string v = st.col_text(0);
      if (!v.empty() && std::stoi(v) < kSchemaVersion) {
        changed = true; // forces the meta UPDATE below to write the new version
      }
    }
  }
  // v13 -> v14: component.version column + label table.
  // component.version: guard on PRAGMA table_info(component).
  if (has_table("component")) {
    const auto ccols = table_columns("component");
    if (!has_col(ccols, "version")) {
      // No backfill — existing components get version = NULL.
      db_.exec("ALTER TABLE component ADD COLUMN version TEXT");
      changed = true;
    }
  }
  // label table: created by the schema script (CREATE TABLE IF NOT EXISTS).
  // Only flip changed here so the meta UPDATE fires.
  if (!has_table("label")) {
    changed = true; // table will be created by the schema script
  }
  // v22 -> v23: repository + clone tables and component.repository_id. The two
  // tables are created by the schema script (CREATE TABLE IF NOT EXISTS); only
  // the new component column needs an ALTER. No backfill -- existing components
  // stay ungrouped (repository_id NULL) until a re-import / `cidx repo` command.
  if (has_table("component")) {
    const auto ccols2 = table_columns("component");
    if (!has_col(ccols2, "repository_id")) {
      db_.exec("ALTER TABLE component ADD COLUMN repository_id INTEGER");
      changed = true;
    }
  }
  if (!has_table("repository")) {
    changed = true; // table will be created by the schema script
  }
  // v14 -> v15: per-file parse diagnostics. Created by the schema script
  // (CREATE TABLE IF NOT EXISTS); no backfill -- a reindex repopulates it.
  if (!has_table("diagnostic")) {
    changed = true; // table will be created by the schema script
  }
  // v16 -> v17: Layer-1 entity_edge + entity_edge_kind tables. Created by the
  // schema script (CREATE TABLE IF NOT EXISTS). entity_edge is a derived,
  // materialised table -- populate via `cidx resolve`. No backfill on migration.
  if (!has_table("entity_edge")) {
    changed = true; // tables will be created by the schema script
  } else {
    // The `nests` entity_edge kind was removed (lexical nesting is a symbol
    // declaration-scope property, not a relation). Clean the DB in place: drop
    // the defunct nests rows (kind 10) and renumber befriends 11 -> 10 to match
    // the new contiguous seed. Order matters -- delete the old kind-10 rows
    // BEFORE renumbering 11 -> 10 so the two never collide on
    // UNIQUE(src,dst,kind,via). Also drop the stale entity_edge_kind rows so the
    // schema script's INSERT OR IGNORE reseeds (10,'befriends'). Mirrors
    // storage.py.
    //
    // Gate on the STALE DATA (a leftover 'nests' seed row), NOT the schema
    // version: an earlier build bumped schema_version to 18 WITHOUT cleaning, so
    // a version gate would skip those already-stamped DBs. Idempotent -- after
    // cleanup there is no 'nests' row, so it never runs again.
    bool stale = false;
    {
      auto st = db_.prepare(
          "SELECT 1 FROM entity_edge_kind WHERE name = 'nests' LIMIT 1");
      stale = st.step();
    }
    if (stale) {
      db_.exec("DELETE FROM entity_edge WHERE kind = 10");
      db_.exec("UPDATE entity_edge SET kind = 10 WHERE kind = 11");
      db_.exec("DELETE FROM entity_edge_kind WHERE id IN (10, 11)");
      changed = true;
    }
    // Rename kind 2 'realizes' -> 'implements' (display name only; the stored
    // entity_edge.kind int is unchanged). Data-gated on the old name so it fires
    // regardless of schema_version; the schema script's INSERT OR IGNORE would
    // otherwise leave the stale (2,'realizes') row in place. Mirrors storage.py.
    bool renamed = false;
    {
      auto st = db_.prepare(
          "SELECT 1 FROM entity_edge_kind WHERE id = 2 AND name = 'realizes'");
      renamed = st.step();
    }
    if (renamed) {
      db_.exec("UPDATE entity_edge_kind SET name = 'implements' WHERE id = 2");
      changed = true;
    }
    // v20 -> v21: NULL-safe entity_edge identity. The old table-level
    // UNIQUE(src,dst,kind,via_member_id) never collided on NULL-via rows
    // (SQLite NULL != NULL), so every materialise fanned NULL-via edges out into
    // duplicate copies. The schema script now builds a COALESCE unique index
    // idx_entity_edge_identity; it would fail to create over a DB that already
    // carries those duplicates, so dedup in place first (keep the lowest rowid
    // per logical key). Gate on the index's absence so it runs exactly once;
    // entity_edge is derived, so `cidx resolve` repopulates cleanly. Mirrors
    // storage.py.
    bool has_identity_idx = false;
    {
      auto st = db_.prepare(
          "SELECT 1 FROM sqlite_master WHERE type = 'index' "
          "AND name = 'idx_entity_edge_identity'");
      has_identity_idx = st.step();
    }
    if (!has_identity_idx) {
      db_.exec(
          "DELETE FROM entity_edge WHERE rowid NOT IN ("
          "  SELECT MIN(rowid) FROM entity_edge GROUP BY "
          "    src_id, dst_id, kind, "
          "    COALESCE(via_member_id, -1), COALESCE(create_form, -1))");
      changed = true;
    }
  }
  // v21 -> v22: entity_node + entity_kind tables (the entity's design type).
  // The table is created by the schema script (run after migrate); the
  // constructor backfills it from existing symbols right after -- pure-DB, no
  // re-index/resolve. Mirrors storage.py.
  if (!has_table("entity_node")) {
    needs_entity_node_backfill_ = true;
    changed = true;
  }
  if (changed) {
    auto st =
        db_.prepare("UPDATE meta SET value = ? WHERE key = 'schema_version'");
    st.bind(1, std::string_view(std::to_string(kSchemaVersion)));
    st.step_done();
  }
}

// v15 -> v16: rebuild `symbol` with kind stored as its CXCursorKind int.
// SQLite cannot ALTER a column's type or drop the old `kind IN (...)` CHECK, so
// the table is recreated and rows copied with kind names mapped to integers.
// Foreign keys are disabled for the swap so dropping the old table does not
// cascade-delete edges (edge.src_id/dst_id keep the ids the new rows carry).
// The schema script (run right after migrate) recreates the symbol indexes via
// CREATE INDEX IF NOT EXISTS. Mirrors storage.py _migrate_symbol_kind_to_int.
void Storage::migrate_symbol_kind_to_int() {
  std::string cases;
  for (const auto &kv : symbol_kind_ids_map()) {
    cases += " WHEN '" + std::string(kv.first) +
             "' THEN " + std::to_string(kv.second);
  }
  db_.exec("PRAGMA foreign_keys = OFF");
  db_.exec(
      "CREATE TABLE symbol_new ("
      " id INTEGER PRIMARY KEY,"
      " usr TEXT NOT NULL UNIQUE,"
      " spelling TEXT NOT NULL,"
      " qual_name TEXT,"
      " display_name TEXT,"
      " kind INTEGER NOT NULL,"
      " type_info TEXT,"
      " file_id INTEGER REFERENCES file(id) ON DELETE SET NULL,"
      " line INTEGER,"
      " col INTEGER,"
      " decl_file_id INTEGER REFERENCES file(id) ON DELETE SET NULL,"
      " decl_line INTEGER,"
      " decl_col INTEGER,"
      " decl_path TEXT,"
      " is_definition INTEGER NOT NULL DEFAULT 0,"
      " is_pure INTEGER NOT NULL DEFAULT 0,"
      " is_static INTEGER NOT NULL DEFAULT 0,"
      " is_instantiation INTEGER NOT NULL DEFAULT 0,"
      " linkage TEXT,"
      " access TEXT,"
      " parent_usr TEXT,"
      " resolved INTEGER NOT NULL DEFAULT 0"
      ");"
      "INSERT INTO symbol_new"
      " SELECT id, usr, spelling, qual_name, display_name,"
      "        CASE kind" + cases + " ELSE kind END,"
      "        type_info, file_id, line, col, decl_file_id, decl_line,"
      "        decl_col, decl_path, is_definition, is_pure, is_static,"
      "        is_instantiation, linkage, access, parent_usr, resolved"
      " FROM symbol;"
      "DROP TABLE symbol;"
      "ALTER TABLE symbol_new RENAME TO symbol;");
  db_.exec("PRAGMA foreign_keys = ON");
}

// -- components
// ----------------------------------------------------------------

int64_t Storage::add_component(const std::string &name, const std::string &path,
                               const std::string &kind,
                               const std::optional<std::string> &version) {
  // Preserve indirected (portable) paths verbatim; absolutize plain paths.
  // Mirrors Python: if "$" not in path and "<" not in path: path = abspath(path)
  const std::string abs =
      (path.find('$') == std::string::npos &&
       path.find('<') == std::string::npos)
          ? pathutil::abspath(path)
          : path;
  // v14: COALESCE(excluded.version, component.version) so a re-import that
  // supplies NO version (excluded.version bound NULL) PRESERVES the existing
  // stored version instead of silently wiping it (contract §4.2).
  auto st = db_.prepare(
      "INSERT INTO component (name, path, kind, version) VALUES (?, ?, ?, ?) "
      "ON CONFLICT(path) DO UPDATE SET name = excluded.name, kind = "
      "excluded.kind, version = COALESCE(excluded.version, component.version) "
      "RETURNING id");
  st.bind(1, std::string_view(name));
  st.bind(2, std::string_view(abs));
  st.bind(3, std::string_view(kind));
  bind_opt(st, 4, version);
  if (!st.step()) {
    throw StorageError("component upsert returned no id");
  }
  const int64_t cid = st.col_int64(0);
  st.step_done();
  return cid;
}

bool Storage::set_component_version(const std::string &name,
                                    const std::optional<std::string> &version) {
  // Explicit clear goes through this path (not add_component) to avoid the
  // COALESCE guard that would no-op a NULL-clear.
  auto st = db_.prepare(
      "UPDATE component SET version = ? WHERE name = ?");
  bind_opt(st, 1, version);
  st.bind(2, std::string_view(name));
  st.step_done();
  return db_.changes() > 0;
}

bool Storage::set_component_effective_version(const std::string &name,
                                              const std::string &version) {
  // Mirrors Python Storage.set_component_effective_version: only act when the
  // name resolves to exactly one row; pick property-vs-embedded by splitting
  // the STORED path (so portable <label>/$VAR prefixes survive the rewrite).
  std::vector<Component> rows;
  for (const auto &c : list_components()) {
    if (c.name == name) {
      rows.push_back(c);
    }
  }
  if (rows.size() != 1) {
    return false;
  }
  const Component &comp = rows.front();
  const auto [base, seg] = CompileDb::split_base_version(comp.path);
  if (!seg.empty()) {
    // version embedded in the path: swap the trailing segment.
    std::string new_path = pathutil::normpath(pathutil::join(base, version));
    if (new_path.find('$') == std::string::npos &&
        new_path.find('<') == std::string::npos) {
      new_path = pathutil::abspath(new_path);
    }
    auto st = db_.prepare(
        "UPDATE component SET path = ?, version = NULL WHERE id = ?");
    st.bind(1, std::string_view(new_path));
    st.bind(2, comp.id);
    st.step_done();
  } else {
    auto st = db_.prepare("UPDATE component SET version = ? WHERE id = ?");
    st.bind(1, std::string_view(version));
    st.bind(2, comp.id);
    st.step_done();
  }
  return true;
}

// static
std::string Storage::effective_root(const Component &comp) {
  // Stored effective root (NOT resolved; may contain $VAR).
  if (!comp.version || comp.version->empty()) {
    return comp.path;
  }
  return pathutil::normpath(pathutil::join(comp.path, *comp.version));
}

std::optional<Component> Storage::get_component(const std::string &path) {
  const std::string abs = pathutil::abspath(path);
  // Step 1: exact match on stored BASE path (fast-path for unversioned comps).
  {
    auto st = db_.prepare(std::string("SELECT ") + kComponentCols +
                          " FROM component WHERE path = ?");
    st.bind(1, std::string_view(abs));
    if (st.step()) {
      return component_from(st);
    }
  }
  // Step 2: scan all components and match against effective root (required
  // when version-detection split the trailing segment off the stored base).
  {
    auto st =
        db_.prepare(std::string("SELECT ") + kComponentCols + " FROM component");
    while (st.step()) {
      Component c = component_from(st);
      const std::string root =
          pathutil::abspath(pathutil::resolve_fs_path(effective_root(c)));
      if (root == abs) {
        return c;
      }
    }
  }
  return std::nullopt;
}

std::optional<Component>
Storage::get_component_by_name(const std::string &name) {
  auto st = db_.prepare(std::string("SELECT ") + kComponentCols +
                        " FROM component WHERE name = ?");
  st.bind(1, std::string_view(name));
  if (!st.step()) {
    return std::nullopt;
  }
  return component_from(st);
}

std::optional<Component> Storage::get_component_by_id(int64_t component_id) {
  auto st = db_.prepare(std::string("SELECT ") + kComponentCols +
                        " FROM component WHERE id = ?");
  st.bind(1, component_id);
  if (!st.step()) {
    return std::nullopt;
  }
  return component_from(st);
}

std::optional<Component>
Storage::component_for_path(const std::string &abs_path) {
  const std::string abs = pathutil::abspath(abs_path);
  std::optional<Component> best;
  std::string best_root;
  auto st =
      db_.prepare(std::string("SELECT ") + kComponentCols + " FROM component");
  while (st.step()) {
    Component c = component_from(st);
    // Use the resolved effective root (base+version) for prefix matching
    // (contract §4.4 item 1 / §2 hazard). Strip trailing slashes.
    std::string root =
        pathutil::abspath(pathutil::resolve_fs_path(effective_root(c)));
    while (!root.empty() && root.back() == '/') {
      root.pop_back(); // rstrip(os.sep)
    }
    const bool owns = abs == root || abs.starts_with(root + "/");
    if (owns && (!best || root.size() > best_root.size())) {
      best = std::move(c);
      best_root = root;
    }
  }
  return best;
}

void Storage::delete_component(int64_t component_id) {
  // Symbols reference files with ON DELETE SET NULL, so remove them before the
  // cascade nulls their file ids -- otherwise they linger as file-less orphans.
  // Directories and files then vanish via the component's ON DELETE CASCADE.
  static const char kSub[] =
      "SELECT f.id FROM file f JOIN directory d ON f.directory_id = d.id "
      "WHERE d.component_id = ?";
  auto del_sym =
      db_.prepare(std::string("DELETE FROM symbol WHERE file_id IN (") + kSub +
                  ") OR decl_file_id IN (" + kSub + ")");
  del_sym.bind(1, component_id);
  del_sym.bind(2, component_id);
  del_sym.step_done();
  auto del_comp = db_.prepare("DELETE FROM component WHERE id = ?");
  del_comp.bind(1, component_id);
  del_comp.step_done();
}

void Storage::delete_directory(int64_t directory_id) {
  // Files cascade on directory delete; symbols (ON DELETE SET NULL) would
  // linger file-less, so delete them first.
  static const char kSub[] = "SELECT id FROM file WHERE directory_id = ?";
  auto del_sym =
      db_.prepare(std::string("DELETE FROM symbol WHERE file_id IN (") + kSub +
                  ") OR decl_file_id IN (" + kSub + ")");
  del_sym.bind(1, directory_id);
  del_sym.bind(2, directory_id);
  del_sym.step_done();
  auto del_dir = db_.prepare("DELETE FROM directory WHERE id = ?");
  del_dir.bind(1, directory_id);
  del_dir.step_done();
}

void Storage::delete_file(int64_t file_id) {
  // Symbols reference files with ON DELETE SET NULL; delete them first so they
  // do not linger file-less.
  auto del_sym =
      db_.prepare("DELETE FROM symbol WHERE file_id = ? OR decl_file_id = ?");
  del_sym.bind(1, file_id);
  del_sym.bind(2, file_id);
  del_sym.step_done();
  auto del_file = db_.prepare("DELETE FROM file WHERE id = ?");
  del_file.bind(1, file_id);
  del_file.step_done();
}

void Storage::delete_symbol(int64_t symbol_id) {
  auto del = db_.prepare("DELETE FROM symbol WHERE id = ?");
  del.bind(1, symbol_id);
  del.step_done();
}

std::vector<Component>
Storage::list_components(const std::optional<std::string> &name,
                         const std::optional<std::string> &kind) {
  std::string sql = std::string("SELECT ") + kComponentCols + " FROM component";
  std::vector<std::string> where;
  std::vector<SqlValue> args;
  if (name && !name->empty()) {
    where.push_back("name LIKE ? ESCAPE '\\'");
    args.emplace_back(fuzzy_like(*name));
  }
  if (kind) {
    where.push_back("kind = ?");
    args.emplace_back(*kind);
  }
  if (!where.empty()) {
    sql += " WHERE " + join_strings(where, " AND ");
  }
  sql += " ORDER BY name, path";
  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < args.size(); ++i) {
    st.bind(static_cast<int>(i + 1), args[i]);
  }
  std::vector<Component> out;
  while (st.step()) {
    out.push_back(component_from(st));
  }
  return out;
}

void Storage::set_component_repository(
    int64_t component_id, const std::optional<int64_t> &repository_id) {
  auto st = db_.prepare("UPDATE component SET repository_id = ? WHERE id = ?");
  if (repository_id) {
    st.bind(1, *repository_id);
  } else {
    st.bind_null(1);
  }
  st.bind(2, component_id);
  st.step_done();
}

std::vector<Component>
Storage::components_for_repository(int64_t repository_id) {
  auto st = db_.prepare(std::string("SELECT ") + kComponentCols +
                        " FROM component WHERE repository_id = ? "
                        "ORDER BY name, path");
  st.bind(1, repository_id);
  std::vector<Component> out;
  while (st.step()) {
    out.push_back(component_from(st));
  }
  return out;
}

// -- repositories / clones (v23)
// -----------------------------------------------------------------

int64_t Storage::add_repository(const std::string &name,
                                const std::string &kind,
                                const std::optional<std::string> &remote_url) {
  auto st = db_.prepare(
      "INSERT INTO repository (name, kind, remote_url) VALUES (?, ?, ?) "
      "ON CONFLICT(name) DO UPDATE SET kind = excluded.kind, "
      "remote_url = COALESCE(excluded.remote_url, repository.remote_url) "
      "RETURNING id");
  st.bind(1, std::string_view(name));
  st.bind(2, std::string_view(kind));
  bind_opt(st, 3, remote_url);
  if (!st.step()) {
    throw StorageError("repository upsert returned no id");
  }
  const int64_t rid = st.col_int64(0);
  st.step_done();
  return rid;
}

std::optional<Repository>
Storage::get_repository_by_name(const std::string &name) {
  auto st = db_.prepare(std::string("SELECT ") + kRepositoryCols +
                        " FROM repository WHERE name = ?");
  st.bind(1, std::string_view(name));
  if (!st.step()) {
    return std::nullopt;
  }
  return repository_from(st);
}

std::optional<Repository> Storage::get_repository_by_id(int64_t repository_id) {
  auto st = db_.prepare(std::string("SELECT ") + kRepositoryCols +
                        " FROM repository WHERE id = ?");
  st.bind(1, repository_id);
  if (!st.step()) {
    return std::nullopt;
  }
  return repository_from(st);
}

std::optional<Repository>
Storage::get_repository_by_remote(const std::string &remote_url) {
  auto st = db_.prepare(std::string("SELECT ") + kRepositoryCols +
                        " FROM repository WHERE remote_url = ? "
                        "ORDER BY id LIMIT 1");
  st.bind(1, std::string_view(remote_url));
  if (!st.step()) {
    return std::nullopt;
  }
  return repository_from(st);
}

std::vector<Repository>
Storage::list_repositories(const std::optional<std::string> &name,
                           const std::optional<std::string> &kind) {
  std::string sql =
      std::string("SELECT ") + kRepositoryCols + " FROM repository";
  std::vector<std::string> where;
  std::vector<SqlValue> args;
  if (name && !name->empty()) {
    where.push_back("name LIKE ? ESCAPE '\\'");
    args.emplace_back(fuzzy_like(*name));
  }
  if (kind) {
    where.push_back("kind = ?");
    args.emplace_back(*kind);
  }
  if (!where.empty()) {
    sql += " WHERE " + join_strings(where, " AND ");
  }
  sql += " ORDER BY name";
  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < args.size(); ++i) {
    st.bind(static_cast<int>(i + 1), args[i]);
  }
  std::vector<Repository> out;
  while (st.step()) {
    out.push_back(repository_from(st));
  }
  return out;
}

void Storage::set_active_clone(int64_t repository_id,
                               const std::optional<int64_t> &clone_id) {
  auto st =
      db_.prepare("UPDATE repository SET active_clone_id = ? WHERE id = ?");
  if (clone_id) {
    st.bind(1, *clone_id);
  } else {
    st.bind_null(1);
  }
  st.bind(2, repository_id);
  st.step_done();
}

void Storage::delete_repository(int64_t repository_id) {
  auto st = db_.prepare("DELETE FROM repository WHERE id = ?");
  st.bind(1, repository_id);
  st.step_done();
}

int64_t Storage::add_clone(int64_t repository_id, const std::string &path,
                           const std::optional<std::string> &label) {
  // Mirror Python: absolutize plain paths; preserve portable ($/<) verbatim.
  const std::string abs =
      (path.find('$') == std::string::npos &&
       path.find('<') == std::string::npos)
          ? pathutil::abspath(path)
          : path;
  auto st = db_.prepare(
      "INSERT INTO clone (repository_id, path, label) VALUES (?, ?, ?) "
      "ON CONFLICT(path) DO UPDATE SET repository_id = excluded.repository_id, "
      "label = COALESCE(excluded.label, clone.label) RETURNING id");
  st.bind(1, repository_id);
  st.bind(2, std::string_view(abs));
  bind_opt(st, 3, label);
  if (!st.step()) {
    throw StorageError("clone upsert returned no id");
  }
  const int64_t cid = st.col_int64(0);
  st.step_done();
  return cid;
}

std::optional<Clone> Storage::get_clone_by_id(int64_t clone_id) {
  auto st = db_.prepare(std::string("SELECT ") + kCloneCols +
                        " FROM clone WHERE id = ?");
  st.bind(1, clone_id);
  if (!st.step()) {
    return std::nullopt;
  }
  return clone_from(st);
}

std::optional<Clone> Storage::get_clone_by_path(const std::string &path) {
  const std::string abs =
      (path.find('$') == std::string::npos &&
       path.find('<') == std::string::npos)
          ? pathutil::abspath(path)
          : path;
  auto st = db_.prepare(std::string("SELECT ") + kCloneCols +
                        " FROM clone WHERE path = ?");
  st.bind(1, std::string_view(abs));
  if (!st.step()) {
    return std::nullopt;
  }
  return clone_from(st);
}

std::vector<Clone>
Storage::list_clones(const std::optional<int64_t> &repository_id) {
  std::string sql = std::string("SELECT ") + kCloneCols + " FROM clone";
  if (repository_id) {
    sql += " WHERE repository_id = ?";
  }
  sql += " ORDER BY id";
  auto st = db_.prepare(sql);
  if (repository_id) {
    st.bind(1, *repository_id);
  }
  std::vector<Clone> out;
  while (st.step()) {
    out.push_back(clone_from(st));
  }
  return out;
}

void Storage::delete_clone(int64_t clone_id) {
  auto clr = db_.prepare("UPDATE repository SET active_clone_id = NULL "
                         "WHERE active_clone_id = ?");
  clr.bind(1, clone_id);
  clr.step_done();
  auto del = db_.prepare("DELETE FROM clone WHERE id = ?");
  del.bind(1, clone_id);
  del.step_done();
}

int64_t Storage::rebase_components(int64_t repository_id,
                                   const std::string &old_root,
                                   const std::string &new_root) {
  std::string oldr = pathutil::abspath(old_root);
  std::string newr = pathutil::abspath(new_root);
  while (!oldr.empty() && oldr.back() == '/') {
    oldr.pop_back();
  }
  while (!newr.empty() && newr.back() == '/') {
    newr.pop_back();
  }
  if (oldr == newr) {
    return 0;
  }
  int64_t n = 0;
  for (const Component &comp : components_for_repository(repository_id)) {
    const std::string &p = comp.path;
    if (p.find('<') != std::string::npos || p.find('$') != std::string::npos) {
      continue; // portable path: clone-root agnostic already
    }
    std::string new_path;
    if (p == oldr) {
      new_path = newr;
    } else if (p.starts_with(oldr + "/")) {
      new_path = newr + p.substr(oldr.size());
    } else {
      continue;
    }
    auto upd = db_.prepare("UPDATE component SET path = ? WHERE id = ?");
    upd.bind(1, std::string_view(new_path));
    upd.bind(2, comp.id);
    upd.step_done();
    ++n;
  }
  return n;
}

// -- directories
// -----------------------------------------------------------------

int64_t Storage::add_directory(int64_t component_id, const std::string &path) {
  std::string p = path.empty() ? std::string(".") : pathutil::normpath(path);
  if (p == ".") {
    p = "";
  }
  auto st = db_.prepare(
      "INSERT INTO directory (component_id, path) VALUES (?, ?) "
      "ON CONFLICT(component_id, path) DO UPDATE SET path = excluded.path "
      "RETURNING id");
  st.bind(1, component_id);
  st.bind(2, std::string_view(p));
  if (!st.step()) {
    throw StorageError("directory upsert returned no id");
  }
  const int64_t did = st.col_int64(0);
  st.step_done();
  return did;
}

std::optional<Directory> Storage::get_directory(int64_t component_id,
                                                const std::string &path) {
  const std::string p =
      (path.empty() || path == ".") ? std::string() : pathutil::normpath(path);
  auto st = db_.prepare(std::string("SELECT ") + kDirectoryCols +
                        " FROM directory WHERE component_id = ? AND path = ?");
  st.bind(1, component_id);
  st.bind(2, std::string_view(p));
  if (!st.step()) {
    return std::nullopt;
  }
  return directory_from(st);
}

std::optional<Directory> Storage::get_directory_by_id(int64_t directory_id) {
  auto st = db_.prepare(std::string("SELECT ") + kDirectoryCols +
                        " FROM directory WHERE id = ?");
  st.bind(1, directory_id);
  if (!st.step()) {
    return std::nullopt;
  }
  return directory_from(st);
}

std::vector<std::pair<Directory, std::string>>
Storage::list_directories(const std::optional<int64_t> &component_id,
                          const std::optional<std::string> &name) {
  std::string sql =
      "SELECT d.id, d.component_id, d.path, c.name AS comp_name "
      "FROM directory d JOIN component c ON c.id = d.component_id";
  std::vector<std::string> where;
  std::vector<SqlValue> args;
  if (component_id) {
    where.push_back("d.component_id = ?");
    args.emplace_back(*component_id);
  }
  if (name && !name->empty()) {
    where.push_back("d.path LIKE ? ESCAPE '\\'");
    args.emplace_back(fuzzy_like(*name));
  }
  if (!where.empty()) {
    sql += " WHERE " + join_strings(where, " AND ");
  }
  sql += " ORDER BY c.name, d.path";
  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < args.size(); ++i) {
    st.bind(static_cast<int>(i + 1), args[i]);
  }
  std::vector<std::pair<Directory, std::string>> out;
  while (st.step()) {
    out.emplace_back(directory_from(st), st.col_text(3));
  }
  return out;
}

std::string Storage::dir_scope_sql(const std::string &dir_path,
                                   std::vector<SqlValue> &args) {
  std::string rel = pathutil::normpath(dir_path);
  if (rel == "." || rel.empty()) {
    rel = "";
  }
  const std::string esc = escape_like(rel);
  args.emplace_back(rel);
  // '' is the component root: its subtree is every directory.
  args.emplace_back(rel.empty() ? std::string("%") : esc + "/%");
  return "(d.path = ? OR d.path LIKE ? ESCAPE '\\')";
}

// -- files
// -------------------------------------------------------------------------

int64_t Storage::add_file(
    int64_t directory_id, const std::string &name,
    const std::optional<double> &mtime, const std::optional<std::string> &md5,
    const std::optional<std::vector<std::string>> &compile_options,
    const std::optional<std::string> &driver) {
  std::optional<std::string> opts;
  if (compile_options) {
    opts = json_min::encode_string_array(*compile_options);
  }
  auto st =
      db_.prepare("INSERT INTO file (directory_id, name, mtime, md5, "
                  "compile_options, driver) "
                  "VALUES (?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT(directory_id, name) DO UPDATE SET "
                  "  mtime           = COALESCE(excluded.mtime, file.mtime), "
                  "  compile_options = CASE WHEN file.args_overridden = 1 "
                  "                         THEN file.compile_options "
                  "                         ELSE COALESCE(excluded.compile_options, "
                  "file.compile_options) END, "
                  "  driver          = CASE WHEN file.args_overridden = 1 "
                  "                         THEN file.driver "
                  "                         ELSE COALESCE(excluded.driver, "
                  "file.driver) END, "
                  "  indexed         = CASE WHEN excluded.md5 IS NOT NULL "
                  "                          AND excluded.md5 IS NOT file.md5 "
                  "                         THEN 0 ELSE file.indexed END, "
                  "  md5             = COALESCE(excluded.md5, file.md5) "
                  "RETURNING id");
  st.bind(1, directory_id);
  st.bind(2, std::string_view(name));
  bind_opt(st, 3, mtime);
  bind_opt(st, 4, md5);
  bind_opt(st, 5, opts);
  bind_opt(st, 6, driver);
  if (!st.step()) {
    throw StorageError("file upsert returned no id");
  }
  const int64_t fid = st.col_int64(0);
  st.step_done();
  return fid;
}

std::optional<std::tuple<int64_t, std::string, std::string>>
Storage::split_path(const std::string &abs_path) {
  const std::string abs = pathutil::abspath(abs_path);
  const auto comp = component_for_path(abs);
  if (!comp) {
    return std::nullopt;
  }
  // Use the resolved effective root (base+version) for relpath computation
  // (contract §4.4 item 2).
  const std::string root =
      pathutil::abspath(pathutil::resolve_fs_path(effective_root(*comp)));
  const std::string rel = pathutil::relpath(abs, root);
  auto [rel_dir, name] = pathutil::split(rel);
  if (rel_dir == ".") {
    rel_dir = "";
  }
  return std::make_tuple(comp->id, rel_dir, name);
}

int64_t Storage::add_file_path(
    const std::string &abs_path, const std::optional<double> &mtime,
    const std::optional<std::string> &md5,
    const std::optional<std::vector<std::string>> &compile_options,
    const std::optional<std::string> &driver) {
  const auto sp = split_path(abs_path);
  if (!sp) {
    throw StorageError("no component owns " + pathutil::abspath(abs_path) +
                       " (add_component first)");
  }
  const auto &[comp_id, rel_dir, name] = *sp;
  const int64_t dir_id = add_directory(comp_id, rel_dir);
  return add_file(dir_id, name, mtime, md5, compile_options, driver);
}

std::optional<File> Storage::get_file(const std::string &abs_path) {
  const auto sp = split_path(abs_path);
  if (!sp) {
    return std::nullopt;
  }
  const auto &[comp_id, rel_dir, name] = *sp;
  auto st = db_.prepare(
      "SELECT f.id, f.directory_id, f.name, f.mtime, f.md5, f.compile_options, "
      "f.driver, f.indexed, f.indexed_at, f.args_overridden "
      "FROM file f JOIN directory d ON d.id = f.directory_id "
      "WHERE d.component_id = ? AND d.path = ? AND f.name = ?");
  st.bind(1, comp_id);
  st.bind(2, std::string_view(rel_dir));
  st.bind(3, std::string_view(name));
  if (!st.step()) {
    return std::nullopt;
  }
  return file_from(st);
}

std::optional<File> Storage::get_file_by_id(int64_t file_id) {
  auto st = db_.prepare(std::string("SELECT ") + kFileCols +
                        " FROM file WHERE id = ?");
  st.bind(1, file_id);
  if (!st.step()) {
    return std::nullopt;
  }
  return file_from(st);
}

std::optional<std::string> Storage::file_abs_path(int64_t file_id) {
  // SELECT c.path AND c.version so we can build the effective root.
  auto st = db_.prepare(
      "SELECT c.path, c.version, d.path AS rel, f.name AS name "
      "FROM file f JOIN directory d ON d.id = f.directory_id "
      "JOIN component c ON c.id = d.component_id WHERE f.id = ?");
  st.bind(1, file_id);
  if (!st.step()) {
    return std::nullopt;
  }
  // Build effective root using stored path + version (contract §4.4 item 3).
  Component tmp;
  tmp.path = st.col_text(0);
  tmp.version = opt_text(st, 1);
  const std::string root =
      pathutil::abspath(pathutil::resolve_fs_path(effective_root(tmp)));
  return reconstruct_path(root, st.col_text(2), st.col_text(3));
}

std::optional<std::string> Storage::directory_abs_path(int64_t directory_id) {
  auto st = db_.prepare(
      "SELECT c.path, c.version, d.path AS rel FROM directory d "
      "JOIN component c ON c.id = d.component_id WHERE d.id = ?");
  st.bind(1, directory_id);
  if (!st.step()) {
    return std::nullopt;
  }
  Component tmp;
  tmp.path = st.col_text(0);
  tmp.version = opt_text(st, 1);
  const std::string root =
      pathutil::abspath(pathutil::resolve_fs_path(effective_root(tmp)));
  const std::string rel = st.col_text(2);
  return rel.empty() ? root : pathutil::join(root, rel);
}

std::vector<std::pair<File, std::string>>
Storage::list_files(const std::optional<int64_t> &component_id,
                    const std::optional<std::string> &dir_path,
                    const std::optional<std::string> &name,
                    const std::optional<bool> &indexed) {
  std::string sql =
      "SELECT f.id, f.directory_id, f.name, f.mtime, f.md5, f.compile_options, "
      "f.driver, f.indexed, f.indexed_at, f.args_overridden, "
      "c.path AS root, c.version, d.path AS rel "
      "FROM file f JOIN directory d ON d.id = f.directory_id "
      "JOIN component c ON c.id = d.component_id";
  std::vector<std::string> where;
  std::vector<SqlValue> args;
  if (component_id) {
    where.push_back("d.component_id = ?");
    args.emplace_back(*component_id);
  }
  if (dir_path) {
    where.push_back(dir_scope_sql(*dir_path, args));
  }
  if (name && !name->empty()) {
    where.push_back("f.name LIKE ? ESCAPE '\\'");
    args.emplace_back(fuzzy_like(*name));
  }
  if (indexed) {
    where.push_back("f.indexed = ?");
    args.emplace_back(static_cast<int64_t>(*indexed ? 1 : 0));
  }
  if (!where.empty()) {
    sql += " WHERE " + join_strings(where, " AND ");
  }
  sql += " ORDER BY c.path, d.path, f.name";
  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < args.size(); ++i) {
    st.bind(static_cast<int>(i + 1), args[i]);
  }
  std::vector<std::pair<File, std::string>> out;
  while (st.step()) {
    // Columns 10=c.path, 11=c.version, 12=d.path (rel); f.name is col 2.
    Component tmp;
    tmp.path = st.col_text(10);
    tmp.version = opt_text(st, 11);
    const std::string root =
        pathutil::abspath(pathutil::resolve_fs_path(effective_root(tmp)));
    out.emplace_back(file_from(st),
                     reconstruct_path(root, st.col_text(12), st.col_text(2)));
  }
  return out;
}

void Storage::mark_file_indexed(int64_t file_id,
                                const std::optional<double> &mtime) {
  auto st =
      db_.prepare("UPDATE file SET indexed = 1, indexed_at = datetime('now'), "
                  "  mtime = COALESCE(?, mtime) WHERE id = ?");
  bind_opt(st, 1, mtime);
  st.bind(2, file_id);
  st.step_done();
}

void Storage::set_file_indexed(int64_t file_id, bool indexed) {
  // Flip the indexed/pending flag in place; symbols are untouched. Setting
  // indexed=0 marks the file pending so the next `index` re-parses it
  // (regenerating graph edges) without losing its existing symbols.
  auto st = db_.prepare("UPDATE file SET indexed = ? WHERE id = ?");
  st.bind(1, static_cast<int64_t>(indexed ? 1 : 0));
  st.bind(2, file_id);
  st.step_done();
}

void Storage::set_file_compile_options(
    int64_t file_id, const std::vector<std::string> &options,
    const std::optional<std::string> &driver, bool update_driver) {
  // Replace a file's stored flags (and optionally its driver) and mark it
  // args_overridden=1 so a later `import` (without --force) keeps the edit.
  const std::string opts = json_min::encode_string_array(options);
  if (update_driver) {
    auto st = db_.prepare("UPDATE file SET compile_options = ?, driver = ?, "
                          "args_overridden = 1 WHERE id = ?");
    st.bind(1, std::string_view(opts));
    bind_opt(st, 2, driver);
    st.bind(3, file_id);
    st.step_done();
  } else {
    auto st = db_.prepare("UPDATE file SET compile_options = ?, "
                          "args_overridden = 1 WHERE id = ?");
    st.bind(1, std::string_view(opts));
    st.bind(2, file_id);
    st.step_done();
  }
}

void Storage::update_file_compile_options(
    int64_t file_id, const std::vector<std::string> &options) {
  // UPDATE compile_options WITHOUT setting args_overridden (realias semantics).
  // Port of storage.py update_file_compile_options.
  const std::string opts = json_min::encode_string_array(options);
  auto st =
      db_.prepare("UPDATE file SET compile_options = ? WHERE id = ?");
  st.bind(1, std::string_view(opts));
  st.bind(2, file_id);
  st.step_done();
}

bool Storage::is_file_indexed(const std::string &abs_path,
                              const std::optional<double> &mtime,
                              const std::optional<std::string> &md5) {
  const auto f = get_file(abs_path);
  if (!f || !f->indexed) {
    return false;
  }
  if (mtime && (!f->mtime || *f->mtime < *mtime)) {
    return false;
  }
  if (md5 && f->md5 != md5) {
    return false;
  }
  return true;
}

// -- diagnostics (v15)
// ----------------------------------------------------------------------

void Storage::replace_diagnostics(int64_t file_id,
                                  const std::vector<Diagnostic> &diags) {
  // Wholesale refresh: drop the file's stale rows, then insert in TU order so
  // ids follow the diagnostic sequence (parity with storage.py).
  {
    auto del = db_.prepare("DELETE FROM diagnostic WHERE file_id = ?");
    del.bind(1, file_id);
    del.step_done();
  }
  for (const Diagnostic &d : diags) {
    auto st = db_.prepare(
        "INSERT INTO diagnostic "
        "(file_id, severity, spelling, file_path, line, col) "
        "VALUES (?, ?, ?, ?, ?, ?)");
    st.bind(1, file_id);
    st.bind(2, static_cast<int64_t>(d.severity));
    st.bind(3, std::string_view(d.spelling));
    bind_opt(st, 4, d.file_path);
    bind_opt(st, 5, d.line);
    bind_opt(st, 6, d.col);
    st.step_done();
  }
}

std::vector<Diagnostic> Storage::get_diagnostics(int64_t file_id) {
  auto st = db_.prepare(
      "SELECT id, file_id, severity, spelling, file_path, line, col "
      "FROM diagnostic WHERE file_id = ? ORDER BY id");
  st.bind(1, file_id);
  std::vector<Diagnostic> out;
  while (st.step()) {
    Diagnostic d;
    d.id = st.col_int64(0);
    d.file_id = st.col_int64(1);
    d.severity = static_cast<int>(st.col_int64(2));
    d.spelling = st.col_text(3);
    d.file_path = opt_text(st, 4);
    d.line = opt_int64(st, 5);
    d.col = opt_int64(st, 6);
    out.push_back(std::move(d));
  }
  return out;
}

std::map<int64_t, std::map<int, int64_t>> Storage::diagnostic_counts() {
  auto st = db_.prepare("SELECT file_id, severity, COUNT(*) FROM diagnostic "
                        "GROUP BY file_id, severity");
  std::map<int64_t, std::map<int, int64_t>> out;
  while (st.step()) {
    const int64_t fid = st.col_int64(0);
    const int sev = static_cast<int>(st.col_int64(1));
    out[fid][sev] = st.col_int64(2);
  }
  return out;
}

// -- symbols
// ----------------------------------------------------------------------

int64_t Storage::add_symbol(const Symbol &sym) {
  if (!is_symbol_kind(sym.kind)) {
    throw StorageError("unknown symbol kind '" + sym.kind + "'");
  }
  auto st = db_.prepare(
      "INSERT INTO symbol (usr, spelling, qual_name, display_name, kind, "
      "type_info, file_id, line, col, decl_file_id, decl_line, decl_col, "
      "is_definition, is_pure, is_static, is_instantiation, linkage, access, "
      "parent_usr, resolved, decl_path) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
      "ON CONFLICT(usr) DO UPDATE SET "
      "  spelling         = excluded.spelling, "
      "  qual_name        = COALESCE(excluded.qual_name, symbol.qual_name), "
      "  display_name     = COALESCE(excluded.display_name, symbol.display_name), "
      "  kind             = excluded.kind, "
      "  type_info        = COALESCE(excluded.type_info, symbol.type_info), "
      "  file_id          = CASE WHEN excluded.is_definition >= "
      "symbol.is_definition "
      "                          THEN excluded.file_id ELSE symbol.file_id END, "
      "  line             = CASE WHEN excluded.is_definition >= "
      "symbol.is_definition "
      "                          THEN excluded.line ELSE symbol.line END, "
      "  col              = CASE WHEN excluded.is_definition >= "
      "symbol.is_definition "
      "                          THEN excluded.col ELSE symbol.col END, "
      "  decl_file_id     = COALESCE(excluded.decl_file_id, symbol.decl_file_id), "
      "  decl_line        = COALESCE(excluded.decl_line, symbol.decl_line), "
      "  decl_col         = COALESCE(excluded.decl_col, symbol.decl_col), "
      "  is_definition    = MAX(excluded.is_definition, symbol.is_definition), "
      "  is_pure          = MAX(excluded.is_pure, symbol.is_pure), "
      "  is_static        = MAX(excluded.is_static, symbol.is_static), "
      "  is_instantiation = MAX(excluded.is_instantiation, symbol.is_instantiation), "
      "  linkage          = COALESCE(excluded.linkage, symbol.linkage), "
      "  access           = COALESCE(excluded.access, symbol.access), "
      "  parent_usr       = COALESCE(excluded.parent_usr, symbol.parent_usr), "
      "  resolved         = MAX(excluded.resolved, symbol.resolved) "
      "RETURNING id");
  st.bind(1, std::string_view(sym.usr));
  st.bind(2, std::string_view(sym.spelling));
  bind_opt(st, 3, sym.qual_name);
  bind_opt(st, 4, sym.display_name);
  st.bind(5, symbol_kind_id(sym.kind)); // stored as CXCursorKind int (v16)
  bind_opt(st, 6, sym.type_info);
  bind_opt(st, 7, sym.file_id);
  bind_opt(st, 8, sym.line);
  bind_opt(st, 9, sym.col);
  bind_opt(st, 10, sym.decl_file_id);
  bind_opt(st, 11, sym.decl_line);
  bind_opt(st, 12, sym.decl_col);
  st.bind(13, static_cast<int64_t>(sym.is_definition ? 1 : 0));
  st.bind(14, static_cast<int64_t>(sym.is_pure ? 1 : 0));
  st.bind(15, static_cast<int64_t>(sym.is_static ? 1 : 0));
  st.bind(16, static_cast<int64_t>(sym.is_instantiation ? 1 : 0));
  bind_opt(st, 17, sym.linkage);
  bind_opt(st, 18, sym.access);
  bind_opt(st, 19, sym.parent_usr);
  st.bind(20, static_cast<int64_t>(sym.resolved ? 1 : 0));
  // decl_path is INSERTed but intentionally NOT in the ON CONFLICT SET: a real
  // add_symbol never clobbers a stub's recorded external path (mirrors Python).
  bind_opt(st, 21, sym.decl_path);
  if (!st.step()) {
    throw StorageError("symbol upsert returned no id");
  }
  const int64_t sid = st.col_int64(0);
  st.step_done();
  return sid;
}

bool Storage::update_symbol(
    const std::string &usr,
    const std::vector<std::pair<std::string, SqlValue>> &values) {
  std::vector<std::string> bad;
  for (const auto &kv : values) {
    if (std::find(kSymbolInsertCols.begin(), kSymbolInsertCols.end(),
                  kv.first) == kSymbolInsertCols.end()) {
      bad.push_back(kv.first);
    }
  }
  if (!bad.empty()) {
    // Dedupe then sort, then format as Python's list repr: ['col1', 'col2']
    // (Python: raises f"unknown symbol column(s): {sorted(set(bad))}")
    std::sort(bad.begin(), bad.end());
    bad.erase(std::unique(bad.begin(), bad.end()), bad.end());
    std::string repr = "[";
    for (std::size_t i = 0; i < bad.size(); ++i) {
      if (i > 0) {
        repr += ", ";
      }
      repr += '\'' + bad[i] + '\'';
    }
    repr += ']';
    throw StorageError("unknown symbol column(s): " + repr);
  }
  for (const auto &kv : values) {
    if (kv.first == "kind") {
      const auto *k = std::get_if<std::string>(&kv.second);
      if (k == nullptr || !is_symbol_kind(*k)) {
        throw StorageError("unknown symbol kind in update_symbol");
      }
    }
  }
  if (values.empty()) {
    return lookup_symbol(usr).has_value();
  }
  std::vector<std::string> sets;
  sets.reserve(values.size());
  for (const auto &kv : values) {
    sets.push_back(kv.first + " = ?");
  }
  auto st = db_.prepare("UPDATE symbol SET " + join_strings(sets, ", ") +
                        " WHERE usr = ?");
  int idx = 1;
  for (const auto &kv : values) {
    st.bind(idx++, kv.second);
  }
  st.bind(idx, std::string_view(usr));
  st.step_done();
  return db_.changes() > 0;
}

std::optional<Symbol> Storage::lookup_symbol(const std::string &usr) {
  auto st = db_.prepare(std::string("SELECT ") + kSymbolCols +
                        " FROM symbol WHERE usr = ?");
  st.bind(1, std::string_view(usr));
  if (!st.step()) {
    return std::nullopt;
  }
  return symbol_from(st);
}

std::optional<Symbol> Storage::lookup_symbol_by_id(int64_t symbol_id) {
  auto st = db_.prepare(std::string("SELECT ") + kSymbolCols +
                        " FROM symbol WHERE id = ?");
  st.bind(1, symbol_id);
  if (!st.step()) {
    return std::nullopt;
  }
  return symbol_from(st);
}

std::vector<Symbol>
Storage::lookup_symbols_by_name(const std::string &spelling,
                                const std::optional<std::string> &kind) {
  std::string sql =
      std::string("SELECT ") + kSymbolCols + " FROM symbol WHERE spelling = ?";
  std::vector<SqlValue> args;
  args.emplace_back(spelling);
  if (kind) {
    sql += " AND kind = ?";
    args.emplace_back(symbol_kind_id(*kind)); // stored as int (v16)
  }
  sql += " ORDER BY usr";
  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < args.size(); ++i) {
    st.bind(static_cast<int>(i + 1), args[i]);
  }
  std::vector<Symbol> out;
  while (st.step()) {
    out.push_back(symbol_from(st));
  }
  return out;
}

std::vector<Symbol>
Storage::lookup_symbols_by_qual_name(const std::string &qual_name,
                                     const std::optional<std::string> &kind) {
  std::string sql =
      std::string("SELECT ") + kSymbolCols + " FROM symbol WHERE qual_name = ?";
  std::vector<SqlValue> args;
  args.emplace_back(qual_name);
  if (kind) {
    sql += " AND kind = ?";
    args.emplace_back(symbol_kind_id(*kind)); // stored as int (v16)
  }
  sql += " ORDER BY usr";
  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < args.size(); ++i) {
    st.bind(static_cast<int>(i + 1), args[i]);
  }
  std::vector<Symbol> out;
  while (st.step()) {
    out.push_back(symbol_from(st));
  }
  return out;
}

std::vector<Symbol>
Storage::search_symbols(const std::string &pattern,
                        const std::optional<std::string> &kind) {
  // '%seg%seg%' on qual_name: each '::'-separated segment must appear, in
  // order, as a substring. Only % and _ are escaped (storage.py parity).
  std::vector<std::string> segs;
  std::size_t start = 0;
  while (start <= pattern.size()) {
    const std::size_t pos = pattern.find("::", start);
    const std::string seg = pattern.substr(
        start, pos == std::string::npos ? std::string::npos : pos - start);
    if (!seg.empty()) {
      std::string esc;
      for (const char c : seg) {
        if (c == '%') {
          esc += "\\%";
        } else if (c == '_') {
          esc += "\\_";
        } else {
          esc += c;
        }
      }
      segs.push_back(esc);
    }
    if (pos == std::string::npos) {
      break;
    }
    start = pos + 2;
  }
  const std::string like = "%" + join_strings(segs, "%") + "%";
  std::string sql = std::string("SELECT ") + kSymbolCols +
                    " FROM symbol WHERE qual_name LIKE ? ESCAPE '\\'";
  std::vector<SqlValue> args;
  args.emplace_back(like);
  if (kind) {
    sql += " AND kind = ?";
    args.emplace_back(symbol_kind_id(*kind)); // stored as int (v16)
  }
  sql += " ORDER BY LENGTH(qual_name), qual_name";
  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < args.size(); ++i) {
    st.bind(static_cast<int>(i + 1), args[i]);
  }
  std::vector<Symbol> out;
  while (st.step()) {
    out.push_back(symbol_from(st));
  }
  return out;
}

std::vector<Symbol>
Storage::list_symbols(const std::optional<int64_t> &component_id,
                      const std::optional<std::string> &dir_path,
                      const std::optional<int64_t> &file_id,
                      const std::optional<std::string> &name,
                      const std::optional<std::string> &kind) {
  std::string sql = std::string("SELECT ") + kSymbolColsS + " FROM symbol s";
  std::vector<std::string> where;
  std::vector<SqlValue> args;
  if (component_id || dir_path) {
    // Location scope matches if EITHER the definition site or the declaration
    // site falls inside (storage.py:701-714).
    std::vector<std::string> scope;
    std::vector<SqlValue> scope_args;
    if (component_id) {
      scope.push_back("d.component_id = ?");
      scope_args.emplace_back(*component_id);
    }
    if (dir_path) {
      scope.push_back(dir_scope_sql(*dir_path, scope_args));
    }
    where.push_back("EXISTS (SELECT 1 FROM file f "
                    "JOIN directory d ON d.id = f.directory_id "
                    "WHERE f.id IN (s.file_id, s.decl_file_id) AND " +
                    join_strings(scope, " AND ") + ")");
    for (auto &a : scope_args) {
      args.push_back(std::move(a));
    }
  }
  if (file_id) {
    where.push_back("(s.file_id = ? OR s.decl_file_id = ?)");
    args.emplace_back(*file_id);
    args.emplace_back(*file_id);
  }
  if (name && !name->empty()) {
    where.push_back("COALESCE(s.qual_name, s.spelling) LIKE ? ESCAPE '\\'");
    args.emplace_back(fuzzy_like(*name));
  }
  if (kind) {
    where.push_back("s.kind = ?");
    args.emplace_back(symbol_kind_id(*kind)); // stored as int (v16)
  }
  if (!where.empty()) {
    sql += " WHERE " + join_strings(where, " AND ");
  }
  sql += " ORDER BY LENGTH(COALESCE(s.qual_name, s.spelling)),"
         " COALESCE(s.qual_name, s.spelling)";
  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < args.size(); ++i) {
    st.bind(static_cast<int>(i + 1), args[i]);
  }
  std::vector<Symbol> out;
  while (st.step()) {
    out.push_back(symbol_from(st));
  }
  return out;
}

std::vector<Symbol> Storage::symbols_in_file(int64_t file_id) {
  auto st = db_.prepare(std::string("SELECT ") + kSymbolCols +
                        " FROM symbol WHERE file_id = ? ORDER BY line, col");
  st.bind(1, file_id);
  std::vector<Symbol> out;
  while (st.step()) {
    out.push_back(symbol_from(st));
  }
  return out;
}

std::vector<Symbol> Storage::unresolved_symbols() {
  auto st = db_.prepare(std::string("SELECT ") + kSymbolCols +
                        " FROM symbol WHERE resolved = 0 ORDER BY usr");
  std::vector<Symbol> out;
  while (st.step()) {
    out.push_back(symbol_from(st));
  }
  return out;
}

// -- graph layer (v7)
// -----------------------------------------------------------------

int64_t Storage::mint_symbol_id(const std::string &usr,
                                const std::string &spelling,
                                const std::string &qual_name,
                                const std::string &display_name,
                                const std::string &kind,
                                const std::optional<int64_t> &decl_file_id,
                                const std::optional<int64_t> &decl_line,
                                const std::optional<int64_t> &decl_col,
                                const std::optional<std::string> &decl_path,
                                bool is_instantiation, bool is_named_instance) {
  // The follow-up SELECT returns the stable id whether the row was minted or
  // already present. 'function' is the fallback kind when the cursor kind is
  // unknown; the real def's add_symbol upsert overwrites kind/location/resolved
  // later. On a repeat mint we only UPGRADE an unnamed stub (empty spelling) --
  // name and kind together -- never clobber a real symbol's; the decl location
  // (registered id or raw path) is filled in only when still absent (COALESCE).
  // decl_path carries the location of a target in an UNREGISTERED (system/
  // stdlib) file so the stub stays located instead of @<no-location>.
  // is_instantiation marks implicit template-instantiation nodes (v13); MAX()
  // ensures a stub->instantiation promotion always upgrades but never downgrades.
  auto ins = db_.prepare(
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
      "  is_named_instance = MAX(symbol.is_named_instance, excluded.is_named_instance)");
  ins.bind(1, std::string_view(usr));
  ins.bind(2, std::string_view(spelling));
  if (qual_name.empty()) {
    ins.bind_null(3);
  } else {
    ins.bind(3, std::string_view(qual_name));
  }
  if (display_name.empty()) {
    ins.bind_null(4);
  } else {
    ins.bind(4, std::string_view(display_name));
  }
  ins.bind(5, symbol_kind_id(kind)); // kind stored as CXCursorKind int (v16)
  bind_opt(ins, 6, decl_file_id);
  bind_opt(ins, 7, decl_line);
  bind_opt(ins, 8, decl_col);
  bind_opt(ins, 9, decl_path);
  ins.bind(10, static_cast<int64_t>(is_instantiation ? 1 : 0));
  ins.bind(11, static_cast<int64_t>(is_named_instance ? 1 : 0));
  ins.step_done();
  auto sel = db_.prepare("SELECT id FROM symbol WHERE usr = ?");
  sel.bind(1, std::string_view(usr));
  if (!sel.step()) {
    throw StorageError("mint_symbol_id: SELECT returned no row for usr=" + usr);
  }
  return sel.col_int64(0);
}

int64_t Storage::add_edge(const Edge &e) {
  auto st = db_.prepare(
      "INSERT INTO edge (src_id, dst_id, kind, count, base_access, is_virtual, "
      "                  vtable_slot) "
      "VALUES (?, ?, ?, ?, ?, ?, ?) "
      "ON CONFLICT(src_id, dst_id, kind) DO UPDATE SET "
      "  count       = edge.count + excluded.count, "
      "  base_access = COALESCE(excluded.base_access, edge.base_access), "
      "  is_virtual  = COALESCE(excluded.is_virtual,  edge.is_virtual), "
      "  vtable_slot = COALESCE(excluded.vtable_slot, edge.vtable_slot) "
      "RETURNING id");
  st.bind(1, e.src_id);
  st.bind(2, e.dst_id);
  st.bind(3, e.kind);
  st.bind(4, e.count);
  bind_opt(st, 5, e.base_access);
  bind_opt(st, 6, e.is_virtual);
  bind_opt(st, 7, e.vtable_slot);
  if (!st.step()) {
    throw StorageError("add_edge: upsert returned no id");
  }
  const int64_t eid = st.col_int64(0);
  st.step_done();
  return eid;
}

void Storage::add_edge_site(const EdgeSite &s) {
  auto st = db_.prepare(
      "INSERT OR IGNORE INTO edge_site "
      "(edge_id, file_id, line, col, conditional, args_sig, "
      " recv_src_kind, recv_type_usr, recv_decl_usr, recv_param_pos,"
      " recv_type_is_value) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
  st.bind(1, s.edge_id);
  bind_opt(st, 2, s.file_id);
  bind_opt(st, 3, s.line);
  bind_opt(st, 4, s.col);
  st.bind(5, s.conditional);
  bind_opt(st, 6, s.args_sig);
  bind_opt(st, 7, s.recv_src_kind);
  bind_opt(st, 8, s.recv_type_usr);
  bind_opt(st, 9, s.recv_decl_usr);
  bind_opt(st, 10, s.recv_param_pos);
  bind_opt(st, 11, s.recv_type_is_value);
  st.step_done();
}

void Storage::add_call_arg(const CallArg &a) {
  auto st = db_.prepare(
      "INSERT OR IGNORE INTO call_arg "
      "(edge_id, file_id, line, col, position, src_kind, "
      " type_usr, decl_usr, callee_usr, type_is_value) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)");
  st.bind(1, a.edge_id);
  st.bind(2, a.file_id);
  st.bind(3, a.line);
  st.bind(4, a.col);
  st.bind(5, a.position);
  st.bind(6, std::string_view(a.src_kind));
  bind_opt(st, 7, a.type_usr);
  bind_opt(st, 8, a.decl_usr);
  bind_opt(st, 9, a.callee_usr);
  bind_opt(st, 10, a.type_is_value);
  st.step_done();
}

void Storage::add_template_param(const TemplateParam &p) {
  auto st = db_.prepare(
      "INSERT OR REPLACE INTO template_param "
      "(owner_id, position, param_kind, name, default_txt) "
      "VALUES (?, ?, ?, ?, ?)");
  st.bind(1, p.owner_id);
  st.bind(2, p.position);
  st.bind(3, p.param_kind);
  bind_opt(st, 4, p.name);
  bind_opt(st, 5, p.default_txt);
  st.step_done();
}

void Storage::add_template_arg(const TemplateArg &a) {
  auto st = db_.prepare(
      "INSERT OR REPLACE INTO template_arg "
      "(owner_id, position, arg_kind, ref_id, literal) "
      "VALUES (?, ?, ?, ?, ?)");
  st.bind(1, a.owner_id);
  st.bind(2, a.position);
  st.bind(3, a.arg_kind);
  bind_opt(st, 4, a.ref_id);
  bind_opt(st, 5, a.literal);
  st.step_done();
}

void Storage::delete_edges_for_file(int64_t file_id) {
  // Exclude contains (kind=3): declaration-level structural edges emitted
  // during header indexing. Namespaces reopen in every .cpp TU, so deleting
  // contains on each re-index would permanently erase the header-phase edges.
  // Contains edges are idempotent (UPSERT); excluding them here is safe.
  auto st = db_.prepare(
      "DELETE FROM edge WHERE kind != 3 AND src_id IN "
      "(SELECT id FROM symbol WHERE file_id = ?)");
  st.bind(1, file_id);
  st.step_done();
}

void Storage::rollup_edge_counts() {
  // For calls (1) and uses (7): set count = COUNT(edge_site) so the edge
  // reflects true site count after multi-TU indexing.
  db_.exec(
      "UPDATE edge SET count = ("
      "  SELECT COUNT(*) FROM edge_site WHERE edge_site.edge_id = edge.id"
      ") "
      "WHERE kind IN (1, 7)"
      "  AND EXISTS (SELECT 1 FROM edge_site WHERE edge_site.edge_id = edge.id)");
}

std::vector<Edge> Storage::cross_repo_edges() {
  auto st = db_.prepare(
      "SELECT e.id, e.src_id, e.dst_id, e.kind, e.count, "
      "       e.base_access, e.is_virtual, e.vtable_slot "
      "FROM edge e "
      "  JOIN symbol s1 ON s1.id = e.src_id "
      "  JOIN symbol s2 ON s2.id = e.dst_id "
      "  JOIN file f1 ON f1.id = s1.file_id "
      "  JOIN directory d1 ON d1.id = f1.directory_id "
      "  JOIN file f2 ON f2.id = s2.file_id "
      "  JOIN directory d2 ON d2.id = f2.directory_id "
      "WHERE d1.component_id <> d2.component_id");
  std::vector<Edge> out;
  while (st.step()) {
    Edge e;
    e.id = st.col_int64(0);
    e.src_id = st.col_int64(1);
    e.dst_id = st.col_int64(2);
    e.kind = st.col_int64(3);
    e.count = st.col_int64(4);
    e.base_access = opt_int64(st, 5);
    e.is_virtual = opt_int64(st, 6);
    e.vtable_slot = opt_int64(st, 7);
    out.push_back(e);
  }
  return out;
}

// -- entity_edge (v17) --------------------------------------------------------

void Storage::add_entity_edge(int64_t src_id, int64_t dst_id, int64_t kind,
                               int64_t count,
                               std::optional<int64_t> via_member_id,
                               int64_t multiplicity, int64_t access,
                               int64_t is_virtual,
                               std::optional<int64_t> create_form,
                               int64_t partial) {
  auto st = db_.prepare(
      "INSERT INTO entity_edge "
      "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
      " access, is_virtual, create_form, partial) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
      "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
      "  count       = excluded.count, "
      "  multiplicity = excluded.multiplicity, "
      "  access      = excluded.access, "
      "  is_virtual  = excluded.is_virtual, "
      "  create_form = COALESCE(excluded.create_form, entity_edge.create_form), "
      "  partial     = excluded.partial");
  st.bind(1, src_id);
  st.bind(2, dst_id);
  st.bind(3, kind);
  st.bind(4, count);
  if (via_member_id) {
    st.bind(5, *via_member_id);
  } else {
    st.bind_null(5);
  }
  st.bind(6, multiplicity);
  st.bind(7, access);
  st.bind(8, is_virtual);
  if (create_form) {
    st.bind(9, *create_form);
  } else {
    st.bind_null(9);
  }
  st.bind(10, partial);
  st.step_done();
}

void Storage::clear_entity_edges() {
  db_.exec("DELETE FROM entity_edge");
}

// ---------------------------------------------------------------------------
// materialise_entity_edges: pure-DB roll-up of all 11 entity relation kinds.
// Mirrors indexer/entity_rollup.py:materialize_entity_edges() byte-identically.
// ---------------------------------------------------------------------------

// Per-pass precomputed lookups (perf: O(n^2) -> O(n)). The collapse +
// interface/abstractness helpers used to issue a fresh SQL query (often
// several) PER edge row across every phase -- on a large corpus that is the
// dominant cost of `resolve` and made the pass run for a very long time with no
// DB writes (read-heavy, so index.db mtime / journal never moved -- looking
// frozen). RollupState precomputes the same answers ONCE per materialise pass
// from tables that are READ-ONLY for the pass (edge, symbol), then the hot
// helpers become in-memory map/set lookups. Byte-identical to the old per-row
// queries (the parity gate + acceptance suite verify), so a pure speedup.
// Mirrors entity_rollup._RollupState (Python).
struct RollupState {
  std::unordered_map<int64_t, int64_t> next_hop;       // src -> first 4/5 dst
  std::unordered_map<int64_t, int64_t> collapse_cache; // memoised collapse
  std::unordered_set<std::string> non_pure_method_owners;
  std::unordered_set<std::string> field_owners;
  std::unordered_set<std::string> pure_method_owners;
  std::unordered_map<int64_t, std::string> usr_by_id; // entity-kind ids only

  explicit RollupState(cidx::SqliteDb &db) {
    // collapse next-hop: FIRST (kind, dst_id) per src among kind 4/5 edges ==
    // the old `WHERE src_id=? AND kind IN (4,5) ORDER BY kind, dst_id LIMIT 1`
    // for every src in one ordered scan (emplace keeps the first per key).
    {
      auto st = db.prepare("SELECT src_id, dst_id FROM edge WHERE kind IN (4, 5) "
                           "ORDER BY src_id, kind, dst_id");
      while (st.step()) {
        next_hop.emplace(st.col_int64(0), st.col_int64(1));
      }
    }
    // Interface / abstractness owner-sets keyed by parent_usr (the three
    // COUNT(*) probes the old is_interface ran PER call, hoisted to 3 scans).
    {
      auto st = db.prepare("SELECT DISTINCT parent_usr FROM symbol "
                           "WHERE kind = 21 AND is_pure = 0 AND parent_usr IS NOT NULL");
      while (st.step()) {
        non_pure_method_owners.insert(st.col_text(0));
      }
    }
    {
      auto st = db.prepare("SELECT DISTINCT parent_usr FROM symbol "
                           "WHERE kind = 6 AND parent_usr IS NOT NULL");
      while (st.step()) {
        field_owners.insert(st.col_text(0));
      }
    }
    {
      auto st = db.prepare("SELECT DISTINCT parent_usr FROM symbol "
                           "WHERE kind = 21 AND is_pure = 1 AND parent_usr IS NOT NULL");
      while (st.step()) {
        pure_method_owners.insert(st.col_text(0));
      }
    }
    {
      auto st = db.prepare("SELECT id, usr FROM symbol WHERE kind IN (2,3,4,5,31)");
      while (st.step()) {
        usr_by_id.emplace(st.col_int64(0), st.col_text(1));
      }
    }
  }

  int64_t collapse(int64_t sym_id) {
    auto hit = collapse_cache.find(sym_id);
    if (hit != collapse_cache.end()) {
      return hit->second;
    }
    std::set<int64_t> seen;
    int64_t cur = sym_id;
    while (seen.find(cur) == seen.end()) {
      seen.insert(cur);
      auto nit = next_hop.find(cur);
      if (nit == next_hop.end()) {
        break;
      }
      cur = nit->second;
    }
    collapse_cache.emplace(sym_id, cur);
    return cur;
  }

  bool is_interface(int64_t sym_id) const {
    auto it = usr_by_id.find(sym_id);
    if (it == usr_by_id.end()) {
      return false;
    }
    const std::string &usr = it->second;
    if (non_pure_method_owners.count(usr) != 0) {
      return false;
    }
    if (field_owners.count(usr) != 0) {
      return false;
    }
    return pure_method_owners.count(usr) != 0;
  }

  bool has_pure(int64_t sym_id) const {
    auto it = usr_by_id.find(sym_id);
    return it != usr_by_id.end() && pure_method_owners.count(it->second) != 0;
  }
};

// Module-global state for the in-progress pass. Set by materialise_entity_edges
// (and cpp_materialise_entity_nodes when called standalone for the v21->v22
// backfill); cleared by the matching CtxGuard destructor. resolve is
// single-threaded and the helpers only run synchronously within a pass.
RollupState *g_rollup_ctx = nullptr;

// RAII guard: builds + installs a RollupState only if none is active yet (the
// outermost caller "owns" it), and uninstalls on scope exit. A nested call
// (entity_nodes inside materialise_entity_edges) reuses the active state with
// no rebuild. Mirrors the owns_ctx logic in entity_rollup (Python).
struct CtxGuard {
  std::optional<RollupState> st;
  bool owned;
  explicit CtxGuard(cidx::SqliteDb &db) : owned(g_rollup_ctx == nullptr) {
    if (owned) {
      st.emplace(db);
      g_rollup_ctx = &*st;
    }
  }
  ~CtxGuard() {
    if (owned) {
      g_rollup_ctx = nullptr;
    }
  }
  CtxGuard(const CtxGuard &) = delete;
  CtxGuard &operator=(const CtxGuard &) = delete;
  CtxGuard(CtxGuard &&) = delete;
  CtxGuard &operator=(CtxGuard &&) = delete;
};

// Template-instance collapse (ADR-008 decision 6 / OQ-3): map a template
// instance/specialization symbol onto its primary template. Both the Layer-0
// instantiates(5) and specializes(4) edges point instance -> primary, so we
// follow an outgoing 4/5 edge until none remains. Returns sym_id unchanged
// when it is not an instance/specialization. Mirrors entity_rollup._collapse_to_primary.
// Delegates to the per-pass precomputed next-hop map; `db` is unused (kept for
// signature stability with the call sites).
static int64_t cpp_collapse_to_primary(cidx::SqliteDb &db, int64_t sym_id) {
  (void)db;
  return g_rollup_ctx->collapse(sym_id);
}

// Phase 1: generalizes(1) / implements(2) from inherits(2) edges.
static void cpp_materialise_inheritance(cidx::SqliteDb &db) {
  // Is sym_id a pure Interface? Delegates to the per-pass precomputed
  // owner-sets (RollupState), identical to the old per-row COUNT(*) probes.
  const auto is_interface = [](int64_t sym_id) -> bool {
    return g_rollup_ctx->is_interface(sym_id);
  };

  auto st = db.prepare(
      "SELECT e.src_id, e.dst_id, e.base_access, e.is_virtual "
      "FROM edge e "
      "JOIN symbol src ON src.id = e.src_id "
      "JOIN symbol dst ON dst.id = e.dst_id "
      "WHERE e.kind = 2 "
      "  AND src.kind IN (2,3,4,5) "
      "  AND dst.kind IN (2,3,4,5)");

  struct InhRow { int64_t src; int64_t dst; int64_t acc; int64_t virt; };
  std::vector<InhRow> rows;
  while (st.step()) {
    InhRow r;
    r.src  = st.col_int64(0);
    r.dst  = st.col_int64(1);
    r.acc  = st.col_int64(2);
    r.virt = st.col_int64(3);
    rows.push_back(r);
  }

  for (const auto &r : rows) {
    // Collapse the DERIVED side (src) onto its primary template, but keep the
    // BASE (dst) un-collapsed: a template used as a base
    // (`class Cache : public Singleton<Cache>`) is its OWN design entity, so we
    // want `Cache generalizes Singleton<Cache>` and let the separate
    // instantiates(11) edge carry `Singleton<Cache> -> Singleton`.  (Pre-CRTP-
    // fix this was moot -- no base specifier had an instantiates(5) Layer-0
    // edge, so collapsing the dst was always a no-op.)
    int64_t src = cpp_collapse_to_primary(db, r.src);
    int64_t dst = r.dst;
    if (src == dst) continue;  // no self-edge
    int64_t ek = is_interface(dst) ? 2 : 1;  // implements=2 or generalizes=1
    auto ins = db.prepare(
        "INSERT INTO entity_edge "
        "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
        " access, is_virtual, create_form, partial) "
        "VALUES (?, ?, ?, 1, NULL, 1, ?, ?, NULL, 0) "
        "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
        "  access     = excluded.access, "
        "  is_virtual = excluded.is_virtual");
    ins.bind(1, src);
    ins.bind(2, dst);
    ins.bind(3, ek);
    ins.bind(4, r.acc);
    ins.bind(5, r.virt);
    ins.step_done();
  }
}

// Phase 2: specializes(3) from Layer-0 specializes(4) edges between entity
// symbols. These come ONLY from EXPLICIT / PARTIAL specializations (the
// extractor emits kind 4 for those and kind 5 (instantiates) for plain
// instantiations, so the two are disjoint at Layer-0). Mirrors
// entity_rollup._materialise_specializes.
static void cpp_materialise_specializes(cidx::SqliteDb &db) {
  auto st = db.prepare(
      "SELECT e.src_id, e.dst_id "
      "FROM edge e "
      "JOIN symbol src ON src.id = e.src_id "
      "JOIN symbol dst ON dst.id = e.dst_id "
      "WHERE e.kind = 4 "
      "  AND src.kind IN (2,3,4,5,31) "
      "  AND dst.kind IN (2,3,4,5,31)");
  std::vector<std::pair<int64_t,int64_t>> rows;
  while (st.step()) rows.emplace_back(st.col_int64(0), st.col_int64(1));
  for (const auto &[src0, dst0] : rows) {
    // The specialization is its OWN design entity -- do NOT collapse the SOURCE
    // onto the primary (that would self-suppress the edge). Collapse only the
    // destination (already the primary; this is a no-op there but keeps the
    // phase robust to chains).
    int64_t src = src0;
    int64_t dst = cpp_collapse_to_primary(db, dst0);
    if (src == dst) continue;
    auto ins = db.prepare(
        "INSERT INTO entity_edge "
        "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
        " access, is_virtual, create_form, partial) "
        "VALUES (?, ?, 3, 1, NULL, 1, 0, 0, NULL, 0) "
        "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
        "  count = entity_edge.count + 1");
    ins.bind(1, src);
    ins.bind(2, dst);
    ins.step_done();
  }
}

// Phase 2b: instantiates(11) from Layer-0 instantiates(5) edges between entity
// symbols. src = the concrete instance `X<B>`, dst = the primary template `X`.
// An implicit instantiation is a distinct design entity (UML <<bind>>), so --
// exactly like specializes -- the SOURCE is kept un-collapsed (collapsing it
// would follow its own kind-5 edge to the primary and self-suppress the row).
// Mirrors entity_rollup._materialise_instantiates.
static void cpp_materialise_instantiates(cidx::SqliteDb &db) {
  auto st = db.prepare(
      "SELECT e.src_id, e.dst_id "
      "FROM edge e "
      "JOIN symbol src ON src.id = e.src_id "
      "JOIN symbol dst ON dst.id = e.dst_id "
      "WHERE e.kind = 5 "
      "  AND src.kind IN (2,3,4,5,31) "
      "  AND dst.kind IN (2,3,4,5,31)");
  std::vector<std::pair<int64_t,int64_t>> rows;
  while (st.step()) rows.emplace_back(st.col_int64(0), st.col_int64(1));
  for (const auto &[src0, dst0] : rows) {
    int64_t src = src0;
    int64_t dst = cpp_collapse_to_primary(db, dst0);
    if (src == dst) continue;
    auto ins = db.prepare(
        "INSERT INTO entity_edge "
        "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
        " access, is_virtual, create_form, partial) "
        "VALUES (?, ?, 11, 1, NULL, 1, 0, 0, NULL, 0) "
        "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
        "  count = entity_edge.count + 1");
    ins.bind(1, src);
    ins.bind(2, dst);
    ins.step_done();
  }
}

// Classify field type spelling → (entity_edge kind, multiplicity).
// Split the inside of a <...> on TOP-LEVEL commas (depth-aware). Mirrors
// entity_rollup._split_template_args.
static std::vector<std::string> cpp_split_template_args(const std::string &inner) {
  std::vector<std::string> args;
  int depth = 0;
  std::string cur;
  auto flush = [&] {
    size_t a = cur.find_first_not_of(' ');
    if (a != std::string::npos) {
      size_t b = cur.find_last_not_of(' ');
      args.push_back(cur.substr(a, b - a + 1));
    }
    cur.clear();
  };
  for (char ch : inner) {
    if (ch == '<') { ++depth; cur.push_back(ch); }
    else if (ch == '>') { --depth; cur.push_back(ch); }
    else if (ch == ',' && depth == 0) { flush(); }
    else cur.push_back(ch);
  }
  flush();
  return args;
}

// For `s` starting with a `...<` wrapper `prefix`, return the VALUE type: the
// LAST top-level template arg (map<K,V> -> V). Mirrors _wrapper_value_type.
static std::string cpp_wrapper_value_type(const std::string &s,
                                          const char *prefix) {
  std::string inner = s.substr(strlen(prefix));
  while (!inner.empty() && inner.back() == ' ') inner.pop_back();
  if (!inner.empty() && inner.back() == '>') inner.pop_back();
  while (!inner.empty() && inner.back() == ' ') inner.pop_back();
  auto args = cpp_split_template_args(inner);
  return args.empty() ? inner : args.back();
}

static std::pair<int64_t,int64_t> cpp_classify_field_type(
    const std::string &type_info) {
  const std::string s = [&] {
    std::string r = type_info;
    // Strip const/volatile
    for (const auto *q : {"const ", "volatile "}) {
      std::string::size_type p;
      while ((p = r.find(q)) != std::string::npos) r.erase(p, strlen(q));
    }
    while (!r.empty() && r.front() == ' ') r.erase(r.begin());
    while (!r.empty() && r.back()  == ' ') r.pop_back();
    return r;
  }();
  // Array
  if (!s.empty() && s.back() == ']') return {4, 4};
  // Containers
  static const char *containers[] = {
    "std::vector<", "vector<", "std::list<", "list<",
    "std::deque<", "deque<", "std::set<", "set<",
    "std::unordered_set<", "unordered_set<",
    "std::map<", "std::unordered_map<", nullptr
  };
  for (const char **c = containers; *c; ++c) {
    if (s.substr(0, strlen(*c)) == *c) {
      // Classify the VALUE type (last template arg, so map<K,V> uses V).
      auto [ik, _] = cpp_classify_field_type(cpp_wrapper_value_type(s, *c));
      return {ik, 3};
    }
  }
  // unique_ptr / optional -> composes (EXCLUSIVE ownership: destroyed with the
  // owner, cannot outlive it -- same lifetime as a value member), 0..1.
  static const char *excl[] = {"std::unique_ptr<", "unique_ptr<",
                                "std::optional<", "optional<", nullptr};
  for (const char **u = excl; *u; ++u) {
    if (s.substr(0, strlen(*u)) == *u) return {4, 2};  // composes=4
  }
  // shared_ptr -> aggregates (SHARED ownership: the pointee can outlive the
  // owner while other shared_ptrs keep it alive).
  static const char *shared[] = {"std::shared_ptr<", "shared_ptr<", nullptr};
  for (const char **u = shared; *u; ++u) {
    if (s.substr(0, strlen(*u)) == *u) return {5, 2};  // aggregates=5
  }
  static const char *weak_raw[] = {"std::weak_ptr<", "weak_ptr<", nullptr};
  for (const char **w = weak_raw; *w; ++w) {
    if (s.substr(0, strlen(*w)) == *w) return {6, 2};  // associates=6
  }
  if (!s.empty() && s.back() == '*') return {6, 2};
  if (!s.empty() && s.back() == '&') return {6, 2};
  return {4, 1};  // composes=4, multiplicity=1 (value)
}

// Resolve entity from type spelling (strips wrappers, looks up by qual_name/spelling).
static std::optional<int64_t> cpp_resolve_entity_from_type(
    cidx::SqliteDb &db, std::string type_info) {
  // Strip qualifiers
  for (const auto *q : {"const ", "volatile "}) {
    std::string::size_type p;
    while ((p = type_info.find(q)) != std::string::npos)
      type_info.erase(p, strlen(q));
  }
  while (!type_info.empty() && type_info.front() == ' ')
    type_info.erase(type_info.begin());
  while (!type_info.empty() && type_info.back() == ' ') type_info.pop_back();
  // Strip trailing * & []
  bool stripped = true;
  while (stripped) {
    stripped = false;
    if (!type_info.empty() && type_info.back() == '*') {
      type_info.pop_back(); stripped = true;
    } else if (!type_info.empty() && type_info.back() == '&') {
      type_info.pop_back(); stripped = true;
    } else if (!type_info.empty() && type_info.back() == ']') {
      auto p = type_info.rfind('[');
      if (p != std::string::npos) { type_info = type_info.substr(0,p); stripped = true; }
    }
    while (!type_info.empty() && type_info.back() == ' ') type_info.pop_back();
  }
  // Strip smart-ptr / container wrappers
  static const char *wrappers[] = {
    "std::unique_ptr<", "unique_ptr<", "std::shared_ptr<", "shared_ptr<",
    "std::weak_ptr<",   "weak_ptr<",   "std::optional<",   "optional<",
    "std::vector<", "vector<", "std::list<", "list<",
    "std::deque<", "deque<", "std::set<", "set<",
    "std::unordered_set<", "unordered_set<",
    "std::map<", "std::unordered_map<", nullptr
  };
  for (const char **w = wrappers; *w; ++w) {
    if (type_info.substr(0, strlen(*w)) == *w) {
      // Recurse on the VALUE type (last template arg) so map<K,V> -> V and
      // nested generics peel one level at a time.
      return cpp_resolve_entity_from_type(db, cpp_wrapper_value_type(type_info, *w));
    }
  }
  // Lookup by qual_name
  auto st1 = db.prepare(
      "SELECT id FROM symbol WHERE qual_name = ? AND kind IN (2,3,4,5) LIMIT 1");
  st1.bind(1, std::string_view(type_info));
  if (st1.step()) return st1.col_int64(0);
  // Lookup by spelling
  auto st2 = db.prepare(
      "SELECT id FROM symbol WHERE spelling = ? AND kind IN (2,3,4,5) LIMIT 1");
  st2.bind(1, std::string_view(type_info));
  if (st2.step()) return st2.col_int64(0);
  return std::nullopt;
}

// Phase 3: composes/aggregates/associates from field_of(8) edges.
static void cpp_materialise_field_relations(cidx::SqliteDb &db) {
  struct FieldRow {
    int64_t field_id, owner_id, field_kind_int;
    std::string type_info, field_access;
  };
  auto st = db.prepare(
      "SELECT e.src_id, e.dst_id, s.type_info, s.kind AS field_kind, "
      "       s.access AS field_access "
      "FROM edge e "
      "JOIN symbol s ON s.id = e.src_id "
      "JOIN symbol owner ON owner.id = e.dst_id "
      "WHERE e.kind = 8 "
      "  AND owner.kind IN (2,3,4,5) "
      "  AND s.kind IN (6, 21)");
  std::vector<FieldRow> rows;
  while (st.step()) {
    FieldRow r;
    r.field_id       = st.col_int64(0);
    r.owner_id       = st.col_int64(1);
    r.type_info      = st.col_text(2);
    r.field_kind_int = st.col_int64(3);
    r.field_access   = st.col_text(4);
    rows.push_back(r);
  }

  static const std::map<std::string,int64_t> acc_map = {
    {"public",0}, {"protected",1}, {"private",2}
  };

  for (const auto &r : rows) {
    if (r.field_kind_int != 6) continue;  // only data members
    if (r.type_info.empty()) continue;

    // Stage 4: prefer a structural member -> NAMED-INSTANCE uses(7) edge. A
    // `X<B> m_;` member mints the `X<B>` instance (is_named_instance=1) and the
    // extractor records a uses(7) edge member -> instance keyed on the spec USR
    // (unambiguous across namespaces -- unlike a display_name match). The named
    // instance is its OWN design entity, so it is NOT collapsed onto the primary
    // -> we emit `A composes/associates X<B>`, completing A -> X<B> -> B. Reached
    // ONLY for minted named instances (non-system specializations); `std::vector
    // <Foo>` is never minted, so its peel-to-Foo resolution below is unchanged.
    std::optional<int64_t> ref_entity_id;
    bool skip_ref_collapse = false;
    auto nist = db.prepare(
        "SELECT e.dst_id FROM edge e "
        "JOIN symbol s ON s.id = e.dst_id "
        "WHERE e.src_id = ? AND e.kind = 7 AND s.is_named_instance = 1 "
        "ORDER BY e.dst_id LIMIT 1");
    nist.bind(1, r.field_id);
    if (nist.step()) {
      ref_entity_id = nist.col_int64(0);
      skip_ref_collapse = true;
    }

    if (!ref_entity_id) {
      // Try template_arg.ref_id first.  Use the LAST type arg (highest position)
      // so map<K,V> picks the VALUE V, not the key K; single-arg containers /
      // smart-ptrs are unaffected.
      auto tst = db.prepare(
          "SELECT ref_id FROM template_arg WHERE owner_id = ? "
          "AND arg_kind = 1 AND ref_id IS NOT NULL ORDER BY position DESC LIMIT 1");
      tst.bind(1, r.field_id);
      if (tst.step()) ref_entity_id = tst.col_int64(0);

      if (!ref_entity_id)
        ref_entity_id = cpp_resolve_entity_from_type(db, r.type_info);
    }
    if (!ref_entity_id) continue;

    // Confirm referent is entity
    auto ck = db.prepare("SELECT kind FROM symbol WHERE id = ?");
    ck.bind(1, *ref_entity_id);
    if (!ck.step()) continue;
    const int64_t ref_kind = ck.col_int64(0);
    if (ref_kind != 2 && ref_kind != 3 && ref_kind != 4 && ref_kind != 5) continue;

    auto [ek, mult] = cpp_classify_field_type(r.type_info);
    int64_t access_int = 0;
    if (acc_map.count(r.field_access)) access_int = acc_map.at(r.field_access);

    // Collapse the owner onto its primary template.  The referent is collapsed
    // too UNLESS it is a named instance (kept un-collapsed so the edge points at
    // `X<B>`, not the primary `X`).
    int64_t owner_pid = cpp_collapse_to_primary(db, r.owner_id);
    int64_t ref_pid = skip_ref_collapse
                          ? *ref_entity_id
                          : cpp_collapse_to_primary(db, *ref_entity_id);
    if (owner_pid == ref_pid) continue;

    auto ins = db.prepare(
        "INSERT INTO entity_edge "
        "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
        " access, is_virtual, create_form, partial) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, 0, NULL, 0) "
        "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
        "  count = entity_edge.count + 1");
    ins.bind(1, owner_pid);
    ins.bind(2, ref_pid);
    ins.bind(3, ek);
    ins.bind(4, r.field_id);
    ins.bind(5, mult);
    ins.bind(6, access_int);
    ins.step_done();
  }
}

// Strip wrappers/qualifiers/ptr-ref-array off a field type, returning the bare
// innermost token (e.g. 'std::vector<T>' -> 'T', 'const T *' -> 'T'). Mirrors
// entity_rollup._strip_to_param_core. Used to discover which template parameter
// a primary-template member binds.
static std::string cpp_strip_to_param_core(const std::string &type_spelling) {
  auto strip_quals = [](std::string s) {
    for (const auto *q : {"const ", "volatile "}) {
      std::string::size_type p;
      while ((p = s.find(q)) != std::string::npos) s.erase(p, strlen(q));
    }
    while (!s.empty() && s.front() == ' ') s.erase(s.begin());
    while (!s.empty() && s.back() == ' ') s.pop_back();
    return s;
  };
  std::string s = strip_quals(type_spelling);
  while (!s.empty() && (s.back() == '&' || s.back() == '*' || s.back() == ']')) {
    if (s.back() == ']') {
      auto p = s.rfind('[');
      s = (p == std::string::npos) ? std::string() : s.substr(0, p);
    } else {
      s.pop_back();
    }
    while (!s.empty() && s.front() == ' ') s.erase(s.begin());
    while (!s.empty() && s.back() == ' ') s.pop_back();
  }
  s = strip_quals(s);
  static const char *wrappers[] = {
    "std::unique_ptr<", "unique_ptr<", "std::shared_ptr<", "shared_ptr<",
    "std::weak_ptr<",   "weak_ptr<",   "std::optional<",   "optional<",
    "std::vector<", "vector<", "std::list<", "list<",
    "std::deque<", "deque<", "std::set<", "set<",
    "std::unordered_set<", "unordered_set<",
    "std::map<", "std::unordered_map<", nullptr
  };
  for (const char **w = wrappers; *w; ++w) {
    if (s.substr(0, strlen(*w)) == *w) {
      return cpp_strip_to_param_core(cpp_wrapper_value_type(s, *w));
    }
  }
  return s;
}

// Phase 3b: composes/aggregates/associates for NAMED template instances.
// A `using Y = X<B>;` mints the X<B> instance (is_named_instance=1) but libclang
// materialises NO members for it, so Phase 3 cannot classify them. Instead read
// the PRIMARY's members and SUBSTITUTE the instance's bound type: for a member
// binding template param i (bare T, vector<T>, unique_ptr<T>, T*, ...), look up
// the instance's template_arg at position i (-> B) and emit X<B> <ownership> B.
// The instance is NOT collapsed onto the primary. Mirrors
// entity_rollup._materialise_instance_composition.
static void cpp_materialise_instance_composition(cidx::SqliteDb &db) {
  struct InstRow { int64_t inst_id, prim_id; };
  std::vector<InstRow> instances;
  {
    auto st = db.prepare(
        "SELECT e.src_id, e.dst_id "
        "FROM edge e "
        "JOIN symbol inst ON inst.id = e.src_id "
        "JOIN symbol prim ON prim.id = e.dst_id "
        "WHERE e.kind = 5 AND inst.is_named_instance = 1 AND prim.kind = 31 "
        "ORDER BY e.src_id, e.dst_id");
    while (st.step()) instances.push_back({st.col_int64(0), st.col_int64(1)});
  }

  static const std::map<std::string,int64_t> acc_map = {
    {"public",0}, {"protected",1}, {"private",2}
  };

  for (const auto &inst : instances) {
    // primary template parameter NAME -> position (type params only)
    std::map<std::string,int64_t> param_pos;
    {
      auto st = db.prepare(
          "SELECT position, name FROM template_param WHERE owner_id = ? "
          "AND param_kind = 1 ORDER BY position");
      st.bind(1, inst.prim_id);
      while (st.step()) {
        const std::string nm = st.col_text(1);
        if (!nm.empty()) param_pos.emplace(nm, st.col_int64(0));
      }
    }

    // instance bound TYPE args: position -> ref_id (the entity B). NULL ref_id
    // (builtin arg) recorded as nullopt so it is skipped below.
    std::map<int64_t, std::optional<int64_t>> bound;
    {
      auto st = db.prepare(
          "SELECT position, ref_id FROM template_arg WHERE owner_id = ? "
          "AND arg_kind = 1 ORDER BY position");
      st.bind(1, inst.inst_id);
      while (st.step()) {
        std::optional<int64_t> ref;
        if (!st.col_is_null(1)) ref = st.col_int64(1);
        bound[st.col_int64(0)] = ref;
      }
    }

    // primary template's data members
    struct FieldRow { int64_t field_id; std::string type_info, access; };
    std::vector<FieldRow> fields;
    {
      auto st = db.prepare(
          "SELECT e.src_id, s.type_info, s.access "
          "FROM edge e "
          "JOIN symbol s ON s.id = e.src_id "
          "WHERE e.kind = 8 AND e.dst_id = ? AND s.kind = 6 "
          "ORDER BY e.src_id");
      st.bind(1, inst.prim_id);
      while (st.step())
        fields.push_back({st.col_int64(0), st.col_text(1), st.col_text(2)});
    }

    for (const auto &f : fields) {
      if (f.type_info.empty()) continue;
      const std::string core = cpp_strip_to_param_core(f.type_info);
      auto pit = param_pos.find(core);
      int64_t ref_entity_id;
      if (pit != param_pos.end()) {
        // Parameterised member (binds T): substitute the instance's bound type
        // -> X<B> <ownership> B.
        auto bit = bound.find(pit->second);
        if (bit == bound.end() || !bit->second) continue;  // builtin/unindexed
        ref_entity_id = *bit->second;
      } else {
        // Stage 3: CONCRETE (non-parameterised) member, e.g. `Widget w;` on the
        // primary -> carry `X<B> <ownership> Widget` onto the instance too.
        // System / unindexed concrete types resolve to nullopt and are skipped,
        // so no std:: explosion.
        auto re = cpp_resolve_entity_from_type(db, f.type_info);
        if (!re) continue;
        ref_entity_id = *re;
      }

      auto ck = db.prepare("SELECT kind FROM symbol WHERE id = ?");
      ck.bind(1, ref_entity_id);
      if (!ck.step()) continue;
      const int64_t ref_kind = ck.col_int64(0);
      if (ref_kind != 2 && ref_kind != 3 && ref_kind != 4 && ref_kind != 5)
        continue;
      if (inst.inst_id == ref_entity_id) continue;

      auto [ek, mult] = cpp_classify_field_type(f.type_info);
      int64_t access_int = 0;
      if (acc_map.count(f.access)) access_int = acc_map.at(f.access);

      auto ins = db.prepare(
          "INSERT INTO entity_edge "
          "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
          " access, is_virtual, create_form, partial) "
          "VALUES (?, ?, ?, 1, ?, ?, ?, 0, NULL, 0) "
          "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
          "  count = entity_edge.count + 1");
      ins.bind(1, inst.inst_id);
      ins.bind(2, ref_entity_id);
      ins.bind(3, ek);
      ins.bind(4, f.field_id);
      ins.bind(5, mult);
      ins.bind(6, access_int);
      ins.step_done();
    }
  }
}

// Phase 4: creates(7) / destroys(9) from PR1 construction/destruction edges.
static void cpp_materialise_creates_destroys(cidx::SqliteDb &db) {
  // Layer-0 construct/destroy edge.kind -> create_form
  static const std::map<int64_t,int64_t> form_map = {
    {10,3},{11,4},{12,5},{13,7},{14,8},{15,6}
  };
  constexpr int64_t destroy_kind = 16;

  struct SiteRow { int64_t src_fn, dst_sym, l0_kind; };
  auto st = db.prepare(
      "SELECT e.src_id, e.dst_id, e.kind "
      "FROM edge e "
      "JOIN symbol src ON src.id = e.src_id "
      "JOIN symbol dst ON dst.id = e.dst_id "
      "WHERE e.kind IN (10,11,12,13,14,15,16)");
  std::vector<SiteRow> rows;
  while (st.step()) {
    rows.push_back({st.col_int64(0), st.col_int64(1), st.col_int64(2)});
  }

  for (const auto &r : rows) {
    // Enclosing entity (method_of=9, owner must be entity)
    auto own_st = db.prepare(
        "SELECT e.dst_id FROM edge e "
        "JOIN symbol owner ON owner.id = e.dst_id "
        "WHERE e.src_id = ? AND e.kind = 9 "
        "  AND owner.kind IN (2,3,4,5) LIMIT 1");
    own_st.bind(1, r.src_fn);
    if (!own_st.step()) continue;  // free fn: no entity src
    const int64_t owner_entity = own_st.col_int64(0);

    // Target entity: ctor/dtor parent → record
    std::optional<int64_t> target;
    auto par_st = db.prepare(
        "SELECT id FROM symbol "
        "WHERE usr = (SELECT parent_usr FROM symbol WHERE id = ?) "
        "  AND kind IN (2,3,4,5) LIMIT 1");
    par_st.bind(1, r.dst_sym);
    if (par_st.step()) {
      target = par_st.col_int64(0);
    } else {
      // dst itself might be entity (rare)
      auto dk = db.prepare("SELECT kind FROM symbol WHERE id = ?");
      dk.bind(1, r.dst_sym);
      if (dk.step()) {
        int64_t k = dk.col_int64(0);
        if (k==2||k==3||k==4||k==5) target = r.dst_sym;
      }
    }
    if (!target) continue;

    // Collapse both endpoints onto their primary template.
    int64_t owner_pid = cpp_collapse_to_primary(db, owner_entity);
    int64_t target_pid = cpp_collapse_to_primary(db, *target);
    if (owner_pid == target_pid) continue;

    if (r.l0_kind == destroy_kind) {
      auto ins = db.prepare(
          "INSERT INTO entity_edge "
          "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
          " access, is_virtual, create_form, partial) "
          "VALUES (?, ?, 9, 1, NULL, 1, 0, 0, NULL, 0) "
          "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
          "  count = entity_edge.count + 1");
      ins.bind(1, owner_pid);
      ins.bind(2, target_pid);
      ins.step_done();
    } else {
      int64_t create_form = form_map.at(r.l0_kind);
      int64_t partial = (r.l0_kind == 15) ? 1 : 0;
      auto ins = db.prepare(
          "INSERT INTO entity_edge "
          "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
          " access, is_virtual, create_form, partial) "
          "VALUES (?, ?, 7, 1, NULL, 1, 0, 0, ?, ?) "
          "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
          "  count = entity_edge.count + 1, "
          "  create_form = COALESCE(excluded.create_form, entity_edge.create_form), "
          "  partial = excluded.partial");
      ins.bind(1, owner_pid);
      ins.bind(2, target_pid);
      ins.bind(3, create_form);
      ins.bind(4, partial);
      ins.step_done();
    }
  }

  // By-value return (create_form=2): method return type → creates(7, partial=1)
  struct RetRow { int64_t method_id, owner_id; std::string type_info; };
  auto rst = db.prepare(
      "SELECT s.id, s.type_info, e.dst_id AS owner_id "
      "FROM symbol s "
      "JOIN edge e ON e.src_id = s.id AND e.kind = 9 "
      "JOIN symbol owner ON owner.id = e.dst_id AND owner.kind IN (2,3,4,5) "
      "WHERE s.kind IN (21, 24) AND s.type_info IS NOT NULL");
  std::vector<RetRow> ret_rows;
  while (rst.step()) {
    ret_rows.push_back({rst.col_int64(0), rst.col_int64(2), rst.col_text(1)});
  }
  for (const auto &r : ret_rows) {
    const std::string &ti = r.type_info;
    std::string ret_type;
    auto paren = ti.find('(');
    if (paren != std::string::npos && paren > 0) {
      ret_type = ti.substr(0, paren);
      while (!ret_type.empty() && ret_type.back() == ' ') ret_type.pop_back();
    } else {
      ret_type = ti;
    }
    if (ret_type.empty() || ret_type == "void" || ret_type == "auto") continue;
    auto ret_eid = cpp_resolve_entity_from_type(db, ret_type);
    if (!ret_eid) continue;

    // Collapse both endpoints onto their primary template.
    int64_t owner_pid = cpp_collapse_to_primary(db, r.owner_id);
    int64_t ret_pid = cpp_collapse_to_primary(db, *ret_eid);
    if (ret_pid == owner_pid) continue;  // constructors return own type

    auto ins = db.prepare(
        "INSERT INTO entity_edge "
        "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
        " access, is_virtual, create_form, partial) "
        "VALUES (?, ?, 7, 1, NULL, 1, 0, 0, 2, 1) "
        "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
        "  count = entity_edge.count + 1");
    ins.bind(1, owner_pid);
    ins.bind(2, ret_pid);
    ins.step_done();
  }
}

// Phase 5: uses(8) from method→method calls across entity boundaries.
static void cpp_materialise_uses(cidx::SqliteDb &db) {
  struct UseRow { int64_t caller, callee, is_pure; };
  auto st = db.prepare(
      "SELECT e.src_id, e.dst_id, dst.is_pure "
      "FROM edge e "
      "JOIN symbol src ON src.id = e.src_id "
      "JOIN symbol dst ON dst.id = e.dst_id "
      "WHERE e.kind IN (1, 7) "
      "  AND src.kind IN (21, 8, 24, 25, 30) "
      "  AND dst.kind IN (21, 8, 24, 25, 30)");
  std::vector<UseRow> rows;
  while (st.step()) {
    rows.push_back({st.col_int64(0), st.col_int64(1), st.col_int64(2)});
  }
  for (const auto &r : rows) {
    // Caller owner entity
    auto co = db.prepare(
        "SELECT e.dst_id FROM edge e "
        "JOIN symbol owner ON owner.id = e.dst_id "
        "WHERE e.src_id = ? AND e.kind = 9 "
        "  AND owner.kind IN (2,3,4,5) LIMIT 1");
    co.bind(1, r.caller);
    if (!co.step()) continue;
    int64_t src_eid = co.col_int64(0);

    // Callee owner entity
    auto coe = db.prepare(
        "SELECT e.dst_id FROM edge e "
        "JOIN symbol owner ON owner.id = e.dst_id "
        "WHERE e.src_id = ? AND e.kind = 9 "
        "  AND owner.kind IN (2,3,4,5) LIMIT 1");
    coe.bind(1, r.callee);
    if (!coe.step()) continue;
    int64_t dst_eid = coe.col_int64(0);

    // Collapse both endpoints onto their primary template.
    src_eid = cpp_collapse_to_primary(db, src_eid);
    dst_eid = cpp_collapse_to_primary(db, dst_eid);
    if (src_eid == dst_eid) continue;
    int64_t partial = r.is_pure ? 1 : 0;

    auto ins = db.prepare(
        "INSERT INTO entity_edge "
        "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
        " access, is_virtual, create_form, partial) "
        "VALUES (?, ?, 8, 1, ?, 1, 0, 0, NULL, ?) "
        "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
        "  count = entity_edge.count + 1, "
        "  partial = MAX(entity_edge.partial, excluded.partial)");
    ins.bind(1, src_eid);
    ins.bind(2, dst_eid);
    ins.bind(3, r.callee);
    ins.bind(4, partial);
    ins.step_done();
  }
}

// Phase 6: befriends(10) from friend(17) edges between entity symbols.
static void cpp_materialise_befriends(cidx::SqliteDb &db) {
  auto st = db.prepare(
      "SELECT e.src_id, e.dst_id "
      "FROM edge e "
      "JOIN symbol src ON src.id = e.src_id "
      "JOIN symbol dst ON dst.id = e.dst_id "
      "WHERE e.kind = 17 "
      "  AND src.kind IN (2,3,4,5) "
      "  AND dst.kind IN (2,3,4,5)");
  std::vector<std::pair<int64_t,int64_t>> rows;
  while (st.step()) rows.emplace_back(st.col_int64(0), st.col_int64(1));
  for (const auto &[src0, dst0] : rows) {
    int64_t src = cpp_collapse_to_primary(db, src0);
    int64_t dst = cpp_collapse_to_primary(db, dst0);
    if (src == dst) continue;
    auto ins = db.prepare(
        "INSERT INTO entity_edge "
        "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
        " access, is_virtual, create_form, partial) "
        "VALUES (?, ?, 10, 1, NULL, 1, 0, 0, NULL, 0) "
        "ON CONFLICT(src_id, dst_id, kind, COALESCE(via_member_id, -1), COALESCE(create_form, -1)) DO UPDATE SET "
        "  count = entity_edge.count + 1");
    ins.bind(1, src);
    ins.bind(2, dst);
    ins.step_done();
  }
}

// Phase 7: entity_node(id, kind) -- the materialized design type of every
// entity symbol. Mirrors entity_rollup._materialise_entity_nodes byte-identically.
// Abstractness (own pure-virtual methods + own data fields) decides
// class/abstract_class/interface (and the same split for class templates);
// union/enum keep their own type. The C++ keyword (class vs struct) is NOT
// distinguished here -- that lives at the low-level symbol layer.
static void cpp_materialise_entity_nodes(cidx::SqliteDb &db) {
  // Usable standalone (the v21->v22 entity_node backfill in the Storage ctor
  // calls this directly), so it installs the per-pass RollupState itself when
  // one is not already active (i.e. when NOT called from materialise_entity_edges).
  CtxGuard guard(db);
  // entity_kind ids: class=1 abstract_class=2 interface=3 union=4 enum=5
  // class_template=6 abstract_class_template=7 interface_template=8.
  // is_interface / has_pure delegate to the precomputed owner-sets.
  const auto classify = [](int64_t sym_id, int64_t sym_kind) -> int64_t {
    if (sym_kind == 5) return 5;  // enum
    if (sym_kind == 3) return 4;  // union
    bool is_template = (sym_kind == 31);
    if (g_rollup_ctx->is_interface(sym_id)) return is_template ? 8 : 3;
    if (g_rollup_ctx->has_pure(sym_id)) return is_template ? 7 : 2;
    return is_template ? 6 : 1;
  };

  db.exec("DELETE FROM entity_node");
  std::vector<std::pair<int64_t, int64_t>> rows;  // (id, kind)
  {
    auto st = db.prepare("SELECT id, kind FROM symbol WHERE kind IN (2,3,4,5,31)");
    while (st.step()) rows.emplace_back(st.col_int64(0), st.col_int64(1));
  }
  for (const auto &[sym_id, sym_kind] : rows) {
    auto ins = db.prepare(
        "INSERT OR REPLACE INTO entity_node (id, kind) VALUES (?, ?)");
    ins.bind(1, sym_id);
    ins.bind(2, classify(sym_id, sym_kind));
    ins.step_done();
  }
}

void Storage::materialise_entity_edges() {
  // Idempotent: full re-materialise each resolve. The DELETE runs INSIDE the
  // rebuild transaction so a failure in any phase rolls back to the previous
  // rows instead of leaving entity_edge empty (atomic resolve).
  //
  // RollupState precomputes the collapse next-hop map + interface owner-sets
  // ONCE for the whole pass (edge/symbol are read-only here), so every phase's
  // collapse / interface lookups are in-memory instead of per-row SQL.
  CtxGuard guard(db_);
  {
    auto txn = transaction();
    db_.exec("DELETE FROM entity_edge");
    const auto run = [&](const char *name, void (*fn)(cidx::SqliteDb &)) {
      fn(db_);
      auto c = db_.prepare("SELECT COUNT(*) FROM entity_edge");
      const int64_t n = c.step() ? c.col_int64(0) : 0;
    };
    run("inheritance", cpp_materialise_inheritance);
    run("specializes", cpp_materialise_specializes);
    run("instantiates", cpp_materialise_instantiates);
    run("field_relations", cpp_materialise_field_relations);
    run("instance_composition", cpp_materialise_instance_composition);
    run("creates_destroys", cpp_materialise_creates_destroys);
    run("uses", cpp_materialise_uses);
    run("befriends", cpp_materialise_befriends);
    run("entity_nodes", cpp_materialise_entity_nodes);
    txn.commit();
  }
}

int Storage::resolve_pass() {
  // Roll up edge.count for calls/uses from edge_site counts.
  rollup_edge_counts();
  // Materialise Layer-1 entity_edge from the Layer-0 graph.
  materialise_entity_edges();
  // Count remaining stub symbols: a minted placeholder never backfilled by a
  // real symbol -- resolved=0 with NO location (neither a definition nor a decl
  // site). NOT keyed on spelling -- stubs are now minted NAMED, so the absence
  // of any location is the robust signal (matches Sym::is_stub).
  auto st = db_.prepare(
      "SELECT COUNT(*) FROM symbol "
      "WHERE resolved = 0 AND file_id IS NULL AND decl_file_id IS NULL");
  if (!st.step()) {
    return 0;
  }
  return static_cast<int>(st.col_int64(0));
}

// -- fuzzy matching
// -----------------------------------------------------------------

std::string Storage::fuzzy_like(std::string_view text) {
  // '%c%c%' from the non-space chars, escaping '\ % _' (G18); used with
  // LIKE ... ESCAPE '\' — ASCII case-insensitive.
  std::vector<std::string> chars;
  for (const char c : text) {
    if (std::isspace(static_cast<unsigned char>(c)) != 0) {
      continue;
    }
    if (c == '\\') {
      chars.emplace_back("\\\\");
    } else if (c == '%') {
      chars.emplace_back("\\%");
    } else if (c == '_') {
      chars.emplace_back("\\_");
    } else {
      chars.emplace_back(1, c);
    }
  }
  return "%" + join_strings(chars, "%") + "%";
}

// -- stats
// ----------------------------------------------------------------------------

Stats Storage::stats() {
  const auto one = [this](const char *sql) {
    auto st = db_.prepare(sql);
    if (!st.step()) {
      throw StorageError("stats query returned no row");
    }
    return st.col_int64(0);
  };
  Stats s;
  s.components = one("SELECT COUNT(*) FROM component");
  s.directories = one("SELECT COUNT(*) FROM directory");
  s.files = one("SELECT COUNT(*) FROM file");
  s.files_indexed = one("SELECT COUNT(*) FROM file WHERE indexed = 1");
  s.symbols = one("SELECT COUNT(*) FROM symbol");
  s.symbols_unresolved = one("SELECT COUNT(*) FROM symbol WHERE resolved = 0");
  {
    auto st = db_.prepare(
        "SELECT kind, COUNT(*) AS n FROM symbol GROUP BY kind ORDER BY kind");
    while (st.step()) {
      s.symbols_by_kind[symbol_kind_name(st.col_int64(0))] = st.col_int64(1);
    }
  }
  s.edges = one("SELECT COUNT(*) FROM edge");
  {
    auto st = db_.prepare(
        "SELECT k.name, COUNT(*) AS n FROM edge e "
        "JOIN edge_kind k ON k.id = e.kind GROUP BY k.name ORDER BY k.name");
    while (st.step()) {
      s.edges_by_kind[st.col_text(0)] = st.col_int64(1);
    }
  }
  return s;
}

// ============================================================================
// M6 graph read-only accessors (A1–A8)
// ============================================================================

// A1 — total edge count (query.py:558)
int64_t Storage::edge_count() {
  auto st = db_.prepare("SELECT COUNT(*) FROM edge");
  if (!st.step()) {
    return 0;
  }
  return st.col_int64(0);
}

// A2 — true once graph_resolved_at is set (query.py:579-583)
bool Storage::graph_resolved() {
  auto st = db_.prepare(
      "SELECT value FROM meta WHERE key = 'graph_resolved_at'");
  if (!st.step()) {
    return false;
  }
  const std::string val = st.col_text(0);
  return !val.empty();
}

// A3 — fetch one symbol by USR (query.py:666-668)
std::optional<Symbol> Storage::graph_symbol_by_usr(const std::string &usr) {
  auto st = db_.prepare(std::string("SELECT ") + kSymbolColsS +
                        " FROM symbol s WHERE s.usr = ?");
  st.bind(1, std::string_view(usr));
  if (!st.step()) {
    return std::nullopt;
  }
  return symbol_from_offset(st, 0);
}

// A4 — fetch one symbol by numeric id (query.py:666-668)
std::optional<Symbol> Storage::graph_symbol_by_id(int64_t id) {
  auto st = db_.prepare(std::string("SELECT ") + kSymbolColsS +
                        " FROM symbol s WHERE s.id = ?");
  st.bind(1, id);
  if (!st.step()) {
    return std::nullopt;
  }
  return symbol_from_offset(st, 0);
}

// A5 — fuzzy COALESCE(qual_name,spelling) lookup (query.py:707-738, R1)
// Escapes ONLY % and _ (NOT backslash — matching query.py:719).
std::vector<Symbol> Storage::find_symbols(const std::string &pattern,
                                          const std::optional<std::string> &kind,
                                          int limit) {
  // Build like: "%" + join("%", escaped_segs) + "%"
  // where escaped_segs = each "::" segment with % and _ escaped.
  std::vector<std::string> segs;
  std::size_t start = 0;
  while (start <= pattern.size()) {
    const std::size_t pos = pattern.find("::", start);
    const std::string seg = pattern.substr(
        start, pos == std::string::npos ? std::string::npos : pos - start);
    if (!seg.empty()) {
      std::string esc;
      esc.reserve(seg.size());
      for (const char c : seg) {
        if (c == '%') {
          esc += "\\%";
        } else if (c == '_') {
          esc += "\\_";
        } else {
          esc += c;
        }
      }
      segs.push_back(esc);
    }
    if (pos == std::string::npos) {
      break;
    }
    start = pos + 2;
  }
  std::string like = "%";
  for (std::size_t i = 0; i < segs.size(); ++i) {
    if (i != 0) {
      like += "%";
    }
    like += segs[i];
  }
  like += "%";

  std::string sql = std::string("SELECT ") + kSymbolColsS +
                    " FROM symbol s WHERE COALESCE(s.qual_name, s.spelling) "
                    "LIKE ? ESCAPE '\\'";
  std::vector<SqlValue> args;
  args.emplace_back(like);
  if (kind) {
    sql += " AND s.kind = ?";
    args.emplace_back(symbol_kind_id(*kind)); // stored as int (v16)
  }
  sql += " ORDER BY LENGTH(COALESCE(s.qual_name, s.spelling)), "
         "COALESCE(s.qual_name, s.spelling) LIMIT ?";
  args.emplace_back(static_cast<int64_t>(limit));

  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < args.size(); ++i) {
    st.bind(static_cast<int>(i + 1), args[i]);
  }
  std::vector<Symbol> out;
  while (st.step()) {
    out.push_back(symbol_from_offset(st, 0));
  }
  return out;
}

// A6 — typed-edge query (query.py:782-813)
std::vector<Storage::GraphEdgeRow>
Storage::graph_edges(int64_t mine_id, const std::string &direction,
                     const std::vector<int64_t> &kind_ids, bool count_resolved,
                     int limit) {
  // direction "in": mine=dst_id, peer=src_id
  // direction "out": mine=src_id, peer=dst_id
  std::string mine, peer;
  if (direction == "in") {
    mine = "dst_id";
    peer = "src_id";
  } else {
    mine = "src_id";
    peer = "dst_id";
  }

  const std::string count_expr =
      count_resolved
          ? "e.count"
          : "(SELECT COUNT(*) FROM edge_site es WHERE es.edge_id = e.id)";

  std::string sql =
      "SELECT e.id AS eid, e.src_id, e.dst_id, e.kind AS ekind, " +
      count_expr +
      " AS ecount, e.count AS rawcount, "
      "e.base_access, e.is_virtual, " +
      std::string(kSymbolColsS) +
      " FROM edge e JOIN symbol s ON s.id = e." + peer +
      " WHERE e." + mine + " = ?";

  std::vector<SqlValue> args;
  args.emplace_back(mine_id);

  if (!kind_ids.empty()) {
    sql += " AND e.kind IN (";
    for (std::size_t i = 0; i < kind_ids.size(); ++i) {
      if (i != 0) {
        sql += ",";
      }
      sql += "?";
    }
    sql += ")";
    for (int64_t kid : kind_ids) {
      args.emplace_back(kid);
    }
  }
  sql += " ORDER BY ecount DESC, e.kind LIMIT ?";
  args.emplace_back(static_cast<int64_t>(limit));

  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < args.size(); ++i) {
    st.bind(static_cast<int>(i + 1), args[i]);
  }

  std::vector<GraphEdgeRow> out;
  while (st.step()) {
    GraphEdgeRow row;
    row.eid = st.col_int64(0);
    row.src_id = st.col_int64(1);
    row.dst_id = st.col_int64(2);
    row.ekind = st.col_int64(3);
    row.ecount = st.col_int64(4);
    row.rawcount = st.col_int64(5);
    row.base_access = opt_int64(st, 6);
    row.is_virtual = opt_int64(st, 7);
    // Sym columns start at col 8 (R: column-order mismatch, plan §CRITICAL)
    row.sym = symbol_from_offset(st, 8);
    out.push_back(std::move(row));
  }
  return out;
}

// A7 — batch-load edge_site rows for many edge_ids (query.py:839-870)
std::map<int64_t, std::vector<Storage::EdgeSiteRow>>
Storage::edge_sites_for(const std::vector<int64_t> &edge_ids) {
  if (edge_ids.empty()) {
    return {};
  }
  std::string sql =
      "SELECT edge_id, file_id, line, col, conditional, args_sig, "
      "       recv_src_kind, recv_type_usr, recv_decl_usr, recv_param_pos, "
      "       recv_type_is_value "
      "FROM edge_site WHERE edge_id IN (";
  for (std::size_t i = 0; i < edge_ids.size(); ++i) {
    if (i != 0) {
      sql += ",";
    }
    sql += "?";
  }
  sql += ") ORDER BY edge_id, file_id, line, col";

  auto st = db_.prepare(sql);
  for (std::size_t i = 0; i < edge_ids.size(); ++i) {
    st.bind(static_cast<int>(i + 1), edge_ids[i]);
  }

  std::map<int64_t, std::vector<EdgeSiteRow>> out;
  while (st.step()) {
    EdgeSiteRow row;
    row.edge_id = st.col_int64(0);
    row.file_id = opt_int64(st, 1);
    row.line = opt_int64(st, 2);
    row.col = opt_int64(st, 3);
    row.conditional = st.col_int64(4) != 0;
    row.args_sig = opt_text(st, 5);
    row.recv_src_kind = opt_text(st, 6);
    row.recv_type_usr = opt_text(st, 7);
    row.recv_decl_usr = opt_text(st, 8);
    row.recv_param_pos = opt_int64(st, 9);
    row.recv_type_is_value = opt_int64(st, 10);
    out[row.edge_id].push_back(std::move(row));
  }
  return out;
}

// A8 — single-edge sites with LIMIT (query.py:884-906)
std::vector<Storage::EdgeSiteRow>
Storage::edge_sites_one(int64_t edge_id, int limit) {
  auto st = db_.prepare(
      "SELECT file_id, line, col, conditional, args_sig, "
      "       recv_src_kind, recv_type_usr, recv_decl_usr, recv_param_pos, "
      "       recv_type_is_value "
      "FROM edge_site WHERE edge_id = ? ORDER BY file_id, line, col LIMIT ?");
  st.bind(1, edge_id);
  st.bind(2, static_cast<int64_t>(limit));

  std::vector<EdgeSiteRow> out;
  while (st.step()) {
    EdgeSiteRow row;
    row.edge_id = edge_id;
    row.file_id = opt_int64(st, 0);
    row.line = opt_int64(st, 1);
    row.col = opt_int64(st, 2);
    row.conditional = st.col_int64(3) != 0;
    row.args_sig = opt_text(st, 4);
    row.recv_src_kind = opt_text(st, 5);
    row.recv_type_usr = opt_text(st, 6);
    row.recv_decl_usr = opt_text(st, 7);
    row.recv_param_pos = opt_int64(st, 8);
    row.recv_type_is_value = opt_int64(st, 9);
    out.push_back(std::move(row));
  }
  return out;
}

// -- labels (v14) ------------------------------------------------------------

int64_t Storage::add_label(const std::string &name, const std::string &path) {
  auto st = db_.prepare(
      "INSERT INTO label (name, path) VALUES (?, ?) "
      "ON CONFLICT(name) DO UPDATE SET path = excluded.path "
      "RETURNING id");
  st.bind(1, std::string_view(name));
  st.bind(2, std::string_view(path));
  if (!st.step()) {
    throw StorageError("label upsert returned no id");
  }
  const int64_t lid = st.col_int64(0);
  st.step_done();
  return lid;
}

bool Storage::remove_label(const std::string &name) {
  auto st = db_.prepare("DELETE FROM label WHERE name = ?");
  st.bind(1, std::string_view(name));
  st.step_done();
  return db_.changes() > 0;
}

std::optional<std::string> Storage::get_label(const std::string &name) {
  auto st = db_.prepare("SELECT path FROM label WHERE name = ?");
  st.bind(1, std::string_view(name));
  if (!st.step()) {
    return std::nullopt;
  }
  return st.col_text(0);
}

std::vector<std::pair<std::string, std::string>> Storage::list_labels() {
  auto st = db_.prepare("SELECT name, path FROM label ORDER BY name");
  std::vector<std::pair<std::string, std::string>> out;
  while (st.step()) {
    out.emplace_back(st.col_text(0), st.col_text(1));
  }
  return out;
}

std::map<std::string, std::tuple<std::string, std::string, bool>>
Storage::component_alias_index() {
  // Group components by name; split the resolved effective root into
  // (base, version) so matching is version-agnostic. Mirrors
  // Python Storage.component_alias_index.
  struct Row {
    std::string base;
    std::string ver;       // "" = none
    bool path_unversioned; // stored path carries no embedded version
  };
  std::map<std::string, std::vector<Row>> by_name;
  for (const auto &c : list_components()) {
    const std::string eff =
        pathutil::abspath(pathutil::resolve_fs_path(effective_root(c)));
    const auto [base, ver] = CompileDb::split_base_version(eff);
    const auto [pbase, pver] = CompileDb::split_base_version(
        pathutil::abspath(pathutil::resolve_fs_path(c.path)));
    (void)pbase;
    by_name[c.name].push_back({base, ver, pver.empty()});
  }
  std::map<std::string, std::tuple<std::string, std::string, bool>> out;
  for (const auto &[name, rows] : by_name) {
    std::string base = rows.front().base;
    bool one_base = true;
    for (const auto &r : rows) {
      if (r.base != base) {
        one_base = false;
        break;
      }
    }
    if (!one_base) {
      continue; // ambiguous: same name, different base dirs
    }
    std::string maxver;
    for (const auto &r : rows) {
      if (r.ver.empty()) {
        continue;
      }
      if (maxver.empty() ||
          CompileDb::version_key(r.ver) > CompileDb::version_key(maxver)) {
        maxver = r.ver;
      }
    }
    const bool bumpable = rows.size() == 1 && rows.front().path_unversioned;
    out.emplace(name, std::make_tuple(base, maxver, bumpable));
  }
  return out;
}

std::vector<std::tuple<std::string, std::string, bool>>
Storage::list_alias_pairs() {
  // Explicit labels (exact) PLUS components (version-stripped base,
  // version-agnostic). Labels win on a name collision. std::map keeps the
  // result sorted by name (== Python sorted). Mirrors Python list_alias_pairs.
  std::map<std::string, std::tuple<std::string, bool>> pairs; // name->(path,ver)
  for (const auto &nv : list_labels()) {
    pairs[nv.first] = {nv.second, false}; // labels first / win
  }
  for (const auto &[name, entry] : component_alias_index()) {
    if (pairs.find(name) == pairs.end()) {
      pairs[name] = {std::get<0>(entry), true}; // version-stripped base
    }
  }
  std::vector<std::tuple<std::string, std::string, bool>> out;
  out.reserve(pairs.size());
  for (const auto &[name, pv] : pairs) {
    out.emplace_back(name, std::get<0>(pv), std::get<1>(pv));
  }
  return out;
}

std::optional<std::string> Storage::get_alias(const std::string &name) {
  std::optional<std::string> lab = get_label(name);
  if (lab.has_value()) {
    return lab;
  }
  const auto idx = component_alias_index();
  const auto it = idx.find(name);
  if (it == idx.end()) {
    return std::nullopt;
  }
  const auto &[base, maxver, bump] = it->second;
  (void)bump;
  return maxver.empty() ? base : pathutil::join(base, maxver);
}

} // namespace cidx

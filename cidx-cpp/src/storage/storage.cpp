// Port of indexer/storage.py. The schema text and the three upsert statements
// are copied character-for-character (design §4/§4.2); read queries keep the
// same WHERE/ORDER BY text but select explicit column lists so that rows from
// MIGRATED databases (where ALTER TABLE appended columns at the end) decode
// correctly without name-based row factories.
#include "storage/storage.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <exception>
#include <filesystem>
#include <string>

#include "util/errors.hpp"
#include "util/json_min.hpp"
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
    path         TEXT NOT NULL,
    UNIQUE (component_id, path)
);

CREATE TABLE IF NOT EXISTS file (
    id              INTEGER PRIMARY KEY,
    directory_id    INTEGER NOT NULL REFERENCES directory(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    mtime           REAL,
    md5             TEXT,
    compile_options TEXT,
    driver          TEXT,
    indexed         INTEGER NOT NULL DEFAULT 0,
    indexed_at      TEXT,
    args_overridden INTEGER NOT NULL DEFAULT 0,
    UNIQUE (directory_id, name)
);

CREATE TABLE IF NOT EXISTS symbol (
    id           INTEGER PRIMARY KEY,
    usr          TEXT NOT NULL UNIQUE,
    spelling     TEXT NOT NULL,
    qual_name    TEXT,
    display_name TEXT,
    kind         TEXT NOT NULL CHECK (kind IN ('class', 'class-template',
                 'constructor', 'destructor', 'enum', 'enum-constant',
                 'function', 'function-template', 'macro', 'member', 'method',
                 'namespace', 'struct', 'type-alias', 'typedef', 'union',
                 'variable')),
    type_info    TEXT,
    file_id      INTEGER REFERENCES file(id) ON DELETE SET NULL,
    line         INTEGER,
    col          INTEGER,
    decl_file_id INTEGER REFERENCES file(id) ON DELETE SET NULL,
    decl_line    INTEGER,
    decl_col     INTEGER,
    decl_path    TEXT,                         -- raw decl path for a target in an
                                              -- UNREGISTERED (system/stdlib) file
                                              -- no component owns: the AST has the
                                              -- location but there is no file row,
                                              -- so the stub keeps the path here
    is_definition INTEGER NOT NULL DEFAULT 0,
    is_pure      INTEGER NOT NULL DEFAULT 0,
    is_static    INTEGER NOT NULL DEFAULT 0,  -- v12: C++ static member function
    is_instantiation INTEGER NOT NULL DEFAULT 0,  -- v13: implicit template
                                              -- instantiation node (own USR,
                                              -- definition via instantiates edge)
    linkage      TEXT,
    access       TEXT,
    parent_usr   TEXT,
    resolved     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_symbol_spelling ON symbol(spelling);
CREATE INDEX IF NOT EXISTS idx_symbol_qual     ON symbol(qual_name);
CREATE INDEX IF NOT EXISTS idx_symbol_file     ON symbol(file_id);
CREATE INDEX IF NOT EXISTS idx_symbol_parent   ON symbol(parent_usr);
CREATE INDEX IF NOT EXISTS idx_symbol_kind     ON symbol(kind);

-- ---- v7 graph layer (PLAN §2/§6) -----------------------------------------

CREATE TABLE IF NOT EXISTS edge_kind (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
INSERT OR IGNORE INTO edge_kind (id, name) VALUES
  (1,'calls'), (2,'inherits'), (3,'contains'), (4,'specializes'),
  (5,'instantiates'), (6,'overrides'), (7,'uses'),
  (8,'field_of'), (9,'method_of');

CREATE TABLE IF NOT EXISTS edge (
    id          INTEGER PRIMARY KEY,
    src_id      INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    dst_id      INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    kind        INTEGER NOT NULL REFERENCES edge_kind(id),
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

-- ---- v14: label registry (portable paths §5) --------------------------------

CREATE TABLE IF NOT EXISTS label (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,   -- label key, e.g. 'libfoo-include'
    path TEXT NOT NULL           -- stored verbatim; may contain $VAR
);

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '14');
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
constexpr char kComponentCols[] = "id, name, path, kind, version";
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
  s.kind = st.col_text(off + 5);
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

Storage::Storage(const std::string &path) : db_(prepare_db_path(path)) {
  db_.exec("PRAGMA foreign_keys = ON");
  migrate(); // BEFORE the schema script: its indexes need migrated columns
             // (G19)
  db_.exec(kSchema);
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
  if (changed) {
    auto st =
        db_.prepare("UPDATE meta SET value = ? WHERE key = 'schema_version'");
    st.bind(1, std::string_view(std::to_string(kSchemaVersion)));
    st.step_done();
  }
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
  st.bind(5, std::string_view(sym.kind));
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
    args.emplace_back(*kind);
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
    args.emplace_back(*kind);
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
    args.emplace_back(*kind);
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
    args.emplace_back(*kind);
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
                                bool is_instantiation) {
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
      "                    is_instantiation, resolved) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
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
      "  is_instantiation = MAX(symbol.is_instantiation, excluded.is_instantiation)");
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
  ins.bind(5, std::string_view(kind));
  bind_opt(ins, 6, decl_file_id);
  bind_opt(ins, 7, decl_line);
  bind_opt(ins, 8, decl_col);
  bind_opt(ins, 9, decl_path);
  ins.bind(10, static_cast<int64_t>(is_instantiation ? 1 : 0));
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

int Storage::resolve_pass() {
  // Roll up edge.count for calls/uses from edge_site counts.
  rollup_edge_counts();
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
      s.symbols_by_kind[st.col_text(0)] = st.col_int64(1);
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
    args.emplace_back(*kind);
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

} // namespace cidx

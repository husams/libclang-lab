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
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    path  TEXT NOT NULL UNIQUE,
    kind  TEXT NOT NULL DEFAULT 'repo'
          CHECK (kind IN ('repo', 'external'))
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
    is_definition INTEGER NOT NULL DEFAULT 0,
    is_pure      INTEGER NOT NULL DEFAULT 0,
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

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '6');
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
constexpr std::array<std::string_view, 18> kSymbolInsertCols = {
    "usr",       "spelling",   "qual_name",     "display_name", "kind",
    "type_info", "file_id",    "line",          "col",          "decl_file_id",
    "decl_line", "decl_col",   "is_definition", "is_pure",      "linkage",
    "access",    "parent_usr", "resolved",
};

// Explicit SELECT lists (stable column positions even on migrated DBs).
constexpr char kComponentCols[] = "id, name, path, kind";
constexpr char kDirectoryCols[] = "id, component_id, path";
constexpr char kFileCols[] =
    "id, directory_id, name, mtime, md5, compile_options, driver, indexed, "
    "indexed_at";
constexpr char kSymbolCols[] =
    "id, usr, spelling, qual_name, display_name, kind, type_info, file_id, "
    "line, col, decl_file_id, decl_line, decl_col, is_definition, is_pure, "
    "linkage, access, parent_usr, resolved";
constexpr char kSymbolColsS[] =
    "s.id, s.usr, s.spelling, s.qual_name, s.display_name, s.kind, "
    "s.type_info, s.file_id, s.line, s.col, s.decl_file_id, s.decl_line, "
    "s.decl_col, s.is_definition, s.is_pure, s.linkage, s.access, "
    "s.parent_usr, s.resolved";

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
  return f;
}

Symbol symbol_from(const SqliteStmt &st) {
  Symbol s;
  s.id = st.col_int64(0);
  s.usr = st.col_text(1);
  s.spelling = st.col_text(2);
  s.qual_name = opt_text(st, 3);
  s.display_name = opt_text(st, 4);
  s.kind = st.col_text(5);
  s.type_info = opt_text(st, 6);
  s.file_id = opt_int64(st, 7);
  s.line = opt_int64(st, 8);
  s.col = opt_int64(st, 9);
  s.decl_file_id = opt_int64(st, 10);
  s.decl_line = opt_int64(st, 11);
  s.decl_col = opt_int64(st, 12);
  s.is_definition = st.col_int64(13) != 0;
  s.is_pure = st.col_int64(14) != 0;
  s.linkage = opt_text(st, 15);
  s.access = opt_text(st, 16);
  s.parent_usr = opt_text(st, 17);
  s.resolved = st.col_int64(18) != 0;
  return s;
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
  if (has_table("file")) {
    const auto fcols = table_columns("file");
    if (!has_col(fcols, "driver")) {
      // No backfill possible from stored data -- re-import to populate.
      db_.exec("ALTER TABLE file ADD COLUMN driver TEXT");
      changed = true;
    }
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
                               const std::string &kind) {
  const std::string abs = pathutil::abspath(path);
  auto st =
      db_.prepare("INSERT INTO component (name, path, kind) VALUES (?, ?, ?) "
                  "ON CONFLICT(path) DO UPDATE SET name = excluded.name, kind "
                  "= excluded.kind "
                  "RETURNING id");
  st.bind(1, std::string_view(name));
  st.bind(2, std::string_view(abs));
  st.bind(3, std::string_view(kind));
  if (!st.step()) {
    throw StorageError("component upsert returned no id");
  }
  const int64_t cid = st.col_int64(0);
  st.step_done();
  return cid;
}

std::optional<Component> Storage::get_component(const std::string &path) {
  auto st = db_.prepare(std::string("SELECT ") + kComponentCols +
                        " FROM component WHERE path = ?");
  st.bind(1, std::string_view(pathutil::abspath(path)));
  if (!st.step()) {
    return std::nullopt;
  }
  return component_from(st);
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
  auto st =
      db_.prepare(std::string("SELECT ") + kComponentCols + " FROM component");
  while (st.step()) {
    Component c = component_from(st);
    std::string root = c.path;
    while (!root.empty() && root.back() == '/') {
      root.pop_back(); // rstrip(os.sep)
    }
    const bool owns = abs == root || abs.starts_with(root + "/");
    if (owns && (!best || root.size() > best->path.size())) {
      best = std::move(c);
    }
  }
  return best;
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
                  "  compile_options = COALESCE(excluded.compile_options, "
                  "file.compile_options), "
                  "  driver          = COALESCE(excluded.driver, file.driver), "
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
  const std::string rel = pathutil::relpath(abs, comp->path);
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
      "f.driver, f.indexed, f.indexed_at "
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
  auto st =
      db_.prepare("SELECT c.path AS root, d.path AS rel, f.name AS name "
                  "FROM file f JOIN directory d ON d.id = f.directory_id "
                  "JOIN component c ON c.id = d.component_id WHERE f.id = ?");
  st.bind(1, file_id);
  if (!st.step()) {
    return std::nullopt;
  }
  return reconstruct_path(st.col_text(0), st.col_text(1), st.col_text(2));
}

std::vector<std::pair<File, std::string>>
Storage::list_files(const std::optional<int64_t> &component_id,
                    const std::optional<std::string> &dir_path,
                    const std::optional<std::string> &name,
                    const std::optional<bool> &indexed) {
  std::string sql =
      "SELECT f.id, f.directory_id, f.name, f.mtime, f.md5, f.compile_options, "
      "f.driver, f.indexed, f.indexed_at, c.path AS root, d.path AS rel "
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
    out.emplace_back(
        file_from(st),
        reconstruct_path(st.col_text(9), st.col_text(10), st.col_text(2)));
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
      "is_definition, is_pure, linkage, access, parent_usr, resolved) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
      "ON CONFLICT(usr) DO UPDATE SET "
      "  spelling      = excluded.spelling, "
      "  qual_name     = COALESCE(excluded.qual_name, symbol.qual_name), "
      "  display_name  = COALESCE(excluded.display_name, symbol.display_name), "
      "  kind          = excluded.kind, "
      "  type_info     = COALESCE(excluded.type_info, symbol.type_info), "
      "  file_id       = CASE WHEN excluded.is_definition >= "
      "symbol.is_definition "
      "                       THEN excluded.file_id ELSE symbol.file_id END, "
      "  line          = CASE WHEN excluded.is_definition >= "
      "symbol.is_definition "
      "                       THEN excluded.line ELSE symbol.line END, "
      "  col           = CASE WHEN excluded.is_definition >= "
      "symbol.is_definition "
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
  bind_opt(st, 15, sym.linkage);
  bind_opt(st, 16, sym.access);
  bind_opt(st, 17, sym.parent_usr);
  st.bind(18, static_cast<int64_t>(sym.resolved ? 1 : 0));
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
  auto st = db_.prepare(
      "SELECT kind, COUNT(*) AS n FROM symbol GROUP BY kind ORDER BY kind");
  while (st.step()) {
    s.symbols_by_kind[st.col_text(0)] = st.col_int64(1);
  }
  return s;
}

} // namespace cidx

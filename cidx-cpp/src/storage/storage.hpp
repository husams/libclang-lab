// SQLite persistence layer — byte/semantics-compatible port of
// indexer/storage.py (schema v6, design §4/§5.3).
//
// Connection sequence (G19: migration BEFORE the schema script, because the
// schema's indexes reference migrated columns):
//   mkdir -p dirname(path)  [skipped for :memory:]
//   open -> PRAGMA foreign_keys = ON -> migrate() -> schema script
//
// Every public mutator commits unless inside a Transaction (the SQLite C API
// autocommits per statement, which is exactly Python's _commit()-unless-in-txn
// contract once Transaction issues an explicit BEGIN). The upsert SQL is
// ported character-for-character from storage.py — semantics frozen by
// tests/storage_smoke_test.cpp (the executable spec, G13/G14).
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <tuple>
#include <utility>
#include <vector>

#include "storage/records.hpp"
#include "storage/sqlite.hpp"

namespace cidx {

constexpr int kSchemaVersion = 6;

// Allowed symbol.kind values (storage.py SYMBOL_KINDS) — enforced both by the
// SQL CHECK and by an application-side StorageError (§3.2).
bool is_symbol_kind(std::string_view kind);

class Storage;

// RAII transaction: BEGIN on construction, COMMIT on clean destruction,
// ROLLBACK when destroyed during exception unwind (Python _Transaction).
class Transaction {
public:
  explicit Transaction(Storage &db);
  ~Transaction();
  Transaction(const Transaction &) = delete;
  Transaction &operator=(const Transaction &) = delete;

  void commit();   // explicit early commit
  void rollback(); // explicit early rollback

private:
  Storage &db_;
  bool done_ = false;
  int uncaught_on_entry_;
};

class Storage {
public:
  explicit Storage(const std::string &path = ":memory:");

  // Batch many mutations into one commit (the documented 100x win):
  //   { auto txn = db.transaction(); ...; }   // commits at scope end
  Transaction transaction() { return Transaction(*this); }

  // -- components ------------------------------------------------------------
  int64_t add_component(const std::string &name, const std::string &path,
                        const std::string &kind = "repo");
  std::optional<Component> get_component(const std::string &path);
  std::optional<Component> get_component_by_name(const std::string &name);
  std::optional<Component> get_component_by_id(int64_t component_id);
  // Longest-prefix match computed app-side (G16); nested components resolve
  // to the deeper root.
  std::optional<Component> component_for_path(const std::string &abs_path);
  std::vector<Component>
  list_components(const std::optional<std::string> &name = std::nullopt,
                  const std::optional<std::string> &kind = std::nullopt);

  // -- directories -----------------------------------------------------------
  int64_t add_directory(int64_t component_id, const std::string &path);
  std::optional<Directory> get_directory(int64_t component_id,
                                         const std::string &path);
  std::optional<Directory> get_directory_by_id(int64_t directory_id);
  std::vector<std::pair<Directory, std::string>> // (row, component name)
  list_directories(const std::optional<int64_t> &component_id = std::nullopt,
                   const std::optional<std::string> &name = std::nullopt);

  // -- files -------------------------------------------------------------
  int64_t add_file(int64_t directory_id, const std::string &name,
                   const std::optional<double> &mtime = std::nullopt,
                   const std::optional<std::string> &md5 = std::nullopt,
                   const std::optional<std::vector<std::string>>
                       &compile_options = std::nullopt,
                   const std::optional<std::string> &driver = std::nullopt);
  // Throws StorageError when no component owns abs_path (add_component first).
  int64_t
  add_file_path(const std::string &abs_path,
                const std::optional<double> &mtime = std::nullopt,
                const std::optional<std::string> &md5 = std::nullopt,
                const std::optional<std::vector<std::string>> &compile_options =
                    std::nullopt,
                const std::optional<std::string> &driver = std::nullopt);
  std::optional<File> get_file(const std::string &abs_path);
  std::optional<File> get_file_by_id(int64_t file_id);
  std::optional<std::string> file_abs_path(int64_t file_id);
  std::vector<std::pair<File, std::string>> // (row, reconstructed abs path)
  list_files(const std::optional<int64_t> &component_id = std::nullopt,
             const std::optional<std::string> &dir_path = std::nullopt,
             const std::optional<std::string> &name = std::nullopt,
             const std::optional<bool> &indexed = std::nullopt);
  void mark_file_indexed(int64_t file_id,
                         const std::optional<double> &mtime = std::nullopt);
  bool is_file_indexed(const std::string &abs_path,
                       const std::optional<double> &mtime = std::nullopt,
                       const std::optional<std::string> &md5 = std::nullopt);

  // -- symbols -----------------------------------------------------------
  // Upsert keyed by USR; throws StorageError on a bad kind. Definition wins
  // over a stored declaration; a declaration never downgrades a definition.
  int64_t add_symbol(const Symbol &sym);
  // Update named columns of the symbol with this USR; false when absent.
  // Throws StorageError on unknown columns or a bad kind value (smoke parity).
  bool
  update_symbol(const std::string &usr,
                const std::vector<std::pair<std::string, SqlValue>> &values);
  std::optional<Symbol> lookup_symbol(const std::string &usr);
  std::optional<Symbol> lookup_symbol_by_id(int64_t symbol_id);
  std::vector<Symbol>
  lookup_symbols_by_name(const std::string &spelling,
                         const std::optional<std::string> &kind = std::nullopt);
  // '::'-segment fuzzy match on qual_name, ordered LENGTH(qual_name) first.
  std::vector<Symbol>
  search_symbols(const std::string &pattern,
                 const std::optional<std::string> &kind = std::nullopt);
  // Location scope matches definition OR declaration site (§3.5).
  std::vector<Symbol>
  list_symbols(const std::optional<int64_t> &component_id = std::nullopt,
               const std::optional<std::string> &dir_path = std::nullopt,
               const std::optional<int64_t> &file_id = std::nullopt,
               const std::optional<std::string> &name = std::nullopt,
               const std::optional<std::string> &kind = std::nullopt);
  std::vector<Symbol> symbols_in_file(int64_t file_id);
  std::vector<Symbol> unresolved_symbols();

  Stats stats();

  // Raw connection — exposed for tests (schema assertions on :memory: DBs)
  // and future maintenance commands. Not part of the indexing flow.
  SqliteDb &raw_db() { return db_; }

  // %c%c% char-in-order LIKE pattern with '\ % _' escaping (G18); public
  // statics so fuzzy_match_test can pin them directly.
  static std::string fuzzy_like(std::string_view text);
  // WHERE fragment matching a directory and its whole subtree; root '' -> '%'
  // (G17). Appends the two LIKE args to `args`.
  static std::string dir_scope_sql(const std::string &dir_path,
                                   std::vector<SqlValue> &args);

private:
  friend class Transaction;

  void migrate(); // column-presence detection, §4.1
  // (component_id, relative dir, file name) for an absolute path; nullopt
  // when no component owns it.
  std::optional<std::tuple<int64_t, std::string, std::string>>
  split_path(const std::string &abs_path);

  SqliteDb db_;
  bool in_txn_ = false;
};

} // namespace cidx

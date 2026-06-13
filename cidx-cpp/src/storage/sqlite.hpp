// Thin RAII wrappers over the SQLite3 C API (design D3).
// Smallest possible layer under storage/storage.hpp: no ORM, no statement
// cache. The C API autocommits each statement unless an explicit BEGIN is
// open, which is exactly the Python `_commit()`-unless-in-txn pattern once
// Transaction (storage.hpp) issues BEGIN/COMMIT/ROLLBACK.
//
// SQLite floor: >= 3.35 (RETURNING). Probed on the gcc-index-test box
// (192.168.1.115, Ubuntu 24.04: libsqlite3 3.45.1) — design §4.2: the
// RETURNING path is the ONLY path shipped; the ctor asserts the runtime
// library version.
#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <variant>

struct sqlite3;
struct sqlite3_stmt;

namespace cidx {

// One dynamically-typed SQL parameter / cell (update_symbol values, query
// args). Mirrors Python's None/int/float/str binding.
using SqlValue = std::variant<std::nullptr_t, int64_t, double, std::string>;

// Owns a sqlite3_stmt*. Movable, non-copyable. Bind indexes are 1-based,
// column indexes 0-based (SQLite convention).
class SqliteStmt {
public:
  SqliteStmt(sqlite3 *db, std::string_view sql); // throws StorageError
  ~SqliteStmt();
  SqliteStmt(SqliteStmt &&other) noexcept;
  SqliteStmt &operator=(SqliteStmt &&other) noexcept;
  SqliteStmt(const SqliteStmt &) = delete;
  SqliteStmt &operator=(const SqliteStmt &) = delete;

  void bind_null(int idx);
  void bind(int idx, int64_t value);
  void bind(int idx, double value);
  void bind(int idx, std::string_view value);
  void bind(int idx, const SqlValue &value);

  // true = SQLITE_ROW, false = SQLITE_DONE; throws StorageError otherwise.
  bool step();
  // Runs the statement to completion (e.g. DML with RETURNING).
  void step_done();

  bool col_is_null(int idx) const;
  int64_t col_int64(int idx) const;
  double col_double(int idx) const;
  std::string col_text(int idx) const; // NULL -> ""

private:
  sqlite3 *db_ = nullptr;
  sqlite3_stmt *stmt_ = nullptr;
};

// Owns a sqlite3*. Non-copyable, non-movable (Storage holds it by value).
class SqliteDb {
public:
  explicit SqliteDb(const std::string &path); // throws StorageError
  ~SqliteDb();
  SqliteDb(const SqliteDb &) = delete;
  SqliteDb &operator=(const SqliteDb &) = delete;

  SqliteStmt prepare(std::string_view sql);
  void exec(std::string_view sql_script); // multi-statement, throws on error
  int64_t changes() const;                // rows affected by the last DML
  sqlite3 *raw() { return db_; }

private:
  sqlite3 *db_ = nullptr;
};

} // namespace cidx

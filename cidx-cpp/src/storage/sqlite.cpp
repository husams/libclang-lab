#include "storage/sqlite.hpp"

#include <sqlite3.h>

#include <string>

#include "util/errors.hpp"

namespace cidx {

namespace {

[[noreturn]] void throw_db_error(sqlite3 *db, const std::string &context) {
  const char *msg = db ? sqlite3_errmsg(db) : "out of memory";
  throw StorageError(context + ": " + (msg ? msg : "unknown SQLite error"));
}

} // namespace

// -- SqliteStmt --------------------------------------------------------------

SqliteStmt::SqliteStmt(sqlite3 *db, std::string_view sql) : db_(db) {
  const int rc = sqlite3_prepare_v2(
      db_, sql.data(), static_cast<int>(sql.size()), &stmt_, nullptr);
  if (rc != SQLITE_OK) {
    throw_db_error(db_, "prepare failed for \"" + std::string(sql) + "\"");
  }
}

SqliteStmt::~SqliteStmt() { sqlite3_finalize(stmt_); }

SqliteStmt::SqliteStmt(SqliteStmt &&other) noexcept
    : db_(other.db_), stmt_(other.stmt_) {
  other.db_ = nullptr;
  other.stmt_ = nullptr;
}

SqliteStmt &SqliteStmt::operator=(SqliteStmt &&other) noexcept {
  if (this != &other) {
    sqlite3_finalize(stmt_);
    db_ = other.db_;
    stmt_ = other.stmt_;
    other.db_ = nullptr;
    other.stmt_ = nullptr;
  }
  return *this;
}

void SqliteStmt::bind_null(int idx) {
  if (sqlite3_bind_null(stmt_, idx) != SQLITE_OK) {
    throw_db_error(db_, "bind_null");
  }
}

void SqliteStmt::bind(int idx, int64_t value) {
  if (sqlite3_bind_int64(stmt_, idx, value) != SQLITE_OK) {
    throw_db_error(db_, "bind int64");
  }
}

void SqliteStmt::bind(int idx, double value) {
  if (sqlite3_bind_double(stmt_, idx, value) != SQLITE_OK) {
    throw_db_error(db_, "bind double");
  }
}

void SqliteStmt::bind(int idx, std::string_view value) {
  if (sqlite3_bind_text(stmt_, idx, value.data(),
                        static_cast<int>(value.size()),
                        SQLITE_TRANSIENT) != SQLITE_OK) {
    throw_db_error(db_, "bind text");
  }
}

void SqliteStmt::bind(int idx, const SqlValue &value) {
  if (std::holds_alternative<std::nullptr_t>(value)) {
    bind_null(idx);
  } else if (const auto *i = std::get_if<int64_t>(&value)) {
    bind(idx, *i);
  } else if (const auto *d = std::get_if<double>(&value)) {
    bind(idx, *d);
  } else {
    bind(idx, std::string_view(std::get<std::string>(value)));
  }
}

bool SqliteStmt::step() {
  const int rc = sqlite3_step(stmt_);
  if (rc == SQLITE_ROW) {
    return true;
  }
  if (rc == SQLITE_DONE) {
    return false;
  }
  throw_db_error(db_, "step");
}

void SqliteStmt::step_done() {
  while (step()) {
  }
}

bool SqliteStmt::col_is_null(int idx) const {
  return sqlite3_column_type(stmt_, idx) == SQLITE_NULL;
}

int64_t SqliteStmt::col_int64(int idx) const {
  return sqlite3_column_int64(stmt_, idx);
}

double SqliteStmt::col_double(int idx) const {
  return sqlite3_column_double(stmt_, idx);
}

std::string SqliteStmt::col_text(int idx) const {
  const unsigned char *text = sqlite3_column_text(stmt_, idx);
  if (text == nullptr) {
    return "";
  }
  const int len = sqlite3_column_bytes(stmt_, idx);
  return std::string(reinterpret_cast<const char *>(text),
                     static_cast<std::size_t>(len));
}

// -- SqliteDb ----------------------------------------------------------------

SqliteDb::SqliteDb(const std::string &path) {
  // Design §4.2: the RETURNING upserts are the only path shipped; refuse to
  // run against a pre-3.35 runtime with a clear message instead of a SQL
  // syntax error later.
  if (sqlite3_libversion_number() < 3035000) {
    throw StorageError(std::string("cidx requires SQLite >= 3.35 (RETURNING "
                                   "support); found ") +
                       sqlite3_libversion());
  }
  const int rc = sqlite3_open_v2(
      path.c_str(), &db_, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nullptr);
  if (rc != SQLITE_OK) {
    std::string msg = db_ ? sqlite3_errmsg(db_) : "out of memory";
    sqlite3_close(db_);
    db_ = nullptr;
    throw StorageError("cannot open database " + path + ": " + msg);
  }
}

SqliteDb::~SqliteDb() { sqlite3_close(db_); }

SqliteStmt SqliteDb::prepare(std::string_view sql) {
  return SqliteStmt(db_, sql);
}

void SqliteDb::exec(std::string_view sql_script) {
  char *err = nullptr;
  const int rc = sqlite3_exec(db_, std::string(sql_script).c_str(), nullptr,
                              nullptr, &err);
  if (rc != SQLITE_OK) {
    std::string msg = err ? err : "unknown SQLite error";
    sqlite3_free(err);
    throw StorageError("exec failed: " + msg);
  }
}

int64_t SqliteDb::changes() const {
  return sqlite3_changes(const_cast<sqlite3 *>(db_));
}

} // namespace cidx

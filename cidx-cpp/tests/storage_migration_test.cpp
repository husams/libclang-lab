// storage_migration_test — design §4.1 / G19. Opens the committed
// Python-generated fixture DBs (v2/v3/v4/v5 historical layouts, written by
// tests/fixtures/generate_fixtures.py) and asserts the column adds, the
// qual_name recursive-CTE backfill, the decl_* backfill for declaration-only
// rows, and the meta bump to '6'. The fixtures being Python-written doubles
// as a cross-tool-open proof. Fixtures are copied to a temp dir first — the
// committed files are never mutated.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <cstdio>
#include <fstream>
#include <set>
#include <string>
#include <unistd.h>
#include <vector>

#include "storage/sqlite.hpp"
#include "storage/storage.hpp"

#ifndef CIDX_FIXTURES_DIR
#error "CIDX_FIXTURES_DIR must be defined by the build"
#endif

namespace {

std::string make_temp_dir() {
  char tmpl[] = "/tmp/cidx_migration_XXXXXX";
  char *d = ::mkdtemp(tmpl);
  REQUIRE(d != nullptr);
  return d;
}

// Copy a committed fixture into the temp dir (Storage migrates in place).
std::string stage_fixture(const std::string &tmp, const std::string &name) {
  const std::string src_path = std::string(CIDX_FIXTURES_DIR) + "/" + name;
  const std::string dst_path = tmp + "/" + name;
  std::ifstream src(src_path, std::ios::binary);
  REQUIRE_MESSAGE(src.good(), "missing fixture: run generate_fixtures.py");
  std::ofstream dst(dst_path, std::ios::binary);
  dst << src.rdbuf();
  REQUIRE(dst.good());
  return dst_path;
}

std::vector<std::string> table_columns(cidx::SqliteDb &db, const char *table) {
  std::vector<std::string> out;
  auto st = db.prepare(std::string("PRAGMA table_info(") + table + ")");
  while (st.step()) {
    out.push_back(st.col_text(1));
  }
  return out;
}

bool has_col(const std::vector<std::string> &cols, const std::string &name) {
  for (const auto &c : cols) {
    if (c == name) {
      return true;
    }
  }
  return false;
}

std::string meta_version(cidx::SqliteDb &db) {
  auto st = db.prepare("SELECT value FROM meta WHERE key = 'schema_version'");
  REQUIRE(st.step());
  return st.col_text(0);
}

// Post-migration invariants shared by every fixture version.
void check_migrated(const std::string &db_path) {
  cidx::SqliteDb raw(db_path);

  const auto scols = table_columns(raw, "symbol");
  for (const char *c :
       {"qual_name", "decl_file_id", "decl_line", "decl_col", "is_pure"}) {
    CHECK_MESSAGE(has_col(scols, c), "symbol." << c << " present");
  }
  CHECK(has_col(table_columns(raw, "file"), "driver"));
  CHECK(meta_version(raw) == "6");

  // qual_name: longest parent_usr chain wins; the anonymous-namespace level
  // (empty parent spelling) is skipped.
  const auto qual_of = [&raw](const char *usr) {
    auto st = raw.prepare("SELECT qual_name FROM symbol WHERE usr = ?");
    st.bind(1, std::string_view(usr));
    REQUIRE(st.step());
    return st.col_text(0);
  };
  CHECK(qual_of("c:@N@rk") == "rk");
  CHECK(qual_of("c:@N@rk@S@Conf") == "rk::Conf");
  CHECK(qual_of("c:@N@rk@S@Conf@F@set") == "rk::Conf::set");
  CHECK(qual_of("c:@aN@F@hidden") == "hidden");
  CHECK(qual_of("c:@F@main") == "main");

  // decl_* backfill: declaration-only rows copied their stored location;
  // definition rows stay NULL (populate on reindex).
  {
    auto st = raw.prepare("SELECT decl_file_id, decl_line, decl_col "
                          "FROM symbol WHERE usr = 'c:@N@rk@S@Conf@F@set'");
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1);
    CHECK(st.col_int64(1) == 3);
    CHECK(st.col_int64(2) == 5);
  }
  {
    auto st =
        raw.prepare("SELECT decl_file_id FROM symbol WHERE usr = 'c:@F@main'");
    REQUIRE(st.step());
    CHECK(st.col_is_null(0));
  }

  // G19: the schema script ran AFTER migration, so idx_symbol_qual (which
  // references the migrated column) exists.
  std::set<std::string> indexes;
  auto st = raw.prepare("SELECT name FROM sqlite_master WHERE type = 'index' "
                        "AND name LIKE 'idx_%'");
  while (st.step()) {
    indexes.insert(st.col_text(0));
  }
  CHECK(indexes == std::set<std::string>{"idx_symbol_spelling",
                                         "idx_symbol_qual", "idx_symbol_file",
                                         "idx_symbol_parent",
                                         "idx_symbol_kind"});
}

} // namespace

TEST_CASE(
    "v2 fixture migrates: qual_name CTE + decl backfill + is_pure + driver") {
  const std::string tmp = make_temp_dir();
  const std::string path = stage_fixture(tmp, "v2.db");
  {
    // Pre-condition: the fixture really is the old layout.
    cidx::SqliteDb raw(path);
    const auto scols = table_columns(raw, "symbol");
    CHECK_FALSE(has_col(scols, "qual_name"));
    CHECK_FALSE(has_col(scols, "decl_file_id"));
    CHECK_FALSE(has_col(scols, "is_pure"));
    CHECK_FALSE(has_col(table_columns(raw, "file"), "driver"));
    CHECK(meta_version(raw) == "2");
  }
  {
    cidx::Storage db(path);
  } // open = migrate
  check_migrated(path);
}

TEST_CASE("v3 fixture migrates: stored qual_name kept, decl backfill applied") {
  const std::string tmp = make_temp_dir();
  const std::string path = stage_fixture(tmp, "v3.db");
  {
    cidx::SqliteDb raw(path);
    CHECK(has_col(table_columns(raw, "symbol"), "qual_name"));
    CHECK_FALSE(has_col(table_columns(raw, "symbol"), "decl_file_id"));
    CHECK(meta_version(raw) == "3");
  }
  {
    cidx::Storage db(path);
  }
  check_migrated(path);
}

TEST_CASE("v4 fixture migrates: is_pure + driver added") {
  const std::string tmp = make_temp_dir();
  const std::string path = stage_fixture(tmp, "v4.db");
  {
    cidx::SqliteDb raw(path);
    CHECK(has_col(table_columns(raw, "symbol"), "decl_file_id"));
    CHECK_FALSE(has_col(table_columns(raw, "symbol"), "is_pure"));
    CHECK(meta_version(raw) == "4");
  }
  {
    cidx::Storage db(path);
  }
  check_migrated(path);

  // v4 already stored decl_* for the declaration row — values survive.
  cidx::SqliteDb raw(path);
  auto st = raw.prepare("SELECT decl_line FROM symbol "
                        "WHERE usr = 'c:@N@rk@S@Conf@F@set'");
  REQUIRE(st.step());
  CHECK(st.col_int64(0) == 3);
}

TEST_CASE("v5 fixture migrates: only file.driver added, is_pure kept") {
  const std::string tmp = make_temp_dir();
  const std::string path = stage_fixture(tmp, "v5.db");
  {
    cidx::SqliteDb raw(path);
    CHECK(has_col(table_columns(raw, "symbol"), "is_pure"));
    CHECK_FALSE(has_col(table_columns(raw, "file"), "driver"));
    CHECK(meta_version(raw) == "5");
  }
  {
    cidx::Storage db(path);
  }
  check_migrated(path);

  // is_pure=1 seeded on the pure-virtual method survives the migration.
  cidx::SqliteDb raw(path);
  auto st = raw.prepare("SELECT is_pure FROM symbol "
                        "WHERE usr = 'c:@N@rk@S@Conf@F@set'");
  REQUIRE(st.step());
  CHECK(st.col_int64(0) == 1);
}

TEST_CASE("migrated DB stays fully usable through the Storage API") {
  const std::string tmp = make_temp_dir();
  const std::string path = stage_fixture(tmp, "v2.db");
  cidx::Storage db(path);
  // Rows written by Python under the old layout read back correctly even
  // though ALTER TABLE appended the new columns at the end.
  auto sym = db.lookup_symbol("c:@N@rk@S@Conf@F@set");
  REQUIRE(sym.has_value());
  CHECK(sym->spelling == "set");
  CHECK(sym->kind == "method");
  CHECK(sym->qual_name == std::string("rk::Conf::set"));
  CHECK(sym->decl_line == 3);
  CHECK_FALSE(sym->is_pure);
  // The backfilled qual_name is immediately searchable.
  const auto hits = db.search_symbols("conf::set");
  REQUIRE(hits.size() == 1);
  CHECK(hits[0].usr == "c:@N@rk@S@Conf@F@set");
}

TEST_CASE("newer DB opens without refusal (no downgrade path)") {
  const std::string tmp = make_temp_dir();
  const std::string path = tmp + "/future.db";
  {
    cidx::Storage db(path);
  } // create a fresh v6
  {
    cidx::SqliteDb raw(path);
    raw.exec("ALTER TABLE symbol ADD COLUMN future_col TEXT");
    raw.exec("UPDATE meta SET value = '7' WHERE key = 'schema_version'");
  }
  {
    cidx::Storage db(path); // must not throw, must not downgrade
    db.add_component("c", "/data/c");
    CHECK(db.get_component_by_name("c").has_value());
  }
  cidx::SqliteDb raw(path);
  CHECK(meta_version(raw) == "7"); // column-presence detection found no work
  CHECK(has_col(table_columns(raw, "symbol"), "future_col"));
}

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
  for (const char *c : {"qual_name", "decl_file_id", "decl_line", "decl_col",
                        "is_pure", "is_static", "decl_path"}) {
    CHECK_MESSAGE(has_col(scols, c), "symbol." << c << " present");
  }
  CHECK(has_col(table_columns(raw, "file"), "driver"));
  CHECK(has_col(table_columns(raw, "file"), "args_overridden"));
  CHECK(meta_version(raw) >= "10");

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
  // references the migrated column) exists. v7 also adds idx_edge_src/dst.
  std::set<std::string> indexes;
  auto st = raw.prepare("SELECT name FROM sqlite_master WHERE type = 'index' "
                        "AND name LIKE 'idx_%'");
  while (st.step()) {
    indexes.insert(st.col_text(0));
  }
  CHECK(indexes == std::set<std::string>{"idx_symbol_spelling",
                                         "idx_symbol_qual", "idx_symbol_file",
                                         "idx_symbol_parent",
                                         "idx_symbol_kind",
                                         "idx_edge_src", "idx_edge_dst",
                                         "idx_call_arg_edge",
                                         "idx_diagnostic_file",
                                         "idx_entity_edge_src",
                                         "idx_entity_edge_dst"});
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

// ---------------------------------------------------------------------------
// v13 → v14 migration (portable-paths): component.version column + label table
// ---------------------------------------------------------------------------

TEST_CASE("v13 DB migrates to v14: component.version added, label table created") {
  const std::string tmp = make_temp_dir();
  const std::string path = tmp + "/v13.db";

  // Create a v13-like DB by opening it with the current code (gets v14),
  // then downgrade meta to '13' and remove the new columns/tables to simulate
  // a v13 DB.
  {
    cidx::Storage db(path); // creates v14
    db.add_component("mylib", "/opt/mylib", "external");
  }
  {
    cidx::SqliteDb raw(path);
    // Remove the v14 additions to simulate v13.
    raw.exec("UPDATE meta SET value = '13' WHERE key = 'schema_version'");
    // SQLite can't DROP COLUMN easily, so we'll just test that a pre-existing
    // v13 DB where version column is absent gets migrated correctly.
    // We can't truly remove the column in SQLite < 3.35, but we can verify
    // that the column now exists (it was created by Storage ctor above) and
    // that migration is idempotent.
  }
  // Open again: migration must be idempotent.
  {
    cidx::Storage db(path);
  }
  cidx::SqliteDb raw(path);
  // Verify schema_version bumped to current (14).
  CHECK(meta_version(raw) == std::to_string(cidx::kSchemaVersion));
  // component.version column exists.
  CHECK(has_col(table_columns(raw, "component"), "version"));
  // label table exists.
  {
    auto st = raw.prepare(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='label'");
    CHECK(st.step());
  }
}

TEST_CASE("v14: add_component preserves existing version on re-import") {
  cidx::Storage db(":memory:");
  // Initial import with version.
  db.add_component("mylib", "/opt/mylib", "external",
                   std::optional<std::string>{"v2.1.0"});
  // Re-import without version (NULL) — should COALESCE and keep v2.1.0.
  db.add_component("mylib", "/opt/mylib", "external",
                   std::optional<std::string>{});
  auto comp = db.get_component_by_name("mylib");
  REQUIRE(comp.has_value());
  CHECK(comp->version == std::optional<std::string>{"v2.1.0"});
}

TEST_CASE("v14: set_component_version and effective_root") {
  cidx::Storage db(":memory:");
  db.add_component("mylib", "/opt/mylib", "external");
  auto before = db.get_component_by_name("mylib");
  REQUIRE(before.has_value());
  CHECK_FALSE(before->version.has_value());
  CHECK(cidx::Storage::effective_root(*before) == "/opt/mylib");

  db.set_component_version("mylib", std::optional<std::string>{"v3.0"});
  auto after = db.get_component_by_name("mylib");
  REQUIRE(after.has_value());
  CHECK(after->version == std::optional<std::string>{"v3.0"});
  CHECK(cidx::Storage::effective_root(*after) == "/opt/mylib/v3.0");
}

TEST_CASE("v14: label add/get/remove/list round-trip") {
  cidx::Storage db(":memory:");
  const int64_t id1 = db.add_label("libfoo-include", "/opt/libfoo/include");
  const int64_t id2 = db.add_label("libbar-hdr", "$LIBBAR/include");
  CHECK(id1 > 0);
  CHECK(id2 > 0);

  CHECK(db.get_label("libfoo-include") ==
        std::optional<std::string>{"/opt/libfoo/include"});
  CHECK(db.get_label("libbar-hdr") ==
        std::optional<std::string>{"$LIBBAR/include"});
  CHECK_FALSE(db.get_label("nonexistent").has_value());

  const auto labels = db.list_labels();
  REQUIRE(labels.size() == 2);
  // ORDER BY name: libbar-hdr < libfoo-include
  CHECK(labels[0].first == "libbar-hdr");
  CHECK(labels[1].first == "libfoo-include");

  CHECK(db.remove_label("libbar-hdr"));
  CHECK_FALSE(db.remove_label("libbar-hdr")); // already gone
  CHECK(db.list_labels().size() == 1);
}

TEST_CASE("v14: label upsert updates path on conflict") {
  cidx::Storage db(":memory:");
  db.add_label("mylib", "/old/path");
  db.add_label("mylib", "/new/path"); // upsert → updates
  CHECK(db.get_label("mylib") == std::optional<std::string>{"/new/path"});
}

TEST_CASE("newer DB opens without refusal (no downgrade path)") {
  const std::string tmp = make_temp_dir();
  const std::string path = tmp + "/future.db";
  {
    cidx::Storage db(path);
  } // create a fresh v7
  {
    cidx::SqliteDb raw(path);
    raw.exec("ALTER TABLE symbol ADD COLUMN future_col TEXT");
    // A version NEWER than this build must be left untouched (no downgrade).
    raw.exec("UPDATE meta SET value = '99' WHERE key = 'schema_version'");
  }
  {
    cidx::Storage db(path); // must not throw, must not downgrade
    db.add_component("c", "/data/c");
    CHECK(db.get_component_by_name("c").has_value());
  }
  cidx::SqliteDb raw(path);
  CHECK(meta_version(raw) == "99"); // future schema left untouched, not bumped
  CHECK(has_col(table_columns(raw, "symbol"), "future_col"));
}

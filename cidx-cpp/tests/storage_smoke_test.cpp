// storage_smoke_test — assertion-for-assertion PORT of
// project/indexer/_storage_smoke.py (the executable spec for design §3.4/§3.5,
// G13–G18). Same order as the Python file, including the reopen-persistence
// check and the update_symbol unknown-column / bad-kind throws. Extra
// TEST_CASEs pin the fresh schema-v6 shape (story acceptance) and the SQL
// CHECK rejection of a bad symbol kind (§3.2: rejected by BOTH layers).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <algorithm>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

#include "storage/records.hpp"
#include "storage/sqlite.hpp"
#include "storage/storage.hpp"
#include "util/errors.hpp"

namespace {

std::string make_temp_dir() {
  char tmpl[] = "/tmp/cidx_storage_XXXXXX";
  char *d = ::mkdtemp(tmpl);
  REQUIRE(d != nullptr);
  return d;
}

void makedirs(const std::string &path) {
  std::string cur;
  for (std::size_t i = 0; i <= path.size(); ++i) {
    if (i == path.size() || path[i] == '/') {
      if (!cur.empty()) {
        ::mkdir(cur.c_str(), 0755);
      }
    }
    if (i < path.size()) {
      cur += path[i];
    }
  }
}

std::vector<std::string> usrs_of(const std::vector<cidx::Symbol> &syms) {
  std::vector<std::string> out;
  for (const auto &s : syms) {
    out.push_back(s.usr);
  }
  return out;
}

} // namespace

TEST_CASE("storage smoke (port of _storage_smoke.py)") {
  const std::string tmp = make_temp_dir();
  const std::string repo = tmp + "/myrepo";
  makedirs(repo + "/src");
  const std::string db_path = tmp + "/index.db";

  cidx::Stats st;
  {
    cidx::Storage db(db_path);

    // -- components --------------------------------------------------
    const int64_t comp = db.add_component("myrepo", repo);
    CHECK_MESSAGE(db.add_component("myrepo", repo) == comp,
                  "idempotent on path");
    const int64_t ext = db.add_component("libc", "/usr/include", "external");
    CHECK(ext != comp);
    REQUIRE(db.get_component(repo).has_value());
    CHECK(db.get_component(repo)->name == "myrepo");
    REQUIRE(db.component_for_path(repo + "/src/a.c").has_value());
    CHECK(db.component_for_path(repo + "/src/a.c")->id == comp);

    // -- directories -------------------------------------------------
    const int64_t d_src = db.add_directory(comp, "src");
    CHECK_MESSAGE(db.add_directory(comp, "src") == d_src, "idempotent");
    db.add_directory(comp, ""); // d_root
    REQUIRE(db.get_directory(comp, "src").has_value());
    CHECK(db.get_directory(comp, "src")->id == d_src);

    // -- files ---------------------------------------------------------
    const std::vector<std::string> opts = {"-I.", "-DDEBUG"};
    const int64_t f1 =
        db.add_file(d_src, "a.c", 100.0, std::string("aaa"), opts);
    CHECK_MESSAGE(db.add_file(d_src, "a.c") == f1, "idempotent");
    const std::string a_c = repo + "/src/a.c";
    CHECK_MESSAGE(db.add_file_path(a_c) == f1,
                  "path convenience resolves to same row");
    REQUIRE(db.file_abs_path(f1).has_value());
    CHECK(*db.file_abs_path(f1) == a_c);

    auto rec = db.get_file(a_c);
    REQUIRE(rec.has_value());
    REQUIRE(rec->compile_options.has_value());
    CHECK_MESSAGE(*rec->compile_options == opts, "options round-trip");
    CHECK(rec->md5 == std::string("aaa"));
    CHECK_FALSE(rec->indexed);

    CHECK_MESSAGE(!db.is_file_indexed(a_c), "not indexed yet");
    db.mark_file_indexed(f1, 100.0);
    CHECK(db.is_file_indexed(a_c));
    CHECK_MESSAGE(db.is_file_indexed(a_c, 100.0), "fresh");
    CHECK_MESSAGE(!db.is_file_indexed(a_c, 200.0), "stale mtime -> reindex");
    CHECK_MESSAGE(db.is_file_indexed(a_c, std::nullopt, std::string("aaa")),
                  "same content");
    CHECK_MESSAGE(!db.is_file_indexed(a_c, std::nullopt, std::string("bbb")),
                  "changed content -> reindex");
    CHECK_MESSAGE(!db.is_file_indexed("/nowhere/else.c"), "unknown component");

    // re-import with a new md5 resets the indexed flag (G13)
    db.add_file(d_src, "a.c", std::nullopt, std::string("ccc"));
    CHECK_MESSAGE(!db.is_file_indexed(a_c), "content change clears indexed");
    db.mark_file_indexed(f1);
    CHECK(db.is_file_indexed(a_c));

    // -- symbols -------------------------------------------------------
    cidx::Symbol decl;
    decl.usr = "c:@F@multiply";
    decl.spelling = "multiply";
    decl.kind = "function";
    decl.type_info = "int (int, int)";
    decl.file_id = f1;
    decl.line = 3;
    decl.col = 5;
    decl.decl_file_id = f1;
    decl.decl_line = 3;
    decl.decl_col = 5;
    decl.is_definition = false;
    const int64_t sid = db.add_symbol(decl);
    REQUIRE(db.lookup_symbol("c:@F@multiply").has_value());
    CHECK_FALSE(db.lookup_symbol("c:@F@multiply")->is_definition);

    // definition upserts over the declaration (same USR, same row);
    // the declaration site recorded earlier survives alongside it
    cidx::Symbol defn;
    defn.usr = "c:@F@multiply";
    defn.spelling = "multiply";
    defn.kind = "function";
    defn.type_info = "int (int, int)";
    defn.file_id = f1;
    defn.line = 10;
    defn.col = 1;
    defn.is_definition = true;
    defn.resolved = true;
    CHECK_MESSAGE(db.add_symbol(defn) == sid, "USR upsert, not a new row");
    auto got = db.lookup_symbol("c:@F@multiply");
    REQUIRE(got.has_value());
    CHECK(got->is_definition);
    CHECK(got->resolved);
    CHECK(got->line == 10);
    CHECK_MESSAGE(got->decl_line == 3,
                  "decl site survives the definition upsert");

    // a later declaration must NOT downgrade the stored definition's location
    db.add_symbol(decl);
    got = db.lookup_symbol("c:@F@multiply");
    REQUIRE(got.has_value());
    CHECK_MESSAGE(got->line == 10, "definition wins");
    CHECK_MESSAGE(got->decl_line == 3, "decl site stays");

    // qual_name: stored, upsert-preserved, and fuzzy-searchable
    cidx::Symbol set_sym;
    set_sym.usr = "c:@N@rk@S@Conf@F@set";
    set_sym.spelling = "set";
    set_sym.kind = "method";
    set_sym.qual_name = "rk::Conf::set";
    set_sym.parent_usr = "c:@N@rk@S@Conf";
    set_sym.is_pure = true;
    set_sym.is_static = true;
    set_sym.resolved = true;
    db.add_symbol(set_sym);
    got = db.lookup_symbol("c:@N@rk@S@Conf@F@set");
    REQUIRE(got.has_value());
    CHECK(got->qual_name == std::string("rk::Conf::set"));
    CHECK_MESSAGE(got->is_pure, "is_pure round-trips");
    CHECK_MESSAGE(got->is_static, "is_static round-trips");
    cidx::Symbol set_again;
    set_again.usr = "c:@N@rk@S@Conf@F@set";
    set_again.spelling = "set";
    set_again.kind = "method";
    set_again.resolved = true;
    db.add_symbol(set_again);
    got = db.lookup_symbol("c:@N@rk@S@Conf@F@set");
    REQUIRE(got.has_value());
    CHECK_MESSAGE(got->qual_name == std::string("rk::Conf::set"),
                  "NULL must not clobber qual_name");
    CHECK_MESSAGE(usrs_of(db.search_symbols("conf::set")) ==
                      std::vector<std::string>{"c:@N@rk@S@Conf@F@set"},
                  "segment fuzzy match");
    CHECK(db.search_symbols("conf::set", std::string("function")).empty());
    CHECK(db.search_symbols("nosuchthing").empty());

    // update_symbol
    CHECK(db.update_symbol(
        "c:@F@multiply",
        {{"display_name", cidx::SqlValue{std::string("multiply(int, int)")}}}));
    CHECK(db.lookup_symbol("c:@F@multiply")->display_name ==
          std::string("multiply(int, int)"));
    CHECK_FALSE(db.update_symbol("c:@F@missing",
                                 {{"resolved", cidx::SqlValue{int64_t{1}}}}));
    CHECK_THROWS_AS(db.update_symbol("c:@F@multiply",
                                     {{"bogus", cidx::SqlValue{int64_t{1}}}}),
                    cidx::StorageError); // unknown column must raise
    {
      cidx::Symbol bad;
      bad.usr = "x";
      bad.spelling = "x";
      bad.kind = "not-a-kind";
      CHECK_THROWS_AS(db.add_symbol(bad),
                      cidx::StorageError); // unknown kind must raise
    }

    // name lookup returns every row with that spelling
    cidx::Symbol other_mul;
    other_mul.usr = "c:a.c@F@multiply";
    other_mul.spelling = "multiply";
    other_mul.kind = "function";
    other_mul.is_definition = true;
    db.add_symbol(other_mul);
    const auto hits = db.lookup_symbols_by_name("multiply");
    CHECK(hits.size() == 2);
    CHECK(std::all_of(hits.begin(), hits.end(), [](const cidx::Symbol &h) {
      return h.spelling == "multiply";
    }));
    CHECK(db.lookup_symbols_by_name("multiply", std::string("struct")).empty());

    // bulk insert inside one transaction (explicit commit required — R2)
    {
      auto txn = db.transaction();
      for (int i = 0; i < 50; ++i) {
        cidx::Symbol s;
        s.usr = "c:@S@T" + std::to_string(i);
        s.spelling = "T" + std::to_string(i);
        s.kind = "struct";
        s.resolved = true;
        db.add_symbol(s);
      }
      txn.commit();
    }

    // unresolved + per-file views
    {
      const auto unresolved_usrs = usrs_of(db.unresolved_symbols());
      const std::set<std::string> got_usrs(unresolved_usrs.begin(),
                                           unresolved_usrs.end());
      CHECK(got_usrs == std::set<std::string>{"c:a.c@F@multiply"});
    }
    CHECK(usrs_of(db.symbols_in_file(f1)) ==
          std::vector<std::string>{"c:@F@multiply"});

    // -- by-id getters -----------------------------------------------------
    REQUIRE(db.get_component_by_id(comp).has_value());
    CHECK(db.get_component_by_id(comp)->name == "myrepo");
    CHECK_FALSE(db.get_component_by_id(99999).has_value());
    REQUIRE(db.get_directory_by_id(d_src).has_value());
    CHECK(db.get_directory_by_id(d_src)->path == "src");
    CHECK(db.get_directory_by_id(d_src)->component_id == comp);
    CHECK_FALSE(db.get_directory_by_id(99999).has_value());
    REQUIRE(db.get_file_by_id(f1).has_value());
    CHECK(db.get_file_by_id(f1)->name == "a.c");
    CHECK(db.get_file_by_id(f1)->directory_id == d_src);
    CHECK_FALSE(db.get_file_by_id(99999).has_value());

    // -- list views --------------------------------------------------------
    {
      std::vector<std::string> names;
      for (const auto &c : db.list_components()) {
        names.push_back(c.name);
      }
      CHECK(names == std::vector<std::string>{"libc", "myrepo"});
    }
    {
      std::vector<std::string> names;
      for (const auto &c : db.list_components(std::string("myrp"))) {
        names.push_back(c.name);
      }
      CHECK_MESSAGE(names == std::vector<std::string>{"myrepo"},
                    "fuzzy: chars in order");
    }
    {
      std::vector<std::string> names;
      for (const auto &c :
           db.list_components(std::nullopt, std::string("external"))) {
        names.push_back(c.name);
      }
      CHECK(names == std::vector<std::string>{"libc"});
    }
    CHECK(db.list_components(std::string("zzz")).empty());

    {
      const auto dirs = db.list_directories(comp);
      std::vector<std::pair<std::string, std::string>> got_dirs;
      for (const auto &[d, n] : dirs) {
        got_dirs.emplace_back(d.path, n);
      }
      CHECK(got_dirs == std::vector<std::pair<std::string, std::string>>{
                            {"", "myrepo"}, {"src", "myrepo"}});
    }
    {
      std::vector<std::string> paths;
      for (const auto &[d, n] :
           db.list_directories(std::nullopt, std::string("sr"))) {
        (void)n;
        paths.push_back(d.path);
      }
      CHECK(paths == std::vector<std::string>{"src"});
    }

    const auto paths_of =
        [](const std::vector<std::pair<cidx::File, std::string>> &rows) {
          std::vector<std::string> out;
          for (const auto &[f, p] : rows) {
            (void)f;
            out.push_back(p);
          }
          return out;
        };
    CHECK(paths_of(db.list_files(comp)) == std::vector<std::string>{a_c});
    CHECK(paths_of(db.list_files(comp, std::string("src"))) ==
          std::vector<std::string>{a_c});
    CHECK_MESSAGE(paths_of(db.list_files(comp, std::string(""))) ==
                      std::vector<std::string>{a_c},
                  "root subtree covers everything");
    CHECK(db.list_files(comp, std::string("other")).empty());
    CHECK_MESSAGE(paths_of(db.list_files(std::nullopt, std::nullopt,
                                         std::string("ac"))) ==
                      std::vector<std::string>{a_c},
                  "fuzzy name");
    CHECK(
        db.list_files(std::nullopt, std::nullopt, std::nullopt, false).empty());
    CHECK(paths_of(db.list_files(std::nullopt, std::nullopt, std::nullopt,
                                 true)) == std::vector<std::string>{a_c});

    CHECK_MESSAGE(usrs_of(db.list_symbols(comp)) ==
                      std::vector<std::string>{"c:@F@multiply"},
                  "scoped by definition/declaration site");
    CHECK(usrs_of(db.list_symbols(comp, std::string("src"))) ==
          std::vector<std::string>{"c:@F@multiply"});
    CHECK(usrs_of(db.list_symbols(std::nullopt, std::nullopt, f1)) ==
          std::vector<std::string>{"c:@F@multiply"});
    CHECK_MESSAGE(
        usrs_of(db.list_symbols(std::nullopt, std::nullopt, std::nullopt,
                                std::string("cfset"))) ==
            std::vector<std::string>{"c:@N@rk@S@Conf@F@set"},
        "fuzzy hits the qualified name");
    CHECK(db.list_symbols(std::nullopt, std::nullopt, std::nullopt,
                          std::nullopt, std::string("struct"))
              .size() == 50);
    CHECK(db.list_symbols(comp, std::nullopt, std::nullopt, std::nullopt,
                          std::string("struct"))
              .empty());

    // -- stats -----------------------------------------------------------
    st = db.stats();
    CHECK(st.components == 2);
    CHECK(st.files == 1);
    CHECK(st.files_indexed == 1);
    CHECK(st.symbols == 53);
    CHECK(st.symbols_by_kind == std::map<std::string, int64_t>{{"function", 2},
                                                               {"method", 1},
                                                               {"struct", 50}});
    CHECK(st.symbols_unresolved == 1);
  }

  // data survives reopen
  {
    cidx::Storage db(db_path);
    REQUIRE(db.lookup_symbol("c:@F@multiply").has_value());
    CHECK(db.lookup_symbol("c:@F@multiply")->display_name ==
          std::string("multiply(int, int)"));
  }

  // §3.2 (v16): the SQL CHECK on symbol.kind was dropped (kind is now an
  // INTEGER == CXCursorKind); validation is app-side only (the add_symbol throw
  // was asserted above). A raw insert bypassing add_symbol is no longer
  // rejected by a constraint.
  {
    cidx::SqliteDb raw(db_path);
    auto ok = raw.prepare(
        "INSERT INTO symbol (usr, spelling, kind) VALUES ('y', 'y', 8)");
    CHECK_NOTHROW(ok.step_done());
  }
}

TEST_CASE("fresh Storage produces schema v19 (file-backed and :memory:)") {
  // :memory: exercises the skip-mkdir branch; raw_db() lets us assert the
  // schema shape on the same connection.
  cidx::Storage db(":memory:");
  auto &raw = db.raw_db();

  // tables — v7 adds edge_kind, edge, edge_site, template_param, template_arg
  std::set<std::string> tables;
  {
    auto st = raw.prepare("SELECT name FROM sqlite_master WHERE type = 'table' "
                          "AND name NOT LIKE 'sqlite_%'");
    while (st.step()) {
      tables.insert(st.col_text(0));
    }
  }
  // v14 adds label; v15 adds diagnostic; v17 adds entity_edge + entity_edge_kind
  CHECK(tables == std::set<std::string>{"meta", "component", "directory",
                                        "file", "symbol", "symbol_kind",
                                        "edge_kind", "edge", "edge_site",
                                        "template_param", "template_arg",
                                        "call_arg", "label", "diagnostic",
                                        "entity_edge_kind", "entity_edge"});

  // columns, in declared order (byte-compatible v6 layout)
  const auto cols = [&raw](const char *table) {
    std::vector<std::string> out;
    auto st = raw.prepare(std::string("PRAGMA table_info(") + table + ")");
    while (st.step()) {
      out.push_back(st.col_text(1));
    }
    return out;
  };
  CHECK(cols("meta") == std::vector<std::string>{"key", "value"});
  // v14 adds the version column to component
  CHECK(cols("component") ==
        std::vector<std::string>{"id", "name", "path", "kind", "version"});
  CHECK(cols("directory") ==
        std::vector<std::string>{"id", "component_id", "path"});
  CHECK(cols("file") == std::vector<std::string>{"id", "directory_id", "name",
                                                 "mtime", "md5",
                                                 "compile_options", "driver",
                                                 "indexed", "indexed_at",
                                                 "args_overridden"});
  CHECK(cols("symbol") == std::vector<std::string>{
                              "id", "usr", "spelling", "qual_name",
                              "display_name", "kind", "type_info", "file_id",
                              "line", "col", "decl_file_id", "decl_line",
                              "decl_col", "decl_path", "is_definition",
                              "is_pure", "is_static", "is_instantiation",
                              "is_named_instance",
                              "linkage", "access", "parent_usr", "resolved"});

  // the indexes (5 symbol + 2 edge + 1 call_arg + 1 diagnostic)
  std::set<std::string> indexes;
  {
    auto st = raw.prepare("SELECT name FROM sqlite_master WHERE type = 'index' "
                          "AND name LIKE 'idx_%'");
    while (st.step()) {
      indexes.insert(st.col_text(0));
    }
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

  // meta row + pragma parity (D25: foreign_keys ON, default journal mode)
  {
    auto st =
        raw.prepare("SELECT value FROM meta WHERE key = 'schema_version'");
    REQUIRE(st.step());
    CHECK(st.col_text(0) == "20");
  }
  {
    auto st = raw.prepare("PRAGMA foreign_keys");
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1);
  }

  // FK actions are part of the byte-compatible DDL: spot-check via the stored
  // SQL text of the symbol table.
  {
    auto st = raw.prepare("SELECT sql FROM sqlite_master WHERE type = 'table' "
                          "AND name = 'symbol'");
    REQUIRE(st.step());
    const std::string ddl = st.col_text(0);
    CHECK(ddl.find("ON DELETE SET NULL") != std::string::npos);
    // v16: kind is an INTEGER (CXCursorKind); the old name CHECK list is gone.
    CHECK(ddl.find("kind         INTEGER NOT NULL") != std::string::npos);
  }
  {
    auto st = raw.prepare(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'file'");
    REQUIRE(st.step());
    CHECK(st.col_text(0).find("ON DELETE CASCADE") != std::string::npos);
  }

  // all 17 kinds pass the CHECK (incl. the unreachable-but-stored 'macro')
  for (const char *kind :
       {"class", "struct", "union", "function", "method", "member",
        "constructor", "destructor", "enum", "enum-constant", "typedef",
        "type-alias", "class-template", "function-template", "variable",
        "namespace", "macro"}) {
    cidx::Symbol s;
    s.usr = std::string("c:kind@") + kind;
    s.spelling = "k";
    s.kind = kind;
    CHECK_NOTHROW(db.add_symbol(s));
  }
}

TEST_CASE("transaction rolls back on exception, commits on clean exit") {
  cidx::Storage db(":memory:");
  const int64_t comp = db.add_component("c", "/data/c");
  const int64_t dir = db.add_directory(comp, "");

  // rollback path: the Transaction dtor runs during unwind
  try {
    auto txn = db.transaction();
    db.add_file(dir, "rolled-back.c");
    throw std::runtime_error("boom");
  } catch (const std::runtime_error &) {
  }
  CHECK(db.list_files().empty());

  // commit path: explicit txn.commit() required (R2: dtor is rollback-only)
  {
    auto txn = db.transaction();
    db.add_file(dir, "kept.c");
    txn.commit();
  }
  CHECK(db.list_files().size() == 1);

  // explicit early commit
  {
    auto txn = db.transaction();
    db.add_file(dir, "kept2.c");
    txn.commit();
  }
  CHECK(db.list_files().size() == 2);
}

TEST_CASE("commit() propagates failure — not silently swallowed (R2)") {
  // Regression: before R2, Transaction::~Transaction swallowed all COMMIT
  // errors; a disk-full or busy COMMIT would return success and leave the DB
  // in an inconsistent state. Now ~Transaction is rollback-only; callers must
  // call txn.commit() explicitly, and a failing commit() throws.
  //
  // We trigger a synthetic COMMIT failure by manually issuing a ROLLBACK on
  // the underlying connection (bypassing Transaction::done_). SQLite then
  // returns SQLITE_ERROR ("cannot commit - no transaction is active") on the
  // subsequent COMMIT — identical in kind to a SQLITE_FULL / SQLITE_IOERR that
  // would occur on disk full.
  cidx::Storage db(":memory:");
  const int64_t comp = db.add_component("c2", "/data/c2");
  const int64_t dir = db.add_directory(comp, "");

  {
    auto txn = db.transaction();
    db.add_file(dir, "will-not-be-kept.c");

    // Forcibly roll back the underlying SQLite transaction, simulating a
    // COMMIT-time failure (disk full / I/O error) without relying on OS state.
    db.raw_db().exec("ROLLBACK");

    // txn.commit() must now throw, not silently succeed.
    CHECK_THROWS_AS(txn.commit(), cidx::StorageError);
    // txn falls out of scope; ~Transaction sees done_=false but no active
    // transaction — the ROLLBACK in the dtor is harmless (SQLite ignores it).
  }

  // The file must NOT have been persisted (the underlying txn was rolled back).
  CHECK(db.list_files().empty());

  // After the failed commit, the Storage object must still be usable.
  {
    auto txn2 = db.transaction();
    db.add_file(dir, "recovery.c");
    txn2.commit();
  }
  CHECK(db.list_files().size() == 1);
}

// delete_component (import --force): the component, its directories and files
// (ON DELETE CASCADE) and every symbol indexed from those files (explicit --
// symbol file refs are ON DELETE SET NULL) must vanish, leaving other
// components fully intact.
TEST_CASE("diagnostics: replace/get/counts, refresh, locationless, cascade") {
  cidx::Storage db(":memory:");
  const int64_t comp = db.add_component("d", "/repo/d");
  const int64_t dir = db.add_directory(comp, "");
  const int64_t fid = db.add_file(dir, "a.c");

  auto mk = [](int sev, std::string spelling,
               std::optional<std::string> path, std::optional<int64_t> line,
               std::optional<int64_t> col) {
    cidx::Diagnostic d;
    d.severity = sev;
    d.spelling = std::move(spelling);
    d.file_path = std::move(path);
    d.line = line;
    d.col = col;
    return d;
  };

  // Round-trip in TU (insertion) order; counts grouped by severity.
  db.replace_diagnostics(
      fid, {mk(2, "unused 'x'", std::string("/r/a.c"), 3, 5),
            mk(3, "implicit decl", std::string("/r/a.c"), 7, 1),
            mk(2, "shadow", std::string("/r/a.c"), 9, 2)});
  auto got = db.get_diagnostics(fid);
  REQUIRE(got.size() == 3);
  CHECK(got[0].severity == 2);
  CHECK(got[0].spelling == "unused 'x'");
  CHECK(got[0].line == 3);
  CHECK(got[1].severity == 3);
  CHECK(got[0].id < got[1].id); // ids follow TU order
  CHECK(db.diagnostic_counts() ==
        std::map<int64_t, std::map<int, int64_t>>{{fid, {{2, 2}, {3, 1}}}});

  // Locationless diagnostic stores NULL file_path/line/col.
  db.replace_diagnostics(
      fid, {mk(2, "linker input unused", std::nullopt, std::nullopt,
               std::nullopt)});
  got = db.get_diagnostics(fid);
  REQUIRE(got.size() == 1); // wholesale refresh dropped the old three
  CHECK_FALSE(got[0].file_path.has_value());
  CHECK_FALSE(got[0].line.has_value());
  CHECK_FALSE(got[0].col.has_value());

  // Re-index of a now-clean file drops every row.
  db.replace_diagnostics(fid, {});
  CHECK(db.get_diagnostics(fid).empty());
  CHECK(db.diagnostic_counts().empty());

  // ON DELETE CASCADE: deleting the file removes its diagnostics.
  db.replace_diagnostics(fid, {mk(3, "e", std::nullopt, std::nullopt,
                                  std::nullopt)});
  db.delete_file(fid);
  CHECK(db.diagnostic_counts().empty());
}

TEST_CASE("delete_component removes files (cascade) and symbols (explicit)") {
  cidx::Storage db(":memory:");

  // Component A: one file, one symbol defined+declared in it.
  const int64_t a = db.add_component("a", "/repo/a");
  const int64_t da = db.add_directory(a, "");
  const int64_t fa = db.add_file(da, "a.c");
  cidx::Symbol sa;
  sa.usr = "c:@F@a_fn";
  sa.spelling = "a_fn";
  sa.kind = "function";
  sa.file_id = fa;
  sa.decl_file_id = fa;
  db.add_symbol(sa);

  // Component B: untouched bystander with its own file + symbol.
  const int64_t b = db.add_component("b", "/repo/b");
  const int64_t dbdir = db.add_directory(b, "");
  const int64_t fb = db.add_file(dbdir, "b.c");
  cidx::Symbol sb;
  sb.usr = "c:@F@b_fn";
  sb.spelling = "b_fn";
  sb.kind = "function";
  sb.file_id = fb;
  sb.decl_file_id = fb;
  db.add_symbol(sb);

  // Cross symbol: defined in B but DECLARED in A's file -> the decl_file_id
  // match means it is "related" to A and is removed when A is deleted.
  cidx::Symbol cross;
  cross.usr = "c:@F@cross";
  cross.spelling = "cross";
  cross.kind = "function";
  cross.file_id = fb;
  cross.decl_file_id = fa;
  db.add_symbol(cross);

  db.delete_component(a);

  CHECK_FALSE(db.get_component("/repo/a").has_value());
  CHECK_FALSE(db.get_file("/repo/a/a.c").has_value());
  CHECK_MESSAGE(!db.lookup_symbol("c:@F@a_fn").has_value(),
                "A's symbol deleted, not orphaned");
  CHECK_MESSAGE(!db.lookup_symbol("c:@F@cross").has_value(),
                "decl-site-in-A symbol deleted too");

  CHECK(db.get_component("/repo/b").has_value());
  REQUIRE(db.get_file("/repo/b/b.c").has_value());
  CHECK_MESSAGE(db.lookup_symbol("c:@F@b_fn").has_value(),
                "bystander component B untouched");
}

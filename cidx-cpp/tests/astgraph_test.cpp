// astgraph_test — the per-TU AST->SQLite graph dumper (src/astgraph/).
// All cases perform real parses on temp-dir sources, live in doctest suite
// "clang", and runtime-SKIP (exit 77) when no libclang loads — the same
// pattern as ast_test. Assertions run plain SQL over the produced <TU>.db,
// i.e. exactly what a Soufflé program would read.
#define DOCTEST_CONFIG_IMPLEMENT
#include "doctest/doctest.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <string>
#include <vector>

#include "astgraph/astgraph.hpp"
#include "clangx/libclang.hpp"
#include "clangx/parse.hpp"
#include "clangx/toolchain.hpp"
#include "storage/sqlite.hpp"
#include "util/errors.hpp"

namespace fs = std::filesystem;
using cidx::LibClang;
using cidx::ParsedTu;
using cidx::Parser;
using cidx::SqliteDb;
using cidx::SqliteStmt;
using cidx::Toolchain;
namespace ag = cidx::astgraph;

namespace {

bool g_clang_skipped = false;

LibClang *require_libclang() {
  LibClang &lib = LibClang::instance();
  try {
    lib.load();
  } catch (const cidx::CidxError &e) {
    g_clang_skipped = true;
    MESSAGE("SKIP: no loadable libclang: " << std::string(e.what()));
    return nullptr;
  }
  return &lib;
}

std::string make_temp_dir() {
  char tmpl[] = "/tmp/cidx_astgraph_XXXXXX";
  char *d = ::mkdtemp(tmpl);
  REQUIRE(d != nullptr);
  return d;
}

void write_file(const std::string &path, const std::string &content) {
  fs::create_directories(fs::path(path).parent_path());
  std::ofstream out(path, std::ios::binary);
  REQUIRE(out.good());
  out << content;
}

int64_t q_int(SqliteDb &db, const std::string &sql) {
  SqliteStmt stmt = db.prepare(sql);
  REQUIRE(stmt.step());
  return stmt.col_int64(0);
}

std::string q_text(SqliteDb &db, const std::string &sql) {
  SqliteStmt stmt = db.prepare(sql);
  REQUIRE(stmt.step());
  return stmt.col_text(0);
}

// Parse `content` as `name` in a fresh temp dir and dump it; returns the
// dump DB path (kept alive by the returned temp dir string pair).
struct Dumped {
  std::string dir;
  std::string db_path;
  ag::DumpStats stats;
};

Dumped dump_source(const std::string &name, const std::string &content,
                   const std::vector<std::string> &args, bool main_only) {
  Dumped out;
  out.dir = make_temp_dir();
  const std::string src = out.dir + "/" + name;
  write_file(src, content);
  Toolchain toolchain;
  Parser parser(toolchain);
  const ParsedTu tu = parser.parse(src, args, std::nullopt);
  out.db_path = out.dir + "/" + name + ".db";
  ag::Options opts;
  opts.main_only = main_only;
  out.stats = ag::dump_tu(tu, out.db_path, opts, src, args, std::nullopt);
  return out;
}

constexpr const char *kCppSample = R"cpp(
namespace zoo {
struct Base {
  virtual int rank() const { return 0; }
  virtual ~Base() = default;
};
struct Derived : Base {
  int rank() const override { return 1; }
};
typedef Base *BasePtr;
int probe(const Derived &d);
int probe(const Derived &d) { return d.rank(); }
} // namespace zoo
int main() { zoo::Derived d; return zoo::probe(d); }
)cpp";

} // namespace

TEST_SUITE("clang") {

TEST_CASE("astgraph: schema, catalogs and no-NULL sentinels") {
  if (require_libclang() == nullptr)
    return;
  const Dumped d =
      dump_source("sample.cpp", kCppSample, {"-std=c++17"}, false);
  CHECK(d.stats.cursor_nodes > 0);
  CHECK(d.stats.type_nodes > 0);
  CHECK(d.stats.edges > 0);
  CHECK(d.stats.symbols > 0);

  SqliteDb db(d.db_path);
  // meta provenance
  CHECK(q_text(db, "SELECT value FROM meta WHERE key='schema_version'") ==
        std::to_string(ag::kSchemaVersion));
  CHECK(q_text(db, "SELECT value FROM meta WHERE key='main_only'") == "0");
  // fixed relation catalog: 19 rows, ids 1..19
  CHECK(q_int(db, "SELECT COUNT(*) FROM relation_kind") == 19);
  CHECK(q_int(db, "SELECT MAX(id) FROM relation_kind") == ag::kRelClassType);
  CHECK(q_text(db, "SELECT name FROM relation_kind WHERE id=1") == "child");
  // node_kind namespacing: cursor kinds < 1000, type kinds >= 1000
  CHECK(q_int(db, "SELECT COUNT(*) FROM node_kind WHERE id < 1000") > 0);
  CHECK(q_int(db,
              "SELECT COUNT(*) FROM node_kind WHERE id >= 1000 AND "
              "category != 'type'") == 0);
  // Soufflé contract: no NULL anywhere edges point at real nodes
  CHECK(q_int(db,
              "SELECT COUNT(*) FROM edge e LEFT JOIN node s ON s.id=e.src_id "
              "LEFT JOIN node t ON t.id=e.dst_id "
              "WHERE s.id IS NULL OR t.id IS NULL") == 0);
  CHECK(q_int(db, "SELECT COUNT(*) FROM node WHERE symbol_id != 0 AND "
                  "symbol_id NOT IN (SELECT id FROM symbol)") == 0);
  // stats match the tables
  CHECK(q_int(db, "SELECT COUNT(*) FROM node") ==
        d.stats.cursor_nodes + d.stats.type_nodes);
  CHECK(q_int(db, "SELECT COUNT(*) FROM edge") == d.stats.edges);
  CHECK(q_int(db, "SELECT COUNT(*) FROM symbol") == d.stats.symbols);
}

TEST_CASE("astgraph: semantic cross-reference edges are present") {
  if (require_libclang() == nullptr)
    return;
  const Dumped d =
      dump_source("sample.cpp", kCppSample, {"-std=c++17"}, false);
  SqliteDb db(d.db_path);

  // overrides: Derived::rank -> Base::rank (both named 'rank')
  CHECK(q_int(db,
              "SELECT COUNT(*) FROM edge e "
              "JOIN node s ON s.id=e.src_id JOIN node t ON t.id=e.dst_id "
              "WHERE e.rel_id=8 AND s.spelling='rank' AND "
              "t.spelling='rank'") == 1);
  // definition: probe prototype -> probe definition, distinct nodes
  CHECK(q_int(db,
              "SELECT COUNT(*) FROM edge e "
              "JOIN node s ON s.id=e.src_id JOIN node t ON t.id=e.dst_id "
              "WHERE e.rel_id=3 AND s.spelling='probe' AND "
              "t.spelling='probe' AND s.id != t.id AND "
              "s.is_definition=0 AND t.is_definition=1") >= 1);
  // has_type: every VarDecl carries a type edge to a type-space node
  CHECK(q_int(db,
              "SELECT COUNT(*) FROM node v "
              "WHERE v.kind_id=9 AND NOT EXISTS (" // 9 = VAR_DECL
              "  SELECT 1 FROM edge e JOIN node t ON t.id=e.dst_id "
              "  WHERE e.src_id=v.id AND e.rel_id=9 AND "
              "        t.kind_id >= 1000)") == 0);
  // underlying_type: the BasePtr typedef points at a pointer type node
  CHECK(q_int(db,
              "SELECT COUNT(*) FROM edge e "
              "JOIN node s ON s.id=e.src_id JOIN node t ON t.id=e.dst_id "
              "WHERE e.rel_id=17 AND s.spelling='BasePtr' AND "
              "t.kind_id >= 1000") == 1);
  // symbols dedupe by USR: exactly one STRUCT_DECL symbol named Derived
  // (a second 'Derived' symbol exists — the implicit constructor's USR)
  CHECK(q_int(db, "SELECT COUNT(*) FROM symbol WHERE name='Derived' AND "
                  "kind_id=2") == 1); // 2 = STRUCT_DECL

  // Datalog shape check: the recursive child-closure from the TU root (the
  // exact query a Soufflé `ancestor` rule computes) reaches every node that
  // has an incoming child edge.
  const int64_t reachable = q_int(
      db, "WITH RECURSIVE desc(id) AS ("
          "  SELECT dst_id FROM edge WHERE rel_id=1 AND src_id=1 "
          "  UNION SELECT e.dst_id FROM edge e JOIN desc d "
          "    ON e.src_id=d.id WHERE e.rel_id=1) "
          "SELECT COUNT(*) FROM desc");
  CHECK(reachable ==
        q_int(db, "SELECT COUNT(DISTINCT dst_id) FROM edge WHERE rel_id=1"));
  CHECK(reachable > 20);
}

TEST_CASE("astgraph: --main-only prunes header subtrees, keeps referenced "
          "decls shallow") {
  if (require_libclang() == nullptr)
    return;
  const std::string dir = make_temp_dir();
  write_file(dir + "/helper.hpp",
             "#pragma once\nint helper_fn(int x);\ninline int unused_fn(int "
             "y) { return y * 2; }\n");
  const std::string content = "#include \"helper.hpp\"\n"
                              "int use(void) { return helper_fn(1); }\n";
  const std::vector<std::string> args = {"-std=c++17", "-I" + dir};

  const Dumped full = dump_source("use_full.cpp", content, args, false);
  const Dumped pruned = dump_source("use_main.cpp", content, args, true);

  SqliteDb fdb(full.db_path);
  SqliteDb pdb(pruned.db_path);
  CHECK(q_text(pdb, "SELECT value FROM meta WHERE key='main_only'") == "1");
  // pruning must shrink the dump
  CHECK(pruned.stats.cursor_nodes < full.stats.cursor_nodes);
  // full walk materializes unused_fn structurally; the pruned one must not
  CHECK(q_int(fdb, "SELECT COUNT(*) FROM node WHERE spelling='unused_fn'") >=
        1);
  CHECK(q_int(pdb, "SELECT COUNT(*) FROM node WHERE spelling='unused_fn'") ==
        0);
  // ...but the REFERENCED header decl still surfaces as a shallow node
  CHECK(q_int(pdb, "SELECT COUNT(*) FROM node WHERE spelling='helper_fn'") >=
        1);
  CHECK(q_int(pdb, "SELECT COUNT(*) FROM symbol WHERE name='helper_fn'") ==
        1);
  // and the header is registered in file (non-main)
  CHECK(q_int(pdb, "SELECT COUNT(*) FROM file WHERE path LIKE '%helper.hpp' "
                   "AND is_main=0") == 1);
  CHECK(q_int(pdb, "SELECT COUNT(*) FROM file WHERE is_main=1") == 1);
}

} // TEST_SUITE("clang")

int main(int argc, char **argv) {
  doctest::Context ctx(argc, argv);
  const int res = ctx.run();
  if (ctx.shouldExit()) {
    return res;
  }
  if (res == 0 && g_clang_skipped) {
    return 77; // CTest SKIP_RETURN_CODE — "no libclang loadable" is a skip
  }
  return res;
}

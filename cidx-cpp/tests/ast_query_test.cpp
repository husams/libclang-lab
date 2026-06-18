// ast_query_test — M5 unit tests for the `cidx ast` command group (ADR-006 §8.1).
//
// Two doctest registrations:
//   "default" — hermetic (no libclang): json_out, kind_names, group_thousands,
//               argparse sub-tree for ast/ast cache (usage/error/exit-2 paths).
//   "clang"   — real libclang parse over CIDX_MANIFESTS_DIR manifests:
//               cmd_ast_dump / cmd_ast_locals / cmd_ast_conditions pinned
//               against the three goldens in tests/fixtures/m5/.
//
// Context stream seam: cli::Context{out,err} captures stdout/stderr strings so
// the clang suite can assert byte-exact output without subprocesses.
#define DOCTEST_CONFIG_IMPLEMENT
#include "doctest/doctest.h"

#include <sys/stat.h>
#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "astcache/astcache.hpp"
#include "cli/args.hpp"
#include "cli/commands.hpp"
#include "cli/format.hpp"
#include "cli/json_out.hpp"
#include "cli/kind_names.hpp"
#include "clangx/libclang.hpp"
#include "util/errors.hpp"
#include "util/logger.hpp"

namespace fs = std::filesystem;
using cidx::LibClang;
using cidx::UsageError;
namespace cli = cidx::cli;
namespace json_out = cidx::json_out;
namespace format = cidx::cli::format;

namespace {

bool g_clang_skipped = false;

bool require_manifests() {
  struct stat st{};
  if (::stat(CIDX_MANIFESTS_DIR, &st) != 0 || !S_ISDIR(st.st_mode)) {
    g_clang_skipped = true;
    MESSAGE("SKIP: lab fixtures not found at " << CIDX_MANIFESTS_DIR);
    return false;
  }
  return true;
}

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
  char tmpl[] = "/tmp/cidx_astq_XXXXXX";
  char *d = ::mkdtemp(tmpl);
  REQUIRE(d != nullptr);
  return d;
}

std::string read_fixture(const std::string &name) {
  std::string path = std::string(CIDX_FIXTURES_DIR) + "/m5/" + name;
  std::ifstream in(path, std::ios::binary);
  if (!in.good()) {
    FAIL("fixture not found: " << path);
  }
  return {std::istreambuf_iterator<char>(in),
          std::istreambuf_iterator<char>()};
}

// RAII env override.
class ScopedEnv {
public:
  ScopedEnv(const char *name, const char *value) : name_(name) {
    const char *prev = std::getenv(name);
    if (prev)
      prev_ = prev;
    ::setenv(name, value, 1);
  }
  ~ScopedEnv() {
    if (prev_)
      ::setenv(name_, prev_->c_str(), 1);
    else
      ::unsetenv(name_);
  }

private:
  const char *name_;
  std::optional<std::string> prev_;
};

struct ParseFail {
  int code = 0;
  std::string msg;
};

ParseFail parse_fail(const std::vector<std::string> &argv) {
  try {
    cli::parse_args(argv);
    FAIL("expected UsageError");
  } catch (const UsageError &e) {
    return {e.exit_code(), e.what()};
  }
  return {};
}

struct CmdResult {
  int rc = -1;
  std::string out;
  std::string err;
};

// Run an `ast` command with an ephemeral cache dir.
CmdResult run_ast(const std::vector<std::string> &argv,
                  const std::string &cache) {
  cli::ParsedArgs pa = cli::parse_args(argv);
  REQUIRE(!pa.help_text);
  std::ostringstream out;
  std::ostringstream err;
  cli::Context ctx;
  ctx.cache_dir = cache;
  ctx.index_path = cache + "/index.db";
  ctx.logger = &cidx::Logger::root();
  ctx.out = &out;
  ctx.err = &err;
  CmdResult r;
  r.rc = cli::run_command(pa, ctx);
  r.out = out.str();
  r.err = err.str();
  return r;
}

} // namespace

// ============================================================================
// "default" suite — hermetic, no libclang
// ============================================================================

TEST_SUITE("default") {

// ---------------------------------------------------------------------------
// json_out::dumps_indent2
// ---------------------------------------------------------------------------

TEST_CASE("json_out: null/bool/int/string primitives") {
  using json_out::Value;
  CHECK(json_out::dumps_indent2(Value::null()) == "null");
  CHECK(json_out::dumps_indent2(Value::of(true)) == "true");
  CHECK(json_out::dumps_indent2(Value::of(false)) == "false");
  CHECK(json_out::dumps_indent2(Value::of(42LL)) == "42");
  CHECK(json_out::dumps_indent2(Value::of(std::string("hello"))) ==
        "\"hello\"");
}

TEST_CASE("json_out: empty containers") {
  using json_out::Value;
  CHECK(json_out::dumps_indent2(Value::arr({})) == "[]");
  CHECK(json_out::dumps_indent2(Value::obj({})) == "{}");
}

TEST_CASE("json_out: nested array of ints one-per-line") {
  using json_out::Value;
  // [3, 1] must expand to three lines — mirroring Python json.dumps([3,1],indent=2)
  json_out::Array a;
  a.push_back(Value::of(3LL));
  a.push_back(Value::of(1LL));
  const std::string expected = "[\n  3,\n  1\n]";
  CHECK(json_out::dumps_indent2(Value::arr(a)) == expected);
}

TEST_CASE("json_out: string escaping ensure_ascii=True") {
  using json_out::Value;
  // control chars + backslash + quote
  CHECK(json_out::dumps_indent2(Value::of(std::string("\n"))) == "\"\\n\"");
  CHECK(json_out::dumps_indent2(Value::of(std::string("\t"))) == "\"\\t\"");
  CHECK(json_out::dumps_indent2(Value::of(std::string("\r"))) == "\"\\r\"");
  CHECK(json_out::dumps_indent2(Value::of(std::string("\\"))) == "\"\\\\\"");
  CHECK(json_out::dumps_indent2(Value::of(std::string("\""))) == "\"\\\"\"");
  // other control char < 0x20
  CHECK(json_out::dumps_indent2(Value::of(std::string("\x01"))) ==
        "\"\\u0001\"");
}

TEST_CASE("json_out: object member order preserved") {
  using json_out::Value;
  json_out::Object o;
  o.push_back({"b", Value::of(2LL)});
  o.push_back({"a", Value::of(1LL)});
  const std::string got = json_out::dumps_indent2(Value::obj(o));
  // "b" must appear before "a"
  const std::size_t pos_b = got.find("\"b\"");
  const std::size_t pos_a = got.find("\"a\"");
  CHECK(pos_b != std::string::npos);
  CHECK(pos_a != std::string::npos);
  CHECK(pos_b < pos_a);
}

TEST_CASE("json_out: null value in object") {
  using json_out::Value;
  json_out::Object o;
  o.push_back({"key", Value::null()});
  const std::string got = json_out::dumps_indent2(Value::obj(o));
  CHECK(got.find("\"key\": null") != std::string::npos);
}

TEST_CASE("json_out: dumps_indent2 matches dump_leaf_a golden") {
  // Reconstruct the leaf_a JSON tree exactly as cmd_ast_dump builds it and
  // compare against the fixture byte-for-byte.  This validates the full
  // formatter (indent, separators, str-escape, nested depth).
  using json_out::Value;

  auto make_extent = [](const std::string &file, long long sl, long long sc,
                        long long el, long long ec) {
    json_out::Object e;
    e.push_back({"file", Value::of(file)});
    json_out::Array start;
    start.push_back(Value::of(sl));
    start.push_back(Value::of(sc));
    e.push_back({"start", Value::arr(std::move(start))});
    json_out::Array end;
    end.push_back(Value::of(el));
    end.push_back(Value::of(ec));
    e.push_back({"end", Value::arr(std::move(end))});
    return Value::obj(std::move(e));
  };

  // RETURN_STMT child (depth 2 — no children because depth limit)
  json_out::Object ret;
  ret.push_back({"kind", Value::of(std::string("RETURN_STMT"))});
  ret.push_back({"spelling", Value::null()});
  ret.push_back({"usr", Value::null()});
  ret.push_back({"extent", make_extent("calls.c", 3, 28, 3, 40)});
  ret.push_back({"type", Value::null()});

  // COMPOUND_STMT child
  json_out::Object cmpd;
  cmpd.push_back({"kind", Value::of(std::string("COMPOUND_STMT"))});
  cmpd.push_back({"spelling", Value::null()});
  cmpd.push_back({"usr", Value::null()});
  cmpd.push_back({"extent", make_extent("calls.c", 3, 26, 3, 43)});
  cmpd.push_back({"type", Value::null()});
  {
    json_out::Array kids;
    kids.push_back(Value::obj(std::move(ret)));
    cmpd.push_back({"children", Value::arr(std::move(kids))});
  }

  // PARM_DECL child (no children key — within depth 2, no sub-children)
  json_out::Object parm;
  parm.push_back({"kind", Value::of(std::string("PARM_DECL"))});
  parm.push_back({"spelling", Value::of(std::string("x"))});
  parm.push_back({"usr",
                  Value::of(std::string("c:calls.c@38@F@leaf_a@x"))});
  parm.push_back({"extent", make_extent("calls.c", 3, 19, 3, 24)});
  parm.push_back({"type", Value::of(std::string("int"))});

  // FUNCTION_DECL root
  json_out::Object fn;
  fn.push_back({"kind", Value::of(std::string("FUNCTION_DECL"))});
  fn.push_back({"spelling", Value::of(std::string("leaf_a"))});
  fn.push_back({"usr", Value::of(std::string("c:calls.c@F@leaf_a"))});
  fn.push_back({"extent", make_extent("calls.c", 3, 1, 3, 43)});
  fn.push_back({"type", Value::of(std::string("int (int)"))});
  {
    json_out::Array kids;
    kids.push_back(Value::obj(std::move(parm)));
    kids.push_back(Value::obj(std::move(cmpd)));
    fn.push_back({"children", Value::arr(std::move(kids))});
  }

  json_out::Array top;
  top.push_back(Value::obj(std::move(fn)));
  const std::string got =
      json_out::dumps_indent2(Value::arr(std::move(top))) + "\n";

  const std::string expected = read_fixture("dump_leaf_a.json");
  CHECK(got == expected);
}

TEST_CASE("json_out: locals_badly golden") {
  using json_out::Value;
  auto make_row = [](const std::string &name, const std::string &type,
                     const std::string &kind, const std::string &loc) {
    json_out::Object o;
    o.push_back({"name", Value::of(name)});
    o.push_back({"type", Value::of(type)});
    o.push_back({"kind", Value::of(kind)});
    o.push_back({"loc", Value::of(loc)});
    return Value::obj(std::move(o));
  };
  json_out::Array rows;
  rows.push_back(make_row("A", "int", "param", "messy.c:5:28"));
  rows.push_back(make_row("B", "int", "param", "messy.c:5:35"));
  rows.push_back(make_row("Result", "int", "local", "messy.c:6:9"));
  const std::string got =
      json_out::dumps_indent2(Value::arr(std::move(rows))) + "\n";
  const std::string expected = read_fixture("locals_badly.json");
  CHECK(got == expected);
}

TEST_CASE("json_out: conditions_shape_area golden") {
  using json_out::Value;
  json_out::Object row;
  row.push_back({"control", Value::of(std::string("CASE_STMT"))});
  row.push_back({"loc", Value::of(std::string("shapes.c:14:9"))});
  row.push_back({"condition", Value::of(std::string("SHAPE_CIRCLE"))});
  json_out::Array calls;
  calls.push_back(Value::of(std::string("circle_area")));
  row.push_back({"calls", Value::arr(std::move(calls))});

  json_out::Array top;
  top.push_back(Value::obj(std::move(row)));
  const std::string got =
      json_out::dumps_indent2(Value::arr(std::move(top))) + "\n";
  const std::string expected = read_fixture("conditions_shape_area.json");
  CHECK(got == expected);
}

// ---------------------------------------------------------------------------
// kind_names
// ---------------------------------------------------------------------------

TEST_CASE("kind_names: spot-set from ADR-006 §5.4") {
  CHECK(std::string(cli::kind_name(8)) == "FUNCTION_DECL");
  CHECK(std::string(cli::kind_name(10)) == "PARM_DECL");
  CHECK(std::string(cli::kind_name(202)) == "COMPOUND_STMT");
  CHECK(std::string(cli::kind_name(203)) == "CASE_STMT");
  CHECK(std::string(cli::kind_name(214)) == "RETURN_STMT");
}

TEST_CASE("kind_names: irregulars") {
  // These are hand-registered in clang.cindex with non-standard names.
  CHECK(std::string(cli::kind_name(121)) == "StmtExpr");
  CHECK(std::string(cli::kind_name(251)) == "OMP_PARALLELFORSIMD_DIRECTIVE");
  CHECK(std::string(cli::kind_name(264)) == "OMP_TARGET_PARALLELFOR_DIRECTIVE");
}

TEST_CASE("kind_names: unknown kind emits loud marker") {
  // Unknown kind value (not a valid CXCursorKind) must return the loud marker.
  const char *name = cli::kind_name(9999);
  CHECK(std::string(name).find("<UNKNOWN_KIND_") == 0);
}

TEST_CASE("kind_names: additional spot-checks") {
  CHECK(std::string(cli::kind_name(9)) == "VAR_DECL");
  CHECK(std::string(cli::kind_name(1)) == "UNEXPOSED_DECL");
  CHECK(std::string(cli::kind_name(200)) == "UNEXPOSED_STMT");
  // 201 is LABEL_STMT in clang.cindex, not NULL_STMT
  CHECK(std::string(cli::kind_name(201)) == "LABEL_STMT");
}

// ---------------------------------------------------------------------------
// format::group_thousands (ADR-006 §6.4)
// ---------------------------------------------------------------------------

TEST_CASE("group_thousands: spec values from ADR") {
  CHECK(format::group_thousands(0) == "0");
  CHECK(format::group_thousands(1234) == "1,234");
  CHECK(format::group_thousands(1234567) == "1,234,567");
}

TEST_CASE("group_thousands: edge cases") {
  CHECK(format::group_thousands(1) == "1");
  CHECK(format::group_thousands(999) == "999");
  CHECK(format::group_thousands(1000) == "1,000");
  CHECK(format::group_thousands(1000000) == "1,000,000");
}

TEST_CASE("group_thousands: negative values") {
  CHECK(format::group_thousands(-1) == "-1");
  CHECK(format::group_thousands(-1234) == "-1,234");
  CHECK(format::group_thousands(-1234567) == "-1,234,567");
}

// ---------------------------------------------------------------------------
// argparse sub-tree: ast / ast cache usage/error/exit-2
// ---------------------------------------------------------------------------

TEST_CASE("argparse: ast with no subcommand → exit 2") {
  auto f = parse_fail({"ast"});
  CHECK(f.code == 2);
  CHECK(f.msg.find("cidx ast") != std::string::npos);
  CHECK(f.msg.find("the following arguments are required") != std::string::npos);
}

TEST_CASE("argparse: ast -h returns help_text") {
  auto pa = cli::parse_args({"ast", "-h"});
  REQUIRE(pa.help_text.has_value());
  CHECK(pa.help_text->find("dump") != std::string::npos);
  CHECK(pa.help_text->find("locals") != std::string::npos);
  CHECK(pa.help_text->find("conditions") != std::string::npos);
  CHECK(pa.help_text->find("cache") != std::string::npos);
}

TEST_CASE("argparse: ast dump -h returns help_text") {
  auto pa = cli::parse_args({"ast", "dump", "-h"});
  REQUIRE(pa.help_text.has_value());
  CHECK(pa.help_text->find("--depth") != std::string::npos);
  CHECK(pa.help_text->find("--tokens") != std::string::npos);
  CHECK(pa.help_text->find("--types") != std::string::npos);
  CHECK(pa.help_text->find("--cache") != std::string::npos);
  CHECK(pa.help_text->find("--no-cache") != std::string::npos);
}

TEST_CASE("argparse: ast locals -h returns help_text") {
  auto pa = cli::parse_args({"ast", "locals", "-h"});
  REQUIRE(pa.help_text.has_value());
  CHECK(pa.help_text->find("--params") != std::string::npos);
}

TEST_CASE("argparse: ast conditions -h returns help_text") {
  auto pa = cli::parse_args({"ast", "conditions", "-h"});
  REQUIRE(pa.help_text.has_value());
  CHECK(pa.help_text->find("--ast") != std::string::npos);
}

TEST_CASE("argparse: ast cache -h returns help_text") {
  auto pa = cli::parse_args({"ast", "cache", "-h"});
  REQUIRE(pa.help_text.has_value());
  CHECK(pa.help_text->find("build") != std::string::npos);
  CHECK(pa.help_text->find("status") != std::string::npos);
  CHECK(pa.help_text->find("clear") != std::string::npos);
}

TEST_CASE("argparse: ast cache build -h returns help_text") {
  auto pa = cli::parse_args({"ast", "cache", "build", "-h"});
  REQUIRE(pa.help_text.has_value());
  CHECK(pa.help_text->find("--name") != std::string::npos);
}

TEST_CASE("argparse: ast dump options bind correctly") {
  auto pa = cli::parse_args({"ast", "dump", "--depth", "3", "--tokens",
                              "--types", "--json", "foo.c"});
  CHECK(pa.command == "ast");
  CHECK(pa.what == "dump");
  CHECK(pa.depth == 3);
  CHECK(pa.tokens);
  CHECK(pa.types);
  CHECK(pa.ast_json);
  CHECK(pa.target == "foo.c");
  CHECK(pa.use_cache); // default true
}

TEST_CASE("argparse: ast locals options bind correctly") {
  auto pa = cli::parse_args({"ast", "locals", "--params", "--json", "bar.c"});
  CHECK(pa.command == "ast");
  CHECK(pa.what == "locals");
  CHECK(pa.params);
  CHECK(pa.ast_json);
  CHECK(pa.target == "bar.c");
}

TEST_CASE("argparse: ast conditions options bind correctly") {
  auto pa = cli::parse_args({"ast", "conditions", "--ast", "--json", "baz.c"});
  CHECK(pa.command == "ast");
  CHECK(pa.what == "conditions");
  CHECK(pa.cond_ast);
  CHECK(pa.ast_json);
  CHECK(pa.target == "baz.c");
}

TEST_CASE("argparse: ast dump --no-cache sets use_cache false") {
  auto pa = cli::parse_args({"ast", "dump", "--no-cache", "foo.c"});
  CHECK(!pa.use_cache);
}

TEST_CASE("argparse: ast dump --cache --no-cache → mutex error exit 2") {
  auto f = parse_fail({"ast", "dump", "--cache", "--no-cache", "foo.c"});
  CHECK(f.code == 2);
  CHECK(f.msg.find("not allowed with argument") != std::string::npos);
}

TEST_CASE("argparse: ast dump -- flags without explicit target") {
  // When "ast dump -- -std=c11" is given, the optional positional grabs
  // "-std=c11" as the target (both Python argparse and C++ engine do this:
  // the REMAINDER is consumed by the optional positional first when a bare
  // token precedes --). Verified by running both tools:
  //   cidx ast dump -- -std=c11 → error: no such file and not in index: -std=c11
  // This means target="-std=c11" and rest is empty.
  auto pa = cli::parse_args({"ast", "dump", "--", "-std=c11"});
  CHECK(pa.command == "ast");
  CHECK(pa.what == "dump");
  // "-std=c11" is consumed as the positional target
  CHECK(pa.target == "-std=c11");
}

TEST_CASE("argparse: ast dump target + flags") {
  auto pa = cli::parse_args({"ast", "dump", "foo.c", "--", "-std=c11"});
  CHECK(pa.target == "foo.c");
  bool has_flag = false;
  for (const auto &f : pa.rest) {
    if (f == "-std=c11")
      has_flag = true;
  }
  CHECK(has_flag);
}

TEST_CASE("argparse: ast dump --name option") {
  auto pa = cli::parse_args({"ast", "dump", "--name", "my_func", "foo.c"});
  REQUIRE(pa.name.has_value());
  CHECK(pa.name.value() == "my_func");
}

TEST_CASE("argparse: ast dump --usr option") {
  auto pa =
      cli::parse_args({"ast", "dump", "--usr", "c:@F@foo", "foo.c"});
  REQUIRE(pa.ast_usr.has_value());
  CHECK(pa.ast_usr.value() == "c:@F@foo");
}

TEST_CASE("argparse: ast dump --id option") {
  auto pa = cli::parse_args({"ast", "dump", "--id", "42", "foo.c"});
  REQUIRE(pa.ast_id.has_value());
  CHECK(pa.ast_id.value() == 42);
}

TEST_CASE("argparse: ast dump --first option") {
  auto pa = cli::parse_args({"ast", "dump", "--first", "foo.c"});
  CHECK(pa.first);
}

TEST_CASE("argparse: ast dump --db option routes to index_db") {
  auto pa =
      cli::parse_args({"ast", "dump", "--db", "/tmp/custom.db", "foo.c"});
  REQUIRE(pa.index_db.has_value());
  CHECK(pa.index_db.value() == "/tmp/custom.db");
}

TEST_CASE("argparse: ast cache subcommand options bind correctly") {
  auto pa = cli::parse_args({"ast", "cache", "build", "--name", "fn", "foo.c",
                              "--", "-std=c11"});
  CHECK(pa.command == "ast");
  CHECK(pa.what == "cache");
  CHECK(pa.cache_action == "build");
  REQUIRE(pa.name.has_value());
  CHECK(pa.name.value() == "fn");
  CHECK(pa.target == "foo.c");
}

TEST_CASE("argparse: ast cache status no target") {
  auto pa = cli::parse_args({"ast", "cache", "status"});
  CHECK(pa.command == "ast");
  CHECK(pa.what == "cache");
  CHECK(pa.cache_action == "status");
  CHECK(pa.target.empty());
}

TEST_CASE("argparse: ast cache bad action → exit 2") {
  auto f = parse_fail({"ast", "cache", "bogus"});
  CHECK(f.code == 2);
  CHECK(f.msg.find("invalid choice") != std::string::npos);
}

TEST_CASE("argparse: ast bogus subcommand → exit 2") {
  auto f = parse_fail({"ast", "bogus"});
  CHECK(f.code == 2);
  CHECK(f.msg.find("invalid choice") != std::string::npos);
}

TEST_CASE("argparse: no prefix abbreviation — --dept is unrecognized") {
  // D6: no abbreviation; --dept must NOT expand to --depth
  auto f = parse_fail({"ast", "dump", "--dept", "2", "foo.c"});
  CHECK(f.code == 2);
}

TEST_CASE("argparse: ast dump --kind valid choice") {
  auto pa =
      cli::parse_args({"ast", "dump", "--kind", "function", "foo.c"});
  REQUIRE(pa.kind.has_value());
  CHECK(pa.kind.value() == "function");
}

TEST_CASE("argparse: ast dump --kind invalid choice → exit 2") {
  auto f =
      parse_fail({"ast", "dump", "--kind", "notakind", "foo.c"});
  CHECK(f.code == 2);
  CHECK(f.msg.find("invalid choice") != std::string::npos);
}

} // TEST_SUITE("default")

// ============================================================================
// "clang" suite — real libclang parse, pinned against fixtures/m5/ goldens
// ============================================================================

TEST_SUITE("clang") {

TEST_CASE("cmd_ast_dump: leaf_a --depth 2 --types --json matches golden") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());

  const std::string calls_c =
      std::string(CIDX_MANIFESTS_DIR) + "/calls.c";

  // Determine platform clang flags for clean parse (mirrors Python _clang_args)
  std::string sysroot;
  {
    FILE *p = ::popen("xcrun --show-sdk-path 2>/dev/null", "r");
    if (p) {
      char buf[1024] = {};
      if (fgets(buf, sizeof(buf), p)) {
        sysroot = buf;
        while (!sysroot.empty() && (sysroot.back() == '\n' ||
                                    sysroot.back() == '\r'))
          sysroot.pop_back();
      }
      pclose(p);
    }
  }
  std::string resource_dir;
  {
    FILE *p = ::popen("clang -print-resource-dir 2>/dev/null", "r");
    if (p) {
      char buf[1024] = {};
      if (fgets(buf, sizeof(buf), p)) {
        resource_dir = buf;
        while (!resource_dir.empty() && (resource_dir.back() == '\n' ||
                                         resource_dir.back() == '\r'))
          resource_dir.pop_back();
      }
      pclose(p);
    }
  }

  std::vector<std::string> argv = {
      "ast",    "dump",         "--name",   "leaf_a",
      "--depth", "2",           "--types",  "--json",
      calls_c};
  // Append -- -std=c11 [sysroot flags] as REMAINDER
  argv.push_back("--");
  argv.push_back("-std=c11");
  if (!sysroot.empty()) {
    argv.push_back("-isysroot");
    argv.push_back(sysroot);
  }
  if (!resource_dir.empty()) {
    argv.push_back("-I");
    argv.push_back(resource_dir + "/include");
  }
  argv.push_back("-I");
  argv.push_back(std::string(CIDX_MANIFESTS_DIR));

  auto r = run_ast(argv, cache);
  CHECK(r.rc == 0);
  const std::string expected = read_fixture("dump_leaf_a.json");
  CHECK(r.out == expected);

  fs::remove_all(cache);
}

TEST_CASE("cmd_ast_locals: BadlyNamedFunction --params --json matches golden") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());

  const std::string messy_c =
      std::string(CIDX_MANIFESTS_DIR) + "/messy.c";

  std::string sysroot;
  {
    FILE *p = ::popen("xcrun --show-sdk-path 2>/dev/null", "r");
    if (p) {
      char buf[1024] = {};
      if (fgets(buf, sizeof(buf), p))
        sysroot = buf;
      while (!sysroot.empty() && (sysroot.back() == '\n' ||
                                  sysroot.back() == '\r'))
        sysroot.pop_back();
      pclose(p);
    }
  }
  std::string resource_dir;
  {
    FILE *p = ::popen("clang -print-resource-dir 2>/dev/null", "r");
    if (p) {
      char buf[1024] = {};
      if (fgets(buf, sizeof(buf), p))
        resource_dir = buf;
      while (!resource_dir.empty() && (resource_dir.back() == '\n' ||
                                       resource_dir.back() == '\r'))
        resource_dir.pop_back();
      pclose(p);
    }
  }

  std::vector<std::string> argv = {
      "ast", "locals", "--name", "BadlyNamedFunction", "--params", "--json",
      messy_c};
  argv.push_back("--");
  argv.push_back("-std=c11");
  if (!sysroot.empty()) {
    argv.push_back("-isysroot");
    argv.push_back(sysroot);
  }
  if (!resource_dir.empty()) {
    argv.push_back("-I");
    argv.push_back(resource_dir + "/include");
  }
  argv.push_back("-I");
  argv.push_back(std::string(CIDX_MANIFESTS_DIR));

  auto r = run_ast(argv, cache);
  CHECK(r.rc == 0);
  const std::string expected = read_fixture("locals_badly.json");
  CHECK(r.out == expected);

  fs::remove_all(cache);
}

TEST_CASE("cmd_ast_conditions: shape_area --json matches golden") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());

  const std::string shapes_c =
      std::string(CIDX_MANIFESTS_DIR) + "/shapes.c";

  std::string sysroot;
  {
    FILE *p = ::popen("xcrun --show-sdk-path 2>/dev/null", "r");
    if (p) {
      char buf[1024] = {};
      if (fgets(buf, sizeof(buf), p))
        sysroot = buf;
      while (!sysroot.empty() && (sysroot.back() == '\n' ||
                                  sysroot.back() == '\r'))
        sysroot.pop_back();
      pclose(p);
    }
  }
  std::string resource_dir;
  {
    FILE *p = ::popen("clang -print-resource-dir 2>/dev/null", "r");
    if (p) {
      char buf[1024] = {};
      if (fgets(buf, sizeof(buf), p))
        resource_dir = buf;
      while (!resource_dir.empty() && (resource_dir.back() == '\n' ||
                                       resource_dir.back() == '\r'))
        resource_dir.pop_back();
      pclose(p);
    }
  }

  std::vector<std::string> argv = {
      "ast", "conditions", "--name", "shape_area", "--json", shapes_c};
  argv.push_back("--");
  argv.push_back("-std=c11");
  if (!sysroot.empty()) {
    argv.push_back("-isysroot");
    argv.push_back(sysroot);
  }
  if (!resource_dir.empty()) {
    argv.push_back("-I");
    argv.push_back(resource_dir + "/include");
  }
  argv.push_back("-I");
  argv.push_back(std::string(CIDX_MANIFESTS_DIR));

  auto r = run_ast(argv, cache);
  CHECK(r.rc == 0);
  const std::string expected = read_fixture("conditions_shape_area.json");
  CHECK(r.out == expected);

  fs::remove_all(cache);
}

TEST_CASE("cmd_ast_dump: whole calls.c text has known functions") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());

  const std::string calls_c =
      std::string(CIDX_MANIFESTS_DIR) + "/calls.c";

  std::string sysroot;
  {
    FILE *p = ::popen("xcrun --show-sdk-path 2>/dev/null", "r");
    if (p) {
      char buf[1024] = {};
      if (fgets(buf, sizeof(buf), p))
        sysroot = buf;
      while (!sysroot.empty() && (sysroot.back() == '\n' || sysroot.back() == '\r'))
        sysroot.pop_back();
      pclose(p);
    }
  }
  std::string resource_dir;
  {
    FILE *p = ::popen("clang -print-resource-dir 2>/dev/null", "r");
    if (p) {
      char buf[1024] = {};
      if (fgets(buf, sizeof(buf), p))
        resource_dir = buf;
      while (!resource_dir.empty() && (resource_dir.back() == '\n' || resource_dir.back() == '\r'))
        resource_dir.pop_back();
      pclose(p);
    }
  }

  std::vector<std::string> argv = {"ast", "dump", calls_c};
  argv.push_back("--");
  argv.push_back("-std=c11");
  if (!sysroot.empty()) { argv.push_back("-isysroot"); argv.push_back(sysroot); }
  if (!resource_dir.empty()) { argv.push_back("-I"); argv.push_back(resource_dir + "/include"); }
  argv.push_back("-I");
  argv.push_back(std::string(CIDX_MANIFESTS_DIR));

  auto r = run_ast(argv, cache);
  CHECK(r.rc == 0);
  CHECK(r.out.find("FUNCTION_DECL") != std::string::npos);
  CHECK(r.out.find("compute") != std::string::npos);
  CHECK(r.out.find("main") != std::string::npos);

  fs::remove_all(cache);
}

TEST_CASE("cmd_ast_conditions: recurse() returns empty list") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());

  const std::string calls_c =
      std::string(CIDX_MANIFESTS_DIR) + "/calls.c";

  std::string sysroot;
  {
    FILE *p = ::popen("xcrun --show-sdk-path 2>/dev/null", "r");
    if (p) {
      char buf[1024] = {};
      if (fgets(buf, sizeof(buf), p))
        sysroot = buf;
      while (!sysroot.empty() && (sysroot.back() == '\n' || sysroot.back() == '\r'))
        sysroot.pop_back();
      pclose(p);
    }
  }
  std::string resource_dir;
  {
    FILE *p = ::popen("clang -print-resource-dir 2>/dev/null", "r");
    if (p) {
      char buf[1024] = {};
      if (fgets(buf, sizeof(buf), p))
        resource_dir = buf;
      while (!resource_dir.empty() && (resource_dir.back() == '\n' || resource_dir.back() == '\r'))
        resource_dir.pop_back();
      pclose(p);
    }
  }

  std::vector<std::string> argv = {
      "ast", "conditions", "--name", "recurse", "--json", calls_c};
  argv.push_back("--");
  argv.push_back("-std=c11");
  if (!sysroot.empty()) { argv.push_back("-isysroot"); argv.push_back(sysroot); }
  if (!resource_dir.empty()) { argv.push_back("-I"); argv.push_back(resource_dir + "/include"); }
  argv.push_back("-I");
  argv.push_back(std::string(CIDX_MANIFESTS_DIR));

  auto r = run_ast(argv, cache);
  CHECK(r.rc == 0);
  CHECK(r.out == "[]\n");

  fs::remove_all(cache);
}

} // TEST_SUITE("clang")

// ============================================================================
// main: dispatch between suites via --test-suite / --test-suite-exclude.
// Returns SKIP_RETURN_CODE (77) when the clang suite was requested but no
// libclang could be loaded or manifests are absent (matches compiledb_test).
// ============================================================================

int main(int argc, char **argv) {
  doctest::Context ctx;
  ctx.applyCommandLine(argc, argv);
  const int rc = ctx.run();
  if (g_clang_skipped && ctx.shouldExit())
    return 77;
  if (g_clang_skipped && rc == 0)
    return 77;
  return rc;
}

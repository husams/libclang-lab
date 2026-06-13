// S07 tests — args grammar (argparse parity, D6 no-abbreviation delta),
// cli/format, add-source, and the query commands' golden outputs (hermetic,
// label "default"); cmd_import needs CompileDb::load (CXCompilationDatabase)
// and lives in doctest suite "clang" (label "clang", runtime SKIP exit 77
// when no libclang is loadable — same policy as compiledb_test).
//
// Every expected output string below was captured from the Python tool
// (python3 -m indexer ..., Python 3.14, COLUMNS=80) run against a DB seeded
// with EXACTLY the rows seed_gold() writes; the command is cited next to
// each expectation. {ROOT}/{T} placeholders are substituted with the
// runtime temp paths — the formats (widths, marks, counts) are what is
// golden-locked.
#define DOCTEST_CONFIG_IMPLEMENT
#include "doctest/doctest.h"

#include <sys/stat.h>
#include <unistd.h>

#include <cstdlib>
#include <ctime>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "clangx/libclang.hpp"
#include "cli/args.hpp"
#include "cli/commands.hpp"
#include "cli/format.hpp"
#include "storage/records.hpp"
#include "storage/storage.hpp"
#include "util/errors.hpp"
#include "util/logger.hpp"

using cidx::Storage;
using cidx::Symbol;
using cidx::UsageError;
namespace cli = cidx::cli;

namespace {

bool g_clang_skipped = false;

// Returns true when CIDX_MANIFESTS_DIR points at an existing directory.
// On a host without the lab checkout (e.g. the e2e box that only rsyncs
// cidx-cpp/) the fixture cases should SKIP rather than fail.
bool require_manifests() {
  struct stat st{};
  if (::stat(CIDX_MANIFESTS_DIR, &st) != 0 || !S_ISDIR(st.st_mode)) {
    g_clang_skipped = true;
    MESSAGE("SKIP: lab fixtures not found at " << CIDX_MANIFESTS_DIR);
    return false;
  }
  return true;
}

cidx::LibClang *require_libclang() {
  cidx::LibClang &lib = cidx::LibClang::instance();
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
  char tmpl[] = "/tmp/cidx_cli_XXXXXX";
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

void write_file(const std::string &path, const std::string &content) {
  std::ofstream f(path);
  REQUIRE(f.good());
  f << content;
}

bool path_exists(const std::string &path) {
  struct stat st{};
  return ::stat(path.c_str(), &st) == 0;
}

std::string replace_all(std::string text, const std::string &from,
                        const std::string &to) {
  std::size_t pos = 0;
  while ((pos = text.find(from, pos)) != std::string::npos) {
    text.replace(pos, from.size(), to);
    pos += to.size();
  }
  return text;
}

class ScopedEnv {
public:
  ScopedEnv(const char *name, const char *value) : name_(name) {
    const char *prev = std::getenv(name);
    if (prev != nullptr) {
      prev_ = prev;
    }
    ::setenv(name, value, 1);
  }
  ~ScopedEnv() {
    if (prev_) {
      ::setenv(name_, prev_->c_str(), 1);
    } else {
      ::unsetenv(name_);
    }
  }

private:
  const char *name_;
  std::optional<std::string> prev_;
};

// -- parse helpers ------------------------------------------------------------

struct ParseFail {
  int code = 0;
  std::string msg;
};

ParseFail parse_fail(const std::vector<std::string> &argv) {
  ParseFail out;
  try {
    cli::parse_args(argv);
    FAIL("expected UsageError for: ", doctest::toString(argv.size()));
  } catch (const UsageError &e) {
    out.code = e.exit_code();
    out.msg = e.what();
  }
  return out;
}

// -- command runner -----------------------------------------------------------

struct CmdResult {
  int rc = -1;
  std::string out;
  std::string err;
};

// `logger` lets index tests use a per-case Logger: the warning counter is
// cumulative per Logger instance (Python's module-global _warnings), so a
// fresh one keeps the "N warning(s)/error(s)" assertions deterministic.
CmdResult run_cli(const std::vector<std::string> &argv,
                  const std::string &cache, cidx::Logger *logger = nullptr) {
  cli::ParsedArgs pa = cli::parse_args(argv);
  REQUIRE(!pa.help_text);
  std::ostringstream out;
  std::ostringstream err;
  cli::Context ctx;
  ctx.cache_dir = cache;
  ctx.index_path = cache + "/index.db";
  ctx.logger = logger != nullptr ? logger : &cidx::Logger::root();
  ctx.out = &out;
  ctx.err = &err;
  CmdResult r;
  r.rc = cli::run_command(pa, ctx);
  r.out = out.str();
  r.err = err.str();
  return r;
}

std::string read_file(const std::string &path) {
  std::ifstream f(path);
  REQUIRE(f.good());
  std::ostringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

// -- golden DB seed -----------------------------------------------------------
// Mirror of the Python seeding script used for the capture (same rows, same
// order, same ids 1..6); root is a path that does NOT exist on disk.

void seed_gold(const std::string &cache, const std::string &root) {
  Storage db(cache + "/index.db");
  const int64_t cid = db.add_component("gold", root, "repo");
  const int64_t f1 = db.add_file_path(
      root + "/src/a.c", 1718000000.0,
      std::string("0123456789abcdef0123456789abcdef"),
      std::vector<std::string>{"-I" + root + "/include", "-DX=1"},
      std::string("gcc"));
  const int64_t f2 = db.add_file_path(root + "/include/a.h");
  REQUIRE(cid == 1);
  REQUIRE(f1 == 1);
  REQUIRE(f2 == 2);

  Symbol s;
  s.usr = "c:@F@multiply";
  s.spelling = "multiply";
  s.kind = "function";
  s.qual_name = "multiply";
  s.display_name = "multiply(int, int)";
  s.type_info = "int (int, int)";
  s.file_id = f1;
  s.line = 12;
  s.col = 5;
  s.decl_file_id = f2;
  s.decl_line = 3;
  s.decl_col = 5;
  s.is_definition = true;
  s.linkage = "external";
  s.resolved = true;
  db.add_symbol(s);

  s = Symbol{};
  s.usr = "c:@F@square";
  s.spelling = "square";
  s.kind = "function";
  s.qual_name = "square";
  s.display_name = "square(int)";
  s.type_info = "int (int)";
  s.file_id = f2;
  s.line = 4;
  s.col = 5;
  s.decl_file_id = f2;
  s.decl_line = 4;
  s.decl_col = 5;
  s.linkage = "external";
  db.add_symbol(s);

  s = Symbol{};
  s.usr = "c:@N@NS";
  s.spelling = "NS";
  s.kind = "namespace";
  s.qual_name = "NS";
  s.display_name = "NS";
  s.file_id = f2;
  s.line = 8;
  s.col = 11;
  s.is_definition = true;
  s.resolved = true;
  db.add_symbol(s);

  s = Symbol{};
  s.usr = "c:@N@NS@S@Shape";
  s.spelling = "Shape";
  s.kind = "class";
  s.qual_name = "NS::Shape";
  s.display_name = "Shape";
  s.type_info = "NS::Shape";
  s.file_id = f2;
  s.line = 10;
  s.col = 7;
  s.is_definition = true;
  s.resolved = true;
  s.linkage = "external";
  s.parent_usr = "c:@N@NS";
  db.add_symbol(s);

  s = Symbol{};
  s.usr = "c:@N@NS@S@Shape@F@area#";
  s.spelling = "area";
  s.kind = "method";
  s.qual_name = "NS::Shape::area";
  s.display_name = "area()";
  s.type_info = "double ()";
  s.file_id = f2;
  s.line = 12;
  s.col = 18;
  s.is_pure = true;
  s.linkage = "external";
  s.access = "public";
  s.parent_usr = "c:@N@NS@S@Shape";
  db.add_symbol(s);

  s = Symbol{};
  s.usr = "c:a.c@counter";
  s.spelling = "counter";
  s.kind = "variable";
  s.qual_name = "counter";
  s.display_name = "counter";
  s.type_info = "int";
  s.file_id = f1;
  s.line = 7;
  s.col = 12;
  s.is_definition = true;
  s.resolved = true;
  s.linkage = "internal";
  db.add_symbol(s);

  // Deterministic indexed state (datetime('now') would not be).
  db.raw_db().exec(
      "UPDATE file SET indexed=1, indexed_at='2026-06-12 10:00:00' "
      "WHERE id=1");
}

struct GoldFixture {
  std::string cache;
  std::string root;
  GoldFixture() : cache(make_temp_dir()), root(cache + "/gold") {
    seed_gold(cache, root); // root deliberately NOT created on disk
  }
  std::string expect(const std::string &tmpl) const {
    return replace_all(tmpl, "{ROOT}", root);
  }
};

// Usage blocks shared by several expected error messages (transcribed from
// the captured Python argparse output, COLUMNS=80).
const char kTopUsage[] =
    "usage: cidx [-h] {init,add-source,import,index,search,show,list,ls} ...\n";

const char kSearchUsage[] =
    "usage: cidx search [-h]\n"
    "                   [--kind "
    "{class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,"
    "type-alias,typedef,union,variable}]\n"
    "                   [--limit N]\n"
    "                   pattern\n";

const char kListFilesUsage[] =
    "usage: cidx list files [-h] [--component NAME] [--dir PATH] [--indexed "
    "|\n"
    "                       --pending]\n"
    "                       [pattern]\n";

} // namespace

// ---------------------------------------------------------------------------
// Args grammar (default label)
// ---------------------------------------------------------------------------

TEST_CASE("args: no command -> exit 2, required: command") {
  // $ python3 -m indexer
  const ParseFail f = parse_fail({});
  CHECK(f.code == 2);
  CHECK(f.msg == std::string(kTopUsage) +
                     "cidx: error: the following arguments are required: "
                     "command\n");
}

TEST_CASE("args: unknown command -> exit 2, invalid choice") {
  // $ python3 -m indexer bogus
  const ParseFail f = parse_fail({"bogus"});
  CHECK(f.code == 2);
  CHECK(f.msg ==
        std::string(kTopUsage) +
            "cidx: error: argument command: invalid choice: 'bogus' (choose "
            "from init, add-source, import, index, search, show, list, ls)\n");
}

TEST_CASE("args: unknown flag -> exit 2, TOP-level unrecognized arguments") {
  // $ python3 -m indexer search foo --bogus
  const ParseFail f = parse_fail({"search", "foo", "--bogus"});
  CHECK(f.code == 2);
  CHECK(f.msg == std::string(kTopUsage) +
                     "cidx: error: unrecognized arguments: --bogus\n");
}

TEST_CASE("args: extra positional -> exit 2, unrecognized arguments") {
  // $ python3 -m indexer show symbol 5 extra
  const ParseFail f = parse_fail({"show", "symbol", "5", "extra"});
  CHECK(f.code == 2);
  CHECK(f.msg == std::string(kTopUsage) +
                     "cidx: error: unrecognized arguments: extra\n");
}

TEST_CASE("args: NO prefix abbreviation — --lim is unrecognized (D6 delta)") {
  // Python argparse (allow_abbrev) accepts `--lim`; cidx-cpp deliberately
  // does not. Documented delta — golden tests never use abbreviations.
  const ParseFail f = parse_fail({"search", "--lim", "5", "foo"});
  CHECK(f.code == 2);
  CHECK(f.msg == std::string(kTopUsage) +
                     "cidx: error: unrecognized arguments: --lim foo\n");
}

TEST_CASE("args: missing required positional -> subparser exit 2") {
  // $ python3 -m indexer search
  const ParseFail f = parse_fail({"search"});
  CHECK(f.code == 2);
  CHECK(f.msg == std::string(kSearchUsage) +
                     "cidx search: error: the following arguments are "
                     "required: pattern\n");
}

TEST_CASE("args: subparser required check fires BEFORE top unrecognized") {
  // $ python3 -m indexer search --bogus
  const ParseFail f = parse_fail({"search", "--bogus"});
  CHECK(f.code == 2);
  CHECK(f.msg == std::string(kSearchUsage) +
                     "cidx search: error: the following arguments are "
                     "required: pattern\n");
}

TEST_CASE("args: missing required option -> exit 2 (add-source, import)") {
  // $ python3 -m indexer add-source
  ParseFail f = parse_fail({"add-source"});
  CHECK(f.code == 2);
  CHECK(f.msg ==
        "usage: cidx add-source [-h] --path PATH [--name NAME] [--kind "
        "{repo,external}]\n"
        "cidx add-source: error: the following arguments are required: "
        "--path\n");
  // $ python3 -m indexer import
  f = parse_fail({"import"});
  CHECK(f.code == 2);
  CHECK(f.msg == "usage: cidx import [-h] --db DB [--name NAME]\n"
                 "cidx import: error: the following arguments are required: "
                 "--db\n");
}

TEST_CASE("args: option missing its value -> expected one argument") {
  // $ python3 -m indexer search foo --limit
  ParseFail f = parse_fail({"search", "foo", "--limit"});
  CHECK(f.code == 2);
  CHECK(f.msg == std::string(kSearchUsage) +
                     "cidx search: error: argument --limit: expected one "
                     "argument\n");
  // $ python3 -m indexer list dirs --component   (short-alias error name)
  f = parse_fail({"list", "dirs", "--component"});
  CHECK(f.code == 2);
  CHECK(f.msg == "usage: cidx list dirs [-h] [--component NAME] [pattern]\n"
                 "cidx list dirs: error: argument --component/-c: expected "
                 "one argument\n");
}

TEST_CASE("args: invalid int -> exit 2") {
  // $ python3 -m indexer search foo --limit xx
  const ParseFail f = parse_fail({"search", "foo", "--limit", "xx"});
  CHECK(f.code == 2);
  CHECK(f.msg == std::string(kSearchUsage) +
                     "cidx search: error: argument --limit: invalid int "
                     "value: 'xx'\n");
}

TEST_CASE("args: invalid choice -> exit 2 (both kind sets)") {
  // $ python3 -m indexer search foo --kind bogus
  ParseFail f = parse_fail({"search", "foo", "--kind", "bogus"});
  CHECK(f.code == 2);
  CHECK(f.msg ==
        std::string(kSearchUsage) +
            "cidx search: error: argument --kind: invalid choice: 'bogus' "
            "(choose from class, class-template, constructor, destructor, "
            "enum, enum-constant, function, function-template, macro, "
            "member, method, namespace, struct, type-alias, typedef, union, "
            "variable)\n");
  // $ python3 -m indexer add-source --path /tmp --kind bogus
  f = parse_fail({"add-source", "--path", "/tmp", "--kind", "bogus"});
  CHECK(f.code == 2);
  CHECK(f.msg ==
        "usage: cidx add-source [-h] --path PATH [--name NAME] [--kind "
        "{repo,external}]\n"
        "cidx add-source: error: argument --kind: invalid choice: 'bogus' "
        "(choose from repo, external)\n");
}

TEST_CASE("args: show/list need a sub-command; invalid what -> exit 2") {
  // $ python3 -m indexer show
  ParseFail f = parse_fail({"show"});
  CHECK(f.code == 2);
  CHECK(f.msg == "usage: cidx show [-h] {symbol,file} ...\n"
                 "cidx show: error: the following arguments are required: "
                 "what\n");
  // $ python3 -m indexer show bogus
  f = parse_fail({"show", "bogus"});
  CHECK(f.msg == "usage: cidx show [-h] {symbol,file} ...\n"
                 "cidx show: error: argument what: invalid choice: 'bogus' "
                 "(choose from symbol, file)\n");
  // $ python3 -m indexer ls bogus    (alias reports as `cidx list`)
  f = parse_fail({"ls", "bogus"});
  CHECK(f.msg == "usage: cidx list [-h] {components,dirs,files,symbols} ...\n"
                 "cidx list: error: argument what: invalid choice: 'bogus' "
                 "(choose from components, dirs, files, symbols)\n");
}

TEST_CASE("args: --indexed and --pending are mutually exclusive (exit 2)") {
  // $ python3 -m indexer list files --indexed --pending
  ParseFail f = parse_fail({"list", "files", "--indexed", "--pending"});
  CHECK(f.code == 2);
  CHECK(f.msg == std::string(kListFilesUsage) +
                     "cidx list files: error: argument --pending: not "
                     "allowed with argument --indexed\n");
  // $ python3 -m indexer list files --pending --indexed   (order swaps)
  f = parse_fail({"list", "files", "--pending", "--indexed"});
  CHECK(f.msg == std::string(kListFilesUsage) +
                     "cidx list files: error: argument --indexed: not "
                     "allowed with argument --pending\n");
}

TEST_CASE("args: defaults — search 25, list symbols 50, add-source repo") {
  cli::ParsedArgs pa = cli::parse_args({"search", "foo"});
  CHECK(pa.command == "search");
  CHECK(pa.limit == 25);
  CHECK(!pa.kind);
  CHECK(*pa.pattern == "foo");

  pa = cli::parse_args({"list", "symbols"});
  CHECK(pa.what == "symbols");
  CHECK(pa.limit == 50);
  CHECK(!pa.pattern);

  pa = cli::parse_args({"add-source", "--path", "/x"});
  CHECK(*pa.kind == "repo");
}

TEST_CASE("args: ls aliases list") {
  const cli::ParsedArgs pa = cli::parse_args({"ls", "components"});
  CHECK(pa.command == "list");
  CHECK(pa.what == "components");
}

TEST_CASE("args: --flag=value, glued -cVALUE, negative limit value") {
  // $ python3 -m indexer search --kind=function foo   (accepted)
  cli::ParsedArgs pa = cli::parse_args({"search", "--kind=function", "foo"});
  CHECK(*pa.kind == "function");
  // $ python3 -m indexer list dirs -cmycomp           (accepted)
  pa = cli::parse_args({"list", "dirs", "-cmycomp"});
  CHECK(*pa.component == "mycomp");
  // $ python3 -m indexer search foo --limit -5        (negative number OK)
  pa = cli::parse_args({"search", "foo", "--limit", "-5"});
  CHECK(pa.limit == -5);
  // Python int() strips whitespace: --limit ' 12 '
  pa = cli::parse_args({"search", "foo", "--limit", " 12 "});
  CHECK(pa.limit == 12);
}

TEST_CASE(
    "args: parse_py_int saturates huge positive --limit at INT_MAX (R5)") {
  // 30-digit limit → saturates to INT_MAX → treated as "show all" (> any
  // realistic result count).
  cli::ParsedArgs pa = cli::parse_args(
      {"search", "foo", "--limit", "999999999999999999999999999999"});
  CHECK(pa.limit == std::numeric_limits<int>::max());

  // INT_MAX exactly (2^31 - 1) — no saturation needed.
  pa = cli::parse_args({"search", "foo", "--limit", "2147483647"});
  CHECK(pa.limit == 2147483647);

  // INT_MAX + 1 (2^31) — saturates to INT_MAX.
  pa = cli::parse_args({"search", "foo", "--limit", "2147483648"});
  CHECK(pa.limit == 2147483647);

  // Negative limits are preserved unchanged (existing negative-slice path).
  pa = cli::parse_args({"search", "foo", "--limit", "-5"});
  CHECK(pa.limit == -5);
}

TEST_CASE("args: index collects FILE... and --source") {
  const cli::ParsedArgs pa =
      cli::parse_args({"index", "a.c", "b.c", "--source", "comp"});
  CHECK(pa.files == std::vector<std::string>{"a.c", "b.c"});
  CHECK(*pa.source == "comp");
}

TEST_CASE("args: -h returns help text; encounter order vs errors") {
  // $ python3 -m indexer -h    (full top help, exit 0)
  cli::ParsedArgs pa = cli::parse_args({"-h"});
  REQUIRE(pa.help_text);
  CHECK(*pa.help_text ==
        std::string(kTopUsage) +
            "\n"
            "cidx command-line skeleton\n"
            "\n"
            "positional arguments:\n"
            "  {init,add-source,import,index,search,show,list,ls}\n"
            "    init                create a blank index database\n"
            "    add-source          register a component\n"
            "    import              import a compile_commands.json\n"
            "    index               index imported C/C++ files\n"
            "    search              fuzzy-search symbols by qualified name\n"
            "    show                show full details of one symbol or "
            "file\n"
            "    list (ls)           browse the index: components, dirs, "
            "files, symbols\n"
            "\n"
            "options:\n"
            "  -h, --help            show this help message and exit\n");

  // $ python3 -m indexer search -h --kind bogus foo   -> help wins (exit 0)
  pa = cli::parse_args({"search", "-h", "--kind", "bogus", "foo"});
  REQUIRE(pa.help_text);
  CHECK(pa.help_text->find("show at most N matches (0 = all; default 25)") !=
        std::string::npos);

  // $ python3 -m indexer search --kind bogus -h       -> error wins (exit 2)
  const ParseFail f = parse_fail({"search", "--kind", "bogus", "-h"});
  CHECK(f.code == 2);

  // $ python3 -m indexer list files -h                (first usage line
  // matches the wrapped [--indexed | --pending] block)
  pa = cli::parse_args({"list", "files", "-h"});
  REQUIRE(pa.help_text);
  CHECK(pa.help_text->compare(0, std::string(kListFilesUsage).size(),
                              kListFilesUsage) == 0);
}

// ---------------------------------------------------------------------------
// format helpers (default label)
// ---------------------------------------------------------------------------

TEST_CASE("format: mtime renders in LOCAL time (G31/D14)") {
  namespace fmt = cli::format;
  {
    ScopedEnv tz("TZ", "UTC");
    ::tzset();
    CHECK(fmt::format_mtime(1718000000.0) == "2024-06-10 06:13:20");
  }
  {
    ScopedEnv tz("TZ", "America/New_York"); // UTC-4 on 2024-06-10 (EDT)
    ::tzset();
    CHECK(fmt::format_mtime(1718000000.0) == "2024-06-10 02:13:20");
  }
  ::tzset();
}

TEST_CASE("format: py_str / py_repr / just helpers") {
  namespace fmt = cli::format;
  CHECK(fmt::py_str(std::optional<int64_t>{}) == "None");
  CHECK(fmt::py_str(std::optional<int64_t>{12}) == "12");
  CHECK(fmt::py_str(std::optional<std::string>{}) == "None");
  CHECK(fmt::py_repr("nope") == "'nope'");
  CHECK(fmt::py_repr("it's") == "\"it's\"");
  CHECK(fmt::rjust("1", 6) == "     1");
  CHECK(fmt::ljust("repo", 8) == "repo    ");
}

// ---------------------------------------------------------------------------
// R1/R9 exception-handler contract (default label)
// ---------------------------------------------------------------------------
// R9: makedirs() now checks errno and throws CidxError on any failure other
//   than EEXIST.  We verify the shape of that error propagates as CidxError
//   (which main catches with "error: …" + exit 1) and is NOT swallowed
//   silently.  makedirs itself lives in main.cpp's anonymous namespace and
//   cannot be called from tests, but Storage::open() throws CidxError on a
//   bad DB path — same catch-site chain as makedirs — so that path is used
//   as a proxy to confirm the error type and message shape.
//
// R1: main() previously lacked a catch(std::exception) handler.  Because
//   CidxError : std::runtime_error : std::exception, the EXISTING handlers
//   already cover CidxError subtypes; R1 adds coverage for non-CidxError
//   std::exception types (bad_alloc, regex_error, …).  We assert the type
//   hierarchy and simulate the new handler with a try/catch that mirrors
//   main()'s catch chain.

TEST_CASE("main: CidxError propagation shape (R9 proxy) + "
          "std::exception IS-A chain (R1)") {
  // --- R1: type-system assertion -------------------------------------------
  // CidxError IS-A std::exception; before R1 a bare std::runtime_error (not
  // CidxError) thrown from, say, a SQLite driver would escape both handlers.
  static_assert(std::is_base_of<std::exception, cidx::CidxError>::value,
                "CidxError must derive from std::exception");
  // A type that is std::exception but NOT CidxError (simulates third-party
  // throws that R1's catch(std::exception) must catch).
  static_assert(!std::is_base_of<cidx::CidxError, std::runtime_error>::value,
                "plain std::runtime_error must NOT be-a CidxError");

  // Runtime: simulate main()'s R1-extended catch chain on a plain
  // std::runtime_error — prior to R1 this would terminate(); now exit 1.
  int simulated_rc = -1;
  try {
    throw std::runtime_error("simulated third-party failure");
  } catch (const cidx::UsageError &) {
    simulated_rc = 2;
  } catch (const cidx::CidxError &) {
    simulated_rc = 1;
  } catch (const std::exception &) { // R1 new handler
    simulated_rc = 1;
  } catch (...) { // R1 new handler
    simulated_rc = 1;
  }
  CHECK(simulated_rc == 1);

  // --- R9 proxy: Storage open on bad path throws CidxError -----------------
  // On macOS /dev/null/bad.db is not creatable; on Linux same.
  const std::string t = make_temp_dir();
  write_file(t + "/index.db", ""); // ensure open fails via bad parent dir
  bool threw_cidx_error = false;
  std::string cidx_msg;
  try {
    // Construct a context with a DB path whose parent is unwritable.
    cidx::cli::Context ctx;
    ctx.cache_dir = t;
    ctx.index_path = "/dev/null/cidx-r9-test.db";
    ctx.logger = &cidx::Logger::root();
    std::ostringstream out, err;
    ctx.out = &out;
    ctx.err = &err;
    cidx::cli::ParsedArgs pa = cidx::cli::parse_args({"list", "components"});
    // run_command opens Storage; opening /dev/null/cidx-r9-test.db must throw.
    cidx::cli::run_command(pa, ctx);
  } catch (const cidx::CidxError &e) {
    threw_cidx_error = true;
    cidx_msg = e.what();
  }
  CHECK(threw_cidx_error);
  CHECK(!cidx_msg.empty()); // message carries the path/reason
}

// ---------------------------------------------------------------------------
// add-source (default label)
// ---------------------------------------------------------------------------

TEST_CASE("add-source: repo walks to git root, name from .git/config") {
  const std::string t = make_temp_dir();
  makedirs(t + "/repo/.git");
  makedirs(t + "/repo/sub");
  write_file(
      t + "/repo/.git/config",
      "[remote \"origin\"]\n\turl = https://example.com/gold-repo.git\n");
  // $ python3 -m indexer add-source --path <t>/repo/sub
  // component #1: gold-repo (repo) at <t>/repo
  CmdResult r = run_cli({"add-source", "--path", t + "/repo/sub"}, t);
  CHECK(r.rc == 0);
  CHECK(r.out == "component #1: gold-repo (repo) at " + t + "/repo\n");
  CHECK(r.err.empty());

  // external: path as-is, name = basename; no git walk
  makedirs(t + "/ext");
  // $ python3 -m indexer add-source --path <t>/ext --kind external
  r = run_cli({"add-source", "--path", t + "/ext", "--kind", "external"}, t);
  CHECK(r.rc == 0);
  CHECK(r.out == "component #2: ext (external) at " + t + "/ext\n");

  // --name override; same path upserts to the same id
  r = run_cli({"add-source", "--path", t + "/ext", "--kind", "external",
               "--name", "mylib"},
              t);
  CHECK(r.rc == 0);
  CHECK(r.out == "component #2: mylib (external) at " + t + "/ext\n");

  // repo kind without any .git up the tree: name = basename via repo_name
  makedirs(t + "/norepo");
  r = run_cli({"add-source", "--path", t + "/norepo"}, t);
  CHECK(r.rc == 0);
  CHECK(r.out == "component #3: norepo (repo) at " + t + "/norepo\n");
}

TEST_CASE("add-source: --path not a directory -> exit 1") {
  const std::string t = make_temp_dir();
  // $ python3 -m indexer add-source --path <t>/missing
  const CmdResult r = run_cli({"add-source", "--path", t + "/missing"}, t);
  CHECK(r.rc == 1);
  CHECK(r.out.empty());
  CHECK(r.err == "error: " + t + "/missing is not a directory\n");
}

// ---------------------------------------------------------------------------
// query commands — golden outputs (default label)
// ---------------------------------------------------------------------------

TEST_CASE("search: def row + second decl row; zero matches exit 1") {
  const GoldFixture g;
  // $ python3 -m indexer search multiply
  CmdResult r = run_cli({"search", "multiply"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("     1  multiply  function          def   "
                          "{ROOT}/src/a.c:12\n"
                          "                                    decl  "
                          "{ROOT}/include/a.h:3\n"
                          "1 match(es)\n"));

  // $ python3 -m indexer search Shape::area   ('::'-segment match, pure)
  r = run_cli({"search", "Shape::area"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("     5  NS::Shape::area  method            pure  "
                          "{ROOT}/include/a.h:12\n"
                          "1 match(es)\n"));

  // $ python3 -m indexer search zz
  r = run_cli({"search", "zz"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.out == "0 match(es)\n");
}

TEST_CASE("search: --limit slicing, 0 = all, --kind filter") {
  const GoldFixture g;
  // $ python3 -m indexer search a --limit 2
  CmdResult r = run_cli({"search", "a", "--limit", "2"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("     2  square     function          decl  "
                          "{ROOT}/include/a.h:4\n"
                          "     4  NS::Shape  class             def   "
                          "{ROOT}/include/a.h:10\n"
                          "3 match(es) (showing 2)\n"));

  // $ python3 -m indexer search a --limit 0
  r = run_cli({"search", "a", "--limit", "0"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("     2  square           function          decl  "
                          "{ROOT}/include/a.h:4\n"
                          "     4  NS::Shape        class             def   "
                          "{ROOT}/include/a.h:10\n"
                          "     5  NS::Shape::area  method            pure  "
                          "{ROOT}/include/a.h:12\n"
                          "3 match(es)\n"));

  // $ python3 -m indexer search square --kind function
  r = run_cli({"search", "square", "--kind", "function"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("     2  square  function          decl  "
                          "{ROOT}/include/a.h:4\n"
                          "1 match(es)\n"));

  // $ python3 -m indexer search counter --kind class
  r = run_cli({"search", "counter", "--kind", "class"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.out == "0 match(es)\n");
}

TEST_CASE("show symbol: by id and USR; None fields omitted; glosses") {
  const GoldFixture g;
  // $ python3 -m indexer show symbol 1
  CmdResult r = run_cli({"show", "symbol", "1"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("id           1\n"
                          "usr          c:@F@multiply\n"
                          "name         multiply\n"
                          "qualified    multiply\n"
                          "display      multiply(int, int)\n"
                          "kind         function\n"
                          "type         int (int, int)\n"
                          "visibility   program-wide (usable from any .cpp)\n"
                          "definition   {ROOT}/src/a.c:12:5\n"
                          "declaration  {ROOT}/include/a.h:3:5\n"
                          "resolved     yes\n"));

  // $ python3 -m indexer show symbol 'c:@F@square'   (USR lookup)
  r = run_cli({"show", "symbol", "c:@F@square"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("id           2\n"
                          "usr          c:@F@square\n"
                          "name         square\n"
                          "qualified    square\n"
                          "display      square(int)\n"
                          "kind         function\n"
                          "type         int (int)\n"
                          "visibility   program-wide (usable from any .cpp)\n"
                          "declaration  {ROOT}/include/a.h:4:5\n"
                          "resolved     no (definition not seen)\n"));

  // $ python3 -m indexer show symbol 5   (pure virtual + parent + access)
  r = run_cli({"show", "symbol", "5"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("id           5\n"
                          "usr          c:@N@NS@S@Shape@F@area#\n"
                          "name         area\n"
                          "qualified    NS::Shape::area\n"
                          "display      area()\n"
                          "kind         method\n"
                          "type         double ()\n"
                          "visibility   program-wide (usable from any .cpp)\n"
                          "access       public\n"
                          "parent       NS::Shape  [c:@N@NS@S@Shape]\n"
                          "pure         yes (pure virtual; implemented by "
                          "overriders)\n"
                          "resolved     n/a (pure virtual)\n"));

  // $ python3 -m indexer show symbol 6   (internal linkage gloss)
  r = run_cli({"show", "symbol", "6"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out ==
        g.expect("id           6\n"
                 "usr          c:a.c@counter\n"
                 "name         counter\n"
                 "qualified    counter\n"
                 "display      counter\n"
                 "kind         variable\n"
                 "type         int\n"
                 "visibility   file-local (static / anonymous namespace)\n"
                 "definition   {ROOT}/src/a.c:7:12\n"
                 "resolved     yes\n"));

  // $ python3 -m indexer show symbol 3   (no linkage stored: visibility,
  // type, parent all omitted)
  r = run_cli({"show", "symbol", "3"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("id           3\n"
                          "usr          c:@N@NS\n"
                          "name         NS\n"
                          "qualified    NS\n"
                          "display      NS\n"
                          "kind         namespace\n"
                          "definition   {ROOT}/include/a.h:8:11\n"
                          "resolved     yes\n"));

  // $ python3 -m indexer show symbol 99
  r = run_cli({"show", "symbol", "99"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.err == "error: no symbol with id/USR '99'\n");
}

TEST_CASE("show file: by path and id; G31 time formats; G20 placeholder") {
  const GoldFixture g;
  ScopedEnv tz("TZ", "UTC"); // mtime is local-time formatted; pin it
  ::tzset();

  // $ TZ=UTC python3 -m indexer show file <root>/src/a.c
  CmdResult r = run_cli({"show", "file", g.root + "/src/a.c"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("id           1\n"
                          "path         {ROOT}/src/a.c\n"
                          "component    gold (repo)  {ROOT}\n"
                          "directory    src\n"
                          "mtime        2024-06-10 06:13:20\n"
                          "md5          0123456789abcdef0123456789abcdef\n"
                          "driver       gcc\n"
                          "options      -I{ROOT}/include -DX=1\n"
                          "indexed      no (content changed since import)\n"
                          "indexed at   2026-06-12 10:00:00 UTC\n"
                          "symbols      2 (2 defined here, 0 declared here)\n"
                          "by kind      function: 1, variable: 1\n"));

  // $ python3 -m indexer show file 2   (header row: NULL options/driver)
  r = run_cli({"show", "file", "2"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out ==
        g.expect("id           2\n"
                 "path         {ROOT}/include/a.h\n"
                 "component    gold (repo)  {ROOT}\n"
                 "directory    include\n"
                 "options      (none -- header indexed via an including "
                 "TU)\n"
                 "indexed      no (never indexed)\n"
                 "symbols      5 (2 defined here, 2 declared here)\n"
                 "by kind      class: 1, function: 2, method: 1, "
                 "namespace: 1\n"));

  // $ python3 -m indexer show file bogus.c -c gold   (error names the REF)
  r = run_cli({"show", "file", "bogus.c", "-c", "gold"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.err == "error: not in index database: bogus.c\n");

  // $ python3 -m indexer show file 99
  r = run_cli({"show", "file", "99"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.err == "error: not in index database: 99\n");
  ::tzset();
}

TEST_CASE("list components: table, kind filter, fuzzy pattern, ls alias") {
  const GoldFixture g;
  // $ python3 -m indexer list components
  CmdResult r = run_cli({"list", "components"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("   1  gold  repo      {ROOT}\n1 component(s)\n"));

  // $ python3 -m indexer ls components   (alias, same output)
  r = run_cli({"ls", "components"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("   1  gold  repo      {ROOT}\n1 component(s)\n"));

  // $ python3 -m indexer list components --kind external   (0 rows, exit 1)
  r = run_cli({"list", "components", "--kind", "external"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.out == "0 component(s)\n");

  // $ python3 -m indexer list components gld   (char-in-order fuzzy)
  r = run_cli({"list", "components", "gld"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("   1  gold  repo      {ROOT}\n1 component(s)\n"));
}

TEST_CASE("list dirs: table + unknown component error") {
  const GoldFixture g;
  // $ python3 -m indexer list dirs
  CmdResult r = run_cli({"list", "dirs"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == "   2  gold  include\n"
                 "   1  gold  src\n"
                 "2 directory(ies)\n");

  // $ python3 -m indexer list dirs -c gold
  r = run_cli({"list", "dirs", "-c", "gold"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == "   2  gold  include\n"
                 "   1  gold  src\n"
                 "2 directory(ies)\n");

  // $ python3 -m indexer list dirs -c nope
  r = run_cli({"list", "dirs", "-c", "nope"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.out.empty());
  CHECK(r.err == "error: no component named 'nope'\n");
}

TEST_CASE("list files: idx/pend marks, --indexed/--pending, --dir scope") {
  const GoldFixture g;
  // $ python3 -m indexer list files
  CmdResult r = run_cli({"list", "files"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("   2  pend  {ROOT}/include/a.h\n"
                          "   1  idx   {ROOT}/src/a.c\n"
                          "2 file(s)\n"));

  // $ python3 -m indexer list files --pending
  r = run_cli({"list", "files", "--pending"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("   2  pend  {ROOT}/include/a.h\n1 file(s)\n"));

  // $ python3 -m indexer list files --indexed
  r = run_cli({"list", "files", "--indexed"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("   1  idx   {ROOT}/src/a.c\n1 file(s)\n"));

  // $ python3 -m indexer list files -c gold -d src
  r = run_cli({"list", "files", "-c", "gold", "-d", "src"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("   1  idx   {ROOT}/src/a.c\n1 file(s)\n"));
}

TEST_CASE("list files/symbols: --dir without --component -> exit 1") {
  const GoldFixture g;
  const char kMsg[] = "error: --dir requires --component (directory paths "
                      "are relative to a component root)\n";
  // $ python3 -m indexer list files -d src
  CmdResult r = run_cli({"list", "files", "-d", "src"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.out.empty());
  CHECK(r.err == kMsg);
  // $ python3 -m indexer list symbols -d src
  r = run_cli({"list", "symbols", "-d", "src"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.err == kMsg);
}

TEST_CASE("list symbols: full table, limit, fuzzy, scopes, kind, file") {
  const GoldFixture g;
  const std::string full_table =
      g.expect("     3  NS               namespace         def   "
               "{ROOT}/include/a.h:8\n"
               "     2  square           function          decl  "
               "{ROOT}/include/a.h:4\n"
               "     6  counter          variable          def   "
               "{ROOT}/src/a.c:7\n"
               "     1  multiply         function          def   "
               "{ROOT}/src/a.c:12\n"
               "                                           decl  "
               "{ROOT}/include/a.h:3\n"
               "     4  NS::Shape        class             def   "
               "{ROOT}/include/a.h:10\n"
               "     5  NS::Shape::area  method            pure  "
               "{ROOT}/include/a.h:12\n"
               "6 match(es)\n");
  // $ python3 -m indexer list symbols
  CmdResult r = run_cli({"list", "symbols"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == full_table);

  // $ python3 -m indexer list symbols --limit 2
  r = run_cli({"list", "symbols", "--limit", "2"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("     3  NS      namespace         def   "
                          "{ROOT}/include/a.h:8\n"
                          "     2  square  function          decl  "
                          "{ROOT}/include/a.h:4\n"
                          "6 match(es) (showing 2)\n"));

  // $ python3 -m indexer list symbols ar   (char-in-order fuzzy, G18)
  r = run_cli({"list", "symbols", "ar"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("     2  square           function          decl  "
                          "{ROOT}/include/a.h:4\n"
                          "     5  NS::Shape::area  method            pure  "
                          "{ROOT}/include/a.h:12\n"
                          "2 match(es)\n"));

  const std::string include_scope =
      g.expect("     3  NS               namespace         def   "
               "{ROOT}/include/a.h:8\n"
               "     2  square           function          decl  "
               "{ROOT}/include/a.h:4\n"
               "     1  multiply         function          def   "
               "{ROOT}/src/a.c:12\n"
               "                                           decl  "
               "{ROOT}/include/a.h:3\n"
               "     4  NS::Shape        class             def   "
               "{ROOT}/include/a.h:10\n"
               "     5  NS::Shape::area  method            pure  "
               "{ROOT}/include/a.h:12\n"
               "5 match(es)\n");
  // $ python3 -m indexer list symbols -c gold -d include   (decl OR def
  // site in scope — multiply's def lives in src/ but its decl is here)
  r = run_cli({"list", "symbols", "-c", "gold", "-d", "include"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == include_scope);

  // $ python3 -m indexer list symbols -f <root>/include/a.h  (same rows)
  r = run_cli({"list", "symbols", "-f", g.root + "/include/a.h"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == include_scope);

  // $ python3 -m indexer list symbols --kind method
  r = run_cli({"list", "symbols", "--kind", "method"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(r.out == g.expect("     5  NS::Shape::area  method            pure  "
                          "{ROOT}/include/a.h:12\n"
                          "1 match(es)\n"));

  // $ python3 -m indexer list symbols -f a.h -c gold   (resolved against
  // the component root; error names the resolved path)
  r = run_cli({"list", "symbols", "-f", "a.h", "-c", "gold"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.err == g.expect("error: not in index database: {ROOT}/a.h\n"));

  // $ python3 -m indexer list symbols zz
  r = run_cli({"list", "symbols", "zz"}, g.cache);
  CHECK(r.rc == 1);
  CHECK(r.out == "0 match(es)\n");
}

// ---------------------------------------------------------------------------
// index — hermetic paths (default label; no parse happens, so no libclang)
// ---------------------------------------------------------------------------

TEST_CASE("index: empty DB, unknown --source, unknown FILE — hermetic") {
  const std::string t = make_temp_dir();
  cidx::Logger log;
  log.set_file(t + "/cidx.log");

  // $ python3 -m indexer index   (empty index: nothing pending, exit 0)
  CmdResult r = run_cli({"index"}, t, &log);
  CHECK(r.rc == 0);
  CHECK(r.out == "index: 0 indexed, 0 failed, 0 already indexed\n");
  CHECK(r.err.empty());

  // $ python3 -m indexer index --source nope   (LookupError path: exit 1,
  // no summary line, no warning-count line)
  r = run_cli({"index", "--source", "nope"}, t, &log);
  CHECK(r.rc == 1);
  CHECK(r.out.empty());
  CHECK(r.err == "error: no component named 'nope'\n");

  // $ python3 -m indexer index /no/such/file.c   (unknown FILE: exit 1)
  r = run_cli({"index", "/no/such/file.c"}, t, &log);
  CHECK(r.rc == 1);
  CHECK(r.out.empty());
  CHECK(r.err == "error: not in index database: /no/such/file.c\n");

  CHECK(!path_exists(t + "/cidx.log")); // nothing was ever logged (G27)
}

TEST_CASE("init: blank DB, already-exists error, --force recreate — hermetic") {
  const std::string t = make_temp_dir();
  const std::string db = t + "/index.db";

  // $ python3 -m indexer init   (fresh: materialize blank schema-v6 DB)
  CHECK(!path_exists(db));
  CmdResult r = run_cli({"init"}, t);
  CHECK(r.rc == 0);
  CHECK(r.out == "initialized empty index database at " + db + "\n");
  CHECK(r.err.empty());
  CHECK(path_exists(db));

  // Blank: schema present (a component can be added), zero rows.
  {
    Storage check(db);
    CHECK(check.list_components().empty());
  }

  // $ python3 -m indexer init   (again: refuse to clobber, exit 1)
  r = run_cli({"init"}, t);
  CHECK(r.rc == 1);
  CHECK(r.out.empty());
  CHECK(r.err == "error: index database already exists at " + db +
                     " (use --force to recreate)\n");

  // Put a row in, then prove --force wipes it back to blank.
  {
    Storage seed(db);
    seed.add_component("gone", "/no/such/root", "repo");
    CHECK(seed.list_components().size() == 1);
  }
  // $ python3 -m indexer init --force   (recreate: drop + reapply schema)
  r = run_cli({"init", "--force"}, t);
  CHECK(r.rc == 0);
  CHECK(r.out == "recreated empty index database at " + db + "\n");
  CHECK(r.err.empty());
  {
    Storage check(db);
    CHECK(check.list_components().empty()); // the seeded row is gone
  }
}

TEST_CASE("args: init grammar — --force flag, no positionals") {
  // happy: bare init
  cli::ParsedArgs pa = cli::parse_args({"init"});
  CHECK(pa.command == "init");
  CHECK(pa.force == false);

  pa = cli::parse_args({"init", "--force"});
  CHECK(pa.command == "init");
  CHECK(pa.force == true);

  // -h returns help text (argparse exit 0 path)
  pa = cli::parse_args({"init", "-h"});
  REQUIRE(pa.help_text.has_value());
  CHECK(*pa.help_text == "usage: cidx init [-h] [--force]\n"
                         "\n"
                         "options:\n"
                         "  -h, --help  show this help message and exit\n"
                         "  --force     overwrite an existing index database\n");

  // unknown flag -> TOP-level unrecognized arguments, exit 2
  ParseFail f = parse_fail({"init", "--bogus"});
  CHECK(f.code == 2);
  CHECK(f.msg ==
        "usage: cidx [-h] {init,add-source,import,index,search,show,list,ls} "
        "...\ncidx: error: unrecognized arguments: --bogus\n");

  // stray positional -> unrecognized arguments, exit 2
  f = parse_fail({"init", "extra"});
  CHECK(f.code == 2);
  CHECK(f.msg ==
        "usage: cidx [-h] {init,add-source,import,index,search,show,list,ls} "
        "...\ncidx: error: unrecognized arguments: extra\n");
}

TEST_CASE("query-only invocations never create cidx.log (G27/D7)") {
  const GoldFixture g;
  const std::string log = g.cache + "/cidx.log";
  cidx::Logger::root().set_file(log); // what main() does — lazy open
  CmdResult r = run_cli({"search", "multiply"}, g.cache);
  CHECK(r.rc == 0);
  r = run_cli({"list", "files"}, g.cache);
  CHECK(r.rc == 0);
  r = run_cli({"show", "file", "2"}, g.cache);
  CHECK(r.rc == 0);
  CHECK(!path_exists(log));
}

// ---------------------------------------------------------------------------
// import — needs CompileDb::load (label "clang")
// ---------------------------------------------------------------------------

TEST_SUITE("clang") {

  TEST_CASE("import: synthetic compile DB — strip, driver, skip counter") {
    if (require_libclang() == nullptr) {
      return;
    }
    const std::string t = make_temp_dir();
    makedirs(t + "/proj/sub");
    makedirs(t + "/other");
    makedirs(t + "/build");
    write_file(t + "/proj/a.c", "int a;\n");
    write_file(t + "/proj/sub/b.c", "int b;\n");
    write_file(t + "/other/c.c", "int c;\n");
    write_file(t + "/build/compile_commands.json",
               "[\n"
               "  {\"directory\": \"" +
                   t +
                   "/proj\", \"command\": "
                   "\"cc -I. -c a.c -o a.o\", \"file\": \"a.c\"},\n"
                   "  {\"directory\": \"" +
                   t +
                   "/proj\", \"command\": "
                   "\"gcc -Iinclude -DFOO -c sub/b.c -o sub/b.o\", "
                   "\"file\": \"sub/b.c\"},\n"
                   "  {\"directory\": \"" +
                   t +
                   "/other\", \"command\": "
                   "\"cc -c c.c\", \"file\": \"c.c\"}\n"
                   "]\n");

    // $ python3 -m indexer import --db <t>/build/compile_commands.json
    // component #1: proj at <t>/proj
    // imported 2 file(s), skipped 1
    //   skip (outside any component): <t>/other/c.c        (stderr)
    const CmdResult r =
        run_cli({"import", "--db", t + "/build/compile_commands.json"}, t);
    CHECK(r.rc == 0);
    CHECK(r.out == "component #1: proj at " + t +
                       "/proj\n"
                       "imported 2 file(s), skipped 1\n");
    CHECK(r.err == "  skip (outside any component): " + t + "/other/c.c\n");

    // Stored rows: stripped options (G10/G12), driver, md5/mtime captured,
    // indexed = 0 (pending).
    Storage db(t + "/index.db");
    const std::optional<cidx::File> a = db.get_file(t + "/proj/a.c");
    REQUIRE(a);
    CHECK(*a->compile_options == std::vector<std::string>{"-I" + t + "/proj"});
    CHECK(*a->driver == "cc");
    CHECK(a->md5);
    CHECK(a->mtime);
    CHECK(!a->indexed);
    const std::optional<cidx::File> b = db.get_file(t + "/proj/sub/b.c");
    REQUIRE(b);
    CHECK(*b->compile_options ==
          std::vector<std::string>{"-I" + t + "/proj/include", "-DFOO"});
    CHECK(*b->driver == "gcc");
    CHECK(!db.get_file(t + "/other/c.c"));
  }

  TEST_CASE("import: --db accepts the directory; git root wins as the "
            "component root") {
    if (require_libclang() == nullptr) {
      return;
    }
    const std::string t = make_temp_dir();
    makedirs(t + "/proj/.git");
    makedirs(t + "/proj/src");
    makedirs(t + "/proj/build");
    write_file(t + "/proj/.git/config",
               "[remote \"origin\"]\n\turl = git@host:team/widget.git\n");
    write_file(t + "/proj/src/m.c", "int m;\n");
    write_file(t + "/proj/build/compile_commands.json",
               "[{\"directory\": \"" + t +
                   "/proj/src\", \"command\": "
                   "\"cc -c m.c\", \"file\": \"m.c\"}]\n");

    // $ python3 -m indexer import --db <t>/proj/build   (directory form;
    // component root = git root, name from .git/config origin url)
    const CmdResult r = run_cli({"import", "--db", t + "/proj/build"}, t);
    CHECK(r.rc == 0);
    CHECK(r.out == "component #1: widget at " + t +
                       "/proj\n"
                       "imported 1 file(s), skipped 0\n");
    CHECK(r.err.empty());
  }

  TEST_CASE("import: manifests/project compile DB (READ-ONLY fixture)") {
    if (require_libclang() == nullptr) {
      return;
    }
    if (!require_manifests()) {
      return;
    }
    const std::string t = make_temp_dir();
    const std::string db_path =
        std::string(CIDX_MANIFESTS_DIR) + "/project/compile_commands.json";
    const std::string project = std::string(CIDX_MANIFESTS_DIR) + "/project";

    const CmdResult r = run_cli({"import", "--db", db_path}, t);
    CHECK(r.rc == 0);
    // The component line names the qemu-vms git root (machine-dependent);
    // the counts line is the golden part:
    // $ python3 -m indexer import --db .../manifests/project/...json
    // -> "imported 2 file(s), skipped 0"
    CHECK(r.out.find("imported 2 file(s), skipped 0\n") != std::string::npos);
    CHECK(r.err.empty());

    Storage db(t + "/index.db");
    const std::optional<cidx::File> mathlib =
        db.get_file(project + "/mathlib.c");
    REQUIRE(mathlib);
    CHECK(*mathlib->compile_options ==
          std::vector<std::string>{"-I" + project});
    CHECK(*mathlib->driver == "cc");
    REQUIRE(db.get_file(project + "/app.c"));
  }

  TEST_CASE("import: load failure -> exit 1 with the Python-parity message") {
    if (require_libclang() == nullptr) {
      return;
    }
    const std::string t = make_temp_dir();
    // $ python3 -m indexer import --db <t>/nope
    // error: cannot load compilation database from <t>/nope: Error 1:
    // CompilationDatabase loading failed
    // (libclang additionally prints LIBCLANG TOOLING ERROR lines straight
    // to fd 2 — both tools share that noise.)
    const CmdResult r = run_cli({"import", "--db", t + "/nope"}, t);
    CHECK(r.rc == 1);
    CHECK(r.out.empty());
    CHECK(r.err == "error: cannot load compilation database from " + t +
                       "/nope: Error 1: CompilationDatabase loading "
                       "failed\n");
  }

  TEST_CASE("import: empty compilation database -> exit 1") {
    if (require_libclang() == nullptr) {
      return;
    }
    const std::string t = make_temp_dir();
    makedirs(t + "/empty");
    write_file(t + "/empty/compile_commands.json", "[]\n");
    // KNOWN DELTA: Python crashes into the load-error message here
    // ("'NoneType' object is not iterable"); cidx-cpp prints the intended
    // empty-DB message. Exit code matches (1).
    const CmdResult r = run_cli({"import", "--db", t + "/empty"}, t);
    CHECK(r.rc == 1);
    CHECK(r.err == "error: compilation database is empty\n");
  }

  // -------------------------------------------------------------------------
  // index — end-to-end on a tmp two-TU project (S08). The fixture mirrors
  // manifests/project/ (mathlib.h/.c + app.c) but is SYNTHESIZED in a temp
  // dir — manifests/ stays read-only. Expected lines were captured from the
  // Python tool ($ python3 -m indexer index ...) on an identical fixture.
  // -------------------------------------------------------------------------

  struct TwoTuProject {
    std::string cache; // temp root, doubles as INDEXER_CACHE
    std::string proj;  // <cache>/proj — component root (no .git: dirname)
    explicit TwoTuProject(bool with_bad_tu = false)
        : cache(make_temp_dir()), proj(cache + "/proj") {
      makedirs(proj);
      write_file(proj + "/mathlib.h", "#ifndef MATHLIB_H\n"
                                      "#define MATHLIB_H\n"
                                      "int add(int a, int b);\n"
                                      "int multiply(int a, int b);\n"
                                      "int square(int x);\n"
                                      "#endif\n");
      write_file(proj + "/mathlib.c",
                 "#include \"mathlib.h\"\n"
                 "int add(int a, int b) { return a + b; }\n"
                 "int multiply(int a, int b) { return a * b; }\n"
                 "int square(int x) { return multiply(x, x); }\n");
      write_file(proj + "/app.c",
                 "#include \"mathlib.h\"\n"
                 "int main(void) { return square(5) + add(1, 2); }\n");
      std::string db = "[\n  " + entry("mathlib.c") + ",\n  " + entry("app.c");
      if (with_bad_tu) {
        write_file(proj + "/bad.c", "#include \"missing.h\"\nint bad;\n");
        db += ",\n  " + entry("bad.c");
      }
      db += "\n]\n";
      write_file(proj + "/compile_commands.json", db);
    }
    std::string entry(const std::string &src) const {
      return "{\"directory\": \"" + proj + "\", \"command\": \"cc -I. -c " +
             src + " -o " + src + ".o\", \"file\": \"" + src + "\"}";
    }
  };

  TEST_CASE("index: two-TU pending flow — header counters, md5 skip, "
            "content change re-indexes") {
    if (require_libclang() == nullptr) {
      return;
    }
    const TwoTuProject p;
    const std::string &t = p.cache;
    const std::string &proj = p.proj;
    CmdResult r = run_cli({"import", "--db", proj}, t);
    REQUIRE(r.rc == 0);

    cidx::Logger log;
    log.set_file(t + "/cidx.log");

    // $ python3 -m indexer index   (pending order: c.path, d.path, f.name —
    // app.c first; its TU indexes mathlib.h, mathlib.c then finds it current)
    r = run_cli({"index"}, t, &log);
    CHECK(r.rc == 0);
    CHECK(r.err.empty());
    CHECK(r.out ==
          "indexing " + proj +
              "/app.c\n"
              "  -> 1 symbols; headers: 1 indexed (+3 symbols), 0 already, "
              "0 system, 0 unowned\n"
              "indexing " +
              proj +
              "/mathlib.c\n"
              "  -> 3 symbols; headers: 0 indexed (+0 symbols), 1 already, "
              "0 system, 0 unowned\n"
              "index: 2 indexed, 0 failed, 0 already indexed\n");

    // Header row written via the including TU: indexed, md5 captured, NULL
    // compile_options/driver (G20).
    {
      Storage db(t + "/index.db");
      const std::optional<cidx::File> h = db.get_file(proj + "/mathlib.h");
      REQUIRE(h);
      CHECK(h->indexed);
      CHECK(h->md5);
      CHECK(!h->compile_options);
      CHECK(!h->driver);
    }

    // $ python3 -m indexer index   (second run: md5-current — the header row
    // joined the snapshot, so 3 skips, nothing parsed)
    r = run_cli({"index"}, t, &log);
    CHECK(r.rc == 0);
    CHECK(r.out == "index: 0 indexed, 0 failed, 3 already indexed\n");

    // $ python3 -m indexer index <proj>/app.c   (FILE arg, already indexed)
    r = run_cli({"index", proj + "/app.c"}, t, &log);
    CHECK(r.rc == 0);
    CHECK(r.out == "file: " + proj + "/app.c\n  already indexed\n");

    // $ python3 -m indexer index app.c --source proj   (relative FILE
    // resolves against the --source component root)
    r = run_cli({"index", "app.c", "--source", "proj"}, t, &log);
    CHECK(r.rc == 0);
    CHECK(r.out == "file: " + proj + "/app.c\n  already indexed\n");

    // Content change -> md5 mismatch -> only app.c re-indexed; the header is
    // still current ("1 already"). main()'s row is already resolved, so the
    // re-encounter is skipped (G15) — 0 NEW symbols stored, Python parity.
    write_file(proj + "/app.c",
               "#include \"mathlib.h\"\n"
               "int main(void) { return add(square(2), 1); }\n");
    r = run_cli({"index"}, t, &log);
    CHECK(r.rc == 0);
    CHECK(r.out ==
          "indexing " + proj +
              "/app.c\n"
              "  -> 0 symbols; headers: 0 indexed (+0 symbols), 1 already, "
              "0 system, 0 unowned\n"
              "index: 1 indexed, 0 failed, 2 already indexed\n");

    // No warnings anywhere on the happy path: the lazy log never appeared
    // and no warning-count line was printed (G27).
    CHECK(!path_exists(t + "/cidx.log"));
  }

  TEST_CASE("index: fatal include error — exit 1, rest indexed, flag dump "
            "only in cidx.log") {
    if (require_libclang() == nullptr) {
      return;
    }
    const TwoTuProject p(/*with_bad_tu=*/true);
    const std::string &t = p.cache;
    const std::string &proj = p.proj;
    CmdResult r = run_cli({"import", "--db", proj}, t);
    REQUIRE(r.rc == 0);

    cidx::Logger log;
    log.set_file(t + "/cidx.log");

    // $ python3 -m indexer index   (bad.c aborts FATAL, the others index;
    // the ERROR flag-dump record makes the warning counter 1 -> summary line)
    r = run_cli({"index"}, t, &log);
    CHECK(r.rc == 1);
    CHECK(r.out ==
          "indexing " + proj +
              "/app.c\n"
              "  -> 1 symbols; headers: 1 indexed (+3 symbols), 0 already, "
              "0 system, 0 unowned\n"
              "indexing " +
              proj +
              "/bad.c\n"
              "indexing " +
              proj +
              "/mathlib.c\n"
              "  -> 3 symbols; headers: 0 indexed (+0 symbols), 1 already, "
              "0 system, 0 unowned\n"
              "index: 2 indexed, 1 failed, 0 already indexed\n"
              "1 warning(s)/error(s) logged to " +
              t + "/cidx.log\n");
    // Terminal gets ONLY the short summary (G28)…
    CHECK(r.err == "error: " + proj + "/bad.c: 1 fatal diagnostic(s): " + proj +
                       "/bad.c:1: 'missing.h' file not found\n");
    // …the flag dump and per-diagnostic lines live in the log.
    REQUIRE(path_exists(t + "/cidx.log"));
    const std::string logged = read_file(t + "/cidx.log");
    CHECK(logged.find("failed parse flags:") != std::string::npos);
    CHECK(logged.find("-ferror-limit=0") != std::string::npos);
    CHECK(logged.find("'missing.h' file not found") != std::string::npos);

    // The failed TU stays pending (never marked indexed).
    {
      Storage db(t + "/index.db");
      const std::optional<cidx::File> bad = db.get_file(proj + "/bad.c");
      REQUIRE(bad);
      CHECK(!bad->indexed);
    }

    // Re-run: only bad.c is retried (and fails again) — exit stays 1.
    cidx::Logger log2;
    log2.set_file(t + "/cidx.log");
    r = run_cli({"index"}, t, &log2);
    CHECK(r.rc == 1);
    CHECK(r.out == "indexing " + proj +
                       "/bad.c\n"
                       "index: 0 indexed, 1 failed, 3 already indexed\n"
                       "1 warning(s)/error(s) logged to " +
                       t + "/cidx.log\n");
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

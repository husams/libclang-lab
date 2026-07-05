// ast_test — S05: Parser + diagnostic policy (design §5.7; analysis
// §5.2-§5.3; G5, G6, G27, G28). S06 extends this same file with the
// walk/extraction suites.
//
// The final-argv assembly case is hermetic (resource include injected via
// the S04 test seam) and runs under the "default" label. Everything else
// performs real parses, lives in doctest suite "clang", and runtime-SKIPs
// (exit 77) when no libclang loads — same pattern as toolchain_test. All
// sources are synthesized in temp dirs; manifests/ is never touched.
#define DOCTEST_CONFIG_IMPLEMENT
#include "doctest/doctest.h"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <unistd.h>

#include "clangx/ast.hpp"
#include "clangx/libclang.hpp"
#include "clangx/parse.hpp"
#include "clangx/toolchain.hpp"
#include "storage/records.hpp"
#include "storage/storage.hpp"
#include "util/errors.hpp"
#include "util/logger.hpp"

namespace fs = std::filesystem;
using cidx::AstIndexer;
using cidx::ClangParseError;
using cidx::HeaderStats;
using cidx::LibClang;
using cidx::Logger;
using cidx::ParsedTu;
using cidx::Parser;
using cidx::Storage;
using cidx::Symbol;
using cidx::Toolchain;

namespace {

bool g_clang_skipped = false;

// Returns true when CIDX_MANIFESTS_DIR points at an existing directory.
// On a host without the lab checkout (e.g. the e2e box that only rsyncs
// cidx-cpp/) the fixture cases should SKIP rather than fail.
bool require_manifests() {
  if (!fs::is_directory(CIDX_MANIFESTS_DIR)) {
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
  char tmpl[] = "/tmp/cidx_ast_XXXXXX";
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

std::string read_file(const std::string &path) {
  std::ifstream in(path, std::ios::binary);
  std::string s((std::istreambuf_iterator<char>(in)),
                std::istreambuf_iterator<char>());
  return s;
}

std::size_t count_occurrences(const std::string &haystack,
                              const std::string &needle) {
  std::size_t n = 0;
  for (std::size_t pos = haystack.find(needle); pos != std::string::npos;
       pos = haystack.find(needle, pos + needle.size())) {
    ++n;
  }
  return n;
}

// setenv/unsetenv with restore-on-destruction. value == nullptr unsets.
class ScopedEnv {
public:
  ScopedEnv(const char *name, const char *value) : name_(name) {
    const char *prev = std::getenv(name);
    if (prev != nullptr) {
      prev_ = prev;
    }
    if (value != nullptr) {
      ::setenv(name, value, 1);
    } else {
      ::unsetenv(name);
    }
  }
  ~ScopedEnv() {
    if (prev_) {
      ::setenv(name_, prev_->c_str(), 1);
    } else {
      ::unsetenv(name_);
    }
  }
  ScopedEnv(const ScopedEnv &) = delete;
  ScopedEnv &operator=(const ScopedEnv &) = delete;

private:
  const char *name_;
  std::optional<std::string> prev_;
};

// Redirect an fd into a temp file; stop() restores the fd and returns what
// was captured. Used to prove the fatal flag dump never hits the terminal
// (G28).
class CaptureFd {
public:
  CaptureFd(int fd, const std::string &path) : fd_(fd), path_(path) {
    ::fflush(nullptr);
    saved_ = ::dup(fd_);
    const int sink = ::open(path_.c_str(), O_CREAT | O_WRONLY | O_TRUNC, 0600);
    ::dup2(sink, fd_);
    ::close(sink);
  }
  std::string stop() {
    if (saved_ != -1) {
      ::fflush(nullptr);
      ::dup2(saved_, fd_);
      ::close(saved_);
      saved_ = -1;
    }
    return read_file(path_);
  }
  ~CaptureFd() { stop(); }
  CaptureFd(const CaptureFd &) = delete;
  CaptureFd &operator=(const CaptureFd &) = delete;

private:
  int fd_;
  std::string path_;
  int saved_ = -1;
};

// One temp dir + private Logger (file sink on a tmp cidx.log) + per-run
// Toolchain + Parser. CIDX_STRICT is neutralized; the strict test overrides
// it with its own ScopedEnv.
struct ParseFixture {
  ScopedEnv strict{"CIDX_STRICT", nullptr};
  std::string tmp = make_temp_dir();
  std::string log_path = tmp + "/cidx.log";
  Logger log;
  Toolchain tc{log};
  Parser parser{tc, log};

  ParseFixture() { log.set_file(log_path); }
  std::string logged() const { return read_file(log_path); }
};

// N distinct semantic errors, one per line: line i+1 holds
// "use of undeclared identifier 'no_such_<i>'". No fatals.
std::string write_error_source(const std::string &dir, int n) {
  std::string src;
  for (int i = 0; i < n; ++i) {
    src += "int f" + std::to_string(i) + "(void) { return no_such_" +
           std::to_string(i) + "; }\n";
  }
  const std::string path = dir + "/errors_" + std::to_string(n) + ".c";
  write_file(path, src);
  return path;
}

std::string diag_line(const std::string &tu_path, const std::string &file,
                      unsigned line, const std::string &msg) {
  return tu_path + ": diag " + file + ":" + std::to_string(line) + ": " + msg;
}

} // namespace

// ---------------------------------------------------------------------------
// hermetic: final argv assembly (G5) — no libclang needed

TEST_CASE("final argv = stored args + toolchain_flags(cpp, driver) + "
          "-ferror-limit=0, in that order") {
  ScopedEnv strict("CIDX_STRICT", nullptr);
  const std::string tmp = make_temp_dir();
  const std::string res = tmp + "/res";
  fs::create_directories(res);

  Logger log;
  Toolchain tc(log);
  tc.set_resource_include_for_test(res);
  Parser parser(tc, log);

  const std::vector<std::string> stored = {"-DFOO=1", "-I/tmp/x"};
  const std::vector<std::string> argv =
      parser.final_args("/tmp/foo.c", stored, std::nullopt);

  REQUIRE(argv.size() >= 3);
  CHECK(argv[0] == "-DFOO=1");
  CHECK(argv[1] == "-I/tmp/x");
  CHECK(argv.back() == "-ferror-limit=0"); // G5: always appended last
  // -ferror-limit=0 appears exactly once
  CHECK(std::count(argv.begin(), argv.end(), std::string("-ferror-limit=0")) ==
        1);
  // the middle block is toolchain_flags(cpp=false for .c, no driver) verbatim
  const std::vector<std::string> toolchain =
      tc.toolchain_flags(false, std::nullopt);
  const std::vector<std::string> middle(argv.begin() + 2, argv.end() - 1);
  CHECK(middle == toolchain);
  CHECK(std::find(middle.begin(), middle.end(), res) != middle.end());
}

// ---------------------------------------------------------------------------
// real parses

TEST_SUITE("clang") {

  TEST_CASE("clean parse: TU produced, spelling = path as passed, no log "
            "records at all") {
    if (require_libclang() == nullptr) {
      return;
    }
    ParseFixture f;
    const std::string path = f.tmp + "/ok.c";
    write_file(path, "int the_answer(void) { return 42; }\n");

    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    CHECK(tu.tu != nullptr);
    CHECK(tu.index != nullptr);
    CHECK(tu.spelling == path); // G24 prerequisite
    CHECK(f.log.warning_count() == 0);
    // delay=True parity (G27): a clean parse writes nothing, so the lazily
    // created log file must not even exist.
    CHECK_FALSE(fs::exists(f.log_path));
  }

  TEST_CASE("CIDX_MEM=1: a successful parse logs one per-TU memory line") {
    if (require_libclang() == nullptr) {
      return;
    }
    ScopedEnv mem("CIDX_MEM", "1");
    ParseFixture f;
    const std::string path = f.tmp + "/mem.c";
    write_file(path, "int the_answer(void) { return 42; }\n");

    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    CHECK(tu.tu != nullptr);

    const std::string log = f.logged();
    CHECK(log.find(path + ": TU memory total=") != std::string::npos);
    CHECK(log.find(" bytes (") != std::string::npos);
    // A non-empty category breakdown follows the "KiB); " marker (a real
    // parse always uses memory in at least one category, e.g. the AST).
    const std::size_t pos = log.find("KiB); ");
    REQUIRE(pos != std::string::npos);
    CHECK(log.size() > pos + 6);
    CHECK(log[pos + 6] != '\n'); // breakdown is present, not blank
  }

  TEST_CASE(
      "CIDX_MEM unset: a clean parse still writes nothing (default off)") {
    if (require_libclang() == nullptr) {
      return;
    }
    ScopedEnv mem("CIDX_MEM", nullptr);
    ParseFixture f;
    const std::string path = f.tmp + "/nomem.c";
    write_file(path, "int x(void) { return 0; }\n");

    f.parser.parse(path, {}, std::nullopt);
    CHECK_FALSE(fs::exists(f.log_path)); // G27: lazy log, nothing written
  }

  TEST_CASE("nonexistent source -> ClangParseError(\"cannot parse <file>\")") {
    if (require_libclang() == nullptr) {
      return;
    }
    ParseFixture f;
    const std::string path = f.tmp + "/does-not-exist.c";
    CHECK_THROWS_WITH_AS(f.parser.parse(path, {}, std::nullopt),
                         ("cannot parse " + path).c_str(), ClangParseError);
  }

  TEST_CASE("semantic errors, default mode: parse succeeds, ONE warning, "
            "exact summary + INFO diag lines in the log") {
    if (require_libclang() == nullptr) {
      return;
    }
    ParseFixture f;
    const std::string path = write_error_source(f.tmp, 3);

    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    CHECK(tu.tu != nullptr);
    // G27: the summary alone carries WARNING — counter +1 for the file
    CHECK(f.log.warning_count() == 1);

    const std::string logged = f.logged();
    CHECK(logged.find("WARNING cidx.clang: " + path +
                      ": 3 error diagnostic(s) ignored (CIDX_STRICT=1 to "
                      "abort)") != std::string::npos);
    // per-diag INFO lines, exact format "<TU>: diag <file>:<line>: <msg>"
    for (unsigned i = 0; i < 3; ++i) {
      CHECK(logged.find("INFO cidx.clang: " +
                        diag_line(path, path, i + 1,
                                  "use of undeclared identifier 'no_such_" +
                                      std::to_string(i) + "'")) !=
            std::string::npos);
    }
    CHECK(count_occurrences(logged, path + ": diag ") == 3);
    CHECK(logged.find("suppressed") == std::string::npos);
    // tolerated parse: no flag dump
    CHECK(logged.find("failed parse flags") == std::string::npos);
  }

  TEST_CASE("CIDX_STRICT=1: same errors abort; message = first 3 "
            "';'-joined") {
    if (require_libclang() == nullptr) {
      return;
    }
    ParseFixture f;
    ScopedEnv strict("CIDX_STRICT", "1");
    const std::string path = write_error_source(f.tmp, 5);

    const std::string expected =
        path + ": 5 fatal diagnostic(s): " + path +
        ":1: use of undeclared identifier 'no_such_0'; " + path +
        ":2: use of undeclared identifier 'no_such_1'; " + path +
        ":3: use of undeclared identifier 'no_such_2'";
    CHECK_THROWS_WITH_AS(f.parser.parse(path, {}, std::nullopt),
                         expected.c_str(), ClangParseError);
    // the abort path logs the flag dump at ERROR -> counter +1, still one
    // record per file
    CHECK(f.log.warning_count() == 1);
    const std::string logged = f.logged();
    CHECK(logged.find("ERROR cidx.clang: " + path + ": failed parse flags: ") !=
          std::string::npos);
    CHECK(count_occurrences(logged, path + ": diag ") == 5);
  }

  TEST_CASE("fatal diagnostic: throw; flag dump + libclang major in the LOG "
            "only, nothing on stdout/stderr (G28)") {
    LibClang *lib = require_libclang();
    if (lib == nullptr) {
      return;
    }
    ParseFixture f;
    const std::string path = f.tmp + "/bad.c";
    write_file(path, "#include \"no-such-header.h\"\nint x;\n");

    bool threw = false;
    std::string message;
    std::vector<cidx::Diagnostic> carried;
    std::string out;
    std::string err;
    {
      CaptureFd cap_out(1, f.tmp + "/captured.out");
      CaptureFd cap_err(2, f.tmp + "/captured.err");
      try {
        f.parser.parse(path, {}, std::nullopt);
      } catch (const ClangParseError &e) {
        threw = true;
        message = e.what();
        carried = e.diagnostics();
      }
      out = cap_out.stop();
      err = cap_err.stop();
    }
    REQUIRE(threw);
    CHECK(message == path + ": 1 fatal diagnostic(s): " + path +
                         ":1: 'no-such-header.h' file not found");
    // v15: the failed parse carries its diagnostics so the caller can still
    // record WHY the file failed (the fatal header-not-found).
    REQUIRE(carried.size() == 1);
    CHECK(carried[0].severity == 4); // fatal
    CHECK(carried[0].spelling == "'no-such-header.h' file not found");
    CHECK(carried[0].file_path == path);
    CHECK(carried[0].line == 1);
    // terminal stayed clean — the dump went to cidx.log
    CHECK(out.empty());
    CHECK(err.empty());

    const std::string logged = f.logged();
    CHECK(logged.find("ERROR cidx.clang: " + path + ": failed parse flags: ") !=
          std::string::npos);
    CHECK(logged.find("-ferror-limit=0; libclang: " +
                      std::to_string(lib->major())) != std::string::npos);
    CHECK(logged.find(
              "INFO cidx.clang: " +
              diag_line(path, path, 1, "'no-such-header.h' file not found")) !=
          std::string::npos);
    CHECK(f.log.warning_count() == 1);
  }

  TEST_CASE("30 errors: -ferror-limit=0 lifts the 20-cap (G5); exactly 25 "
            "INFO lines + the suppressed line") {
    if (require_libclang() == nullptr) {
      return;
    }
    ParseFixture f;
    const std::string path = write_error_source(f.tmp, 30);

    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    CHECK(tu.tu != nullptr);
    CHECK(f.log.warning_count() == 1);

    const std::string logged = f.logged();
    // all 30 errors were reported (the default 20-error cap would instead
    // have emitted a FATAL 'too many errors emitted' and aborted the parse)
    CHECK(logged.find(path + ": 30 error diagnostic(s) ignored "
                             "(CIDX_STRICT=1 to abort)") != std::string::npos);
    CHECK(count_occurrences(logged, path + ": diag ") == 25);
    CHECK(logged.find("INFO cidx.clang: " + path +
                      ": ... 5 more diagnostic(s) suppressed") !=
          std::string::npos);
    // first 25 shown, the rest cut: error #24 logged, #25 not
    CHECK(logged.find("'no_such_24'") != std::string::npos);
    CHECK(logged.find("'no_such_25'") == std::string::npos);
  }

} // TEST_SUITE("clang")

// ---------------------------------------------------------------------------
// S06: AST indexer — walk / extraction / header suites (design §5.8,
// analysis §5.6; G15, G20-G26). All cases perform real parses. The lab
// manifests are READ-ONLY: they are registered as an `external` component
// inside a throwaway :memory: DB and never written to (story test plan).

namespace {

const std::string kManifestsDir = CIDX_MANIFESTS_DIR;

// ParseFixture plus an in-memory Storage and the indexer under test. The
// system-header env knob is neutralized; cases that exercise it override
// with their own ScopedEnv.
struct IndexFixture : ParseFixture {
  ScopedEnv ignore_sys{cidx::kIgnoreSystemHeadersEnv, nullptr};
  Storage db; // :memory:
  AstIndexer indexer{db, log};

  // Register `root` as an external component (idempotent) and add the file.
  int64_t add_owned_file(const std::string &root, const std::string &path) {
    if (!db.get_component(root)) {
      db.add_component("fixture", root, "external");
    }
    return db.add_file_path(path);
  }
};

// The single stored symbol with this spelling (and kind, when given);
// nullopt when absent or ambiguous.
std::optional<Symbol>
one_sym(Storage &db, const std::string &spelling,
        const std::optional<std::string> &kind = std::nullopt) {
  const std::vector<Symbol> rows = db.lookup_symbols_by_name(spelling, kind);
  if (rows.size() != 1) {
    return std::nullopt;
  }
  return rows.front();
}

} // namespace

TEST_SUITE("clang") {

  TEST_CASE("kind map: all 16 reachable kinds map to the exact stored kind "
            "strings; unmapped kinds are ignored") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string path = f.tmp + "/kinds.cpp";
    write_file(path, "namespace the_ns {\n"
                     "struct the_struct { int the_member; };\n"
                     "union the_union { int ua; float ub; };\n"
                     "enum the_enum { THE_CONSTANT };\n"
                     "typedef int the_typedef;\n"
                     "using the_alias = float;\n"
                     "int the_function(int the_param);\n"
                     "int the_variable;\n"
                     "class the_class {\n"
                     "public:\n"
                     "  the_class();\n"
                     "  ~the_class();\n"
                     "  void the_method();\n"
                     "};\n"
                     "template <typename T> class the_class_template {};\n"
                     "template <typename T> T the_function_template(const T "
                     "&value);\n"
                     "} // namespace the_ns\n");
    const int64_t file_id = f.add_owned_file(f.tmp, path);
    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    // 16 named kinds + the two extra union members ua/ub
    CHECK(f.indexer.index_symbols(tu, tu.spelling, file_id) == 18);

    const std::pair<const char *, const char *> expected[] = {
        {"the_ns", "namespace"},
        {"the_struct", "struct"},
        {"the_member", "member"},
        {"the_union", "union"},
        {"the_enum", "enum"},
        {"THE_CONSTANT", "enum-constant"},
        {"the_typedef", "typedef"},
        {"the_alias", "type-alias"},
        {"the_function", "function"},
        {"the_variable", "variable"},
        {"the_class", "class"},
        {"the_class", "constructor"},
        {"~the_class", "destructor"},
        {"the_method", "method"},
        {"the_class_template", "class-template"},
        {"the_function_template", "function-template"},
    };
    for (const auto &exp : expected) {
      CAPTURE(exp.first);
      CAPTURE(exp.second);
      CHECK(f.db.lookup_symbols_by_name(exp.first, std::string(exp.second))
                .size() == 1);
    }
    // unmapped kinds (ParmDecl here) never become rows
    CHECK(f.db.lookup_symbols_by_name("the_param").empty());
    CHECK(f.db.lookup_symbols_by_name("value").empty());
    // qual_name is built from semantic parents
    const auto method = one_sym(f.db, "the_method");
    REQUIRE(method);
    CHECK(method->qual_name == "the_ns::the_class::the_method");
  }

  TEST_CASE("is_static: static member function flagged; instance methods and "
            "free functions are not (v12)") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string path = f.tmp + "/statics.cpp";
    write_file(path, "namespace ns {\n"
                     "struct Widget {\n"
                     "  static int make(int x);   // static member -> is_static\n"
                     "  int area() const;         // instance method\n"
                     "};\n"
                     "int Widget::make(int x) { return x; }\n"
                     "int Widget::area() const { return 0; }\n"
                     "int free_fn(int x) { return x; }          // external\n"
                     "static int hidden_fn(int x) { return x; } // internal\n"
                     "} // namespace ns\n");
    const int64_t file_id = f.add_owned_file(f.tmp, path);
    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    f.indexer.index_symbols(tu, tu.spelling, file_id);

    const auto make = one_sym(f.db, "make", std::string("method"));
    REQUIRE(make);
    CHECK_MESSAGE(make->is_static, "static member function -> is_static");

    const auto area = one_sym(f.db, "area", std::string("method"));
    REQUIRE(area);
    CHECK_FALSE_MESSAGE(area->is_static, "instance method -> not static");

    const auto ext = one_sym(f.db, "free_fn", std::string("function"));
    REQUIRE(ext);
    CHECK_FALSE(ext->is_static);

    // file-scope `static` free function: is_static stays false; its static-ness
    // is reflected by internal linkage, not by this method-only flag.
    const auto hidden = one_sym(f.db, "hidden_fn", std::string("function"));
    REQUIRE(hidden);
    CHECK_FALSE(hidden->is_static);
    CHECK(hidden->linkage == std::string("internal"));
  }

  TEST_CASE("templates: explicit instantiation -> instantiates (kind 5), "
            "explicit specialization -> specializes (kind 4); TYPE template_arg "
            "rows record the type spelling so Box<bool> != Box<int>") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string path = f.tmp + "/tmpl.cpp";
    write_file(path,
               "namespace nn {\n"
               "template <class T>\n"
               "class Box { T v_; public: explicit Box(T v): v_(v) {}\n"
               "           T get() const { return v_; } };\n"
               "template <>\n"
               "class Box<bool> { bool b_; public: explicit Box(bool b): "
               "b_(b) {}\n"
               "                  bool get() const { return b_; } };\n"
               "template class Box<int>;\n"
               "char use() { Box<char> bc('x'); return bc.get(); }\n"
               "} // namespace nn\n");
    const int64_t file_id = f.add_owned_file(f.tmp, path);
    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    f.indexer.index_symbols(tu, tu.spelling, file_id);
    f.indexer.index_edges(tu, tu.spelling, file_id);

    auto &raw = f.db.raw_db();

    // The class template Box has exactly one formal type parameter.
    {
      const auto box_tmpl =
          f.db.lookup_symbols_by_name("Box", std::string("class-template"));
      REQUIRE_FALSE(box_tmpl.empty());
      auto st = raw.prepare(
          "SELECT COUNT(*) FROM template_param WHERE owner_id = ?");
      st.bind(1, box_tmpl.front().id);
      st.step();
      CHECK(st.col_int64(0) == 1);
    }

    // The explicit instantiation `template class Box<int>;` is recorded as
    // `instantiates` (kind 5), NOT `specializes` (kind 4).
    {
      auto st = raw.prepare(
          "SELECT e.kind FROM edge e "
          "JOIN template_arg ta ON ta.owner_id = e.src_id "
          "WHERE ta.literal = 'int' AND e.kind IN (4,5)");
      REQUIRE(st.step());
      CHECK(st.col_int64(0) == 5);
    }

    // The explicit specialization `template <> class Box<bool>` stays
    // `specializes` (kind 4).
    {
      auto st = raw.prepare(
          "SELECT e.kind FROM edge e "
          "JOIN template_arg ta ON ta.owner_id = e.src_id "
          "WHERE ta.literal = 'bool' AND e.kind IN (4,5)");
      REQUIRE(st.step());
      CHECK(st.col_int64(0) == 4);
    }

    // Every TYPE argument (arg_kind=1) now records its spelling in `literal`:
    // builtins (bool/int/char) used to land as NULL, making instances
    // indistinguishable.
    {
      auto st = raw.prepare(
          "SELECT COUNT(*) FROM template_arg WHERE arg_kind = 1 "
          "AND literal IS NULL");
      st.step();
      CHECK(st.col_int64(0) == 0);
    }

    // The using function nn::use instantiates Box<char> by value: its
    // template_arg literal is recorded.
    {
      auto st = raw.prepare(
          "SELECT COUNT(*) FROM template_arg WHERE literal = 'char'");
      st.step();
      CHECK(st.col_int64(0) >= 1);
    }
  }

  TEST_CASE("project flow: header decls first, definition wins file/line/col "
            "with decl_* preserved; counters across two TUs (G15, G20, G24)") {
    if (require_libclang() == nullptr) {
      return;
    }
    if (!require_manifests()) {
      return;
    }
    IndexFixture f;
    const std::string proj = kManifestsDir + "/project";
    const std::string app_c = proj + "/app.c";
    const std::string mathlib_c = proj + "/mathlib.c";
    const std::string mathlib_h = proj + "/mathlib.h";

    // TU 1: app.c — mathlib.h enters as bare declarations.
    const int64_t app_id = f.add_owned_file(proj, app_c);
    HeaderStats h1;
    {
      const ParsedTu tu = f.parser.parse(app_c, {}, std::nullopt);
      CHECK(f.indexer.index_symbols(tu, tu.spelling, app_id) == 1); // main
      h1 = f.indexer.index_headers(tu);
    } // TU freed here — one AST alive at a time (analysis §2.2)
    CHECK(h1.indexed == 1); // mathlib.h
    CHECK(h1.symbols == 3); // add, multiply, square prototypes
    CHECK(h1.already == 0);
    CHECK(h1.system > 0); // stdio.h + its transitive system includes
    CHECK(h1.unowned == 0);

    const auto h_row = f.db.get_file(mathlib_h);
    REQUIRE(h_row);
    CHECK(h_row->indexed);
    CHECK(h_row->md5);                   // staleness key captured
    CHECK_FALSE(h_row->compile_options); // NULL options/driver (G20)
    CHECK_FALSE(h_row->driver);

    const auto decl = one_sym(f.db, "multiply");
    REQUIRE(decl);
    CHECK(decl->kind == "function");
    CHECK_FALSE(decl->is_definition);
    CHECK_FALSE(decl->resolved);
    CHECK(decl->file_id == h_row->id);
    CHECK(decl->line == 6);
    // a declaration cursor records its own site as the decl site too
    CHECK(decl->decl_file_id == h_row->id);
    CHECK(decl->decl_line == 6);

    // TU 2: mathlib.c — definitions win, decl_* preserved, resolved sticky.
    const int64_t mathlib_id = f.db.add_file_path(mathlib_c);
    HeaderStats h2;
    {
      const ParsedTu tu = f.parser.parse(mathlib_c, {}, std::nullopt);
      CHECK(f.indexer.index_symbols(tu, tu.spelling, mathlib_id) == 3);
      h2 = f.indexer.index_headers(tu);
    }
    CHECK(h2.indexed == 0);
    CHECK(h2.symbols == 0);
    CHECK(h2.already == 1); // mathlib.h row indexed with matching md5
    CHECK(h2.system == 0);  // mathlib.c includes nothing else
    CHECK(h2.unowned == 0);

    const auto def = one_sym(f.db, "multiply");
    REQUIRE(def);
    CHECK(def->is_definition);
    CHECK(def->resolved);
    CHECK(def->file_id == mathlib_id);
    CHECK(def->line == 7);
    CHECK(def->decl_file_id == h_row->id); // decl site preserved
    CHECK(def->decl_line == 6);
  }

  TEST_CASE(
      "symbol (line, col) is the start of the DECLARATION extent, not the "
      "identifying spelling location -- else a source-text slice truncates "
      "the leading struct/union/enum keyword or a function's return type "
      "(mirrors test_symbol_extent_end.py::"
      "test_source_includes_leading_keyword_or_return_type)") {
    if (require_libclang() == nullptr) {
      return;
    }
    if (!require_manifests()) {
      return;
    }
    IndexFixture f;
    const std::string shapes_c = kManifestsDir + "/shapes.c";
    const int64_t c_id = f.add_owned_file(kManifestsDir, shapes_c);
    const ParsedTu tu = f.parser.parse(shapes_c, {}, std::nullopt);
    f.indexer.index_symbols(tu, tu.spelling, c_id);
    f.indexer.index_headers(tu);

    // struct Shape { ... } (shapes.h:23) -- col must land on `struct`
    // (col 9), not the identifying spelling location `Shape` (col 16).
    const auto shape = one_sym(f.db, "Shape", std::string("struct"));
    REQUIRE(shape);
    CHECK(shape->line == 23);
    CHECK(shape->col == 9);

    // double shape_area(const Shape *s) { ... } (shapes.c:12) -- col must
    // land on the return type `double` (col 1), not the function name
    // `shape_area` (col 8).
    const auto shape_area = one_sym(f.db, "shape_area");
    REQUIRE(shape_area);
    CHECK(shape_area->line == 12);
    CHECK(shape_area->col == 1);
  }

  TEST_CASE("shapes.c then shapes.h: resolved rows skipped but decl-patched "
            "(G15); subtree/body pruning (G21); `macro` unreachable (G22); "
            "linkage spellings (D13)") {
    if (require_libclang() == nullptr) {
      return;
    }
    if (!require_manifests()) {
      return;
    }
    IndexFixture f;
    const std::string shapes_c = kManifestsDir + "/shapes.c";
    const std::string shapes_h = kManifestsDir + "/shapes.h";

    const int64_t c_id = f.add_owned_file(kManifestsDir, shapes_c);
    const ParsedTu tu = f.parser.parse(shapes_c, {}, std::nullopt);

    // main file first: exactly the five function definitions
    CHECK(f.indexer.index_symbols(tu, tu.spelling, c_id) == 5);
    // header cursors are not stored by the main-file pass (G24)
    CHECK(f.db.lookup_symbols_by_name("Point").empty());
    // body pruning: locals and params never become rows (G21)
    for (const char *local : {"total", "i", "radius", "sum", "args", "s"}) {
      CAPTURE(local);
      CHECK(f.db.lookup_symbols_by_name(local).empty());
    }
    // options=0 -> no DETAILED_PREPROCESSING_RECORD -> no macro cursors
    // (G22/D19): the kind stays mapped but no row can ever carry it
    CHECK(f.db.lookup_symbols_by_name("PI").empty());
    CHECK(f.db.lookup_symbols_by_name("SQUARE").empty());
    CHECK(f.db.lookup_symbols_by_name("MAX_SHAPES").empty());
    CHECK(f.db.stats().symbols_by_kind.count("macro") == 0);

    const auto def = one_sym(f.db, "shape_area");
    REQUIRE(def);
    CHECK(def->is_definition);
    CHECK(def->resolved);
    CHECK(def->file_id == c_id);
    CHECK(def->line == 12);
    CHECK_FALSE(def->decl_file_id); // definitions leave decl_* null
    CHECK(def->linkage == "external");
    const auto helper = one_sym(f.db, "circle_area");
    REQUIRE(helper);
    CHECK(helper->linkage == "internal"); // static fn

    // headers: shapes.h indexed; its three prototypes hit resolved rows ->
    // skipped (not re-stored) but their decl sites are patched in (G15)
    const HeaderStats h = f.indexer.index_headers(tu);
    CHECK(h.indexed == 1);
    CHECK(h.symbols == 15); // structs/fields/enums/constants/typedefs only
    CHECK(h.already == 0);
    CHECK(h.system >= 3); // stddef.h, math.h, stdarg.h (+ their includes)
    CHECK(h.unowned == 0);

    const auto h_row = f.db.get_file(shapes_h);
    REQUIRE(h_row);
    const auto patched = one_sym(f.db, "shape_area");
    REQUIRE(patched);
    CHECK(patched->is_definition); // stored definition untouched
    CHECK(patched->resolved);
    CHECK(patched->file_id == c_id);
    CHECK(patched->line == 12);
    CHECK(patched->decl_file_id == h_row->id); // decl site patched
    CHECK(patched->decl_line == 31);

    // no-linkage spelling via the header typedef (D13)
    const auto td = one_sym(f.db, "Point", std::string("typedef"));
    REQUIRE(td);
    CHECK(td->linkage == "no-linkage");
    // system-header symbols never appear under the default policy (G26)
    CHECK(f.db.lookup_symbols_by_name("size_t").empty());
  }

  TEST_CASE("geometry: ns::Class::method qual names from semantic parents; "
            "access + pure-virtual via the D13 tables") {
    if (require_libclang() == nullptr) {
      return;
    }
    if (!require_manifests()) {
      return;
    }
    IndexFixture f;
    const std::string geometry_cpp = kManifestsDir + "/geometry.cpp";
    const std::string geometry_hpp = kManifestsDir + "/geometry.hpp";

    const int64_t cpp_id = f.add_owned_file(kManifestsDir, geometry_cpp);
    const ParsedTu tu = f.parser.parse(geometry_cpp, {}, std::nullopt);
    // geo, Shape::Shape, ~Shape, Shape::name, Circle::Circle, Circle::area,
    // widest
    CHECK(f.indexer.index_symbols(tu, tu.spelling, cpp_id) == 7);

    // out-of-line definitions are qualified by their CLASS (semantic
    // parents), not the file scope they sit in (G25)
    const auto area_def = one_sym(f.db, "area");
    REQUIRE(area_def);
    CHECK(area_def->kind == "method");
    CHECK(area_def->qual_name == "geo::Circle::area");
    CHECK(area_def->is_definition);
    const auto widest = one_sym(f.db, "widest");
    REQUIRE(widest);
    CHECK(widest->qual_name == "geo::widest");

    const HeaderStats h = f.indexer.index_headers(tu);
    CHECK(h.indexed == 1); // geometry.hpp; <string>/<vector>/<cmath> system
    CHECK(h.symbols == 14);
    CHECK(h.system > 0);
    CHECK(h.unowned == 0);

    const auto h_row = f.db.get_file(geometry_hpp);
    REQUIRE(h_row);

    const std::vector<Symbol> areas =
        f.db.lookup_symbols_by_name("area", std::string("method"));
    REQUIRE(areas.size() == 2);
    const auto by_qual = [&areas](const char *qual) {
      return std::find_if(areas.begin(), areas.end(), [qual](const Symbol &s) {
        return s.qual_name == std::string(qual);
      });
    };
    // pure virtual declaration: Shape::area
    const auto pure = by_qual("geo::Shape::area");
    REQUIRE(pure != areas.end());
    CHECK(pure->is_pure);
    CHECK(pure->access == "public");
    CHECK_FALSE(pure->is_definition);
    CHECK_FALSE(pure->resolved);
    // Circle::area kept its .cpp definition and gained the header decl site
    const auto circle_area = by_qual("geo::Circle::area");
    REQUIRE(circle_area != areas.end());
    CHECK(circle_area->is_definition);
    CHECK(circle_area->file_id == cpp_id);
    CHECK(circle_area->decl_file_id == h_row->id);
    CHECK_FALSE(circle_area->is_pure);

    // member access spellings (D13)
    const auto name_field = one_sym(f.db, "name_");
    REQUIRE(name_field);
    CHECK(name_field->kind == "member");
    CHECK(name_field->access == "protected");
    CHECK(name_field->qual_name == "geo::Shape::name_");
    const auto radius_field = one_sym(f.db, "radius_");
    REQUIRE(radius_field);
    CHECK(radius_field->access == "private");

    // templates + scoped enum from the header
    const auto ct = one_sym(f.db, "Box", std::string("class-template"));
    REQUIRE(ct);
    CHECK(ct->qual_name == "geo::Box");
    const auto ft = one_sym(f.db, "max_of");
    REQUIRE(ft);
    CHECK(ft->kind == "function-template");
    CHECK(ft->qual_name == "geo::max_of");
    const auto red = one_sym(f.db, "Red");
    REQUIRE(red);
    CHECK(red->kind == "enum-constant");
    CHECK(red->qual_name == "geo::Color::Red");
  }

  TEST_CASE("anonymous entities: indexed when they carry a USR; qual_name "
            "skips empty-spelling levels (G25)") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    // C++: an anonymous NAMESPACE has an empty spelling but a USR ('...@aN')
    const std::string path = f.tmp + "/anonns.cpp";
    write_file(path, "namespace outer_ns {\n"
                     "namespace {\n"
                     "int hidden_var;\n"
                     "}\n"
                     "}\n");
    const int64_t file_id = f.add_owned_file(f.tmp, path);
    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    CHECK(f.indexer.index_symbols(tu, tu.spelling, file_id) == 3);

    // the anonymous namespace IS a row: empty spelling, non-empty USR (G25)
    const std::vector<Symbol> anon_ns =
        f.db.lookup_symbols_by_name("", std::string("namespace"));
    REQUIRE(anon_ns.size() == 1);
    CHECK_FALSE(anon_ns.front().usr.empty());
    // its own empty level is skipped -> qualified by the named parent only
    CHECK(anon_ns.front().qual_name == "outer_ns");

    // entities INSIDE it skip the empty level in their qual_name, and the
    // anonymous-namespace variable shows the 'internal' linkage spelling
    const auto hidden = one_sym(f.db, "hidden_var");
    REQUIRE(hidden);
    CHECK(hidden->qual_name == "outer_ns::hidden_var");
    CHECK(hidden->linkage == "internal");
    CHECK(hidden->parent_usr == anon_ns.front().usr);

    // C: anonymous structs/enums carry a '(unnamed at <file>:<l>:<c>)'
    // SPELLING under modern libclang (probed against the Python reference
    // on the wheel's libclang 18) — those levels are NOT empty, so they
    // stay in qual_name. Parity target is ast.py on the same libclang, not
    // the empty-spelling folklore.
    const std::string c_path = f.tmp + "/anon.c";
    write_file(c_path, "struct outer_s {\n"
                       "  struct { int inner_field; } nested;\n"
                       "};\n"
                       "enum { ANON_CONST = 7 };\n");
    const int64_t c_id = f.add_owned_file(f.tmp, c_path);
    const ParsedTu ctu = f.parser.parse(c_path, {}, std::nullopt);
    CHECK(f.indexer.index_symbols(ctu, ctu.spelling, c_id) > 0);
    const auto inner = one_sym(f.db, "inner_field");
    REQUIRE(inner);
    REQUIRE(inner->qual_name);
    CHECK(inner->qual_name->rfind("outer_s::", 0) == 0);
    CHECK(inner->qual_name->find("::inner_field") != std::string::npos);
    const auto constant = one_sym(f.db, "ANON_CONST");
    REQUIRE(constant);
    CHECK(constant->parent_usr); // the anonymous enum still has a USR
  }

  TEST_CASE("system headers: skipped by default, indexed when "
            "$INDEXER_IGNORE_SYSTEM_HEADERS spells false (G26); header row "
            "carries NULL options/driver (G20); md5 'already' skip") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string sys_dir = f.tmp + "/sys";
    const std::string sys_hdr = sys_dir + "/syshdr.h";
    write_file(sys_hdr, "int sys_fn(void);\n");
    const std::string main_c = f.tmp + "/main.c";
    write_file(main_c, "#include <syshdr.h>\nint main_fn(void) { return 0; "
                       "}\n");
    const std::vector<std::string> args = {"-isystem", sys_dir};

    const int64_t main_id = f.add_owned_file(f.tmp, main_c);
    const ParsedTu tu = f.parser.parse(main_c, args, std::nullopt);
    CHECK(f.indexer.index_symbols(tu, tu.spelling, main_id) == 1);

    // default: the -isystem header of THIS parse is skipped as system (the
    // check is per-TU, G26)
    {
      const HeaderStats h = f.indexer.index_headers(tu);
      CHECK(h.indexed == 0);
      CHECK(h.symbols == 0);
      CHECK(h.already == 0);
      CHECK(h.system == 1);
      CHECK(h.unowned == 0);
      CHECK(f.db.lookup_symbols_by_name("sys_fn").empty());
    }
    // the exact falsy set flips the policy: the header is owned -> indexed
    {
      ScopedEnv env(cidx::kIgnoreSystemHeadersEnv, "0");
      const HeaderStats h = f.indexer.index_headers(tu);
      CHECK(h.indexed == 1);
      CHECK(h.symbols == 1);
      CHECK(h.already == 0);
      CHECK(h.system == 0);
      CHECK(h.unowned == 0);

      const auto row = f.db.get_file(sys_hdr);
      REQUIRE(row);
      CHECK(row->indexed);
      CHECK(row->md5);
      CHECK_FALSE(row->compile_options); // G20: NULL options/driver
      CHECK_FALSE(row->driver);
      const auto sym = one_sym(f.db, "sys_fn");
      REQUIRE(sym);
      CHECK(sym->file_id == row->id);
      CHECK_FALSE(sym->resolved); // bare declaration

      // second pass: row indexed with matching md5 -> already
      const HeaderStats h2 = f.indexer.index_headers(tu);
      CHECK(h2.already == 1);
      CHECK(h2.indexed == 0);
      CHECK(h2.system == 0);
      // the explicit parameter overrides the env (ast.py:201-202): the
      // system check runs before the already check
      const HeaderStats h3 = f.indexer.index_headers(tu, true);
      CHECK(h3.system == 1);
      CHECK(h3.already == 0);
    }
  }

  TEST_CASE("include spelling vs abspath: cursors matched against the "
            "SPELLING, rows stored under the abspath (G23); unowned headers "
            "counted") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    write_file(f.tmp + "/out/hdr.h", "int owned_fn(void);\n");
    const std::string main_c = f.tmp + "/comp/main.c";
    write_file(main_c, "#include \"../out/hdr.h\"\nint comp_fn(void) { "
                       "return 0; }\n");

    // owned: component over the tmp root; the include SPELLING goes through
    // 'comp/../out' while dedupe/storage use the normalized abspath
    const int64_t main_id = f.add_owned_file(f.tmp, main_c);
    {
      const ParsedTu tu = f.parser.parse(main_c, {}, std::nullopt);
      CHECK(f.indexer.index_symbols(tu, tu.spelling, main_id) == 1);
      const HeaderStats h = f.indexer.index_headers(tu);
      CHECK(h.indexed == 1);
      CHECK(h.symbols == 1); // owned_fn extracted via the spelling match
      CHECK(h.unowned == 0);
      const auto row = f.db.get_file(f.tmp + "/out/hdr.h"); // normalized
      REQUIRE(row);
      const auto sym = one_sym(f.db, "owned_fn");
      REQUIRE(sym);
      CHECK(sym->file_id == row->id);
    }

    // unowned: a DB owning only comp/ has nowhere to store the header
    Storage db2;
    AstIndexer indexer2{db2, f.log};
    db2.add_component("comp-only", f.tmp + "/comp", "external");
    const int64_t main_id2 = db2.add_file_path(main_c);
    const ParsedTu tu2 = f.parser.parse(main_c, {}, std::nullopt);
    CHECK(indexer2.index_symbols(tu2, tu2.spelling, main_id2) == 1);
    const HeaderStats h2 = indexer2.index_headers(tu2);
    CHECK(h2.indexed == 0);
    CHECK(h2.symbols == 0);
    CHECK(h2.already == 0);
    CHECK(h2.system == 0);
    CHECK(h2.unowned == 1);
    CHECK(db2.lookup_symbols_by_name("owned_fn").empty());
  }

  TEST_CASE("geometry: graph edges — inherits/field_of/method_of/"
            "template_param/uses extracted from geometry.cpp + geometry.hpp "
            "(M1 functional graph tests)") {
    if (require_libclang() == nullptr) {
      return;
    }
    if (!require_manifests()) {
      return;
    }
    IndexFixture f;
    const std::string geometry_cpp = kManifestsDir + "/geometry.cpp";
    const std::string geometry_hpp = kManifestsDir + "/geometry.hpp";

    // Step 1: index the main .cpp file symbols.
    const int64_t cpp_id = f.add_owned_file(kManifestsDir, geometry_cpp);
    const ParsedTu tu = f.parser.parse(geometry_cpp, {}, std::nullopt);
    f.indexer.index_symbols(tu, tu.spelling, cpp_id);

    // Step 2: index headers (extracts geometry.hpp symbols + their edges).
    // index_edges is called per header inside index_headers (QD-1 fix).
    const HeaderStats h = f.indexer.index_headers(tu);
    CHECK(h.indexed == 1);  // geometry.hpp
    CHECK(h.symbols >= 10); // Shape, Circle, Box, max_of, Color, ...

    // Step 3: index edges for the main .cpp (calls + uses + body descent).
    f.indexer.index_edges(tu, tu.spelling, cpp_id);

    // Use raw SQL to count edges by kind.
    auto &raw = f.db.raw_db();

    auto count_edges = [&raw](int64_t kind_id) -> int64_t {
      auto st = raw.prepare("SELECT COUNT(*) FROM edge WHERE kind = ?");
      st.bind(1, kind_id);
      st.step();
      return st.col_int64(0);
    };

    // inherits (kind=2): Circle -> Shape (exactly one inheritance edge)
    CHECK(count_edges(2) == 1);

    // field_of (kind=8): Shape::name_, Circle::radius_, Box::value_
    CHECK(count_edges(8) >= 2);

    // method_of (kind=9): Shape::{area,name,~Shape}, Circle::{Circle,area}
    CHECK(count_edges(9) >= 4);

    // template_param: Box<T>, max_of<T> should each have 1 param row
    {
      auto st = raw.prepare("SELECT COUNT(*) FROM template_param");
      st.step();
      CHECK(st.col_int64(0) >= 2);
    }

    // uses (kind=7): widest() now also names Color via the TYPE_REF `Color::Red`
    // in `Box<Color> bc(Color::Red)` expression position -> a uses edge from
    // widest to the indexed enum Color (TYPE_REF uses-edge feature, v0.5.0).
    {
      auto st = raw.prepare(
          "SELECT COUNT(*) FROM edge e "
          "JOIN symbol src ON src.id = e.src_id "
          "JOIN symbol dst ON dst.id = e.dst_id "
          "WHERE e.kind = 7 AND src.spelling = 'widest' "
          "  AND dst.spelling = 'Color'");
      st.step();
      CHECK(st.col_int64(0) == 1); // widest uses Color via Color::Red TYPE_REF
    }

    // Verify the inherits edge: src=Circle, dst=Shape
    {
      const auto circle =
          f.db.lookup_symbols_by_name("Circle", std::string("class"));
      const auto shape =
          f.db.lookup_symbols_by_name("Shape", std::string("class"));
      REQUIRE_FALSE(circle.empty());
      REQUIRE_FALSE(shape.empty());
      const int64_t circle_id = circle.front().id;
      const int64_t shape_id = shape.front().id;
      auto st = raw.prepare(
          "SELECT COUNT(*) FROM edge WHERE src_id=? AND dst_id=? AND kind=2");
      st.bind(1, circle_id);
      st.bind(2, shape_id);
      st.step();
      CHECK(st.col_int64(0) == 1); // Circle inherits Shape
    }

    // Verify field_of: name_ -> Shape
    {
      const auto name_fld = f.db.lookup_symbols_by_name("name_");
      const auto shape =
          f.db.lookup_symbols_by_name("Shape", std::string("class"));
      if (!name_fld.empty() && !shape.empty()) {
        auto st = raw.prepare(
            "SELECT COUNT(*) FROM edge WHERE src_id=? AND dst_id=? AND kind=8");
        st.bind(1, name_fld.front().id);
        st.bind(2, shape.front().id);
        st.step();
        CHECK(st.col_int64(0) == 1); // name_ is field_of Shape
      }
    }

    // instantiates (kind=5): widest() body has Box<int> bi and Box<double> bd;
    // both are class-template instantiations -> instantiates edges to Box<T>.
    // box_instantiates >= 1 (may be 2 but edges are upserted so duplicates
    // compress to a single src->dst row).
    CHECK(count_edges(5) >= 1);

    // template_arg: the VAR_DECL handler emits template_arg rows with owner_id
    // = widest fn's sym_id. At least one row must have ref_id IS NOT NULL
    // (i.e., the type argument resolves to an indexed symbol — int maps to NULL
    // since int has no USR; double likewise; but the edge itself is asserted
    // above). Accept count >= 1 (two VAR_DECL for bi and bd).
    {
      auto st = raw.prepare("SELECT COUNT(*) FROM template_arg");
      st.step();
      CHECK(st.col_int64(0) >= 1);
    }

    // template_arg.ref_id IS NOT NULL (Item 2): Box<Color> in widest() uses
    // Color (an indexed enum), so clang_getTypeDeclaration resolves it to a
    // symbol with a USR -> ref_id is populated.  The join must return "Color".
    {
      auto st = raw.prepare(
          "SELECT s.spelling FROM template_arg ta "
          "JOIN symbol s ON s.id = ta.ref_id "
          "WHERE ta.ref_id IS NOT NULL "
          "LIMIT 1");
      const bool has_row = st.step();
      CHECK(has_row); // at least one template_arg with a non-NULL ref_id
      if (has_row) {
        CHECK(st.col_text(0) == std::string("Color"));
      }
    }

    // contains (kind=3) — Item 1: namespace geo contains its child symbols.
    // geometry.hpp declares Color/Shape/Circle/max_of/Box inside geo (5 edges);
    // geometry.cpp may contribute additional out-of-line entries.
    CHECK(count_edges(3) >= 5);

    // Verify one specific contains edge: geo -> Shape (class).
    // Join through the symbol table to handle multiple rows with the same
    // spelling (e.g., Shape::Shape constructor vs Shape class). The fix in
    // delete_edges_for_file (kind!=3 exclusion) ensures the class-level
    // contains edge from the header-indexing pass is not wiped by the later
    // geometry.cpp edge re-index.
    {
      auto st = raw.prepare(
          "SELECT COUNT(*) FROM edge e "
          "JOIN symbol src ON src.id = e.src_id "
          "JOIN symbol dst ON dst.id = e.dst_id "
          "WHERE e.kind = 3 "  // v16: symbol.kind is a CXCursorKind int
          "  AND src.spelling = 'geo' "
          "  AND src.kind = (SELECT id FROM symbol_kind WHERE name='namespace') "
          "  AND dst.spelling = 'Shape' "
          "  AND dst.kind = (SELECT id FROM symbol_kind WHERE name='class')");
      st.step();
      CHECK(st.col_int64(0) >= 1); // geo contains Shape (class)
    }
  }

  TEST_CASE("type uses: a class named only as a parameter / return / field / "
            "local / typedef type earns an inbound `uses` edge "
            "(_emit_type_use parity)") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string path = f.tmp + "/types.cpp";
    write_file(path,
               "namespace RdKafka {\n"
               "  class Conf { public: int x; Conf *self(); };\n"
               "  class Producer {\n"
               "   public:\n"
               "    static Producer *create(const Conf *conf, int n);\n"
               "    Conf *m_conf;\n"
               "  };\n"
               "  Producer *Producer::create(const Conf *conf, int n) {\n"
               "    Conf local; (void)local; (void)conf; (void)n;\n"
               "    return 0;\n"
               "  }\n"
               "  typedef Conf ConfAlias;\n"
               "}\n");

    const int64_t file_id = f.add_owned_file(f.tmp, path);
    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    f.indexer.index_symbols(tu, tu.spelling, file_id);
    f.indexer.index_edges(tu, tu.spelling, file_id);

    const auto conf = one_sym(f.db, "Conf", std::string("class"));
    REQUIRE(conf.has_value());

    // Collect the qual_names of every symbol with a uses (kind=7) edge -> Conf.
    std::unordered_set<std::string> users;
    {
      auto &raw = f.db.raw_db();
      auto st = raw.prepare("SELECT s.qual_name FROM edge e "
                            "JOIN symbol s ON s.id = e.src_id "
                            "WHERE e.dst_id = ? AND e.kind = 7");
      st.bind(1, conf->id);
      while (st.step()) {
        users.insert(st.col_text(0));
      }
    }

    CHECK(users.count("RdKafka::Producer::create") == 1); // param + return
    CHECK(users.count("RdKafka::Producer::m_conf") == 1);  // field
    CHECK(users.count("RdKafka::Conf::self") == 1);        // return type
    CHECK(users.count("RdKafka::ConfAlias") == 1);         // typedef underlying
    CHECK(users.count("RdKafka::Conf") == 0);              // no self-edge
  }

  TEST_CASE("typeref uses: a bare type NAME in expression position "
            "(static-call receiver, scoped-enum access) earns an inbound "
            "`uses` edge; self-owner is skipped (TYPE_REF branch parity)") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string path = f.tmp + "/tr.cpp";
    // Mirrors project/tests/test_type_uses.py TYPEREF_SOURCE byte-for-byte
    // behavior: Widget::instance() and Color::Red/Green are TYPE_REFs in
    // expression position; Widget methods naming Widget itself are self-owner.
    write_file(path,
               "namespace N {\n"
               "  enum class Color { Red, Green };\n"
               "  struct Widget {\n"
               "    static Widget* instance();\n"
               "    int v;\n"
               "    void touch() { (void)this; }\n"
               "    Color self_color() { return Color::Red; }\n"
               "  };\n"
               "  struct Other {\n"
               "    void use() {\n"
               "      Widget* w = Widget::instance(); (void)w;\n"
               "      Color c = Color::Green; (void)c;\n"
               "    }\n"
               "  };\n"
               "}\n");

    const int64_t file_id = f.add_owned_file(f.tmp, path);
    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    f.indexer.index_symbols(tu, tu.spelling, file_id);
    f.indexer.index_edges(tu, tu.spelling, file_id);

    // Map dst spelling -> set(src qual_name) over kind=7 edges.
    auto users_of = [&f](const char *dst_spelling) {
      std::unordered_set<std::string> users;
      auto &raw = f.db.raw_db();
      auto st = raw.prepare("SELECT src.qual_name FROM edge e "
                            "JOIN symbol src ON src.id = e.src_id "
                            "JOIN symbol dst ON dst.id = e.dst_id "
                            "WHERE e.kind = 7 AND dst.spelling = ?");
      st.bind(1, std::string_view{dst_spelling});
      while (st.step()) {
        users.insert(st.col_text(0));
      }
      return users;
    };

    const auto widget_users = users_of("Widget");
    const auto color_users = users_of("Color");

    // Static-call receiver TYPE_REF -> use edge to Widget.
    CHECK(widget_users.count("N::Other::use") == 1);
    // Scoped-enum access TYPE_REFs -> use edges to Color.
    CHECK(color_users.count("N::Other::use") == 1);
    CHECK(color_users.count("N::Widget::self_color") == 1);
    // Self-owner skip: a Widget method must NOT use its own class.
    CHECK(widget_users.count("N::Widget::touch") == 0);
    CHECK(widget_users.count("N::Widget::self_color") == 0);
  }

  TEST_CASE("dependent calls inside a template body are recovered: a template "
            "method calling a function template earns a calls edge to the "
            "primary; ambiguous overload sets link to ALL indexed candidates") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string path = f.tmp + "/tmplcalls.cpp";
    write_file(path,
               "namespace nn {\n"
               "template <class T> T combine(T a, T b) { return a + b; }\n"
               "template <class T> int describe(const T&) { return 0; }\n"
               "template <class T>\n"
               "struct Stack {\n"
               "  T data_[4]; int n_ = 0;\n"
               "  int summary() const {\n"
               "    T acc = data_[0];\n"
               "    for (int i = 1; i < n_; ++i) acc = combine(acc, data_[i]);\n"
               "    return describe(acc);\n"
               "  }\n"
               "};\n"
               "int over(int); double over(double);\n"
               "template <class T> int caller(T v) { return (int)over(v); }\n"
               "}\n");

    const int64_t file_id = f.add_owned_file(f.tmp, path);
    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    f.indexer.index_symbols(tu, tu.spelling, file_id);
    f.indexer.index_edges(tu, tu.spelling, file_id);

    // calls map: src qual_name -> set of dst qual_names (kind = 1).
    auto callees_of = [&](const std::string &src_qual) {
      std::unordered_set<std::string> out;
      auto &raw = f.db.raw_db();
      auto st = raw.prepare("SELECT b.qual_name FROM edge e "
                            "JOIN symbol a ON a.id = e.src_id "
                            "JOIN symbol b ON b.id = e.dst_id "
                            "WHERE e.kind = 1 AND a.qual_name = ?");
      st.bind(1, std::string_view{src_qual});
      while (st.step()) {
        out.insert(st.col_text(0));
      }
      return out;
    };

    const auto summary_callees = callees_of("nn::Stack::summary");
    CHECK(summary_callees.count("nn::combine") == 1);  // recovered fn template
    CHECK(summary_callees.count("nn::describe") == 1); // recovered fn template

    // `over(v)` in caller<T> is a dependent overload SET (int/double). libclang
    // can't say which overload is selected, so the site links to EVERY indexed
    // overload of that name (both share qual_name nn::over) rather than dropping
    // the call -- a sound over-approximation for find-references. Mirror of
    // Python test_ambiguous_overload_links_all_candidates.
    CHECK(callees_of("nn::caller").count("nn::over") == 1);
  }

  TEST_CASE("overloaded member function template called from another template "
            "body links to the member template (regression for the "
            "BzRuleValueCache::set/get no-references report)") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string path = f.tmp + "/membovl.cpp";
    write_file(path,
               "namespace mm {\n"
               "struct Cache {\n"
               "  template <class V> void set(int k, V v) {}\n"
               "  template <class V> void set(int k, V v, int ttl) {}\n"
               "  template <class T> T get(int k) { return T{}; }\n"
               "};\n"
               "template <class T>\n"
               "void useBoth(Cache& c, int k, T v) {\n"
               "  c.set(k, v);\n"            // 2-candidate dependent overload set
               "  c.set(k, v, 5);\n"         // 2-candidate dependent overload set
               "  T a = c.template get<T>(k);\n" // single candidate
               "}\n"
               "}\n");

    const int64_t file_id = f.add_owned_file(f.tmp, path);
    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    f.indexer.index_symbols(tu, tu.spelling, file_id);
    f.indexer.index_edges(tu, tu.spelling, file_id);

    auto callees_of = [&](const std::string &src_qual) {
      std::unordered_set<std::string> out;
      auto &raw = f.db.raw_db();
      auto st = raw.prepare("SELECT b.qual_name FROM edge e "
                            "JOIN symbol a ON a.id = e.src_id "
                            "JOIN symbol b ON b.id = e.dst_id "
                            "WHERE e.kind = 1 AND a.qual_name = ?");
      st.bind(1, std::string_view{src_qual});
      while (st.step()) {
        out.insert(st.col_text(0));
      }
      return out;
    };

    const auto cs = callees_of("mm::useBoth");
    CHECK(cs.count("mm::Cache::set") == 1); // overloaded member template linked
    CHECK(cs.count("mm::Cache::get") == 1); // single-overload member template
  }

  TEST_CASE("function and method template specializations store template args "
            "without marking ordinary owners as instantiations") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string path = f.tmp + "/callable_specs.cpp";
    write_file(path,
                 "namespace spec {\n"
                 "struct MyType {};\n"
                 "struct Other {};\n"
                 "template <class Inner> struct Payload { Inner value; };\n"
                 "struct Context {\n"
                 "  template <class T> void reg() {}\n"
                 "  template <class T, int N> void regN() {}\n"
                 "  template <class T> void regComplex() {}\n"
                 "};\n"
                 "template <class T> T identity(T v) { return v; }\n"
                 "int use(Context& c) {\n"
                 "  c.reg<MyType>();\n"
                 "  c.regN<Other, 7>();\n"
                 "  c.regComplex<Payload<Other>>();\n"
                 "  return identity<int>(3);\n"
                 "}\n"
                 "}\n");

    const int64_t file_id = f.add_owned_file(f.tmp, path);
    const ParsedTu tu = f.parser.parse(path, {}, std::nullopt);
    f.indexer.index_symbols(tu, tu.spelling, file_id);
    f.indexer.index_edges(tu, tu.spelling, file_id);

    auto &raw = f.db.raw_db();

    auto args_for = [&](const std::string &qual, const std::string &kind) {
      std::vector<std::string> out;
      auto st = raw.prepare(
          "SELECT ta.literal FROM template_arg ta "
          "JOIN symbol s ON s.id = ta.owner_id "
          "JOIN symbol_kind sk ON sk.id = s.kind "
          "WHERE s.qual_name = ? AND s.is_instantiation = 1 AND sk.name = ? "
          "ORDER BY ta.position");
      st.bind(1, std::string_view{qual});
      st.bind(2, std::string_view{kind});
      while (st.step()) {
        out.push_back(st.col_text(0));
      }
      return out;
    };

    CHECK(args_for("spec::Context::reg", "method") ==
          std::vector<std::string>{"MyType"});
    CHECK(args_for("spec::Context::regN", "method") ==
          std::vector<std::string>{"Other", "7"});
    CHECK(args_for("spec::Context::regComplex", "method") ==
          std::vector<std::string>{"Payload<Other>"});
    CHECK(args_for("spec::identity", "function") ==
          std::vector<std::string>{"int"});

    auto ref_for = [&](const std::string &qual, const std::string &literal) {
      auto st = raw.prepare(
          "SELECT ta.ref_id FROM template_arg ta "
          "JOIN symbol s ON s.id = ta.owner_id "
          "WHERE s.qual_name = ? AND s.is_instantiation = 1 "
          "AND ta.literal = ? LIMIT 1");
      st.bind(1, std::string_view{qual});
      st.bind(2, std::string_view{literal});
      REQUIRE(st.step());
      return st.col_is_null(0) ? std::optional<int64_t>{}
                               : std::optional<int64_t>{st.col_int64(0)};
    };

    const auto my_type_ref = ref_for("spec::Context::reg", "MyType");
    REQUIRE(my_type_ref);
    CHECK(f.db.lookup_symbol_by_id(*my_type_ref)->qual_name == "spec::MyType");

    const auto other_ref = ref_for("spec::Context::regN", "Other");
    REQUIRE(other_ref);
    CHECK(f.db.lookup_symbol_by_id(*other_ref)->qual_name == "spec::Other");
    CHECK_FALSE(ref_for("spec::Context::regN", "7"));

    const auto payload_ref =
        ref_for("spec::Context::regComplex", "Payload<Other>");
    REQUIRE(payload_ref);
    const auto payload_sym = f.db.lookup_symbol_by_id(*payload_ref);
    REQUIRE(payload_sym);
    CHECK(payload_sym->qual_name == "spec::Payload");
    CHECK(payload_sym->kind == "class-template");

    {
      auto st = raw.prepare(
          "SELECT is_instantiation FROM symbol WHERE qual_name = 'spec::Context'");
      REQUIRE(st.step());
      CHECK(st.col_int64(0) == 0);
    }
    {
      auto st = raw.prepare(
          "SELECT owner.qual_name, owner.is_instantiation FROM edge e "
          "JOIN symbol m ON m.id = e.src_id "
          "JOIN symbol owner ON owner.id = e.dst_id "
          "WHERE e.kind = 9 AND m.qual_name = 'spec::Context::reg' "
          "AND m.is_instantiation = 1");
      REQUIRE(st.step());
      CHECK(st.col_text(0) == "spec::Context");
      CHECK(st.col_int64(1) == 0);
    }
    {
      auto st = raw.prepare(
          "SELECT COUNT(*) FROM edge e "
          "JOIN symbol s ON s.id = e.src_id "
          "WHERE e.kind = 9 AND s.qual_name = 'spec::identity' "
          "AND s.is_instantiation = 1");
      REQUIRE(st.step());
      CHECK(st.col_int64(0) == 0);
    }
  }

  // --- cross-TU wrong-order indexing (USR-keyed stub + backfill) ----------- #
  //
  // Mirror of Python test_cross_tu_wrong_order_stub_backfill. A dependent call
  // to a member function template whose DEFINING TU is indexed AFTER the
  // consuming TU must still link, because the callee USR is TU-invariant: the
  // consuming TU mints a USR-keyed stub, and the later index of the cache TU
  // backfills the same USR. Covers BOTH the single-candidate recovered path
  // (`get`) and the multi-candidate overloaded path (`set`, two overloads).
  TEST_CASE("cross-TU wrong order: the consuming TU indexed BEFORE the cache TU "
            "mints USR-keyed stubs that backfill on a later index + resolve "
            "(order independence; regression for v0.14.2)") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string use_dir = f.tmp + "/use";
    const std::string lib_dir = f.tmp + "/lib";
    // The cache header lives under lib/, which is UNOWNED at first index time.
    write_file(lib_dir + "/cache.hpp",
               "#ifndef MM_CACHE_HPP\n"
               "#define MM_CACHE_HPP\n"
               "#include <string>\n"
               "namespace mm {\n"
               "class Cache {\n"
               "  int n_ = 0;\n"
               "public:\n"
               "  template <class T> void set(const std::string& k, T v) { n_ "
               "+= (int)sizeof(v); }\n"
               "  template <class T> void set(const std::string& k, const T* p) "
               "{ n_ += (int)k.size(); }\n"
               "  template <class T> T get(const std::string&) const { return "
               "T(); }\n"
               "};\n"
               "}  // namespace mm\n"
               "#endif\n");
    write_file(lib_dir + "/cache.cpp", "#include \"cache.hpp\"\n");
    write_file(use_dir + "/use.hpp",
               "#ifndef MM_USE_HPP\n"
               "#define MM_USE_HPP\n"
               "#include \"cache.hpp\"\n"
               "namespace mm {\n"
               "template <class T>\n"
               "T cache_roundtrip(Cache& c, const std::string& k, T v) {\n"
               "  c.set(k, v);          // overloaded -> multi-candidate "
               "dependent call\n"
               "  return c.get<T>(k);   // single-candidate dependent call\n"
               "}\n"
               "}  // namespace mm\n"
               "#endif\n");
    write_file(use_dir + "/use.cpp",
               "#include \"use.hpp\"\n"
               "namespace mm {\n"
               "int exercise(Cache& c) { return cache_roundtrip<int>(c, \"k\", "
               "1); }\n"
               "}  // namespace mm\n");

    auto callees_of = [&](const std::string &src_qual) {
      std::unordered_set<std::string> out;
      auto &raw = f.db.raw_db();
      auto st = raw.prepare("SELECT b.qual_name FROM edge e "
                            "JOIN symbol a ON a.id = e.src_id "
                            "JOIN symbol b ON b.id = e.dst_id "
                            "WHERE e.kind = 1 AND a.qual_name = ?");
      st.bind(1, std::string_view{src_qual});
      while (st.step()) {
        out.insert(st.col_text(0));
      }
      return out;
    };
    auto unresolved_count = [&](const std::string &qual) {
      auto &raw = f.db.raw_db();
      auto st = raw.prepare("SELECT COUNT(*) FROM symbol "
                            "WHERE qual_name = ? AND resolved = 0");
      st.bind(1, std::string_view{qual});
      REQUIRE(st.step());
      return st.col_int64(0);
    };

    // Only the use/ directory is a registered component: cache.hpp under lib/
    // is unowned when use.cpp is indexed.
    f.db.add_component("use", use_dir, "external");
    const std::string use_cpp = use_dir + "/use.cpp";
    const int64_t use_fid = f.db.add_file_path(use_cpp);
    const std::vector<std::string> use_opts = {"-I" + use_dir, "-I" + lib_dir,
                                               "-std=c++17"};
    {
      // index_one's body (symbols -> headers two-pass -> edges) inline; the TU
      // is freed before the assertions, mirroring A.index_source.
      const ParsedTu tu = f.parser.parse(use_cpp, use_opts, std::nullopt);
      REQUIRE(f.indexer.index_symbols(tu, use_cpp, use_fid) >= 0);
      f.indexer.index_headers(tu);
      f.indexer.index_edges(tu, use_cpp, use_fid);
    }

    // Before the cache TU is indexed, the calls already exist as edges to
    // (unresolved) USR-keyed stubs -- order-independence depends on this.
    const auto pre = callees_of("mm::cache_roundtrip");
    CHECK(pre.count("mm::Cache::set") == 1); // overloaded dependent call stub
    CHECK(pre.count("mm::Cache::get") == 1); // single dependent call stub
    CHECK(unresolved_count("mm::Cache::set") ==
          2); // both set overloads as unresolved stubs

    // Now index the defining TU: same USRs backfill the stubs to resolved.
    f.db.add_component("lib", lib_dir, "external");
    const std::string cache_cpp = lib_dir + "/cache.cpp";
    const int64_t cache_fid = f.db.add_file_path(cache_cpp);
    {
      const ParsedTu tu = f.parser.parse(
          cache_cpp, {"-I" + lib_dir, "-std=c++17"}, std::nullopt);
      f.indexer.index_symbols(tu, cache_cpp, cache_fid);
      f.indexer.index_headers(tu);
      f.indexer.index_edges(tu, cache_cpp, cache_fid);
    }
    f.db.resolve_pass();

    const auto post = callees_of("mm::cache_roundtrip");
    CHECK(post.count("mm::Cache::set") == 1);
    CHECK(post.count("mm::Cache::get") == 1);
    CHECK(unresolved_count("mm::Cache::set") == 0); // backfilled to resolved
    CHECK(unresolved_count("mm::Cache::get") == 0);
  }

  // --- system-header gating (no stub for stdlib/ADL overload sets) --------- #
  //
  // Mirror of Python test_system_overload_set_makes_no_stub. The stub-minting
  // that makes wrong-order indexing work is GATED to non-system headers: a
  // dependent call to a STDLIB overload set (`std::swap`) must NOT mint a
  // USR-keyed stub, or every TU touching the standard library would accumulate
  // permanent unresolved externals. `std::swap(a, b)` on dependent operands is
  // a multi-candidate dependent overload set whose every candidate lives in
  // <utility>, so it exercises the emit_overloaded_calls system-header skip.
  TEST_CASE("system-header gating: a dependent call to a std overload set "
            "(std::swap) mints no stub and adds no edge (regression for "
            "v0.14.2 gating)") {
    if (require_libclang() == nullptr) {
      return;
    }
    IndexFixture f;
    const std::string path = f.tmp + "/swapstd.cpp";
    write_file(path, "#include <utility>\n"
                     "namespace nn {\n"
                     "template <class T>\n"
                     "void swap_them(T& a, T& b) {\n"
                     "  std::swap(a, b);   // multi-candidate dependent overload "
                     "set, all in <utility>\n"
                     "}\n"
                     "}  // namespace nn\n");
    const int64_t file_id = f.add_owned_file(f.tmp, path);
    const ParsedTu tu = f.parser.parse(path, {"-std=c++17"}, std::nullopt);
    f.indexer.index_symbols(tu, tu.spelling, file_id);
    f.indexer.index_edges(tu, tu.spelling, file_id);

    auto &raw = f.db.raw_db();
    // No std:: symbol row was minted at all (resolved or not).
    {
      auto st = raw.prepare(
          "SELECT COUNT(*) FROM symbol WHERE qual_name LIKE 'std::%'");
      REQUIRE(st.step());
      CHECK(st.col_int64(0) == 0);
    }
    // And no calls edge points at a std:: target.
    {
      auto st = raw.prepare("SELECT COUNT(*) FROM edge e "
                            "JOIN symbol b ON b.id = e.dst_id "
                            "WHERE e.kind = 1 AND b.qual_name LIKE 'std::%'");
      REQUIRE(st.step());
      CHECK(st.col_int64(0) == 0);
    }
  }

} // TEST_SUITE("clang") — S06

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

// S03 tests — compiledb strip/sanitize/driver (hermetic, label "default")
// and CompileDb::load / LibClang over the real manifests compile DBs
// (suite "clang", label "clang").
//
// Skip policy (A1 amendment): libclang is now linked, so "no libclang" cannot
// occur — the binary wouldn't link.  SKIP-77 is retained ONLY for the
// fixture-gap case: when CIDX_MANIFESTS_DIR is absent (e.g. the e2e box that
// rsyncs only cidx-cpp/).  The custom main() exits 77 in that case.
#define DOCTEST_CONFIG_IMPLEMENT
#include "doctest/doctest.h"

#include <sys/stat.h>

#include <cstdlib>
#include <optional>
#include <string>
#include <vector>

#include "clangx/libclang.hpp"
#include "compiledb/compiledb.hpp"
#include "util/errors.hpp"
#include "util/pathutil.hpp"

using cidx::CompileCommand;
using cidx::CompileDb;
using cidx::LibClang;

namespace {

bool g_fixture_skipped = false;

// Returns true when CIDX_MANIFESTS_DIR points at an existing directory.
// On a host without the lab checkout (e.g. the e2e box that only rsyncs
// cidx-cpp/) the fixture cases should SKIP rather than fail.
bool require_manifests() {
  struct stat st{};
  if (::stat(CIDX_MANIFESTS_DIR, &st) != 0 || !S_ISDIR(st.st_mode)) {
    g_fixture_skipped = true;
    MESSAGE("SKIP: lab fixtures not found at " << CIDX_MANIFESTS_DIR);
    return false;
  }
  return true;
}

// A1: libclang is linked — load() is a no-op, always succeeds.
// Returns the singleton; never returns nullptr.
LibClang *require_libclang() {
  LibClang &lib = LibClang::instance();
  lib.load(); // no-op; kept for call-site compatibility
  return &lib;
}

// setenv/unsetenv with restore-on-destruction (the clang-labelled ctest
// registration may inject CIDX_LIBCLANG; don't clobber it for later cases).
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

const CompileCommand &find_command(const std::vector<CompileCommand> &cmds,
                                   const std::string &filename) {
  for (const CompileCommand &c : cmds) {
    if (c.filename == filename) {
      return c;
    }
  }
  static CompileCommand missing;
  FAIL("no command for filename ", filename);
  return missing;
}

} // namespace

// ---------------------------------------------------------------------------
// Hermetic cases (default label) — arg vectors fed directly.
// ---------------------------------------------------------------------------

TEST_CASE("strip: driver, -c/-o pair, source, glued -I absolutized") {
  // The manifests shapes.c command, verbatim.
  const std::vector<std::string> argv = {
      "cc", "-I.",      "-std=c11", "-DMAX_SHAPES=64",
      "-c", "shapes.c", "-o",       "shapes.o"};
  const auto out = CompileDb::strip_for_libclang(argv, "shapes.c", "/x/y");
  CHECK(out ==
        std::vector<std::string>{"-I/x/y", "-std=c11", "-DMAX_SHAPES=64"});
}

TEST_CASE("strip: bare drops -- -M family -Werror -pedantic-errors") {
  const std::vector<std::string> argv = {"gcc",
                                         "--",
                                         "-M",
                                         "-MM",
                                         "-MD",
                                         "-MMD",
                                         "-MG",
                                         "-MP",
                                         "-MV",
                                         "-Werror",
                                         "-pedantic-errors",
                                         "-DKEEP",
                                         "a.c"};
  const auto out = CompileDb::strip_for_libclang(argv, "a.c", "/d");
  CHECK(out == std::vector<std::string>{"-DKEEP"});
}

TEST_CASE("strip: pair drops consume the following argument") {
  const std::vector<std::string> argv = {"cc",     "-o",
                                         "out.o",  "-MF",
                                         "deps.d", "-MT",
                                         "target", "-MQ",
                                         "q",      "-dependency-file",
                                         "d.d",    "--serialize-diagnostics",
                                         "s.dia",  "-DKEEP",
                                         "a.c"};
  const auto out = CompileDb::strip_for_libclang(argv, "a.c", "/d");
  CHECK(out == std::vector<std::string>{"-DKEEP"});
}

TEST_CASE("strip: trailing pair-flag with no argument does not crash") {
  const std::vector<std::string> argv = {"cc", "-DKEEP", "-o"};
  const auto out = CompileDb::strip_for_libclang(argv, "a.c", "/d");
  CHECK(out == std::vector<std::string>{"-DKEEP"});
}

TEST_CASE("strip: prefix drops -Werror= -Wp,-M and glued -MF/-MT/-MQ") {
  const std::vector<std::string> argv = {
      "cc",        "-Werror=format", "-Wp,-MD,foo.d", "-Wp,-MMD,bar.d",
      "-MFdeps.d", "-MTtarget",      "-MQquoted",     "-Wall",
      "a.c"};
  const auto out = CompileDb::strip_for_libclang(argv, "a.c", "/d");
  CHECK(out == std::vector<std::string>{"-Wall"});
}

TEST_CASE("strip: source dropped by full path OR basename (G10)") {
  const std::vector<std::string> argv = {"cc", "/abs/dir/foo.c", "foo.c",
                                         "-DKEEP"};
  const auto out =
      CompileDb::strip_for_libclang(argv, "/abs/dir/foo.c", "/abs/dir");
  CHECK(out == std::vector<std::string>{"-DKEEP"});
}

TEST_CASE("strip: -I/-isystem/-iquote absolutized, spaced and glued (G12)") {
  const std::vector<std::string> argv = {
      "cc",           "-I",      "foo", "-Iglued/dir", "-isystem", "sys",
      "-isystemsys2", "-iquote", "q",   "-iquoteq2",   "a.c"};
  const auto out = CompileDb::strip_for_libclang(argv, "a.c", "/base");
  CHECK(out == std::vector<std::string>{"-I", "/base/foo", "-I/base/glued/dir",
                                        "-isystem", "/base/sys",
                                        "-isystem/base/sys2", "-iquote",
                                        "/base/q", "-iquote/base/q2"});
}

TEST_CASE("strip: absolute include paths pass through unchanged") {
  const std::vector<std::string> argv = {"cc", "-I/usr/include", "-isystem",
                                         "/opt/inc", "a.c"};
  const auto out = CompileDb::strip_for_libclang(argv, "a.c", "/base");
  CHECK(out ==
        std::vector<std::string>{"-I/usr/include", "-isystem", "/opt/inc"});
}

TEST_CASE("strip: relative include normpathed against directory") {
  const std::vector<std::string> argv = {"cc", "-I.", "-I../inc", "a.c"};
  const auto out = CompileDb::strip_for_libclang(argv, "a.c", "/x/y");
  CHECK(out == std::vector<std::string>{"-I/x/y", "-I/x/inc"});
}

TEST_CASE("strip: everything else untouched, order preserved") {
  const std::vector<std::string> argv = {
      "cc", "-std=c++17", "-DFOO=bar", "-Wall", "-fPIC", "-pthread", "a.c"};
  const auto out = CompileDb::strip_for_libclang(argv, "a.c", "/d");
  CHECK(out == std::vector<std::string>{"-std=c++17", "-DFOO=bar", "-Wall",
                                        "-fPIC", "-pthread"});
}

TEST_CASE("sanitize: re-applies drop rules only — no argv0/source/path fixes "
          "(G11)") {
  // argv[0] position is NOT special and the source token is NOT dropped.
  const std::vector<std::string> stored = {
      "-I.", "-Werror", "-MF", "deps.d", "-Wp,-MD,x.d", "-MFglued",
      "-c",  "foo.c",   "-o",  "foo.o",  "-DKEEP"};
  const auto out = CompileDb::sanitize(stored);
  CHECK(out == std::vector<std::string>{"-I.", "foo.c", "-DKEEP"});
}

TEST_CASE("sanitize: clean vector passes through") {
  const std::vector<std::string> stored = {"-I/abs", "-std=c11", "-DX=1"};
  CHECK(CompileDb::sanitize(stored) == stored);
}

TEST_CASE("driver: bare name kept bare for PATH resolution") {
  CHECK(CompileDb::driver({"cc", "-c", "a.c"}, "/b") == "cc");
  CHECK(CompileDb::driver({"g++"}, "/b") == "g++");
}

TEST_CASE("driver: relative path with separator absolutized") {
  CHECK(CompileDb::driver({"./gcc", "a.c"}, "/b") == "/b/gcc");
  CHECK(CompileDb::driver({"tools/bin/g++", "a.c"}, "/b") ==
        "/b/tools/bin/g++");
}

TEST_CASE("driver: absolute path unchanged") {
  CHECK(CompileDb::driver({"/opt/1A/toolchain/bin/g++", "a.c"}, "/b") ==
        "/opt/1A/toolchain/bin/g++");
}

TEST_CASE("db_dir_from_arg: trailing compile_commands.json stripped") {
  CHECK(CompileDb::db_dir_from_arg("compile_commands.json") == ".");
  CHECK(CompileDb::db_dir_from_arg("foo/compile_commands.json") == "foo/");
  CHECK(CompileDb::db_dir_from_arg("/a/b/compile_commands.json") == "/a/b/");
  CHECK(CompileDb::db_dir_from_arg("/a/b") == "/a/b");
  CHECK(CompileDb::db_dir_from_arg("some/dir") == "some/dir");
}

TEST_CASE("parse_clang_major: regex + 0 fallback (P12)") {
  CHECK(LibClang::parse_clang_major("clang version 18.1.8 "
                                    "(https://github.com/llvm/llvm-project)") ==
        18);
  CHECK(LibClang::parse_clang_major("Ubuntu clang version 21.1.1") == 21);
  CHECK(LibClang::parse_clang_major("clang version 7") == 7);
  CHECK(LibClang::parse_clang_major("garbage with no version") == 0);
  CHECK(LibClang::parse_clang_major("") == 0);
  CHECK(LibClang::parse_clang_major("version x.y") == 0);
}

// A1 facade contract tests — replaces the former dlopen/R3/R4 cases.

TEST_CASE("A1: LibClang facade is always loaded (no-dlopen build)") {
  // A1: loaded() must return true unconditionally — the binary cannot link
  // without libclang, so there is no "not loaded" state.
  cidx::LibClang lib;
  CHECK(lib.loaded());
}

TEST_CASE("A1: library_path() returns the non-empty build-time path") {
  // A1.3: library_path() returns the CIDX_LIBCLANG_PATH compile definition.
  // It must be a non-empty absolute path (not a bare name like "libclang.so")
  // so that Toolchain (S04) can derive the resource-dir from its dirname.
  cidx::LibClang lib;
  const std::string path = lib.library_path();
  REQUIRE_FALSE(path.empty());
  // Compile-definition check: must equal the macro exactly.
  CHECK(path == std::string(CIDX_LIBCLANG_PATH));
  // Must be absolute (Toolchain resource-dir derivation requires dirname).
  CHECK(path[0] == '/');
}

TEST_CASE("A1: load() is a no-op when CIDX_LIBCLANG env is unset") {
  // When the env var is absent, load() must be callable without side-effects.
  ScopedEnv clear_env("CIDX_LIBCLANG", "");
  cidx::LibClang lib;
  CHECK_NOTHROW(lib.load());
  CHECK(lib.loaded());
}

TEST_CASE("A1: load() emits a one-shot warning when CIDX_LIBCLANG is set") {
  // A1.3: a stale CIDX_LIBCLANG export must not fail — we warn once and
  // continue.  Verify: (a) no exception, (b) the instance stays loaded,
  // (c) a second call is truly idempotent (static flag, fires once per process
  // image — we verify non-throwing; the warning-counter path is covered by
  // env_logger_test).
  ScopedEnv env("CIDX_LIBCLANG", "/some/stale/libclang.so");
  cidx::LibClang lib;
  CHECK_NOTHROW(lib.load());
  CHECK(lib.loaded());
  // Second call: also no-op, no throw.
  CHECK_NOTHROW(lib.load());
}

// ---------------------------------------------------------------------------
// libclang-dependent cases (label "clang"; runtime SKIP -> exit 77).
// ---------------------------------------------------------------------------

TEST_SUITE("clang") {

  TEST_CASE("LibClang facade: loaded, major() in range, library_path set") {
    LibClang *lib = require_libclang(); // always non-null (A1)
    MESSAGE("build-time libclang path: " << lib->library_path());
    CHECK(lib->loaded());
    CHECK_FALSE(lib->library_path().empty());
    CHECK(lib->library_path()[0] == '/'); // must be absolute
    CHECK(lib->major() > 0);   // linked libclang must parse to a real version
    CHECK(lib->major() < 100); // sanity upper bound
  }

  TEST_CASE("CompileDb::load over manifests/compile_commands.json") {
    require_libclang(); // ensure load() called (no-op, A1)
    if (!require_manifests()) {
      return;
    }
    const std::string manifests = CIDX_MANIFESTS_DIR;
    const auto cmds = CompileDb::load(manifests + "/compile_commands.json");
    REQUIRE(cmds.size() == 2);

    const CompileCommand &shapes = find_command(cmds, "shapes.c");
    CHECK(shapes.directory == manifests);
    CHECK(shapes.driver == "cc");
    CHECK(shapes.args == std::vector<std::string>{"-I" + manifests, "-std=c11",
                                                  "-DMAX_SHAPES=64"});

    const CompileCommand &calls = find_command(cmds, "calls.c");
    CHECK(calls.directory == manifests);
    CHECK(calls.driver == "cc");
    CHECK(calls.args == std::vector<std::string>{"-std=c11"});
  }

  TEST_CASE("CompileDb::load accepts the directory form of --db") {
    require_libclang();
    if (!require_manifests()) {
      return;
    }
    const std::string project = std::string(CIDX_MANIFESTS_DIR) + "/project";
    const auto cmds = CompileDb::load(project); // no trailing json filename
    REQUIRE(cmds.size() == 2);

    const CompileCommand &mathlib = find_command(cmds, "mathlib.c");
    CHECK(mathlib.directory == project);
    CHECK(mathlib.driver == "cc");
    CHECK(mathlib.args == std::vector<std::string>{"-I" + project});

    const CompileCommand &app = find_command(cmds, "app.c");
    CHECK(app.args == std::vector<std::string>{"-I" + project});
  }

  TEST_CASE("CompileDb::load throws CidxError on a database-less directory") {
    require_libclang();
    CHECK_THROWS_AS(CompileDb::load("/nonexistent-cidx-db-dir"),
                    cidx::CidxError);
  }

} // TEST_SUITE("clang")

int main(int argc, char **argv) {
  doctest::Context ctx(argc, argv);
  const int res = ctx.run();
  if (ctx.shouldExit()) {
    return res;
  }
  // SKIP-77 only for fixture-gap (A1: libclang-absence can no longer occur).
  if (res == 0 && g_fixture_skipped) {
    return 77; // CTest SKIP_RETURN_CODE
  }
  return res;
}

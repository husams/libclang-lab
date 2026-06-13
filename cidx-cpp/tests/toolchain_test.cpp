// toolchain_test — S04: driver probe + search-list parsing (G8), builtin-dir
// substitution (G3), no-resource verbatim fallback (G7), gnuc masquerade with
// the malloc-attr cap and the _FloatN asymmetry (G4), host-default order
// (G2), resource-include search order, memoization (D8/D26), is_cpp (G9).
//
// Hermetic by construction: every driver is the tests/fixtures/fake-driver
// shell script symlinked under a driver-shaped name in a temp dir and
// configured through FAKE_* env vars; glibc header trees and resource dirs
// are written into temp trees; the libclang major is injected through the
// Toolchain test seam (set_libclang_major_for_test covers the major()==0
// cap-when-undeterminable path even though A1 guarantees a real major).
// The "clang" doctest suite touches a real LibClang::major() — it always
// runs under A1 (binary cannot link without libclang; no-dylib skip removed).
#define DOCTEST_CONFIG_IMPLEMENT
#include "doctest/doctest.h"

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <string>
#include <vector>

#include <unistd.h>

#include "clangx/libclang.hpp"
#include "clangx/toolchain.hpp"
#include "util/logger.hpp"

namespace fs = std::filesystem;
using cidx::LibClang;
using cidx::Logger;
using cidx::Toolchain;

namespace {

// A1: load() is a no-op (binary links libclang at build time); it never
// throws. require_libclang() now always succeeds — kept as a named helper so
// the clang test suite reads naturally.
LibClang *require_libclang() {
  LibClang &lib = LibClang::instance();
  lib.load(); // no-op under A1; emits one-shot warning if CIDX_LIBCLANG is set
  return &lib;
}

std::string make_temp_dir() {
  char tmpl[] = "/tmp/cidx_toolchain_XXXXXX";
  char *d = ::mkdtemp(tmpl);
  REQUIRE(d != nullptr);
  return d;
}

void make_dirs(const std::string &path) { fs::create_directories(path); }

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

// Neutralizes every env var the module or the fake driver reads; later plain
// ::setenv calls inside the test are rolled back to the ambient value when
// the guard dies.
struct EnvGuard {
  ScopedEnv resource{"CIDX_RESOURCE_DIR", nullptr};
  ScopedEnv gnuc{"CIDX_GNUC_VERSION", nullptr};
  ScopedEnv dirs{"FAKE_SEARCH_DIRS", nullptr};
  ScopedEnv framework{"FAKE_FRAMEWORK_DIR", nullptr};
  ScopedEnv mute{"FAKE_NO_SEARCH_OUTPUT", nullptr};
  ScopedEnv fullver{"FAKE_DUMPFULLVERSION", nullptr};
  ScopedEnv ver{"FAKE_DUMPVERSION", nullptr};
  ScopedEnv log{"FAKE_DRIVER_LOG", nullptr};
};

class ScopedChdir {
public:
  explicit ScopedChdir(const std::string &dir) : saved_(fs::current_path()) {
    fs::current_path(dir);
  }
  ~ScopedChdir() { fs::current_path(saved_); }
  ScopedChdir(const ScopedChdir &) = delete;
  ScopedChdir &operator=(const ScopedChdir &) = delete;

private:
  fs::path saved_;
};

// Symlink the fake-driver fixture under a driver-shaped basename (the gcc
// regex and the probe both see only the path string we pass).
std::string install_driver(const std::string &dir, const std::string &name) {
  const std::string fixture = std::string(CIDX_FIXTURES_DIR) + "/fake-driver";
  const std::string link = dir + "/" + name;
  REQUIRE(::symlink(fixture.c_str(), link.c_str()) == 0);
  return link;
}

bool has_token(const std::vector<std::string> &flags, const std::string &t) {
  return std::find(flags.begin(), flags.end(), t) != flags.end();
}

std::optional<std::string> gnuc_value(const std::vector<std::string> &flags) {
  const std::string prefix = "-fgnuc-version=";
  for (const std::string &f : flags) {
    if (f.rfind(prefix, 0) == 0) {
      return f.substr(prefix.size());
    }
  }
  return std::nullopt;
}

bool has_floatn(const std::vector<std::string> &flags) {
  return has_token(flags, "-D_Float32=float");
}

} // namespace

// ---------------------------------------------------------------------------
// is_cpp (G9) — args checked BEFORE the extension

TEST_CASE("is_cpp decision table") {
  CHECK(Toolchain::is_cpp("foo.c", {"--driver-mode=g++"}));
  CHECK(Toolchain::is_cpp("foo.c", {"-xc++"}));
  CHECK(Toolchain::is_cpp("foo.c", {"-x", "c++"}));
  // -x value startswith("c++") — covers c++-header etc.
  CHECK(Toolchain::is_cpp("foo.c", {"-x", "c++-header"}));
  // arg beats extension: a found "-x c" answers false WITHOUT consulting
  // the .cpp extension (Python returns startswith() directly).
  CHECK_FALSE(Toolchain::is_cpp("foo.cpp", {"-x", "c"}));
  // trailing "-x" -> IndexError parity -> extension decides
  CHECK(Toolchain::is_cpp("foo.cpp", {"-x"}));
  // first "-x" wins (args.index parity)
  CHECK(Toolchain::is_cpp("foo.c", {"-x", "c++", "-x", "c"}));

  // extension table, lowercased
  CHECK(Toolchain::is_cpp("foo.cpp", {}));
  CHECK(Toolchain::is_cpp("foo.CC", {}));
  CHECK(Toolchain::is_cpp("foo.cxx", {}));
  CHECK(Toolchain::is_cpp("foo.c++", {}));
  CHECK(Toolchain::is_cpp("foo.hpp", {}));
  CHECK(Toolchain::is_cpp("foo.hh", {}));
  CHECK(Toolchain::is_cpp("foo.HXX", {}));
  CHECK_FALSE(Toolchain::is_cpp("foo.c", {}));
  CHECK_FALSE(Toolchain::is_cpp("foo.h", {}));
  CHECK_FALSE(Toolchain::is_cpp("foo", {}));
  // os.path.splitext: leading dots of the basename are not separators
  CHECK_FALSE(Toolchain::is_cpp(".cpp", {}));
  CHECK_FALSE(Toolchain::is_cpp("/some.dir/noext", {}));
  CHECK(Toolchain::is_cpp("/some.dir/a.cpp", {}));
}

// ---------------------------------------------------------------------------
// driver_search_dirs (G8)

TEST_CASE("driver_search_dirs parses the -v list: order, normpath, "
          "exists-filter, framework skip, end marker") {
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  make_dirs(tmp + "/b");
  make_dirs(tmp + "/a");
  make_dirs(tmp + "/fw");
  const std::string driver = install_driver(tmp, "mycc");
  // b first (driver order kept), a with a trailing slash (normpath),
  // a nonexistent dir (dropped).
  ::setenv("FAKE_SEARCH_DIRS",
           (tmp + "/b:" + tmp + "/a/:" + tmp + "/missing").c_str(), 1);
  ::setenv("FAKE_FRAMEWORK_DIR", (tmp + "/fw").c_str(), 1);

  Logger log;
  Toolchain tc(log);
  const std::vector<std::string> dirs = tc.driver_search_dirs(driver, "c");
  CHECK(dirs == std::vector<std::string>{tmp + "/b", tmp + "/a"});
  // the existing-but-framework dir is skipped, and the existing "/" the
  // fixture prints AFTER "End of search list" never shows up
  CHECK_FALSE(has_token(dirs, tmp + "/fw"));
  CHECK_FALSE(has_token(dirs, "/"));
}

TEST_CASE("driver_search_dirs: mute and missing drivers yield empty") {
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  const std::string driver = install_driver(tmp, "mycc");
  ::setenv("FAKE_NO_SEARCH_OUTPUT", "1", 1);

  Logger log;
  Toolchain tc(log);
  CHECK(tc.driver_search_dirs(driver, "c").empty());
  CHECK(tc.driver_search_dirs(tmp + "/no-such-driver", "c").empty());
}

// ---------------------------------------------------------------------------
// host defaults (G2)

TEST_CASE("mute driver falls through to host defaults; order is sysroot -> "
          "libc++ -> clang builtins") {
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  const std::string driver = install_driver(tmp, "mycc");
  ::setenv("FAKE_NO_SEARCH_OUTPUT", "1", 1);
  const std::string res = tmp + "/res-include";
  make_dirs(res);

  Logger log;
  Toolchain tc(log);
  tc.set_resource_include_for_test(res);

  const std::vector<std::string> c_flags = tc.toolchain_flags(false, driver);
  const std::vector<std::string> cpp_flags = tc.toolchain_flags(true, driver);
#ifdef __APPLE__
  REQUIRE(c_flags.size() == 4);
  CHECK(c_flags[0] == "-isysroot");
  CHECK_FALSE(c_flags[1].empty());
  CHECK(c_flags[2] == "-isystem");
  CHECK(c_flags[3] == res);
  REQUIRE(cpp_flags.size() == 6);
  CHECK(cpp_flags[0] == "-isysroot");
  CHECK(cpp_flags[2] == "-isystem");
  CHECK(cpp_flags[3] == cpp_flags[1] + "/usr/include/c++/v1");
  CHECK(cpp_flags[4] == "-isystem");
  CHECK(cpp_flags[5] == res);
#else
  // non-darwin host default: resource include only
  CHECK(c_flags == std::vector<std::string>{"-isystem", res});
  CHECK(cpp_flags == std::vector<std::string>{"-isystem", res});
#endif
}

TEST_CASE("no driver at all -> host defaults too") {
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  const std::string res = tmp + "/res";
  make_dirs(res);
  Logger log;
  Toolchain tc(log);
  tc.set_resource_include_for_test(res);
  const std::vector<std::string> flags =
      tc.toolchain_flags(false, std::nullopt);
  REQUIRE(!flags.empty());
  CHECK(flags.back() == res);
  CHECK(flags[flags.size() - 2] == "-isystem");
  // empty-string driver is falsy in Python — same fallback
  CHECK(tc.toolchain_flags(false, std::string()) == flags);
}

// ---------------------------------------------------------------------------
// builtin-dir substitution (G3)

TEST_CASE("gcc builtin include + include-fixed are replaced by ONE resource "
          "include at the first occurrence's position") {
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  const std::string cxx = tmp + "/cxx";
  const std::string gcc_inc =
      tmp + "/lib/gcc/x86_64-redhat-linux/8.5.0/include";
  const std::string gcc_fixed =
      tmp + "/lib/gcc/x86_64-redhat-linux/8.5.0/include-fixed";
  const std::string libc = tmp + "/usr-include";
  for (const std::string &d : {cxx, gcc_inc, gcc_fixed, libc}) {
    make_dirs(d);
  }
  const std::string res = tmp + "/res";
  make_dirs(res);
  const std::string driver = install_driver(tmp, "mycc"); // non-gcc: no gnuc
  ::setenv("FAKE_SEARCH_DIRS",
           (cxx + ":" + gcc_inc + ":" + gcc_fixed + ":" + libc).c_str(), 1);

  Logger log;
  Toolchain tc(log);
  tc.set_resource_include_for_test(res);
  const std::vector<std::string> flags = tc.toolchain_flags(false, driver);
  CHECK(flags == std::vector<std::string>{"-nostdinc", "-isystem", cxx,
                                          "-isystem", res, "-isystem", libc});
}

TEST_CASE("foreign clang resource dirs and lib32/lib64 variants match the "
          "builtin regex") {
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  const std::string res = tmp + "/res";
  make_dirs(res);

  const auto run_case = [&](const std::string &builtin_dir) {
    const std::string sub = make_temp_dir();
    const std::string before = sub + "/before";
    const std::string builtin = sub + builtin_dir;
    const std::string after = sub + "/after";
    for (const std::string &d : {before, builtin, after}) {
      make_dirs(d);
    }
    const std::string driver = install_driver(sub, "mycc");
    ::setenv("FAKE_SEARCH_DIRS", (before + ":" + builtin + ":" + after).c_str(),
             1);
    Logger log;
    Toolchain tc(log);
    tc.set_resource_include_for_test(res);
    return tc.toolchain_flags(false, driver);
  };

  for (const char *variant : {"/lib/clang/17/include", "/lib64/gcc/t/9/include",
                              "/lib32/gcc-cross/t/9/include"}) {
    CAPTURE(variant);
    const std::vector<std::string> flags = run_case(variant);
    REQUIRE(flags.size() == 7);
    CHECK(flags[4] == res); // substituted at the builtin's position
    CHECK_FALSE(has_token(flags, std::string(variant)));
  }
}

TEST_CASE("no dir matches the builtin regex -> resource include appended "
          "last") {
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  const std::string a = tmp + "/a";
  const std::string b = tmp + "/b";
  make_dirs(a);
  make_dirs(b);
  const std::string res = tmp + "/res";
  make_dirs(res);
  const std::string driver = install_driver(tmp, "mycc");
  ::setenv("FAKE_SEARCH_DIRS", (a + ":" + b).c_str(), 1);

  Logger log;
  Toolchain tc(log);
  tc.set_resource_include_for_test(res);
  CHECK(tc.toolchain_flags(false, driver) ==
        std::vector<std::string>{"-nostdinc", "-isystem", a, "-isystem", b,
                                 "-isystem", res});
}

// ---------------------------------------------------------------------------
// G7: resource include missing entirely

TEST_CASE("missing resource include -> WARNING + verbatim driver list "
          "including the gcc builtin dirs; the warning re-fires per call") {
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  const std::string cxx = tmp + "/cxx";
  const std::string gcc_inc = tmp + "/lib/gcc/t/8/include";
  for (const std::string &d : {cxx, gcc_inc}) {
    make_dirs(d);
  }
  const std::string driver = install_driver(tmp, "mycc");
  ::setenv("FAKE_SEARCH_DIRS", (cxx + ":" + gcc_inc).c_str(), 1);

  Logger log;
  log.set_file(tmp + "/cidx.log");
  Toolchain tc(log);
  tc.set_resource_include_for_test(std::nullopt);

  const std::vector<std::string> flags = tc.toolchain_flags(false, driver);
  CHECK(flags == std::vector<std::string>{"-nostdinc", "-isystem", cxx,
                                          "-isystem", gcc_inc});
  CHECK(log.warning_count() == 1);
  const std::string logged = read_file(tmp + "/cidx.log");
  CHECK(logged.find("no clang builtin headers found (set CIDX_RESOURCE_DIR "
                    "or install clang); falling back to " +
                    driver + "'s own builtin headers") != std::string::npos);

  // Python warns on EVERY driver_flags call — the memoized result must
  // re-emit (warning counter is load-bearing for the CLI summary line).
  CHECK(tc.toolchain_flags(false, driver) == flags);
  CHECK(log.warning_count() == 2);
}

// ---------------------------------------------------------------------------
// gnuc masquerade decision table (G4)

namespace {

struct GnucCase {
  std::string tmp;
  std::string driver;
  std::string search_dir;
  Logger log;
  Toolchain tc{log};

  explicit GnucCase(const std::string &driver_name) {
    tmp = make_temp_dir();
    search_dir = tmp + "/sd";
    make_dirs(search_dir);
    make_dirs(tmp + "/res");
    driver = install_driver(tmp, driver_name);
    ::setenv("FAKE_SEARCH_DIRS", search_dir.c_str(), 1);
    tc.set_resource_include_for_test(tmp + "/res");
  }
  void add_attr_dealloc_cdefs() {
    write_file(search_dir + "/sys/cdefs.h",
               "#define __attr_dealloc(dealloc, argno) ...\n");
  }
  void add_floatn_common(bool keyword13) {
    write_file(search_dir + "/bits/floatn-common.h",
               keyword13 ? "#if !__GNUC_PREREQ (13, 0)\n"
                         : "#if !__GNUC_PREREQ (7, 0)\n");
  }
  std::vector<std::string> flags(bool cpp) {
    return tc.toolchain_flags(cpp, driver);
  }
};

} // namespace

TEST_CASE("gnuc: non-gcc driver name -> no flag and no version probe") {
  EnvGuard env;
  GnucCase c("mycc");
  ::setenv("FAKE_DRIVER_LOG", (c.tmp + "/probe.log").c_str(), 1);
  ::setenv("FAKE_DUMPFULLVERSION", "11.4.0", 1);
  const std::vector<std::string> flags = c.flags(false);
  CHECK_FALSE(gnuc_value(flags));
  CHECK_FALSE(has_floatn(flags));
  // the basename regex gates the subprocess: no -dumpfullversion probe ran
  CHECK(read_file(c.tmp + "/probe.log").find("dump") == std::string::npos);
}

TEST_CASE("gnuc: gcc-8.5 -> -fgnuc-version=8.5.0, no cap, FloatN for C only") {
  EnvGuard env;
  GnucCase c("gcc-8.5");
  ::setenv("FAKE_DUMPFULLVERSION", "8.5.0", 1);
  c.tc.set_libclang_major_for_test(18);
  const std::vector<std::string> flags = c.flags(false);
  CHECK(gnuc_value(flags) == std::string("8.5.0"));
  CHECK(has_floatn(flags)); // C && major >= 7: always
}

TEST_CASE("gnuc: -dumpfullversion failure falls back to -dumpversion; "
          "malformed output is rejected") {
  EnvGuard env;
  {
    GnucCase c("gcc");
    ::setenv("FAKE_DUMPVERSION", "9.2", 1); // no FAKE_DUMPFULLVERSION: exit 1
    CHECK(gnuc_value(c.flags(false)) == std::string("9.2"));
  }
  {
    GnucCase c("g++");
    ::setenv("FAKE_DUMPFULLVERSION", "not a version", 1);
    ::setenv("FAKE_DUMPVERSION", "8", 1);
    CHECK(gnuc_value(c.flags(false)) == std::string("8"));
  }
}

TEST_CASE("gnuc cap: gcc-11 + __attr_dealloc cdefs + libclang major < 21 "
          "-> capped to 10.9") {
  EnvGuard env;
  GnucCase c("gcc-11");
  ::setenv("FAKE_DUMPFULLVERSION", "11.4.1", 1);
  c.add_attr_dealloc_cdefs();
  c.tc.set_libclang_major_for_test(18);
  const std::vector<std::string> flags = c.flags(false);
  CHECK(gnuc_value(flags) == std::string("10.9"));
  CHECK(has_floatn(flags)); // capped major 10 is still >= 7
}

TEST_CASE("gnuc cap: same probe but libclang major >= 21 -> no cap") {
  EnvGuard env;
  GnucCase c("gcc-11");
  ::setenv("FAKE_DUMPFULLVERSION", "11.4.1", 1);
  c.add_attr_dealloc_cdefs();
  c.tc.set_libclang_major_for_test(21);
  CHECK(gnuc_value(c.flags(false)) == std::string("11.4.1"));
}

TEST_CASE("gnuc cap: major >= 11 but cdefs lacks __attr_dealloc -> no cap") {
  EnvGuard env;
  GnucCase c("gcc-11");
  ::setenv("FAKE_DUMPFULLVERSION", "11.4.1", 1);
  write_file(c.search_dir + "/sys/cdefs.h", "/* pre-2.34 glibc */\n");
  c.tc.set_libclang_major_for_test(18);
  CHECK(gnuc_value(c.flags(false)) == std::string("11.4.1"));
}

TEST_CASE("gnuc: explicit CIDX_GNUC_VERSION bypasses the cap") {
  EnvGuard env;
  GnucCase c("gcc-11");
  ::setenv("FAKE_DUMPFULLVERSION", "11.4.1", 1);
  c.add_attr_dealloc_cdefs();
  c.tc.set_libclang_major_for_test(18);
  ::setenv("CIDX_GNUC_VERSION", "12", 1);
  CHECK(gnuc_value(c.flags(false)) == std::string("12"));
}

TEST_CASE("gnuc: CIDX_GNUC_VERSION=off disables the flag entirely") {
  EnvGuard env;
  GnucCase c("gcc-11");
  ::setenv("FAKE_DUMPFULLVERSION", "11.4.1", 1);
  ::setenv("CIDX_GNUC_VERSION", "off", 1);
  const std::vector<std::string> flags = c.flags(false);
  CHECK_FALSE(gnuc_value(flags));
  CHECK_FALSE(has_floatn(flags));
}

TEST_CASE("gnuc: env override applies even to a non-gcc driver") {
  EnvGuard env;
  GnucCase c("mycc");
  ::setenv("CIDX_GNUC_VERSION", "10", 1);
  CHECK(gnuc_value(c.flags(false)) == std::string("10"));
}

TEST_CASE("FloatN asymmetry: C++ needs major >= 13 AND the floatn-common "
          "keyword probe") {
  EnvGuard env;
  { // C++ major 12 -> never
    GnucCase c("g++-12");
    ::setenv("FAKE_DUMPFULLVERSION", "12.3.0", 1);
    c.add_floatn_common(true);
    const std::vector<std::string> flags = c.flags(true);
    CHECK(gnuc_value(flags) == std::string("12.3.0"));
    CHECK_FALSE(has_floatn(flags));
  }
  { // C++ major 13 + __GNUC_PREREQ (13 -> aliases
    GnucCase c("g++-13");
    ::setenv("FAKE_DUMPFULLVERSION", "13.2.0", 1);
    c.add_floatn_common(true);
    CHECK(has_floatn(c.flags(true)));
  }
  { // C++ major 13, glibc typedef path (no __GNUC_PREREQ (13) -> NO aliases
    GnucCase c("g++-13");
    ::setenv("FAKE_DUMPFULLVERSION", "13.2.0", 1);
    c.add_floatn_common(false);
    CHECK_FALSE(has_floatn(c.flags(true)));
  }
  { // C major 7 -> aliases regardless of any probe
    GnucCase c("gcc-7");
    ::setenv("FAKE_DUMPFULLVERSION", "7.5.0", 1);
    CHECK(has_floatn(c.flags(false)));
  }
  { // C major 6 -> none
    GnucCase c("gcc-6");
    ::setenv("FAKE_DUMPFULLVERSION", "6.3.0", 1);
    CHECK_FALSE(has_floatn(c.flags(false)));
  }
}

// ---------------------------------------------------------------------------
// memoization (D8, D26): one subprocess per (driver[,lang]) per run

TEST_CASE("repeated toolchain_flags issue exactly one probe per (driver, "
          "lang) and one version probe per driver") {
  EnvGuard env;
  GnucCase c("gcc-8.5");
  ::setenv("FAKE_DUMPFULLVERSION", "8.5.0", 1);
  const std::string probe_log = c.tmp + "/probe.log";
  ::setenv("FAKE_DRIVER_LOG", probe_log.c_str(), 1);
  c.tc.set_libclang_major_for_test(18);

  const std::vector<std::string> first = c.flags(false);
  CHECK(c.flags(false) == first); // memo hit, byte-identical
  c.flags(true);                  // new lang -> one more search probe
  c.flags(true);

  const std::string logged = read_file(probe_log);
  CHECK(count_occurrences(logged, "-E -x c - -v") == 1);
  CHECK(count_occurrences(logged, "-E -x c++ - -v") == 1);
  CHECK(count_occurrences(logged, "-dumpfullversion") == 1);
  CHECK(count_occurrences(logged, "-dumpversion") == 0);
}

// ---------------------------------------------------------------------------
// resource_include search order

TEST_CASE("resource_include step 1: $CIDX_RESOURCE_DIR/include wins when it "
          "holds stddef.h") {
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  write_file(tmp + "/res/include/stddef.h", "");
  ::setenv("CIDX_RESOURCE_DIR", (tmp + "/res").c_str(), 1);
  Logger log;
  Toolchain tc(log);
  CHECK(tc.resource_include() == tmp + "/res/include");
}

TEST_CASE("resource_include step 2: lib/clang/*/include next to the linked "
          "library, reverse-sorted, stddef.h required") {
  EnvGuard env;
  { // 19 beats 18 (sorted(reverse=True))
    const std::string tmp = make_temp_dir();
    write_file(tmp + "/lib/clang/18/include/stddef.h", "");
    write_file(tmp + "/lib/clang/19/include/stddef.h", "");
    Logger log;
    Toolchain tc(log);
    tc.set_libclang_path_for_test(tmp + "/lib/libclang.so");
    CHECK(tc.resource_include() == tmp + "/lib/clang/19/include");
  }
  { // a higher version without stddef.h is skipped
    const std::string tmp = make_temp_dir();
    write_file(tmp + "/lib/clang/18/include/stddef.h", "");
    make_dirs(tmp + "/lib/clang/19/include"); // empty: no stddef.h
    Logger log;
    Toolchain tc(log);
    tc.set_libclang_path_for_test(tmp + "/lib/libclang.so");
    CHECK(tc.resource_include() == tmp + "/lib/clang/18/include");
  }
  // an env CIDX_RESOURCE_DIR without stddef.h falls through to step 2
  {
    const std::string tmp = make_temp_dir();
    make_dirs(tmp + "/res/include"); // no stddef.h
    write_file(tmp + "/lib/clang/18/include/stddef.h", "");
    ::setenv("CIDX_RESOURCE_DIR", (tmp + "/res").c_str(), 1);
    Logger log;
    Toolchain tc(log);
    tc.set_libclang_path_for_test(tmp + "/lib/libclang.so");
    CHECK(tc.resource_include() == tmp + "/lib/clang/18/include");
  }
}

TEST_CASE("resource_include: a bare library name (no dirname) -> step 2 "
          "is skipped (seam exercises empty-dirname guard)") {
  // Under A1, library_path() always returns an absolute path so this guard is
  // dead in production.  The seam (set_libclang_path_for_test) is used here to
  // verify the guard: if step 2 ran with libdir == "" the relative glob
  // clang/*/include would find the tree below from cwd.
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  write_file(tmp + "/clang/99/include/stddef.h", "");
  ScopedChdir cd(tmp);
  Logger log;
  Toolchain tc(log);
  tc.set_libclang_path_for_test("libclang.so");
  const std::optional<std::string> inc = tc.resource_include();
  // steps 3/4 may legitimately answer on the host; it just must never be
  // the relative tree
  CHECK(inc != std::string("clang/99/include"));
  if (inc) {
    CHECK(inc->find(tmp) == std::string::npos);
  }
}

TEST_CASE("resource_include is memoized — env changes after the first call "
          "are invisible (lru_cache parity)") {
  EnvGuard env;
  const std::string tmp = make_temp_dir();
  write_file(tmp + "/resA/include/stddef.h", "");
  write_file(tmp + "/resB/include/stddef.h", "");
  ::setenv("CIDX_RESOURCE_DIR", (tmp + "/resA").c_str(), 1);
  Logger log;
  Toolchain tc(log);
  CHECK(tc.resource_include() == tmp + "/resA/include");
  ::setenv("CIDX_RESOURCE_DIR", (tmp + "/resB").c_str(), 1);
  CHECK(tc.resource_include() == tmp + "/resA/include");
}

TEST_CASE("pick_best_resource: best NUMERIC version wins across glob "
          "candidates; non-numeric sorts as (0,)") {
  const std::string tmp = make_temp_dir();
  const std::string v9 = tmp + "/a/lib/clang/9/include";
  const std::string v18 = tmp + "/b/lib/clang/18/include";
  const std::string weird = tmp + "/c/lib/clang/weird/include";
  for (const std::string &d : {v9, v18, weird}) {
    write_file(d + "/stddef.h", "");
  }
  // 18 > 9 numerically (a reverse STRING sort would pick 9)
  CHECK(Toolchain::pick_best_resource({v9, v18, weird}) == v18);
  CHECK(Toolchain::pick_best_resource({weird, v9}) == v9);
  // stddef.h is required
  const std::string empty_inc = tmp + "/d/lib/clang/99/include";
  make_dirs(empty_inc);
  CHECK(Toolchain::pick_best_resource({empty_inc}) == std::nullopt);
  CHECK(Toolchain::pick_best_resource({}) == std::nullopt);
}

// ---------------------------------------------------------------------------
// clang-labelled suite: the only major()-dependent smoke (real libclang)

TEST_SUITE("clang") {

  TEST_CASE("gnuc cap follows the REAL LibClang::major()") {
    // A1: require_libclang() always succeeds (binary links libclang).
    LibClang *lib = require_libclang();
    EnvGuard env;
    GnucCase c("gcc-11");
    ::setenv("FAKE_DUMPFULLVERSION", "11.4.1", 1);
    c.add_attr_dealloc_cdefs();
    // No major override: Toolchain consults LibClang::instance().major().
    const std::optional<std::string> ver = gnuc_value(c.flags(false));
    if (lib->major() < 21) {
      CHECK(ver == std::string("10.9"));
    } else {
      CHECK(ver == std::string("11.4.1"));
    }
  }

} // TEST_SUITE("clang")

int main(int argc, char **argv) {
  doctest::Context ctx(argc, argv);
  const int res = ctx.run();
  // A1: no-dylib skip path removed (binary always links libclang).
  // Exit 77 (CTest SKIP_RETURN_CODE) is no longer emitted by this test.
  return ctx.shouldExit() ? res : res;
}

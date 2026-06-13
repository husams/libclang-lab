// Toolchain resolution + gnuc masquerade (design §5.6; analysis §5.4-§5.5).
// Port of the toolchain half of project/indexer/clang/util.py.
//
// Why this exists (G1): the pip libclang wheel ships the dylib but NOT clang's
// builtin headers, so a bare parse dies with a fatal 'stddef.h not found' that
// silently truncates the AST. toolchain_flags() appends the missing search
// paths -- either by replicating the compile command's driver (`<driver> -E -x
// <lang> - -v`, G8) with the driver's builtin dir swapped for THIS libclang's
// resource include (G3, include-fixed gotcha), or by host defaults (macOS
// sysroot -> libc++ -> clang builtins; order load-bearing, G2). The
// -fgnuc-version masquerade (G4) makes clang claim the driver's __GNUC__,
// with the glibc malloc-attr cap (10.9 when libclang < 21) and the
// C/C++-asymmetric _FloatN aliases.
//
// All driver probes are memoized in plain std::map (D8: one instance per run,
// not thread-safe by design; D26: in-process only, no persistent cache). One
// subprocess per (driver[,lang]) per run.
#pragma once

#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "util/logger.hpp"

namespace cidx {

class Toolchain {
public:
  // Default sink is the process logger; tests pass their own (D7 precedent).
  explicit Toolchain(Logger &log = Logger::root()) : log_(log) {}
  Toolchain(const Toolchain &) = delete;
  Toolchain &operator=(const Toolchain &) = delete;

  // util.py:334-358 -- search-path flags appended to every parse. With a
  // driver that answers, its search list is replicated (driver_flags);
  // otherwise host defaults: macOS `-isysroot $(xcrun --show-sdk-path)`
  // [+ `-isystem <sdk>/usr/include/c++/v1` iff cpp] then `-isystem
  // <resource include>` (G2); non-darwin: resource include only.
  std::vector<std::string>
  toolchain_flags(bool cpp, const std::optional<std::string> &driver);

  // util.py:322-331 -- args checked BEFORE the extension (G9):
  // `--driver-mode=g++`, `-xc++`, or `-x` followed by a value starting with
  // "c++" (a found `-x c` answers false WITHOUT consulting the extension);
  // else extension in {.cpp .cc .cxx .c++ .hpp .hh .hxx}, lowercased.
  static bool is_cpp(const std::string &filename,
                     const std::vector<std::string> &args);

  // util.py:130-159 -- the driver's `#include <...>` system search list, in
  // driver order. `<driver> -E -x <lang> - -v` with empty stdin and a 30 s
  // timeout (D9); stderr parsed between "#include <...> search starts here"
  // and "End of search list"; "(framework directory)" lines skipped; only
  // existing dirs kept, normpathed. Empty on a mute/missing driver (G8).
  // Memoized per (driver, lang). Public for unit tests; production goes
  // through toolchain_flags().
  std::vector<std::string> driver_search_dirs(const std::string &driver,
                                              const std::string &lang);

  // util.py:74-126 -- clang's builtin-header dir; first dir containing
  // stddef.h wins. Search order: $CIDX_RESOURCE_DIR/include ->
  // <dirname(libclang path)>/clang/*/include (reverse-sorted, Python
  // sorted(reverse=True) parity) -> PATH clang/clang++ -print-resource-dir ->
  // glob fallbacks via pick_best_resource(). Memoized (incl. a nullopt
  // result). Public for unit tests.
  std::optional<std::string> resource_include();

  // The step-4 glob fallback selector: among `.../<ver>/include` candidates
  // containing stddef.h, pick the best numeric version (non-numeric -> (0,);
  // ties broken by path string, Python max((key, inc)) parity). Exposed
  // static so tests can feed tmp-built candidate lists.
  static std::optional<std::string>
  pick_best_resource(const std::vector<std::string> &candidates);

  // --- test seams (story S04: hermetic gnuc/cap and resource-dir cases) ----
  // Inject the libclang major consulted by the gnuc cap (G4) instead of
  // LibClang::instance().major().
  void set_libclang_major_for_test(int major) { major_override_ = major; }
  // Stand in for LibClang::instance().library_path() in resource_include()
  // step 2 (a bare name has no dirname and yields no derivation).
  void set_libclang_path_for_test(const std::string &path) {
    libclang_path_override_ = path;
  }
  // Pre-seed the resource_include() memo (nullopt = "searched, missing" --
  // drives the G7 verbatim-fallback path without touching the host).
  void set_resource_include_for_test(std::optional<std::string> include_dir) {
    resource_memo_set_ = true;
    resource_memo_ = std::move(include_dir);
  }

private:
  // util.py:280-319 -- -nostdinc + gnuc flags + dirs as -isystem in driver
  // order; any dir matching kBuiltinDirRe is dropped and replaced -- once, at
  // the FIRST occurrence's position -- by this libclang's resource include;
  // appended last when never matched (G3). No resource include anywhere ->
  // WARNING + the driver list verbatim (G7; the warning re-fires on memo hits
  // so the per-call Python log behavior is preserved). Memoized (driver,lang).
  std::vector<std::string> driver_flags(const std::string &driver, bool cpp);

  // util.py:165-179 -- nullopt for a non-gcc basename (regex
  // (^|-)(gcc|g\+\+)(-[\d.]+)?$); else -dumpfullversion then -dumpversion,
  // accepted iff the full output matches \d+(\.\d+)*. Memoized per driver.
  std::optional<std::string> gcc_version(const std::string &driver);

  // util.py:233-266 -- the -fgnuc-version flag + _FloatN -D aliases (G4).
  std::vector<std::string> gnuc_flags(const std::string &driver, bool cpp);

  // util.py:202-221 -- (cxx13_floatn_keywords, malloc_attr_args) probed from
  // the driver's search dirs (bits/floatn-common.h "__GNUC_PREREQ (13",
  // sys/cdefs.h "__attr_dealloc"). Memoized per (driver, cpp).
  std::pair<bool, bool> glibc_probe(const std::string &driver, bool cpp);

  // util.py:61-71 -- macOS SDK path via xcrun, nullopt elsewhere. Memoized.
  std::optional<std::string> sysroot();

  // Major of the loaded libclang; the test override wins, otherwise
  // LibClang::instance() (0 when undeterminable -- Python parity).
  int libclang_major() const;

  Logger &log_;
  std::optional<int> major_override_;
  std::optional<std::string> libclang_path_override_;

  struct DriverFlagsEntry {
    std::vector<std::string> flags;
    bool warned_no_resource = false;
  };
  std::map<std::pair<std::string, std::string>, std::vector<std::string>>
      search_dirs_memo_;
  std::map<std::string, std::optional<std::string>> gcc_version_memo_;
  std::map<std::pair<std::string, bool>, std::pair<bool, bool>> glibc_memo_;
  std::map<std::pair<std::string, bool>, DriverFlagsEntry> driver_flags_memo_;
  bool resource_memo_set_ = false;
  std::optional<std::string> resource_memo_;
  bool sysroot_memo_set_ = false;
  std::optional<std::string> sysroot_memo_;
};

} // namespace cidx

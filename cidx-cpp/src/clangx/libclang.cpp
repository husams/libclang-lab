// Amendment A1 (spec/02-design.md §12): libclang is now linked at build time;
// the dlopen/dlsym shim is gone.  This file contains only the singleton,
// load() no-op, major() caching, and parse_clang_major() regex helper.
#include "clangx/libclang.hpp"

#include <cstdlib>
#include <regex>

#include "util/env.hpp"
#include "util/logger.hpp"

namespace cidx {

LibClang &LibClang::instance() {
  static LibClang inst;
  return inst;
}

void LibClang::load() {
  // A1.3: CIDX_LIBCLANG at runtime is ignored — it is now a configure-time
  // CMake hint only.  Warn exactly once so a stale export is visible but not
  // fatal.
  static bool warned = false;
  if (!warned) {
    const std::optional<std::string> env = get_env("CIDX_LIBCLANG");
    if (env && !env->empty()) {
      warned = true;
      Logger::root().warning(
          "cidx",
          "CIDX_LIBCLANG is set but ignored: this build links libclang at " +
              library_path() + " (set at build time)");
    }
  }
}

int LibClang::parse_clang_major(const std::string &version_string) {
  static const std::regex kVersionRe("version (\\d+)");
  std::smatch m;
  if (!std::regex_search(version_string, m, kVersionRe)) {
    return 0; // P12: no match -> 0
  }
  return std::atoi(m[1].str().c_str());
}

int LibClang::major() {
  if (!major_) {
    const CxString version(*this, clang_getClangVersion());
    major_ = parse_clang_major(version.str());
  }
  return *major_;
}

} // namespace cidx

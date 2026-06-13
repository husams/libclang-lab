// Toolchain resolution + gnuc masquerade -- see toolchain.hpp. Line-level
// behavior is pinned to project/indexer/clang/util.py (cited per function).
#include "clangx/toolchain.hpp"

#include <glob.h>
#include <sys/stat.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <regex>
#include <sstream>

#include "clangx/libclang.hpp"
#include "util/env.hpp"
#include "util/pathutil.hpp"
#include "util/subprocess.hpp"

namespace cidx {
namespace {

constexpr const char *kLogName = "cidx.clang";
constexpr const char *kResourceEnv = "CIDX_RESOURCE_DIR";
constexpr const char *kGnucEnv = "CIDX_GNUC_VERSION";

// util.py:227-231 -- _Float32 & co are gcc keywords clang doesn't implement;
// alias them to plain types. Parse-fidelity only, no codegen.
constexpr std::array<const char *, 5> kFloatnAliases = {
    "-D_Float32=float", "-D_Float64=double", "-D_Float128=long double",
    "-D_Float32x=double", "-D_Float64x=long double"};

// util.py:162
const std::regex &gcc_driver_re() {
  static const std::regex re(R"((^|-)(gcc|g\+\+)(-[\d.]+)?$)");
  return re;
}

// util.py:277 -- a compiler's private header dirs inside its search list:
// gcc's lib/gcc/<triple>/<ver>/include + include-fixed, or clang's
// lib/clang/<ver>/include. Never fed to libclang (G3): gcc's
// include-fixed/limits.h keys on _GCC_LIMITS_H_, which clang's own limits.h
// defines before #include_next -- severing the chain to glibc's limits.h.
const std::regex &builtin_dir_re() {
  static const std::regex re(
      R"([/\\]lib(32|64)?[/\\](gcc|gcc-cross|clang)[/\\])");
  return re;
}

// util.py:177 -- re.fullmatch(r"\d+(\.\d+)*", v)
const std::regex &version_re() {
  static const std::regex re(R"(\d+(\.\d+)*)");
  return re;
}

// util.py:216 -- re.search(r"__GNUC_PREREQ\s*\(13", ...)
const std::regex &gnuc_prereq13_re() {
  static const std::regex re(R"(__GNUC_PREREQ\s*\(13)");
  return re;
}

std::string strip(const std::string &s) {
  const char *ws = " \t\n\r\f\v";
  const std::size_t b = s.find_first_not_of(ws);
  if (b == std::string::npos) {
    return std::string();
  }
  const std::size_t e = s.find_last_not_of(ws);
  return s.substr(b, e - b + 1);
}


bool is_dir(const std::string &path) {
  struct stat st{};
  return ::stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}

bool path_exists(const std::string &path) {
  struct stat st{};
  return ::stat(path.c_str(), &st) == 0;
}

// open(..., errors="ignore") parity: raw bytes, the probes only search for
// ASCII substrings. Unreadable -> "".
std::string read_file_ignore_errors(const std::string &path) {
  std::ifstream in(path, std::ios::binary);
  std::ostringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

std::vector<std::string> glob_paths(const std::string &pattern) {
  std::vector<std::string> out;
  ::glob_t g{};
  if (::glob(pattern.c_str(), 0, nullptr, &g) == 0) {
    out.reserve(g.gl_pathc);
    for (std::size_t i = 0; i < g.gl_pathc; ++i) {
      out.emplace_back(g.gl_pathv[i]);
    }
  }
  ::globfree(&g);
  return out;
}

std::vector<std::string> split_lines(const std::string &text) {
  std::vector<std::string> lines;
  std::string::size_type pos = 0;
  while (pos <= text.size()) {
    const std::string::size_type nl = text.find('\n', pos);
    if (nl == std::string::npos) {
      if (pos < text.size()) {
        lines.push_back(text.substr(pos));
      }
      break;
    }
    lines.push_back(text.substr(pos, nl - pos));
    pos = nl + 1;
  }
  return lines;
}

// int(ver.split(".")[0]) with ValueError -> 0 (util.py:256-259).
int parse_major_or_zero(const std::string &ver) {
  std::string seg = strip(ver.substr(0, ver.find('.')));
  if (seg.empty()) {
    return 0;
  }
  std::size_t i = (seg[0] == '+' || seg[0] == '-') ? 1 : 0;
  if (i == seg.size()) {
    return 0;
  }
  for (std::size_t j = i; j < seg.size(); ++j) {
    if (std::isdigit(static_cast<unsigned char>(seg[j])) == 0) {
      return 0;
    }
  }
  return static_cast<int>(std::strtol(seg.c_str(), nullptr, 10));
}

// tuple(int(p) for p in ver.split(".")) with ValueError -> (0,)
// (util.py:118-123).
std::vector<long> version_key(const std::string &ver) {
  std::vector<long> key;
  std::string::size_type pos = 0;
  while (true) {
    const std::string::size_type dot = ver.find('.', pos);
    const std::string part = ver.substr(
        pos, dot == std::string::npos ? std::string::npos : dot - pos);
    if (part.empty() ||
        !std::all_of(part.begin(), part.end(),
                     [](unsigned char c) { return std::isdigit(c) != 0; })) {
      return {0};
    }
    key.push_back(std::strtol(part.c_str(), nullptr, 10));
    if (dot == std::string::npos) {
      break;
    }
    pos = dot + 1;
  }
  return key;
}

bool resource_check(const std::string &include_dir) {
  return path_exists(pathutil::join(include_dir, std::string("stddef.h")));
}

} // namespace

// ---------------------------------------------------------------------------
// driver probes

std::vector<std::string>
Toolchain::driver_search_dirs(const std::string &driver,
                              const std::string &lang) {
  const auto key = std::make_pair(driver, lang);
  const auto it = search_dirs_memo_.find(key);
  if (it != search_dirs_memo_.end()) {
    return it->second;
  }
  std::vector<std::string> dirs;
  const RunResult res = run({driver, "-E", "-x", lang, "-", "-v"}, 30.0);
  // Python returns () on TimeoutExpired; a nonzero exit still gets parsed
  // (subprocess.run without check=True).
  if (!res.timed_out) {
    bool active = false;
    for (const std::string &line : split_lines(res.err)) {
      if (line.starts_with("#include <...> search starts here")) {
        active = true;
        continue;
      }
      if (line.starts_with("End of search list")) {
        break;
      }
      if (active && !line.empty() && line[0] == ' ') {
        std::string d = strip(line);
        if (d.ends_with("(framework directory)")) { // macOS noise
          continue;
        }
        d = pathutil::normpath(d);
        if (is_dir(d)) {
          dirs.push_back(d);
        }
      }
    }
  }
  search_dirs_memo_.emplace(key, dirs);
  return dirs;
}

std::optional<std::string> Toolchain::gcc_version(const std::string &driver) {
  const auto it = gcc_version_memo_.find(driver);
  if (it != gcc_version_memo_.end()) {
    return it->second;
  }
  std::optional<std::string> version;
  if (std::regex_search(pathutil::basename(driver), gcc_driver_re())) {
    for (const char *flag : {"-dumpfullversion", "-dumpversion"}) {
      const RunResult res = run({driver, flag}, 30.0);
      if (res.exit_code != 0 || res.timed_out) { // check_output raise parity
        continue;
      }
      const std::string v = strip(res.out);
      if (std::regex_match(v, version_re())) {
        version = v;
        break;
      }
    }
  }
  gcc_version_memo_.emplace(driver, version);
  return version;
}

std::pair<bool, bool> Toolchain::glibc_probe(const std::string &driver,
                                             bool cpp) {
  const auto key = std::make_pair(driver, cpp);
  const auto it = glibc_memo_.find(key);
  if (it != glibc_memo_.end()) {
    return it->second;
  }
  bool floatn13 = false;
  bool malloc_args = false;
  for (const std::string &d : driver_search_dirs(driver, cpp ? "c++" : "c")) {
    const std::string f =
        pathutil::join(d, std::string("bits"), std::string("floatn-common.h"));
    if (!floatn13 && path_exists(f)) {
      const std::string text = read_file_ignore_errors(f);
      floatn13 = std::regex_search(text, gnuc_prereq13_re());
    }
    const std::string c =
        pathutil::join(d, std::string("sys"), std::string("cdefs.h"));
    if (!malloc_args && path_exists(c)) {
      malloc_args = read_file_ignore_errors(c).find("__attr_dealloc") !=
                    std::string::npos;
    }
  }
  const auto result = std::make_pair(floatn13, malloc_args);
  glibc_memo_.emplace(key, result);
  return result;
}

int Toolchain::libclang_major() const {
  if (major_override_) {
    return *major_override_; // test seam: set_libclang_major_for_test(0) covers
                             // the cap-when-undeterminable path (G4)
  }
  // Python _libclang_major(): 0 when undeterminable.
  // Under A1: loaded() is always true and major() never throws, so the guards
  // below are dead in production.  Kept for safety and seam completeness.
  try {
    LibClang &lib = LibClang::instance();
    if (!lib.loaded()) {
      return 0;
    }
    return lib.major();
  } catch (...) {
    return 0;
  }
}

// ---------------------------------------------------------------------------
// gnuc masquerade (util.py:233-266, G4)

std::vector<std::string> Toolchain::gnuc_flags(const std::string &driver,
                                               bool cpp) {
  const char *raw = std::getenv(kGnucEnv);
  if (env_flag_disabled_gnuc(raw)) {
    return {};
  }
  const std::string env = raw != nullptr ? strip(raw) : std::string();
  std::string ver;
  if (!env.empty()) {
    ver = env;
  } else {
    const std::optional<std::string> derived = gcc_version(driver);
    if (!derived) {
      return {}; // non-gcc driver, no override -> no flag
    }
    ver = *derived;
  }
  int major = parse_major_or_zero(ver);
  const std::pair<bool, bool> probe = glibc_probe(driver, cpp);
  const bool floatn13 = probe.first;
  const bool malloc_args = probe.second;
  // glibc >= 2.34 decorates allocators with malloc(deallocator) once the
  // compiler claims gcc >= 11; only libclang >= 21 parses that. Cap skipped
  // when the env var set the version explicitly.
  if (env.empty() && major >= 11 && malloc_args && libclang_major() < 21) {
    ver = "10.9";
    major = 10;
  }
  std::vector<std::string> flags = {"-fgnuc-version=" + ver};
  // _FloatN keywords: C (claimed >= 7) always; C++ only on glibc >= 2.38
  // (claimed >= 13) -- older glibc C++ typedefs them and the -D aliases
  // would mangle the typedefs.
  if ((major >= 7 && !cpp) || (major >= 13 && cpp && floatn13)) {
    flags.insert(flags.end(), kFloatnAliases.begin(), kFloatnAliases.end());
  }
  return flags;
}

// ---------------------------------------------------------------------------
// resource include (util.py:74-126)

std::optional<std::string>
Toolchain::pick_best_resource(const std::vector<std::string> &candidates) {
  std::optional<std::string> best;
  std::vector<long> best_key;
  for (const std::string &cand : candidates) {
    if (!resource_check(cand)) {
      continue;
    }
    const std::string ver = pathutil::basename(pathutil::dirname(cand));
    std::vector<long> key = version_key(ver);
    // max(found) over (key, inc) tuples -- path string breaks key ties.
    if (!best || key > best_key || (key == best_key && cand > *best)) {
      best = cand;
      best_key = std::move(key);
    }
  }
  return best;
}

std::optional<std::string> Toolchain::resource_include() {
  if (resource_memo_set_) {
    return resource_memo_;
  }
  resource_memo_set_ = true; // memoize even a miss (lru_cache parity)

  // 1. $CIDX_RESOURCE_DIR/include
  const std::optional<std::string> rd = get_env(kResourceEnv);
  if (rd && !rd->empty()) {
    const std::string inc =
        pathutil::join(pathutil::expanduser(*rd), std::string("include"));
    if (resource_check(inc)) {
      resource_memo_ = inc;
      return resource_memo_;
    }
  }

  // 2. lib/clang/<v>/include next to the linked libclang (a full LLVM install
  // ships both side by side).
  //
  // Under A1, library_path() returns the build-time absolute path baked in via
  // CIDX_LIBCLANG_PATH — its dirname is always a non-empty absolute directory.
  // The empty-dirname guard below is dead in production but remains for the
  // test-seam path (set_libclang_path_for_test can inject a bare name to
  // verify that step 2 does not run relative globs against cwd).
  const std::string lib = libclang_path_override_
                              ? *libclang_path_override_
                              : LibClang::instance().library_path();
  if (!lib.empty()) {
    const std::string libdir = pathutil::dirname(lib);
    if (!libdir.empty()) {
      std::vector<std::string> cands =
          glob_paths(pathutil::join(libdir, std::string("clang"),
                                    std::string("*"), std::string("include")));
      // sorted(..., reverse=True) -- reverse-LEXICOGRAPHIC, Python parity.
      std::sort(cands.begin(), cands.end(), std::greater<std::string>());
      for (const std::string &cand : cands) {
        if (resource_check(cand)) {
          resource_memo_ = cand;
          return resource_memo_;
        }
      }
    }
  }

  // 3. a PATH clang's -print-resource-dir (the pip wheel ships no headers).
  for (const char *cc : {"clang", "clang++"}) {
    const RunResult res = run({cc, "-print-resource-dir"}, 30.0);
    if (res.exit_code != 0 || res.timed_out) {
      continue;
    }
    const std::string inc =
        pathutil::join(strip(res.out), std::string("include"));
    if (resource_check(inc)) {
      resource_memo_ = inc;
      return resource_memo_;
    }
  }

  // 4. well-known LLVM install prefixes, best numeric version across all.
  std::vector<std::string> cands;
  for (const char *pattern :
       {"/opt/llvm*/lib*/clang/*/include",
        "/usr/lib/llvm-*/lib/clang/*/include",
        "/usr/local/llvm*/lib/clang/*/include", "/usr/lib*/clang/*/include"}) {
    std::vector<std::string> hits = glob_paths(pattern);
    cands.insert(cands.end(), hits.begin(), hits.end());
  }
  resource_memo_ = pick_best_resource(cands);
  return resource_memo_;
}

// ---------------------------------------------------------------------------
// host defaults + driver replication

std::optional<std::string> Toolchain::sysroot() {
  if (sysroot_memo_set_) {
    return sysroot_memo_;
  }
  sysroot_memo_set_ = true;
#ifdef __APPLE__
  const RunResult res = run({"xcrun", "--show-sdk-path"}, 30.0);
  if (res.exit_code == 0 && !res.timed_out) {
    sysroot_memo_ = strip(res.out);
  }
#endif
  return sysroot_memo_;
}

std::vector<std::string> Toolchain::driver_flags(const std::string &driver,
                                                 bool cpp) {
  const auto key = std::make_pair(driver, cpp);
  const auto it = driver_flags_memo_.find(key);
  const bool cached = it != driver_flags_memo_.end();
  if (cached && !it->second.warned_no_resource) {
    return it->second.flags;
  }
  // G7 warning: hoisted so memo-hit re-emit and first-computation share one
  // string; behavior is byte-identical — Python logs per call (not per probe).
  auto warn_no_resource = [&]() {
    log_.warning(kLogName, "no clang builtin headers found (set " +
                               std::string(kResourceEnv) +
                               " or install clang); falling back to " + driver +
                               "'s own builtin headers");
  };
  // Python logs the no-resource warning on EVERY driver_flags call; re-emit
  // it on memo hits so cidx.log and the warning counter stay byte-identical.
  if (cached) {
    warn_no_resource();
    return it->second.flags;
  }

  DriverFlagsEntry entry;
  const std::vector<std::string> dirs =
      driver_search_dirs(driver, cpp ? "c++" : "c");
  if (dirs.empty()) {
    driver_flags_memo_.emplace(key, entry);
    return entry.flags; // caller falls back to host defaults
  }
  const std::vector<std::string> gnuc = gnuc_flags(driver, cpp);
  const std::optional<std::string> res = resource_include();
  if (!res) {
    // G7: no clang builtin headers anywhere. Dropping the driver's builtin
    // dirs would make every <stddef.h> include fatal, so replicate the search
    // list verbatim instead (gcc's own stddef.h parses fine under libclang).
    warn_no_resource();
    entry.warned_no_resource = true;
    entry.flags.push_back("-nostdinc");
    entry.flags.insert(entry.flags.end(), gnuc.begin(), gnuc.end());
    for (const std::string &d : dirs) {
      entry.flags.push_back("-isystem");
      entry.flags.push_back(d);
    }
    driver_flags_memo_.emplace(key, entry);
    return entry.flags;
  }
  entry.flags.push_back("-nostdinc");
  entry.flags.insert(entry.flags.end(), gnuc.begin(), gnuc.end());
  bool substituted = false;
  for (const std::string &d : dirs) {
    if (std::regex_search(d, builtin_dir_re())) {
      if (!substituted) { // once, at the FIRST occurrence's position (G3)
        entry.flags.push_back("-isystem");
        entry.flags.push_back(*res);
        substituted = true;
      }
      continue;
    }
    entry.flags.push_back("-isystem");
    entry.flags.push_back(d);
  }
  if (!substituted) {
    entry.flags.push_back("-isystem");
    entry.flags.push_back(*res);
  }
  driver_flags_memo_.emplace(key, entry);
  return entry.flags;
}

std::vector<std::string>
Toolchain::toolchain_flags(bool cpp, const std::optional<std::string> &driver) {
  if (driver && !driver->empty()) {
    std::vector<std::string> flags = driver_flags(*driver, cpp);
    if (!flags.empty()) {
      return flags;
    }
  }
  // Host defaults -- order load-bearing (G2): sysroot -> libc++ -> clang
  // builtins; builtins first breaks <cstddef>'s include_next chain.
  std::vector<std::string> flags;
  const std::optional<std::string> sdk = sysroot();
  if (sdk && !sdk->empty()) {
    flags.push_back("-isysroot");
    flags.push_back(*sdk);
    if (cpp) {
      flags.push_back("-isystem");
      flags.push_back(pathutil::join(*sdk, std::string("usr"),
                                     std::string("include"), std::string("c++"),
                                     std::string("v1")));
    }
  }
  const std::optional<std::string> res = resource_include();
  if (res && !res->empty()) {
    flags.push_back("-isystem");
    flags.push_back(*res);
  }
  return flags;
}

// ---------------------------------------------------------------------------
// language detection (util.py:322-331, G9)

bool Toolchain::is_cpp(const std::string &filename,
                       const std::vector<std::string> &args) {
  const auto has = [&args](const char *token) {
    return std::find(args.begin(), args.end(), token) != args.end();
  };
  if (has("--driver-mode=g++") || has("-xc++")) {
    return true;
  }
  const auto x = std::find(args.begin(), args.end(), "-x");
  if (x != args.end()) {
    const auto value = std::next(x);
    if (value != args.end()) {
      // Python returns args[i+1].startswith("c++") directly: a found "-x c"
      // answers false WITHOUT falling through to the extension.
      return value->starts_with("c++");
    }
    // trailing "-x" -> IndexError -> extension check
  }
  // os.path.splitext: last '.' in the basename, leading dots excluded.
  std::string ext;
  const std::size_t sep = filename.rfind('/');
  const std::size_t dot = filename.rfind('.');
  if (dot != std::string::npos && (sep == std::string::npos || dot > sep)) {
    std::size_t fn = sep == std::string::npos ? 0 : sep + 1;
    while (fn < dot) {
      if (filename[fn] != '.') {
        ext = filename.substr(dot);
        break;
      }
      ++fn;
    }
  }
  std::transform(ext.begin(), ext.end(), ext.begin(), [](unsigned char c) {
    return static_cast<char>(std::tolower(c));
  });
  static const std::array<const char *, 7> kCppSuffixes = {
      ".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx"};
  return std::find(kCppSuffixes.begin(), kCppSuffixes.end(), ext) !=
         kCppSuffixes.end();
}

} // namespace cidx

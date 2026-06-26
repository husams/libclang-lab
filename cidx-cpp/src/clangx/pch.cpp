// pch.cpp -- shared system/C++ precompiled header. Behaviour port of
// project/indexer/pch.py (cited per function).
#include "clangx/pch.hpp"

#include <clang-c/Index.h>

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <sys/stat.h>
#include <thread>

#include "astcache/astcache.hpp"
#include "clangx/libclang.hpp"
#include "clangx/parse.hpp"
#include "util/env.hpp"
#include "util/errors.hpp"
#include "util/pathutil.hpp"
#include "util/subprocess.hpp"

namespace cidx {
namespace pch {
namespace {

constexpr const char *kNoPchEnv = "CIDX_NO_PCH";

// pch.py DEFAULT_HEADERS -- heavy STL headers nearly every TU pulls in.
const std::vector<std::string> kDefaultHeaders = {
    "algorithm",     "array",      "atomic",     "chrono",
    "cstddef",       "cstdint",    "cstdio",     "cstdlib",
    "cstring",       "deque",      "exception",  "functional",
    "iosfwd",        "iostream",   "iterator",   "limits",
    "list",          "map",        "memory",     "mutex",
    "numeric",       "optional",   "ostream",    "set",
    "sstream",       "stdexcept",  "string",     "thread",
    "tuple",         "type_traits","unordered_map","unordered_set",
    "utility",       "vector"};

// pch.py _TAKES_VALUE / _DROP_EXACT / _DROP_PREFIX.
const std::set<std::string> kTakesValue = {
    "-Xlinker", "-MT",     "-MF",         "-include", "-include-pch",
    "-x",       "-isystem","-iquote",     "-idirafter",
    // Separated flag+value pairs dropped WHOLE: the common-flag set is
    // order-insensitive, so a kept "-arch arm64" scrambles into two
    // disconnected tokens and a dangling "-arch" breaks the umbrella parse.
    "-arch",    "-target", "-Xclang",     "-mllvm"};
const std::set<std::string> kDropExact = {"-shared", "-static", "-rdynamic",
                                          "-pthread"};
const std::vector<std::string> kDropPrefix = {"-I",      "-L",       "-l",
                                              "-Wl,",    "-iquote",  "-isystem",
                                              "-idirafter"};

// --from-corpus survey: TU sources and non-standalone include fragments.
const std::set<std::string> kSrcExt = {".c",  ".cc", ".cpp", ".cxx",
                                       ".c++", ".cp", ".m",  ".mm"};
const std::set<std::string> kFragmentExt = {".def", ".inc", ".td",
                                            ".gen", ".x",   ".inl"};
const std::vector<std::string> kIncludeDirFlags = {"-I", "-isystem", "-iquote",
                                                   "-idirafter"};

// Lower-cased file extension of `path` (including the dot), or "".
std::string lower_ext(const std::string &path) {
  const std::string base = pathutil::basename(path);
  const std::size_t dot = base.rfind('.');
  if (dot == std::string::npos || dot == 0) {
    return "";
  }
  std::string ext = base.substr(dot);
  std::transform(ext.begin(), ext.end(), ext.begin(),
                 [](unsigned char c) { return std::tolower(c); });
  return ext;
}

bool path_exists(const std::string &p) {
  struct stat st {};
  return ::stat(p.c_str(), &st) == 0;
}

// `<driver> <opts> -E -H -o /dev/null <path>` -> (direct, transitive) header
// sets. -H prints the inclusion tree to stderr, one line per header, leading
// dots = depth; depth 1 is a direct #include (a safe umbrella entry point).
// Returns ({}, {}) on any failure -- an unscannable TU contributes nothing.
std::pair<std::set<std::string>, std::set<std::string>>
scan_one(const std::optional<std::string> &driver,
         const std::vector<std::string> &opts, const std::string &path) {
  std::vector<std::string> argv;
  argv.push_back(driver ? *driver : "c++");
  for (const std::string &o : opts) {
    argv.push_back(o);
  }
  argv.emplace_back("-E");
  argv.emplace_back("-H");
  argv.emplace_back("-o");
  argv.emplace_back("/dev/null");
  argv.push_back(path);

  std::set<std::string> direct;
  std::set<std::string> trans;
  const RunResult res = run(argv, 180.0);
  if (res.timed_out) {
    return {direct, trans};
  }
  std::istringstream iss(res.err);
  std::string line;
  while (std::getline(iss, line)) {
    std::size_t depth = 0;
    while (depth < line.size() && line[depth] == '.') {
      ++depth;
    }
    if (depth == 0 || depth >= line.size() || line[depth] != ' ') {
      continue;
    }
    std::string hdr = line.substr(depth + 1);
    // trim trailing whitespace / CR.
    while (!hdr.empty() &&
           std::isspace(static_cast<unsigned char>(hdr.back()))) {
      hdr.pop_back();
    }
    if (hdr.empty() || hdr.find('/') == std::string::npos) {
      continue;
    }
    const std::string ext = lower_ext(hdr);
    if (kSrcExt.count(ext) != 0 || kFragmentExt.count(ext) != 0) {
      continue;
    }
    hdr = pathutil::normpath(hdr);
    if (!path_exists(hdr)) { // skip phantom / generated headers
      continue;
    }
    trans.insert(hdr);
    if (depth == 1) {
      direct.insert(hdr);
    }
  }
  return {direct, trans};
}

bool truthy_env(const char *name) {
  std::string v = get_env(name).value_or("");
  std::string out;
  for (char c : v) {
    if (!std::isspace(static_cast<unsigned char>(c))) {
      out += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
  }
  return !(out.empty() || out == "0" || out == "off" || out == "none" ||
           out == "false");
}

bool file_exists(const std::string &p) {
  struct stat st {};
  return ::stat(p.c_str(), &st) == 0;
}

// Minimal JSON string-field reader: the value of "key" if it is a JSON string,
// else nullopt (absent, null, or non-string). Sufficient for our own sidecar,
// whose strings carry no control characters (clang version + driver).
std::optional<std::string> json_string_field(const std::string &text,
                                              const std::string &key) {
  const std::string needle = "\"" + key + "\"";
  std::size_t pos = text.find(needle);
  if (pos == std::string::npos) {
    return std::nullopt;
  }
  pos = text.find(':', pos + needle.size());
  if (pos == std::string::npos) {
    return std::nullopt;
  }
  ++pos;
  while (pos < text.size() &&
         std::isspace(static_cast<unsigned char>(text[pos]))) {
    ++pos;
  }
  if (pos >= text.size() || text[pos] != '"') {
    return std::nullopt; // null / number / absent
  }
  ++pos;
  std::string out;
  while (pos < text.size() && text[pos] != '"') {
    if (text[pos] == '\\' && pos + 1 < text.size()) {
      ++pos;
    }
    out += text[pos];
    ++pos;
  }
  return out;
}

std::string json_escape(const std::string &s) {
  std::string r;
  r.reserve(s.size());
  for (char c : s) {
    if (c == '"' || c == '\\') {
      r += '\\';
    }
    r += c;
  }
  return r;
}

std::string json_array(const std::vector<std::string> &items) {
  std::string r = "[";
  for (std::size_t i = 0; i < items.size(); ++i) {
    r += (i == 0 ? "\n    \"" : ",\n    \"");
    r += json_escape(items[i]);
    r += "\"";
  }
  r += items.empty() ? "]" : "\n  ]";
  return r;
}

std::string iso_now() {
  std::time_t t = std::time(nullptr);
  std::tm tmv {};
#if defined(_WIN32)
  localtime_s(&tmv, &t);
#else
  localtime_r(&t, &tmv);
#endif
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &tmv);
  return std::string(buf);
}

} // namespace

// --- paths -------------------------------------------------------------------

std::string pch_path() { return astcache::files_dir() + "/system.pch"; }
std::string sidecar_path() { return astcache::files_dir() + "/system.pch.json"; }
std::string umbrella_path() {
  return astcache::files_dir() + "/system_umbrella.hpp";
}

const std::vector<std::string> &default_headers() { return kDefaultHeaders; }

// --- flag selection (pch.py pch_relevant) ------------------------------------

std::vector<std::string> pch_relevant(const std::vector<std::string> &options) {
  std::vector<std::string> keep;
  bool skip_next = false;
  for (const std::string &a : options) {
    if (skip_next) {
      skip_next = false;
      continue;
    }
    if (kDropExact.count(a) != 0) {
      continue;
    }
    if (kTakesValue.count(a) != 0) {
      skip_next = true;
      continue;
    }
    bool dropped = false;
    for (const std::string &pfx : kDropPrefix) {
      if (a.rfind(pfx, 0) == 0) {
        dropped = true;
        break;
      }
    }
    if (dropped) {
      continue;
    }
    keep.push_back(a);
  }
  return keep;
}

// --- corpus header survey (cidx pch build --from-corpus) ---------------------

std::vector<std::pair<std::string, std::string>>
include_dirs(const std::vector<std::string> &options) {
  std::vector<std::pair<std::string, std::string>> pairs;
  std::string take;
  bool taking = false;
  for (const std::string &a : options) {
    if (taking) {
      pairs.emplace_back(take, a);
      taking = false;
      continue;
    }
    if (std::find(kIncludeDirFlags.begin(), kIncludeDirFlags.end(), a) !=
        kIncludeDirFlags.end()) {
      take = a;
      taking = true;
      continue;
    }
    for (const std::string &f : kIncludeDirFlags) {
      if (a.size() > f.size() && a.rfind(f, 0) == 0) {
        pairs.emplace_back(f, a.substr(f.size()));
        break;
      }
    }
  }
  return pairs;
}

HeaderSurvey survey_headers(const std::vector<TuFlags> &tus, int jobs) {
  HeaderSurvey survey;
  if (tus.empty()) {
    return survey;
  }
  unsigned hw = std::thread::hardware_concurrency();
  unsigned nthreads = jobs > 0 ? static_cast<unsigned>(jobs)
                               : (hw != 0U ? hw : 4U);
  nthreads = std::max(1U, std::min(nthreads, static_cast<unsigned>(tus.size())));

  std::atomic<std::size_t> next{0};
  std::mutex merge_mu;
  auto worker = [&]() {
    for (;;) {
      const std::size_t i = next.fetch_add(1);
      if (i >= tus.size()) {
        return;
      }
      const TuFlags &t = tus[i];
      std::pair<std::set<std::string>, std::set<std::string>> r =
          scan_one(t.driver, t.options, t.path);
      std::lock_guard<std::mutex> lk(merge_mu);
      for (const std::string &h : r.second) {
        ++survey.freq[h];
      }
      survey.directable.insert(r.first.begin(), r.first.end());
    }
  };

  std::vector<std::thread> pool;
  pool.reserve(nthreads);
  for (unsigned i = 0; i < nthreads; ++i) {
    pool.emplace_back(worker);
  }
  for (std::thread &th : pool) {
    th.join();
  }
  return survey;
}

std::vector<std::string> select_shared_headers(const HeaderSurvey &survey,
                                               int n_cpp, double coverage,
                                               int min_tus) {
  const double threshold =
      std::max(coverage * static_cast<double>(n_cpp),
               static_cast<double>(min_tus));
  // Rank by frequency desc, then path asc for a stable order.
  std::vector<std::pair<std::string, int>> ranked(survey.freq.begin(),
                                                  survey.freq.end());
  std::sort(ranked.begin(), ranked.end(),
            [](const std::pair<std::string, int> &a,
               const std::pair<std::string, int> &b) {
              if (a.second != b.second) {
                return a.second > b.second;
              }
              return a.first < b.first;
            });
  std::vector<std::string> out;
  for (const std::pair<std::string, int> &p : ranked) {
    if (static_cast<double>(p.second) >= threshold &&
        survey.directable.count(p.first) != 0) {
      out.push_back(p.first);
    }
  }
  return out;
}

// --- consumption gate (pch.py consume_args) ----------------------------------

std::vector<std::string>
consume_args(bool cpp, const std::optional<std::string> &driver) {
  if (!cpp || truthy_env(kNoPchEnv)) {
    return {};
  }
  const std::string pp = pch_path();
  if (!file_exists(pp)) {
    return {};
  }
  std::ifstream f(sidecar_path());
  if (!f.is_open()) {
    return {};
  }
  std::ostringstream ss;
  ss << f.rdbuf();
  const std::string side = ss.str();
  const std::optional<std::string> sv =
      json_string_field(side, "libclang_version");
  if (!sv || *sv != astcache::libclang_version()) {
    return {};
  }
  // sidecar driver: a JSON string -> that driver; null/absent -> nullopt.
  const std::optional<std::string> sd = json_string_field(side, "driver");
  if (sd != driver) { // both nullopt also matches (None==None parity)
    return {};
  }
  return {"-include-pch", pp};
}

// --- build / status / clear --------------------------------------------------

int build_pch(Parser &parser, const std::vector<std::string> &flags,
              const std::vector<std::string> &headers,
              const std::optional<std::string> &driver, int n_cpp_tus,
              std::ostream &out, std::ostream &err, bool quoted, bool corpus,
              double coverage) {
  LibClang &lib = LibClang::instance();
  lib.load();

  // Write the umbrella header.
  ::mkdir(astcache::cache_dir().c_str(), 0755);
  ::mkdir(astcache::files_dir().c_str(), 0755);
  {
    std::ofstream uf(umbrella_path());
    if (!uf.is_open()) {
      err << "error: cannot write umbrella header: " << umbrella_path() << "\n";
      return 1;
    }
    uf << "// Generated by `cidx pch build` -- shared system/C++ precompiled "
          "header.\n"
       << "// Edit via `cidx pch build --include <header>`; do not hand-edit.\n";
    // Absolute corpus headers are #include "..."-quoted (resolve without -I);
    // bare system/STL names are #include <...>-angled.
    for (const std::string &h : headers) {
      if (quoted) {
        uf << "#include \"" << h << "\"\n";
      } else {
        uf << "#include <" << h << ">\n";
      }
    }
  }

  // Assemble the umbrella's flags via final_args (args + toolchain + ferror),
  // adding -x c++-header. No PCH injection happens here (we drive libclang
  // directly, not Parser::parse), so a stale PCH never pollutes the build.
  std::vector<std::string> umbrella_args = flags;
  umbrella_args.emplace_back("-x");
  umbrella_args.emplace_back("c++-header");
  const std::vector<std::string> final =
      parser.final_args(umbrella_path(), umbrella_args, driver);
  std::vector<const char *> argv;
  argv.reserve(final.size());
  for (const std::string &a : final) {
    argv.push_back(a.c_str());
  }

  CXIndex index = lib.clang_createIndex(0, 0);
  CXTranslationUnit tu = nullptr;
  const CXErrorCode rc = lib.clang_parseTranslationUnit2(
      index, umbrella_path().c_str(), argv.data(),
      static_cast<int>(argv.size()), nullptr, 0,
      CXTranslationUnit_Incomplete, &tu);
  if (rc != CXError_Success || tu == nullptr) {
    if (tu != nullptr) {
      lib.clang_disposeTranslationUnit(tu);
    }
    lib.clang_disposeIndex(index);
    err << "error: failed to parse the umbrella header\n";
    return 1;
  }

  const int save_rc =
      lib.clang_saveTranslationUnit(tu, pch_path().c_str(), 0);
  lib.clang_disposeTranslationUnit(tu);
  lib.clang_disposeIndex(index);
  if (save_rc != CXSaveError_None) {
    err << "error: failed to save the PCH (code " << save_rc << ")\n";
    return 1;
  }

  // Sidecar (valid JSON; cross-readable with the Python tool).
  {
    std::ofstream sf(sidecar_path());
    if (!sf.is_open()) {
      err << "error: cannot write the PCH sidecar\n";
      return 1;
    }
    sf << "{\n"
       << "  \"libclang_version\": \""
       << json_escape(astcache::libclang_version()) << "\",\n"
       << "  \"driver\": "
       << (driver ? "\"" + json_escape(*driver) + "\"" : "null") << ",\n"
       << "  \"flags\": " << json_array(flags) << ",\n"
       << "  \"headers\": " << json_array(headers) << ",\n"
       << "  \"n_cpp_tus\": " << n_cpp_tus << ",\n"
       << "  \"built_at\": \"" << iso_now() << "\",\n"
       << "  \"cpp\": true,\n"
       << "  \"mode\": \"" << (corpus ? "corpus" : "system") << "\"";
    if (corpus) {
      sf << ",\n  \"coverage\": " << coverage;
    }
    sf << "\n}";
  }

  struct stat st {};
  const long size = ::stat(pch_path().c_str(), &st) == 0
                        ? static_cast<long>(st.st_size)
                        : 0;
  std::string flagstr;
  for (std::size_t i = 0; i < flags.size(); ++i) {
    flagstr += (i ? " " : "") + flags[i];
  }
  out << "built system PCH: " << pch_path() << "  (" << size << " bytes)\n"
      << "  C++ TUs in index : " << n_cpp_tus << "\n"
      << "  driver           : " << (driver ? *driver : "(host default)")
      << "\n"
      << "  flags            : " << (flagstr.empty() ? "(none)" : flagstr)
      << "\n"
      << "  headers          : " << headers.size() << " (umbrella: "
      << umbrella_path() << ")\n"
      << "  injected as `-include-pch` into every matching C++ parse.\n";
  return 0;
}

int status_pch(std::ostream &out) {
  if (!file_exists(pch_path())) {
    out << "no system PCH built (run `cidx pch build`)\n";
    return 0;
  }
  struct stat st {};
  const long size = ::stat(pch_path().c_str(), &st) == 0
                        ? static_cast<long>(st.st_size)
                        : 0;
  out << "system PCH : " << pch_path() << "  (" << size << " bytes)\n";
  std::ifstream f(sidecar_path());
  if (!f.is_open()) {
    out << "sidecar    : MISSING/unreadable -- PCH will NOT be injected\n";
    return 0;
  }
  std::ostringstream ss;
  ss << f.rdbuf();
  const std::string side = ss.str();
  const std::optional<std::string> ver =
      json_string_field(side, "libclang_version");
  const std::optional<std::string> drv = json_string_field(side, "driver");
  const std::optional<std::string> built = json_string_field(side, "built_at");
  const bool ver_ok = ver && *ver == astcache::libclang_version();
  out << "built at   : " << (built ? *built : "?") << "\n"
      << "driver     : " << (drv ? *drv : "(host default)") << "\n"
      << "libclang   : " << (ver ? *ver : "?") << "\n"
      << "validity   : "
      << (ver_ok ? "OK -- injected into matching C++ parses"
                 : "STALE (libclang version changed) -- rebuild")
      << "\n";
  return 0;
}

int clear_pch(std::ostream &out) {
  int removed = 0;
  for (const std::string &p : {pch_path(), sidecar_path(), umbrella_path()}) {
    if (std::remove(p.c_str()) == 0) {
      ++removed;
    }
  }
  if (removed != 0) {
    out << "removed " << removed << " file(s)\n";
  } else {
    out << "no system PCH to clear\n";
  }
  return 0;
}

} // namespace pch
} // namespace cidx

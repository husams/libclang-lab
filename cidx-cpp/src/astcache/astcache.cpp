// astcache.cpp — on-disk AST cache implementation (ADR-005, M5).
// Byte-parity port of project/indexer/astcache.py.
// CLI subcommand handlers (cmd_ast_cache_*) live in commands.cpp to avoid a
// circular dependency on cli/args.hpp.
#include "astcache/astcache.hpp"

#include <sys/stat.h>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <ostream>
#include <sstream>

#include "clangx/libclang.hpp"
#include "clangx/parse.hpp"
#include "clangx/toolchain.hpp"
#include "util/env.hpp"
#include "util/hashing.hpp"
#include "util/pathutil.hpp"

namespace cidx {
namespace astcache {

namespace {

// Parse a minimal sidecar JSON (4 known fields only).
std::optional<Sidecar> parse_sidecar_json(const std::string &text) {
  auto get_str = [&](const std::string &key) -> std::optional<std::string> {
    const std::string needle = "\"" + key + "\": \"";
    auto pos = text.find(needle);
    if (pos == std::string::npos) {
      return std::nullopt;
    }
    pos += needle.size();
    auto end = text.find('"', pos);
    if (end == std::string::npos) {
      return std::nullopt;
    }
    return text.substr(pos, end - pos);
  };
  auto get_num = [&](const std::string &key) -> std::optional<double> {
    const std::string needle = "\"" + key + "\": ";
    auto pos = text.find(needle);
    if (pos == std::string::npos) {
      return std::nullopt;
    }
    pos += needle.size();
    std::string num;
    while (pos < text.size() &&
           (std::isdigit(static_cast<unsigned char>(text[pos])) ||
            text[pos] == '.' || text[pos] == '-' || text[pos] == 'e' ||
            text[pos] == 'E' || text[pos] == '+')) {
      num += text[pos++];
    }
    if (num.empty()) {
      return std::nullopt;
    }
    try {
      return std::stod(num);
    } catch (...) {
      return std::nullopt;
    }
  };

  Sidecar s;
  auto absp = get_str("abspath");
  auto fh = get_str("flags_hash");
  auto lv = get_str("libclang_version");
  auto mt = get_num("src_mtime");
  if (!absp || !fh || !lv || !mt) {
    return std::nullopt;
  }
  s.abspath = *absp;
  s.flags_hash = *fh;
  s.src_mtime = *mt;
  s.libclang_version = *lv;
  return s;
}

} // namespace

// --- directory helpers -------------------------------------------------------

std::string cache_dir() {
  auto env = cidx::get_env("INDEXER_CACHE");
  const std::string raw =
      (env && !env->empty()) ? *env : std::string("~/.cache/cidx");
  return pathutil::expanduser(raw);
}

std::string files_dir() { return pathutil::join(cache_dir(), "files"); }

// --- hashing -----------------------------------------------------------------

std::string flags_hash(const AstTarget &t) {
  AstCacheKey k{std::string(), t.flags, t.driver};
  return sha1_flags_hash(k);
}

std::string cache_key(const AstTarget &t) {
  AstCacheKey k{t.abspath, t.flags, t.driver};
  return sha1_cache_key(k);
}

// --- libclang version --------------------------------------------------------

const std::string &libclang_version() {
  static std::string ver;
  static bool loaded = false;
  if (!loaded) {
    LibClang &lib = LibClang::instance();
    CxString cs(lib, lib.clang_getClangVersion());
    ver = cs.str();
    loaded = true;
  }
  return ver;
}

// --- parse counter -----------------------------------------------------------

static int g_parse_count = 0;

int parse_count() { return g_parse_count; }
void reset_parse_count() { g_parse_count = 0; }

// --- sidecar -----------------------------------------------------------------

std::optional<Sidecar> read_sidecar(const std::string &path) {
  std::ifstream f(path);
  if (!f.is_open()) {
    return std::nullopt;
  }
  std::ostringstream ss;
  ss << f.rdbuf();
  if (!f) {
    return std::nullopt;
  }
  return parse_sidecar_json(ss.str());
}

bool write_sidecar(const std::string &path, const AstTarget &t,
                   double src_mtime) {
  // Mirror Python json.dump: insertion-ordered fields, %.17g for mtime.
  std::ofstream f(path);
  if (!f.is_open()) {
    return false;
  }
  char mtime_buf[64];
  std::snprintf(mtime_buf, sizeof(mtime_buf), "%.17g", src_mtime);

  auto escape = [](const std::string &s) {
    std::string r;
    r.reserve(s.size());
    for (char c : s) {
      if (c == '"' || c == '\\') {
        r += '\\';
      }
      r += c;
    }
    return r;
  };

  f << "{\n"
    << "  \"abspath\": \"" << escape(t.abspath) << "\",\n"
    << "  \"flags_hash\": \"" << flags_hash(t) << "\",\n"
    << "  \"src_mtime\": " << mtime_buf << ",\n"
    << "  \"libclang_version\": \"" << escape(libclang_version()) << "\"\n"
    << "}";
  return f.good();
}

// --- validity ----------------------------------------------------------------

double src_mtime_of(const struct stat &st) {
  double mtime = static_cast<double>(st.st_mtime);
#ifdef __APPLE__
  mtime = static_cast<double>(st.st_mtimespec.tv_sec) +
          static_cast<double>(st.st_mtimespec.tv_nsec) * 1e-9;
#elif defined(__linux__)
  mtime = static_cast<double>(st.st_mtim.tv_sec) +
          static_cast<double>(st.st_mtim.tv_nsec) * 1e-9;
#endif
  return mtime;
}

bool is_valid(const AstTarget &t, const Sidecar &side) {
  // 1. Source file still accessible.
  struct stat st{};
  if (::stat(t.abspath.c_str(), &st) != 0) {
    return false;
  }
  // 2. flags_hash matches (one-way hash covers flags + driver).
  if (side.flags_hash != flags_hash(t)) {
    return false;
  }
  // 3. src_mtime: compare as doubles (ADR-006 §6.1).
  if (side.src_mtime != src_mtime_of(st)) {
    return false;
  }
  // 4. libclang version string matches exactly.
  if (side.libclang_version != libclang_version()) {
    return false;
  }
  // 5. abspath sanity.
  if (side.abspath != t.abspath) {
    return false;
  }
  return true;
}

// --- low-level TU helpers ----------------------------------------------------

std::optional<ParsedTu> load_ast(const std::string &path) {
  LibClang &lib = LibClang::instance();
  CXIndex idx = lib.clang_createIndex(0, 0);
  if (idx == nullptr) {
    return std::nullopt;
  }
  CXTranslationUnit tu = lib.clang_createTranslationUnit(idx, path.c_str());
  if (tu == nullptr) {
    lib.clang_disposeIndex(idx);
    return std::nullopt;
  }
  ParsedTu pt;
  pt.index = idx;
  pt.tu = tu;
  pt.spelling = path;
  return pt;
}

std::optional<ParsedTu> reparse(const AstTarget &t, std::ostream *err) {
  ++g_parse_count;
  try {
    Toolchain tc;  // per-reparse instance (single-threaded; memoized inside)
    Parser parser(tc);
    return parser.parse(t.abspath, t.flags, t.driver);
  } catch (const std::exception &e) {
    if (err) {
      *err << "error: " << e.what() << "\n";
    }
    return std::nullopt;
  }
}

void try_save(CXTranslationUnit tu, const std::string &ast_path,
              const std::string &side_path, const AstTarget &t) {
  LibClang &lib = LibClang::instance();
  const int save_rc =
      lib.clang_saveTranslationUnit(tu, ast_path.c_str(), /*options=*/0);
  if (save_rc != 0) {
    std::remove(ast_path.c_str());
    return;
  }
  struct stat st{};
  if (::stat(t.abspath.c_str(), &st) != 0) {
    std::remove(ast_path.c_str());
    return;
  }
  if (!write_sidecar(side_path, t, src_mtime_of(st))) {
    // Remove .ast so there is no dangling .ast without a sidecar.
    std::remove(ast_path.c_str());
  }
}

// --- main entry point --------------------------------------------------------

std::optional<ParsedTu> load_or_parse(const AstTarget &t, bool use_cache,
                                      std::ostream *err) {
  namespace fs = std::filesystem;
  const std::string fd = files_dir();
  std::error_code ec;
  fs::create_directories(fd, ec); // best-effort mkdir -p

  const std::string key = cache_key(t);
  const std::string ast_path = pathutil::join(fd, key + ".ast");
  const std::string side_path = pathutil::join(fd, key + ".json");

  if (use_cache) {
    auto side = read_sidecar(side_path);
    if (side && is_valid(t, *side)) {
      struct stat st{};
      if (::stat(ast_path.c_str(), &st) == 0) {
        auto tu_opt = load_ast(ast_path);
        if (tu_opt) {
          return tu_opt;
        }
        // Corrupt or version-skew .ast: fall through and reparse.
      }
    }
  }

  auto tu = reparse(t, err);
  if (!tu) {
    return std::nullopt;
  }
  if (use_cache) {
    try_save(tu->tu, ast_path, side_path, t);
  }
  return tu;
}

} // namespace astcache
} // namespace cidx

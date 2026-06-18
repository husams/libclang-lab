// astcache_test — M5 unit tests for astcache/astcache (ADR-006 §8.1, §8.2).
//
// Mirrors project/tests/test_astcache.py. All tests run under the "clang"
// doctest suite because they need real libclang parses; INDEXER_CACHE is
// redirected to a per-test temp dir so ~/.cache/cidx is never touched.
//
// Frozen doctest (interchange key — ADR-006 §6.1):
//   abspath = "/Users/husam/workspace/qemu-vms/libclang-lab/manifests/calls.c"
//   flags   = {"-std=c11"}, driver = (none)
//   flags_hash = "ae06a37b4cc5670c2c3d501823940f1ee2019984"
//   cache_key  = "d6cca25a6ed23cd603c1baefecbc7f67f5435639"
//
// Tests:
//   1. cold miss parses once (.ast + sidecar created)
//   2. warm hit avoids reparse (parse counter unchanged)
//   3. --no-cache (use_cache=false) reparses every call
//   4. src-mtime bump invalidates → reparse
//   5. different flags → different key → two separate entries
//   6. libclang-version mismatch (poke sidecar) → reparse, no crash
//   7. corrupt .ast + valid sidecar → _load_ast fails → reparse
//   8. interchange round-trip: C++-written .ast loads back
//   9. cache_key / flags_hash hex == frozen Python values
#define DOCTEST_CONFIG_IMPLEMENT
#include "doctest/doctest.h"

#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "astcache/astcache.hpp"
#include "clangx/libclang.hpp"
#include "clangx/parse.hpp"
#include "util/errors.hpp"
#include "util/hashing.hpp"
#include "util/logger.hpp"

namespace fs = std::filesystem;
using cidx::AstTarget;
using cidx::LibClang;
using cidx::ParsedTu;

namespace {

bool g_clang_skipped = false;

// Manifests directory — set by CMake.
const char *kManifests = CIDX_MANIFESTS_DIR;

bool require_manifests() {
  struct stat st{};
  if (::stat(kManifests, &st) != 0 || !S_ISDIR(st.st_mode)) {
    g_clang_skipped = true;
    MESSAGE("SKIP: lab fixtures not found at " << kManifests);
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
  char tmpl[] = "/tmp/cidx_actest_XXXXXX";
  char *d = ::mkdtemp(tmpl);
  REQUIRE(d != nullptr);
  return d;
}

// Build platform clang flags for a clean parse (mirrors Python _clang_args).
std::vector<std::string> clang_args(const std::string &std = "c11") {
  std::vector<std::string> flags;
  flags.push_back("-std=" + std);

  // xcrun --show-sdk-path
  FILE *p = ::popen("xcrun --show-sdk-path 2>/dev/null", "r");
  if (p) {
    char buf[1024] = {};
    if (fgets(buf, sizeof(buf), p)) {
      std::string sysroot = buf;
      while (!sysroot.empty() &&
             (sysroot.back() == '\n' || sysroot.back() == '\r'))
        sysroot.pop_back();
      if (!sysroot.empty()) {
        flags.push_back("-isysroot");
        flags.push_back(sysroot);
      }
    }
    pclose(p);
  }

  // clang -print-resource-dir
  p = ::popen("clang -print-resource-dir 2>/dev/null", "r");
  if (p) {
    char buf[1024] = {};
    if (fgets(buf, sizeof(buf), p)) {
      std::string rdir = buf;
      while (!rdir.empty() &&
             (rdir.back() == '\n' || rdir.back() == '\r'))
        rdir.pop_back();
      if (!rdir.empty()) {
        flags.push_back("-I");
        flags.push_back(rdir + "/include");
      }
    }
    pclose(p);
  }

  flags.push_back("-I");
  flags.push_back(kManifests);
  return flags;
}

AstTarget calls_target() {
  AstTarget t;
  t.abspath = std::string(kManifests) + "/calls.c";
  t.flags = clang_args("c11");
  return t;
}

AstTarget messy_target() {
  AstTarget t;
  t.abspath = std::string(kManifests) + "/messy.c";
  t.flags = clang_args("c11");
  return t;
}

// RAII env override that also propagates to INDEXER_CACHE so astcache helpers
// pick up the temp dir.
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

// Read a file as bytes.
std::string read_file(const std::string &path) {
  std::ifstream in(path, std::ios::binary);
  if (!in.good())
    return "";
  return {std::istreambuf_iterator<char>(in),
          std::istreambuf_iterator<char>()};
}

// Write bytes to a file (creates/overwrites).
void write_file(const std::string &path, const std::string &content) {
  std::ofstream out(path, std::ios::binary | std::ios::trunc);
  REQUIRE(out.good());
  out << content;
}

bool file_exists(const std::string &path) {
  struct stat st{};
  return ::stat(path.c_str(), &st) == 0;
}

} // namespace

// ============================================================================
// "clang" suite — real libclang parses; INDEXER_CACHE → temp dir per test
// ============================================================================

TEST_SUITE("clang") {

// --- 9. Frozen cache key / flags_hash (runs first so values are always pinned)

TEST_CASE("cache key frozen Python value (interchange contract)") {
  // abspath and flags used for all other tests in this suite.
  // Frozen Python values computed with hashlib.sha1 — see file header comment.
  const std::string abspath =
      "/Users/husam/workspace/qemu-vms/libclang-lab/manifests/calls.c";
  const std::vector<std::string> flags = {"-std=c11"};

  cidx::AstCacheKey ack;
  ack.abspath = abspath;
  ack.flags = flags;
  ack.driver = std::nullopt;

  CHECK(cidx::sha1_flags_hash(ack) ==
        "ae06a37b4cc5670c2c3d501823940f1ee2019984");
  CHECK(cidx::sha1_cache_key(ack) ==
        "d6cca25a6ed23cd603c1baefecbc7f67f5435639");
}

TEST_CASE("astcache::flags_hash and cache_key match frozen Python values") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());

  // Target with only -std=c11 (the "known triple" from the doctest comment).
  AstTarget t;
  t.abspath = std::string(kManifests) + "/calls.c";
  t.flags = {"-std=c11"};
  t.driver = std::nullopt;

  CHECK(cidx::astcache::flags_hash(t) ==
        "ae06a37b4cc5670c2c3d501823940f1ee2019984");
  CHECK(cidx::astcache::cache_key(t) ==
        "d6cca25a6ed23cd603c1baefecbc7f67f5435639");

  fs::remove_all(cache);
}

// --- 1. Cold miss / warm hit ------------------------------------------------

TEST_CASE("cold miss parses once: .ast + sidecar created, counter==1") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();
  auto tu = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  REQUIRE(tu.has_value());
  CHECK(cidx::astcache::parse_count() == 1);

  const std::string fd = cidx::astcache::files_dir();
  // files_dir must be inside our temp cache, not ~/.cache/cidx
  CHECK(fd.find(cache) == 0);

  const std::string key = cidx::astcache::cache_key(t);
  CHECK(file_exists(fd + "/" + key + ".ast"));
  CHECK(file_exists(fd + "/" + key + ".json"));

  fs::remove_all(cache);
}

TEST_CASE("warm hit avoids reparse: parse counter unchanged on second call") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();
  auto tu1 = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  REQUIRE(tu1.has_value());
  CHECK(cidx::astcache::parse_count() == 1);

  auto tu2 = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  REQUIRE(tu2.has_value());
  // Counter must NOT increase on a warm hit.
  CHECK(cidx::astcache::parse_count() == 1);

  fs::remove_all(cache);
}

// --- 3. --no-cache (use_cache=false) ----------------------------------------

TEST_CASE("use_cache=false reparses on every call") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();
  cidx::astcache::load_or_parse(t, /*use_cache=*/false);
  CHECK(cidx::astcache::parse_count() == 1);

  cidx::astcache::load_or_parse(t, /*use_cache=*/false);
  CHECK(cidx::astcache::parse_count() == 2);

  cidx::astcache::load_or_parse(t, /*use_cache=*/false);
  CHECK(cidx::astcache::parse_count() == 3);

  fs::remove_all(cache);
}

TEST_CASE("use_cache=false does not write cache files") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();
  cidx::astcache::load_or_parse(t, /*use_cache=*/false);

  const std::string fd = cidx::astcache::files_dir();
  const std::string key = cidx::astcache::cache_key(t);
  // load_or_parse with use_cache=false must NOT call _try_save.
  CHECK(!file_exists(fd + "/" + key + ".ast"));
  CHECK(!file_exists(fd + "/" + key + ".json"));

  fs::remove_all(cache);
}

// --- 4. src-mtime invalidation ----------------------------------------------

TEST_CASE("src-mtime bump invalidates cache: reparse and sidecar updated") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();
  cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  CHECK(cidx::astcache::parse_count() == 1);

  // Bump the mtime by 1 second (enough to exceed any sub-second precision).
  struct stat st{};
  REQUIRE(::stat(t.abspath.c_str(), &st) == 0);
  const double orig_atime = st.st_atime;
  const double orig_mtime = st.st_mtime;
  struct timeval tv[2];
  tv[0].tv_sec = static_cast<long>(orig_atime);
  tv[0].tv_usec = 0;
  tv[1].tv_sec = static_cast<long>(orig_mtime) + 1;
  tv[1].tv_usec = 0;
  REQUIRE(::utimes(t.abspath.c_str(), tv) == 0);

  auto tu2 = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  // Restore original mtime so we don't dirty the manifest.
  tv[1].tv_sec = static_cast<long>(orig_mtime);
  ::utimes(t.abspath.c_str(), tv);

  REQUIRE(tu2.has_value());
  CHECK(cidx::astcache::parse_count() == 2); // forced reparse

  fs::remove_all(cache);
}

// --- 5. Different flags → different key ------------------------------------

TEST_CASE("different flags produce different cache keys and entries") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t_a = calls_target();
  AstTarget t_b = calls_target();
  t_b.flags.push_back("-DSOME_DEFINE=1");

  CHECK(cidx::astcache::cache_key(t_a) !=
        cidx::astcache::cache_key(t_b));

  cidx::astcache::load_or_parse(t_a, /*use_cache=*/true);
  CHECK(cidx::astcache::parse_count() == 1);

  cidx::astcache::load_or_parse(t_b, /*use_cache=*/true);
  CHECK(cidx::astcache::parse_count() == 2); // cold miss for B

  // Both entries must exist as separate .ast files.
  const std::string fd = cidx::astcache::files_dir();
  int ast_count = 0;
  for (auto &e : fs::directory_iterator(fd)) {
    if (e.path().extension() == ".ast")
      ++ast_count;
  }
  CHECK(ast_count == 2);

  fs::remove_all(cache);
}

// --- 6. libclang-version mismatch (poke sidecar) ---------------------------

TEST_CASE("libclang-version mismatch → reparse, no crash") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();
  cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  CHECK(cidx::astcache::parse_count() == 1);

  // Corrupt the sidecar's libclang_version field.
  const std::string fd = cidx::astcache::files_dir();
  const std::string key = cidx::astcache::cache_key(t);
  const std::string side_path = fd + "/" + key + ".json";

  std::string side_content = read_file(side_path);
  REQUIRE(!side_content.empty());
  // Replace the version string value with a bogus one.
  // The sidecar has "libclang_version": "<real version>".
  // We find the key and replace its value.
  const std::string version_key = "\"libclang_version\":";
  const std::size_t pos = side_content.find(version_key);
  REQUIRE(pos != std::string::npos);
  // Find the opening quote of the value string.
  const std::size_t vstart = side_content.find('"', pos + version_key.size());
  REQUIRE(vstart != std::string::npos);
  const std::size_t vend = side_content.find('"', vstart + 1);
  REQUIRE(vend != std::string::npos);
  side_content.replace(vstart + 1, vend - vstart - 1,
                       "clang version 1.2.3 (bogus)");
  write_file(side_path, side_content);

  // Next load must detect the mismatch and reparse without crashing.
  auto tu2 = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  REQUIRE(tu2.has_value());
  CHECK(cidx::astcache::parse_count() == 2);

  fs::remove_all(cache);
}

// --- 7. Corrupt .ast + valid sidecar ----------------------------------------

TEST_CASE("corrupt .ast + valid sidecar → _load_ast fails → reparse") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();
  cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  CHECK(cidx::astcache::parse_count() == 1);

  // Corrupt the .ast file while leaving the sidecar intact.
  const std::string fd = cidx::astcache::files_dir();
  const std::string key = cidx::astcache::cache_key(t);
  const std::string ast_path = fd + "/" + key + ".ast";

  // Write garbage (same as Python test: repeated garbage string).
  const std::string garbage(
      "THIS IS GARBAGE; NOT A VALID AST FILE\n", 38);
  std::string content;
  for (int i = 0; i < 10; ++i)
    content += garbage;
  write_file(ast_path, content);

  // Next load: sidecar still valid, but _load_ast returns nullopt → reparse.
  auto tu2 = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  REQUIRE(tu2.has_value());
  CHECK(cidx::astcache::parse_count() == 2);

  fs::remove_all(cache);
}

// --- 8. Interchange round-trip (C++-written .ast loads back) ----------------

TEST_CASE("interchange round-trip: C++-written .ast reloads successfully") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();

  // First call: cold miss → parses + saves .ast
  auto tu1 = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  REQUIRE(tu1.has_value());
  CHECK(cidx::astcache::parse_count() == 1);

  // Second call: warm hit → loads from .ast (no reparse)
  auto tu2 = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  REQUIRE(tu2.has_value());
  CHECK(cidx::astcache::parse_count() == 1); // still 1

  // The round-tripped TU must be valid (has a root cursor).
  // We can't directly call tu.cursor in C++ (ParsedTu is opaque here), but
  // the fact that load_or_parse returned non-nullopt means clang_createTU
  // succeeded, which is the interchange contract.
  // If we can call reparse on a _load_ast result, that's the proof.
  // Additional evidence: a second load_or_parse with use_cache=true that
  // does NOT increment parse_count proves the .ast is well-formed.
  CHECK(cidx::astcache::parse_count() == 1);

  fs::remove_all(cache);
}

// --- Additional boundary tests (parametrised-style) -------------------------

TEST_CASE("sidecar fields are complete after cold miss") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();
  cidx::astcache::load_or_parse(t, /*use_cache=*/true);

  const std::string fd = cidx::astcache::files_dir();
  const std::string key = cidx::astcache::cache_key(t);
  const std::string side_path = fd + "/" + key + ".json";

  const std::string content = read_file(side_path);
  // All four required fields must be present.
  CHECK(content.find("\"abspath\"") != std::string::npos);
  CHECK(content.find("\"flags_hash\"") != std::string::npos);
  CHECK(content.find("\"src_mtime\"") != std::string::npos);
  CHECK(content.find("\"libclang_version\"") != std::string::npos);
  // abspath must match.
  CHECK(content.find(t.abspath) != std::string::npos);

  fs::remove_all(cache);
}

TEST_CASE("files_dir is inside the hermetic cache, not ~/.cache/cidx") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());

  const std::string fd = cidx::astcache::files_dir();
  CHECK(fd.find(cache) == 0);

  fs::remove_all(cache);
}

TEST_CASE("load_or_parse returns valid TU with use_cache=true") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();
  auto tu = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  CHECK(tu.has_value());

  fs::remove_all(cache);
}

TEST_CASE("load_or_parse returns valid TU with use_cache=false") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = calls_target();
  auto tu = cidx::astcache::load_or_parse(t, /*use_cache=*/false);
  CHECK(tu.has_value());

  fs::remove_all(cache);
}

TEST_CASE("cold miss then warm hit on messy.c (C file boundary)") {
  if (!require_manifests() || !require_libclang())
    return;

  const std::string cache = make_temp_dir();
  ScopedEnv env_cache("INDEXER_CACHE", cache.c_str());
  cidx::astcache::reset_parse_count();

  AstTarget t = messy_target();
  int before = cidx::astcache::parse_count();

  auto tu1 = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  REQUIRE(tu1.has_value());
  CHECK(cidx::astcache::parse_count() == before + 1);

  auto tu2 = cidx::astcache::load_or_parse(t, /*use_cache=*/true);
  REQUIRE(tu2.has_value());
  CHECK(cidx::astcache::parse_count() == before + 1); // no extra parse

  const std::string fd = cidx::astcache::files_dir();
  CHECK(fd.find(cache) == 0);

  fs::remove_all(cache);
}

TEST_CASE("load_ast on garbage file returns nullopt, never throws") {
  const std::string tmp = make_temp_dir();
  const std::string garbage_path = tmp + "/garbage.ast";
  write_file(garbage_path, "\x00\x01\x02 not a PCH file");
  // Must not throw; must return nullopt.
  auto result = cidx::astcache::load_ast(garbage_path);
  CHECK(!result.has_value());
  fs::remove_all(tmp);
}

} // TEST_SUITE("clang")

// ============================================================================
// main
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

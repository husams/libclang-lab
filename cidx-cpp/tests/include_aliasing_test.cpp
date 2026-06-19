// include_aliasing_test.cpp — hermetic port of
// project/tests/test_include_aliasing.py (v0.6.0).
//
// Covers: build_label_map sort/resolve, alias_options (longest-match,
// remainder, space form, isystem, iquote, indirected/relative/unmatched
// unchanged), resolve_options (label decode, env-var decode, plain absolute
// unchanged), round-trip, and update_file_compile_options NOT setting
// args_overridden.
//
// All tests are hermetic ("default" label): no libclang, no Python required.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <cstdlib> // setenv / unsetenv
#include <optional>
#include <string>
#include <vector>

#include "compiledb/compiledb.hpp"
#include "storage/storage.hpp"

using cidx::CompileDb;
using cidx::Storage;

namespace {

// Convenience: build a label map with no lookup override (autoderive=false is
// set inside build_label_map already).
std::vector<std::pair<std::string, std::string>>
build_map(const std::vector<std::pair<std::string, std::string>> &labels) {
  return CompileDb::build_label_map(labels);
}

// Helper: set an env var and restore the old value on destruction.
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

} // namespace

// ---------------------------------------------------------------------------
// build_label_map: resolves stored paths + sorts longest-first then by name
// (mirrors test_build_label_map_resolves_and_sorts_longest_first)
// ---------------------------------------------------------------------------
TEST_CASE("build_label_map resolves and sorts longest-first") {
  // Python: monkeypatch.setenv("REP", "/opt/rep")
  ScopedEnv env("REP", "/opt/rep");

  // ("a", "/opt/rep/a"), ("ab", "$REP/a/b"), ("c", "/c")
  // resolved: a->/opt/rep/a, ab->/opt/rep/a/b, c->/c
  // sorted by (-len, name): /opt/rep/a/b, /opt/rep/a, /c
  const auto lm = build_map(
      {{"a", "/opt/rep/a"}, {"ab", "$REP/a/b"}, {"c", "/c"}});
  REQUIRE(lm.size() == 3);
  CHECK(lm[0].first == "ab");
  CHECK(lm[0].second == "/opt/rep/a/b");
  CHECK(lm[1].first == "a");
  CHECK(lm[1].second == "/opt/rep/a");
  CHECK(lm[2].first == "c");
  CHECK(lm[2].second == "/c");
}

// ---------------------------------------------------------------------------
// alias_options: longest-match + remainder
// ---------------------------------------------------------------------------
TEST_CASE("alias_options longest match and remainder") {
  const std::vector<std::pair<std::string, std::string>> lm = {
      {"inc", "/p/inc"}, {"p", "/p"}}; // already longest-first

  // exact match
  CHECK(CompileDb::alias_options({"-I/p/inc"}, lm) ==
        std::vector<std::string>{"-I<inc>"});
  // sub-path remainder
  CHECK(CompileDb::alias_options({"-I/p/inc/sub"}, lm) ==
        std::vector<std::string>{"-I<inc>/sub"});
  // most-specific wins over shorter label
  CHECK(CompileDb::alias_options({"-I/p/other"}, lm) ==
        std::vector<std::string>{"-I<p>/other"});
}

// ---------------------------------------------------------------------------
// alias_options: space form and -isystem/-iquote
// ---------------------------------------------------------------------------
TEST_CASE("alias_options space form and isystem") {
  const std::vector<std::pair<std::string, std::string>> lm = {
      {"inc", "/p/inc"}};

  // space form: -I path
  CHECK(CompileDb::alias_options({"-I", "/p/inc"}, lm) ==
        (std::vector<std::string>{"-I", "<inc>"}));
  // -isystem space form
  CHECK(CompileDb::alias_options({"-isystem", "/p/inc"}, lm) ==
        (std::vector<std::string>{"-isystem", "<inc>"}));
  // -iquote glued form
  CHECK(CompileDb::alias_options({"-iquote/p/inc"}, lm) ==
        std::vector<std::string>{"-iquote<inc>"});
}

// ---------------------------------------------------------------------------
// alias_options: leaves unmatched, non-path, already-indirected tokens alone
// (mirrors test_alias_options_leaves_unmatched_and_nonpath_tokens)
// ---------------------------------------------------------------------------
TEST_CASE("alias_options leaves unmatched and non-path tokens unchanged") {
  const std::vector<std::pair<std::string, std::string>> lm = {
      {"inc", "/p/inc"}};
  const std::vector<std::string> opts = {"-DFOO=1", "-std=c++17",
                                         "-I/other/place", "-I<inc>"};
  CHECK(CompileDb::alias_options(opts, lm) == opts);
}

// ---------------------------------------------------------------------------
// alias_options: ignores relative values
// ---------------------------------------------------------------------------
TEST_CASE("alias_options ignores relative values") {
  const std::vector<std::pair<std::string, std::string>> lm = {
      {"inc", "/p/inc"}};
  CHECK(CompileDb::alias_options({"-Iinclude"}, lm) ==
        std::vector<std::string>{"-Iinclude"});
}

// ---------------------------------------------------------------------------
// resolve_options: decodes label and env-var tokens
// ---------------------------------------------------------------------------
TEST_CASE("resolve_options decodes label and envvar") {
  ScopedEnv env("REP", "/opt/rep");
  auto lookup = [](const std::string &n) -> std::optional<std::string> {
    if (n == "inc") {
      return "/p/inc";
    }
    return std::nullopt;
  };

  CHECK(CompileDb::resolve_options({"-I<inc>"}, lookup) ==
        std::vector<std::string>{"-I/p/inc"});
  CHECK(CompileDb::resolve_options({"-I<inc>/sub"}, lookup) ==
        std::vector<std::string>{"-I/p/inc/sub"});
  CHECK(CompileDb::resolve_options({"-I$REP/x"}, lookup) ==
        std::vector<std::string>{"-I/opt/rep/x"});
  // Plain absolute path is left untouched.
  CHECK(CompileDb::resolve_options({"-I/abs/dir"}, lookup) ==
        std::vector<std::string>{"-I/abs/dir"});
}

// ---------------------------------------------------------------------------
// Round-trip: alias_options then resolve_options gives back the original.
// ---------------------------------------------------------------------------
TEST_CASE("encode then decode round trip") {
  const auto lm = build_map({{"inc", "/p/inc"}});
  const std::vector<std::string> original = {"-I/p/inc/sub", "-DK=1"};
  const auto encoded = CompileDb::alias_options(original, lm);
  CHECK(encoded == (std::vector<std::string>{"-I<inc>/sub", "-DK=1"}));

  auto lookup = [](const std::string &n) -> std::optional<std::string> {
    if (n == "inc") {
      return "/p/inc";
    }
    return std::nullopt;
  };
  const auto decoded = CompileDb::resolve_options(encoded, lookup);
  CHECK(decoded == original);
}

// ---------------------------------------------------------------------------
// Storage: update_file_compile_options does NOT set args_overridden
// (mirrors test_storage_realias_helper_does_not_set_args_overridden)
// ---------------------------------------------------------------------------
TEST_CASE("update_file_compile_options does not set args_overridden") {
  // Use :memory: for hermeticity.
  Storage db(":memory:");
  const int64_t cid = db.add_component("c", "/tmp/c");
  const int64_t did = db.add_directory(cid, "");
  const int64_t fid = db.add_file(did, "a.c", std::nullopt, std::nullopt,
                                  std::vector<std::string>{"-I/abs/inc"});
  // rewrite without setting args_overridden
  db.update_file_compile_options(fid, {"-I<inc>"});
  const auto rec = db.get_file_by_id(fid);
  REQUIRE(rec.has_value());
  CHECK(rec->compile_options.has_value());
  CHECK(*rec->compile_options == std::vector<std::string>{"-I<inc>"});
  CHECK(rec->args_overridden == 0); // realias is not a manual override
}

// ---------------------------------------------------------------------------
// v0.8.0: component-aware alias registry (list_alias_pairs / get_alias)
// (mirrors test_alias_registry_* in test_include_aliasing.py)
// ---------------------------------------------------------------------------
TEST_CASE("alias registry includes unique-named components") {
  Storage db(":memory:");
  db.add_component("Numactl", "/opt/osp/Numactl");
  db.add_component("memhog", "/opt/osp/Numactl/memhog", "repo",
                   std::optional<std::string>{"1.2.0"});
  const auto pairs = db.list_alias_pairs();
  // std::map -> sorted by name: Numactl, memhog
  CHECK(pairs == std::vector<std::pair<std::string, std::string>>{
                     {"Numactl", "/opt/osp/Numactl"},
                     {"memhog", "/opt/osp/Numactl/memhog/1.2.0"}});
  CHECK(db.get_alias("Numactl") == std::optional<std::string>{"/opt/osp/Numactl"});
  CHECK(db.get_alias("memhog") ==
        std::optional<std::string>{"/opt/osp/Numactl/memhog/1.2.0"});
  const auto lm = CompileDb::build_label_map(
      db.list_alias_pairs(),
      [&db](const std::string &n) { return db.get_alias(n); });
  // Longest match wins (memhog under Numactl).
  CHECK(CompileDb::alias_options({"-I/opt/osp/Numactl/memhog/1.2.0/inc"}, lm) ==
        std::vector<std::string>{"-I<memhog>/inc"});
  CHECK(CompileDb::alias_options({"-I/opt/osp/Numactl/src"}, lm) ==
        std::vector<std::string>{"-I<Numactl>/src"});
  // Round-trip decode resolves the component name back to the abs dir.
  CHECK(CompileDb::resolve_options(
            {"-I<memhog>/inc"},
            [&db](const std::string &n) { return db.get_alias(n); }) ==
        std::vector<std::string>{"-I/opt/osp/Numactl/memhog/1.2.0/inc"});
}

TEST_CASE("alias registry skips duplicate component names") {
  Storage db(":memory:");
  db.add_component("dup", "/a/dup");
  db.add_component("dup", "/b/dup");
  for (const auto &[name, path] : db.list_alias_pairs()) {
    CHECK(name != "dup");
  }
  CHECK(db.get_alias("dup") == std::nullopt);
}

TEST_CASE("alias registry: explicit label wins over component") {
  Storage db(":memory:");
  db.add_component("foo", "/component/foo");
  db.add_label("foo", "/label/foo");
  bool found = false;
  for (const auto &[name, path] : db.list_alias_pairs()) {
    if (name == "foo") {
      found = true;
      CHECK(path == "/label/foo");
    }
  }
  CHECK(found);
  CHECK(db.get_alias("foo") == std::optional<std::string>{"/label/foo"});
}

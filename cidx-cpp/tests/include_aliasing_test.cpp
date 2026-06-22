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

// Convenience: build a label map from (name, path) pairs (all non-versioned,
// i.e. label entries). autoderive=false is set inside build_label_map.
std::vector<cidx::AliasEntry>
build_map(const std::vector<std::pair<std::string, std::string>> &labels) {
  std::vector<cidx::AliasEntry> entries;
  entries.reserve(labels.size());
  for (const auto &[n, p] : labels) {
    entries.emplace_back(n, p, false);
  }
  return CompileDb::build_label_map(entries);
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
  CHECK(std::get<0>(lm[0]) == "ab");
  CHECK(std::get<1>(lm[0]) == "/opt/rep/a/b");
  CHECK(std::get<0>(lm[1]) == "a");
  CHECK(std::get<1>(lm[1]) == "/opt/rep/a");
  CHECK(std::get<0>(lm[2]) == "c");
  CHECK(std::get<1>(lm[2]) == "/c");
}

// ---------------------------------------------------------------------------
// alias_options: longest-match + remainder
// ---------------------------------------------------------------------------
TEST_CASE("alias_options longest match and remainder") {
  const std::vector<cidx::AliasEntry> lm = {
      {"inc", "/p/inc", false}, {"p", "/p", false}}; // already longest-first

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
  const std::vector<cidx::AliasEntry> lm = {{"inc", "/p/inc", false}};

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
  const std::vector<cidx::AliasEntry> lm = {{"inc", "/p/inc", false}};
  const std::vector<std::string> opts = {"-DFOO=1", "-std=c++17",
                                         "-I/other/place", "-I<inc>"};
  CHECK(CompileDb::alias_options(opts, lm) == opts);
}

// ---------------------------------------------------------------------------
// alias_options: ignores relative values
// ---------------------------------------------------------------------------
TEST_CASE("alias_options ignores relative values") {
  const std::vector<cidx::AliasEntry> lm = {{"inc", "/p/inc", false}};
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
// v0.9.0: version-agnostic component alias registry
// (mirrors test_alias_registry_* in test_include_aliasing.py)
// ---------------------------------------------------------------------------
TEST_CASE("alias registry matches component base, strips version") {
  Storage db(":memory:");
  db.add_component("Numactl", "/opt/osp/Numactl"); // unversioned
  db.add_component("memhog", "/opt/osp/Numactl/memhog", "repo",
                   std::optional<std::string>{"1.2.0"});
  using V = std::vector<std::tuple<std::string, std::string, bool>>;
  // std::map -> sorted by name: Numactl, memhog; component base, versioned=true
  CHECK(db.list_alias_pairs() ==
        V{{"Numactl", "/opt/osp/Numactl", true},
          {"memhog", "/opt/osp/Numactl/memhog", true}});
  CHECK(db.get_alias("Numactl") == std::optional<std::string>{"/opt/osp/Numactl"});
  CHECK(db.get_alias("memhog") ==
        std::optional<std::string>{"/opt/osp/Numactl/memhog/1.2.0"}); // +max ver
  const auto lm = CompileDb::build_label_map(
      db.list_alias_pairs(),
      [&db](const std::string &n) { return db.get_alias(n); });
  // version segment stripped from the stored token
  CHECK(CompileDb::alias_options({"-I/opt/osp/Numactl/memhog/1.2.0/inc"}, lm) ==
        std::vector<std::string>{"-I<memhog>/inc"});
  // a DIFFERENT version still matches the same base (version-agnostic)
  CHECK(CompileDb::alias_options({"-I/opt/osp/Numactl/memhog/9.9.9/inc"}, lm) ==
        std::vector<std::string>{"-I<memhog>/inc"});
  CHECK(CompileDb::alias_options({"-I/opt/osp/Numactl/src"}, lm) ==
        std::vector<std::string>{"-I<Numactl>/src"});
  // round-trip decode injects the registered max version
  CHECK(CompileDb::resolve_options(
            {"-I<memhog>/inc"},
            [&db](const std::string &n) { return db.get_alias(n); }) ==
        std::vector<std::string>{"-I/opt/osp/Numactl/memhog/1.2.0/inc"});
}

TEST_CASE("alias registry collapses same-base multi-version") {
  Storage db(":memory:");
  // version-in-path registration, same base /m/OTF, different versions
  db.add_component("mdw::OTF", "/m/OTF/18-0-0-100");
  db.add_component("mdw::OTF", "/m/OTF/18-0-0-275");
  using V = std::vector<std::tuple<std::string, std::string, bool>>;
  CHECK(db.list_alias_pairs() == V{{"mdw::OTF", "/m/OTF", true}});
  // numeric-max wins (275 > 100), not lexicographic
  CHECK(db.get_alias("mdw::OTF") ==
        std::optional<std::string>{"/m/OTF/18-0-0-275"});
  const auto lm = CompileDb::build_label_map(
      db.list_alias_pairs(),
      [&db](const std::string &n) { return db.get_alias(n); });
  CHECK(CompileDb::alias_options({"-I/m/OTF/18-0-0-100/generated/include"}, lm) ==
        std::vector<std::string>{"-I<mdw::OTF>/generated/include"});
}

TEST_CASE("alias registry skips conflicting bases") {
  Storage db(":memory:");
  db.add_component("dup", "/a/dup");
  db.add_component("dup", "/b/dup");
  for (const auto &[name, path, versioned] : db.list_alias_pairs()) {
    CHECK(name != "dup");
  }
  CHECK(db.get_alias("dup") == std::nullopt);
}

TEST_CASE("alias registry: explicit label wins over component") {
  Storage db(":memory:");
  db.add_component("foo", "/component/foo");
  db.add_label("foo", "/label/foo");
  bool found = false;
  for (const auto &[name, path, versioned] : db.list_alias_pairs()) {
    if (name == "foo") {
      found = true;
      CHECK(path == "/label/foo");
      CHECK(versioned == false); // label entry, exact match
    }
  }
  CHECK(found);
  CHECK(db.get_alias("foo") == std::optional<std::string>{"/label/foo"});
}

TEST_CASE("version_key numeric per-segment ordering") {
  CHECK(CompileDb::version_key("18-0-0-275") >
        CompileDb::version_key("18-0-0-100"));
  CHECK(CompileDb::version_key("18-0-0-100") >
        CompileDb::version_key("18-0-0-11"));
  CHECK(CompileDb::version_key("v2.0") > CompileDb::version_key("1.9"));
}

// -- v0.27.0: set_component_effective_version (property vs embedded) ----------

TEST_CASE("set_component_effective_version: version-as-property updates column") {
  Storage db(":memory:");
  db.add_component("OTF", "/m/OTF", "external", std::optional<std::string>{"1-0-0"});
  CHECK(db.set_component_effective_version("OTF", "1-0-1"));
  const auto comp = db.get_component_by_name("OTF");
  REQUIRE(comp);
  CHECK(comp->path == "/m/OTF");
  CHECK(comp->version == std::optional<std::string>{"1-0-1"});
  CHECK(db.get_alias("OTF") == std::optional<std::string>{"/m/OTF/1-0-1"});
}

TEST_CASE("set_component_effective_version: embedded version rewrites path") {
  Storage db(":memory:");
  // version-in-path registration (no version column)
  db.add_component("OTF", "/m/OTF/1-0-0");
  CHECK(db.set_component_effective_version("OTF", "1-0-1"));
  const auto comp = db.get_component_by_name("OTF");
  REQUIRE(comp);
  CHECK(comp->path == "/m/OTF/1-0-1"); // trailing segment swapped
  CHECK(comp->version == std::nullopt);
  CHECK(db.get_alias("OTF") == std::optional<std::string>{"/m/OTF/1-0-1"});
}

TEST_CASE("set_component_effective_version: ambiguous multi-row is a no-op") {
  Storage db(":memory:");
  db.add_component("OTF", "/m/OTF/1-0-0");
  db.add_component("OTF", "/m/OTF/1-0-1");
  CHECK_FALSE(db.set_component_effective_version("OTF", "2-0-0"));
}

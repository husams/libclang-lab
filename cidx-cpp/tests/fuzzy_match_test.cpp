// fuzzy_match_test — the two fuzzy algorithms (G18), _dir_scope_sql's root
// case (G17), and component_for_path's longest-prefix resolution (G16).
// Pattern construction is pinned directly (Storage::fuzzy_like /
// Storage::dir_scope_sql are public statics for exactly this) and the
// LIKE-escape / case / ordering semantics are asserted end-to-end through
// the query API against an in-memory DB.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <string>
#include <vector>

#include "storage/records.hpp"
#include "storage/sqlite.hpp"
#include "storage/storage.hpp"

namespace {

cidx::Symbol sym(const std::string &usr, const std::string &spelling,
                 const std::string &kind,
                 const std::string &qual_name = std::string()) {
  cidx::Symbol s;
  s.usr = usr;
  s.spelling = spelling;
  s.kind = kind;
  if (!qual_name.empty()) {
    s.qual_name = qual_name;
  }
  return s;
}

std::vector<std::string> quals_of(const std::vector<cidx::Symbol> &syms) {
  std::vector<std::string> out;
  for (const auto &s : syms) {
    out.push_back(s.qual_name.value_or(s.spelling));
  }
  return out;
}

} // namespace

// -- _fuzzy_like pattern construction (storage.py:336-345)
// ---------------------

TEST_CASE("fuzzy_like builds %c%c% from non-space chars") {
  CHECK(cidx::Storage::fuzzy_like("shp") == "%s%h%p%");
  CHECK(cidx::Storage::fuzzy_like("a b\tc") == "%a%b%c%");
  CHECK_MESSAGE(cidx::Storage::fuzzy_like("") == "%%",
                "empty text -> '%%' (Python '%' + ''.join + '%')");
}

TEST_CASE("fuzzy_like escapes the LIKE metacharacters \\ % _") {
  CHECK(cidx::Storage::fuzzy_like("%") == "%\\%%");
  CHECK(cidx::Storage::fuzzy_like("_") == "%\\_%");
  CHECK(cidx::Storage::fuzzy_like("\\") == "%\\\\%");
  CHECK(cidx::Storage::fuzzy_like("a_b") == "%a%\\_%b%");
}

// -- _dir_scope_sql (storage.py:413-422)
// ---------------------------------------

TEST_CASE("dir_scope_sql: subtree fragment, root '' -> '%' (G17)") {
  std::vector<cidx::SqlValue> args;
  const std::string frag = cidx::Storage::dir_scope_sql("src", args);
  CHECK(frag == "(d.path = ? OR d.path LIKE ? ESCAPE '\\')");
  REQUIRE(args.size() == 2);
  CHECK(std::get<std::string>(args[0]) == "src");
  CHECK(std::get<std::string>(args[1]) == "src/%");

  SUBCASE("'' and '.' both mean the component root: subtree = everything") {
    for (const char *root : {"", "."}) {
      std::vector<cidx::SqlValue> a;
      cidx::Storage::dir_scope_sql(root, a);
      CHECK(std::get<std::string>(a[0]) == "");
      CHECK(std::get<std::string>(a[1]) == "%");
    }
  }
  SUBCASE("path is normalized and LIKE-escaped") {
    std::vector<cidx::SqlValue> a;
    cidx::Storage::dir_scope_sql("src//./my_dir", a);
    CHECK(std::get<std::string>(a[0]) == "src/my_dir");
    CHECK(std::get<std::string>(a[1]) == "src/my\\_dir/%");
  }
}

// -- char-in-order fuzzy through the query API
// ---------------------------------

TEST_CASE(
    "list filters: chars in order, escapes honored, ASCII case-insensitive") {
  cidx::Storage db(":memory:");
  db.add_component("a%b", "/data/pct");
  db.add_component("a_b", "/data/us");
  db.add_component("aXb", "/data/x");
  db.add_component("MyRepo", "/data/myrepo");

  const auto names = [&db](const std::optional<std::string> &pattern) {
    std::vector<std::string> out;
    for (const auto &c : db.list_components(pattern)) {
      out.push_back(c.name);
    }
    return out;
  };

  // '%' in the pattern is escaped: matches only the literal-% name, not all.
  CHECK(names(std::string("a%b")) == std::vector<std::string>{"a%b"});
  // '_' is escaped: must not act as single-char wildcard (would hit aXb too).
  CHECK(names(std::string("a_b")) == std::vector<std::string>{"a_b"});
  // chars in order: 'mrp' ~ 'MyRepo'; LIKE is ASCII case-insensitive.
  CHECK(names(std::string("mrp")) == std::vector<std::string>{"MyRepo"});
  CHECK(names(std::string("MYREPO")) == std::vector<std::string>{"MyRepo"});
  // in-order only: reversed chars do not match.
  CHECK(names(std::string("ba")).empty());
}

TEST_CASE("backslash in the pattern is escaped (no ESCAPE-clause injection)") {
  cidx::Storage db(":memory:");
  db.add_component("a\\b", "/data/bs");
  db.add_component("ab", "/data/ab");
  std::vector<std::string> out;
  for (const auto &c : db.list_components(std::string("a\\b"))) {
    out.push_back(c.name);
  }
  // The literal backslash must be required by the pattern.
  CHECK(out == std::vector<std::string>{"a\\b"});
}

// -- '::'-segment fuzzy + ordering (search_symbols)
// -----------------------------

TEST_CASE("search_symbols: segments in order, length-first ordering (G18)") {
  cidx::Storage db(":memory:");
  db.add_symbol(sym("u1", "set", "method", "rk::ConfImpl::set"));
  db.add_symbol(sym("u2", "set", "method", "rk::Conf::set"));
  db.add_symbol(sym("u3", "setter", "function", "setter"));
  db.add_symbol(sym("u4", "get", "method", "rk::Conf::get"));

  // each '::' segment must appear in order as a substring of qual_name
  CHECK(quals_of(db.search_symbols("conf::set")) ==
        std::vector<std::string>{"rk::Conf::set", "rk::ConfImpl::set"});
  // shortest match first, then lexicographic
  CHECK(
      quals_of(db.search_symbols("set")) ==
      std::vector<std::string>{"setter", "rk::Conf::set", "rk::ConfImpl::set"});
  // segments are ordered: 'set::conf' must NOT match 'rk::Conf::set'
  CHECK(db.search_symbols("set::conf").empty());
  // kind filter
  CHECK(quals_of(db.search_symbols("set", std::string("function"))) ==
        std::vector<std::string>{"setter"});
  // '%'/'_' inside a segment are escaped
  db.add_symbol(sym("u5", "odd", "function", "odd%name"));
  CHECK(quals_of(db.search_symbols("odd%name")) ==
        std::vector<std::string>{"odd%name"});
  CHECK(db.search_symbols("oddXname").empty());
  // case-insensitive
  CHECK(quals_of(db.search_symbols("CONF::SET")) ==
        std::vector<std::string>{"rk::Conf::set", "rk::ConfImpl::set"});
}

TEST_CASE(
    "list_symbols name filter: length-first on COALESCE(qual, spelling)") {
  cidx::Storage db(":memory:");
  db.add_symbol(sym("u1", "alpha_beta", "function", "ns::alpha_beta"));
  db.add_symbol(sym("u2", "ab", "function")); // no qual_name -> spelling
  const auto got = db.list_symbols(std::nullopt, std::nullopt, std::nullopt,
                                   std::string("ab"));
  CHECK(quals_of(got) == std::vector<std::string>{"ab", "ns::alpha_beta"});
}

// -- directory subtree scoping through list_files
// --------------------------------

TEST_CASE("dir scope: subtree match without prefix bleed (G17)") {
  cidx::Storage db(":memory:");
  const int64_t comp = db.add_component("repo", "/data/repo");
  const int64_t d_root = db.add_directory(comp, "");
  const int64_t d_src = db.add_directory(comp, "src");
  const int64_t d_sub = db.add_directory(comp, "src/sub");
  const int64_t d_src2 = db.add_directory(comp, "src2");
  db.add_file(d_root, "root.c");
  db.add_file(d_src, "a.c");
  db.add_file(d_sub, "b.c");
  db.add_file(d_src2, "c.c");

  const auto names = [&db, comp](const std::string &dir) {
    std::vector<std::string> out;
    for (const auto &[f, p] : db.list_files(comp, dir)) {
      (void)p;
      out.push_back(f.name);
    }
    return out;
  };
  // 'src' covers itself and its subtree — NOT the sibling 'src2'
  CHECK(names("src") == std::vector<std::string>{"a.c", "b.c"});
  // root '' covers everything
  CHECK(names("") == std::vector<std::string>{"root.c", "a.c", "b.c", "c.c"});
  // '.' normalizes to the root
  CHECK(names(".") == std::vector<std::string>{"root.c", "a.c", "b.c", "c.c"});
  CHECK(names("src/sub") == std::vector<std::string>{"b.c"});
  CHECK(names("nope").empty());
}

// -- component_for_path longest prefix (G16)
// --------------------------------------

TEST_CASE("component_for_path: longest prefix wins, separator-aware") {
  cidx::Storage db(":memory:");
  const int64_t outer = db.add_component("outer", "/data/x");
  const int64_t nested = db.add_component("nested", "/data/x/vendor/lib");
  db.add_component("other", "/data/xy");

  REQUIRE(db.component_for_path("/data/x/main.c").has_value());
  CHECK(db.component_for_path("/data/x/main.c")->id == outer);
  // nested components resolve to the deeper root
  REQUIRE(db.component_for_path("/data/x/vendor/lib/a.c").has_value());
  CHECK(db.component_for_path("/data/x/vendor/lib/a.c")->id == nested);
  // the component root itself is owned
  CHECK(db.component_for_path("/data/x/vendor/lib")->id == nested);
  // '/data/xy/...' must not match the '/data/x' prefix (sep-aware compare)
  CHECK(db.component_for_path("/data/xyz/a.c") == std::nullopt);
  CHECK(db.component_for_path("/nowhere/a.c") == std::nullopt);
}

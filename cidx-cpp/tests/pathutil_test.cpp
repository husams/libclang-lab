// pathutil_test — Python os.path (posixpath) parity tables (D11).
//
// Every expected value below was generated once with python3 and pasted, e.g.:
//   python3 -c "import posixpath
//   for p in ['', '.', '..', '/', '//', '///', '//a', ...]:
//       print(repr(p), '->', repr(posixpath.normpath(p)))"
// (same pattern for relpath/join/split/dirname/basename/expanduser).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <array>
#include <cstdlib>
#include <string>
#include <utility>
#include <vector>

#include "util/errors.hpp"
#include "util/pathutil.hpp"

namespace pu = cidx::pathutil;

TEST_CASE("normpath matches posixpath.normpath") {
  const std::vector<std::pair<std::string, std::string>> table = {
      {"", "."}, // '' -> '.'
      {".", "."},         {"..", ".."},
      {"/", "/"},         {"//", "//"}, // exactly two leading slashes preserved
      {"///", "/"},       {"//a", "//a"},
      {"///a/b", "/a/b"}, {"a/", "a"},     // trailing-sep stripping
      {"a//b", "a/b"},                     // '//' collapse
      {"a/./b", "a/b"},   {"a/b/..", "a"}, // '..' collapse
      {"a/b/../..", "."}, {"a/b/../../..", ".."},
      {"../a", "../a"},   {"./", "."},
      {"/a/../..", "/"}, // '..' above an absolute root is dropped
      {"/..", "/"},       {"a/b/c/../../d", "a/d"},
      {".//x", "x"},      {"a/b/.", "a/b"},
      {"/a/b/", "/a/b"},  {"~/x", "~/x"}, // normpath never expands '~'
  };
  for (const auto &row : table) {
    const std::string &input = row.first;
    CAPTURE(input);
    CHECK(pu::normpath(input) == row.second);
  }
}

TEST_CASE("relpath matches posixpath.relpath") {
  const std::vector<std::pair<std::pair<std::string, std::string>, std::string>>
      table = {
          {{"/a/b/c", "/a/b"}, "c"},
          {{"/a/b", "/a/b"}, "."},
          {{"/a/b", "/a/b/c"}, ".."},
          {{"/a/b/c", "/d/e"}, "../../a/b/c"}, // across distinct roots
          {{"/", "/a/b"}, "../.."},
          {{"/a/b", "/"}, "a/b"},
          {{"/x", "/x/y/z"}, "../.."},
      };
  for (const auto &row : table) {
    const std::string &path = row.first.first;
    const std::string &start = row.first.second;
    CAPTURE(path);
    CAPTURE(start);
    CHECK(pu::relpath(path, start) == row.second);
  }
  // posixpath.relpath('') raises ValueError.
  CHECK_THROWS_AS(pu::relpath("", "/a"), cidx::CidxError);
}

TEST_CASE("join matches posixpath.join") {
  CHECK(pu::join(std::string("a"), "b") == "a/b");
  CHECK(pu::join(std::string("a/"), "b") == "a/b");
  CHECK(pu::join(std::string("a"), "/b") == "/b"); // absolute part resets
  CHECK(pu::join(std::string(""), "b") == "b");
  CHECK(pu::join(std::string("a"), "") == "a/");
  CHECK(pu::join(std::string("a"), "b", "c") == "a/b/c");
  CHECK(pu::join(std::string("/a"), "b/", "c") == "/a/b/c");
  CHECK(pu::join(std::string("a"), "", "b") == "a/b");
}

TEST_CASE("split/dirname/basename match posixpath") {
  // (path, dirname, basename)
  const std::vector<std::array<std::string, 3>> table = {
      {"/a/b/c", "/a/b", "c"},
      {"/a/b/", "/a/b", ""},
      {"a", "", "a"},
      {"", "", ""},
      {"/", "/", ""},
      {"//a", "//", "a"}, // all-slash head is kept verbatim
      {"a/b", "a", "b"},
      {"/a", "/", "a"},
  };
  for (const auto &row : table) {
    CAPTURE(row[0]);
    CHECK(pu::dirname(row[0]) == row[1]);
    CHECK(pu::basename(row[0]) == row[2]);
    const auto [head, tail] = pu::split(row[0]);
    CHECK(head == row[1]);
    CHECK(tail == row[2]);
  }
}

TEST_CASE("abspath = normpath(join(cwd, p))") {
  CHECK(pu::abspath("/a/../b") == "/b");
  CHECK(pu::abspath("/a/b/") == "/a/b");
  const std::string cwd = pu::getcwd();
  CHECK(pu::abspath(".") == pu::normpath(cwd));
  CHECK(pu::abspath("x/y") == pu::normpath(cwd + "/x/y"));
  CHECK(pu::abspath("") == pu::normpath(cwd));
  CHECK(pu::isabs(pu::abspath("rel")));
}

TEST_CASE("expanduser matches posixpath.expanduser") {
  // Python ground truth (HOME='/tmp/pyhome'):
  //   expanduser('~') == '/tmp/pyhome'; '~/x' == '/tmp/pyhome/x';
  //   'x/~' unchanged; '~nosuchuser_xyz/a' unchanged.
  // HOME='/': expanduser('~') == '/'; '~/a' == '/a'.
  const char *saved = std::getenv("HOME");
  const std::string saved_home = (saved != nullptr) ? saved : "";

  ::setenv("HOME", "/tmp/pyhome", 1);
  CHECK(pu::expanduser("~") == "/tmp/pyhome");
  CHECK(pu::expanduser("~/x") == "/tmp/pyhome/x");
  CHECK(pu::expanduser("x/~") == "x/~");
  CHECK(pu::expanduser("~nosuchuser_xyz/a") == "~nosuchuser_xyz/a");
  CHECK(pu::expanduser("plain") == "plain");

  ::setenv("HOME", "/", 1);
  CHECK(pu::expanduser("~") == "/");
  CHECK(pu::expanduser("~/a") == "/a");

  if (saved != nullptr) {
    ::setenv("HOME", saved_home.c_str(), 1);
  } else {
    ::unsetenv("HOME");
  }
}

// ---------------------------------------------------------------------------
// Portable-paths (v14) — expandvars, label_expand, resolve_fs_path
// (contract §1.1, §1.2, §1.3)
// ---------------------------------------------------------------------------

TEST_CASE("expandvars: fast-path when no $ present") {
  CHECK(pu::expandvars("plain") == "plain");
  CHECK(pu::expandvars("") == "");
  CHECK(pu::expandvars("/usr/local/include") == "/usr/local/include");
}

TEST_CASE("expandvars: defined variable is substituted") {
  const char *saved = std::getenv("CIDX_TEST_VAR");
  const std::string saved_val = (saved != nullptr) ? saved : "";
  ::setenv("CIDX_TEST_VAR", "/opt/foo", 1);

  CHECK(pu::expandvars("$CIDX_TEST_VAR/bar") == "/opt/foo/bar");
  CHECK(pu::expandvars("${CIDX_TEST_VAR}/bar") == "/opt/foo/bar");
  CHECK(pu::expandvars("prefix_$CIDX_TEST_VAR") == "prefix_/opt/foo");

  if (saved != nullptr) {
    ::setenv("CIDX_TEST_VAR", saved_val.c_str(), 1);
  } else {
    ::unsetenv("CIDX_TEST_VAR");
  }
}

TEST_CASE("expandvars: undefined variable left literal") {
  // Guarantee the var is absent.
  ::unsetenv("CIDX_NOSUCH_VAR_99");
  CHECK(pu::expandvars("$CIDX_NOSUCH_VAR_99/x") == "$CIDX_NOSUCH_VAR_99/x");
  CHECK(pu::expandvars("${CIDX_NOSUCH_VAR_99}/x") == "${CIDX_NOSUCH_VAR_99}/x");
}

TEST_CASE("expandvars: unterminated brace left literal") {
  // ${FOO without closing } is left literal (CPython parity).
  ::unsetenv("CIDX_NOSUCH_VAR_99");
  CHECK(pu::expandvars("${CIDX_NOSUCH_VAR_99") == "${CIDX_NOSUCH_VAR_99");
}

TEST_CASE("expandvars: parity table — contract §1.1") {
  // All cases generated from Python 3.x os.path.expandvars.
  ::setenv("CIDX_A", "AAA", 1);
  ::setenv("CIDX_B", "", 1); // defined but empty → empty string (not literal)
  ::unsetenv("CIDX_MISSING");

  // $VAR forms
  CHECK(pu::expandvars("$CIDX_A") == "AAA");
  CHECK(pu::expandvars("$CIDX_B") == "");        // defined-empty → ""
  CHECK(pu::expandvars("$CIDX_MISSING") == "$CIDX_MISSING");
  CHECK(pu::expandvars("pre/$CIDX_A/suf") == "pre/AAA/suf"); // delimiters are non-word

  // ${VAR} forms
  CHECK(pu::expandvars("${CIDX_A}") == "AAA");
  CHECK(pu::expandvars("${CIDX_A}/x") == "AAA/x");
  CHECK(pu::expandvars("${CIDX_MISSING}") == "${CIDX_MISSING}");

  // Multiple substitutions
  CHECK(pu::expandvars("$CIDX_A/$CIDX_A") == "AAA/AAA");

  // No $: fast-path
  CHECK(pu::expandvars("no_dollar") == "no_dollar");

  ::unsetenv("CIDX_A");
  ::unsetenv("CIDX_B");
}

TEST_CASE("label_expand: autoderive (no registry)") {
  // Default LabelResolver has no lookup and autoderive=true.
  pu::LabelResolver res;

  // <libfoo-include> → "/" + "libfoo-include".replace('-','/') = /libfoo/include
  CHECK(pu::label_expand("<libfoo-include>", res) == "/libfoo/include");
  // No placeholder: returned unchanged.
  CHECK(pu::label_expand("/usr/include", res) == "/usr/include");
  // Prefix + placeholder
  CHECK(pu::label_expand("/x/<lib-hdr>", res) == "/x//lib/hdr");
}

TEST_CASE("label_expand: registry hit takes priority") {
  pu::LabelResolver res(
      [](const std::string &name) -> std::optional<std::string> {
        if (name == "mylib-include") {
          return "/opt/mylib/include";
        }
        return std::nullopt;
      },
      /*autoderive=*/true);

  CHECK(pu::label_expand("<mylib-include>", res) == "/opt/mylib/include");
  // Miss → autoderive
  CHECK(pu::label_expand("<other-lib>", res) == "/other/lib");
}

TEST_CASE("label_expand: no autoderive, no registry → literal") {
  pu::LabelResolver res({}, /*autoderive=*/false);
  CHECK(pu::label_expand("<mylib>", res) == "<mylib>");
}

TEST_CASE("label_expand: unterminated < left literal") {
  pu::LabelResolver res;
  CHECK(pu::label_expand("<noclose", res) == "<noclose");
  CHECK(pu::label_expand("prefix<noclose", res) == "prefix<noclose");
}

TEST_CASE("resolve_fs_path: chain label->envvar->expanduser->normpath") {
  ::setenv("CIDX_ROOT", "/opt/cidx", 1);
  ::setenv("HOME", "/tmp/home", 1);

  pu::LabelResolver res(
      [](const std::string &name) -> std::optional<std::string> {
        if (name == "cidx-root") {
          return "$CIDX_ROOT";
        }
        return std::nullopt;
      });

  // <cidx-root>/src → $CIDX_ROOT/src → /opt/cidx/src → normpath = /opt/cidx/src
  CHECK(pu::resolve_fs_path("<cidx-root>/src", res) == "/opt/cidx/src");

  // Tilde expansion
  CHECK(pu::resolve_fs_path("~/data/../bin", res) == "/tmp/home/bin");

  // No-arg overload: just expandvars + expanduser + normpath
  CHECK(pu::resolve_fs_path("$CIDX_ROOT/lib/../include") ==
        "/opt/cidx/include");

  ::unsetenv("CIDX_ROOT");
  // Restore HOME to a safe value to avoid breaking subsequent tests.
  ::setenv("HOME", "/tmp/home", 1);
}

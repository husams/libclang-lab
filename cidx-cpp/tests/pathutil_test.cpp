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

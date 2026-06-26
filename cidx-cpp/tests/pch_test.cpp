// pch_test — pure-logic coverage for the corpus-survey helpers in clangx/pch
// (cidx pch build --from-corpus). Parity with project/tests/test_pch.py.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <algorithm>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "clangx/pch.hpp"

using cidx::pch::HeaderSurvey;
using cidx::pch::include_dirs;
using cidx::pch::pch_relevant;
using cidx::pch::select_shared_headers;

TEST_CASE("pch_relevant drops -arch/-target value pairs whole") {
  const std::vector<std::string> kept = pch_relevant(
      {"-std=c++17", "-arch", "arm64", "-target", "arm64-apple", "-DK"});
  for (const std::string &gone : {"-arch", "arm64", "-target", "arm64-apple"}) {
    CHECK(std::find(kept.begin(), kept.end(), gone) == kept.end());
  }
  CHECK(std::find(kept.begin(), kept.end(), "-std=c++17") != kept.end());
  CHECK(std::find(kept.begin(), kept.end(), "-DK") != kept.end());
}

TEST_CASE("include_dirs handles separate and joined spellings") {
  const auto pairs = include_dirs({"-I/a", "-I", "/b", "-isystem", "/sys",
                                   "-isystem/joined", "-DX", "-iquote", "/q"});
  const std::vector<std::pair<std::string, std::string>> want = {
      {"-I", "/a"},       {"-I", "/b"},      {"-isystem", "/sys"},
      {"-isystem", "/joined"}, {"-iquote", "/q"}};
  CHECK(pairs == want);
}

TEST_CASE("select_shared_headers applies coverage + directability") {
  HeaderSurvey s;
  s.freq = {{"/h/hot.h", 9}, {"/h/mid.h", 6}, {"/h/rare.h", 1},
            {"/h/internal.h", 10}};
  s.directable = {"/h/hot.h", "/h/mid.h", "/h/rare.h"}; // internal.h never direct

  // n=10, coverage 0.7 -> threshold 7: only hot.h (9); internal.h (10) filtered
  // out as never-directly-included.
  CHECK(select_shared_headers(s, 10, 0.7, 0) ==
        std::vector<std::string>{"/h/hot.h"});
  // coverage 0.5 admits mid.h too, most-shared first; internal.h still out.
  CHECK(select_shared_headers(s, 10, 0.5, 0) ==
        std::vector<std::string>{"/h/hot.h", "/h/mid.h"});
  // min_tus raises the bar above coverage.
  CHECK(select_shared_headers(s, 10, 0.0, 7) ==
        std::vector<std::string>{"/h/hot.h"});
}

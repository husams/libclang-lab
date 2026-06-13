// json_min_test — decode of Python json.dumps(list[str]) output, encode
// round-trip, and rejection of non-array / non-string payloads (D5).
//
// Pinned inputs generated once with:
//   python3 -c "import json
//   for l in [[], ['a'], ['a','b'], ['-I/usr/include','-DX=\"y\"'],
//             ['héllo','日本','\U0001D11E'],
//             ['tab\there\nnl','back\\\\slash','quote\"q']]:
//       print(json.dumps(l))"
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <string>
#include <vector>

#include "util/errors.hpp"
#include "util/json_min.hpp"

using cidx::json_min::decode_string_array;
using cidx::json_min::encode_string_array;

TEST_CASE("decodes Python json.dumps output with ', ' separators") {
  CHECK(decode_string_array("[]") == std::vector<std::string>{});
  CHECK(decode_string_array("[\"a\"]") == std::vector<std::string>{"a"});
  CHECK(decode_string_array("[\"a\", \"b\"]") ==
        std::vector<std::string>{"a", "b"});
  CHECK(decode_string_array("[\"-I/usr/include\", \"-DX=\\\"y\\\"\"]") ==
        std::vector<std::string>{"-I/usr/include", "-DX=\"y\""});
}

TEST_CASE("decodes ensure_ascii \\uXXXX escapes to UTF-8") {
  // json.dumps(['héllo','日本','𝄞']) ->
  //   ["héllo", "日本", "𝄞"]   (surrogate pair!)
  const auto items = decode_string_array(
      "[\"h\\u00e9llo\", \"\\u65e5\\u672c\", \"\\ud834\\udd1e\"]");
  REQUIRE(items.size() == 3);
  CHECK(items[0] == "h\xc3\xa9llo");             // U+00E9
  CHECK(items[1] == "\xe6\x97\xa5\xe6\x9c\xac"); // 日本
  CHECK(items[2] == "\xf0\x9d\x84\x9e");         // U+1D11E via surrogate pair
}

TEST_CASE("decodes control-char and backslash escapes") {
  // json.dumps(['tab\there\nnl','back\\slash','quote"q'])
  const auto items = decode_string_array(
      "[\"tab\\there\\nnl\", \"back\\\\slash\", \"quote\\\"q\"]");
  REQUIRE(items.size() == 3);
  CHECK(items[0] == "tab\there\nnl");
  CHECK(items[1] == "back\\slash");
  CHECK(items[2] == "quote\"q");
}

TEST_CASE("decode tolerates surrounding whitespace") {
  CHECK(decode_string_array("  [ \"a\" ,\n\t\"b\" ]  ") ==
        std::vector<std::string>{"a", "b"});
}

TEST_CASE("encode -> decode round-trips") {
  const std::vector<std::vector<std::string>> cases = {
      {},
      {"a"},
      {"-I/usr/include", "-DX=\"y\""},
      {"h\xc3\xa9llo", "\xe6\x97\xa5\xe6\x9c\xac", "\xf0\x9d\x84\x9e"},
      {"tab\there\nnl", "back\\slash", "quote\"q", std::string("\x01\x1f", 2)},
  };
  for (const auto &items : cases) {
    CAPTURE(items.size());
    CHECK(decode_string_array(encode_string_array(items)) == items);
  }
  // Write format is compact (read-compat is the only contract — D5).
  CHECK(encode_string_array({"a", "b"}) == "[\"a\",\"b\"]");
  CHECK(encode_string_array({}) == "[]");
}

TEST_CASE("rejects non-array and non-string payloads with an error") {
  CHECK_THROWS_AS(decode_string_array("{\"a\": 1}"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("\"str\""), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("123"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("null"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("[1]"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("[null]"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("[[\"a\"]]"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array(""), cidx::CidxError);
}

TEST_CASE("rejects malformed arrays") {
  CHECK_THROWS_AS(decode_string_array("["), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("[\"a\""), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("[\"a\",]"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("[\"a\" \"b\"]"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("[\"unterminated]"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("[\"bad\\q\"]"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("[\"trunc\\u00\"]"), cidx::CidxError);
  CHECK_THROWS_AS(decode_string_array("[] trailing"), cidx::CidxError);
}

// Minimal JSON codec for arrays of strings only (design D5).
// The compile_options DB column is the only JSON in the system. Contract:
//   * decode accepts anything Python json.dumps(list[str]) emits, including
//     ensure_ascii \uXXXX escapes (with surrogate pairs -> UTF-8) and the
//     default ", " separators;
//   * encode emits compact ["a","b"] — write format is free, READ
//     compatibility is the only contract;
//   * non-array / non-string payloads and malformed text are rejected with a
//     CidxError.
#pragma once

#include <string>
#include <vector>

namespace cidx {
namespace json_min {

// Throws CidxError on malformed input, non-array payloads, or non-string
// elements.
std::vector<std::string> decode_string_array(const std::string &text);

// Compact encoding: ["a","b"]; control chars escaped, UTF-8 passed through.
std::string encode_string_array(const std::vector<std::string> &items);

} // namespace json_min
} // namespace cidx

// json_out -- byte-replica of Python json.dumps(obj, indent=2).
//
// Builds a small value tree (Null, Bool, Int, Str, Arr, Obj) and serialises
// it with the EXACT formatting rules of CPython json.dumps(indent=2):
//   - 2-space indent per nesting level
//   - opening bracket stays on current line
//   - each element/member on its own indented line
//   - closing bracket on its own line at parent indent
//   - empty containers: [] / {} on one line
//   - member separator: ": " (note: space after colon, CPython default with indent)
//   - item separator: "," with NO trailing space (CPython drops the space when
//     indent is set)
//   - null / true / false lowercase
//   - strings: ensure_ascii=True — non-ASCII and control chars escaped
//   - no trailing whitespace on any line
//   - dumps_indent2() does NOT append a trailing newline (the caller adds it)
//
// This is a SEPARATE emitter from util/json_min (which is strings-only,
// compact, read-compatible). json_out is write-only, pretty, and supports the
// full value tree needed by the `ast` command group (ADR-006 §3).
#pragma once

#include <cstdint>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

namespace cidx {
namespace json_out {

struct Value;
using Array = std::vector<Value>;
using Member = std::pair<std::string, Value>; // insertion-ordered
using Object = std::vector<Member>;           // NOT a map — preserves order

struct Value {
  enum class T { Null, Bool, Int, Str, Arr, Obj } t = T::Null;
  bool b = false;
  long long i = 0;
  std::string s;
  Array a;
  Object o;

  static Value null();
  static Value of(bool v);
  // Any integral type (except bool) -> Int. A constrained template — rather
  // than a single of(long long) overload — keeps the call portable: int64_t is
  // `long long` on macOS but `long` on LP64 Linux, and a `long` argument would
  // otherwise be ambiguous between of(bool) and of(long long). bool is excluded
  // so it routes to of(bool).
  template <typename Int, typename = std::enable_if_t<
                              std::is_integral_v<Int> &&
                              !std::is_same_v<std::remove_cv_t<Int>, bool>>>
  static Value of(Int v) {
    Value out;
    out.t = T::Int;
    out.i = static_cast<long long>(v);
    return out;
  }
  static Value of(const std::string &v);
  static Value of(std::string &&v);
  static Value arr(Array v);
  static Value obj(Object v);
};

// Serialise to json.dumps(v, indent=2). Returns the bracketed text with
// NO trailing newline — the caller appends "\n" (matching the Python handler's
// print() convention).
std::string dumps_indent2(const Value &v);

} // namespace json_out
} // namespace cidx

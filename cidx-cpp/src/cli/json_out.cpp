#include "cli/json_out.hpp"

#include <cassert>
#include <cstdint>
#include <sstream>

namespace cidx {
namespace json_out {

// ---------------------------------------------------------------------------
// Value factory methods

Value Value::null() {
  Value v;
  v.t = T::Null;
  return v;
}

Value Value::of(bool val) {
  Value v;
  v.t = T::Bool;
  v.b = val;
  return v;
}

Value Value::of(long long val) {
  Value v;
  v.t = T::Int;
  v.i = val;
  return v;
}

Value Value::of(const std::string &val) {
  Value v;
  v.t = T::Str;
  v.s = val;
  return v;
}

Value Value::of(std::string &&val) {
  Value v;
  v.t = T::Str;
  v.s = std::move(val);
  return v;
}

Value Value::arr(Array val) {
  Value v;
  v.t = T::Arr;
  v.a = std::move(val);
  return v;
}

Value Value::obj(Object val) {
  Value v;
  v.t = T::Obj;
  v.o = std::move(val);
  return v;
}

// ---------------------------------------------------------------------------
// String escaping — CPython json encoder, ensure_ascii=True:
//   - "  \  \b  \f  \n  \r  \t  -> their two-char escape sequences
//   - other control chars < 0x20 -> \uXXXX (lowercase hex)
//   - non-ASCII (>= 0x80)        -> \uXXXX or surrogate pair for > 0xFFFF
//   The input is assumed to be valid UTF-8.

namespace {

void encode_string(std::ostringstream &out, const std::string &s) {
  out << '"';
  const unsigned char *p =
      reinterpret_cast<const unsigned char *>(s.data());
  const unsigned char *end = p + s.size();

  while (p < end) {
    unsigned char c = *p;

    if (c < 0x80) {
      // ASCII
      switch (c) {
      case '"':
        out << "\\\"";
        break;
      case '\\':
        out << "\\\\";
        break;
      case '\b':
        out << "\\b";
        break;
      case '\f':
        out << "\\f";
        break;
      case '\n':
        out << "\\n";
        break;
      case '\r':
        out << "\\r";
        break;
      case '\t':
        out << "\\t";
        break;
      default:
        if (c < 0x20) {
          // Other control chars: \uXXXX lowercase
          char buf[7];
          std::snprintf(buf, sizeof(buf), "\\u%04x",
                        static_cast<unsigned>(c));
          out << buf;
        } else {
          out << static_cast<char>(c);
        }
        break;
      }
      ++p;
    } else {
      // Decode UTF-8 code point
      uint32_t codepoint = 0;
      int remaining = 0;
      if ((c & 0xE0) == 0xC0) {
        codepoint = c & 0x1F;
        remaining = 1;
      } else if ((c & 0xF0) == 0xE0) {
        codepoint = c & 0x0F;
        remaining = 2;
      } else if ((c & 0xF8) == 0xF0) {
        codepoint = c & 0x07;
        remaining = 3;
      } else {
        // Invalid UTF-8 byte — emit as replacement
        out << "\\ufffd";
        ++p;
        continue;
      }
      ++p;
      for (int j = 0; j < remaining && p < end; ++j, ++p) {
        if ((*p & 0xC0) != 0x80) {
          break; // malformed — best effort
        }
        codepoint = (codepoint << 6) | (*p & 0x3F);
      }

      if (codepoint <= 0xFFFF) {
        // BMP: single \uXXXX
        char buf[7];
        std::snprintf(buf, sizeof(buf), "\\u%04x", codepoint);
        out << buf;
      } else {
        // Surrogate pair for > 0xFFFF
        codepoint -= 0x10000;
        uint32_t high = 0xD800 + ((codepoint >> 10) & 0x3FF);
        uint32_t low = 0xDC00 + (codepoint & 0x3FF);
        char buf[13];
        std::snprintf(buf, sizeof(buf), "\\u%04x\\u%04x", high, low);
        out << buf;
      }
    }
  }
  out << '"';
}

// Recursive pretty-printer matching CPython json.dumps(indent=2) output.
// depth = current nesting level (0 = top level).
void emit(std::ostringstream &out, const Value &v, int depth) {
  const std::string indent(2 * depth, ' ');
  const std::string inner(2 * (depth + 1), ' ');

  switch (v.t) {
  case Value::T::Null:
    out << "null";
    break;
  case Value::T::Bool:
    out << (v.b ? "true" : "false");
    break;
  case Value::T::Int:
    out << v.i;
    break;
  case Value::T::Str:
    encode_string(out, v.s);
    break;
  case Value::T::Arr:
    if (v.a.empty()) {
      out << "[]";
    } else {
      out << "[\n";
      for (std::size_t k = 0; k < v.a.size(); ++k) {
        out << inner;
        emit(out, v.a[k], depth + 1);
        if (k + 1 < v.a.size()) {
          out << ",";
        }
        out << "\n";
      }
      out << indent << "]";
    }
    break;
  case Value::T::Obj:
    if (v.o.empty()) {
      out << "{}";
    } else {
      out << "{\n";
      for (std::size_t k = 0; k < v.o.size(); ++k) {
        out << inner;
        encode_string(out, v.o[k].first);
        out << ": ";
        emit(out, v.o[k].second, depth + 1);
        if (k + 1 < v.o.size()) {
          out << ",";
        }
        out << "\n";
      }
      out << indent << "}";
    }
    break;
  }
}

} // namespace

std::string dumps_indent2(const Value &v) {
  std::ostringstream out;
  emit(out, v, 0);
  return out.str();
}

} // namespace json_out
} // namespace cidx

#include "util/json_min.hpp"

#include <cstdio>

#include "util/errors.hpp"

namespace cidx {
namespace json_min {

namespace {

[[noreturn]] void fail(const std::string &what, std::size_t pos) {
  throw CidxError("json_min: " + what + " at offset " + std::to_string(pos));
}

void skip_ws(const std::string &s, std::size_t &i) {
  while (i < s.size() &&
         (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) {
    ++i;
  }
}

void append_utf8(std::string &out, unsigned cp) {
  if (cp < 0x80) {
    out += static_cast<char>(cp);
  } else if (cp < 0x800) {
    out += static_cast<char>(0xC0 | (cp >> 6));
    out += static_cast<char>(0x80 | (cp & 0x3F));
  } else if (cp < 0x10000) {
    out += static_cast<char>(0xE0 | (cp >> 12));
    out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
    out += static_cast<char>(0x80 | (cp & 0x3F));
  } else {
    out += static_cast<char>(0xF0 | (cp >> 18));
    out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
    out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
    out += static_cast<char>(0x80 | (cp & 0x3F));
  }
}

unsigned parse_hex4(const std::string &s, std::size_t &i) {
  if (i + 4 > s.size()) {
    fail("truncated \\u escape", i);
  }
  unsigned v = 0;
  for (int k = 0; k < 4; ++k) {
    const char c = s[i + static_cast<std::size_t>(k)];
    v <<= 4;
    if (c >= '0' && c <= '9') {
      v |= static_cast<unsigned>(c - '0');
    } else if (c >= 'a' && c <= 'f') {
      v |= static_cast<unsigned>(c - 'a' + 10);
    } else if (c >= 'A' && c <= 'F') {
      v |= static_cast<unsigned>(c - 'A' + 10);
    } else {
      fail("bad hex digit in \\u escape", i);
    }
  }
  i += 4;
  return v;
}

// i points at the opening quote; on return i is past the closing quote.
std::string parse_string(const std::string &s, std::size_t &i) {
  if (i >= s.size() || s[i] != '"') {
    fail("expected string", i);
  }
  ++i;
  std::string out;
  while (true) {
    if (i >= s.size()) {
      fail("unterminated string", i);
    }
    const char c = s[i];
    if (c == '"') {
      ++i;
      return out;
    }
    if (c != '\\') {
      out += c; // raw UTF-8 bytes pass through verbatim
      ++i;
      continue;
    }
    ++i; // consume backslash
    if (i >= s.size()) {
      fail("truncated escape", i);
    }
    const char e = s[i];
    ++i;
    switch (e) {
    case '"':
      out += '"';
      break;
    case '\\':
      out += '\\';
      break;
    case '/':
      out += '/';
      break;
    case 'b':
      out += '\b';
      break;
    case 'f':
      out += '\f';
      break;
    case 'n':
      out += '\n';
      break;
    case 'r':
      out += '\r';
      break;
    case 't':
      out += '\t';
      break;
    case 'u': {
      unsigned cp = parse_hex4(s, i);
      if (cp >= 0xD800 && cp <= 0xDBFF) { // high surrogate
        if (i + 1 < s.size() && s[i] == '\\' && s[i + 1] == 'u') {
          std::size_t save = i;
          i += 2;
          const unsigned lo = parse_hex4(s, i);
          if (lo >= 0xDC00 && lo <= 0xDFFF) {
            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
          } else {
            i = save; // unpaired; emit the lone surrogate as-is
          }
        }
      }
      append_utf8(out, cp);
      break;
    }
    default:
      fail("unknown escape", i - 1);
    }
  }
}

} // namespace

std::vector<std::string> decode_string_array(const std::string &text) {
  std::size_t i = 0;
  skip_ws(text, i);
  if (i >= text.size() || text[i] != '[') {
    fail("payload is not a JSON array", i);
  }
  ++i;
  std::vector<std::string> items;
  skip_ws(text, i);
  if (i < text.size() && text[i] == ']') {
    ++i;
  } else {
    while (true) {
      skip_ws(text, i);
      if (i >= text.size() || text[i] != '"') {
        fail("array element is not a string", i);
      }
      items.push_back(parse_string(text, i));
      skip_ws(text, i);
      if (i >= text.size()) {
        fail("unterminated array", i);
      }
      if (text[i] == ',') {
        ++i;
        continue;
      }
      if (text[i] == ']') {
        ++i;
        break;
      }
      fail("expected ',' or ']'", i);
    }
  }
  skip_ws(text, i);
  if (i != text.size()) {
    fail("trailing data after array", i);
  }
  return items;
}

std::string encode_string_array(const std::vector<std::string> &items) {
  std::string out = "[";
  bool first = true;
  for (const auto &item : items) {
    if (!first) {
      out += ',';
    }
    first = false;
    out += '"';
    for (const char ch : item) {
      const auto c = static_cast<unsigned char>(ch);
      switch (c) {
      case '"':
        out += "\\\"";
        break;
      case '\\':
        out += "\\\\";
        break;
      case '\b':
        out += "\\b";
        break;
      case '\f':
        out += "\\f";
        break;
      case '\n':
        out += "\\n";
        break;
      case '\r':
        out += "\\r";
        break;
      case '\t':
        out += "\\t";
        break;
      default:
        if (c < 0x20) {
          char esc[8];
          std::snprintf(esc, sizeof esc, "\\u%04x", c);
          out += esc;
        } else {
          out += ch; // UTF-8 bytes verbatim
        }
      }
    }
    out += '"';
  }
  out += ']';
  return out;
}

} // namespace json_min
} // namespace cidx

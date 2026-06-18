#include "cli/format.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <ctime>
#include <limits>

#include "storage/storage.hpp"

namespace cidx {
namespace cli {
namespace format {

std::string rjust(const std::string &s, std::size_t width) {
  if (s.size() >= width) {
    return s;
  }
  return std::string(width - s.size(), ' ') + s;
}

std::string ljust(const std::string &s, std::size_t width) {
  if (s.size() >= width) {
    return s;
  }
  return s + std::string(width - s.size(), ' ');
}

std::string py_str(const std::optional<int64_t> &v) {
  return v ? std::to_string(*v) : "None";
}

std::string py_str(const std::optional<std::string> &v) {
  return v ? *v : "None";
}

std::string py_repr(const std::string &s) {
  // Python repr: single quotes unless the string contains ' and no ".
  const bool has_single = s.contains('\'');
  const bool has_double = s.contains('"');
  const char quote = (has_single && !has_double) ? '"' : '\'';
  std::string out(1, quote);
  for (char c : s) {
    if (c == '\\' || c == quote) {
      out += '\\';
    }
    out += c;
  }
  out += quote;
  return out;
}

std::string format_mtime(double epoch) {
  // datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") —
  // local time, fraction dropped (cli.py:421-424).
  const auto t = static_cast<std::time_t>(std::floor(epoch));
  std::tm tm_buf{};
  ::localtime_r(&t, &tm_buf);
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &tm_buf);
  return buf;
}

void print_symbols(Storage &db, const std::vector<Symbol> &hits, int limit,
                   std::ostream &out) {
  // Python: shown = hits[:limit] if limit else hits (0 = all; negative
  // slices drop from the end — slice parity kept even if never used).
  std::size_t shown_n = hits.size();
  if (limit > 0) {
    shown_n = std::min<std::size_t>(shown_n, static_cast<std::size_t>(limit));
  } else if (limit < 0) {
    const auto drop = static_cast<std::size_t>(-static_cast<long>(limit));
    shown_n = drop >= shown_n ? 0 : shown_n - drop;
  }

  // width = max(len(qual_name or spelling) for shown), default 0.
  std::size_t width = 0;
  for (std::size_t i = 0; i < shown_n; ++i) {
    const Symbol &s = hits[i];
    const std::string &name =
        (s.qual_name && !s.qual_name->empty()) ? *s.qual_name : s.spelling;
    width = std::max(width, name.size());
  }

  for (std::size_t i = 0; i < shown_n; ++i) {
    const Symbol &s = hits[i];
    const std::string &name =
        (s.qual_name && !s.qual_name->empty()) ? *s.qual_name : s.spelling;
    const char *mark = s.is_pure ? "pure" : s.is_definition ? "def " : "decl";
    const std::string path =
        s.file_id ? py_str(db.file_abs_path(*s.file_id)) : "?";
    // f"{s.id:>6}  {name:<{width}}  {s.kind:<17} {mark}  {path}:{s.line}"
    out << rjust(std::to_string(s.id), 6) << "  " << ljust(name, width) << "  "
        << ljust(s.kind, 17) << " " << mark << "  " << path << ":"
        << py_str(s.line) << "\n";
    if (s.is_definition && s.decl_file_id) {
      const std::string dpath = py_str(db.file_abs_path(*s.decl_file_id));
      // f"{'':>6}  {'':<{width}}  {'':<17} decl  {dpath}:{s.decl_line}"
      out << rjust("", 6) << "  " << ljust("", width) << "  " << ljust("", 17)
          << " decl  " << dpath << ":" << py_str(s.decl_line) << "\n";
    }
  }
  // f"{len(hits)} match(es){extra}"
  out << hits.size() << " match(es)";
  if (shown_n < hits.size()) {
    out << " (showing " << shown_n << ")";
  }
  out << "\n";
}

void print_field(std::ostream &out, const std::string &key,
                 const std::string &value) {
  out << ljust(key, 12) << " " << value << "\n";
}

std::string group_thousands(int64_t n) {
  // Mirror Python f"{n:,}": comma-separated groups of 3 digits.
  bool negative = n < 0;
  // Use unsigned arithmetic to handle INT64_MIN safely.
  uint64_t abs_n = negative ? (n == std::numeric_limits<int64_t>::min()
                                   ? static_cast<uint64_t>(9223372036854775808ULL)
                                   : static_cast<uint64_t>(-n))
                            : static_cast<uint64_t>(n);
  if (abs_n == 0) {
    return "0";
  }
  // Build digits in reverse, inserting commas every 3.
  std::string rev;
  rev.reserve(26);
  int pos = 0;
  while (abs_n > 0) {
    if (pos > 0 && pos % 3 == 0) {
      rev += ',';
    }
    rev += static_cast<char>('0' + (abs_n % 10));
    abs_n /= 10;
    ++pos;
  }
  if (negative) {
    rev += '-';
  }
  std::string result(rev.rbegin(), rev.rend());
  return result;
}

} // namespace format
} // namespace cli
} // namespace cidx

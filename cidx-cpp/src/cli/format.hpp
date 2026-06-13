// Output formatting shared by the query commands (design §6.3, G31, D14).
// Every helper reproduces a Python f-string from cli.py byte-for-byte; the
// formats are golden-locked against the Python tool's output.
#pragma once

#include <cstdint>
#include <optional>
#include <ostream>
#include <string>
#include <vector>

#include "storage/records.hpp"

namespace cidx {

class Storage;

namespace cli {
namespace format {

// Python f-string alignment: {value:>width} / {value:<width}.
std::string rjust(const std::string &s, std::size_t width);
std::string ljust(const std::string &s, std::size_t width);

// Python str(None) == "None" — how absent ints/strings render inside
// f-strings (e.g. "path:None" when a symbol has no stored line).
std::string py_str(const std::optional<int64_t> &v);
std::string py_str(const std::optional<std::string> &v);

// Python repr() quoting for the {name!r} error messages ('nope').
std::string py_repr(const std::string &s);

// show file mtime: LOCAL time via localtime_r + strftime, format copied
// exactly from cli.py:421-424 (D14, G31).
std::string format_mtime(double epoch);

// The symbol table shared by 'search' and 'list symbols' (cli.py:248-261):
// id, qual name (else spelling), kind, def /decl/pure mark, path:line, a
// second decl row for definitions with a stored decl site, and the trailing
// "N match(es)[ (showing M)]" line. Slicing semantics are Python's
// hits[:limit]-if-limit (0 = all).
void print_symbols(Storage &db, const std::vector<Symbol> &hits, int limit,
                   std::ostream &out);

// Key/value dump line used by 'show': f"{key:<12} {value}".
void print_field(std::ostream &out, const std::string &key,
                 const std::string &value);

} // namespace format
} // namespace cli
} // namespace cidx

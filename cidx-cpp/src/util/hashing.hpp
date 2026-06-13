// Content hashing for staleness detection (design D4, G30).
// Port of indexer/utils/hashing.py: md5_of returns the lowercase 32-hex MD5
// of a file's current content — identical to hashlib.md5(...).hexdigest() —
// or std::nullopt when the file is missing/unreadable. The algorithm and
// format are frozen: every existing index.db's staleness data depends on them.
#pragma once

#include <cstddef>
#include <optional>
#include <string>

namespace cidx {

// MD5 hex digest of an in-memory buffer (lowercase 32-hex).
std::string md5_hex(const void *data, std::size_t len);
std::string md5_hex(const std::string &data);

// MD5 hex digest of a file's content; nullopt if unreadable (G30).
std::optional<std::string> md5_of(const std::string &path);

} // namespace cidx

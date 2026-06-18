// Content hashing for staleness detection (design D4, G30).
// Port of indexer/utils/hashing.py: md5_of returns the lowercase 32-hex MD5
// of a file's current content — identical to hashlib.md5(...).hexdigest() —
// or std::nullopt when the file is missing/unreadable. The algorithm and
// format are frozen: every existing index.db's staleness data depends on them.
#pragma once

#include <cstddef>
#include <optional>
#include <string>
#include <vector>

namespace cidx {

// MD5 hex digest of an in-memory buffer (lowercase 32-hex).
std::string md5_hex(const void *data, std::size_t len);
std::string md5_hex(const std::string &data);

// MD5 hex digest of a file's content; nullopt if unreadable (G30).
std::optional<std::string> md5_of(const std::string &path);

// SHA-1 hex digest of an in-memory buffer (lowercase 40-hex).
// Matches Python hashlib.sha1(data).hexdigest() byte-for-byte.
//
// Doctest (frozen Python value):
//   abspath = "/Users/husam/workspace/qemu-vms/libclang-lab/manifests/calls.c"
//   flags   = {"-std=c11"}
//   driver  = (none)
//   key  = sha1(abspath + "\0" + "\0".join(flags))
//       -> "d6cca25a6ed23cd603c1baefecbc7f67f5435639"
std::string sha1_hex(const std::string &data);

// Cache key: sha1(abspath + "\0" + "\0".join(flags) [+ "\0drv\0" + driver])
// Mirrors Python astcache.cache_key() byte-for-byte (ADR-005 §interchange).
struct AstCacheKey {
  std::string abspath;
  std::vector<std::string> flags;
  std::optional<std::string> driver;
};
std::string sha1_cache_key(const AstCacheKey &k);

// Flags-only hash: sha1("\0".join(flags) [+ "\0drv\0" + driver])
// Mirrors Python astcache.flags_hash().
std::string sha1_flags_hash(const AstCacheKey &k);

} // namespace cidx

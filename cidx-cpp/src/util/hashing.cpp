#include "util/hashing.hpp"

#include <cstdio>

extern "C" {
#include "md5/md5.h" // vendored public-domain RFC 1321 implementation (D4)
#include "sha1/sha1.h" // vendored public-domain SHA-1 (ADR-006 M5)
}

namespace cidx {

namespace {

std::string digest_to_hex_16(const unsigned char digest[16]) {
  static const char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(32);
  for (int i = 0; i < 16; ++i) {
    out += kHex[digest[i] >> 4];
    out += kHex[digest[i] & 0x0F];
  }
  return out;
}

std::string digest_to_hex_20(const unsigned char digest[20]) {
  static const char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(40);
  for (int i = 0; i < 20; ++i) {
    out += kHex[digest[i] >> 4];
    out += kHex[digest[i] & 0x0F];
  }
  return out;
}

std::string digest_to_hex(const unsigned char digest[16]) {
  return digest_to_hex_16(digest);
}

} // namespace

std::string md5_hex(const void *data, std::size_t len) {
  MD5_CTX ctx;
  MD5_Init(&ctx);
  MD5_Update(&ctx, data, static_cast<unsigned long>(len));
  unsigned char digest[16];
  MD5_Final(digest, &ctx);
  return digest_to_hex(digest);
}

std::string md5_hex(const std::string &data) {
  return md5_hex(data.data(), data.size());
}

std::optional<std::string> md5_of(const std::string &path) {
  std::FILE *fh = std::fopen(path.c_str(), "rb");
  if (fh == nullptr) {
    return std::nullopt;
  }
  MD5_CTX ctx;
  MD5_Init(&ctx);
  char buf[65536];
  std::size_t got = 0;
  while ((got = std::fread(buf, 1, sizeof buf, fh)) > 0) {
    MD5_Update(&ctx, buf, static_cast<unsigned long>(got));
  }
  const bool failed = std::ferror(fh) != 0; // e.g. EISDIR — OSError parity
  std::fclose(fh);
  if (failed) {
    return std::nullopt;
  }
  unsigned char digest[16];
  MD5_Final(digest, &ctx);
  return digest_to_hex(digest);
}

// ---------------------------------------------------------------------------
// SHA-1 (ADR-006 M5) — byte-identical to Python hashlib.sha1

std::string sha1_hex(const std::string &data) {
  SHA1_CTX ctx;
  SHA1_Init(&ctx);
  SHA1_Update(&ctx, data.data(), static_cast<unsigned long>(data.size()));
  unsigned char digest[20];
  SHA1_Final(digest, &ctx);
  return digest_to_hex_20(digest);
}

// Helper: append flags (joined by \0) and optional driver to SHA-1 context.
static void sha1_add_flags(SHA1_CTX &ctx,
                           const std::vector<std::string> &flags,
                           const std::optional<std::string> &driver) {
  for (std::size_t i = 0; i < flags.size(); ++i) {
    if (i > 0) {
      const char sep = '\0';
      SHA1_Update(&ctx, &sep, 1);
    }
    SHA1_Update(&ctx, flags[i].data(),
                static_cast<unsigned long>(flags[i].size()));
  }
  if (driver) {
    // Python: b"\0drv\0" + driver.encode()
    const char prefix[] = "\0drv\0";
    SHA1_Update(&ctx, prefix, 5); // 5 bytes: \0 d r v \0
    SHA1_Update(&ctx, driver->data(),
                static_cast<unsigned long>(driver->size()));
  }
}

std::string sha1_cache_key(const AstCacheKey &k) {
  SHA1_CTX ctx;
  SHA1_Init(&ctx);
  // abspath + "\0"
  SHA1_Update(&ctx, k.abspath.data(),
              static_cast<unsigned long>(k.abspath.size()));
  const char sep = '\0';
  SHA1_Update(&ctx, &sep, 1);
  // flags [+ "\0drv\0" + driver]
  sha1_add_flags(ctx, k.flags, k.driver);
  unsigned char digest[20];
  SHA1_Final(digest, &ctx);
  return digest_to_hex_20(digest);
}

std::string sha1_flags_hash(const AstCacheKey &k) {
  SHA1_CTX ctx;
  SHA1_Init(&ctx);
  sha1_add_flags(ctx, k.flags, k.driver);
  unsigned char digest[20];
  SHA1_Final(digest, &ctx);
  return digest_to_hex_20(digest);
}

} // namespace cidx

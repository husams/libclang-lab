#include "util/hashing.hpp"

#include <cstdio>

extern "C" {
#include "md5/md5.h" // vendored public-domain RFC 1321 implementation (D4)
}

namespace cidx {

namespace {

std::string digest_to_hex(const unsigned char digest[16]) {
  static const char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(32);
  for (int i = 0; i < 16; ++i) {
    out += kHex[digest[i] >> 4];
    out += kHex[digest[i] & 0x0F];
  }
  return out;
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

} // namespace cidx

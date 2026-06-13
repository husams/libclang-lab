// hashing_test — md5_hex / md5_of vs hashlib.md5().hexdigest() (D4, G30).
//
// Expected digests generated once with:
//   python3 -c "import hashlib
//   for s in [b'', b'abc', b'The quick brown fox jumps over the lazy dog',
//             b'cidx'*100, b'hello world\n']:
//       print(hashlib.md5(s).hexdigest())"
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <cstdio>
#include <string>

#include <sys/stat.h>
#include <unistd.h>

#include "util/hashing.hpp"

namespace {

std::string make_temp_dir() {
  char tmpl[] = "/tmp/cidx_hashing_XXXXXX";
  char *d = ::mkdtemp(tmpl);
  REQUIRE(d != nullptr);
  return d;
}

void write_file(const std::string &path, const std::string &content) {
  std::FILE *fh = std::fopen(path.c_str(), "wb");
  REQUIRE(fh != nullptr);
  REQUIRE(std::fwrite(content.data(), 1, content.size(), fh) == content.size());
  std::fclose(fh);
}

} // namespace

TEST_CASE("md5_hex matches hashlib.md5().hexdigest() pinned values") {
  CHECK(cidx::md5_hex(std::string("")) == "d41d8cd98f00b204e9800998ecf8427e");
  CHECK(cidx::md5_hex(std::string("abc")) ==
        "900150983cd24fb0d6963f7d28e17f72");
  CHECK(cidx::md5_hex(
            std::string("The quick brown fox jumps over the lazy dog")) ==
        "9e107d9d372bb6826bd81d3542a419d6");
  // Multi-block input (400 bytes > one 64-byte MD5 block).
  std::string big;
  for (int i = 0; i < 100; ++i) {
    big += "cidx";
  }
  CHECK(cidx::md5_hex(big) == "b2fd6060116bca4054f70e580008dd65");
}

TEST_CASE("md5_hex is lowercase 32-hex") {
  const std::string hex = cidx::md5_hex(std::string("abc"));
  CHECK(hex.size() == 32);
  for (const char c : hex) {
    CHECK(((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')));
  }
}

TEST_CASE("md5_of hashes file content") {
  const std::string dir = make_temp_dir();
  const std::string path = dir + "/sample.txt";
  write_file(path, "hello world\n");
  auto digest = cidx::md5_of(path);
  REQUIRE(digest.has_value());
  CHECK(*digest == "6f5902ac237024bdd0c176cb93063dc4");

  SUBCASE("empty file") {
    const std::string empty = dir + "/empty.txt";
    write_file(empty, "");
    auto d = cidx::md5_of(empty);
    REQUIRE(d.has_value());
    CHECK(*d == "d41d8cd98f00b204e9800998ecf8427e");
  }
}

TEST_CASE("md5_of -> nullopt on missing/unreadable files (G30)") {
  const std::string dir = make_temp_dir();

  SUBCASE("missing file") {
    CHECK_FALSE(cidx::md5_of(dir + "/does-not-exist").has_value());
  }
  SUBCASE("directory is unreadable as a file") {
    CHECK_FALSE(cidx::md5_of(dir).has_value());
  }
  SUBCASE("permission-denied file") {
    if (::geteuid() == 0) {
      return; // root ignores mode bits — cannot exercise this path
    }
    const std::string locked = dir + "/locked.txt";
    write_file(locked, "secret");
    REQUIRE(::chmod(locked.c_str(), 0) == 0);
    CHECK_FALSE(cidx::md5_of(locked).has_value());
    ::chmod(locked.c_str(), 0600); // allow cleanup
  }
}

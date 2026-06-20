#include "util/files.hpp"

#include <algorithm>
#include <array>
#include <cctype>

#include "storage/storage.hpp"
#include "util/hashing.hpp"
#include "util/pathutil.hpp"

namespace cidx {
namespace files {

bool is_header(const std::string &path) {
  // Extension after the last '.', but only within the final path segment.
  const auto slash = path.find_last_of("/\\");
  const auto dot = path.find_last_of('.');
  if (dot == std::string::npos || (slash != std::string::npos && dot < slash)) {
    return true; // no extension (e.g. a bare libstdc++ header)
  }
  std::string ext = path.substr(dot);
  std::transform(ext.begin(), ext.end(), ext.begin(),
                 [](unsigned char c) { return std::tolower(c); });
  static const std::array<const char *, 10> kHeaderSuffixes = {
      ".h", ".hh", ".hpp", ".hxx", ".h++", ".hp", ".inc", ".tcc", ".ipp",
      ".ixx"};
  return std::any_of(kHeaderSuffixes.begin(), kHeaderSuffixes.end(),
                     [&](const char *s) { return ext == s; });
}

IndexStatus index_status(const File &rec, const std::string &path) {
  if (!rec.indexed) {
    return IndexStatus::kNotIndexed;
  }
  if (!rec.md5) {
    return IndexStatus::kNoStoredMd5;
  }
  if (rec.md5 != md5_of(path)) { // unreadable file -> nullopt -> mismatch
    return IndexStatus::kMd5Mismatch;
  }
  return IndexStatus::kOk;
}

IndexStatus index_status(Storage &db, const std::string &abs_path) {
  const auto rec = db.get_file(abs_path);
  if (!rec) {
    return IndexStatus::kNotIndexed;
  }
  return index_status(*rec, abs_path);
}

const char *index_status_reason(IndexStatus status) {
  switch (status) {
  case IndexStatus::kNotIndexed:
    return "no (never indexed)";
  case IndexStatus::kNoStoredMd5:
    return "no (no stored md5)";
  case IndexStatus::kMd5Mismatch:
    return "no (content changed since import)";
  case IndexStatus::kOk:
    return "yes (indexed, md5 match)";
  }
  return "no (never indexed)"; // unreachable; silences -Wreturn-type
}

std::string resolve_file_arg(const std::string &arg,
                             const std::optional<std::string> &root) {
  if (pathutil::isabs(arg)) {
    return pathutil::abspath(arg);
  }
  return pathutil::abspath(
      pathutil::join(root ? *root : pathutil::getcwd(), arg));
}

} // namespace files
} // namespace cidx

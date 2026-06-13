#include "util/files.hpp"

#include "storage/storage.hpp"
#include "util/hashing.hpp"
#include "util/pathutil.hpp"

namespace cidx {
namespace files {

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

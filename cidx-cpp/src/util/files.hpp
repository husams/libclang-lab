// File-argument resolution and index-state logic — port of
// indexer/utils/files.py (design §5.9). The CLI skip decision is md5-ONLY
// (analysis §4): mtime is stored metadata but never consulted here.
#pragma once

#include <optional>
#include <string>

#include "storage/records.hpp"

namespace cidx {

class Storage;

namespace files {

// Exactly the four outcomes of files.py:20-28, in check order.
enum class IndexStatus {
  kNotIndexed,  // indexed flag not set (never indexed)
  kNoStoredMd5, // indexed but no md5 captured -> treat as never indexed
  kMd5Mismatch, // content changed since import
  kOk,          // indexed and md5 current
};

// Index state for an already-fetched file row vs the file's current content.
IndexStatus index_status(const File &rec, const std::string &path);

// Convenience: looks the row up by absolute path first; a missing row is
// kNotIndexed (design §5.9 signature).
IndexStatus index_status(Storage &db, const std::string &abs_path);

// The human reason string the Python CLI prints for each status.
const char *index_status_reason(IndexStatus status);

// True for a header (by extension, or no extension e.g. a bare libstdc++
// header). Mirrors cli.py _is_header: a header is indexed via its including
// TU's index_headers() pass, never parsed standalone; a TU source is indexed
// even when its compile command sanitizes to no flags.
bool is_header(const std::string &path);

// Absolute path for a CLI file argument: relative paths resolve against
// `root` (the --source component path) when given, else against the CWD.
std::string
resolve_file_arg(const std::string &arg,
                 const std::optional<std::string> &root = std::nullopt);

} // namespace files
} // namespace cidx

// Python os.path (posixpath) semantics, reimplemented (design D11).
// std::filesystem's lexically_normal disagrees with posixpath on edge cases
// that are STORED in the DB (directory.path values, _dir_scope_sql), so all
// path logic in cidx goes through this module. POSIX-only target; '/' is the
// only separator.
//
// Parity notes (each behavior pinned by tests against Python-generated
// tables):
//   * normpath: pure-lexical collapse of '.', '..', '//'; "" -> "."; never a
//     trailing separator; a leading "//" (exactly two) is preserved.
//   * abspath(p) = normpath(join(getcwd(), p)) for relative p.
//   * relpath synthesizes ".." across distinct subtrees; empty path throws.
//   * expanduser: "~"/"~user" via $HOME / passwd; unknown user -> unchanged.
//   * join: an absolute component resets the result (posixpath.join).
#pragma once

#include <string>
#include <utility>

namespace cidx {
namespace pathutil {

bool isabs(const std::string &path);
std::string normpath(const std::string &path);
std::string abspath(const std::string &path);

// Throws CidxError when path is empty (ValueError parity).
std::string relpath(const std::string &path, const std::string &start = ".");

std::string expanduser(const std::string &path);

// posixpath.split: (head, tail); head keeps no trailing '/' unless it is all
// slashes.
std::pair<std::string, std::string> split(const std::string &path);
std::string dirname(const std::string &path);
std::string basename(const std::string &path);

std::string getcwd(); // throws CidxError on failure

namespace detail {
void join_one(std::string &path, const std::string &part);
} // namespace detail

// posixpath.join(a, b, ...) — variadic fold over the Python loop.
template <typename... Parts>
std::string join(std::string path, const Parts &...parts) {
  (detail::join_one(path, parts), ...);
  return path;
}

} // namespace pathutil
} // namespace cidx

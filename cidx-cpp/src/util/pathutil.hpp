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
//
// Portable-paths resolution chain (design portable_paths_contract.md §1):
//   * expandvars: $VAR / ${VAR} — exact port of CPython posixpath.expandvars.
//     Undefined var -> left literal; $$ not special.
//   * LabelResolver / label_expand: <name> placeholder substitution.
//   * resolve_fs_path: label_expand -> expandvars -> expanduser -> normpath.
#pragma once

#include <functional>
#include <optional>
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

// ---------------------------------------------------------------------------
// Portable-paths resolution chain (design §1, portable_paths_contract.md)
// ---------------------------------------------------------------------------

// Exact port of CPython posixpath.expandvars (string branch).
// Semantics: $VAR and ${VAR} replaced from the process environment; undefined
// variable -> left literal; $$ is NOT special (first $ is a literal prefix
// outside the match; $FOO expands). See §1.1 for the full parity table.
std::string expandvars(const std::string &path);

// Label resolver: bundles the registry lookup and the autoderive policy.
// A default-constructed LabelResolver has no lookup (always misses) and
// autoderive=true — matching Python label_expand(token, lookup=None).
struct LabelResolver {
  // Returns the stored path for a label name, or nullopt on miss.
  std::function<std::optional<std::string>(const std::string &)> lookup;
  bool autoderive = true; // autoderive "/" + name.replace("-", "/") on miss

  LabelResolver() = default;
  LabelResolver(
      std::function<std::optional<std::string>(const std::string &)> lk,
      bool ad = true)
      : lookup(std::move(lk)), autoderive(ad) {}
};

// Replace every <name> occurrence inside token using the resolver (§1.2).
// Registry hit -> its stored path. Else autoderive "/" + name.replace("-","/"
// when autoderive=true. Else leave <name> literal.
std::string label_expand(const std::string &token, const LabelResolver &labels);

// Full resolution chain (§1.3): label_expand -> expandvars -> expanduser ->
// normpath. Does NOT call abspath (caller applies when absolute path required).
std::string resolve_fs_path(const std::string &stored,
                            const LabelResolver &labels);

// Overload with empty resolver (autoderive only; no registry lookup).
// Equivalent to resolve_fs_path(stored, LabelResolver{}).
std::string resolve_fs_path(const std::string &stored);

} // namespace pathutil
} // namespace cidx

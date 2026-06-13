// Git repository discovery — port of indexer/utils/repo.py (design D10).
// No shelling out to `git`: repo_name reads .git/config with a tiny INI
// scanner that matches configparser's behavior on the one section we read.
#pragma once

#include <optional>
#include <string>

namespace cidx {
namespace repo {

// Walk up from `path` looking for a directory containing `.git`; nullopt when
// the filesystem root is reached without finding one.
std::optional<std::string> git_root(const std::string &path);

// Component name: the `[remote "origin"]` url basename from .git/config with
// a trailing '.git' stripped; falls back to basename(root) when the config or
// the origin url is missing/unreadable.
std::string repo_name(const std::string &root);

} // namespace repo
} // namespace cidx

// Git repository discovery — port of indexer/utils/repo.py (design D10).
// No shelling out to `git`: repo_name reads the shared .git/config with a tiny
// INI scanner that matches configparser's behavior on the one section we read.
// Worktree-aware: a linked worktree's `.git` is a FILE (`gitdir: …`) whose
// gitdir holds a `commondir` pointing at the main repo's `.git`; the helpers
// follow that chain so a worktree resolves to its main repo's name/remote.
#pragma once

#include <optional>
#include <string>

namespace cidx {
namespace repo {

// Walk up from `path` looking for a `.git` directory OR file (worktree); nullopt
// when the filesystem root is reached without finding one.
std::optional<std::string> git_root(const std::string &path);

// Component name: the `[remote "origin"]` url basename from the shared config
// with a trailing '.git' stripped; else the main working-tree directory name
// (so a worktree shares its main repo's name); else basename(root).
std::string repo_name(const std::string &root);

// The raw `[remote "origin"]` url from the repo's shared config (whitespace-
// stripped, no basename/'.git' trimming; worktree-aware), or nullopt when
// unavailable. Mirrors Python indexer.utils.repo.git_remote_url.
std::optional<std::string> git_remote_url(const std::string &root);

} // namespace repo
} // namespace cidx

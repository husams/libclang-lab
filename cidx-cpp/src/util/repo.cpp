#include "util/repo.hpp"

#include <cctype>
#include <filesystem>
#include <fstream>
#include <string>

#include "util/pathutil.hpp"

namespace cidx {
namespace repo {

namespace {

std::string strip(const std::string &s) {
  std::size_t begin = 0;
  std::size_t end = s.size();
  while (begin < end && std::isspace(static_cast<unsigned char>(s[begin]))) {
    ++begin;
  }
  while (end > begin && std::isspace(static_cast<unsigned char>(s[end - 1]))) {
    --end;
  }
  return s.substr(begin, end - begin);
}

std::string lower(std::string s) {
  for (char &c : s) {
    c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  return s;
}

// First line of a file, whitespace-stripped; nullopt when unreadable. Used for
// the worktree `.git` (`gitdir: …`) and `commondir` pointer files, both of which
// are single-line. Mirrors Python's `open(p).read().strip()` for those files.
std::optional<std::string> first_line(const std::string &p) {
  std::ifstream in(p);
  if (!in) {
    return std::nullopt;
  }
  std::string line;
  std::getline(in, line);
  return strip(line);
}

// Resolve <root>/.git to the repo's git directory: a normal checkout has `.git`
// as a directory (returned as-is); a linked worktree has `.git` as a file
// `gitdir: <path>` pointing at its private gitdir (`…/.git/worktrees/<name>`).
std::optional<std::string> git_dir(const std::string &root) {
  const std::string dot = pathutil::join(root, ".git");
  std::error_code ec;
  if (std::filesystem::is_directory(dot, ec)) {
    return dot;
  }
  if (std::filesystem::is_regular_file(dot, ec)) {
    const std::optional<std::string> line = first_line(dot);
    if (line && line->rfind("gitdir:", 0) == 0) {
      std::string gd = strip(line->substr(std::string("gitdir:").size()));
      if (!pathutil::isabs(gd)) {
        gd = pathutil::normpath(pathutil::join(root, gd));
      }
      return gd;
    }
  }
  return std::nullopt;
}

// The shared git directory holding `config`: the `.git` dir for a normal repo;
// for a worktree, the main repo's `.git`, reached via the `commondir` file in
// the worktree's private gitdir.
std::optional<std::string> git_common_dir(const std::string &root) {
  const std::optional<std::string> gd = git_dir(root);
  if (!gd) {
    return std::nullopt;
  }
  const std::string commondir = pathutil::join(*gd, "commondir");
  std::error_code ec;
  if (std::filesystem::is_regular_file(commondir, ec)) {
    const std::optional<std::string> line = first_line(commondir);
    if (!line) {
      return gd;
    }
    std::string cd = *line;
    if (!pathutil::isabs(cd)) {
      cd = pathutil::normpath(pathutil::join(*gd, cd));
    }
    return cd;
  }
  return gd;
}

} // namespace

std::optional<std::string> git_root(const std::string &path) {
  std::string cur = pathutil::abspath(path);
  while (true) {
    std::error_code ec;
    const std::string dot = pathutil::join(cur, ".git");
    if (std::filesystem::is_directory(dot, ec) ||
        std::filesystem::is_regular_file(dot, ec)) {
      return cur;
    }
    const std::string parent = pathutil::dirname(cur);
    if (parent == cur) {
      return std::nullopt;
    }
    cur = parent;
  }
}

std::optional<std::string> git_remote_url(const std::string &root) {
  const std::optional<std::string> common = git_common_dir(root);
  if (!common) {
    return std::nullopt;
  }
  std::ifstream in(pathutil::join(*common, "config"));
  if (!in) {
    return std::nullopt;
  }
  bool in_origin = false;
  std::string line;
  while (std::getline(in, line)) {
    const std::string s = strip(line);
    if (s.empty() || s[0] == '#' || s[0] == ';') {
      continue;
    }
    if (s[0] == '[') {
      const std::size_t close = s.find(']');
      in_origin = close != std::string::npos &&
                  s.substr(1, close - 1) == "remote \"origin\"";
      continue;
    }
    if (!in_origin) {
      continue;
    }
    // configparser default delimiters are '=' and ':'; keys are lowercased.
    const std::size_t delim = s.find_first_of("=:");
    if (delim == std::string::npos ||
        lower(strip(s.substr(0, delim))) != "url") {
      continue;
    }
    return strip(s.substr(delim + 1));
  }
  return std::nullopt;
}

std::string repo_name(const std::string &root) {
  const std::optional<std::string> raw = git_remote_url(root);
  if (raw) {
    std::string url = *raw;
    while (!url.empty() && url.back() == '/') {
      url.pop_back(); // url.rstrip('/')
    }
    const std::size_t slash = url.rfind('/');
    std::string name = slash == std::string::npos ? url : url.substr(slash + 1);
    if (name.size() >= 4 && name.compare(name.size() - 4, 4, ".git") == 0) {
      name = name.substr(0, name.size() - 4);
    }
    return name;
  }
  // No origin url: the main checkout is the parent of the shared `.git` dir, so a
  // worktree shares its main repo's directory name. Falls back to root basename.
  const std::optional<std::string> common = git_common_dir(root);
  if (common) {
    return pathutil::basename(pathutil::dirname(pathutil::abspath(*common)));
  }
  return pathutil::basename(root);
}

} // namespace repo
} // namespace cidx

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

} // namespace

std::optional<std::string> git_root(const std::string &path) {
  std::string cur = pathutil::abspath(path);
  while (true) {
    std::error_code ec;
    if (std::filesystem::is_directory(pathutil::join(cur, ".git"), ec)) {
      return cur;
    }
    const std::string parent = pathutil::dirname(cur);
    if (parent == cur) {
      return std::nullopt;
    }
    cur = parent;
  }
}

std::string repo_name(const std::string &root) {
  const std::string fallback = pathutil::basename(root);
  std::ifstream in(pathutil::join(root, ".git", "config"));
  if (!in) {
    return fallback;
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
    std::string url = strip(s.substr(delim + 1));
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
  return fallback;
}

} // namespace repo
} // namespace cidx

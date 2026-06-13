#include "util/pathutil.hpp"

#include <cerrno>
#include <cstdlib>
#include <vector>

#include <pwd.h>
#include <unistd.h>

#include "util/errors.hpp"

namespace cidx {
namespace pathutil {

namespace {

// path.split('/') — including empty components.
std::vector<std::string> split_components(const std::string &path) {
  std::vector<std::string> comps;
  std::size_t pos = 0;
  while (true) {
    const std::size_t next = path.find('/', pos);
    if (next == std::string::npos) {
      comps.push_back(path.substr(pos));
      break;
    }
    comps.push_back(path.substr(pos, next - pos));
    pos = next + 1;
  }
  return comps;
}

// [x for x in abspath(p).split('/') if x]
std::vector<std::string> abs_parts(const std::string &path) {
  std::vector<std::string> parts;
  for (auto &c : split_components(abspath(path))) {
    if (!c.empty()) {
      parts.push_back(std::move(c));
    }
  }
  return parts;
}

} // namespace

bool isabs(const std::string &path) { return !path.empty() && path[0] == '/'; }

std::string normpath(const std::string &path) {
  // Port of posixpath.normpath.
  if (path.empty()) {
    return ".";
  }
  int initial_slashes = (path[0] == '/') ? 1 : 0;
  // POSIX allows one or two initial slashes; three or more -> one.
  if (initial_slashes == 1 && path.size() >= 2 && path[1] == '/' &&
      !(path.size() >= 3 && path[2] == '/')) {
    initial_slashes = 2;
  }
  std::vector<std::string> new_comps;
  for (auto &comp : split_components(path)) {
    if (comp.empty() || comp == ".") {
      continue;
    }
    if (comp != ".." || (initial_slashes == 0 && new_comps.empty()) ||
        (!new_comps.empty() && new_comps.back() == "..")) {
      new_comps.push_back(std::move(comp));
    } else if (!new_comps.empty()) {
      new_comps.pop_back();
    }
    // ".." at an absolute root is dropped.
  }
  std::string out(static_cast<std::size_t>(initial_slashes), '/');
  for (std::size_t i = 0; i < new_comps.size(); ++i) {
    if (i != 0) {
      out += '/';
    }
    out += new_comps[i];
  }
  return out.empty() ? "." : out;
}

std::string abspath(const std::string &path) {
  if (isabs(path)) {
    return normpath(path);
  }
  return normpath(join(getcwd(), path));
}

std::string relpath(const std::string &path, const std::string &start) {
  if (path.empty()) {
    throw CidxError("relpath: no path specified");
  }
  const std::vector<std::string> start_list = abs_parts(start);
  const std::vector<std::string> path_list = abs_parts(path);
  std::size_t common = 0;
  while (common < start_list.size() && common < path_list.size() &&
         start_list[common] == path_list[common]) {
    ++common;
  }
  std::string rel;
  for (std::size_t k = common; k < start_list.size(); ++k) {
    detail::join_one(rel, "..");
  }
  for (std::size_t k = common; k < path_list.size(); ++k) {
    detail::join_one(rel, path_list[k]);
  }
  return rel.empty() ? "." : rel;
}

std::string expanduser(const std::string &path) {
  // Port of posixpath.expanduser.
  if (path.empty() || path[0] != '~') {
    return path;
  }
  std::size_t i = path.find('/', 1);
  if (i == std::string::npos) {
    i = path.size();
  }
  std::string userhome;
  if (i == 1) {
    const char *home = std::getenv("HOME");
    if (home != nullptr) {
      userhome = home;
    } else {
      const struct passwd *pw = ::getpwuid(::getuid());
      if (pw == nullptr) {
        return path;
      }
      userhome = pw->pw_dir;
    }
  } else {
    const std::string user = path.substr(1, i - 1);
    const struct passwd *pw = ::getpwnam(user.c_str());
    if (pw == nullptr) {
      return path; // KeyError parity: unknown user -> path unchanged
    }
    userhome = pw->pw_dir;
  }
  while (!userhome.empty() && userhome.back() == '/') {
    userhome.pop_back();
  }
  const std::string result = userhome + path.substr(i);
  return result.empty() ? "/" : result;
}

std::pair<std::string, std::string> split(const std::string &path) {
  const std::size_t i = path.rfind('/');
  if (i == std::string::npos) {
    return {"", path};
  }
  std::string head = path.substr(0, i + 1);
  std::string tail = path.substr(i + 1);
  if (head.find_first_not_of('/') != std::string::npos) {
    const std::size_t end = head.find_last_not_of('/');
    head.erase(end + 1);
  }
  return {std::move(head), std::move(tail)};
}

std::string dirname(const std::string &path) { return split(path).first; }

std::string basename(const std::string &path) { return split(path).second; }

std::string getcwd() {
  std::vector<char> buf(256);
  while (::getcwd(buf.data(), buf.size()) == nullptr) {
    if (errno != ERANGE) {
      throw CidxError("getcwd failed");
    }
    buf.resize(buf.size() * 2);
  }
  return std::string(buf.data());
}

namespace detail {

void join_one(std::string &path, const std::string &part) {
  // Body of posixpath.join's loop.
  if (!part.empty() && part[0] == '/') {
    path = part;
  } else if (path.empty() || path.back() == '/') {
    path += part;
  } else {
    path += '/';
    path += part;
  }
}

} // namespace detail

} // namespace pathutil
} // namespace cidx

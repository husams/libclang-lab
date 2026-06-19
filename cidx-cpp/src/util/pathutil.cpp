#include "util/pathutil.hpp"

#include <cerrno>
#include <cstdlib>
#include <regex>
#include <string>
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

// ---------------------------------------------------------------------------
// expandvars — exact port of CPython posixpath.expandvars (string branch)
// Contract: portable_paths_contract.md §1.1
// ---------------------------------------------------------------------------

std::string expandvars(const std::string &path) {
  // Fast-path: if no '$' present, return unchanged (CPython: 'if '$' not in
  // path: return path'). No regex engine touched.
  if (path.find('$') == std::string::npos) {
    return path;
  }

  // Pattern: \$([A-Za-z0-9_]+|\{[^}]*\}?)
  // \w written as explicit [A-Za-z0-9_] (never locale-tainted stdlib \w).
  static const std::regex kVarRe(R"(\$([A-Za-z0-9_]+|\{[^}]*\}?))");

  std::string out;
  out.reserve(path.size());
  std::size_t pos = 0;

  // Hand-rolled sregex_iterator walk — see contract §1.1 why regex_replace
  // cannot implement the "leave literal" branch correctly.
  const auto begin =
      std::sregex_iterator(path.begin(), path.end(), kVarRe);
  const auto end = std::sregex_iterator();

  for (auto it = begin; it != end; ++it) {
    const std::smatch &m = *it;
    // Append literal text before this match.
    const auto match_pos = static_cast<std::size_t>(m.position());
    out.append(path, pos, match_pos - pos);

    // group(1) = the capture (either \w+ or \{[^}]*\}?)
    const std::string name = m[1].str();
    std::string lookup_name = name;
    bool unterminated_brace = false;

    if (!name.empty() && name[0] == '{') {
      if (name.back() != '}') {
        // Unterminated brace — e.g. "${FOO" — leave literal.
        unterminated_brace = true;
      } else {
        // Strip braces: {FOO} -> FOO
        lookup_name = name.substr(1, name.size() - 2);
      }
    }

    if (unterminated_brace) {
      // Leave the entire match text literal.
      out += m[0].str();
    } else {
      const char *val = std::getenv(lookup_name.c_str());
      if (val != nullptr) {
        out += val; // defined: substitute value (empty string if "")
      } else {
        out += m[0].str(); // undefined: leave literal (KeyError parity)
      }
    }

    pos = static_cast<std::size_t>(m.position()) +
          static_cast<std::size_t>(m.length());
  }

  // Append tail after the last match.
  out.append(path, pos, path.size() - pos);
  return out;
}

// ---------------------------------------------------------------------------
// label_expand — §1.2
// ---------------------------------------------------------------------------

std::string label_expand(const std::string &token,
                         const LabelResolver &labels) {
  // Fast-path: no '<' means no placeholder.
  if (token.find('<') == std::string::npos) {
    return token;
  }

  // Match <name> where name is any non-<> run.
  // We walk manually to allow overlapping-free replacement.
  std::string out;
  out.reserve(token.size());
  std::size_t pos = 0;

  while (pos < token.size()) {
    const std::size_t open = token.find('<', pos);
    if (open == std::string::npos) {
      out.append(token, pos, token.size() - pos);
      break;
    }
    // Append literal up to '<'.
    out.append(token, pos, open - pos);

    const std::size_t close = token.find('>', open + 1);
    if (close == std::string::npos) {
      // No closing '>': append rest literally.
      out.append(token, open, token.size() - open);
      pos = token.size();
      break;
    }

    const std::string name = token.substr(open + 1, close - open - 1);

    // 1. Registry hit.
    bool resolved = false;
    if (labels.lookup) {
      const auto hit = labels.lookup(name);
      if (hit.has_value()) {
        out += *hit;
        resolved = true;
      }
    }

    // 2. Autoderive.
    if (!resolved && labels.autoderive && !name.empty()) {
      std::string derived = "/";
      for (char c : name) {
        derived += (c == '-') ? '/' : c;
      }
      out += derived;
      resolved = true;
    }

    // 3. Leave literal.
    if (!resolved) {
      out += '<';
      out += name;
      out += '>';
    }

    pos = close + 1;
  }

  return out;
}

// ---------------------------------------------------------------------------
// resolve_fs_path — §1.3
// ---------------------------------------------------------------------------

std::string resolve_fs_path(const std::string &stored,
                            const LabelResolver &labels) {
  // Order is load-bearing: labels -> envvars -> ~ -> normpath.
  std::string s = label_expand(stored, labels);
  s = expandvars(s);
  s = expanduser(s);
  s = normpath(s);
  return s;
}

std::string resolve_fs_path(const std::string &stored) {
  return resolve_fs_path(stored, LabelResolver{});
}

} // namespace pathutil
} // namespace cidx

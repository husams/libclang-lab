#include "compiledb/compiledb.hpp"

#include <algorithm>
#include <cstring>
#include <functional>
#include <optional>
#include <regex>
#include <set>
#include <string>
#include <utility>

#include "clangx/libclang.hpp"
#include "util/errors.hpp"
#include "util/pathutil.hpp"

namespace cidx {
namespace {

// Frozen drop sets (compiledb.py, G10). Rationale baked into the Python
// comments: -M* writes build artifacts into dirs that don't exist outside a
// real build (fatal "error opening '...'"); -Werror promotes warnings gcc
// never emitted into clang error diagnostics. We also drop everything that
// only affects LINKING, CODEGEN-output, or the MODULE/diagnostic CACHE — none
// of it influences the AST a libclang *parse* produces. Header-search flags
// (-nostdinc / -nostdinc++) and preprocessor-affecting flags (-pthread, -fPIC)
// are NOT linker flags and are deliberately KEPT.
const std::set<std::string> kDrop = {
    "-c",       "--",
    // dependency generation
    "-M",       "-MM",      "-MD",      "-MMD", "-MG", "-MP", "-MV",
    // warnings-as-errors
    "-Werror",  "-pedantic-errors",
    // link-stage modes (no effect on parsing)
    "-shared",  "-static",  "-rdynamic", "-pie", "-no-pie", "-s", "-pipe",
    "-nostdlib", "-nodefaultlibs", "-nostartfiles",
    "-static-libgcc", "-shared-libgcc", "-static-libstdc++",
};
const std::set<std::string> kDropWithArg = {
    "-o", "-MF", "-MT", "-MQ", "-dependency-file", "--serialize-diagnostics",
    // linker options that take a separate argument
    "-Xlinker", "-T", "-L", "-l",
};
const char *const kDropPrefix[] = {
    "-Werror=", // -Werror=return-type: keep it a plain warning
    "-Wp,-M",   // -Wp,-MD,<file> / -Wp,-MMD,<file>
    "-MF",      // glued forms: -MF<file> etc.
    "-MT",
    "-MQ",
    // linker / library / cache (glued forms)
    "-l",     // -lfoo  (no frontend flag starts with -l)
    "-L",     // -L/usr/lib (no frontend flag starts with -L)
    "-Wl,",   // linker passthrough
    "-Wa,",   // assembler passthrough
    "-fuse-ld=",            // linker selection
    "-fmodules-cache-path=", // module/diagnostic cache dir
};

bool has_drop_prefix(const std::string &tok) {
  for (const char *prefix : kDropPrefix) {
    if (tok.starts_with(prefix)) {
      return true;
    }
  }
  return false;
}

// A leading NAME=value token is an env-var assignment; the name must start with
// a letter/underscore so a flag like -DFOO=bar (starts '-') is never matched.
bool is_env_assignment(const std::string &tok) {
  static const std::regex kEnvAssign(R"(^[A-Za-z_][A-Za-z0-9_-]*=)");
  return std::regex_search(tok, kEnvAssign);
}

// Compiler-launcher wrappers that sit before the real compiler (by basename).
const std::set<std::string> kLaunchers = {
    "ccache", "sccache", "distcc", "icecc", "icerun", "env", "time", "nice",
};

bool is_launcher(const std::string &tok) {
  return kLaunchers.count(pathutil::basename(tok)) != 0;
}

// compiledb.py:23-24 — absolute paths returned UNCHANGED (not normalized).
std::string abs_against(const std::string &p, const std::string &base) {
  if (pathutil::isabs(p)) {
    return p;
  }
  return pathutil::normpath(pathutil::join(base, p));
}

std::string to_string(const LibClang &lib, CXString s) {
  return CxString(lib, s).str();
}

} // namespace

std::string CompileDb::db_dir_from_arg(const std::string &db_arg) {
  static const std::string kJson = "compile_commands.json";
  if (db_arg.size() >= kJson.size() &&
      db_arg.compare(db_arg.size() - kJson.size(), kJson.size(), kJson) == 0) {
    std::string dir = db_arg.substr(0, db_arg.size() - kJson.size());
    return dir.empty() ? "." : dir; // `db_path[:-len(...)] or "."`
  }
  return db_arg;
}

std::vector<CompileCommand> CompileDb::load(const std::string &db_arg) {
  LibClang &lib = LibClang::instance();
  lib.load();

  const std::string abs_dir = pathutil::abspath(db_dir_from_arg(db_arg));
  CXCompilationDatabase_Error error = CXCompilationDatabase_NoError;
  CXCompilationDatabase db =
      lib.clang_CompilationDatabase_fromDirectory(abs_dir.c_str(), &error);
  if (error != CXCompilationDatabase_NoError || db == nullptr) {
    if (db != nullptr) {
      lib.clang_CompilationDatabase_dispose(db);
    }
    throw CidxError("could not load compilation database from '" + abs_dir +
                    "'");
  }

  std::vector<CompileCommand> out;
  CXCompileCommands cmds =
      lib.clang_CompilationDatabase_getAllCompileCommands(db);
  if (cmds != nullptr) {
    const unsigned n = lib.clang_CompileCommands_getSize(cmds);
    for (unsigned i = 0; i < n; ++i) {
      CXCompileCommand cmd = lib.clang_CompileCommands_getCommand(cmds, i);
      CompileCommand cc;
      cc.directory =
          to_string(lib, lib.clang_CompileCommand_getDirectory(cmd));
      cc.filename = to_string(lib, lib.clang_CompileCommand_getFilename(cmd));
      std::vector<std::string> raw;
      const unsigned argc = lib.clang_CompileCommand_getNumArgs(cmd);
      raw.reserve(argc);
      for (unsigned j = 0; j < argc; ++j) {
        raw.push_back(to_string(lib, lib.clang_CompileCommand_getArg(cmd, j)));
      }
      cc.driver = driver(raw, cc.directory);
      cc.args = strip_for_libclang(raw, cc.filename, cc.directory);
      out.push_back(std::move(cc));
    }
    lib.clang_CompileCommands_dispose(cmds);
  }
  lib.clang_CompilationDatabase_dispose(db);
  return out;
}

std::vector<std::string>
CompileDb::strip_for_libclang(const std::vector<std::string> &argv,
                              const std::string &filename,
                              const std::string &directory) {
  const std::set<std::string> src = {filename, pathutil::basename(filename)};
  std::vector<std::string> out;
  // Drop the whole command prefix: env-var assignments + launcher wrappers
  // (ccache ...) AND the real compiler token at command_start.
  size_t i = argv.empty() ? 0 : command_start(argv) + 1;
  while (i < argv.size()) {
    const std::string &tok = argv[i++];
    if (kDrop.count(tok) != 0) {
      continue;
    }
    if (kDropWithArg.count(tok) != 0) {
      if (i < argv.size()) {
        ++i; // drop flag + its argument
      }
      continue;
    }
    if (has_drop_prefix(tok)) {
      continue;
    }
    if (src.count(tok) != 0) {
      continue;
    }
    bool matched = false;
    for (const char *flag : {"-I", "-isystem", "-iquote"}) {
      const size_t flen = std::strlen(flag);
      if (tok == flag) { // space form: -I path
        const std::string arg = i < argv.size() ? argv[i++] : std::string();
        out.push_back(flag);
        // Preserve rule (portable-paths §5): if the value contains '<' or '$'
        // it is a template/env-var reference — emit verbatim, not absolutized.
        if (arg.find('<') != std::string::npos ||
            arg.find('$') != std::string::npos) {
          out.push_back(arg);
        } else {
          out.push_back(abs_against(arg, directory));
        }
        matched = true;
        break;
      }
      if (tok.size() > flen && tok.starts_with(flag)) { // glued
        const std::string val = tok.substr(flen);
        // Preserve rule: if the value portion contains '<' or '$', emit the
        // entire original token verbatim (flag + value, already glued).
        if (val.find('<') != std::string::npos ||
            val.find('$') != std::string::npos) {
          out.push_back(tok); // verbatim: e.g. "-I<libfoo-include>/include"
        } else {
          out.push_back(flag + abs_against(val, directory));
        }
        matched = true;
        break;
      }
    }
    if (!matched) {
      out.push_back(tok);
    }
  }
  return out;
}

std::vector<std::string>
CompileDb::sanitize(const std::vector<std::string> &stored) {
  std::vector<std::string> out;
  // Heal a command prefix an older import stored when argv[0] was an env-var
  // assignment rather than the compiler (e.g. ["CCACHE_COMPRESS=1", ...,
  // "ccache", "g++", "-g", ...]). When the first stored token is an env
  // assignment or launcher, drop through the real compiler at command_start.
  size_t i = 0;
  if (!stored.empty() &&
      (is_env_assignment(stored[0]) || is_launcher(stored[0]))) {
    i = command_start(stored) + 1;
  }
  while (i < stored.size()) {
    const std::string &tok = stored[i++];
    if (kDrop.count(tok) != 0) {
      continue;
    }
    if (kDropWithArg.count(tok) != 0) {
      if (i < stored.size()) {
        ++i;
      }
      continue;
    }
    if (has_drop_prefix(tok)) {
      continue;
    }
    out.push_back(tok);
  }
  return out;
}

size_t CompileDb::command_start(const std::vector<std::string> &args) {
  size_t i = 0;
  const size_t n = args.size();
  while (i < n) {
    const std::string &tok = args[i];
    if (is_env_assignment(tok) || is_launcher(tok)) {
      ++i;
      continue;
    }
    break;
  }
  return i < n ? i : 0;
}

std::string CompileDb::driver(const std::vector<std::string> &argv,
                              const std::string &directory) {
  if (argv.empty()) {
    return std::string();
  }
  // Skip env-assignment + launcher prefix; the real compiler is the driver.
  const std::string &argv0 = argv[command_start(argv)];
  if (!argv0.contains('/')) {
    return argv0; // bare name: PATH resolution at parse time
  }
  return abs_against(argv0, directory);
}

// Version detection regex: ^v?[0-9]+([._-][0-9]+)*$
// Explicit [0-9] (not \d) to be locale-independent (contract §2).
std::pair<std::string, std::string>
CompileDb::split_base_version(const std::string &root) {
  // Step 1: normpath.
  const std::string normed = pathutil::normpath(root);
  // Step 2: split → (base, seg).
  const auto [base, seg] = pathutil::split(normed);
  // Step 3: match seg against version regex.
  if (seg.empty() || base.empty() || base == "/") {
    return {normed, ""};
  }
  // ^v?[0-9]+([._-][0-9]+)*$
  static const std::regex kVersionRe(R"(^v?[0-9]+([._\-][0-9]+)*$)");
  if (std::regex_match(seg, kVersionRe)) {
    return {base, seg};
  }
  return {normed, ""};
}

// ---------------------------------------------------------------------------
// Include-path aliasing (v0.6.0) — port of compiledb.py:
//   _map_include_values / resolve_options / build_label_map / alias_options
// ---------------------------------------------------------------------------

namespace {

// The three include flags (compiledb.py _INCLUDE_FLAGS).
const char *const kIncludeFlags[] = {"-I", "-isystem", "-iquote"};

// Apply fn to each include-path value in options (both space and glued forms).
// All other tokens are copied verbatim.
// Mirrors compiledb.py:_map_include_values.
template <typename Fn>
std::vector<std::string>
map_include_values(const std::vector<std::string> &options, Fn fn) {
  std::vector<std::string> out;
  out.reserve(options.size());
  size_t i = 0;
  while (i < options.size()) {
    const std::string &tok = options[i++];
    bool matched = false;
    for (const char *flag : kIncludeFlags) {
      const size_t flen = std::strlen(flag);
      if (tok == flag) { // space form: -I path
        out.push_back(tok);
        if (i < options.size()) {
          out.push_back(fn(options[i++]));
        }
        matched = true;
        break;
      }
      if (tok.size() > flen && tok.starts_with(flag)) { // glued: -Ipath
        out.push_back(std::string(flag) + fn(tok.substr(flen)));
        matched = true;
        break;
      }
    }
    if (!matched) {
      out.push_back(tok);
    }
  }
  return out;
}

} // namespace

// DECODE: resolve <label>/$VAR/~ in include-path values to absolute paths.
std::vector<std::string>
CompileDb::resolve_options(
    const std::vector<std::string> &options,
    std::function<std::optional<std::string>(const std::string &)> lookup,
    bool autoderive) {
  pathutil::LabelResolver resolver(lookup ? std::move(lookup) : nullptr,
                                   autoderive);
  return map_include_values(options, [&](const std::string &val) -> std::string {
    // Only resolve values that look indirected: contain '<', '$', or start
    // with '~'. Plain absolute paths pass through unchanged.
    if (val.find('<') == std::string::npos &&
        val.find('$') == std::string::npos &&
        (val.empty() || val[0] != '~')) {
      return val;
    }
    return pathutil::abspath(pathutil::resolve_fs_path(val, resolver));
  });
}

// Build the encode label map from (name, stored_path, versioned) entries.
std::vector<AliasEntry>
CompileDb::build_label_map(
    const std::vector<AliasEntry> &labels,
    std::function<std::optional<std::string>(const std::string &)> lookup) {
  // NO autoderive — a label's own stored value is taken literally.
  pathutil::LabelResolver resolver(lookup ? std::move(lookup) : nullptr,
                                   /*autoderive=*/false);
  std::vector<AliasEntry> out;
  out.reserve(labels.size());
  for (const auto &[name, stored, versioned] : labels) {
    const std::string rp =
        pathutil::abspath(pathutil::resolve_fs_path(stored, resolver));
    out.emplace_back(name, rp, versioned);
  }
  // Sort: longest resolved path first; on tie, by name ascending.
  std::sort(out.begin(), out.end(),
            [](const AliasEntry &a, const AliasEntry &b) {
              if (std::get<1>(a).size() != std::get<1>(b).size()) {
                return std::get<1>(a).size() > std::get<1>(b).size(); // longer
              }
              return std::get<0>(a) < std::get<0>(b); // then name ascending
            });
  return out;
}

// Longest-match an absolute path against label_map (compiledb.py:match_alias).
std::optional<std::tuple<std::string, std::string, std::string>>
CompileDb::match_alias(const std::string &absval,
                       const std::vector<AliasEntry> &label_map) {
  for (const auto &[name, rp, versioned] : label_map) {
    if (absval == rp || absval.starts_with(rp + "/")) {
      std::string rem = absval.substr(rp.size()); // "" or "/seg/..."
      std::string vseg;
      if (versioned && rem.starts_with("/")) {
        const std::string tail = rem.substr(1);
        const std::size_t slash = tail.find('/');
        const std::string head =
            slash == std::string::npos ? tail : tail.substr(0, slash);
        if (!head.empty() && is_version_segment(head)) {
          vseg = head;
          rem = slash == std::string::npos ? "" : tail.substr(slash);
        }
      }
      return std::make_tuple(name, vseg, rem);
    }
  }
  return std::nullopt;
}

// Yield each include-path value (compiledb.py:include_values).
std::vector<std::string>
CompileDb::include_values(const std::vector<std::string> &options) {
  std::vector<std::string> out;
  size_t i = 0;
  while (i < options.size()) {
    const std::string &tok = options[i++];
    for (const char *flag : kIncludeFlags) {
      const size_t flen = std::strlen(flag);
      if (tok == flag) {
        if (i < options.size()) {
          out.push_back(options[i++]);
        } else {
          out.emplace_back();
        }
        break;
      }
      if (tok.size() > flen && tok.starts_with(flag)) {
        out.push_back(tok.substr(flen));
        break;
      }
    }
  }
  return out;
}

// ENCODE: rewrite absolute include-path values to <label> tokens.
std::vector<std::string>
CompileDb::alias_options(const std::vector<std::string> &options,
                         const std::vector<AliasEntry> &label_map) {
  return map_include_values(options, [&](const std::string &val) -> std::string {
    // Already indirected, or relative: leave unchanged.
    if (val.find('<') != std::string::npos ||
        val.find('$') != std::string::npos ||
        !pathutil::isabs(val)) {
      return val;
    }
    const auto m = match_alias(pathutil::normpath(val), label_map);
    if (!m.has_value()) {
      return val;
    }
    return "<" + std::get<0>(*m) + ">" + std::get<2>(*m);
  });
}

// True iff seg matches the version regex (compiledb.py:is_version_segment).
bool CompileDb::is_version_segment(const std::string &seg) {
  static const std::regex kVersionRe(R"(^v?[0-9]+([._\-][0-9]+)*$)");
  return std::regex_match(seg, kVersionRe);
}

// Numeric per-segment version key (compiledb.py / pathx.version_key).
std::vector<long long> CompileDb::version_key(const std::string &version) {
  std::string v = version;
  if (!v.empty() && v[0] == 'v') {
    v = v.substr(1);
  }
  std::vector<long long> out;
  std::string cur;
  const auto flush = [&]() {
    if (!cur.empty()) {
      const bool digits = std::all_of(cur.begin(), cur.end(), [](char c) {
        return c >= '0' && c <= '9';
      });
      if (digits) {
        out.push_back(std::stoll(cur));
      }
      cur.clear();
    }
  };
  for (char c : v) {
    if (c == '.' || c == '_' || c == '-') {
      flush();
    } else {
      cur += c;
    }
  }
  flush();
  return out;
}

} // namespace cidx

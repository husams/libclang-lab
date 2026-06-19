#include "compiledb/compiledb.hpp"

#include <cstring>
#include <regex>
#include <set>
#include <string>
#include <utility>

#include "clangx/libclang.hpp"
#include "util/errors.hpp"
#include "util/pathutil.hpp"

namespace cidx {
namespace {

// Frozen drop sets (compiledb.py:33-45, G10). Rationale baked into the
// Python comments: -M* writes build artifacts into dirs that don't exist
// outside a real build (fatal "error opening '...'"); -Werror promotes
// warnings gcc never emitted into clang error diagnostics.
const std::set<std::string> kDrop = {
    "-c",   "--",  "-M",  "-MM",     "-MD",
    "-MMD", "-MG", "-MP", "-MV",     "-Werror",
    "-pedantic-errors",
};
const std::set<std::string> kDropWithArg = {
    "-o", "-MF", "-MT", "-MQ", "-dependency-file", "--serialize-diagnostics",
};
const char *const kDropPrefix[] = {
    "-Werror=", // -Werror=return-type: keep it a plain warning
    "-Wp,-M",   // -Wp,-MD,<file> / -Wp,-MMD,<file>
    "-MF",      // glued forms: -MF<file> etc.
    "-MT",
    "-MQ",
};

bool has_drop_prefix(const std::string &tok) {
  for (const char *prefix : kDropPrefix) {
    if (tok.starts_with(prefix)) {
      return true;
    }
  }
  return false;
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
  size_t i = argv.empty() ? 0 : 1; // drop argv[0] (the driver)
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
  size_t i = 0;
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

std::string CompileDb::driver(const std::vector<std::string> &argv,
                              const std::string &directory) {
  if (argv.empty()) {
    return std::string();
  }
  const std::string &argv0 = argv[0];
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

} // namespace cidx

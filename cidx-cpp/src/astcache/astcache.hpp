// astcache.hpp — on-disk AST cache for `cidx ast` commands (ADR-005, M5).
//
// Caches libclang translation units as .ast files (PCH/TU-save format) under
// ~/.cache/cidx/files/ (or $INDEXER_CACHE/files/), with a JSON sidecar
// holding validity metadata. Byte-parity port of project/indexer/astcache.py.
//
// Key scheme: sha1(abspath + "\0" + flags [+ "\0drv\0" + driver]) — same
// as Python so a .ast written by either tool loads in the other (ADR-005 §2).
//
// Cache subcommands (cidx ast cache build/status/clear) are also here so
// that commands.cpp has a single import (parallel to the Python design
// where astcmd.cmd_cache dispatches to astcache.cmd_*).
#pragma once

#include <sys/stat.h>

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "clangx/parse.hpp"

namespace cidx {

struct AstTarget {
  std::string abspath;
  std::vector<std::string> flags;
  std::optional<std::string> driver;
  std::optional<std::string> focus_usr;  // for indexed targets
  std::optional<std::string> focus_name; // for ad-hoc/spelling targets

  bool whole_file() const { return !focus_usr && !focus_name; }
};

namespace astcache {

// --- directory helpers -------------------------------------------------------

// Cache root: $INDEXER_CACHE else ~/.cache/cidx (expanduser, NOT abspath).
// Mirrors Python astcache.cache_dir() / cli.cache_dir().
std::string cache_dir();

// Directory where .ast / .json files live.
std::string files_dir();

// --- hashing -----------------------------------------------------------------

// SHA-1 hex over flags + optional driver (no abspath prefix).
// Mirrors Python astcache.flags_hash().
std::string flags_hash(const AstTarget &t);

// SHA-1 hex over abspath + "\0" + flags + optional driver.
// Mirrors Python astcache.cache_key(). Frozen doctest:
//   abspath="/Users/husam/workspace/qemu-vms/libclang-lab/manifests/calls.c"
//   flags=["-std=c11"], driver=none -> "d6cca25a6ed23cd603c1baefecbc7f67f5435639"
std::string cache_key(const AstTarget &t);

// --- libclang version --------------------------------------------------------

// Full clang_getClangVersion() string (cached after first call).
// Mirrors Python astcache.libclang_version().
const std::string &libclang_version();

// --- parse counter (testability hook, mirrors Python astcache._PARSE_COUNT) --

int parse_count();
void reset_parse_count();

// --- validity ----------------------------------------------------------------

// Sidecar fields read from the JSON file.
struct Sidecar {
  std::string abspath;
  std::string flags_hash;
  double src_mtime = 0.0;
  std::string libclang_version;
};

// Read the sidecar JSON at `path`; returns nullopt on any failure.
std::optional<Sidecar> read_sidecar(const std::string &path);

// Write a sidecar JSON for `t` to `path`. Returns true on success.
bool write_sidecar(const std::string &path, const AstTarget &t,
                   double src_mtime);

// Returns true iff the cached entry is still fresh for `t`.
// Checks (in order): src accessible; flags_hash; src_mtime; libclang_version;
// abspath sanity. Mirrors Python astcache.is_valid().
bool is_valid(const AstTarget &t, const Sidecar &side);

// High-resolution mtime from a struct stat (POSIX sub-second where available).
// Used by is_valid + try_save; public so cmd_ast_cache_status can reuse it.
double src_mtime_of(const struct stat &st);

// --- low-level TU helpers ----------------------------------------------------

// Load a .ast file via clang_createTranslationUnit; returns nullopt on any
// failure (version skew, corruption). Mirrors Python astcache._load_ast().
std::optional<ParsedTu> load_ast(const std::string &path);

// Reparse `t` using Parser (increments parse_count).
// Returns nullopt on ClangParseError (prints error: to ctx.err if provided).
std::optional<ParsedTu> reparse(const AstTarget &t,
                                std::ostream *err = nullptr);

// Best-effort save: write tu.save() then sidecar. Any failure is silent
// (the caller has a live TU regardless). Mirrors Python astcache._try_save().
void try_save(CXTranslationUnit tu, const std::string &ast_path,
              const std::string &side_path, const AstTarget &t);

// --- main entry point --------------------------------------------------------

// Return a ParsedTu for `t`, using the on-disk cache when use_cache is true.
// On any cache failure, falls back to a live reparse. Returns nullopt only
// when the reparse itself fails (error already printed to err if provided).
// Mirrors Python astcache.load_or_parse().
std::optional<ParsedTu> load_or_parse(const AstTarget &t, bool use_cache,
                                      std::ostream *err = nullptr);

} // namespace astcache
} // namespace cidx

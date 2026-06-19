// compile_commands.json loading + arg strip/sanitize/driver (design §5.5,
// D20). Port of project/indexer/compiledb.py; the drop sets are FROZEN (G10)
// — they are part of the stored compile_options contract.
//
// Loading goes through libclang's CXCompilationDatabase (via the LibClang
// shim, never own JSON parsing) so `command`-string shell-unquoting stays
// byte-identical to the Python path (D20).
#pragma once

#include <functional>
#include <optional>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace cidx {

// Encode-registry entry: (name, match_path, versioned). `versioned` marks a
// component (version-agnostic match — the version segment after the base is
// stripped at encode and re-injected at decode); labels are exact (false).
// Mirrors the Python (name, path, versioned) tuples.
using AliasEntry = std::tuple<std::string, std::string, bool>;

struct CompileCommand {
  std::string directory; // cmd.directory as reported by libclang
  std::string filename;  // cmd.filename, raw (may be relative)
  std::string driver;    // driver(): argv[0], abspath'd iff it has a '/'
  std::vector<std::string> args; // strip_for_libclang() output
};

class CompileDb {
public:
  // --db arg: the compile_commands.json path (trailing filename stripped) or
  // its directory; abspath'd. Throws CidxError on load failure. Requires /
  // triggers LibClang::instance().load().
  static std::vector<CompileCommand> load(const std::string &db_arg);

  // The directory handed to CXCompilationDatabase before abspath
  // (compiledb.py:17-18): trailing "compile_commands.json" stripped, an
  // empty remainder -> ".". Exposed for hermetic tests.
  static std::string db_dir_from_arg(const std::string &db_arg);

  // Raw driver invocation -> flags parse() wants (compiledb.py:70-98):
  // drop argv[0]; apply the drop sets; drop the source file (matched by the
  // command's filename OR its basename, G10); absolutize -I/-isystem/-iquote
  // against `directory` in spaced and glued forms (G12); keep the rest.
  // Preserve rule (portable-paths §5): when a -I/-isystem/-iquote value
  // already contains '<' or '$', emit verbatim (do NOT absolutize).
  static std::vector<std::string>
  strip_for_libclang(const std::vector<std::string> &argv,
                     const std::string &filename, const std::string &directory);

  // Re-apply ONLY the drop rules (no argv[0]/source drop, no path fixing) to
  // already-stored options — heals DBs imported by an older cidx whose drop
  // list was shorter (compiledb.py:48-67, G11).
  static std::vector<std::string>
  sanitize(const std::vector<std::string> &stored);

  // argv[0]; absolutized against `directory` iff it contains a path
  // separator, else kept bare for PATH resolution at parse time
  // (compiledb.py:106-115).
  static std::string driver(const std::vector<std::string> &argv,
                            const std::string &directory);

  // split_base_version(root) — portable-paths §2:
  //   normpath(root), take split(root) → (base, seg).
  //   If seg matches the version regex (^v?[0-9]+([._-][0-9]+)*$) AND base is
  //   non-empty AND base != "/" → return {base, seg}.
  //   Else return {root, ""}.
  // The empty-string second element signals "no version detected".
  static std::pair<std::string, std::string>
  split_base_version(const std::string &root);

  // True iff seg matches the version regex ^v?[0-9]+([._-][0-9]+)*$.
  static bool is_version_segment(const std::string &seg);

  // Numeric per-segment sort key for a version string (drops a leading 'v',
  // splits on '.','_','-', keeps numeric fields). Lexicographic compare of the
  // returned vector matches Python's tuple-of-ints version_key, so
  // 18-0-0-275 > 18-0-0-100 > 18-0-0-11.
  static std::vector<long long> version_key(const std::string &version);

  // ---------------------------------------------------------------------------
  // Include-path aliasing (v0.6.0): encode absolute -I dirs <-> <label> tokens
  // (compiledb.py alias_options / resolve_options / build_label_map).
  // ---------------------------------------------------------------------------

  // DECODE: resolve <label>/$VAR/~ in include-path values to absolute paths.
  // Only values that look indirected (contain '<' or '$' or start with '~')
  // are resolved via the full resolution chain + abspath; plain absolute paths
  // are left untouched. Used at parse/index time so libclang sees real dirs.
  // lookup: returns stored path for a label name, or nullopt on miss.
  static std::vector<std::string>
  resolve_options(const std::vector<std::string> &options,
                  std::function<std::optional<std::string>(const std::string &)>
                      lookup = nullptr,
                  bool autoderive = true);

  // Build the encode label map from (name, stored_path, versioned) entries.
  // Each stored path is resolved to an absolute directory (env-vars expanded,
  // NO autoderive). Sorted longest-resolved-path first, then name, so the
  // longest prefix wins deterministically. The versioned flag passes through.
  // lookup: used to resolve labels within stored paths (rarely needed).
  static std::vector<AliasEntry>
  build_label_map(
      const std::vector<AliasEntry> &labels,
      std::function<std::optional<std::string>(const std::string &)> lookup =
          nullptr);

  // Longest-match an absolute path against label_map. Returns
  // (name, version_segment, remainder) for the first (longest) matching entry,
  // else nullopt. For a versioned (component) entry the segment after the base,
  // if it looks like a version, is captured as version_segment and excluded
  // from remainder (empty string = none). Mirrors compiledb.py:match_alias.
  static std::optional<std::tuple<std::string, std::string, std::string>>
  match_alias(const std::string &absval,
              const std::vector<AliasEntry> &label_map);

  // Yield each include-path VALUE (both `-I path` and `-Ipath`), skipping every
  // other token. Read-only counterpart of the encode walk; used by the import
  // version-bump scan. Mirrors compiledb.py:include_values.
  static std::vector<std::string>
  include_values(const std::vector<std::string> &options);

  // ENCODE: rewrite absolute include-path values to <label> tokens.
  // label_map is the output of build_label_map (sorted longest-first).
  // A value equal to or under an entry's resolved directory becomes
  // "<name>" + remainder (longest match wins; component entries strip version).
  // Values already indirected ('<' or '$') and relative values are unchanged.
  static std::vector<std::string>
  alias_options(const std::vector<std::string> &options,
                const std::vector<AliasEntry> &label_map);
};

} // namespace cidx

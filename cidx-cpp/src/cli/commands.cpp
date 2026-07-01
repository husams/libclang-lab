#include "cli/commands.hpp"

#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iterator>
#include <map>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "astcache/astcache.hpp"
#include "clangx/ast.hpp"
#include "clangx/ast_query.hpp"
#include "clangx/libclang.hpp"
#include "clangx/parse.hpp"
#include "clangx/pch.hpp"
#include "clangx/toolchain.hpp"
#include "cli/format.hpp"
#include "cli/json_out.hpp"
#include "cli/kind_names.hpp"
#include "compiledb/compiledb.hpp"
#include "graph/emit.hpp"
#include "graph/query.hpp"
#include "graph/records.hpp"
#include "storage/records.hpp"
#include "storage/storage.hpp"
#include "util/env.hpp"
#include "util/errors.hpp"
#include "util/files.hpp"
#include "util/hashing.hpp"
#include "util/pathutil.hpp"
#include "util/repo.hpp"

namespace cidx {
namespace cli {
namespace {

namespace fmt = format;

bool is_digits(const std::string &s) {
  if (s.empty()) {
    return false;
  }
  for (char c : s) {
    if (!std::isdigit(static_cast<unsigned char>(c))) {
      return false;
    }
  }
  return true;
}

bool is_directory(const std::string &path) {
  struct stat st{};
  return ::stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}

// clang diagnostic severity labels (cli.py _DIAG_SEVERITY_LABELS).
const char *diag_severity_label(int severity) {
  switch (severity) {
  case 2:
    return "warning";
  case 3:
    return "error";
  case 4:
    return "fatal";
  default:
    return nullptr;
  }
}

// cli.py _diag_flag: compact `list files` indicator from {severity: n}: "-"
// when clean, else e.g. "2E"/"3W"/"1E2W" (errors+fatals fold into E).
std::string diag_flag(const std::map<int, int64_t> &counts) {
  int64_t errs = 0;
  int64_t warns = 0;
  for (const auto &[sev, n] : counts) {
    if (sev >= 3) {
      errs += n;
    } else if (sev == 2) {
      warns += n;
    }
  }
  if (errs == 0 && warns == 0) {
    return "-";
  }
  std::string out;
  if (errs != 0) {
    out += std::to_string(errs) + "E";
  }
  if (warns != 0) {
    out += std::to_string(warns) + "W";
  }
  return out;
}

// cli.py _diag_summary: "2 error(s), 1 warning(s)" — severity desc, nonzero.
std::string diag_summary(const std::map<int, int64_t> &counts) {
  std::string out;
  for (int sev : {4, 3, 2}) {
    const auto it = counts.find(sev);
    if (it == counts.end() || it->second == 0) {
      continue;
    }
    if (!out.empty()) {
      out += ", ";
    }
    out += std::to_string(it->second) + " " + diag_severity_label(sev) + "(s)";
  }
  return out;
}

// os.path.exists parity: any stat success (file or directory).
bool path_exists(const std::string &path) {
  struct stat st{};
  return ::stat(path.c_str(), &st) == 0;
}

// os.path.getmtime float parity: sec + nsec * 1e-9.
std::optional<double> file_mtime(const std::string &path) {
  struct stat st{};
  if (::stat(path.c_str(), &st) != 0) {
    return std::nullopt;
  }
#ifdef __APPLE__
  return static_cast<double>(st.st_mtimespec.tv_sec) +
         static_cast<double>(st.st_mtimespec.tv_nsec) * 1e-9;
#else
  return static_cast<double>(st.st_mtim.tv_sec) +
         static_cast<double>(st.st_mtim.tv_nsec) * 1e-9;
#endif
}

// compiledb.source_path: _abs(cmd.filename, cmd.directory) — absolute
// filenames returned unchanged (not normalized), relative ones
// normpath(join(...)).
std::string source_path(const CompileCommand &cmd) {
  if (pathutil::isabs(cmd.filename)) {
    return cmd.filename;
  }
  return pathutil::normpath(pathutil::join(cmd.directory, cmd.filename));
}

// _lookup_component (cli.py:162-171): nullopt name -> no scoping; unknown
// name -> "error: no component named '<name>'" printed, false returned
// (LookupError -> return 1 in every caller).
bool lookup_component(Storage &db, const std::optional<std::string> &name,
                      std::optional<Component> &out, std::ostream &err) {
  out.reset();
  if (!name || name->empty()) {
    return true;
  }
  out = db.get_component_by_name(*name);
  if (!out) {
    err << "error: no component named " << fmt::py_repr(*name) << "\n";
    return false;
  }
  return true;
}

const char kDirNeedsComponent[] =
    "error: --dir requires --component (directory paths are relative to a "
    "component root)";

// -- index helpers (cli.py:180-231) -----------------------------------------

// _index_one (cli.py:180-197): parse + index one pending file (main TU + its
// headers); returns 0/1. Only ClangParseError is tolerated (error printed,
// fail flag, the run continues with the rest); everything else propagates to
// main() (D23). The ParsedTu lives only inside the try block: its destructor
// frees the TU + Index BEFORE mark_file_indexed runs — Python's
// `del tu` in index_source's finally (one-AST peak memory, design §7).
int index_one(Storage &db, Parser &parser, AstIndexer &indexer, const File &rec,
              const std::string &path, Context &ctx) {
  int stored = 0;
  HeaderStats hs;
  std::vector<Diagnostic> diags;
  try {
    // Stored options are re-sanitize()d at index time (G11) — heals DBs
    // imported by an older cidx whose drop list was shorter.
    // Then decode <label>/$VAR tokens via resolve_options (v0.6.0). get_alias
    // resolves both explicit labels and uniquely-named components (v0.8.0).
    const std::vector<std::string> opts =
        CompileDb::resolve_options(
            CompileDb::sanitize(rec.compile_options ? *rec.compile_options
                                                    : std::vector<std::string>{}),
            [&db](const std::string &n) { return db.get_alias(n); });
    // parse() receives the reconstructed absolute path (G24) and assembles
    // opts + toolchain_flags(is_cpp, driver) + -ferror-limit=0 itself.
    const ParsedTu tu = parser.parse(path, opts, rec.driver);
    // Capture parse diagnostics (warnings + tolerated errors) while the TU is
    // live; persisted against this file's row after the AST is freed.
    diags = Parser::collect_diagnostics(tu);
    stored = indexer.index_symbols(tu, path, rec.id);
    // Stamp each indexed header with this TU's (encoded) options + driver so
    // the header is standalone-reparseable (e.g. `cidx ast dump <header>`).
    hs = indexer.index_headers(tu, std::nullopt, rec.compile_options,
                               rec.driver);
    // v7: extract graph edges AFTER symbols so src_id lookups hit real rows.
    // index_edges opens its own transaction; it's a no-op when graph_enabled_
    // is false (--no-graph was passed).
    indexer.index_edges(tu, path, rec.id);
  } catch (const ClangParseError &e) {
    // The file failed to parse (e.g. a fatal header-not-found): record the
    // diagnostics so `show file` / `list files` explain why, even though no
    // AST was indexed and the file stays pending. A hard load failure carries
    // no per-diagnostic detail -- synthesize one fatal row from the error so
    // the failure is never silently dropped.
    std::vector<Diagnostic> failed = e.diagnostics();
    if (failed.empty()) {
      Diagnostic d;
      d.severity = 4; // clang fatal
      d.spelling = e.what();
      d.file_path = path;
      failed.push_back(std::move(d));
    }
    db.replace_diagnostics(rec.id, failed);
    *ctx.err << "error: " << e.what() << "\n";
    return 1;
  }
  db.replace_diagnostics(rec.id, diags);
  db.mark_file_indexed(rec.id, file_mtime(path));
  // cli.py:194-196 — byte-frozen per-file line (analysis §1.2).
  *ctx.out << "  -> " << stored << " symbols; headers: " << hs.indexed
           << " indexed (+" << hs.symbols << " symbols), " << hs.already
           << " already, " << hs.system << " system, " << hs.unowned
           << " unowned\n";
  return 0;
}

// _index_files (cli.py:200-215): index FILE...; unknown files set the fail
// flag but the loop continues.
int index_files(Storage &db, Parser &parser, AstIndexer &indexer,
                const std::vector<std::string> &file_args,
                const std::optional<std::string> &root, Context &ctx) {
  int rc = 0;
  for (const std::string &f : file_args) {
    const std::string path = files::resolve_file_arg(f, root);
    const std::optional<File> rec = db.get_file(path);
    if (!rec) {
      *ctx.err << "error: not in index database: " << path << "\n";
      rc = 1;
      continue;
    }
    *ctx.out << "file: " << path << "\n";
    if (files::index_status(*rec, path) == files::IndexStatus::kOk) {
      *ctx.out << "  already indexed\n";
      continue;
    }
    rc |= index_one(db, parser, indexer, *rec, path, ctx);
  }
  return rc;
}

// _index_pending (cli.py:218-231): index every file still pending. Python
// iterates db.files() — EVERY row (header rows included) with the md5-only
// skip (analysis §4); list_files() with no filters is the same query/order
// (ORDER BY c.path, d.path, f.name), snapshotted before the loop so header
// rows added while indexing are not re-visited this run.
int index_pending(Storage &db, Parser &parser, AstIndexer &indexer,
                  Context &ctx) {
  int done = 0;
  int skipped = 0;
  int failed = 0;
  int deferred = 0;
  for (const auto &row : db.list_files()) {
    const File &rec = row.first;
    const std::string &path = row.second;
    if (files::index_status(rec, path) == files::IndexStatus::kOk) {
      ++skipped;
      continue;
    }
    // A header (by extension) is indexed via its including TU's
    // index_headers() pass (full -I/-std context, deduped once per run against
    // the live DB), never parsed standalone. Defer it here. A TU source is
    // indexed even when its compile command sanitizes to no flags (e.g.
    // `cc -c x.c -o x.o` -> []) -- it is still a real TU, so its parse
    // diagnostics land on its row.
    if (files::is_header(path)) {
      ++deferred;
      continue;
    }
    *ctx.out << "indexing " << path << "\n";
    if (index_one(db, parser, indexer, rec, path, ctx) == 0) {
      ++done;
    } else {
      ++failed;
    }
  }
  *ctx.out << "index: " << done << " indexed, " << failed << " failed, "
           << skipped << " already indexed";
  if (deferred > 0) {
    *ctx.out << ", " << deferred << " headers via TUs";
  }
  *ctx.out << "\n";
  return failed != 0 ? 1 : 0;
}

// -- delete helpers (cli.py _plural / _selector_str / _under_component /
//    _finish_delete) -----------------------------------------------------

const char *plural(std::size_t n, const char *singular, const char *plural) {
  return n == 1 ? singular : plural;
}

// The selector the user passed, for error messages: "--name foo".
std::string selector_str(const ParsedArgs &args) {
  if (args.del_id) {
    return "--id " + std::to_string(*args.del_id);
  }
  if (args.name) {
    return "--name " + *args.name;
  }
  if (args.del_path) {
    return "--path " + *args.del_path;
  }
  if (args.usr) {
    return "--usr " + *args.usr;
  }
  return "<no selector>";
}

// True when comp is unset, or abs_path lies within the component root.
bool under_component(Storage &db, const std::optional<std::string> &abs_path,
                     const std::optional<Component> &comp) {
  if (!comp) {
    return true;
  }
  if (!abs_path) {
    return false;
  }
  // v24: resolve the (possibly clone-relative) component base.
  std::string root = db.component_abs_base(*comp);
  while (!root.empty() && root.back() == '/') {
    root.pop_back();
  }
  return *abs_path == root || abs_path->starts_with(root + "/");
}

// Shared tail: print matched rows, delete (unless --dry-run), summarize.
int finish_delete(const ParsedArgs &args, Context &ctx,
                  const std::vector<int64_t> &ids,
                  const std::vector<std::string> &lines,
                  const std::function<void(int64_t)> &del_fn,
                  const char *singular, const char *plural_word) {
  for (const std::string &line : lines) {
    *ctx.out << line << "\n";
  }
  if (!args.dry_run) {
    for (const int64_t id : ids) {
      del_fn(id);
    }
  }
  *ctx.out << (args.dry_run ? "would delete " : "deleted ") << ids.size() << " "
           << plural(ids.size(), singular, plural_word) << "\n";
  return 0;
}

} // namespace

std::string resolve_cache_dir() {
  // os.path.expanduser(os.environ.get("INDEXER_CACHE") or "~/.cache/cidx")
  std::optional<std::string> env = get_env("INDEXER_CACHE");
  const std::string raw =
      (env && !env->empty()) ? *env : std::string("~/.cache/cidx");
  return pathutil::expanduser(raw);
}

// -- write commands ----------------------------------------------------------

// cmd_init (cli.py cmd_init): create a blank index database (schema v6, no
// rows) at the cache path. Constructing a Storage applies the schema, so this
// just materializes an empty index.db. Refuses to clobber an existing
// database unless --force; with --force the old file is removed first.
int cmd_init(const ParsedArgs &args, Context &ctx) {
  const bool existed = path_exists(ctx.index_path);
  if (existed && !args.force) {
    *ctx.err << "error: index database already exists at " << ctx.index_path
             << " (use --force to recreate)\n";
    return 1;
  }
  if (existed && std::remove(ctx.index_path.c_str()) != 0) {
    // os.remove raises on failure -> propagates to main() (exit 1).
    throw CidxError("cannot remove " + ctx.index_path);
  }
  { Storage db(ctx.index_path); } // constructing Storage applies the schema
  *ctx.out << (existed ? "recreated" : "initialized")
           << " empty index database at " << ctx.index_path << "\n";
  return 0;
}

// cmd_migrate (cli.py cmd_migrate): upgrade an existing index DB to the current
// schema in place. Constructing a Storage runs the migration (e.g. v15 -> v16
// converts symbol.kind from a TEXT name to its CXCursorKind integer); this just
// does it deliberately and reports the before/after version. Preserves all data
// (symbols, edges) and never re-indexes. --db targets a non-standard index.
int cmd_migrate(const ParsedArgs &args, Context &ctx) {
  (void)args; // --db already folded into ctx.index_path by main()
  if (!path_exists(ctx.index_path)) {
    *ctx.err << "error: no index database at " << ctx.index_path
             << " (run `cidx init` / `cidx import` first)\n";
    return 1;
  }
  // Read schema_version WITHOUT opening a Storage (whose ctor would migrate).
  auto schema_version = [&]() -> std::optional<int> {
    SqliteDb db(ctx.index_path);
    SqliteStmt st =
        db.prepare("SELECT value FROM meta WHERE key = 'schema_version'");
    if (st.step() && !st.col_is_null(0)) {
      const std::string v = st.col_text(0);
      if (!v.empty()) {
        return std::stoi(v);
      }
    }
    return std::nullopt;
  };
  auto vstr = [](const std::optional<int> &v) {
    return v ? std::to_string(*v) : std::string("None"); // Python f"v{None}"
  };
  // A leftover 'nests' row (removed relation) or 'realizes' row (renamed to
  // 'implements') marks a DB whose entity_edge_kind seed must be refreshed, even
  // when schema_version is already current (an earlier build bumped the version
  // without reconciling the seed).
  auto entity_kinds_stale = [&]() -> bool {
    SqliteDb db(ctx.index_path);
    try {
      SqliteStmt st = db.prepare("SELECT 1 FROM entity_edge_kind "
                                 "WHERE name IN ('nests', 'realizes') LIMIT 1");
      return st.step();
    } catch (const std::exception &) {
      return false; // no entity_edge_kind table (pre-v17 DB)
    }
  };
  const std::optional<int> before = schema_version();
  if (before && *before > kSchemaVersion) {
    *ctx.err << "index at " << ctx.index_path << " is schema v" << *before
             << ", newer than this build (v" << kSchemaVersion
             << "); refusing to touch it\n";
    return 1;
  }
  const bool stale = entity_kinds_stale();
  { Storage db(ctx.index_path); } // constructing Storage applies the migration
  const std::optional<int> after = schema_version();
  if (before != after) {
    *ctx.out << "migrated " << ctx.index_path << ": schema v" << vstr(before)
             << " -> v" << vstr(after) << "\n";
  } else if (stale) {
    *ctx.out << "migrated " << ctx.index_path
             << ": refreshed entity relation kinds (schema v" << vstr(after)
             << ")\n";
  } else {
    *ctx.out << ctx.index_path << " already at schema v" << vstr(after)
             << "; nothing to migrate\n";
  }
  return 0;
}

int cmd_add_source(const ParsedArgs &args, Context &ctx) {
  const std::string kind = args.kind ? *args.kind : "repo";
  std::string path = pathutil::abspath(args.path);
  if (!is_directory(path)) {
    *ctx.err << "error: " << path << " is not a directory\n";
    return 1;
  }
  const bool use_git = kind == "repo" && !args.no_git;
  std::optional<std::string> git_root_opt;
  if (use_git) {
    git_root_opt = repo::git_root(path);
    if (git_root_opt) {
      path = *git_root_opt;
    }
  }
  const std::string name =
      args.name
          ? *args.name
          : (use_git ? repo::repo_name(path) : pathutil::basename(path));
  // v23: the (pre-version-split) source/repo dir is the repository's clone path.
  const std::string clone_path = path;
  // v14: version auto-detection (split_base_version) then explicit override.
  std::optional<std::string> version_to_store;
  if (args.version_str) {
    version_to_store = *args.version_str;
  } else if (!args.no_detect_version) {
    const auto [base, seg] = CompileDb::split_base_version(path);
    if (!seg.empty()) {
      path = base; // store base without version segment
      version_to_store = seg;
    }
  }
  Storage db(ctx.index_path);
  // v24: a grouped component stores a clone-relative path, so re-adding the same
  // source resolves the EXISTING component clone-aware (its stored path is no
  // longer the absolute base) and refreshes its metadata in place; only a
  // genuinely new source mints a row. Mirrors Python cmd_add_source.
  int64_t cid;
  if (const auto existing = db.get_component(path); existing) {
    cid = existing->id;
    db.update_component_meta(cid, name, kind, version_to_store);
  } else {
    cid = db.add_component(name, path, kind, version_to_store);
  }
  // v23: group the component under a repository (same kind). The source dir is
  // its first clone and becomes active. Mirrors Python cmd_add_source.
  const std::string repo_name_val = args.repo ? *args.repo : name;
  std::optional<std::string> remote_url =
      git_root_opt ? repo::git_remote_url(*git_root_opt)
                   : std::optional<std::string>{};
  const int64_t rid = db.add_repository(repo_name_val, kind, remote_url);
  const int64_t clone_id = db.add_clone(rid, clone_path);
  const std::optional<Repository> repo = db.get_repository_by_id(rid);
  if (repo && !repo->active_clone_id) {
    db.set_active_clone(rid, clone_id);
  }
  db.set_component_repository(cid, rid);
  // v24: store the component path RELATIVE to its clone root so a later
  // `repo switch` only repoints the active clone (no path rewrite).
  db.relativize_component(cid, clone_path);
  *ctx.out << "component #" << cid << ": " << name << " (" << kind << ") at "
           << path << "\n";
  return 0;
}

// Advance a component's stored version when a ported compile command's -I sits
// under its version-stripped base and carries a numerically HIGHER version than
// the one registered AND that version directory EXISTS on disk ("check the path
// exists before you replace, even when it is newer"). An include whose version
// is missing on disk (or not higher) leaves the version as registered. The
// write goes through set_component_effective_version, which handles both the
// version-as-property and version-embedded-in-path representations and is a
// no-op for ambiguous multi-row names. Mirrors Python cli._bump_component_versions.
void bump_component_versions(Storage &db,
                             const std::vector<CompileCommand> &commands,
                             const std::vector<AliasEntry> &label_map) {
  const auto idx = db.component_alias_index();
  std::map<std::string, std::string> seen; // name -> highest version seen
  for (const CompileCommand &cmd : commands) {
    for (const std::string &val : CompileDb::include_values(cmd.args)) {
      if (val.find('<') != std::string::npos ||
          val.find('$') != std::string::npos || !pathutil::isabs(val)) {
        continue;
      }
      const auto m = CompileDb::match_alias(pathutil::normpath(val), label_map);
      if (!m.has_value()) {
        continue;
      }
      const std::string &name = std::get<0>(*m);
      const std::string &vseg = std::get<1>(*m);
      if (vseg.empty()) {
        continue;
      }
      auto it = seen.find(name);
      if (it == seen.end() ||
          CompileDb::version_key(vseg) > CompileDb::version_key(it->second)) {
        seen[name] = vseg;
      }
    }
  }
  for (const auto &[name, vseg] : seen) {
    const auto it = idx.find(name);
    if (it == idx.end()) {
      continue;
    }
    const auto &[base, maxver, bumpable] = it->second;
    (void)bumpable;
    // Only a strictly-higher version is a candidate for replacement.
    if (!maxver.empty() &&
        CompileDb::version_key(vseg) <= CompileDb::version_key(maxver)) {
      continue;
    }
    // Existence guard: never repoint a component at a version dir absent from
    // disk, even when newer. `base` is the resolved, absolute, version-stripped
    // component base.
    std::error_code ec;
    if (!std::filesystem::is_directory(pathutil::join(base, vseg), ec)) {
      continue;
    }
    db.set_component_effective_version(name, vseg);
  }
}

int cmd_import(const ParsedArgs &args, Context &ctx) {
  // A missing/unloadable libclang is NOT a compilation-database failure:
  // let it propagate to main()'s generic CidxError handler (exit 1 with the
  // real dlopen message). Python's analogue fails at clang.cindex import.
  LibClang::instance().load();

  std::vector<CompileCommand> commands;
  try {
    commands = CompileDb::load(args.db);
  } catch (const CidxError &) {
    // Python prints the cindex exception text; CompilationDatabaseError
    // formats as "Error 1: CompilationDatabase loading failed" for every
    // fromDirectory failure — reproduced verbatim for golden parity.
    *ctx.err << "error: cannot load compilation database from " << args.db
             << ": Error 1: CompilationDatabase loading failed\n";
    return 1;
  }
  if (commands.empty()) {
    *ctx.err << "error: compilation database is empty\n";
    return 1;
  }

  // Component root: the git repo owning the sources, else the directory
  // holding compile_commands.json (its basename names the component). The db
  // dir — not the first source's dir — keeps git-worktree checkouts, whose
  // `.git` is a file rather than a directory, rooted where their build db lives.
  const std::string first_src = source_path(commands[0]);
  const std::optional<std::string> groot = repo::git_root(first_src);
  const std::string root =
      groot ? *groot
            : pathutil::abspath(CompileDb::db_dir_from_arg(args.db));
  const std::string name =
      args.name ? *args.name
                : (groot ? repo::repo_name(root) : pathutil::basename(root));

  // Version is a per-component property: import only AUTO-DETECTS a trailing
  // version segment (e.g. .../1.4.0). Manual version control lives in
  // `cidx component set-version`, not on import. Mirrors Python cmd_import.
  std::string stored_root = root;
  std::optional<std::string> version_to_store;
  {
    const auto [base, seg] = CompileDb::split_base_version(root);
    if (!seg.empty()) {
      stored_root = base;
      version_to_store = seg;
    }
  }

  int imported = 0;
  int skipped = 0;
  Storage db(ctx.index_path);

  // Encode include paths against the alias registry unless --no-alias. The
  // registry is explicit labels PLUS uniquely-named components, so an -I under
  // a component root auto-aliases to <component-name> with no `cidx label add`
  // needed; decode (get_alias) mirrors this same registry.
  // Mirrors Python cmd_import: build_label_map(db.list_alias_pairs(), db.get_alias).
  std::vector<AliasEntry> label_map;
  if (!args.no_alias) {
    const auto pairs = db.list_alias_pairs();
    if (!pairs.empty()) {
      label_map = CompileDb::build_label_map(
          pairs,
          [&db](const std::string &n) { return db.get_alias(n); });
    }
  }
  // Version-agnostic port: a ported -I under a versioned component base may
  // carry a HIGHER version than the registered one — advance the stored
  // component version so <name> decodes to it (bumpable components only).
  if (!label_map.empty()) {
    bump_component_versions(db, commands, label_map);
  }

  if (args.force) {
    const std::optional<Component> existing = db.get_component(stored_root);
    if (existing) {
      // Resolved base (stored path is clone-relative for a grouped component,
      // v24); show the absolute path either way. Mirrors Python cmd_import.
      const std::string existing_base = db.component_abs_base(*existing);
      db.delete_component(existing->id);
      *ctx.out << "force: removed existing component #" << existing->id
               << " at " << existing_base << " (files and indexed symbols)\n";
    }
  }
  // The db-dir/git-root component is created LAZILY: only when a source matches
  // no already-registered component. Matching first means an import whose
  // sources are already covered by existing components (e.g. sub-components
  // Comp_1/Comp_2) does not spawn a spurious project component and re-home
  // those files under it. Mirrors Python cmd_import.
  std::optional<int64_t> root_cid;
  {
    Transaction txn = db.transaction();
    for (const CompileCommand &cmd : commands) {
      const std::string src = source_path(cmd);
      if (!db.component_for_path(src)) {
        if (!root_cid) {
          root_cid = db.add_component(name, stored_root, "repo", version_to_store);
          *ctx.out << "component #" << *root_cid << ": " << name << " at "
                   << stored_root << "\n";
          // The label_map was built BEFORE this lazily-created component
          // existed, so its own -I paths would store as absolute. Rebuild the
          // registry (now including it) and re-run the version bump so its
          // includes encode to <name>. Mirrors Python cmd_import.
          if (!args.no_alias) {
            const auto pairs = db.list_alias_pairs();
            label_map =
                pairs.empty()
                    ? std::vector<AliasEntry>{}
                    : CompileDb::build_label_map(
                          pairs, [&db](const std::string &n) {
                            return db.get_alias(n);
                          });
            if (!label_map.empty()) {
              bump_component_versions(db, commands, label_map);
            }
          }
        }
        if (!db.component_for_path(src)) {
          *ctx.err << "  skip (outside any component): " << src << "\n";
          ++skipped;
          continue;
        }
      }
      // Apply alias_options after stripping (encode include paths).
      std::vector<std::string> opts = cmd.args;
      if (!label_map.empty()) {
        opts = CompileDb::alias_options(opts, label_map);
      }
      db.add_file_path(src, file_mtime(src), md5_of(src), opts, cmd.driver);
      ++imported;
    }
    txn.commit(); // R2: explicit commit so a COMMIT failure is not swallowed
  }
  // v23: group the imported components under a repository. Identity = --repo if
  // given, else the git/dir-derived name; remote_url (git checkout) lets two
  // worktrees map to one repository. The checkout dir (`root`) is registered as
  // a clone and made active when the repository has none. Mirrors Python.
  {
    const std::string repo_name_val = args.repo ? *args.repo : name;
    std::optional<std::string> remote_url =
        groot ? repo::git_remote_url(*groot)
              : std::optional<std::string>{};
    const int64_t rid = db.add_repository(repo_name_val, "repo", remote_url);
    const int64_t clone_id = db.add_clone(rid, root);
    const std::optional<Repository> repo = db.get_repository_by_id(rid);
    if (repo && !repo->active_clone_id) {
      db.set_active_clone(rid, clone_id);
    }
    std::set<int64_t> attached;
    for (const CompileCommand &cmd : commands) {
      const std::optional<Component> comp =
          db.component_for_path(source_path(cmd));
      if (!comp || attached.count(comp->id)) {
        continue;
      }
      attached.insert(comp->id);
      if (!comp->repository_id) {
        db.set_component_repository(comp->id, rid);
      }
      // v24: store each grouped component's path RELATIVE to the clone root,
      // so `repo switch` repoints one pointer instead of N rows.
      db.relativize_component(comp->id, root);
    }
    *ctx.out << "repository '" << repo_name_val << "': " << attached.size()
             << " component(s)\n";
  }
  *ctx.out << "imported " << imported << " file(s), skipped " << skipped
           << "\n";
  return 0;
}

// cmd_index (cli.py:234-245) — the full §6.1 pipeline. libclang is NOT
// loaded eagerly: Parser::parse() loads it on first use (S05), so an index
// run with nothing to do succeeds without a libclang — exactly like the
// Python tool, whose cindex library loads lazily on the first parse.
int cmd_index(const ParsedArgs &args, Context &ctx) {
  Logger &log = ctx.logger != nullptr ? *ctx.logger : Logger::root();
  int rc = 0;
  {
    Storage db(ctx.index_path);
    // _source_root (cli.py:174-177): unknown --source name -> error, exit 1
    // (the warning-count line is NOT printed on this path — Python returns
    // from inside the `with` block before reaching it).
    std::optional<Component> comp;
    if (!lookup_component(db, args.source, comp, *ctx.err)) {
      return 1;
    }
    const std::optional<std::string> root =
        comp ? std::optional<std::string>(db.component_abs_base(*comp)) : std::nullopt;
    // One Toolchain/Parser per run (S04/S05: memoized, single-threaded D15).
    Toolchain toolchain(log);
    Parser parser(toolchain, log);
    AstIndexer indexer(db, log);
    // v7: --no-graph disables edge extraction for this run.
    indexer.set_graph_enabled(!args.no_graph);
    rc = !args.files.empty()
             ? index_files(db, parser, indexer, args.files, root, ctx)
             : index_pending(db, parser, indexer, ctx);
  }
  // cli.py:243-244 — only when the file-sink warning counter is > 0 (G27).
  if (log.warning_count() > 0) {
    *ctx.out << log.warning_count() << " warning(s)/error(s) logged to "
             << pathutil::join(ctx.cache_dir, "cidx.log") << "\n";
  }
  return rc;
}

// -- query commands ------------------------------------------------------

int cmd_search(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  const std::vector<Symbol> hits = db.search_symbols(*args.pattern, args.kind);
  fmt::print_symbols(db, hits, args.limit, *ctx.out);
  return hits.empty() ? 1 : 0;
}

int cmd_list_components(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  const std::vector<Component> comps =
      db.list_components(args.pattern, args.kind);
  // v23: repository_id -> repository name, "-" when ungrouped.
  std::map<int64_t, std::string> repos;
  for (const Repository &r : db.list_repositories()) {
    repos[r.id] = r.name;
  }
  auto repo_name_of = [&](const Component &c) -> std::string {
    if (c.repository_id) {
      const auto it = repos.find(*c.repository_id);
      if (it != repos.end()) {
        return it->second;
      }
    }
    return "-";
  };
  std::size_t width = 0;
  std::size_t vw = 1;
  std::size_t rw = 1;
  for (const Component &c : comps) {
    width = std::max(width, c.name.size());
    if (c.version) {
      vw = std::max(vw, c.version->size());
    }
    rw = std::max(rw, repo_name_of(c).size());
  }
  for (const Component &c : comps) {
    const std::string ver = c.version ? *c.version : "-";
    const std::string rep = repo_name_of(c);
    // Show the resolved (clone-anchored) base, not the stored path, which is
    // relative for a grouped component (v24).
    // f"{c.id:>4}  {c.name:<{width}}  {c.kind:<8}  {ver:<{vw}}  {rep:<{rw}}  {base}"
    *ctx.out << fmt::rjust(std::to_string(c.id), 4) << "  "
             << fmt::ljust(c.name, width) << "  " << fmt::ljust(c.kind, 8)
             << "  " << fmt::ljust(ver, vw) << "  " << fmt::ljust(rep, rw)
             << "  " << db.component_abs_base(c) << "\n";
  }
  *ctx.out << comps.size() << " component(s)\n";
  return comps.empty() ? 1 : 0;
}

int cmd_list_dirs(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  std::optional<Component> comp;
  if (!lookup_component(db, args.component, comp, *ctx.err)) {
    return 1;
  }
  const auto rows = db.list_directories(
      comp ? std::optional<int64_t>(comp->id) : std::nullopt, args.pattern);
  std::size_t width = 0;
  for (const auto &row : rows) {
    width = std::max(width, row.second.size());
  }
  for (const auto &row : rows) {
    const Directory &d = row.first;
    // f"{d.id:>4}  {cname:<{width}}  {d.path or '.'}"
    *ctx.out << fmt::rjust(std::to_string(d.id), 4) << "  "
             << fmt::ljust(row.second, width) << "  "
             << (d.path.empty() ? "." : d.path) << "\n";
  }
  *ctx.out << rows.size() << " directory(ies)\n";
  return rows.empty() ? 1 : 0;
}

int cmd_list_files(const ParsedArgs &args, Context &ctx) {
  if (args.dir && !(args.component && !args.component->empty())) {
    *ctx.err << kDirNeedsComponent << "\n";
    return 1;
  }
  Storage db(ctx.index_path);
  std::optional<Component> comp;
  if (!lookup_component(db, args.component, comp, *ctx.err)) {
    return 1;
  }
  // indexed = True if --indexed else False if --pending else None
  std::optional<bool> indexed;
  if (args.indexed) {
    indexed = true;
  } else if (args.pending) {
    indexed = false;
  }
  const auto rows =
      db.list_files(comp ? std::optional<int64_t>(comp->id) : std::nullopt,
                    args.dir, args.pattern, indexed);
  // Version is a per-component property; show each file's owning-component
  // version. Map file -> directory -> component -> version (two queries).
  std::unordered_map<int64_t, std::optional<std::string>> comp_ver;
  for (const Component &c : db.list_components()) {
    comp_ver[c.id] = c.version;
  }
  std::unordered_map<int64_t, int64_t> dir_comp;
  for (const auto &pr : db.list_directories()) {
    dir_comp[pr.first.id] = pr.first.component_id;
  }
  std::vector<std::string> vers;
  vers.reserve(rows.size());
  std::size_t vw = 1;
  for (const auto &row : rows) {
    std::string v = "-";
    const auto dit = dir_comp.find(row.first.directory_id);
    if (dit != dir_comp.end()) {
      const auto cit = comp_ver.find(dit->second);
      if (cit != comp_ver.end() && cit->second && !cit->second->empty()) {
        v = *cit->second;
      }
    }
    vw = std::max(vw, v.size());
    vers.push_back(v);
  }
  // Parse-diagnostic indicator: "-" clean, else e.g. "2E"/"3W"/"1E2W".
  const auto diag_counts = db.diagnostic_counts();
  std::vector<std::string> flags;
  flags.reserve(rows.size());
  std::size_t fw = 1;
  for (const auto &row : rows) {
    std::string flag = "-";
    const auto dit = diag_counts.find(row.first.id);
    if (dit != diag_counts.end()) {
      flag = diag_flag(dit->second);
    }
    fw = std::max(fw, flag.size());
    flags.push_back(flag);
  }
  for (std::size_t k = 0; k < rows.size(); ++k) {
    const File &rec = rows[k].first;
    const char *mark = rec.indexed ? "idx " : "pend";
    // f"{rec.id:>4}  {mark}  {flag:<{fw}}  {ver:<{vw}}  {path}"
    *ctx.out << fmt::rjust(std::to_string(rec.id), 4) << "  " << mark << "  "
             << fmt::ljust(flags[k], fw) << "  " << fmt::ljust(vers[k], vw)
             << "  " << rows[k].second << "\n";
  }
  *ctx.out << rows.size() << " file(s)\n";
  return rows.empty() ? 1 : 0;
}

int cmd_list_symbols(const ParsedArgs &args, Context &ctx) {
  if (args.dir && !(args.component && !args.component->empty())) {
    *ctx.err << kDirNeedsComponent << "\n";
    return 1;
  }
  Storage db(ctx.index_path);
  std::optional<Component> comp;
  if (!lookup_component(db, args.component, comp, *ctx.err)) {
    return 1;
  }
  std::optional<int64_t> file_id;
  if (args.file_filter && !args.file_filter->empty()) {
    const std::string path = files::resolve_file_arg(
        *args.file_filter,
        comp ? std::optional<std::string>(db.component_abs_base(*comp)) : std::nullopt);
    const std::optional<File> rec = db.get_file(path);
    if (!rec) {
      *ctx.err << "error: not in index database: " << path << "\n";
      return 1;
    }
    file_id = rec->id;
  }
  const std::vector<Symbol> hits =
      db.list_symbols(comp ? std::optional<int64_t>(comp->id) : std::nullopt,
                      args.dir, file_id, args.pattern, args.kind);
  fmt::print_symbols(db, hits, args.limit, *ctx.out);
  return hits.empty() ? 1 : 0;
}

int cmd_show_symbol(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  const std::string &ref = args.symbol;
  const std::optional<Symbol> s =
      is_digits(ref)
          ? db.lookup_symbol_by_id(std::strtoll(ref.c_str(), nullptr, 10))
          : db.lookup_symbol(ref);
  if (!s) {
    *ctx.err << "error: no symbol with id/USR " << fmt::py_repr(ref) << "\n";
    return 1;
  }

  const auto loc =
      [&db](const std::optional<int64_t> &file_id,
            const std::optional<int64_t> &line,
            const std::optional<int64_t> &col) -> std::optional<std::string> {
    if (!file_id) {
      return std::nullopt;
    }
    return fmt::py_str(db.file_abs_path(*file_id)) + ":" + fmt::py_str(line) +
           ":" + fmt::py_str(col);
  };

  const std::optional<Symbol> parent =
      (s->parent_usr && !s->parent_usr->empty())
          ? db.lookup_symbol(*s->parent_usr)
          : std::nullopt;

  std::optional<std::string> visibility;
  if (s->linkage) {
    if (*s->linkage == "external") {
      visibility = "program-wide (usable from any .cpp)";
    } else if (*s->linkage == "internal") {
      visibility = "file-local (static / anonymous namespace)";
    } else if (*s->linkage == "no-linkage") {
      visibility = "local scope only";
    } else {
      visibility = *s->linkage;
    }
  }

  std::optional<std::string> parent_field = s->parent_usr;
  if (parent) {
    parent_field =
        fmt::py_str(parent->qual_name) + "  [" + *s->parent_usr + "]";
  }

  // declaration: a registered decl site, else the raw external decl_path of a
  // stub whose target lives in an unregistered (system/stdlib) file.
  std::optional<std::string> declaration =
      loc(s->decl_file_id, s->decl_line, s->decl_col);
  if (!declaration && s->decl_path) {
    declaration = *s->decl_path + ":" + fmt::py_str(s->decl_line) + ":" +
                  fmt::py_str(s->decl_col);
  }

  const std::vector<std::pair<const char *, std::optional<std::string>>>
      fields = {
          {"id", std::to_string(s->id)},
          {"usr", s->usr},
          {"name", s->spelling},
          {"qualified", s->qual_name},
          {"display", s->display_name},
          {"kind", s->kind},
          {"type", s->type_info},
          {"visibility", visibility},
          {"access", s->access},
          {"parent", parent_field},
          {"pure", s->is_pure ? std::optional<std::string>(
                                    "yes (pure virtual; implemented by "
                                    "overriders)")
                              : std::nullopt},
          {"definition",
           s->is_definition ? loc(s->file_id, s->line, s->col) : std::nullopt},
          {"declaration", declaration},
          {"resolved", s->resolved  ? std::string("yes")
                       : s->is_pure ? std::string("n/a (pure virtual)")
                                    : std::string("no (definition not seen)")},
      };
  for (const auto &field : fields) {
    if (field.second) {
      fmt::print_field(*ctx.out, field.first, *field.second);
    }
  }
  return 0;
}

int cmd_show_file(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  std::optional<Component> comp;
  if (!lookup_component(db, args.component, comp, *ctx.err)) {
    return 1;
  }
  const std::string &ref = args.file;
  std::optional<File> rec;
  std::optional<std::string> path;
  if (is_digits(ref)) { // first column of 'list files'
    rec = db.get_file_by_id(std::strtoll(ref.c_str(), nullptr, 10));
    if (rec) {
      path = db.file_abs_path(rec->id);
    }
  } else {
    path = files::resolve_file_arg(
        ref, comp ? std::optional<std::string>(db.component_abs_base(*comp)) : std::nullopt);
    rec = db.get_file(*path);
  }
  if (!rec || !path) {
    *ctx.err << "error: not in index database: " << ref << "\n";
    return 1;
  }

  const std::optional<Directory> d = db.get_directory_by_id(rec->directory_id);
  const std::optional<Component> owner =
      d ? db.get_component_by_id(d->component_id) : std::nullopt;
  const std::vector<Symbol> syms =
      db.list_symbols(std::nullopt, std::nullopt, rec->id);
  int64_t defined = 0;
  int64_t declared = 0;
  std::map<std::string, int64_t> by_kind;
  for (const Symbol &s : syms) {
    if (s.file_id && *s.file_id == rec->id && s.is_definition) {
      ++defined;
    }
    if (s.decl_file_id && *s.decl_file_id == rec->id) {
      ++declared;
    }
    ++by_kind[s.kind];
  }
  const std::vector<Diagnostic> diags = db.get_diagnostics(rec->id);
  std::map<int, int64_t> diag_counts;
  for (const Diagnostic &dg : diags) {
    ++diag_counts[dg.severity];
  }
  std::optional<std::string> by_kind_field;
  if (!by_kind.empty()) { // std::map iterates sorted — Python sorted(items)
    std::string joined;
    for (const auto &entry : by_kind) {
      if (!joined.empty()) {
        joined += ", ";
      }
      joined += entry.first + ": " + std::to_string(entry.second);
    }
    by_kind_field = joined;
  }
  std::optional<std::string> options_field;
  if (rec->compile_options && !rec->compile_options->empty()) {
    std::string joined;
    for (const std::string &opt : *rec->compile_options) {
      if (!joined.empty()) {
        joined += " ";
      }
      joined += opt;
    }
    options_field = joined;
  } else {
    options_field = "(none -- header indexed via an including TU)";
  }

  const std::vector<std::pair<const char *, std::optional<std::string>>>
      fields = {
          {"id", std::to_string(rec->id)},
          {"path", *path},
          {"component",
           owner ? std::optional<std::string>(owner->name + " (" + owner->kind +
                                              ")  " + owner->path)
                 : std::nullopt},
          {"directory",
           d ? std::optional<std::string>(d->path.empty() ? "." : d->path)
             : std::nullopt},
          {"mtime", rec->mtime ? std::optional<std::string>(
                                     fmt::format_mtime(*rec->mtime))
                               : std::nullopt},
          {"md5", rec->md5},
          {"driver", rec->driver},
          {"options", options_field},
          {"indexed", std::string(files::index_status_reason(
                          files::index_status(*rec, *path)))},
          {"indexed at", rec->indexed_at ? std::optional<std::string>(
                                               *rec->indexed_at + " UTC")
                                         : std::nullopt},
          {"symbols", std::to_string(syms.size()) + " (" +
                          std::to_string(defined) + " defined here, " +
                          std::to_string(declared) + " declared here)"},
          {"by kind", by_kind_field},
          {"diagnostics", diags.empty()
                              ? std::nullopt
                              : std::optional<std::string>(
                                    diag_summary(diag_counts))},
      };
  for (const auto &field : fields) {
    if (field.second) {
      fmt::print_field(*ctx.out, field.first, *field.second);
    }
  }
  // Each captured parse diagnostic, in TU order, under the summary.
  for (const Diagnostic &dg : diags) {
    const char *lbl = diag_severity_label(dg.severity);
    const std::string label =
        lbl != nullptr ? std::string(lbl) : std::to_string(dg.severity);
    const std::string locstr =
        dg.file_path ? (*dg.file_path + ":" + std::to_string(*dg.line) + ":" +
                        std::to_string(*dg.col))
                     : std::string("<no location>");
    *ctx.out << "  " << fmt::ljust(label, 7) << " " << locstr << ": "
             << dg.spelling << "\n";
  }
  return 0;
}

int cmd_delete_component(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  std::vector<Component> matches;
  if (args.del_id) {
    if (std::optional<Component> c = db.get_component_by_id(*args.del_id)) {
      matches.push_back(*c);
    }
  } else if (args.del_path) {
    if (std::optional<Component> c =
            db.get_component(pathutil::abspath(*args.del_path))) {
      matches.push_back(*c);
    }
  } else { // name
    for (const Component &c : db.list_components()) {
      if (c.name == *args.name) {
        matches.push_back(c);
      }
    }
  }
  if (matches.empty()) {
    *ctx.err << "error: no component matches " << selector_str(args) << "\n";
    return 1;
  }
  std::vector<int64_t> ids;
  std::vector<std::string> lines;
  for (const Component &c : matches) {
    ids.push_back(c.id);
    lines.push_back("  #" + std::to_string(c.id) + "  " + c.name + " (" +
                    c.kind + ")  " + c.path);
  }
  return finish_delete(
      args, ctx, ids, lines, [&db](int64_t id) { db.delete_component(id); },
      "component", "components");
}

int cmd_delete_dir(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  std::optional<Component> comp;
  if (!lookup_component(db, args.component, comp, *ctx.err)) {
    return 1;
  }
  std::vector<Directory> matches;
  if (args.del_id) {
    if (std::optional<Directory> d = db.get_directory_by_id(*args.del_id)) {
      if (under_component(db, db.directory_abs_path(d->id), comp)) {
        matches.push_back(*d);
      }
    }
  } else { // path
    const std::string target = pathutil::abspath(*args.del_path);
    const std::optional<int64_t> scope =
        comp ? std::optional<int64_t>(comp->id) : std::nullopt;
    for (const std::pair<Directory, std::string> &pr :
         db.list_directories(scope)) {
      const std::optional<std::string> ap =
          db.directory_abs_path(pr.first.id);
      if (ap && *ap == target) {
        matches.push_back(pr.first);
      }
    }
  }
  if (matches.empty()) {
    *ctx.err << "error: no directory matches " << selector_str(args) << "\n";
    return 1;
  }
  std::vector<int64_t> ids;
  std::vector<std::string> lines;
  for (const Directory &d : matches) {
    ids.push_back(d.id);
    lines.push_back("  #" + std::to_string(d.id) + "  " +
                    db.directory_abs_path(d.id).value_or(""));
  }
  return finish_delete(
      args, ctx, ids, lines, [&db](int64_t id) { db.delete_directory(id); },
      "directory", "directories");
}

int cmd_delete_file(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  std::optional<Component> comp;
  if (!lookup_component(db, args.component, comp, *ctx.err)) {
    return 1;
  }
  std::vector<std::pair<int64_t, std::string>> matches; // (id, abs path)
  if (args.del_id) {
    if (std::optional<File> rec = db.get_file_by_id(*args.del_id)) {
      const std::optional<std::string> ap = db.file_abs_path(rec->id);
      if (under_component(db, ap, comp)) {
        matches.emplace_back(rec->id, ap.value_or(""));
      }
    }
  } else if (args.del_path) {
    const std::string ap = files::resolve_file_arg(
        *args.del_path,
        comp ? std::optional<std::string>(db.component_abs_base(*comp)) : std::nullopt);
    if (std::optional<File> rec = db.get_file(ap)) {
      if (under_component(db, ap, comp)) {
        matches.emplace_back(rec->id, ap);
      }
    }
  } else { // name (basename)
    for (const std::pair<File, std::string> &pr : db.list_files()) {
      if (pathutil::basename(pr.second) == *args.name &&
          under_component(db, pr.second, comp)) {
        matches.emplace_back(pr.first.id, pr.second);
      }
    }
  }
  if (matches.empty()) {
    *ctx.err << "error: no file matches " << selector_str(args) << "\n";
    return 1;
  }
  std::vector<int64_t> ids;
  std::vector<std::string> lines;
  for (const std::pair<int64_t, std::string> &m : matches) {
    ids.push_back(m.first);
    lines.push_back("  #" + std::to_string(m.first) + "  " + m.second);
  }
  return finish_delete(
      args, ctx, ids, lines, [&db](int64_t id) { db.delete_file(id); }, "file",
      "files");
}

int cmd_delete_symbol(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  std::optional<Component> comp;
  if (!lookup_component(db, args.component, comp, *ctx.err)) {
    return 1;
  }
  std::vector<Symbol> matches;
  if (args.del_id) {
    if (std::optional<Symbol> s = db.lookup_symbol_by_id(*args.del_id)) {
      matches.push_back(*s);
    }
  } else if (args.usr) {
    if (std::optional<Symbol> s = db.lookup_symbol(*args.usr)) {
      matches.push_back(*s);
    }
  } else { // name (spelling)
    matches = db.lookup_symbols_by_name(*args.name);
  }
  if (comp) {
    std::vector<Symbol> kept;
    for (const Symbol &s : matches) {
      const std::optional<std::string> here =
          s.file_id ? db.file_abs_path(*s.file_id) : std::nullopt;
      const std::optional<std::string> decl =
          s.decl_file_id ? db.file_abs_path(*s.decl_file_id) : std::nullopt;
      if ((here && under_component(db, here, comp)) ||
          (decl && under_component(db, decl, comp))) {
        kept.push_back(s);
      }
    }
    matches = kept;
  }
  if (matches.empty()) {
    *ctx.err << "error: no symbol matches " << selector_str(args) << "\n";
    return 1;
  }
  std::vector<int64_t> ids;
  std::vector<std::string> lines;
  for (const Symbol &s : matches) {
    ids.push_back(s.id);
    const std::string qual =
        (s.qual_name && !s.qual_name->empty()) ? *s.qual_name : s.spelling;
    lines.push_back("  #" + std::to_string(s.id) + "  " + s.kind + "  " + qual);
  }
  return finish_delete(
      args, ctx, ids, lines, [&db](int64_t id) { db.delete_symbol(id); },
      "symbol", "symbols");
}

int cmd_resolve(const ParsedArgs &args, Context &ctx) {
  (void)args;
  Storage db(ctx.index_path);
  const int stubs = db.resolve_pass();
  const std::vector<Edge> cross = db.cross_repo_edges();
  // ISO 8601 UTC timestamp matching Python's
  // datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ").
  {
    std::time_t now = std::time(nullptr);
    char buf[32];
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", std::gmtime(&now));
    auto st = db.raw_db().prepare(
        "INSERT OR REPLACE INTO meta (key, value) "
        "VALUES ('graph_resolved_at', ?)");
    st.bind(1, std::string_view(buf));
    st.step_done();
  }
  *ctx.out << "resolve: " << stubs << " still-stub, "
           << cross.size() << " cross-repo edge(s)\n";
  return 0;
}

// cmd_pch_build (pch.py cmd_build) -- derive the common C++ flags + dominant
// driver from the index, then compile the umbrella into a cached PCH.
int cmd_pch_build(const ParsedArgs &args, Context &ctx) {
  Logger &log = ctx.logger != nullptr ? *ctx.logger : Logger::root();
  {
    struct stat st {};
    if (::stat(pch::pch_path().c_str(), &st) == 0 && !args.force) {
      *ctx.err << "system PCH already exists: " << pch::pch_path()
               << " (use --force to rebuild)\n";
      return 1;
    }
  }

  std::optional<std::set<std::string>> common;
  std::map<std::optional<std::string>, int> driver_counts;
  int n_cpp = 0;
  std::vector<pch::TuFlags> tus;             // for --from-corpus survey
  std::vector<std::pair<std::string, std::string>> idirs; // union, order-stable
  std::set<std::pair<std::string, std::string>> seen_idirs;
  {
    Storage db(ctx.index_path);
    for (const std::pair<File, std::string> &pr : db.list_files()) {
      const File &rec = pr.first;
      const std::string &path = pr.second;
      if (!rec.compile_options || rec.compile_options->empty()) {
        continue;
      }
      const std::vector<std::string> opts = CompileDb::resolve_options(
          CompileDb::sanitize(*rec.compile_options),
          [&db](const std::string &n) { return db.get_alias(n); });
      if (!Toolchain::is_cpp(path, opts)) {
        continue;
      }
      ++n_cpp;
      const std::vector<std::string> rel = pch::pch_relevant(opts);
      std::set<std::string> s(rel.begin(), rel.end());
      if (!common) {
        common = std::move(s);
      } else {
        std::set<std::string> inter;
        std::set_intersection(common->begin(), common->end(), s.begin(),
                              s.end(),
                              std::inserter(inter, inter.begin()));
        common = std::move(inter);
      }
      ++driver_counts[rec.driver];
      if (args.pch_from_corpus) {
        tus.push_back({rec.driver, opts, path});
        for (const std::pair<std::string, std::string> &p :
             pch::include_dirs(opts)) {
          if (seen_idirs.insert(p).second) {
            idirs.push_back(p);
          }
        }
      }
    }
  }
  if (n_cpp == 0) {
    *ctx.err << "error: no C++ translation units in the index; nothing to "
                "build a C++ PCH from\n";
    return 1;
  }

  std::optional<std::string> dom_driver;
  int best = -1;
  for (const auto &kv : driver_counts) {
    if (kv.second > best) {
      best = kv.second;
      dom_driver = kv.first;
    }
  }
  const std::optional<std::string> driver =
      args.pch_driver ? args.pch_driver : dom_driver;

  std::vector<std::string> flags(common->begin(), common->end()); // sorted set
  if (args.pch_std) {
    std::vector<std::string> kept;
    for (const std::string &f : flags) {
      if (f.rfind("-std=", 0) != 0) {
        kept.push_back(f);
      }
    }
    kept.push_back("-std=" + *args.pch_std);
    flags = std::move(kept);
  }

  std::vector<std::string> headers;
  bool quoted = false;
  if (args.pch_from_corpus) {
    const pch::HeaderSurvey survey = pch::survey_headers(tus, args.pch_jobs);
    headers = pch::select_shared_headers(survey, n_cpp, args.pch_coverage,
                                         args.pch_min_tus);
    if (headers.empty()) {
      *ctx.err << "error: no header is shared by >= " << args.pch_coverage
               << " of " << n_cpp
               << " C++ TUs (lower --coverage / --min-tus, or this corpus is "
                  "too heterogeneous for a single shared PCH)\n";
      return 1;
    }
    // Retain the union of include-search dirs so project headers + their
    // transitive #includes resolve while building the umbrella.
    for (const std::pair<std::string, std::string> &p : idirs) {
      flags.push_back(p.first);
      flags.push_back(p.second);
    }
    quoted = true;
    *ctx.err << "corpus survey: " << survey.freq.size() << " headers across "
             << n_cpp << " C++ TUs; " << headers.size()
             << " shared by >= " << args.pch_coverage << "\n";
  } else {
    headers = pch::default_headers();
  }
  for (const std::string &f : args.pch_add_flags) {
    flags.push_back(f);
  }
  for (const std::string &h : args.pch_add_headers) {
    headers.push_back(h);
  }

  Toolchain toolchain(log);
  Parser parser(toolchain, log);
  return pch::build_pch(parser, flags, headers, driver, n_cpp, *ctx.out,
                        *ctx.err, quoted, args.pch_from_corpus,
                        args.pch_coverage);
}

int cmd_pch_status(const ParsedArgs &args, Context &ctx) {
  (void)args;
  return pch::status_pch(*ctx.out);
}

int cmd_pch_clear(const ParsedArgs &args, Context &ctx) {
  (void)args;
  return pch::clear_pch(*ctx.out);
}

namespace {

std::string str_trim(const std::string &s) {
  std::size_t a = s.find_first_not_of(" \t");
  if (a == std::string::npos) {
    return "";
  }
  std::size_t b = s.find_last_not_of(" \t");
  return s.substr(a, b - a + 1);
}

std::string str_lower(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(),
                 [](unsigned char c) { return std::tolower(c); });
  return s;
}

// Parse 'FIELD = VALUE' in any spacing -> (field, value). Returns false on a
// malformed assignment. Mirrors cli.py _parse_assignment.
bool parse_assignment(const std::vector<std::string> &tokens, std::string &key,
                      std::string &val) {
  std::string expr;
  for (std::size_t k = 0; k < tokens.size(); ++k) {
    if (k != 0) {
      expr += " ";
    }
    expr += tokens[k];
  }
  const std::size_t eq = expr.find('=');
  if (eq != std::string::npos) {
    key = expr.substr(0, eq);
    val = expr.substr(eq + 1);
  } else {
    // exactly two whitespace-separated tokens ("pending False")
    std::vector<std::string> parts;
    std::size_t p = 0;
    while (p < expr.size()) {
      while (p < expr.size() && (expr[p] == ' ' || expr[p] == '\t')) {
        ++p;
      }
      std::size_t q = p;
      while (q < expr.size() && expr[q] != ' ' && expr[q] != '\t') {
        ++q;
      }
      if (q > p) {
        parts.push_back(expr.substr(p, q - p));
      }
      p = q;
    }
    if (parts.size() != 2) {
      return false;
    }
    key = parts[0];
    val = parts[1];
  }
  key = str_lower(str_trim(key));
  val = str_trim(val);
  return !key.empty() && !val.empty();
}

// true/false/1/0/yes/no/on/off (case-insensitive). Mirrors _parse_set_bool.
bool parse_set_bool(const std::string &raw, bool &out) {
  const std::string t = str_lower(str_trim(raw));
  if (t == "true" || t == "1" || t == "yes" || t == "on" || t == "t" ||
      t == "y") {
    out = true;
    return true;
  }
  if (t == "false" || t == "0" || t == "no" || t == "off" || t == "f" ||
      t == "n") {
    out = false;
    return true;
  }
  return false;
}

// -- `cidx file` / `dump-compile-commands` helpers --------------------------

// JSON string literal matching Python json.dumps default (ensure_ascii):
// escapes " \ and the C0 control chars (short forms for \b\f\n\r\t, \u00xx
// otherwise); bytes >= 0x20 pass through (ASCII inputs are byte-identical to
// Python — paths/flags are ASCII).
std::string py_json_string(const std::string &s) {
  std::string out = "\"";
  for (const unsigned char c : s) {
    switch (c) {
    case '"':
      out += "\\\"";
      break;
    case '\\':
      out += "\\\\";
      break;
    case '\n':
      out += "\\n";
      break;
    case '\r':
      out += "\\r";
      break;
    case '\t':
      out += "\\t";
      break;
    case '\b':
      out += "\\b";
      break;
    case '\f':
      out += "\\f";
      break;
    default:
      if (c < 0x20) {
        char buf[8];
        std::snprintf(buf, sizeof(buf), "\\u%04x", c);
        out += buf;
      } else {
        out += static_cast<char>(c);
      }
    }
  }
  out += "\"";
  return out;
}

// json.dumps(list[str]) default form: ["a", "b"] (", " separator). Used by
// `cidx file -dump-args`.
std::string py_json_str_array(const std::vector<std::string> &items) {
  std::string out = "[";
  for (std::size_t i = 0; i < items.size(); ++i) {
    if (i != 0) {
      out += ", ";
    }
    out += py_json_string(items[i]);
  }
  out += "]";
  return out;
}

struct CcEntry {
  std::string directory;
  std::string file;
  std::vector<std::string> arguments;
};

// json.dumps(entries, indent=2): the compile_commands.json array. Byte-matches
// Python's pretty form (2-space indent, ": " / ",\n" separators).
std::string dump_cc_json(const std::vector<CcEntry> &entries) {
  if (entries.empty()) {
    return "[]";
  }
  std::string out = "[\n";
  for (std::size_t i = 0; i < entries.size(); ++i) {
    const CcEntry &e = entries[i];
    out += "  {\n";
    out += "    \"directory\": " + py_json_string(e.directory) + ",\n";
    out += "    \"file\": " + py_json_string(e.file) + ",\n";
    out += "    \"arguments\": ";
    if (e.arguments.empty()) {
      out += "[]";
    } else {
      out += "[\n";
      for (std::size_t j = 0; j < e.arguments.size(); ++j) {
        out += "      " + py_json_string(e.arguments[j]);
        out += (j + 1 < e.arguments.size()) ? ",\n" : "\n";
      }
      out += "    ]";
    }
    out += "\n  }";
    out += (i + 1 < entries.size()) ? ",\n" : "\n";
  }
  out += "]";
  return out;
}

// compiledb.commands_from_text (Python): write the JSON (a lone entry object is
// wrapped in an array) to a throwaway compile_commands.json and load it through
// the same CompilationDatabase path `import` uses, so `-import-args` strips
// args identically. Each entry needs directory, file, and arguments/command.
std::vector<CompileCommand> commands_from_text(const std::string &text) {
  std::size_t b = 0;
  while (b < text.size() &&
         std::isspace(static_cast<unsigned char>(text[b])) != 0) {
    ++b;
  }
  const std::string payload =
      (b < text.size() && text[b] == '{') ? ("[" + text + "]") : text;
  char tmpl[] = "/tmp/cidx_file_XXXXXX";
  char *dir = ::mkdtemp(tmpl);
  if (dir == nullptr) {
    throw CidxError("could not create a temporary directory for -import-args");
  }
  const std::string dpath = dir;
  const std::string fpath = dpath + "/compile_commands.json";
  {
    std::ofstream fh(fpath);
    fh << payload;
  }
  std::vector<CompileCommand> out;
  try {
    out = CompileDb::load(dpath);
  } catch (...) {
    ::unlink(fpath.c_str());
    ::rmdir(dpath.c_str());
    throw;
  }
  ::unlink(fpath.c_str());
  ::rmdir(dpath.c_str());
  return out;
}

// _parse_file_target (cli.py): 'COMPONENT://RELPATH' -> (component, abs_path).
// false + err set on a malformed target or unknown component. The relative
// path resolves against the component root; a leading '/' is stripped so the
// address can never escape the component.
bool parse_file_target(Storage &db, const std::string &target,
                       std::optional<Component> &comp, std::string &abs_path,
                       std::string &err) {
  const std::string sep = "://";
  const std::size_t pos = target.find(sep);
  const std::string malformed =
      "expected COMPONENT://PATH (e.g. 'mylib://src/foo.c'), got " +
      fmt::py_repr(target);
  if (pos == std::string::npos) {
    err = malformed;
    return false;
  }
  const std::string comp_name = target.substr(0, pos);
  std::string rel = target.substr(pos + sep.size());
  if (comp_name.empty() || rel.empty()) {
    err = malformed;
    return false;
  }
  comp = db.get_component_by_name(comp_name);
  if (!comp) {
    err = "no component named " + fmt::py_repr(comp_name);
    return false;
  }
  while (!rel.empty() && rel.front() == '/') {
    rel.erase(rel.begin());
  }
  abs_path = pathutil::normpath(pathutil::join(db.component_abs_base(*comp), rel));
  return true;
}

} // namespace

// cmd_set (cli.py cmd_set): set a mutable file attribute (the pending/indexed
// flag) over a component's files or one file, WITHOUT deleting any symbols.
int cmd_set(const ParsedArgs &args, Context &ctx) {
  std::string key;
  std::string raw_val;
  if (!parse_assignment(args.assignment, key, raw_val)) {
    *ctx.err << "error: expected 'FIELD=VALUE' (e.g. pending=False)\n";
    return 1;
  }
  // field -> invert (pending is the inverse of the 'indexed' flag).
  bool invert = false;
  if (key == "pending") {
    invert = true;
  } else if (key == "indexed") {
    invert = false;
  } else {
    *ctx.err << "error: unknown field '" << key
             << "'; supported: indexed, pending\n";
    return 1;
  }
  bool bval = false;
  if (!parse_set_bool(raw_val, bval)) {
    *ctx.err << "error: expected a boolean (true/false), got '" << raw_val
             << "'\n";
    return 1;
  }
  const bool indexed_value = invert ? !bval : bval;

  Storage db(ctx.index_path);
  std::optional<Component> comp;
  if (!lookup_component(db, args.component, comp, *ctx.err)) {
    return 1;
  }
  std::vector<std::pair<int64_t, std::string>> matches; // (id, abs path)
  if (args.file_filter) {
    const std::string ap = files::resolve_file_arg(
        *args.file_filter,
        comp ? std::optional<std::string>(db.component_abs_base(*comp)) : std::nullopt);
    if (std::optional<File> rec = db.get_file(ap)) {
      if (under_component(db, ap, comp)) {
        matches.emplace_back(rec->id, ap);
      }
    }
  } else {
    for (const std::pair<File, std::string> &pr : db.list_files(
             comp ? std::optional<int64_t>(comp->id) : std::nullopt)) {
      matches.emplace_back(pr.first.id, pr.second);
    }
  }
  if (matches.empty()) {
    *ctx.err << "error: no files match the given selector\n";
    return 1;
  }
  for (const std::pair<int64_t, std::string> &m : matches) {
    *ctx.out << "  #" << m.first << "  " << m.second << "\n";
  }
  if (!args.dry_run) {
    for (const std::pair<int64_t, std::string> &m : matches) {
      db.set_file_indexed(m.first, indexed_value);
    }
  }
  *ctx.out << (args.dry_run ? "would set " : "set ") << key << "="
           << (bval ? "True" : "False") << " on " << matches.size() << " "
           << plural(matches.size(), "file", "files") << "\n";
  return 0;
}

// cmd_file (cli.py cmd_file): inspect or edit one file's stored compile flags,
// addressed as COMPONENT://RELPATH. Edits mark the file args_overridden so a
// later `import` (without --force) keeps them.
int cmd_file(const ParsedArgs &args, Context &ctx) {
  std::vector<std::string> op = args.op;
  if (op.empty()) {
    op.emplace_back("-dump-args");
  }
  const std::string action = op[0];
  const std::vector<std::string> rest(op.begin() + 1, op.end());
  static const char *const kFileOps[] = {"-set-flag", "-unset-flag",
                                         "-import-args", "-dump-args"};
  bool known = false;
  for (const char *o : kFileOps) {
    if (action == o) {
      known = true;
    }
  }
  if (!known) {
    *ctx.err << "error: unknown operation " << fmt::py_repr(action)
             << "; supported: -set-flag, -unset-flag, -import-args, "
                "-dump-args\n";
    return 2;
  }

  Storage db(ctx.index_path);
  std::optional<Component> comp;
  std::string ap;
  std::string err;
  if (!parse_file_target(db, args.target, comp, ap, err)) {
    *ctx.err << "error: " << err << "\n";
    return 1;
  }
  const std::optional<File> rec = db.get_file(ap);
  if (!rec) {
    *ctx.err << "error: not in index database: " << ap << "\n";
    return 1;
  }
  std::vector<std::string> opts =
      rec->compile_options ? *rec->compile_options : std::vector<std::string>{};

  if (action == "-dump-args") {
    *ctx.out << py_json_str_array(opts) << "\n";
    return 0;
  }

  if (action == "-set-flag" || action == "-unset-flag") {
    if (rest.size() != 1) {
      *ctx.err << "error: " << action << " takes exactly one FLAG\n";
      return 2;
    }
    const std::string &flag = rest[0];
    if (action == "-set-flag") {
      if (std::find(opts.begin(), opts.end(), flag) != opts.end()) {
        *ctx.out << "flag already present on " << ap << ": " << flag << "\n";
        return 0;
      }
      opts.push_back(flag);
      db.set_file_compile_options(rec->id, opts);
      *ctx.out << "added flag to " << ap << ": " << flag << "\n";
      return 0;
    }
    const std::size_t n =
        static_cast<std::size_t>(std::count(opts.begin(), opts.end(), flag));
    if (n == 0) {
      *ctx.out << "flag not present on " << ap << ": " << flag << "\n";
      return 0;
    }
    std::vector<std::string> kept;
    for (const std::string &o : opts) {
      if (o != flag) {
        kept.push_back(o);
      }
    }
    db.set_file_compile_options(rec->id, kept);
    *ctx.out << "removed flag from " << ap << ": " << flag << " (" << n << " "
             << plural(n, "occurrence", "occurrences") << ")\n";
    return 0;
  }

  // -import-args
  if (rest.size() != 1) {
    *ctx.err << "error: -import-args takes exactly one JSON entry (or @FILE)\n";
    return 2;
  }
  std::string raw = rest[0];
  if (!raw.empty() && raw[0] == '@') {
    const std::string path = raw.substr(1);
    std::ifstream fh(path);
    if (!fh) {
      *ctx.err << "error: cannot read " << path << ": "
               << std::strerror(errno) << "\n";
      return 1;
    }
    std::stringstream ss;
    ss << fh.rdbuf();
    raw = ss.str();
  }
  std::vector<CompileCommand> commands;
  try {
    commands = commands_from_text(raw);
  } catch (const CidxError &e) {
    *ctx.err << "error: -import-args: cannot parse compile command: " << e.what()
             << "\n";
    return 1;
  }
  if (commands.empty()) {
    *ctx.err << "error: -import-args: no compile command found (need directory, "
                "file, and arguments/command)\n";
    return 1;
  }
  const CompileCommand &cmd = commands[0];
  db.set_file_compile_options(rec->id, cmd.args, cmd.driver, true);
  *ctx.out << "imported " << cmd.args.size() << " arg(s) for " << ap << "\n";
  return 0;
}

// cmd_dump_compile_commands (cli.py): emit a compile_commands.json for a
// component — one entry per file that has stored compile flags.
int cmd_dump_compile_commands(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  std::optional<Component> comp;
  if (!lookup_component(db, args.component, comp, *ctx.err)) {
    return 1;
  }
  std::vector<CcEntry> entries;
  const auto files = db.list_files(
      comp ? std::optional<int64_t>(comp->id) : std::nullopt);
  for (const std::pair<File, std::string> &pr : files) {
    const File &f = pr.first;
    const std::string &ap = pr.second;
    if (!f.compile_options || f.compile_options->empty()) {
      continue;
    }
    CcEntry e;
    // v14: use effective root (base+version) as the directory field so the
    // emitted compile_commands.json paths are consistent with the stored flags.
    e.directory =
        comp ? db.component_abs_base(*comp) : pathutil::split(ap).first;
    e.file = ap;
    e.arguments.push_back(f.driver ? *f.driver : "cc");
    for (const std::string &o : *f.compile_options) {
      e.arguments.push_back(o);
    }
    e.arguments.push_back(ap);
    entries.push_back(std::move(e));
  }
  *ctx.out << dump_cc_json(entries) << "\n";
  return 0;
}

// ---------------------------------------------------------------------------
// AST sub-command handlers (ADR-006 M5)
// Mirrors Python astcmd.cmd_dump / cmd_locals / cmd_conditions / cmd_cache.
// ---------------------------------------------------------------------------

// Helper: find the focus cursor in the TU's main-file top-level cursors.
static CXCursor find_focus(CXTranslationUnit tu,
                           const std::string &filename,
                           const AstTarget &t) {
  CXCursor found = clang_getNullCursor();
  for_file_cursors(tu, filename, [&](CXCursor c) {
    if (clang_Cursor_isNull(found)) {
      if (t.focus_usr) {
        CXString usr = clang_getCursorUSR(c);
        const char *s = clang_getCString(usr);
        if (s && *t.focus_usr == s) {
          found = c;
        }
        clang_disposeString(usr);
      } else if (t.focus_name) {
        CXString sp = clang_getCursorSpelling(c);
        const char *s = clang_getCString(sp);
        if (s && *t.focus_name == s) {
          found = c;
        }
        clang_disposeString(sp);
      }
    }
  });
  return found;
}

int cmd_ast_dump(const ParsedArgs &args, Context &ctx) {
  auto [t_opt, rc] = resolve_target(args, ctx);
  if (!t_opt) {
    return rc;
  }
  const AstTarget &t = *t_opt;

  auto tu_opt = astcache::load_or_parse(t, args.use_cache, ctx.err);
  if (!tu_opt) {
    return 1;
  }

  std::optional<int> max_depth;
  if (args.depth > 0) {
    max_depth = args.depth;
  }

  if (t.whole_file()) {
    // Collect top-level cursors in main file.
    std::vector<CXCursor> roots;
    for_file_cursors(tu_opt->tu, t.abspath, [&](CXCursor c) {
      roots.push_back(c);
    });

    if (args.ast_json) {
      json_out::Array arr;
      for (const CXCursor &c : roots) {
        arr.push_back(cursor_json(c, 0, max_depth, args.tokens, args.types));
      }
      *ctx.out << json_out::dumps_indent2(json_out::Value::arr(std::move(arr)))
               << "\n";
    } else {
      for (const CXCursor &c : roots) {
        dump_text(*ctx.out, c, 0, max_depth, args.tokens, args.types);
      }
    }
  } else {
    CXCursor focus = find_focus(tu_opt->tu, t.abspath, t);
    if (clang_Cursor_isNull(focus)) {
      const std::string sel =
          t.focus_usr ? *t.focus_usr : t.focus_name ? *t.focus_name : "?";
      *ctx.err << "error: could not locate '" << sel << "' in "
               << pathutil::basename(t.abspath) << "\n";
      return 1;
    }
    if (args.ast_json) {
      json_out::Array arr;
      arr.push_back(cursor_json(focus, 0, max_depth, args.tokens, args.types));
      *ctx.out << json_out::dumps_indent2(json_out::Value::arr(std::move(arr)))
               << "\n";
    } else {
      dump_text(*ctx.out, focus, 0, max_depth, args.tokens, args.types);
    }
  }
  return 0;
}

// Helper: resolve target and find the focus function cursor, printing errors.
static std::pair<std::optional<CXCursor>, int>
focus_function(const ParsedArgs & /*args*/, Context &ctx,
               const AstTarget &t, CXTranslationUnit tu) {
  if (t.whole_file()) {
    *ctx.err << "error: this command needs a function "
                "(use --name/--usr/--id, or 'COMPONENT://path --name fn')\n";
    return {std::nullopt, 1};
  }
  CXCursor focus = find_focus(tu, t.abspath, t);
  if (clang_Cursor_isNull(focus)) {
    const std::string sel =
        t.focus_usr ? *t.focus_usr : t.focus_name ? *t.focus_name : "?";
    *ctx.err << "error: could not locate '" << sel << "' in "
             << pathutil::basename(t.abspath) << "\n";
    return {std::nullopt, 1};
  }
  if (!is_function_kind(clang_getCursorKind(focus))) {
    CXString sp = clang_getCursorSpelling(focus);
    const char *name = clang_getCString(sp);
    // B2: Python uses kind.name (e.g. "STRUCT_DECL"), not clang_getCursorKindSpelling
    // (which returns "StructDecl"). Use cli::kind_name for byte-identical output.
    const char *ks = cli::kind_name(static_cast<unsigned>(clang_getCursorKind(focus)));
    *ctx.err << "error: '" << (name ? name : "?") << "' is a "
             << ks << ", not a function\n";
    clang_disposeString(sp);
    return {std::nullopt, 1};
  }
  return {focus, 0};
}

int cmd_ast_locals(const ParsedArgs &args, Context &ctx) {
  auto [t_opt, rc] = resolve_target(args, ctx);
  if (!t_opt) {
    return rc;
  }
  const AstTarget &t = *t_opt;

  auto tu_opt = astcache::load_or_parse(t, args.use_cache, ctx.err);
  if (!tu_opt) {
    return 1;
  }

  auto [focus_opt, frc] = focus_function(args, ctx, t, tu_opt->tu);
  if (!focus_opt) {
    return frc;
  }
  const CXCursor &focus = *focus_opt;

  struct Row {
    std::string name;
    std::string type;
    std::string kind; // "param" or "local"
    std::string loc;
  };
  std::vector<Row> rows;
  for (const SubtreeNode &node : subtree(focus)) {
    const CXCursorKind k = clang_getCursorKind(node.cursor);
    const bool is_var = (k == CXCursor_VarDecl);
    const bool is_param = (k == CXCursor_ParmDecl);
    if (!is_var && (!args.params || !is_param)) {
      continue;
    }
    Row r;
    CXString sp = clang_getCursorSpelling(node.cursor);
    r.name = clang_getCString(sp) ? clang_getCString(sp) : "";
    clang_disposeString(sp);
    CXType ty = clang_getCursorType(node.cursor);
    CXString tsp = clang_getTypeSpelling(ty);
    r.type = clang_getCString(tsp) ? clang_getCString(tsp) : "";
    clang_disposeString(tsp);
    r.kind = is_param ? "param" : "local";
    r.loc = cursor_loc(node.cursor);
    rows.push_back(std::move(r));
  }

  if (args.ast_json) {
    json_out::Array arr;
    for (const Row &r : rows) {
      json_out::Object obj;
      obj.push_back({"name", json_out::Value::of(r.name)});
      obj.push_back({"type", r.type.empty() ? json_out::Value::null()
                                            : json_out::Value::of(r.type)});
      obj.push_back({"kind", json_out::Value::of(r.kind)});
      obj.push_back({"loc", json_out::Value::of(r.loc)});
      arr.push_back(json_out::Value::obj(std::move(obj)));
    }
    *ctx.out << json_out::dumps_indent2(json_out::Value::arr(std::move(arr)))
             << "\n";
  } else {
    CXString fsp = clang_getCursorSpelling(focus);
    const char *fname = clang_getCString(fsp);
    *ctx.out << (fname ? fname : "?") << ": " << rows.size()
             << " variable(s)\n";
    clang_disposeString(fsp);
    for (const Row &r : rows) {
      const std::string tag = (r.kind == "param") ? "param" : "local";
      // Python: f"  {tag:<6} {r['type'] or '?':<24} {r['name']}  @ {r['loc']}"
      *ctx.out << "  " << format::ljust(tag, 6) << " "
               << format::ljust(r.type.empty() ? "?" : r.type, 24) << " "
               << r.name << "  @ " << r.loc << "\n";
    }
  }
  return 0;
}

int cmd_ast_conditions(const ParsedArgs &args, Context &ctx) {
  auto [t_opt, rc] = resolve_target(args, ctx);
  if (!t_opt) {
    return rc;
  }
  const AstTarget &t = *t_opt;

  auto tu_opt = astcache::load_or_parse(t, args.use_cache, ctx.err);
  if (!tu_opt) {
    return 1;
  }

  auto [focus_opt, frc] = focus_function(args, ctx, t, tu_opt->tu);
  if (!focus_opt) {
    return frc;
  }
  const CXCursor &focus = *focus_opt;

  // Build parent_of map and collect calls via subtree walk.
  std::unordered_map<unsigned, CXCursor> parent_of;
  std::vector<CXCursor> calls;
  for (const SubtreeNode &node : subtree(focus)) {
    parent_of[clang_hashCursor(node.cursor)] = node.parent;
    if (clang_getCursorKind(node.cursor) == CXCursor_CallExpr) {
      CXString sp = clang_getCursorSpelling(node.cursor);
      const char *s = clang_getCString(sp);
      if (s && *s) {
        calls.push_back(node.cursor);
      }
      clang_disposeString(sp);
    }
  }

  // For each call, climb parents to find a conditional guard.
  // Python _guarded_by: walks up parent_of until hitting focus or finding guard.
  auto guarded_by = [&](CXCursor call, CXCursor guard) -> bool {
    unsigned node_hash = clang_hashCursor(call);
    auto it = parent_of.find(node_hash);
    while (it != parent_of.end() &&
           clang_hashCursor(it->second) != clang_hashCursor(focus)) {
      if (clang_hashCursor(it->second) == clang_hashCursor(guard)) {
        return true;
      }
      it = parent_of.find(clang_hashCursor(it->second));
    }
    return false;
  };

  // Python _condition_child: first expression-kind child.
  auto condition_child = [](CXCursor stmt) -> CXCursor {
    CXCursor found = clang_getNullCursor();
    clang_visitChildren(
        stmt,
        [](CXCursor c, CXCursor /*p*/, CXClientData d) {
          auto *f = static_cast<CXCursor *>(d);
          if (clang_Cursor_isNull(*f) &&
              clang_isExpression(clang_getCursorKind(c))) {
            *f = c;
          }
          return CXChildVisit_Continue;
        },
        &found);
    return found;
  };

  struct CondRow {
    std::string control; // kind name of guard
    std::string loc;
    std::string condition; // token string of condition expression
    std::vector<std::string> call_names;
    std::optional<json_out::Value> condition_ast;
  };

  std::unordered_map<unsigned, bool> seen;
  std::vector<CondRow> rows;

  for (const CXCursor &call : calls) {
    unsigned node_hash = clang_hashCursor(call);
    auto it = parent_of.find(node_hash);
    CXCursor guard = clang_getNullCursor();
    while (it != parent_of.end() &&
           clang_hashCursor(it->second) != clang_hashCursor(focus)) {
      if (is_cond_kind(clang_getCursorKind(it->second))) {
        guard = it->second;
        break;
      }
      it = parent_of.find(clang_hashCursor(it->second));
    }
    if (clang_Cursor_isNull(guard)) {
      continue;
    }
    const unsigned guard_hash = clang_hashCursor(guard);
    if (seen.count(guard_hash)) {
      continue;
    }
    seen[guard_hash] = true;

    CXCursor cond_c = condition_child(guard);

    // Tokens of condition expression.
    std::string cond_toks;
    if (!clang_Cursor_isNull(cond_c)) {
      CXTranslationUnit tu = clang_Cursor_getTranslationUnit(cond_c);
      CXSourceRange extent = clang_getCursorExtent(cond_c);
      CXToken *toks = nullptr;
      unsigned ntok = 0;
      clang_tokenize(tu, extent, &toks, &ntok);
      for (unsigned ti = 0; ti < ntok; ++ti) {
        if (!cond_toks.empty()) {
          cond_toks += ' ';
        }
        CXString ts = clang_getTokenSpelling(tu, toks[ti]);
        const char *raw = clang_getCString(ts);
        if (raw) {
          cond_toks += raw;
        }
        clang_disposeString(ts);
      }
      if (toks) {
        clang_disposeTokens(tu, toks, ntok);
      }
    }

    // Guarded calls (sorted by spelling).
    std::vector<std::string> guarded;
    for (const CXCursor &c : calls) {
      if (guarded_by(c, guard)) {
        CXString sp = clang_getCursorSpelling(c);
        const char *s = clang_getCString(sp);
        if (s && *s) {
          guarded.push_back(std::string(s));
        }
        clang_disposeString(sp);
      }
    }
    std::sort(guarded.begin(), guarded.end());
    guarded.erase(std::unique(guarded.begin(), guarded.end()), guarded.end());

    CondRow row;
    row.control = kind_name(static_cast<unsigned>(clang_getCursorKind(guard)));
    row.loc = cursor_loc(guard);
    row.condition = cond_toks;
    row.call_names = std::move(guarded);
    if (args.cond_ast && !clang_Cursor_isNull(cond_c)) {
      row.condition_ast = cursor_json(cond_c, 0, std::nullopt, false, true);
    }
    rows.push_back(std::move(row));
  }

  if (args.ast_json) {
    json_out::Array arr;
    for (const CondRow &r : rows) {
      json_out::Object obj;
      obj.push_back({"control", json_out::Value::of(r.control)});
      obj.push_back({"loc", json_out::Value::of(r.loc)});
      obj.push_back({"condition", json_out::Value::of(r.condition)});
      json_out::Array calls_arr;
      for (const std::string &cn : r.call_names) {
        calls_arr.push_back(json_out::Value::of(cn));
      }
      obj.push_back({"calls", json_out::Value::arr(std::move(calls_arr))});
      if (r.condition_ast) {
        obj.push_back({"condition_ast", *r.condition_ast});
      }
      arr.push_back(json_out::Value::obj(std::move(obj)));
    }
    *ctx.out << json_out::dumps_indent2(json_out::Value::arr(std::move(arr)))
             << "\n";
  } else {
    CXString fsp = clang_getCursorSpelling(focus);
    const char *fname = clang_getCString(fsp);
    *ctx.out << (fname ? fname : "?") << ": " << rows.size()
             << " conditional(s) guarding calls\n";
    clang_disposeString(fsp);
    for (const CondRow &r : rows) {
      // Python:
      //   f"  {r['control']:<20} @ {r['loc']}"
      //   f"    cond: {r['condition']}"
      //   f"    -> calls: {', '.join(r['calls'])}"
      *ctx.out << "  " << format::ljust(r.control, 20) << " @ " << r.loc
               << "\n";
      *ctx.out << "    cond: " << r.condition << "\n";
      std::string calls_str;
      for (std::size_t ci = 0; ci < r.call_names.size(); ++ci) {
        if (ci > 0) {
          calls_str += ", ";
        }
        calls_str += r.call_names[ci];
      }
      *ctx.out << "    -> calls: " << calls_str << "\n";
    }
  }
  return 0;
}

// Cache subcommand handlers. Resolution reuses resolve_target() from
// ast_query.cpp; the cache primitives come from astcache.hpp.

int cmd_ast_cache(const ParsedArgs &args, Context &ctx) {
  const std::string &action = args.cache_action;
  if (action == "build") {
    return cmd_ast_cache_build(args, ctx);
  }
  if (action == "status") {
    return cmd_ast_cache_status(args, ctx);
  }
  if (action == "clear") {
    return cmd_ast_cache_clear(args, ctx);
  }
  *ctx.err << "error: unknown cache action '" << action << "'\n";
  return 2;
}

int cmd_ast_cache_build(const ParsedArgs &args, Context &ctx) {
  auto [t_opt, rc] = resolve_target(args, ctx);
  if (!t_opt) {
    return rc;
  }
  const AstTarget &t = *t_opt;

  namespace fs = std::filesystem;
  const std::string fd = astcache::files_dir();
  std::error_code ec;
  fs::create_directories(fd, ec);

  const std::string key = astcache::cache_key(t);
  const std::string ast_path = pathutil::join(fd, key + ".ast");
  const std::string side_path = pathutil::join(fd, key + ".json");

  auto tu_opt = astcache::reparse(t, ctx.err);
  if (!tu_opt) {
    return 1;
  }
  astcache::try_save(tu_opt->tu, ast_path, side_path, t);

  struct stat st{};
  if (::stat(ast_path.c_str(), &st) == 0) {
    const int64_t size = static_cast<int64_t>(st.st_size);
    *ctx.out << "cached: " << ast_path << "  ("
             << format::group_thousands(size) << " bytes)\n";
  } else {
    *ctx.err << "warning: AST save failed for " << t.abspath << "\n";
  }
  return 0;
}

int cmd_ast_cache_status(const ParsedArgs &args, Context &ctx) {
  namespace fs = std::filesystem;
  const std::string fd = astcache::files_dir();
  struct stat fdst{};
  const bool dir_exists =
      (::stat(fd.c_str(), &fdst) == 0 && S_ISDIR(fdst.st_mode));

  // B4: Python keys on args.target only (not --usr/--id/--name) for per-target
  // mode; selectors are not a cache address — they need an index lookup that is
  // out of scope for the cache sub-commands.  Also: dir-exists check must run
  // first so "no cache dir" prints before any target resolution attempt.
  const bool has_target = !args.target.empty();

  if (has_target) {
    auto [t_opt, rc] = resolve_target(args, ctx);
    if (!t_opt) {
      return rc;
    }
    const AstTarget &t = *t_opt;
    const std::string key = astcache::cache_key(t);
    const std::string ast_path = pathutil::join(fd, key + ".ast");
    const std::string side_path = pathutil::join(fd, key + ".json");

    struct stat ast_st{};
    const bool ast_exists = (::stat(ast_path.c_str(), &ast_st) == 0);
    const std::string short_key = key.substr(0, 12);

    if (!ast_exists) {
      if (args.ast_json) {
        // S1: route through json_out to properly escape abspath (backslashes,
        // quotes, etc.) — matches Python json.dumps default.
        json_out::Object absent;
        absent.push_back({"key", json_out::Value::of(short_key)});
        absent.push_back({"present", json_out::Value::of(false)});
        absent.push_back({"abspath", json_out::Value::of(t.abspath)});
        *ctx.out << json_out::dumps_indent2(json_out::Value::obj(std::move(absent)))
                 << "\n";
      } else {
        *ctx.out << short_key << "  ABSENT  " << t.abspath << "\n";
      }
      return 0;
    }

    auto side = astcache::read_sidecar(side_path);
    const bool valid = side && astcache::is_valid(t, *side);
    const int64_t size = static_cast<int64_t>(ast_st.st_size);
    const std::string abspath_str = side ? side->abspath : t.abspath;
    const std::string status_str = valid ? "valid" : "STALE";

    if (args.ast_json) {
      // S1: route through json_out to properly escape abspath.
      json_out::Object present;
      present.push_back({"key", json_out::Value::of(short_key)});
      present.push_back({"present", json_out::Value::of(true)});
      present.push_back({"valid", json_out::Value::of(valid)});
      present.push_back({"size", json_out::Value::of(size)});
      present.push_back({"abspath", json_out::Value::of(abspath_str)});
      *ctx.out << json_out::dumps_indent2(json_out::Value::obj(std::move(present)))
               << "\n";
    } else {
      *ctx.out << short_key << "  "
               << format::rjust(format::group_thousands(size), 10) << "  "
               << format::ljust(status_str, 8) << "  " << abspath_str << "\n";
    }
    return 0;
  }

  // Bulk: enumerate all *.json sidecars.
  if (!dir_exists) {
    if (args.ast_json) {
      *ctx.out
          << "{\"entries\": [], \"total_entries\": 0, \"total_bytes\": 0}\n";
    } else {
      *ctx.out << "cache dir does not exist: " << fd << "\n";
    }
    return 0;
  }

  struct Entry {
    std::string key;
    std::string status;
    int64_t size = 0;
    std::string abspath;
  };
  std::vector<Entry> entries;
  int64_t total_bytes = 0;

  std::vector<std::string> names;
  try {
    for (const auto &de : fs::directory_iterator(fd)) {
      if (de.path().extension() == ".json") {
        names.push_back(de.path().filename().string());
      }
    }
  } catch (...) {
  }
  std::sort(names.begin(), names.end());

  for (const std::string &name : names) {
    const std::string key = name.substr(0, name.size() - 5);
    const std::string ast_path = pathutil::join(fd, key + ".ast");
    const std::string side_path = pathutil::join(fd, name);

    auto side = astcache::read_sidecar(side_path);
    if (!side) {
      entries.push_back({key.substr(0, 12), "orphan-sidecar", 0, "?"});
      continue;
    }
    struct stat ast_st{};
    if (::stat(ast_path.c_str(), &ast_st) != 0) {
      entries.push_back(
          {key.substr(0, 12), "orphan-sidecar", 0, side->abspath});
      continue;
    }
    const int64_t size = static_cast<int64_t>(ast_st.st_size);
    total_bytes += size;
    const std::string &absp = side->abspath;

    struct stat src_st{};
    bool mtime_ok = false;
    if (::stat(absp.c_str(), &src_st) == 0) {
      mtime_ok = (side->src_mtime == astcache::src_mtime_of(src_st));
    }
    const bool version_ok =
        (side->libclang_version == astcache::libclang_version());
    std::string st_str;
    if (!mtime_ok) {
      st_str = "STALE";
    } else if (!version_ok) {
      st_str = "STALE(ver)";
    } else {
      st_str = "valid(flags?)";
    }
    entries.push_back({key.substr(0, 12), st_str, size, absp});
  }

  const int64_t total_entries = static_cast<int64_t>(entries.size());
  if (args.ast_json) {
    // S1: route bulk status through json_out to properly escape abspath strings.
    json_out::Array entries_arr;
    for (const auto &e : entries) {
      json_out::Object entry;
      entry.push_back({"key", json_out::Value::of(e.key)});
      entry.push_back({"status", json_out::Value::of(e.status)});
      entry.push_back({"size", json_out::Value::of(e.size)});
      entry.push_back({"abspath", json_out::Value::of(e.abspath)});
      entries_arr.push_back(json_out::Value::obj(std::move(entry)));
    }
    json_out::Object root;
    root.push_back({"entries", json_out::Value::arr(std::move(entries_arr))});
    root.push_back({"total_entries", json_out::Value::of(total_entries)});
    root.push_back({"total_bytes", json_out::Value::of(total_bytes)});
    *ctx.out << json_out::dumps_indent2(json_out::Value::obj(std::move(root)))
             << "\n";
  } else {
    if (entries.empty()) {
      *ctx.out << "cache is empty\n";
    } else {
      *ctx.out << format::ljust("key", 14) << "  "
               << format::rjust("size", 10) << "  "
               << format::ljust("status", 16) << "  abspath\n";
      *ctx.out << std::string(72, '-') << "\n";
      for (const auto &e : entries) {
        *ctx.out << format::ljust(e.key, 14) << "  "
                 << format::rjust(format::group_thousands(e.size), 10) << "  "
                 << format::ljust(e.status, 16) << "  " << e.abspath << "\n";
      }
    }
    const std::string ies = (total_entries == 1) ? "y" : "ies";
    *ctx.out << "\n"
             << total_entries << " entr" << ies << ", "
             << format::group_thousands(total_bytes) << " bytes total\n";
    if (!entries.empty()) {
      *ctx.out << "note: bulk status cannot re-verify compile flags "
                  "(one-way hash); pass a target for full validation\n";
    }
  }
  return 0;
}

int cmd_ast_cache_clear(const ParsedArgs &args, Context &ctx) {
  namespace fs = std::filesystem;
  const std::string fd = astcache::files_dir();
  // B4: same as cmd_ast_cache_status — only args.target drives per-target mode.
  const bool has_target = !args.target.empty();

  if (has_target) {
    auto [t_opt, rc] = resolve_target(args, ctx);
    if (!t_opt) {
      return rc;
    }
    const AstTarget &t = *t_opt;
    const std::string key = astcache::cache_key(t);
    const std::string ast_path = pathutil::join(fd, key + ".ast");
    const std::string side_path = pathutil::join(fd, key + ".json");
    int removed = 0;
    int64_t freed = 0;
    for (const std::string &p : {ast_path, side_path}) {
      struct stat st{};
      if (::stat(p.c_str(), &st) == 0) {
        freed += static_cast<int64_t>(st.st_size);
        if (std::remove(p.c_str()) == 0) {
          ++removed;
        }
      }
    }
    if (removed > 0) {
      *ctx.out << "removed " << removed << " file(s), "
               << format::group_thousands(freed) << " bytes freed\n";
    } else {
      *ctx.out << "no cache entry for " << t.abspath << "\n";
    }
    return 0;
  }

  struct stat fdst{};
  if (::stat(fd.c_str(), &fdst) != 0 || !S_ISDIR(fdst.st_mode)) {
    *ctx.out << "cache dir does not exist; nothing to clear\n";
    return 0;
  }
  int removed = 0;
  int64_t freed = 0;
  try {
    for (const auto &de : fs::directory_iterator(fd)) {
      const auto ext = de.path().extension().string();
      if (ext == ".ast" || ext == ".json") {
        struct stat st{};
        if (::stat(de.path().c_str(), &st) == 0) {
          freed += static_cast<int64_t>(st.st_size);
          if (std::remove(de.path().c_str()) == 0) {
            ++removed;
          }
        }
      }
    }
  } catch (const std::exception &e) {
    *ctx.err << "error listing cache dir: " << e.what() << "\n";
    return 1;
  }
  *ctx.out << "cleared " << removed << " file(s), "
           << format::group_thousands(freed) << " bytes freed\n";
  return 0;
}

// ============================================================================
// M6 graph command group helpers
// ============================================================================

namespace {

// Parse a comma-separated edge-kind spec into a vector.
// Returns nullopt for null/empty string (= all kinds).
// Mirrors cli.py:_edge_kinds (cli.py:999-1004).
std::optional<std::vector<std::string>>
graph_edge_kinds(const std::optional<std::string> &spec) {
  if (!spec || spec->empty()) {
    return std::nullopt;
  }
  std::vector<std::string> out;
  std::string cur;
  for (char c : *spec) {
    if (c == ',') {
      if (!cur.empty()) {
        // strip leading/trailing spaces
        std::size_t b = 0, e = cur.size();
        while (b < e && cur[b] == ' ') ++b;
        while (e > b && cur[e-1] == ' ') --e;
        if (b < e) out.push_back(cur.substr(b, e-b));
      }
      cur.clear();
    } else {
      cur += c;
    }
  }
  if (!cur.empty()) {
    std::size_t b = 0, e = cur.size();
    while (b < e && cur[b] == ' ') ++b;
    while (e > b && cur[e-1] == ' ') --e;
    if (b < e) out.push_back(cur.substr(b, e-b));
  }
  if (out.empty()) {
    return std::nullopt;
  }
  return out;
}

// Resolve --usr/--id/--name to a single Sym.
// Returns (nullopt, rc) on failure.
// Mirrors cli.py:_select_one (cli.py:1007-1046).
std::pair<std::optional<graph::Sym>, int>
graph_select_one(graph::GraphQuery &g,
                 const std::optional<std::string> &usr_opt,
                 const std::optional<int64_t> &id_opt,
                 const std::optional<std::string> &name_opt,
                 const std::optional<std::string> &kind_opt,
                 bool first,
                 std::ostream &err_out) {
  if (usr_opt) {
    auto s = g.get_by_usr(*usr_opt);
    if (!s) {
      err_out << "error: no symbol with USR "
              << format::py_repr(*usr_opt) << "\n";
      return {std::nullopt, 1};
    }
    return {s, 0};
  }
  if (id_opt) {
    auto s = g.get_by_id(*id_opt);
    if (!s) {
      err_out << "error: no symbol with id " << *id_opt << "\n";
      return {std::nullopt, 1};
    }
    return {s, 0};
  }
  if (!name_opt) {
    err_out << "error: one of the arguments --usr --id --name is required\n";
    return {std::nullopt, 2};
  }
  const std::string &name = *name_opt;
  auto hits = g.find(name, kind_opt, 50);
  if (hits.empty()) {
    err_out << "error: no symbol matches --name " << format::py_repr(name);
    if (kind_opt) {
      err_out << " (kind " << *kind_opt << ")";
    }
    err_out << "\n";
    return {std::nullopt, 1};
  }
  if (hits.size() > 1 && !first) {
    err_out << "error: --name " << format::py_repr(name) << " matches "
            << hits.size()
            << " symbols; disambiguate with --usr/--id (or pass --first):\n";
    const std::size_t show = std::min(hits.size(), std::size_t{25});
    for (std::size_t j = 0; j < show; ++j) {
      const auto &s = hits[j];
      err_out << "  #" << s.id << "  "
              << format::ljust(s.kind, 14) << " " << s.name
              << "  @" << s.loc() << "  [" << s.usr << "]\n";
    }
    if (hits.size() > 25) {
      err_out << "  ... and " << (hits.size() - 25) << " more\n";
    }
    return {std::nullopt, 2};
  }
  return {hits[0], 0};
}

// Open graph + enforce edges. Returns (nullptr, 1) on failure.
// `storage_out` receives the opened Storage (must outlive GraphQuery).
struct GraphHandle {
  std::unique_ptr<Storage> storage;
  std::unique_ptr<graph::GraphQuery> g;
};

std::optional<GraphHandle>
open_graph(const ParsedArgs & /*args*/, Context &ctx) {
  GraphHandle h;

  // Check file exists BEFORE opening Storage (Storage constructor uses
  // SQLITE_OPEN_CREATE which would create the file on disk, making a
  // subsequent stat() always succeed even for a missing index).
  {
    struct stat st{};
    if (::stat(ctx.index_path.c_str(), &st) != 0) {
      const std::string repr = format::py_repr(ctx.index_path);
      *ctx.err << "error: no cidx index at " << repr
               << ". Build one with:\n"
               << "    cd <repo> && cidx add-source --path . && cidx import "
                  "--db <build> && cidx index && cidx resolve\n"
               << "or pass --db PATH / set $INDEXER_CACHE.\n";
      return std::nullopt;
    }
  }

  h.storage = std::make_unique<Storage>(ctx.index_path);
  h.g = std::make_unique<graph::GraphQuery>(*h.storage, ctx.index_path);
  if (h.g->edge_count() == 0) {
    const std::string repr = format::py_repr(ctx.index_path);
    *ctx.err << "error: index " << repr
             << " has no graph edges -- it was built with "
                "`cidx index --no-graph`, or the graph was cleared. Re-run "
                "`cidx index` (without --no-graph) then `cidx resolve`.\n";
    return std::nullopt;
  }
  return h;
}

} // namespace

// ============================================================================
// M6 graph sub-command handlers
// ============================================================================

int cmd_graph_callers(const ParsedArgs &args, Context &ctx) {
  auto h = open_graph(args, ctx);
  if (!h) return 1;
  auto [sym, rc] = graph_select_one(*h->g, args.usr, args.graph_id, args.name,
                                    args.kind, args.first, *ctx.err);
  if (!sym) return rc;
  std::vector<std::string> kinds{"calls"};
  if (!args.direct_only) {
    kinds.emplace_back("dispatch_calls");
  }
  auto edges = h->g->edges_in(sym->id, kinds, args.graph_limit);
  graph::emit_edges(*h->g, edges, args.graph_json, *ctx.out,
                    "callers of " + sym->name + " (@" + sym->loc() + "):");
  return 0;
}

int cmd_graph_callees(const ParsedArgs &args, Context &ctx) {
  auto h = open_graph(args, ctx);
  if (!h) return 1;
  auto [sym, rc] = graph_select_one(*h->g, args.usr, args.graph_id, args.name,
                                    args.kind, args.first, *ctx.err);
  if (!sym) return rc;
  auto edges = h->g->edges_out(sym->id, std::vector<std::string>{"calls"},
                               args.graph_limit);
  graph::emit_edges(*h->g, edges, args.graph_json, *ctx.out,
                    "callees of " + sym->name + " (@" + sym->loc() + "):");
  return 0;
}

int cmd_graph_refs(const ParsedArgs &args, Context &ctx) {
  auto h = open_graph(args, ctx);
  if (!h) return 1;
  auto [sym, rc] = graph_select_one(*h->g, args.usr, args.graph_id, args.name,
                                    args.kind, args.first, *ctx.err);
  if (!sym) return rc;
  auto edges = h->g->references(sym->id, args.graph_limit);
  graph::emit_edges(*h->g, edges, args.graph_json, *ctx.out,
                    "references to " + sym->name + " (@" + sym->loc() + "):");
  return 0;
}

int cmd_graph_neighbors(const ParsedArgs &args, Context &ctx) {
  auto h = open_graph(args, ctx);
  if (!h) return 1;
  auto [sym, rc] = graph_select_one(*h->g, args.usr, args.graph_id, args.name,
                                    args.kind, args.first, *ctx.err);
  if (!sym) return rc;
  auto kinds_vec = graph_edge_kinds(args.edge);
  std::optional<std::vector<int64_t>> kid_ids;
  try {
    kid_ids = h->g->kind_ids(kinds_vec);
  } catch (const std::invalid_argument &e) {
    *ctx.err << "error: " << e.what() << "\n";
    return 1;
  }
  auto edges = h->g->edges(sym->id, args.direction, kid_ids, args.graph_limit);
  const std::string kinds_str = args.edge.value_or("all");
  graph::emit_edges(*h->g, edges, args.graph_json, *ctx.out,
                    args.direction + "-neighbors of " + sym->name +
                        " (@" + sym->loc() + ") over " + kinds_str + ":");
  return 0;
}

int cmd_graph_walk(const ParsedArgs &args, Context &ctx) {
  auto h = open_graph(args, ctx);
  if (!h) return 1;
  auto [sym, rc] = graph_select_one(*h->g, args.usr, args.graph_id, args.name,
                                    args.kind, args.first, *ctx.err);
  if (!sym) return rc;
  auto kinds_vec = graph_edge_kinds(args.edge);
  // walk default edge kind is "calls"
  if (!kinds_vec) {
    kinds_vec = std::vector<std::string>{"calls"};
  }
  graph::Traversal tr;
  try {
    tr = h->g->walk(sym->id, *kinds_vec, args.direction, args.graph_depth,
                    args.graph_limit);
  } catch (const std::invalid_argument &e) {
    *ctx.err << "error: " << e.what() << "\n";
    return 1;
  }
  // Exclude the start node from output
  std::vector<graph::Sym> nodes;
  for (const auto &n : tr.nodes()) {
    if (n.id != sym->id) {
      nodes.push_back(n);
    }
  }
  // Build kinds comma-separated string for header
  std::string kinds_str;
  for (std::size_t ki = 0; ki < kinds_vec->size(); ++ki) {
    if (ki != 0) kinds_str += ",";
    kinds_str += (*kinds_vec)[ki];
  }
  graph::emit_syms(
      nodes, args.graph_json, *ctx.out,
      "reachable from " + sym->name + " (@" + sym->loc() + ") over " +
          kinds_str + " " + args.direction + ", depth<=" +
          std::to_string(args.graph_depth) + ":",
      &tr.depth_by_id);
  return 0;
}

int cmd_graph_path(const ParsedArgs &args, Context &ctx) {
  auto h = open_graph(args, ctx);
  if (!h) return 1;
  auto [src, rc_src] = graph_select_one(*h->g, args.usr, args.graph_id,
                                        args.name, args.kind, args.first,
                                        *ctx.err);
  if (!src) return rc_src;
  auto [dst, rc_dst] = graph_select_one(*h->g, args.to_usr, args.to_id,
                                        args.to_name, args.to_kind, args.first,
                                        *ctx.err);
  if (!dst) return rc_dst;

  auto kinds_vec = graph_edge_kinds(args.edge);
  // path default edge kind is "calls"
  if (!kinds_vec) {
    kinds_vec = std::vector<std::string>{"calls"};
  }
  std::optional<std::vector<graph::Sym>> chain;
  try {
    chain = h->g->reaches(src->id, dst->id, *kinds_vec, args.direction,
                          args.graph_depth);
  } catch (const std::invalid_argument &e) {
    *ctx.err << "error: " << e.what() << "\n";
    return 1;
  }
  if (!chain) {
    if (args.graph_json) {
      *ctx.out << "null\n";
    } else {
      std::string ks;
      for (std::size_t ki = 0; ki < kinds_vec->size(); ++ki) {
        if (ki != 0) ks += ",";
        ks += (*kinds_vec)[ki];
      }
      *ctx.out << "no path from " << src->name << " to " << dst->name
               << " over " << ks << " " << args.direction << " within depth "
               << args.graph_depth << "\n";
    }
    return 1;
  }
  graph::emit_syms(
      *chain, args.graph_json, *ctx.out,
      "path " + src->name + " -> " + dst->name + " (" +
          std::to_string(chain->size() - 1) + " hop(s)):");
  return 0;
}

int cmd_graph_hierarchy(const ParsedArgs &args, Context &ctx) {
  auto h = open_graph(args, ctx);
  if (!h) return 1;
  auto [sym, rc] = graph_select_one(*h->g, args.usr, args.graph_id, args.name,
                                    args.kind, args.first, *ctx.err);
  if (!sym) return rc;
  const bool direct = !args.transitive;
  auto bases = h->g->bases(sym->id, direct);
  auto subs = h->g->subclasses(sym->id, direct);
  std::optional<std::string> access_filter;
  if (args.access != "all") {
    access_filter = args.access;
  }
  std::vector<graph::Sym> mems;
  try {
    mems = h->g->members(sym->id, access_filter);
  } catch (const std::invalid_argument &e) {
    *ctx.err << "error: " << e.what() << "\n";
    return 1;
  }
  if (args.graph_json) {
    using namespace json_out;
    Array barr, sarr, marr;
    for (const auto &s : bases) barr.push_back(s.to_dict());
    for (const auto &s : subs) sarr.push_back(s.to_dict());
    for (const auto &s : mems) marr.push_back(s.to_dict());
    Object o;
    o.push_back({"symbol", sym->to_dict()});
    o.push_back({"bases", Value::arr(std::move(barr))});
    o.push_back({"subclasses", Value::arr(std::move(sarr))});
    o.push_back({"members", Value::arr(std::move(marr))});
    *ctx.out << dumps_indent2(Value::obj(std::move(o))) << "\n";
    return 0;
  }
  const std::string scope = args.transitive ? "all" : "direct";
  *ctx.out << "hierarchy of " << sym->name << " (@" << sym->loc() << "):\n";
  graph::emit_syms(bases, false, *ctx.out, "  bases (" + scope + "):");
  graph::emit_syms(subs, false, *ctx.out, "  subclasses (" + scope + "):");
  graph::emit_syms(mems, false, *ctx.out, "  members:");
  return 0;
}

int cmd_graph_dispatch(const ParsedArgs &args, Context &ctx) {
  auto h = open_graph(args, ctx);
  if (!h) return 1;
  auto [sym, rc] = graph_select_one(*h->g, args.usr, args.graph_id, args.name,
                                    args.kind, args.first, *ctx.err);
  if (!sym) return rc;
  auto targets = h->g->dispatch_targets(sym->id);
  const bool virt = h->g->is_virtual_method(sym->id);
  if (args.graph_json) {
    using namespace json_out;
    Array tarr;
    for (const auto &t : targets) tarr.push_back(t.to_dict());
    Object o;
    o.push_back({"method", sym->to_dict()});
    o.push_back({"is_virtual", Value::of(virt)});
    o.push_back({"targets", Value::arr(std::move(tarr))});
    *ctx.out << dumps_indent2(Value::obj(std::move(o))) << "\n";
    return 0;
  }
  const std::string note = virt ? "" : "  (not a virtual method -- only itself)";
  graph::emit_syms(targets, false, *ctx.out,
                   "run-time dispatch targets of " + sym->name +
                       " (@" + sym->loc() + ")" + note + ":");
  return 0;
}

// ---------------------------------------------------------------------------
// Portable-paths commands (v14): component show/set-version,
//                               label add/rm/list/resolve
// ---------------------------------------------------------------------------

int cmd_component_show(const ParsedArgs &args, Context &ctx) {
  const std::string name = args.name ? *args.name : std::string();
  Storage db(ctx.index_path);
  std::optional<Component> comp = db.get_component_by_name(name);
  if (!comp) {
    *ctx.err << "error: no component named '" << name << "'\n";
    return 1;
  }
  // Output: key-value table, 14-char left-justified key col.
  // Byte-identical with Python: f"{key:<14} {value}"
  // Keys: name, kind, "base path", version, "effective root", "resolved root"
  const std::string eff = Storage::effective_root(*comp);
  const std::string resolved = db.component_abs_base(*comp);
  auto row = [&](const std::string &key, const std::string &val) {
    *ctx.out << fmt::ljust(key, 14) << " " << val << "\n";
  };
  row("name", comp->name);
  row("kind", comp->kind);
  row("base path", comp->path);
  row("version", comp->version ? *comp->version : "(none)");
  row("effective root", eff);
  row("resolved root", resolved);
  return 0;
}

int cmd_component_set_version(const ParsedArgs &args, Context &ctx) {
  const std::string name = args.name ? *args.name : std::string();
  Storage db(ctx.index_path);
  // args.version_str absent or empty means clear the version.
  const std::optional<std::string> ver =
      (args.version_str && !args.version_str->empty())
          ? args.version_str
          : std::optional<std::string>{};
  const bool ok = db.set_component_version(name, ver);
  if (!ok) {
    *ctx.err << "error: no component named '" << name << "'\n";
    return 1;
  }
  if (ver) {
    // Python: f"component '{name}' version set to {version}" — unquoted version.
    *ctx.out << "component '" << name << "' version set to " << *ver << "\n";
  } else {
    *ctx.out << "component '" << name << "' version cleared\n";
  }
  return 0;
}

// -- repo list / show / add-clone / switch / rm (v23) ----------------------

namespace {
// Resolved path of a repository's active clone, or nullopt.
std::optional<std::string> active_clone_path(Storage &db,
                                             const Repository &repo) {
  if (!repo.active_clone_id) {
    return std::nullopt;
  }
  const std::optional<Clone> cl = db.get_clone_by_id(*repo.active_clone_id);
  if (!cl) {
    return std::nullopt;
  }
  return cl->path;
}
} // namespace

int cmd_repo_list(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  const std::vector<Repository> repos = db.list_repositories(args.pattern,
                                                             args.kind);
  std::size_t width = 0;
  for (const Repository &r : repos) {
    width = std::max(width, r.name.size());
  }
  for (const Repository &r : repos) {
    const std::size_t ncomp = db.components_for_repository(r.id).size();
    const std::size_t nclone = db.list_clones(r.id).size();
    const std::optional<std::string> ap = active_clone_path(db, r);
    const std::string active = ap ? *ap : "-";
    // f"{r.id:>4}  {r.name:<{width}}  {r.kind:<8}  "
    // f"{ncomp} component(s)  {nclone} clone(s)  {active}"
    *ctx.out << fmt::rjust(std::to_string(r.id), 4) << "  "
             << fmt::ljust(r.name, width) << "  " << fmt::ljust(r.kind, 8)
             << "  " << ncomp << " component(s)  " << nclone << " clone(s)  "
             << active << "\n";
  }
  *ctx.out << repos.size() << " repositories\n";
  return repos.empty() ? 1 : 0;
}

int cmd_repo_show(const ParsedArgs &args, Context &ctx) {
  const std::string name = args.name ? *args.name : std::string();
  Storage db(ctx.index_path);
  const std::optional<Repository> repo = db.get_repository_by_name(name);
  if (!repo) {
    *ctx.err << "error: no repository named '" << name << "'\n";
    return 1;
  }
  const std::vector<Clone> clones = db.list_clones(repo->id);
  const std::vector<Component> comps = db.components_for_repository(repo->id);
  const std::string remote = repo->remote_url ? *repo->remote_url : "(none)";
  const std::optional<std::string> ap = active_clone_path(db, *repo);
  const std::string active = ap ? *ap : "(none)";
  auto row = [&](const std::string &key, const std::string &val) {
    *ctx.out << fmt::ljust(key, 14) << " " << val << "\n";
  };
  row("name", repo->name);
  row("kind", repo->kind);
  row("remote", remote);
  row("active clone", active);
  row("clones", std::to_string(clones.size()));
  row("components", std::to_string(comps.size()));
  if (!clones.empty()) {
    std::size_t lw = 1;
    for (const Clone &c : clones) {
      if (c.label) {
        lw = std::max(lw, c.label->size());
      }
    }
    *ctx.out << "clones:\n";
    for (const Clone &c : clones) {
      const std::string mark =
          (repo->active_clone_id && c.id == *repo->active_clone_id) ? "*"
                                                                    : " ";
      const std::string lbl = c.label ? *c.label : "-";
      // f"  {mark} {c.id:>4}  {lbl:<{lw}}  {c.path}"
      *ctx.out << "  " << mark << " " << fmt::rjust(std::to_string(c.id), 4)
               << "  " << fmt::ljust(lbl, lw) << "  " << c.path << "\n";
    }
  }
  if (!comps.empty()) {
    std::size_t cw = 0;
    for (const Component &c : comps) {
      cw = std::max(cw, c.name.size());
    }
    *ctx.out << "components:\n";
    for (const Component &c : comps) {
      // f"    {c.id:>4}  {c.name:<{cw}}  {base}" (resolved, clone-anchored)
      *ctx.out << "    " << fmt::rjust(std::to_string(c.id), 4) << "  "
               << fmt::ljust(c.name, cw) << "  " << db.component_abs_base(c)
               << "\n";
    }
  }
  return 0;
}

int cmd_repo_add_clone(const ParsedArgs &args, Context &ctx) {
  const std::string name = args.name ? *args.name : std::string();
  Storage db(ctx.index_path);
  const std::optional<Repository> repo = db.get_repository_by_name(name);
  if (!repo) {
    *ctx.err << "error: no repository named '" << name << "'\n";
    return 1;
  }
  if (!is_directory(pathutil::abspath(args.path))) {
    *ctx.err << "warning: " << args.path
             << " is not a directory (registered anyway)\n";
  }
  const int64_t clone_id = db.add_clone(repo->id, args.path, args.repo_label);
  if (!repo->active_clone_id) {
    db.set_active_clone(repo->id, clone_id);
  }
  const std::optional<Clone> cl = db.get_clone_by_id(clone_id);
  *ctx.out << "clone #" << clone_id << ": " << (cl ? cl->path : std::string())
           << " added to '" << name << "'\n";
  return 0;
}

int cmd_repo_switch(const ParsedArgs &args, Context &ctx) {
  const std::string name = args.name ? *args.name : std::string();
  Storage db(ctx.index_path);
  const std::optional<Repository> repo = db.get_repository_by_name(name);
  if (!repo) {
    *ctx.err << "error: no repository named '" << name << "'\n";
    return 1;
  }
  const std::vector<Clone> clones = db.list_clones(repo->id);
  const std::string want = pathutil::abspath(args.target);
  std::optional<Clone> target;
  for (const Clone &c : clones) {
    if (c.path == want || (c.label && *c.label == args.target)) {
      target = c;
      break;
    }
  }
  if (!target) {
    *ctx.err << "error: '" << args.target << "' is not a clone of '" << name
             << "' (add it with 'cidx repo add-clone')\n";
    return 1;
  }
  if (!is_directory(target->path)) {
    *ctx.err << "warning: clone path " << target->path
             << " is not present on disk\n";
  }
  // v24: grouped component paths are stored RELATIVE to the active clone, so a
  // switch is a single `active_clone_id` update -- no component row rewrite.
  const std::size_t ncomp = db.components_for_repository(repo->id).size();
  db.set_active_clone(repo->id, target->id);
  *ctx.out << "switched '" << name << "' to " << target->path << " (" << ncomp
           << " component(s))\n";
  return 0;
}

int cmd_repo_rm(const ParsedArgs &args, Context &ctx) {
  const std::string name = args.name ? *args.name : std::string();
  Storage db(ctx.index_path);
  const std::optional<Repository> repo = db.get_repository_by_name(name);
  if (!repo) {
    *ctx.err << "error: no repository named '" << name << "'\n";
    return 1;
  }
  const std::vector<Component> comps = db.components_for_repository(repo->id);
  if (args.delete_components) {
    for (const Component &c : comps) {
      db.delete_component(c.id);
    }
  }
  db.delete_repository(repo->id);
  if (args.delete_components) {
    *ctx.out << "removed repository '" << name << "' and " << comps.size()
             << " component(s)\n";
  } else {
    *ctx.out << "removed repository '" << name << "'; " << comps.size()
             << " component(s) detached\n";
  }
  return 0;
}

int cmd_verify(const ParsedArgs &args, Context &ctx) {
  // Byte-identical with Python cmd_verify (cli.py):
  //   component  {status:<8}  {name}  {resolved}
  //   file  MISSING   {path}    (only failures unless --all; OK = "ok" pad 8)
  // followed by the two summary lines. Exit 1 if anything is missing.
  Storage db(ctx.index_path);

  std::vector<Component> components;
  std::optional<int64_t> scope;
  if (args.component) {
    std::optional<Component> comp = db.get_component_by_name(*args.component);
    if (!comp) {
      // Parity: Python LookupError str is "no component named 'NAME'".
      *ctx.err << "error: no component named '" << *args.component << "'\n";
      return 1;
    }
    components.push_back(*comp);
    scope = comp->id;
  } else {
    components = db.list_components();
  }

  auto is_reg_file = [](const std::string &p) {
    struct stat st{};
    return ::stat(p.c_str(), &st) == 0 && S_ISREG(st.st_mode);
  };

  int c_ok = 0, c_missing = 0, c_vermiss = 0;
  for (const Component &c : components) {
    const std::string resolved = db.component_abs_base(c);
    std::string status;
    if (is_directory(resolved)) {
      status = "ok";
      ++c_ok;
    } else if (c.version && is_directory([&] {
                 Component base = c;
                 base.version = std::nullopt;
                 return db.component_abs_base(base);
               }())) {
      status = "VER-MISS";
      ++c_vermiss;
    } else {
      status = "MISSING";
      ++c_missing;
    }
    *ctx.out << "component  " << fmt::ljust(status, 8) << "  " << c.name << "  "
             << resolved << "\n";
  }

  int f_ok = 0, f_missing = 0;
  for (const auto &[rec, path] : db.list_files(scope)) {
    if (is_reg_file(path)) {
      ++f_ok;
      if (args.all) {
        *ctx.out << "file  ok        " << path << "\n";
      }
    } else {
      ++f_missing;
      *ctx.out << "file  MISSING   " << path << "\n";
    }
  }

  *ctx.out << "\n";
  *ctx.out << "components: " << c_ok << " ok, " << c_missing << " missing, "
           << c_vermiss << " version-mismatch\n";
  *ctx.out << "files: " << f_ok << " ok, " << f_missing << " missing\n";
  return (c_missing == 0 && c_vermiss == 0 && f_missing == 0) ? 0 : 1;
}

int cmd_label_add(const ParsedArgs &args, Context &ctx) {
  const std::string lname = args.label_token ? *args.label_token : std::string();
  const std::string lpath = args.label_path ? *args.label_path : std::string();
  if (lname.empty() || lpath.empty()) {
    *ctx.err << "error: NAME and PATH are required\n";
    return 1;
  }
  Storage db(ctx.index_path);
  // Check existence before upsert so we can print "added" vs "updated".
  // Mirrors Python: existing = db.get_label(args.name)
  const std::optional<std::string> existing = db.get_label(lname);
  db.add_label(lname, lpath);
  if (!existing) {
    *ctx.out << "added label " << lname << " -> " << lpath << "\n";
  } else {
    *ctx.out << "updated label " << lname << " -> " << lpath << "\n";
  }
  return 0;
}

int cmd_label_rm(const ParsedArgs &args, Context &ctx) {
  const std::string lname = args.label_token ? *args.label_token : std::string();
  Storage db(ctx.index_path);
  const bool removed = db.remove_label(lname);
  if (!removed) {
    *ctx.err << "error: no label named '" << lname << "'\n";
    return 1;
  }
  // Python: f"removed label {args.name}" (no quotes around name)
  *ctx.out << "removed label " << lname << "\n";
  return 0;
}

int cmd_label_list(const ParsedArgs &args, Context &ctx) {
  (void)args;
  Storage db(ctx.index_path);
  const auto labels = db.list_labels();
  if (labels.empty()) {
    *ctx.out << "0 label(s)\n";
    return 0;
  }
  // Dynamic column width: max(len(name)) + 2-space separator.
  // Python: width = max(len(name) for name, _ in labels)
  //         f"{name:<{width}}  {path}"
  std::size_t width = 0;
  for (const auto &[lname, lpath] : labels) {
    (void)lpath;
    if (lname.size() > width) width = lname.size();
  }
  for (const auto &[lname, lpath] : labels) {
    *ctx.out << fmt::ljust(lname, static_cast<int>(width)) << "  " << lpath
             << "\n";
  }
  *ctx.out << labels.size() << " label(s)\n";
  return 0;
}

int cmd_label_resolve(const ParsedArgs &args, Context &ctx) {
  std::string token = args.label_path ? *args.label_path : std::string();
  // Parity with Python: if no '<' or '$' in the token, treat it as a bare
  // label name and wrap it: f"<{token}>".
  if (token.find('<') == std::string::npos &&
      token.find('$') == std::string::npos) {
    token = "<" + token + ">";
  }
  Storage db(ctx.index_path);
  // Build a LabelResolver backed by the DB. get_alias resolves explicit labels
  // and uniquely-named components, matching parse-time decode (v0.8.0).
  const bool autoderive = !args.no_autoderive_labels;
  pathutil::LabelResolver resolver(
      [&db](const std::string &n) { return db.get_alias(n); }, autoderive);
  const std::string raw = pathutil::resolve_fs_path(token, resolver);
  // Apply abspath only for bare paths (not compound tokens like -I<...>).
  // resolve_fs_path contract: abspath is the caller's responsibility.
  const std::string resolved =
      (!raw.empty() && raw[0] == '-') ? raw : pathutil::abspath(raw);
  *ctx.out << resolved << "\n";
  return 0;
}

// cmd_realias (cli.py cmd_realias): rewrite stored include paths to <label>
// tokens via the registry. Optional COMPONENT restricts to one component.
// Port of Python cmd_realias, byte-identical output strings.
int cmd_realias(const ParsedArgs &args, Context &ctx) {
  Storage db(ctx.index_path);
  const auto pairs = db.list_alias_pairs();
  const auto label_map = CompileDb::build_label_map(
      pairs, [&db](const std::string &n) { return db.get_alias(n); });
  if (label_map.empty()) {
    *ctx.err << "error: no aliases available (register a label with "
                "'cidx label add', or add a component)\n";
    return 1;
  }
  std::optional<int64_t> cid;
  if (args.component && !args.component->empty()) {
    const std::optional<Component> comp =
        db.get_component_by_name(*args.component);
    if (!comp) {
      *ctx.err << "error: no component named '" << *args.component << "'\n";
      return 1;
    }
    cid = comp->id;
  }
  int64_t changed = 0;
  int64_t scanned = 0;
  for (const auto &row : db.list_files(cid)) {
    const File &rec = row.first;
    if (!rec.compile_options || rec.compile_options->empty() || rec.id == 0) {
      continue;
    }
    ++scanned;
    const std::vector<std::string> cur(*rec.compile_options);
    const std::vector<std::string> nw = CompileDb::alias_options(cur, label_map);
    if (nw != cur) {
      db.update_file_compile_options(rec.id, nw);
      ++changed;
    }
  }
  *ctx.out << "realias: " << changed << " file(s) updated, " << scanned
           << " scanned\n";
  return 0;
}

int run_command(const ParsedArgs &args, Context &ctx) {
  if (args.command == "init") {
    return cmd_init(args, ctx);
  }
  if (args.command == "migrate") {
    return cmd_migrate(args, ctx);
  }
  if (args.command == "add-source") {
    return cmd_add_source(args, ctx);
  }
  if (args.command == "import") {
    return cmd_import(args, ctx);
  }
  if (args.command == "realias") {
    return cmd_realias(args, ctx);
  }
  if (args.command == "index") {
    return cmd_index(args, ctx);
  }
  if (args.command == "resolve") {
    return cmd_resolve(args, ctx);
  }
  if (args.command == "pch") {
    if (args.what == "build") {
      return cmd_pch_build(args, ctx);
    }
    if (args.what == "status") {
      return cmd_pch_status(args, ctx);
    }
    return cmd_pch_clear(args, ctx);
  }
  if (args.command == "set") {
    return cmd_set(args, ctx);
  }
  if (args.command == "file") {
    return cmd_file(args, ctx);
  }
  if (args.command == "dump-compile-commands") {
    return cmd_dump_compile_commands(args, ctx);
  }
  if (args.command == "search") {
    return cmd_search(args, ctx);
  }
  if (args.command == "show") {
    return args.what == "symbol" ? cmd_show_symbol(args, ctx)
                                 : cmd_show_file(args, ctx);
  }
  if (args.command == "delete") {
    if (args.what == "component") {
      return cmd_delete_component(args, ctx);
    }
    if (args.what == "dir") {
      return cmd_delete_dir(args, ctx);
    }
    if (args.what == "file") {
      return cmd_delete_file(args, ctx);
    }
    return cmd_delete_symbol(args, ctx);
  }
  if (args.command == "ast") {
    if (args.what == "dump") {
      return cmd_ast_dump(args, ctx);
    }
    if (args.what == "locals") {
      return cmd_ast_locals(args, ctx);
    }
    if (args.what == "conditions") {
      return cmd_ast_conditions(args, ctx);
    }
    return cmd_ast_cache(args, ctx);
  }
  if (args.command == "graph") {
    if (args.what == "callers")   return cmd_graph_callers(args, ctx);
    if (args.what == "callees")   return cmd_graph_callees(args, ctx);
    if (args.what == "refs")      return cmd_graph_refs(args, ctx);
    if (args.what == "neighbors") return cmd_graph_neighbors(args, ctx);
    if (args.what == "walk")      return cmd_graph_walk(args, ctx);
    if (args.what == "path")      return cmd_graph_path(args, ctx);
    if (args.what == "hierarchy") return cmd_graph_hierarchy(args, ctx);
    if (args.what == "dispatch")  return cmd_graph_dispatch(args, ctx);
  }
  if (args.command == "component") {
    if (args.what == "show") return cmd_component_show(args, ctx);
    return cmd_component_set_version(args, ctx);
  }
  if (args.command == "repo") {
    if (args.what == "show") return cmd_repo_show(args, ctx);
    if (args.what == "add-clone") return cmd_repo_add_clone(args, ctx);
    if (args.what == "switch") return cmd_repo_switch(args, ctx);
    if (args.what == "rm") return cmd_repo_rm(args, ctx);
    return cmd_repo_list(args, ctx); // list (and its `ls` alias)
  }
  if (args.command == "verify") {
    return cmd_verify(args, ctx);
  }
  if (args.command == "label") {
    if (args.what == "add") return cmd_label_add(args, ctx);
    if (args.what == "rm") return cmd_label_rm(args, ctx);
    if (args.what == "list") return cmd_label_list(args, ctx);
    return cmd_label_resolve(args, ctx);
  }
  // list
  if (args.what == "components") {
    return cmd_list_components(args, ctx);
  }
  if (args.what == "dirs") {
    return cmd_list_dirs(args, ctx);
  }
  if (args.what == "files") {
    return cmd_list_files(args, ctx);
  }
  return cmd_list_symbols(args, ctx);
}

} // namespace cli
} // namespace cidx

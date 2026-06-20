// argv grammar (design D6) — hand-rolled parser reproducing the Python
// argparse tree of cli.py:452-557: exact flags, defaults (limits 25/50,
// --kind repo), choices, the `ls` alias, mutually-exclusive
// --indexed/--pending, and argparse's exit-2 usage policy (G29). Usage,
// help, and error text are transcribed verbatim from the Python tool
// (python3 -m indexer ... captured with COLUMNS=80, Python 3.14 argparse).
//
// Documented delta (D6): NO prefix-abbreviation — `--lim` is NOT accepted
// for `--limit`; it is reported as an unrecognized argument. Golden tests
// never use abbreviations.
//
// Misuse throws UsageError whose what() is the full argparse-formatted
// message (usage block + "<prog>: error: <msg>\n") and whose exit code is 2;
// main() is the only catch-site (D23).
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace cidx {
namespace cli {

// Tool version. Keep in sync with pyproject.toml [project].version and the
// Python tool (cli.py VERSION). `cidx --version` prints "cidx <kVersion>".
inline constexpr const char *kVersion = "0.14.2";

struct ParsedArgs {
  std::string command; // add-source | import | index | search | show | list
  std::string what;    // show: symbol|file; list: components|dirs|files|symbols

  // -h/--help anywhere: when set, print to stdout and exit 0 (argparse).
  std::optional<std::string> help_text;

  // --version at the top level: print "cidx <kVersion>" to stdout, exit 0.
  bool version = false;

  std::string path;                       // add-source --path (required)
  std::optional<std::string> name;        // add-source/import --name
  std::string db;                         // import --db (required)
  std::vector<std::string> files;         // index FILE...
  std::optional<std::string> source;      // index --source COMPONENT
  std::optional<std::string> pattern;     // search (required) / list (opt.)
  std::optional<std::string> kind;        // --kind (add-source default repo)
  int limit = 0;                          // search 25 / list symbols 50
  std::string symbol;                     // show symbol (required)
  std::string file;                       // show file (required)
  std::optional<std::string> component;   // --component/-c
  std::optional<std::string> dir;         // --dir/-d
  std::optional<std::string> file_filter; // list symbols --file/-f
  bool indexed = false;                   // list files --indexed
  bool pending = false;                   // list files --pending
  bool force = false;                     // init --force
  bool no_git = false;                    // add-source --no-git
  std::optional<int64_t> del_id;          // delete --id
  std::optional<std::string> del_path;    // delete --path
  std::optional<std::string> usr;         // delete symbol --usr
  bool dry_run = false;                   // delete --dry-run
  bool no_graph = false;                  // index --no-graph
  std::vector<std::string> assignment;    // set FIELD=VALUE [FIELD=VALUE ...]
  std::optional<std::string> index_db;    // set/file/dump-cc --db (index override)
  std::string target;                     // file/ast: target path or COMPONENT://PATH
  std::vector<std::string> op;            // file OP ... (REMAINDER tail)
  std::vector<std::string> rest;          // ast -- FLAGS... (REMAINDER tail)

  // -- ast sub-command fields (cidx ast dump|locals|conditions|cache) --------
  std::optional<std::string> ast_usr;    // --usr  (ast: exact clang USR)
  std::optional<int64_t> ast_id;         // --id   (ast: numeric symbol id)
  bool first = false;                    // --first (take closest --name match)
  bool ast_json = false;                 // --json  (emit machine-readable JSON)
  bool use_cache = true;                 // --cache/--no-cache (default: on)
  int depth = 0;                         // dump --depth N (0 = unlimited)
  bool tokens = false;                   // dump --tokens
  bool types = false;                    // dump --types
  bool params = false;                   // locals --params
  bool cond_ast = false;                 // conditions --ast
  std::string cache_action;              // ast cache build|status|clear

  // -- graph sub-command fields (cidx graph callers|callees|…) ---------------
  // Shared selector: reuse usr (above), kind (above), first (above), index_db.
  std::optional<int64_t> graph_id;       // --id  N (graph: numeric symbol id)
  bool graph_json = false;               // --json (emit machine-readable JSON)
  int graph_limit = 50;                  // --limit N (default 50)
  std::string direction{"out"};          // --direction {in,out} (default out)
  std::optional<std::string> edge;       // --edge KINDS (comma-separated)
  int graph_depth = 3;                   // --depth N (walk default 3, path 8)
  // path destination selector
  std::optional<std::string> to_usr;     // --to-usr USR
  std::optional<int64_t> to_id;          // --to-id N
  std::optional<std::string> to_name;    // --to-name FUZZY
  std::optional<std::string> to_kind;    // --to-kind {17 kinds}
  // hierarchy flags
  bool transitive = false;               // --transitive (walk whole hierarchy)
  std::string access{"all"};             // --access {public,protected,private,all}

  // -- portable-paths (v14) fields -------------------------------------------
  std::optional<std::string> version_str; // --version VER (add-source)
  bool no_detect_version = false;          // --no-detect-version (add-source)
  bool no_autoderive_labels = false;       // --no-autoderive-labels (index)
  std::optional<std::string> label_token; // label add/rm/resolve NAME
  std::optional<std::string> label_path;  // label add PATH

  // -- aliasing (v0.6.0) fields ----------------------------------------------
  bool no_alias = false; // import --no-alias (skip alias_options encoding)
};

// argv WITHOUT the program name. Throws UsageError on misuse.
ParsedArgs parse_args(const std::vector<std::string> &argv);

} // namespace cli
} // namespace cidx

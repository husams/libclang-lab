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

struct ParsedArgs {
  std::string command; // add-source | import | index | search | show | list
  std::string what;    // show: symbol|file; list: components|dirs|files|symbols

  // -h/--help anywhere: when set, print to stdout and exit 0 (argparse).
  std::optional<std::string> help_text;

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
  bool rebuild = false;                   // resolve --rebuild
  std::vector<std::string> assignment;    // set FIELD=VALUE [FIELD=VALUE ...]
  std::optional<std::string> index_db;    // set --db (index-path override)
};

// argv WITHOUT the program name. Throws UsageError on misuse.
ParsedArgs parse_args(const std::vector<std::string> &argv);

} // namespace cli
} // namespace cidx

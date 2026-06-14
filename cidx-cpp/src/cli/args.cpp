#include "cli/args.hpp"

#include <cctype>
#include <cstddef>
#include <limits>
#include <map>
#include <string>
#include <vector>

#include "util/errors.hpp"

namespace cidx {
namespace cli {
namespace {

// ---------------------------------------------------------------------------
// Usage / help text — transcribed VERBATIM from the Python tool
// (python3 -m indexer, Python 3.14 argparse, COLUMNS=80). Do not re-wrap.
// ---------------------------------------------------------------------------

const char kTopUsage[] =
    "usage: cidx [-h] {init,add-source,import,index,search,show,list,ls,delete} "
    "...\n";

const char kTopHelp[] =
    "usage: cidx [-h] "
    "{init,add-source,import,index,search,show,list,ls,delete} ...\n"
    "\n"
    "cidx command-line skeleton\n"
    "\n"
    "positional arguments:\n"
    "  {init,add-source,import,index,search,show,list,ls,delete}\n"
    "    init                create a blank index database\n"
    "    add-source          register a component\n"
    "    import              import a compile_commands.json\n"
    "    index               index imported C/C++ files\n"
    "    search              fuzzy-search symbols by qualified name\n"
    "    show                show full details of one symbol or file\n"
    "    list (ls)           browse the index: components, dirs, files, "
    "symbols\n"
    "    delete              delete a component, directory, file, or symbol\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n";

const char kInitUsage[] = "usage: cidx init [-h] [--force]\n";

const char kInitHelp[] =
    "usage: cidx init [-h] [--force]\n"
    "\n"
    "options:\n"
    "  -h, --help  show this help message and exit\n"
    "  --force     overwrite an existing index database\n";

const char kAddSourceUsage[] =
    "usage: cidx add-source [-h] --path PATH [--name NAME] [--kind "
    "{repo,external}]\n"
    "                       [--no-git]\n";

const char kAddSourceHelp[] =
    "usage: cidx add-source [-h] --path PATH [--name NAME] [--kind "
    "{repo,external}]\n"
    "                       [--no-git]\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --path PATH           repo root or library header dir\n"
    "  --name NAME           component name (default: from .git/config)\n"
    "  --kind {repo,external}\n"
    "  --no-git              use --path as-is; do not promote to the enclosing "
    "git\n"
    "                        root\n";

const char kImportUsage[] =
    "usage: cidx import [-h] --db DB [--name NAME] [--force]\n";

const char kImportHelp[] =
    "usage: cidx import [-h] --db DB [--name NAME] [--force]\n"
    "\n"
    "options:\n"
    "  -h, --help   show this help message and exit\n"
    "  --db DB      compile_commands.json (or the directory holding it)\n"
    "  --name NAME  component name override\n"
    "  --force      reimport: delete the existing component (its files and\n"
    "               indexed symbols) before importing\n";

const char kIndexUsage[] =
    "usage: cidx index [-h] [--source COMPONENT] [files ...]\n";

const char kIndexHelp[] =
    "usage: cidx index [-h] [--source COMPONENT] [files ...]\n"
    "\n"
    "positional arguments:\n"
    "  files               restrict to these files (default: all pending)\n"
    "\n"
    "options:\n"
    "  -h, --help          show this help message and exit\n"
    "  --source COMPONENT  resolve relative FILE paths against this "
    "component's\n"
    "                      root\n";

// The 17 symbol kinds, sorted — sorted(SYMBOL_KINDS) in cli.py.
#define CIDX_KIND_BRACE                                                        \
  "{class,class-template,constructor,destructor,enum,enum-constant,function,"  \
  "function-template,macro,member,method,namespace,struct,type-alias,"         \
  "typedef,union,variable}"

const char kSearchUsage[] = "usage: cidx search [-h]\n"
                            "                   [--kind " CIDX_KIND_BRACE "]\n"
                            "                   [--limit N]\n"
                            "                   pattern\n";

const char kSearchHelp[] =
    "usage: cidx search [-h]\n"
    "                   [--kind " CIDX_KIND_BRACE "]\n"
    "                   [--limit N]\n"
    "                   pattern\n"
    "\n"
    "positional arguments:\n"
    "  pattern               '::'-separated substrings matched in order, "
    "e.g.\n"
    "                        'conf::set' hits RdKafka::Conf::set\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --kind " CIDX_KIND_BRACE "\n"
    "                        restrict to one symbol kind\n"
    "  --limit N             show at most N matches (0 = all; default 25)\n";

const char kShowUsage[] = "usage: cidx show [-h] {symbol,file} ...\n";

const char kShowHelp[] = "usage: cidx show [-h] {symbol,file} ...\n"
                         "\n"
                         "positional arguments:\n"
                         "  {symbol,file}\n"
                         "    symbol       one symbol, by id or USR\n"
                         "    file         one file, by id or path\n"
                         "\n"
                         "options:\n"
                         "  -h, --help     show this help message and exit\n";

const char kShowSymbolUsage[] = "usage: cidx show symbol [-h] symbol\n";

const char kShowSymbolHelp[] =
    "usage: cidx show symbol [-h] symbol\n"
    "\n"
    "positional arguments:\n"
    "  symbol      numeric id (first column of 'search') or a clang USR; "
    "USRs\n"
    "              contain $ and * so single-quote them in the shell\n"
    "\n"
    "options:\n"
    "  -h, --help  show this help message and exit\n";

const char kShowFileUsage[] =
    "usage: cidx show file [-h] [--component NAME] file\n";

const char kShowFileHelp[] =
    "usage: cidx show file [-h] [--component NAME] file\n"
    "\n"
    "positional arguments:\n"
    "  file                  numeric id (first column of 'list files') or a "
    "path;\n"
    "                        relative paths resolve against the --component "
    "root\n"
    "                        (else the current directory)\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --component, -c NAME  component root for resolving a relative path\n";

const char kListUsage[] =
    "usage: cidx list [-h] {components,dirs,files,symbols} ...\n";

const char kListHelp[] =
    "usage: cidx list [-h] {components,dirs,files,symbols} ...\n"
    "\n"
    "positional arguments:\n"
    "  {components,dirs,files,symbols}\n"
    "    components          list registered components\n"
    "    dirs                list directories (all, or one component's)\n"
    "    files               list files for a component or a directory in "
    "it\n"
    "    symbols             list symbols for a component, directory, or "
    "file\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n";

const char kListComponentsUsage[] =
    "usage: cidx list components [-h] [--kind {repo,external}] [pattern]\n";

const char kListComponentsHelp[] =
    "usage: cidx list components [-h] [--kind {repo,external}] [pattern]\n"
    "\n"
    "positional arguments:\n"
    "  pattern               optional free-text fuzzy filter: characters "
    "must\n"
    "                        appear in order, e.g. 'shp' matches shapes.c\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --kind {repo,external}\n"
    "                        restrict to one component kind\n";

const char kListDirsUsage[] =
    "usage: cidx list dirs [-h] [--component NAME] [pattern]\n";

const char kListDirsHelp[] =
    "usage: cidx list dirs [-h] [--component NAME] [pattern]\n"
    "\n"
    "positional arguments:\n"
    "  pattern               optional free-text fuzzy filter: characters "
    "must\n"
    "                        appear in order, e.g. 'shp' matches shapes.c\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --component, -c NAME  restrict to this component\n";

const char kListFilesUsage[] =
    "usage: cidx list files [-h] [--component NAME] [--dir PATH] [--indexed "
    "|\n"
    "                       --pending]\n"
    "                       [pattern]\n";

const char kListFilesHelp[] =
    "usage: cidx list files [-h] [--component NAME] [--dir PATH] [--indexed "
    "|\n"
    "                       --pending]\n"
    "                       [pattern]\n"
    "\n"
    "positional arguments:\n"
    "  pattern               optional free-text fuzzy filter: characters "
    "must\n"
    "                        appear in order, e.g. 'shp' matches shapes.c\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --component, -c NAME  restrict to this component\n"
    "  --dir, -d PATH        directory (relative to the component root) "
    "including\n"
    "                        its subtree; needs --component\n"
    "  --indexed             only files already indexed\n"
    "  --pending             only files not yet indexed\n";

const char kListSymbolsUsage[] =
    "usage: cidx list symbols [-h] [--component NAME] [--dir PATH] [--file "
    "FILE]\n"
    "                         [--kind " CIDX_KIND_BRACE "]\n"
    "                         [--limit N]\n"
    "                         [pattern]\n";

const char kListSymbolsHelp[] =
    "usage: cidx list symbols [-h] [--component NAME] [--dir PATH] [--file "
    "FILE]\n"
    "                         [--kind " CIDX_KIND_BRACE "]\n"
    "                         [--limit N]\n"
    "                         [pattern]\n"
    "\n"
    "positional arguments:\n"
    "  pattern               optional free-text fuzzy filter: characters "
    "must\n"
    "                        appear in order, e.g. 'shp' matches shapes.c "
    "(matched\n"
    "                        against the qualified name)\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --component, -c NAME  restrict to this component\n"
    "  --dir, -d PATH        directory (relative to the component root) "
    "including\n"
    "                        its subtree; needs --component\n"
    "  --file, -f FILE       one file; relative paths resolve against the\n"
    "                        --component root (else the current directory)\n"
    "  --kind " CIDX_KIND_BRACE "\n"
    "                        restrict to one symbol kind\n"
    "  --limit N             show at most N matches (0 = all; default 50)\n";

const char kDeleteUsage[] =
    "usage: cidx delete [-h] {component,dir,file,symbol} ...\n";

const char kDeleteHelp[] =
    "usage: cidx delete [-h] {component,dir,file,symbol} ...\n"
    "\n"
    "positional arguments:\n"
    "  {component,dir,file,symbol}\n"
    "    component           delete a component and everything indexed from it\n"
    "    dir                 delete a directory, its files, and their symbols\n"
    "    file                delete a file and its symbols\n"
    "    symbol              delete a symbol\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n";

const char kDeleteComponentUsage[] =
    "usage: cidx delete component [-h] (--id ID | --name NAME | --path PATH)\n"
    "                             [--dry-run]\n";

const char kDeleteComponentHelp[] =
    "usage: cidx delete component [-h] (--id ID | --name NAME | --path PATH)\n"
    "                             [--dry-run]\n"
    "\n"
    "options:\n"
    "  -h, --help   show this help message and exit\n"
    "  --id ID      component id\n"
    "  --name NAME  component name\n"
    "  --path PATH  component root path\n"
    "  --dry-run    preview the matches without deleting anything\n";

const char kDeleteDirUsage[] =
    "usage: cidx delete dir [-h] (--id ID | --path PATH) [--component NAME]\n"
    "                       [--dry-run]\n";

const char kDeleteDirHelp[] =
    "usage: cidx delete dir [-h] (--id ID | --path PATH) [--component NAME]\n"
    "                       [--dry-run]\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --id ID               directory id\n"
    "  --path PATH           directory path\n"
    "  --component, -c NAME  restrict the match to this component\n"
    "  --dry-run             preview the matches without deleting anything\n";

const char kDeleteFileUsage[] =
    "usage: cidx delete file [-h] (--id ID | --name NAME | --path PATH)\n"
    "                        [--component NAME] [--dry-run]\n";

const char kDeleteFileHelp[] =
    "usage: cidx delete file [-h] (--id ID | --name NAME | --path PATH)\n"
    "                        [--component NAME] [--dry-run]\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --id ID               file id\n"
    "  --name NAME           file basename\n"
    "  --path PATH           file path\n"
    "  --component, -c NAME  restrict the match to this component\n"
    "  --dry-run             preview the matches without deleting anything\n";

const char kDeleteSymbolUsage[] =
    "usage: cidx delete symbol [-h] (--id ID | --name NAME | --usr USR)\n"
    "                          [--component NAME] [--dry-run]\n";

const char kDeleteSymbolHelp[] =
    "usage: cidx delete symbol [-h] (--id ID | --name NAME | --usr USR)\n"
    "                          [--component NAME] [--dry-run]\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --id ID               symbol id\n"
    "  --name NAME           symbol spelling\n"
    "  --usr USR             clang USR\n"
    "  --component, -c NAME  restrict the match to this component\n"
    "  --dry-run             preview the matches without deleting anything\n";

// ---------------------------------------------------------------------------
// Choice sets
// ---------------------------------------------------------------------------

const std::vector<std::string> kComponentKinds = {"repo", "external"};
const std::vector<std::string> kSymbolKinds = {
    "class",   "class-template", "constructor", "destructor",
    "enum",    "enum-constant",  "function",    "function-template",
    "macro",   "member",         "method",      "namespace",
    "struct",  "type-alias",     "typedef",     "union",
    "variable"};
const std::vector<std::string> kCommands = {
    "init",   "add-source", "import", "index",
    "search", "show",       "list",   "ls",    "delete"};
const std::vector<std::string> kShowWhats = {"symbol", "file"};
const std::vector<std::string> kListWhats = {"components", "dirs", "files",
                                             "symbols"};
const std::vector<std::string> kDeleteWhats = {"component", "dir", "file",
                                               "symbol"};

// ---------------------------------------------------------------------------
// Engine
// ---------------------------------------------------------------------------

enum class ValueKind { kNone, kString, kInt };

struct OptSpec {
  const char *name;     // long option, "--limit"
  char short_opt;       // 'c' or '\0'
  ValueKind value;      // kNone = store_true flag
  const char *err_name; // argparse's name in messages: "--component/-c"
  const std::vector<std::string> *choices = nullptr;
  int mutex = 0; // mutually-exclusive group id; 0 = none
};

struct Spec {
  const char *prog;  // "cidx search"
  const char *usage; // usage block, trailing '\n'
  const char *help;  // full help text, trailing '\n'
  std::vector<OptSpec> opts;
  std::vector<const char *> positionals; // fixed positionals, in order
  bool rest = false;                     // collect surplus (index FILE...)
  // names reported by the required check, in argparse action-add order
  std::vector<const char *> required;
  // mutex group ids that argparse marks required: when none of a group's
  // members is seen, argparse fails with "one of the arguments ... is required"
  std::vector<int> required_mutex = {};
};

struct ParseState {
  std::map<std::string, std::string> values; // long name -> raw value
  std::map<std::string, bool> flags;
  std::vector<std::string> positionals;
  std::vector<std::string> rest;
  bool help = false;
};

[[noreturn]] void fail(const char *usage, const std::string &prog,
                       const std::string &msg) {
  throw UsageError(std::string(usage) + prog + ": error: " + msg + "\n", 2);
}

[[noreturn]] void fail(const Spec &spec, const std::string &msg) {
  fail(spec.usage, spec.prog, msg);
}

std::string join(const std::vector<std::string> &parts,
                 const std::string &sep) {
  std::string out;
  for (std::size_t i = 0; i < parts.size(); ++i) {
    if (i != 0) {
      out += sep;
    }
    out += parts[i];
  }
  return out;
}

// argparse's _negative_number_matcher: ^-\d+$|^-\d*\.\d+$ — such tokens are
// treated as values/positionals because no option string looks like one.
bool is_negative_number(const std::string &tok) {
  if (tok.size() < 2 || tok[0] != '-') {
    return false;
  }
  std::size_t i = 1;
  std::size_t digits = 0;
  while (i < tok.size() && std::isdigit(static_cast<unsigned char>(tok[i]))) {
    ++i;
    ++digits;
  }
  if (i == tok.size()) {
    return digits > 0; // ^-\d+$
  }
  if (tok[i] != '.') {
    return false;
  }
  ++i; // ^-\d*\.\d+$
  digits = 0;
  while (i < tok.size() && std::isdigit(static_cast<unsigned char>(tok[i]))) {
    ++i;
    ++digits;
  }
  return i == tok.size() && digits > 0;
}

bool is_option_token(const std::string &tok) {
  return tok.size() > 1 && tok[0] == '-' && !is_negative_number(tok);
}

// Python int(str): surrounding whitespace stripped, optional sign, digits.
bool parse_py_int(const std::string &raw, long &out) {
  std::size_t b = 0;
  std::size_t e = raw.size();
  while (b < e && std::isspace(static_cast<unsigned char>(raw[b]))) {
    ++b;
  }
  while (e > b && std::isspace(static_cast<unsigned char>(raw[e - 1]))) {
    --e;
  }
  if (b == e) {
    return false;
  }
  bool neg = false;
  if (raw[b] == '+' || raw[b] == '-') {
    neg = raw[b] == '-';
    ++b;
  }
  if (b == e) {
    return false;
  }
  // Saturate positive accumulation at INT_MAX to avoid signed-overflow UB.
  // Huge positive limit → INT_MAX → "show all" semantics are preserved.
  constexpr long kPosMax = static_cast<long>(std::numeric_limits<int>::max());
  long val = 0;
  bool saturated = false;
  for (std::size_t i = b; i < e; ++i) {
    if (!std::isdigit(static_cast<unsigned char>(raw[i]))) {
      return false;
    }
    const long digit = raw[i] - '0';
    if (!neg && val > (kPosMax - digit) / 10) {
      // Would overflow INT_MAX: consume and validate remaining digits, then cap.
      while (++i < e) {
        if (!std::isdigit(static_cast<unsigned char>(raw[i]))) {
          return false;
        }
      }
      saturated = true;
      val = kPosMax;
      break;
    }
    val = val * 10 + digit;
  }
  (void)saturated;
  out = neg ? -val : val;
  return true;
}

const OptSpec *find_long(const Spec &spec, const std::string &name) {
  for (const OptSpec &o : spec.opts) {
    if (name == o.name) {
      return &o;
    }
  }
  return nullptr;
}

const OptSpec *find_short(const Spec &spec, char c) {
  for (const OptSpec &o : spec.opts) {
    if (o.short_opt != '\0' && o.short_opt == c) {
      return &o;
    }
  }
  return nullptr;
}

// Validate at encounter time (argparse converts/checks per consumed
// argument, in scan order).
void check_value(const Spec &spec, const OptSpec &opt, const std::string &val) {
  if (opt.choices != nullptr) {
    for (const std::string &c : *opt.choices) {
      if (val == c) {
        return;
      }
    }
    fail(spec, "argument " + std::string(opt.err_name) + ": invalid choice: '" +
                   val + "' (choose from " + join(*opt.choices, ", ") + ")");
  }
  if (opt.value == ValueKind::kInt) {
    long parsed = 0;
    if (!parse_py_int(val, parsed)) {
      fail(spec, "argument " + std::string(opt.err_name) +
                     ": invalid int value: '" + val + "'");
    }
  }
}

// Parse tokens[i..] against a leaf spec. Unknown options / surplus
// positionals are appended to `extras` (reported by the caller as the TOP
// parser's "unrecognized arguments" — argparse parse_known_args semantics,
// fired only after subparser-level errors had their chance).
ParseState parse_leaf(const Spec &spec, const std::vector<std::string> &tokens,
                      std::size_t i, std::vector<std::string> &extras) {
  ParseState st;
  std::map<int, const char *> mutex_seen;
  bool only_positionals = false;
  const std::size_t n = tokens.size();
  while (i < n) {
    const std::string &tok = tokens[i];
    if (!only_positionals && tok == "--") {
      only_positionals = true;
      ++i;
      continue;
    }
    if (!only_positionals && is_option_token(tok)) {
      if (tok == "-h" || tok == "--help") {
        st.help = true;
        return st;
      }
      const OptSpec *opt = nullptr;
      std::string inline_val;
      bool has_inline = false;
      if (tok.starts_with("--")) {
        std::string name = tok;
        const std::size_t eq = tok.find('=');
        if (eq != std::string::npos) {
          name = tok.substr(0, eq);
          inline_val = tok.substr(eq + 1);
          has_inline = true;
        }
        opt = find_long(spec, name); // exact match only — no abbreviation (D6)
      } else {
        opt = find_short(spec, tok[1]);
        if (opt != nullptr && tok.size() > 2) { // glued short value: -cNAME
          inline_val = tok.substr(2);
          has_inline = true;
        }
      }
      if (opt == nullptr) {
        extras.push_back(tok);
        ++i;
        continue;
      }
      if (opt->mutex != 0) {
        auto seen = mutex_seen.find(opt->mutex);
        if (seen != mutex_seen.end() && seen->second != opt->err_name) {
          fail(spec, "argument " + std::string(opt->err_name) +
                         ": not allowed with argument " + seen->second);
        }
        mutex_seen[opt->mutex] = opt->err_name;
      }
      if (opt->value == ValueKind::kNone) {
        if (has_inline) {
          fail(spec, "argument " + std::string(opt->err_name) +
                         ": ignored explicit argument '" + inline_val + "'");
        }
        st.flags[opt->name] = true;
        ++i;
        continue;
      }
      std::string val;
      if (has_inline) {
        val = inline_val;
      } else {
        if (i + 1 >= n || is_option_token(tokens[i + 1])) {
          fail(spec, "argument " + std::string(opt->err_name) +
                         ": expected one argument");
        }
        val = tokens[++i];
      }
      check_value(spec, *opt, val);
      st.values[opt->name] = val; // repeated flags: last one wins (argparse)
      ++i;
      continue;
    }
    // positional
    if (st.positionals.size() < spec.positionals.size()) {
      st.positionals.push_back(tok);
    } else if (spec.rest) {
      st.rest.push_back(tok);
    } else {
      extras.push_back(tok);
    }
    ++i;
  }
  // Required check (argparse: after the scan, before the caller's
  // unrecognized-arguments check — verified against the Python tool).
  std::vector<std::string> missing;
  for (const char *name : spec.required) {
    if (name[0] == '-') {
      if (st.values.find(name) == st.values.end()) {
        missing.push_back(name);
      }
    } else {
      std::size_t pos_index = 0;
      for (std::size_t p = 0; p < spec.positionals.size(); ++p) {
        if (std::string(spec.positionals[p]) == name) {
          pos_index = p;
          break;
        }
      }
      if (pos_index >= st.positionals.size()) {
        missing.push_back(name);
      }
    }
  }
  if (!missing.empty()) {
    fail(spec, "the following arguments are required: " + join(missing, ", "));
  }
  // Required mutually-exclusive groups: argparse fails when none of the
  // group's members was supplied. Members are listed in spec.opts order.
  for (const int grp : spec.required_mutex) {
    std::vector<std::string> members;
    bool seen = false;
    for (const OptSpec &o : spec.opts) {
      if (o.mutex == grp) {
        members.emplace_back(o.name);
        if (st.values.find(o.name) != st.values.end() ||
            st.flags.find(o.name) != st.flags.end()) {
          seen = true;
        }
      }
    }
    if (!seen) {
      fail(spec, "one of the arguments " + join(members, " ") + " is required");
    }
  }
  return st;
}

std::optional<std::string> opt_value(const ParseState &st, const char *name) {
  auto it = st.values.find(name);
  if (it == st.values.end()) {
    return std::nullopt;
  }
  return it->second;
}

int int_value(const ParseState &st, const char *name, int def) {
  auto it = st.values.find(name);
  if (it == st.values.end()) {
    return def;
  }
  long parsed = 0;
  parse_py_int(it->second, parsed); // validated at encounter; positive saturated at INT_MAX
  // Clamp to [INT_MIN, INT_MAX] to make static_cast safe.
  if (parsed > static_cast<long>(std::numeric_limits<int>::max())) {
    parsed = static_cast<long>(std::numeric_limits<int>::max());
  } else if (parsed < static_cast<long>(std::numeric_limits<int>::min())) {
    parsed = static_cast<long>(std::numeric_limits<int>::min());
  }
  return static_cast<int>(parsed);
}

// Scan for a sub-command token (the top parser and the show/list parsers all
// do this): options before it go to extras, -h shows this level's help.
struct CommandScan {
  std::optional<std::string> command;
  std::size_t next = 0;
  bool help = false;
};

CommandScan scan_command(const std::vector<std::string> &tokens, std::size_t i,
                         std::vector<std::string> &extras) {
  CommandScan out;
  const std::size_t n = tokens.size();
  while (i < n) {
    const std::string &tok = tokens[i];
    if (tok == "-h" || tok == "--help") {
      out.help = true;
      out.next = i + 1;
      return out;
    }
    if (is_option_token(tok)) {
      extras.push_back(tok);
      ++i;
      continue;
    }
    out.command = tok;
    out.next = i + 1;
    return out;
  }
  out.next = n;
  return out;
}

bool contains(const std::vector<std::string> &v, const std::string &s) {
  for (const std::string &e : v) {
    if (e == s) {
      return true;
    }
  }
  return false;
}

// -- leaf specs --------------------------------------------------------------

const Spec kInitSpec = {
    "cidx init",
    kInitUsage,
    kInitHelp,
    {
        {"--force", '\0', ValueKind::kNone, "--force", nullptr, 0},
    },
    {},
    false,
    {},
};

const Spec kAddSourceSpec = {
    "cidx add-source",
    kAddSourceUsage,
    kAddSourceHelp,
    {
        {"--path", '\0', ValueKind::kString, "--path", nullptr, 0},
        {"--name", '\0', ValueKind::kString, "--name", nullptr, 0},
        {"--kind", '\0', ValueKind::kString, "--kind", &kComponentKinds, 0},
        {"--no-git", '\0', ValueKind::kNone, "--no-git", nullptr, 0},
    },
    {},
    false,
    {"--path"},
};

const Spec kImportSpec = {
    "cidx import",
    kImportUsage,
    kImportHelp,
    {
        {"--db", '\0', ValueKind::kString, "--db", nullptr, 0},
        {"--name", '\0', ValueKind::kString, "--name", nullptr, 0},
        {"--force", '\0', ValueKind::kNone, "--force", nullptr, 0},
    },
    {},
    false,
    {"--db"},
};

const Spec kIndexSpec = {
    "cidx index",
    kIndexUsage,
    kIndexHelp,
    {
        {"--source", '\0', ValueKind::kString, "--source", nullptr, 0},
    },
    {},
    true, // files: nargs="*"
    {},
};

const Spec kSearchSpec = {
    "cidx search",
    kSearchUsage,
    kSearchHelp,
    {
        {"--kind", '\0', ValueKind::kString, "--kind", &kSymbolKinds, 0},
        {"--limit", '\0', ValueKind::kInt, "--limit", nullptr, 0},
    },
    {"pattern"},
    false,
    {"pattern"},
};

const Spec kShowSymbolSpec = {
    "cidx show symbol", kShowSymbolUsage,
    kShowSymbolHelp,    {},
    {"symbol"},         false,
    {"symbol"},
};

const Spec kShowFileSpec = {
    "cidx show file",
    kShowFileUsage,
    kShowFileHelp,
    {
        {"--component", 'c', ValueKind::kString, "--component/-c", nullptr, 0},
    },
    {"file"},
    false,
    {"file"},
};

const Spec kListComponentsSpec = {
    "cidx list components",
    kListComponentsUsage,
    kListComponentsHelp,
    {
        {"--kind", '\0', ValueKind::kString, "--kind", &kComponentKinds, 0},
    },
    {"pattern"},
    false,
    {},
};

const Spec kListDirsSpec = {
    "cidx list dirs",
    kListDirsUsage,
    kListDirsHelp,
    {
        {"--component", 'c', ValueKind::kString, "--component/-c", nullptr, 0},
    },
    {"pattern"},
    false,
    {},
};

const Spec kListFilesSpec = {
    "cidx list files",
    kListFilesUsage,
    kListFilesHelp,
    {
        {"--component", 'c', ValueKind::kString, "--component/-c", nullptr, 0},
        {"--dir", 'd', ValueKind::kString, "--dir/-d", nullptr, 0},
        {"--indexed", '\0', ValueKind::kNone, "--indexed", nullptr, 1},
        {"--pending", '\0', ValueKind::kNone, "--pending", nullptr, 1},
    },
    {"pattern"},
    false,
    {},
};

const Spec kListSymbolsSpec = {
    "cidx list symbols",
    kListSymbolsUsage,
    kListSymbolsHelp,
    {
        {"--component", 'c', ValueKind::kString, "--component/-c", nullptr, 0},
        {"--dir", 'd', ValueKind::kString, "--dir/-d", nullptr, 0},
        {"--file", 'f', ValueKind::kString, "--file/-f", nullptr, 0},
        {"--kind", '\0', ValueKind::kString, "--kind", &kSymbolKinds, 0},
        {"--limit", '\0', ValueKind::kInt, "--limit", nullptr, 0},
    },
    {"pattern"},
    false,
    {},
};

const Spec kDeleteComponentSpec = {
    "cidx delete component",
    kDeleteComponentUsage,
    kDeleteComponentHelp,
    {
        {"--id", '\0', ValueKind::kInt, "--id", nullptr, 1},
        {"--name", '\0', ValueKind::kString, "--name", nullptr, 1},
        {"--path", '\0', ValueKind::kString, "--path", nullptr, 1},
        {"--dry-run", '\0', ValueKind::kNone, "--dry-run", nullptr, 0},
    },
    {},
    false,
    {},
    {1},
};

const Spec kDeleteDirSpec = {
    "cidx delete dir",
    kDeleteDirUsage,
    kDeleteDirHelp,
    {
        {"--id", '\0', ValueKind::kInt, "--id", nullptr, 1},
        {"--path", '\0', ValueKind::kString, "--path", nullptr, 1},
        {"--component", 'c', ValueKind::kString, "--component/-c", nullptr, 0},
        {"--dry-run", '\0', ValueKind::kNone, "--dry-run", nullptr, 0},
    },
    {},
    false,
    {},
    {1},
};

const Spec kDeleteFileSpec = {
    "cidx delete file",
    kDeleteFileUsage,
    kDeleteFileHelp,
    {
        {"--id", '\0', ValueKind::kInt, "--id", nullptr, 1},
        {"--name", '\0', ValueKind::kString, "--name", nullptr, 1},
        {"--path", '\0', ValueKind::kString, "--path", nullptr, 1},
        {"--component", 'c', ValueKind::kString, "--component/-c", nullptr, 0},
        {"--dry-run", '\0', ValueKind::kNone, "--dry-run", nullptr, 0},
    },
    {},
    false,
    {},
    {1},
};

const Spec kDeleteSymbolSpec = {
    "cidx delete symbol",
    kDeleteSymbolUsage,
    kDeleteSymbolHelp,
    {
        {"--id", '\0', ValueKind::kInt, "--id", nullptr, 1},
        {"--name", '\0', ValueKind::kString, "--name", nullptr, 1},
        {"--usr", '\0', ValueKind::kString, "--usr", nullptr, 1},
        {"--component", 'c', ValueKind::kString, "--component/-c", nullptr, 0},
        {"--dry-run", '\0', ValueKind::kNone, "--dry-run", nullptr, 0},
    },
    {},
    false,
    {},
    {1},
};

} // namespace

ParsedArgs parse_args(const std::vector<std::string> &argv) {
  std::vector<std::string> extras;
  ParsedArgs pa;

  CommandScan top = scan_command(argv, 0, extras);
  if (top.help) {
    pa.help_text = kTopHelp;
    return pa;
  }
  if (!top.command) {
    fail(kTopUsage, "cidx", "the following arguments are required: command");
  }
  if (!contains(kCommands, *top.command)) {
    fail(kTopUsage, "cidx",
         "argument command: invalid choice: '" + *top.command +
             "' (choose from " + join(kCommands, ", ") + ")");
  }
  pa.command = *top.command == "ls" ? "list" : *top.command;
  std::size_t i = top.next;

  if (pa.command == "init") {
    ParseState st = parse_leaf(kInitSpec, argv, i, extras);
    if (st.help) {
      pa.help_text = kInitHelp;
      return pa;
    }
    pa.force = st.flags.count("--force") != 0;
  } else if (pa.command == "add-source") {
    ParseState st = parse_leaf(kAddSourceSpec, argv, i, extras);
    if (st.help) {
      pa.help_text = kAddSourceHelp;
      return pa;
    }
    pa.path = st.values["--path"];
    pa.name = opt_value(st, "--name");
    pa.kind = opt_value(st, "--kind");
    if (!pa.kind) {
      pa.kind = "repo"; // argparse default
    }
    pa.no_git = st.flags.count("--no-git") != 0;
  } else if (pa.command == "import") {
    ParseState st = parse_leaf(kImportSpec, argv, i, extras);
    if (st.help) {
      pa.help_text = kImportHelp;
      return pa;
    }
    pa.db = st.values["--db"];
    pa.name = opt_value(st, "--name");
    pa.force = st.flags.count("--force") != 0;
  } else if (pa.command == "index") {
    ParseState st = parse_leaf(kIndexSpec, argv, i, extras);
    if (st.help) {
      pa.help_text = kIndexHelp;
      return pa;
    }
    pa.files = st.rest;
    pa.source = opt_value(st, "--source");
  } else if (pa.command == "search") {
    ParseState st = parse_leaf(kSearchSpec, argv, i, extras);
    if (st.help) {
      pa.help_text = kSearchHelp;
      return pa;
    }
    pa.pattern = st.positionals[0];
    pa.kind = opt_value(st, "--kind");
    pa.limit = int_value(st, "--limit", 25);
  } else if (pa.command == "show") {
    CommandScan what = scan_command(argv, i, extras);
    if (what.help) {
      pa.help_text = kShowHelp;
      return pa;
    }
    if (!what.command) {
      fail(kShowUsage, "cidx show",
           "the following arguments are required: what");
    }
    if (!contains(kShowWhats, *what.command)) {
      fail(kShowUsage, "cidx show",
           "argument what: invalid choice: '" + *what.command +
               "' (choose from " + join(kShowWhats, ", ") + ")");
    }
    pa.what = *what.command;
    if (pa.what == "symbol") {
      ParseState st = parse_leaf(kShowSymbolSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kShowSymbolHelp;
        return pa;
      }
      pa.symbol = st.positionals[0];
    } else {
      ParseState st = parse_leaf(kShowFileSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kShowFileHelp;
        return pa;
      }
      pa.file = st.positionals[0];
      pa.component = opt_value(st, "--component");
    }
  } else if (pa.command == "list") { // list / ls
    CommandScan what = scan_command(argv, i, extras);
    if (what.help) {
      pa.help_text = kListHelp;
      return pa;
    }
    if (!what.command) {
      fail(kListUsage, "cidx list",
           "the following arguments are required: what");
    }
    if (!contains(kListWhats, *what.command)) {
      fail(kListUsage, "cidx list",
           "argument what: invalid choice: '" + *what.command +
               "' (choose from " + join(kListWhats, ", ") + ")");
    }
    pa.what = *what.command;
    if (pa.what == "components") {
      ParseState st = parse_leaf(kListComponentsSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kListComponentsHelp;
        return pa;
      }
      if (!st.positionals.empty()) {
        pa.pattern = st.positionals[0];
      }
      pa.kind = opt_value(st, "--kind");
    } else if (pa.what == "dirs") {
      ParseState st = parse_leaf(kListDirsSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kListDirsHelp;
        return pa;
      }
      if (!st.positionals.empty()) {
        pa.pattern = st.positionals[0];
      }
      pa.component = opt_value(st, "--component");
    } else if (pa.what == "files") {
      ParseState st = parse_leaf(kListFilesSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kListFilesHelp;
        return pa;
      }
      if (!st.positionals.empty()) {
        pa.pattern = st.positionals[0];
      }
      pa.component = opt_value(st, "--component");
      pa.dir = opt_value(st, "--dir");
      pa.indexed = st.flags.count("--indexed") != 0;
      pa.pending = st.flags.count("--pending") != 0;
    } else { // symbols
      ParseState st = parse_leaf(kListSymbolsSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kListSymbolsHelp;
        return pa;
      }
      if (!st.positionals.empty()) {
        pa.pattern = st.positionals[0];
      }
      pa.component = opt_value(st, "--component");
      pa.dir = opt_value(st, "--dir");
      pa.file_filter = opt_value(st, "--file");
      pa.kind = opt_value(st, "--kind");
      pa.limit = int_value(st, "--limit", 50);
    }
  } else { // delete
    CommandScan what = scan_command(argv, i, extras);
    if (what.help) {
      pa.help_text = kDeleteHelp;
      return pa;
    }
    if (!what.command) {
      fail(kDeleteUsage, "cidx delete",
           "the following arguments are required: what");
    }
    if (!contains(kDeleteWhats, *what.command)) {
      fail(kDeleteUsage, "cidx delete",
           "argument what: invalid choice: '" + *what.command +
               "' (choose from " + join(kDeleteWhats, ", ") + ")");
    }
    pa.what = *what.command;
    const Spec *spec = nullptr;
    const char *leaf_help = nullptr;
    if (pa.what == "component") {
      spec = &kDeleteComponentSpec;
      leaf_help = kDeleteComponentHelp;
    } else if (pa.what == "dir") {
      spec = &kDeleteDirSpec;
      leaf_help = kDeleteDirHelp;
    } else if (pa.what == "file") {
      spec = &kDeleteFileSpec;
      leaf_help = kDeleteFileHelp;
    } else {
      spec = &kDeleteSymbolSpec;
      leaf_help = kDeleteSymbolHelp;
    }
    ParseState st = parse_leaf(*spec, argv, what.next, extras);
    if (st.help) {
      pa.help_text = leaf_help;
      return pa;
    }
    if (const std::optional<std::string> id = opt_value(st, "--id")) {
      long parsed = 0;
      parse_py_int(*id, parsed); // validated at encounter time
      pa.del_id = static_cast<int64_t>(parsed);
    }
    pa.name = opt_value(st, "--name");
    pa.del_path = opt_value(st, "--path");
    pa.usr = opt_value(st, "--usr");
    pa.component = opt_value(st, "--component");
    pa.dry_run = st.flags.count("--dry-run") != 0;
  }

  // argparse parse_args: anything parse_known_args left over is reported by
  // the TOP parser, after all subparser-level errors had their chance.
  if (!extras.empty()) {
    fail(kTopUsage, "cidx", "unrecognized arguments: " + join(extras, " "));
  }
  return pa;
}

} // namespace cli
} // namespace cidx

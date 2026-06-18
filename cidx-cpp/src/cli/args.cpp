#include "cli/args.hpp"

#include <cctype>
#include <cstddef>
#include <cstring>
#include <limits>
#include <map>
#include <string>
#include <vector>

#include "util/errors.hpp"
#include "util/pathutil.hpp"

namespace cidx {
namespace cli {
namespace {

// ---------------------------------------------------------------------------
// Usage / help text — transcribed VERBATIM from the Python tool
// (python3 -m indexer, Python 3.14 argparse, COLUMNS=80). Do not re-wrap.
// ---------------------------------------------------------------------------

const char kTopUsage[] =
    "usage: cidx [-h] [--version]\n"
    "            "
    "{init,add-source,import,index,resolve,set,file,dump-compile-commands,"
    "search,show,list,ls,delete,graph,ast} "
    "...\n";

const char kTopHelp[] =
    "usage: cidx [-h] [--version]\n"
    "            "
    "{init,add-source,import,index,resolve,set,file,dump-compile-commands,"
    "search,show,list,ls,delete,graph,ast} "
    "...\n"
    "\n"
    "cidx command-line skeleton\n"
    "\n"
    "positional arguments:\n"
    "  {init,add-source,import,index,resolve,set,file,dump-compile-commands,"
    "search,show,list,ls,delete,graph,ast}\n"
    "    init                create a blank index database\n"
    "    add-source          register a component\n"
    "    import              import a compile_commands.json\n"
    "    index               index imported C/C++ files\n"
    "    resolve             finalize cross-repo edges and roll up edge counts\n"
    "    set                 set a mutable file attribute (e.g. pending "
    "status)\n"
    "    file                inspect or edit one file's stored compile flags\n"
    "    dump-compile-commands\n"
    "                        emit a compile_commands.json for a component\n"
    "    search              fuzzy-search symbols by qualified name\n"
    "    show                show full details of one symbol or file\n"
    "    list (ls)           browse the index: components, dirs, files, "
    "symbols\n"
    "    delete              delete a component, directory, file, or symbol\n"
    "    graph               query the relationship graph (callers, callees, "
    "refs, neighbors, walk, path, hierarchy, dispatch)\n"
    "    ast                 on-demand AST analysis (dump, locals, conditions, "
    "cache)\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --version             show program's version number and exit\n";

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
    "usage: cidx index [-h] [--source COMPONENT] [--no-graph] [files ...]\n";

const char kIndexHelp[] =
    "usage: cidx index [-h] [--source COMPONENT] [--no-graph] [files ...]\n"
    "\n"
    "positional arguments:\n"
    "  files               restrict to these files (default: all pending)\n"
    "\n"
    "options:\n"
    "  -h, --help          show this help message and exit\n"
    "  --source COMPONENT  resolve relative FILE paths against this "
    "component's\n"
    "                      root\n"
    "  --no-graph          skip relationship-graph extraction (calls, inherits, …)\n";

const char kResolveUsage[] =
    "usage: cidx resolve [-h] [--rebuild]\n";

const char kResolveHelp[] =
    "usage: cidx resolve [-h] [--rebuild]\n"
    "\n"
    "options:\n"
    "  -h, --help   show this help message and exit\n"
    "  --rebuild    clear all edges before resolving (forces full re-extract)\n";

const char kSetUsage[] =
    "usage: cidx set [-h] [--component NAME] [--file REL_PATH] [--db PATH]\n"
    "                [--dry-run]\n"
    "                FIELD=VALUE [FIELD=VALUE ...]\n";

const char kSetHelp[] =
    "usage: cidx set [-h] [--component NAME] [--file REL_PATH] [--db PATH]\n"
    "                [--dry-run]\n"
    "                FIELD=VALUE [FIELD=VALUE ...]\n"
    "\n"
    "positional arguments:\n"
    "  FIELD=VALUE           attribute assignment, e.g. 'pending=False' "
    "(fields:\n"
    "                        pending, indexed)\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --component, -c NAME  restrict to this component's files\n"
    "  --file REL_PATH       restrict to one file (path relative to component "
    "root)\n"
    "  --db PATH             operate on this index DB (default: the standard "
    "index)\n"
    "  --dry-run             preview the matches without changing anything\n";

const char kFileUsage[] =
    "usage: cidx file [-h] [--db PATH] COMPONENT://PATH ...\n";

const char kFileHelp[] =
    "usage: cidx file [-h] [--db PATH] COMPONENT://PATH ...\n"
    "\n"
    "positional arguments:\n"
    "  COMPONENT://PATH  file address, e.g. 'mylib://src/foo.c'\n"
    "  OP                -set-flag FLAG | -unset-flag FLAG | -import-args JSON "
    "|\n"
    "                    -dump-args (default when omitted)\n"
    "\n"
    "options:\n"
    "  -h, --help        show this help message and exit\n"
    "  --db PATH         operate on this index DB (default: the standard "
    "index)\n";

const char kDumpCcUsage[] =
    "usage: cidx dump-compile-commands [-h] [--db PATH] COMPONENT\n";

const char kDumpCcHelp[] =
    "usage: cidx dump-compile-commands [-h] [--db PATH] COMPONENT\n"
    "\n"
    "positional arguments:\n"
    "  COMPONENT   component whose files to emit\n"
    "\n"
    "options:\n"
    "  -h, --help  show this help message and exit\n"
    "  --db PATH   operate on this index DB (default: the standard index)\n";

// ---- graph help texts -------------------------------------------------------

const char kGraphUsage[] =
    "usage: cidx graph [-h]\n"
    "                  "
    "{callers,callees,refs,neighbors,walk,path,hierarchy,dispatch} ...\n";

const char kGraphHelp[] =
    "usage: cidx graph [-h]\n"
    "                  "
    "{callers,callees,refs,neighbors,walk,path,hierarchy,dispatch} ...\n"
    "\n"
    "positional arguments:\n"
    "  {callers,callees,refs,neighbors,walk,path,hierarchy,dispatch}\n"
    "    callers             functions that call the symbol\n"
    "    callees             functions the symbol calls\n"
    "    refs                incoming references (calls + uses) to the symbol\n"
    "    neighbors           one-hop typed neighbors\n"
    "    walk                bounded BFS over typed edges\n"
    "    path                shortest path between two symbols, or none\n"
    "    hierarchy           class bases, subclasses, and members\n"
    "    dispatch            run-time targets of a virtual-method call\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n";

// The shared selector block (--usr/--id/--name/--kind/--first/--db/--json/
// --limit) appears in every graph subcommand's usage.
#define GRAPH_SELECTOR_USAGE_ARGS                                              \
  "(--usr USR | --id N | --name FUZZY)\n"                                     \
  "                          [--kind {class,class-template,constructor,"       \
  "destructor,enum,enum-constant,function,function-template,macro,member,"     \
  "method,namespace,struct,type-alias,typedef,union,variable}]\n"             \
  "                          [--first] [--db PATH] [--json] [--limit N]"

// Shared options block (printed identically in every graph subcommand's help).
#define GRAPH_SELECTOR_OPTIONS                                                 \
  "  -h, --help            show this help message and exit\n"                  \
  "  --usr USR             exact clang USR\n"                                  \
  "  --id N                numeric symbol id\n"                                \
  "  --name FUZZY          fuzzy qualified-name match ('conf::set')\n"         \
  "  --kind {class,class-template,constructor,destructor,enum,enum-constant,"  \
  "function,function-template,macro,member,method,namespace,struct,type-alias,"\
  "typedef,union,variable}\n"                                                  \
  "                        restrict a --name match to one symbol kind\n"       \
  "  --first               if --name is ambiguous, take the closest match\n"   \
  "  --db PATH             index database to query (default: the standard cache\n"\
  "                        index)\n"                                           \
  "  --json                emit stable machine-readable JSON\n"                \
  "  --limit N             cap the number of results (default 50)\n"

const char kGraphCallersUsage[] =
    "usage: cidx graph callers [-h] " GRAPH_SELECTOR_USAGE_ARGS "\n";

const char kGraphCallersHelp[] =
    "usage: cidx graph callers [-h] " GRAPH_SELECTOR_USAGE_ARGS "\n"
    "\n"
    "options:\n"
    GRAPH_SELECTOR_OPTIONS;

const char kGraphCalleesUsage[] =
    "usage: cidx graph callees [-h] " GRAPH_SELECTOR_USAGE_ARGS "\n";

const char kGraphCalleesHelp[] =
    "usage: cidx graph callees [-h] " GRAPH_SELECTOR_USAGE_ARGS "\n"
    "\n"
    "options:\n"
    GRAPH_SELECTOR_OPTIONS;

// "usage: cidx graph refs [-h] " is 28 chars → continuation indent = 28 spaces
// (different from callers/callees which are 31 chars wide).
const char kGraphRefsUsage[] =
    "usage: cidx graph refs [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                       [--kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                       [--first] [--db PATH] [--json] [--limit N]\n";

const char kGraphRefsHelp[] =
    "usage: cidx graph refs [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                       [--kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                       [--first] [--db PATH] [--json] [--limit N]\n"
    "\n"
    "options:\n"
    GRAPH_SELECTOR_OPTIONS;

const char kGraphNeighborsUsage[] =
    "usage: cidx graph neighbors [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                            "
    "[--kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}]\n"
    "                            [--first] [--db PATH] [--json] [--limit N]\n"
    "                            [--edge KINDS] [--direction {in,out}]\n";

const char kGraphNeighborsHelp[] =
    "usage: cidx graph neighbors [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                            "
    "[--kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}]\n"
    "                            [--first] [--db PATH] [--json] [--limit N]\n"
    "                            [--edge KINDS] [--direction {in,out}]\n"
    "\n"
    "options:\n"
    GRAPH_SELECTOR_OPTIONS
    "  --edge KINDS          comma-separated edge kinds (calls, contains, "
    "field_of,\n"
    "                        inherits, instantiates, method_of, overrides,\n"
    "                        specializes, uses) (default: all)\n"
    "  --direction {in,out}  edge direction (default out)\n";

const char kGraphWalkUsage[] =
    "usage: cidx graph walk [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                       "
    "[--kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}]\n"
    "                       [--first] [--db PATH] [--json] [--limit N]\n"
    "                       [--edge KINDS] [--direction {in,out}] [--depth N]\n";

const char kGraphWalkHelp[] =
    "usage: cidx graph walk [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                       "
    "[--kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}]\n"
    "                       [--first] [--db PATH] [--json] [--limit N]\n"
    "                       [--edge KINDS] [--direction {in,out}] [--depth N]\n"
    "\n"
    "options:\n"
    GRAPH_SELECTOR_OPTIONS
    "  --edge KINDS          comma-separated edge kinds (calls, contains, "
    "field_of,\n"
    "                        inherits, instantiates, method_of, overrides,\n"
    "                        specializes, uses) (default: calls)\n"
    "  --direction {in,out}  edge direction (default out)\n"
    "  --depth N             max BFS depth (default 3)\n";

const char kGraphPathUsage[] =
    "usage: cidx graph path [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                       "
    "[--kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}]\n"
    "                       [--first] [--db PATH] [--json] [--limit N]\n"
    "                       (--to-usr USR | --to-id N | --to-name FUZZY)\n"
    "                       [--to-kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                       [--edge KINDS] [--direction {in,out}] [--depth N]\n";

const char kGraphPathHelp[] =
    "usage: cidx graph path [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                       "
    "[--kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}]\n"
    "                       [--first] [--db PATH] [--json] [--limit N]\n"
    "                       (--to-usr USR | --to-id N | --to-name FUZZY)\n"
    "                       [--to-kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                       [--edge KINDS] [--direction {in,out}] [--depth N]\n"
    "\n"
    "options:\n"
    GRAPH_SELECTOR_OPTIONS
    "  --to-usr USR          destination by USR\n"
    "  --to-id N             destination by id\n"
    "  --to-name FUZZY       destination by name\n"
    "  --to-kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}\n"
    "                        restrict a --to-name match to one symbol kind\n"
    "  --edge KINDS          comma-separated edge kinds (calls, contains, "
    "field_of,\n"
    "                        inherits, instantiates, method_of, overrides,\n"
    "                        specializes, uses) (default: calls)\n"
    "  --direction {in,out}  edge direction (default out)\n"
    "  --depth N             max search depth (default 8)\n";

const char kGraphHierarchyUsage[] =
    "usage: cidx graph hierarchy [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                            "
    "[--kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}]\n"
    "                            [--first] [--db PATH] [--json] [--limit N]\n"
    "                            [--transitive]\n"
    "                            [--access {public,protected,private,all}]\n";

const char kGraphHierarchyHelp[] =
    "usage: cidx graph hierarchy [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                            "
    "[--kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}]\n"
    "                            [--first] [--db PATH] [--json] [--limit N]\n"
    "                            [--transitive]\n"
    "                            [--access {public,protected,private,all}]\n"
    "\n"
    "options:\n"
    GRAPH_SELECTOR_OPTIONS
    "  --transitive          walk the whole inheritance tree, not just direct "
    "edges\n"
    "  --access {public,protected,private,all}\n"
    "                        filter members by C++ access specifier (default "
    "all)\n";

// "usage: cidx graph dispatch [-h] " is 32 chars → continuation indent = 27
// spaces (argparse aligns continuation under the first arg after the prog+opts).
const char kGraphDispatchUsage[] =
    "usage: cidx graph dispatch [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                           [--kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                           [--first] [--db PATH] [--json] [--limit N]\n";

const char kGraphDispatchHelp[] =
    "usage: cidx graph dispatch [-h] "
    "(--usr USR | --id N | --name FUZZY)\n"
    "                           [--kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                           [--first] [--db PATH] [--json] [--limit N]\n"
    "\n"
    "options:\n"
    GRAPH_SELECTOR_OPTIONS;

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
// ast sub-command usage / help (ADR-006 M5)
// ---------------------------------------------------------------------------

const char kAstUsage[] =
    "usage: cidx ast [-h] {dump,locals,conditions,cache} ...\n";

const char kAstHelp[] =
    "usage: cidx ast [-h] {dump,locals,conditions,cache} ...\n"
    "\n"
    "positional arguments:\n"
    "  {dump,locals,conditions,cache}\n"
    "    dump        dump the AST subtree of a symbol or file\n"
    "    locals      list a function's local variables\n"
    "    conditions  conditionals guarding a call, with their condition\n"
    "    cache       manage the on-disk AST cache\n"
    "\n"
    "options:\n"
    "  -h, --help   show this help message and exit\n";

const char kAstDumpUsage[] =
    "usage: cidx ast dump [-h] [--depth N] [--tokens] [--types] [--usr USR]\n"
    "                     [--id N] [--name FUZZY]\n"
    "                     [--kind {class,class-template,constructor,destructor,"
    "enum,enum-constant,function,function-template,macro,member,method,"
    "namespace,struct,type-alias,typedef,union,variable}]\n"
    "                     [--first] [--db PATH] [--json] [--cache | --no-cache]\n"
    "                     [FILE|COMPONENT://PATH] ...\n";

const char kAstDumpHelp[] =
    "usage: cidx ast dump [-h] [--depth N] [--tokens] [--types] [--usr USR]\n"
    "                     [--id N] [--name FUZZY]\n"
    "                     [--kind {class,class-template,constructor,destructor,"
    "enum,enum-constant,function,function-template,macro,member,method,"
    "namespace,struct,type-alias,typedef,union,variable}]\n"
    "                     [--first] [--db PATH] [--json] [--cache | --no-cache]\n"
    "                     [FILE|COMPONENT://PATH] ...\n"
    "\n"
    "positional arguments:\n"
    "  FILE|COMPONENT://PATH\n"
    "                        a source file, an indexed COMPONENT://PATH, or "
    "(with\n"
    "                        '-- <flags>') an ad-hoc file\n"
    "  -- FLAGS              ad-hoc compile flags after '--' for un-imported "
    "files\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --depth N             limit the dump to N levels (0 = unlimited)\n"
    "  --tokens              show each node's tokens\n"
    "  --types               annotate cursor types\n"
    "  --usr USR             exact clang USR\n"
    "  --id N                numeric symbol id\n"
    "  --name FUZZY          fuzzy qualified-name match (indexed), or an exact\n"
    "                        spelling to find in an ad-hoc file\n"
    "  --kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}\n"
    "                        restrict a --name match to one symbol kind\n"
    "  --first               if --name is ambiguous, take the closest match\n"
    "  --db PATH             index database to read (default: the standard "
    "index)\n"
    "  --json                emit machine-readable JSON\n"
    "  --cache               use the on-disk AST cache (default)\n"
    "  --no-cache            ignore the cache: always reparse (no cache read or\n"
    "                        write)\n";

const char kAstLocalsUsage[] =
    "usage: cidx ast locals [-h] [--params] [--usr USR] [--id N] [--name FUZZY]\n"
    "                       [--kind {class,class-template,constructor,destructor,"
    "enum,enum-constant,function,function-template,macro,member,method,namespace,"
    "struct,type-alias,typedef,union,variable}]\n"
    "                       [--first] [--db PATH] [--json] [--cache | --no-cache]\n"
    "                       [FILE|COMPONENT://PATH] ...\n";

const char kAstLocalsHelp[] =
    "usage: cidx ast locals [-h] [--params] [--usr USR] [--id N] [--name FUZZY]\n"
    "                       [--kind {class,class-template,constructor,destructor,"
    "enum,enum-constant,function,function-template,macro,member,method,namespace,"
    "struct,type-alias,typedef,union,variable}]\n"
    "                       [--first] [--db PATH] [--json] [--cache | --no-cache]\n"
    "                       [FILE|COMPONENT://PATH] ...\n"
    "\n"
    "positional arguments:\n"
    "  FILE|COMPONENT://PATH\n"
    "                        a source file, an indexed COMPONENT://PATH, or "
    "(with\n"
    "                        '-- <flags>') an ad-hoc file\n"
    "  -- FLAGS              ad-hoc compile flags after '--' for un-imported "
    "files\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --params              include parameters, not just body locals\n"
    "  --usr USR             exact clang USR\n"
    "  --id N                numeric symbol id\n"
    "  --name FUZZY          fuzzy qualified-name match (indexed), or an exact\n"
    "                        spelling to find in an ad-hoc file\n"
    "  --kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}\n"
    "                        restrict a --name match to one symbol kind\n"
    "  --first               if --name is ambiguous, take the closest match\n"
    "  --db PATH             index database to read (default: the standard "
    "index)\n"
    "  --json                emit machine-readable JSON\n"
    "  --cache               use the on-disk AST cache (default)\n"
    "  --no-cache            ignore the cache: always reparse (no cache read or\n"
    "                        write)\n";

const char kAstConditionsUsage[] =
    "usage: cidx ast conditions [-h] [--ast] [--usr USR] [--id N] [--name FUZZY]\n"
    "                           [--kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                           [--first] [--db PATH] [--json] [--cache |\n"
    "                           --no-cache]\n"
    "                           [FILE|COMPONENT://PATH] ...\n";

const char kAstConditionsHelp[] =
    "usage: cidx ast conditions [-h] [--ast] [--usr USR] [--id N] [--name FUZZY]\n"
    "                           [--kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                           [--first] [--db PATH] [--json] [--cache |\n"
    "                           --no-cache]\n"
    "                           [FILE|COMPONENT://PATH] ...\n"
    "\n"
    "positional arguments:\n"
    "  FILE|COMPONENT://PATH\n"
    "                        a source file, an indexed COMPONENT://PATH, or "
    "(with\n"
    "                        '-- <flags>') an ad-hoc file\n"
    "  -- FLAGS              ad-hoc compile flags after '--' for un-imported "
    "files\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --ast                 also emit the condition's AST subtree\n"
    "  --usr USR             exact clang USR\n"
    "  --id N                numeric symbol id\n"
    "  --name FUZZY          fuzzy qualified-name match (indexed), or an exact\n"
    "                        spelling to find in an ad-hoc file\n"
    "  --kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}\n"
    "                        restrict a --name match to one symbol kind\n"
    "  --first               if --name is ambiguous, take the closest match\n"
    "  --db PATH             index database to read (default: the standard "
    "index)\n"
    "  --json                emit machine-readable JSON\n"
    "  --cache               use the on-disk AST cache (default)\n"
    "  --no-cache            ignore the cache: always reparse (no cache read or\n"
    "                        write)\n";

const char kAstCacheUsage[] =
    "usage: cidx ast cache [-h] {build,status,clear} ...\n";

const char kAstCacheHelp[] =
    "usage: cidx ast cache [-h] {build,status,clear} ...\n"
    "\n"
    "positional arguments:\n"
    "  {build,status,clear}\n"
    "    build               parse + cache the target's AST (force-reparse)\n"
    "    status              list cache entries, sizes, validity\n"
    "    clear               remove cached AST(s) for a target, or all\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n";

const char kAstCacheBuildUsage[] =
    "usage: cidx ast cache build [-h] [--usr USR] [--id N] [--name FUZZY]\n"
    "                            [--kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                            [--first] [--db PATH] [--json]\n"
    "                            [FILE|COMPONENT://PATH] ...\n";

const char kAstCacheBuildHelp[] =
    "usage: cidx ast cache build [-h] [--usr USR] [--id N] [--name FUZZY]\n"
    "                            [--kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                            [--first] [--db PATH] [--json]\n"
    "                            [FILE|COMPONENT://PATH] ...\n"
    "\n"
    "positional arguments:\n"
    "  FILE|COMPONENT://PATH\n"
    "                        a source file, an indexed COMPONENT://PATH, or "
    "(with\n"
    "                        '-- <flags>') an ad-hoc file\n"
    "  -- FLAGS              ad-hoc compile flags after '--' for un-imported "
    "files\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --usr USR             exact clang USR\n"
    "  --id N                numeric symbol id\n"
    "  --name FUZZY          fuzzy qualified-name match (indexed), or an exact\n"
    "                        spelling to find in an ad-hoc file\n"
    "  --kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}\n"
    "                        restrict a --name match to one symbol kind\n"
    "  --first               if --name is ambiguous, take the closest match\n"
    "  --db PATH             index database to read (default: the standard "
    "index)\n"
    "  --json                emit machine-readable JSON\n";

// B6: per-action help constants so -h shows the correct subcommand name.
const char kAstCacheStatusHelp[] =
    "usage: cidx ast cache status [-h] [--usr USR] [--id N] [--name FUZZY]\n"
    "                             [--kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                             [--first] [--db PATH] [--json]\n"
    "                             [FILE|COMPONENT://PATH] ...\n"
    "\n"
    "positional arguments:\n"
    "  FILE|COMPONENT://PATH\n"
    "                        a source file, an indexed COMPONENT://PATH, or "
    "(with\n"
    "                        '-- <flags>') an ad-hoc file\n"
    "  -- FLAGS              ad-hoc compile flags after '--' for un-imported "
    "files\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --usr USR             exact clang USR\n"
    "  --id N                numeric symbol id\n"
    "  --name FUZZY          fuzzy qualified-name match (indexed), or an exact\n"
    "                        spelling to find in an ad-hoc file\n"
    "  --kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}\n"
    "                        restrict a --name match to one symbol kind\n"
    "  --first               if --name is ambiguous, take the closest match\n"
    "  --db PATH             index database to read (default: the standard "
    "index)\n"
    "  --json                emit machine-readable JSON\n";

const char kAstCacheClearHelp[] =
    "usage: cidx ast cache clear [-h] [--usr USR] [--id N] [--name FUZZY]\n"
    "                            [--kind {class,class-template,constructor,"
    "destructor,enum,enum-constant,function,function-template,macro,member,"
    "method,namespace,struct,type-alias,typedef,union,variable}]\n"
    "                            [--first] [--db PATH] [--json]\n"
    "                            [FILE|COMPONENT://PATH] ...\n"
    "\n"
    "positional arguments:\n"
    "  FILE|COMPONENT://PATH\n"
    "                        a source file, an indexed COMPONENT://PATH, or "
    "(with\n"
    "                        '-- <flags>') an ad-hoc file\n"
    "  -- FLAGS              ad-hoc compile flags after '--' for un-imported "
    "files\n"
    "\n"
    "options:\n"
    "  -h, --help            show this help message and exit\n"
    "  --usr USR             exact clang USR\n"
    "  --id N                numeric symbol id\n"
    "  --name FUZZY          fuzzy qualified-name match (indexed), or an exact\n"
    "                        spelling to find in an ad-hoc file\n"
    "  --kind {class,class-template,constructor,destructor,enum,enum-constant,"
    "function,function-template,macro,member,method,namespace,struct,type-alias,"
    "typedef,union,variable}\n"
    "                        restrict a --name match to one symbol kind\n"
    "  --first               if --name is ambiguous, take the closest match\n"
    "  --db PATH             index database to read (default: the standard "
    "index)\n"
    "  --json                emit machine-readable JSON\n";

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
    "init",  "add-source", "import",                "index",
    "resolve", "set",      "file",                  "dump-compile-commands",
    "search",  "show",     "list",                  "ls",
    "delete",  "graph",    "ast"};
const std::vector<std::string> kGraphWhats = {
    "callers", "callees", "refs", "neighbors", "walk", "path", "hierarchy",
    "dispatch"};
const std::vector<std::string> kAstWhats = {"dump", "locals", "conditions",
                                            "cache"};
const std::vector<std::string> kAstCacheWhats = {"build", "status", "clear"};
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
  // argparse.REMAINDER: once the fixed positionals are filled, every remaining
  // token (option-looking ones included) is captured verbatim into st.rest.
  bool remainder = false;
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
  // Exact match first (fast path; also avoids ambiguity when the token IS a
  // full option name, e.g. "--name" vs "--name-with-suffix").
  for (const OptSpec &o : spec.opts) {
    if (name == o.name) {
      return &o;
    }
  }
  // Unambiguous prefix match — mirrors Python argparse allow_abbrev=True.
  // Return the unique option whose long name starts with `name`; if zero or
  // two-or-more options match, return nullptr (treated as unrecognized).
  const std::size_t nlen = name.size();
  const OptSpec *match = nullptr;
  for (const OptSpec &o : spec.opts) {
    // o.name is const char*; use strncmp to check if it starts with `name`.
    if (std::strncmp(o.name, name.c_str(), nlen) == 0) {
      if (match != nullptr) {
        // Ambiguous: more than one option matches the prefix → unrecognized.
        return nullptr;
      }
      match = &o;
    }
  }
  return match;
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
    // argparse.REMAINDER: after the fixed positionals are filled, capture the
    // rest verbatim (including -flag-looking tokens) without option parsing.
    if (spec.remainder && st.positionals.size() >= spec.positionals.size()) {
      st.rest.push_back(tok);
      ++i;
      continue;
    }
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
  bool version = false;
};

CommandScan scan_command(const std::vector<std::string> &tokens, std::size_t i,
                         std::vector<std::string> &extras,
                         bool allow_version = false) {
  CommandScan out;
  const std::size_t n = tokens.size();
  while (i < n) {
    const std::string &tok = tokens[i];
    if (tok == "-h" || tok == "--help") {
      out.help = true;
      out.next = i + 1;
      return out;
    }
    // argparse's version action fires the instant `--version` is consumed
    // (before the required-subcommand check). Only the top parser registers
    // it, so sub-scans (show/list/delete) leave it to the extras path.
    if (allow_version && tok == "--version") {
      out.version = true;
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
        {"--no-graph", '\0', ValueKind::kNone, "--no-graph", nullptr, 0},
    },
    {},
    true, // files: nargs="*"
    {},
};

const Spec kResolveSpec = {
    "cidx resolve",
    kResolveUsage,
    kResolveHelp,
    {
        {"--rebuild", '\0', ValueKind::kNone, "--rebuild", nullptr, 0},
    },
    {},
    false,
    {},
};

const Spec kSetSpec = {
    "cidx set",
    kSetUsage,
    kSetHelp,
    {
        {"--component", 'c', ValueKind::kString, "--component/-c", nullptr, 0},
        {"--file", '\0', ValueKind::kString, "--file", nullptr, 0},
        {"--db", '\0', ValueKind::kString, "--db", nullptr, 0},
        {"--dry-run", '\0', ValueKind::kNone, "--dry-run", nullptr, 0},
    },
    {"FIELD=VALUE"}, // first positional; name reported by the required check
    true,            // nargs="+": collect surplus FIELD=VALUE tokens
    {"FIELD=VALUE"}, // required
};

const Spec kFileSpec = {
    "cidx file",
    kFileUsage,
    kFileHelp,
    {
        {"--db", '\0', ValueKind::kString, "--db", nullptr, 0},
    },
    {"COMPONENT://PATH"}, // the required target positional
    false,                // surplus is captured by the REMAINDER tail, not rest
    {"COMPONENT://PATH"}, // required
    {},                   // no required-mutex groups
    true,                 // nargs=REMAINDER: capture OP ... verbatim
};

const Spec kDumpCcSpec = {
    "cidx dump-compile-commands",
    kDumpCcUsage,
    kDumpCcHelp,
    {
        {"--db", '\0', ValueKind::kString, "--db", nullptr, 0},
    },
    {"COMPONENT"}, // the required component positional
    false,
    {"COMPONENT"}, // required
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

// -- graph leaf specs (M6) ---------------------------------------------------
// mutex group 1: --usr|--id|--name (required selector)
// mutex group 2: --to-usr|--to-id|--to-name (required path destination)
#define GRAPH_SELECTOR_OPTS                                                    \
  {"--usr", '\0', ValueKind::kString, "--usr", nullptr, 1},                   \
      {"--id", '\0', ValueKind::kInt, "--id", nullptr, 1},                    \
      {"--name", '\0', ValueKind::kString, "--name", nullptr, 1},             \
      {"--kind", '\0', ValueKind::kString, "--kind", &kSymbolKinds, 0},       \
      {"--first", '\0', ValueKind::kNone, "--first", nullptr, 0},             \
      {"--db", '\0', ValueKind::kString, "--db", nullptr, 0},                 \
      {"--json", '\0', ValueKind::kNone, "--json", nullptr, 0},               \
  {"--limit", '\0', ValueKind::kInt, "--limit", nullptr, 0}

const std::vector<std::string> kDirectionChoices = {"in", "out"};
const std::vector<std::string> kAccessChoices = {
    "public", "protected", "private", "all"};

const Spec kGraphCallersSpec = {
    "cidx graph callers",
    kGraphCallersUsage,
    kGraphCallersHelp,
    {GRAPH_SELECTOR_OPTS},
    {},
    false,
    {},
    {1}, // required mutex: one of --usr|--id|--name
};

const Spec kGraphCalleesSpec = {
    "cidx graph callees",
    kGraphCalleesUsage,
    kGraphCalleesHelp,
    {GRAPH_SELECTOR_OPTS},
    {},
    false,
    {},
    {1},
};

const Spec kGraphRefsSpec = {
    "cidx graph refs",
    kGraphRefsUsage,
    kGraphRefsHelp,
    {GRAPH_SELECTOR_OPTS},
    {},
    false,
    {},
    {1},
};

const Spec kGraphNeighborsSpec = {
    "cidx graph neighbors",
    kGraphNeighborsUsage,
    kGraphNeighborsHelp,
    {
        GRAPH_SELECTOR_OPTS,
        {"--edge", '\0', ValueKind::kString, "--edge", nullptr, 0},
        {"--direction", '\0', ValueKind::kString, "--direction",
         &kDirectionChoices, 0},
    },
    {},
    false,
    {},
    {1},
};

const Spec kGraphWalkSpec = {
    "cidx graph walk",
    kGraphWalkUsage,
    kGraphWalkHelp,
    {
        GRAPH_SELECTOR_OPTS,
        {"--edge", '\0', ValueKind::kString, "--edge", nullptr, 0},
        {"--direction", '\0', ValueKind::kString, "--direction",
         &kDirectionChoices, 0},
        {"--depth", '\0', ValueKind::kInt, "--depth", nullptr, 0},
    },
    {},
    false,
    {},
    {1},
};

const Spec kGraphPathSpec = {
    "cidx graph path",
    kGraphPathUsage,
    kGraphPathHelp,
    {
        GRAPH_SELECTOR_OPTS,
        {"--to-usr", '\0', ValueKind::kString, "--to-usr", nullptr, 2},
        {"--to-id", '\0', ValueKind::kInt, "--to-id", nullptr, 2},
        {"--to-name", '\0', ValueKind::kString, "--to-name", nullptr, 2},
        {"--to-kind", '\0', ValueKind::kString, "--to-kind", &kSymbolKinds, 0},
        {"--edge", '\0', ValueKind::kString, "--edge", nullptr, 0},
        {"--direction", '\0', ValueKind::kString, "--direction",
         &kDirectionChoices, 0},
        {"--depth", '\0', ValueKind::kInt, "--depth", nullptr, 0},
    },
    {},
    false,
    {},
    {1, 2}, // both selector and destination required
};

const Spec kGraphHierarchySpec = {
    "cidx graph hierarchy",
    kGraphHierarchyUsage,
    kGraphHierarchyHelp,
    {
        GRAPH_SELECTOR_OPTS,
        {"--transitive", '\0', ValueKind::kNone, "--transitive", nullptr, 0},
        {"--access", '\0', ValueKind::kString, "--access", &kAccessChoices, 0},
    },
    {},
    false,
    {},
    {1},
};

const Spec kGraphDispatchSpec = {
    "cidx graph dispatch",
    kGraphDispatchUsage,
    kGraphDispatchHelp,
    {GRAPH_SELECTOR_OPTS},
    {},
    false,
    {},
    {1},
};

// -- ast leaf specs (ADR-006 M5) --------------------------------------------
// Shared "common" options for all ast sub-commands (mirrors _ast_common).
// mutex group 2: --cache (kNone/true by default) vs --no-cache (kNone/false).
// Since both are kNone flags, we can't encode a default in the engine;
// use_cache = !st.flags.count("--no-cache") (always starts true).
#define AST_COMMON_OPTS                                                        \
  {"--usr", '\0', ValueKind::kString, "--usr", nullptr, 0},                   \
      {"--id", '\0', ValueKind::kInt, "--id", nullptr, 0},                    \
      {"--name", '\0', ValueKind::kString, "--name", nullptr, 0},             \
      {"--kind", '\0', ValueKind::kString, "--kind", &kSymbolKinds, 0},       \
      {"--first", '\0', ValueKind::kNone, "--first", nullptr, 0},             \
      {"--db", '\0', ValueKind::kString, "--db", nullptr, 0},                 \
  {"--json", '\0', ValueKind::kNone, "--json", nullptr, 0}

#define AST_CACHE_OPTS                                                         \
  {"--cache", '\0', ValueKind::kNone, "--cache", nullptr, 2},                 \
      {"--no-cache", '\0', ValueKind::kNone, "--no-cache", nullptr, 2}

const Spec kAstDumpSpec = {
    "cidx ast dump",
    kAstDumpUsage,
    kAstDumpHelp,
    {
        {"--depth", '\0', ValueKind::kInt, "--depth", nullptr, 0},
        {"--tokens", '\0', ValueKind::kNone, "--tokens", nullptr, 0},
        {"--types", '\0', ValueKind::kNone, "--types", nullptr, 0},
        AST_COMMON_OPTS,
        AST_CACHE_OPTS,
    },
    {"target"}, // optional positional (nargs=?)
    false,
    {}, // none required
    {}, // no required mutex
    true, // REMAINDER: captures -- and flags after target
};

const Spec kAstLocalsSpec = {
    "cidx ast locals",
    kAstLocalsUsage,
    kAstLocalsHelp,
    {
        {"--params", '\0', ValueKind::kNone, "--params", nullptr, 0},
        AST_COMMON_OPTS,
        AST_CACHE_OPTS,
    },
    {"target"},
    false,
    {},
    {},
    true,
};

const Spec kAstConditionsSpec = {
    "cidx ast conditions",
    kAstConditionsUsage,
    kAstConditionsHelp,
    {
        {"--ast", '\0', ValueKind::kNone, "--ast", nullptr, 0},
        AST_COMMON_OPTS,
        AST_CACHE_OPTS,
    },
    {"target"},
    false,
    {},
    {},
    true,
};

const Spec kAstCacheBuildSpec = {
    "cidx ast cache build",
    kAstCacheBuildUsage,
    kAstCacheBuildHelp,
    {AST_COMMON_OPTS},
    {"target"},
    false,
    {},
    {},
    true,
};

// status and clear have identical shape to build.
const Spec kAstCacheStatusSpec = {
    "cidx ast cache status", kAstCacheBuildUsage, kAstCacheBuildHelp,
    {AST_COMMON_OPTS},       {"target"},          false,
    {},                      {},                  true,
};
const Spec kAstCacheClearSpec = {
    "cidx ast cache clear", kAstCacheBuildUsage, kAstCacheBuildHelp,
    {AST_COMMON_OPTS},      {"target"},          false,
    {},                     {},                  true,
};

#undef AST_COMMON_OPTS
#undef AST_CACHE_OPTS

} // namespace

ParsedArgs parse_args(const std::vector<std::string> &argv) {
  std::vector<std::string> extras;
  ParsedArgs pa;

  CommandScan top = scan_command(argv, 0, extras, /*allow_version=*/true);
  if (top.help) {
    pa.help_text = kTopHelp;
    return pa;
  }
  if (top.version) {
    pa.version = true;
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
    pa.no_graph = st.flags.count("--no-graph") != 0;
  } else if (pa.command == "resolve") {
    ParseState st = parse_leaf(kResolveSpec, argv, i, extras);
    if (st.help) {
      pa.help_text = kResolveHelp;
      return pa;
    }
    pa.rebuild = st.flags.count("--rebuild") != 0;
  } else if (pa.command == "set") {
    ParseState st = parse_leaf(kSetSpec, argv, i, extras);
    if (st.help) {
      pa.help_text = kSetHelp;
      return pa;
    }
    // assignment = the fixed positional plus any surplus (nargs="+").
    pa.assignment.push_back(st.positionals[0]);
    for (const std::string &tok : st.rest) {
      pa.assignment.push_back(tok);
    }
    pa.component = opt_value(st, "--component");
    pa.file_filter = opt_value(st, "--file");
    pa.index_db = opt_value(st, "--db");
    pa.dry_run = st.flags.count("--dry-run") != 0;
  } else if (pa.command == "file") {
    ParseState st = parse_leaf(kFileSpec, argv, i, extras);
    if (st.help) {
      pa.help_text = kFileHelp;
      return pa;
    }
    pa.target = st.positionals[0];
    pa.op = st.rest; // REMAINDER tail: the operation + its args, verbatim
    pa.index_db = opt_value(st, "--db");
  } else if (pa.command == "dump-compile-commands") {
    ParseState st = parse_leaf(kDumpCcSpec, argv, i, extras);
    if (st.help) {
      pa.help_text = kDumpCcHelp;
      return pa;
    }
    pa.component = st.positionals[0];
    pa.index_db = opt_value(st, "--db");
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
  } else if (pa.command == "delete") {
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
  } else if (pa.command == "graph") {
    // -- graph sub-command (M6) -----------------------------------------------
    // Shared helper to fill graph selector fields from a ParseState.
    auto fill_graph_selector = [&](const ParseState &st) {
      pa.usr = opt_value(st, "--usr");
      if (const auto v = opt_value(st, "--id")) {
        long parsed = 0;
        parse_py_int(*v, parsed);
        pa.graph_id = static_cast<int64_t>(parsed);
      }
      pa.name = opt_value(st, "--name");
      pa.kind = opt_value(st, "--kind");
      pa.first = st.flags.count("--first") != 0;
      pa.index_db = opt_value(st, "--db");
      pa.graph_json = st.flags.count("--json") != 0;
      pa.graph_limit = int_value(st, "--limit", 50);
    };

    CommandScan what = scan_command(argv, i, extras);
    if (what.help) {
      pa.help_text = kGraphHelp;
      return pa;
    }
    if (!what.command) {
      fail(kGraphUsage, "cidx graph",
           "the following arguments are required: what");
    }
    if (!contains(kGraphWhats, *what.command)) {
      fail(kGraphUsage, "cidx graph",
           "argument what: invalid choice: '" + *what.command +
               "' (choose from " + join(kGraphWhats, ", ") + ")");
    }
    pa.what = *what.command;

    if (pa.what == "callers") {
      ParseState st = parse_leaf(kGraphCallersSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kGraphCallersHelp;
        return pa;
      }
      fill_graph_selector(st);
    } else if (pa.what == "callees") {
      ParseState st = parse_leaf(kGraphCalleesSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kGraphCalleesHelp;
        return pa;
      }
      fill_graph_selector(st);
    } else if (pa.what == "refs") {
      ParseState st = parse_leaf(kGraphRefsSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kGraphRefsHelp;
        return pa;
      }
      fill_graph_selector(st);
    } else if (pa.what == "neighbors") {
      ParseState st =
          parse_leaf(kGraphNeighborsSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kGraphNeighborsHelp;
        return pa;
      }
      fill_graph_selector(st);
      pa.edge = opt_value(st, "--edge");
      pa.direction = opt_value(st, "--direction").value_or("out");
    } else if (pa.what == "walk") {
      ParseState st = parse_leaf(kGraphWalkSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kGraphWalkHelp;
        return pa;
      }
      fill_graph_selector(st);
      pa.edge = opt_value(st, "--edge");
      pa.direction = opt_value(st, "--direction").value_or("out");
      pa.graph_depth = int_value(st, "--depth", 3);
    } else if (pa.what == "path") {
      ParseState st = parse_leaf(kGraphPathSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kGraphPathHelp;
        return pa;
      }
      fill_graph_selector(st);
      pa.to_usr = opt_value(st, "--to-usr");
      if (const auto v = opt_value(st, "--to-id")) {
        long parsed = 0;
        parse_py_int(*v, parsed);
        pa.to_id = static_cast<int64_t>(parsed);
      }
      pa.to_name = opt_value(st, "--to-name");
      pa.to_kind = opt_value(st, "--to-kind");
      pa.edge = opt_value(st, "--edge");
      pa.direction = opt_value(st, "--direction").value_or("out");
      pa.graph_depth = int_value(st, "--depth", 8);
    } else if (pa.what == "hierarchy") {
      ParseState st =
          parse_leaf(kGraphHierarchySpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kGraphHierarchyHelp;
        return pa;
      }
      fill_graph_selector(st);
      pa.transitive = st.flags.count("--transitive") != 0;
      pa.access = opt_value(st, "--access").value_or("all");
    } else { // dispatch
      ParseState st =
          parse_leaf(kGraphDispatchSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kGraphDispatchHelp;
        return pa;
      }
      fill_graph_selector(st);
    }
    // Apply abspath+expanduser to --db for graph (cli.py:1819)
    if (pa.index_db) {
      pa.index_db =
          pathutil::abspath(pathutil::expanduser(*pa.index_db));
    }
  } else if (pa.command == "ast") {
    // -- ast sub-command -------------------------------------------------------
    CommandScan what = scan_command(argv, i, extras);
    if (what.help) {
      pa.help_text = kAstHelp;
      return pa;
    }
    if (!what.command) {
      fail(kAstUsage, "cidx ast",
           "the following arguments are required: what");
    }
    if (!contains(kAstWhats, *what.command)) {
      fail(kAstUsage, "cidx ast",
           "argument what: invalid choice: '" + *what.command +
               "' (choose from " + join(kAstWhats, ", ") + ")");
    }
    pa.what = *what.command;

    // Shared lambda to populate the common ast fields from a ParseState.
    auto fill_ast_common = [&](const ParseState &st) {
      pa.ast_usr = opt_value(st, "--usr");
      if (const auto v = opt_value(st, "--id")) {
        long parsed = 0;
        parse_py_int(*v, parsed);
        pa.ast_id = static_cast<int64_t>(parsed);
      }
      pa.name = opt_value(st, "--name");
      pa.kind = opt_value(st, "--kind");
      pa.first = st.flags.count("--first") != 0;
      pa.index_db = opt_value(st, "--db");
      pa.ast_json = st.flags.count("--json") != 0;
      // target: first positional (optional).
      if (!st.positionals.empty()) {
        pa.target = st.positionals[0];
      }
      // rest: REMAINDER captures "-- flags..." verbatim.
      pa.rest = st.rest;
    };

    if (pa.what == "dump") {
      ParseState st = parse_leaf(kAstDumpSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kAstDumpHelp;
        return pa;
      }
      fill_ast_common(st);
      pa.depth = int_value(st, "--depth", 0);
      pa.tokens = st.flags.count("--tokens") != 0;
      pa.types = st.flags.count("--types") != 0;
      // --cache/--no-cache: default true; --no-cache overrides.
      pa.use_cache = st.flags.count("--no-cache") == 0;
    } else if (pa.what == "locals") {
      ParseState st = parse_leaf(kAstLocalsSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kAstLocalsHelp;
        return pa;
      }
      fill_ast_common(st);
      pa.params = st.flags.count("--params") != 0;
      pa.use_cache = st.flags.count("--no-cache") == 0;
    } else if (pa.what == "conditions") {
      ParseState st = parse_leaf(kAstConditionsSpec, argv, what.next, extras);
      if (st.help) {
        pa.help_text = kAstConditionsHelp;
        return pa;
      }
      fill_ast_common(st);
      pa.cond_ast = st.flags.count("--ast") != 0;
      pa.use_cache = st.flags.count("--no-cache") == 0;
    } else { // cache
      CommandScan csub = scan_command(argv, what.next, extras);
      if (csub.help) {
        pa.help_text = kAstCacheHelp;
        return pa;
      }
      if (!csub.command) {
        // B5: Python dest="cache_action" so the required-arg error says
        // "cache_action", not "what".
        fail(kAstCacheUsage, "cidx ast cache",
             "the following arguments are required: cache_action");
      }
      if (!contains(kAstCacheWhats, *csub.command)) {
        // B5: same dest name in the invalid-choice message.
        fail(kAstCacheUsage, "cidx ast cache",
             "argument cache_action: invalid choice: '" + *csub.command +
                 "' (choose from " + join(kAstCacheWhats, ", ") + ")");
      }
      pa.cache_action = *csub.command;
      const Spec &spec = (pa.cache_action == "build")    ? kAstCacheBuildSpec
                         : (pa.cache_action == "status") ? kAstCacheStatusSpec
                                                         : kAstCacheClearSpec;
      ParseState st = parse_leaf(spec, argv, csub.next, extras);
      if (st.help) {
        // B6: route help by action so `-h` shows the correct subcommand's usage.
        if (pa.cache_action == "status") {
          pa.help_text = kAstCacheStatusHelp;
        } else if (pa.cache_action == "clear") {
          pa.help_text = kAstCacheClearHelp;
        } else {
          pa.help_text = kAstCacheBuildHelp;
        }
        return pa;
      }
      fill_ast_common(st);
      // cache sub-commands have no --cache/--no-cache toggle (Python design).
    }
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

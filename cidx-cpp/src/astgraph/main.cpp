// cidx-astgraph entry point — dump one TU's libclang AST into <TU name>.db
// for Soufflé/Datalog reasoning (see astgraph.hpp for the schema contract).
//
// Configuration is SHARED with cidx: the source file's compile args + driver
// are read from the cidx index.db `file` row (same sanitize/resolve pipeline
// as `cidx index`), and the parse goes through the same Toolchain/Parser.
// Exit codes mirror cidx main.cpp: usage=2, any other error=1.
#include <sys/stat.h>

#include <algorithm>
#include <iostream>
#include <optional>
#include <string>
#include <vector>

#include "astgraph/astgraph.hpp"
#include "astgraph/souffle_runner.hpp"
#include "cli/args.hpp"     // kVersion
#include "cli/commands.hpp" // resolve_cache_dir()
#include "cli/json_out.hpp"
#include "clangx/parse.hpp"
#include "clangx/toolchain.hpp"
#include "compiledb/compiledb.hpp"
#include "storage/storage.hpp"
#include "util/errors.hpp"
#include "util/logger.hpp"
#include "util/pathutil.hpp"

namespace {

constexpr const char *kUsage =
    "usage: cidx-astgraph [-h] [--version] [--db PATH] [--out DIR] "
    "[--output PATH] [--main-only] SOURCE\n"
    "       cidx-astgraph analyze --rule callgraph [--jobs N] [--db PATH] "
    "[--out DIR] [--output PATH] [--main-only] SOURCE\n";

constexpr const char *kHelp =
    "Dump one translation unit's libclang AST into a per-TU SQLite graph DB\n"
    "(<basename(SOURCE)>.<identity>.db) for Datalog/Souffle reasoning.\n"
    "\n"
    "positional arguments:\n"
    "  SOURCE       source file of the TU; must be registered in the cidx\n"
    "               index (cidx import) so its compile args can be shared\n"
    "\n"
    "options:\n"
    "  -h, --help   show this help message and exit\n"
    "  --version    show the tool version and exit\n"
    "  --db PATH    cidx index.db to read compile args from\n"
    "               (default: <cache dir>/index.db)\n"
    "  --out DIR    directory for the output DB (default: current dir)\n"
    "  --output PATH\n"
    "               exact output DB path (atomically replaced on success)\n"
    "  --main-only  restrict the structural walk to main-file cursors;\n"
    "               header entities still appear when referenced\n"
    "\n"
    "analysis options:\n"
    "  analyze      run an embedded native Souffle rule after dumping\n"
    "  --rule NAME  native rule to run (currently: callgraph)\n"
    "  --jobs N     Souffle worker count (default: 1)\n";

bool file_exists(const std::string &path) {
  struct stat st{};
  return ::stat(path.c_str(), &st) == 0;
}

struct CliArgs {
  std::optional<std::string> index_db;
  std::optional<std::string> out_dir;
  std::optional<std::string> output_path;
  std::optional<std::string> rule;
  bool analyze = false;
  bool main_only = false;
  int jobs = 1;
  std::string source;
};

CliArgs parse_cli(const std::vector<std::string> &argv) {
  CliArgs out;
  for (std::size_t i = 0; i < argv.size(); ++i) {
    const std::string &a = argv[i];
    auto value = [&](const char *flag) -> std::string {
      if (i + 1 >= argv.size())
        throw cidx::UsageError(std::string(kUsage) + "cidx-astgraph: error: " +
                               flag + " expects a value\n");
      return argv[++i];
    };
    if (a == "--db") {
      out.index_db = value("--db");
    } else if (a == "--out") {
      out.out_dir = value("--out");
    } else if (a == "--output") {
      out.output_path = value("--output");
    } else if (a == "--rule") {
      if (out.rule)
        throw cidx::UsageError(std::string(kUsage) +
                               "cidx-astgraph: error: --rule specified more than once\n");
      out.rule = value("--rule");
    } else if (a == "--jobs") {
      const std::string n = value("--jobs");
      try {
        out.jobs = std::stoi(n);
      } catch (...) {
        throw cidx::UsageError(std::string(kUsage) +
                               "cidx-astgraph: error: --jobs expects an integer\n");
      }
      if (out.jobs < 1)
        throw cidx::UsageError(std::string(kUsage) +
                               "cidx-astgraph: error: --jobs must be at least 1\n");
    } else if (a == "--main-only") {
      out.main_only = true;
    } else if (a == "analyze") {
      if (out.analyze)
        throw cidx::UsageError(std::string(kUsage) +
                               "cidx-astgraph: error: analyze specified more than once\n");
      out.analyze = true;
    } else if (!a.empty() && a[0] == '-') {
      throw cidx::UsageError(std::string(kUsage) +
                             "cidx-astgraph: error: unknown option " + a +
                             "\n");
    } else if (out.source.empty()) {
      out.source = a;
    } else {
      throw cidx::UsageError(std::string(kUsage) +
                             "cidx-astgraph: error: exactly one SOURCE\n");
    }
  }
  if (out.source.empty())
    throw cidx::UsageError(std::string(kUsage) +
                           "cidx-astgraph: error: SOURCE is required\n");
  if (out.analyze && !out.rule)
    throw cidx::UsageError(std::string(kUsage) +
                           "cidx-astgraph: error: analyze requires --rule NAME\n");
  if (!out.analyze && out.rule)
    throw cidx::UsageError(std::string(kUsage) +
                           "cidx-astgraph: error: --rule requires analyze\n");
  return out;
}

cidx::json_out::Value callgraph_json(
    const std::string &source, const std::string &out_path,
    const std::vector<cidx::astgraph::CallFact> &calls) {
  using cidx::json_out::Array;
  using cidx::json_out::Object;
  using cidx::json_out::Value;
  Array rows;
  rows.reserve(calls.size());
  for (const auto &call : calls) {
    rows.push_back(Value::obj(Object{
        {"caller_node", Value::of(call.caller_node)},
        {"caller_usr", Value::of(call.caller_usr)},
        {"caller_name", Value::of(call.caller_name)},
        {"callee_node", Value::of(call.callee_node)},
        {"callee_usr", Value::of(call.callee_usr)},
        {"callee_name", Value::of(call.callee_name)},
        {"line", Value::of(call.line)},
    }));
  }
  return Value::obj(Object{
      {"rule", Value::of("callgraph")},
      {"source", Value::of(source)},
      {"ast_db", Value::of(out_path)},
      {"calls", Value::arr(std::move(rows))},
  });
}

} // namespace

int main(int argc, char **argv) {
  const std::vector<std::string> raw(argv + 1, argv + argc);
  for (const std::string &a : raw) {
    if (a == "-h" || a == "--help") {
      std::cout << kUsage << "\n" << kHelp;
      return 0;
    }
    if (a == "--version") {
      std::cout << "cidx-astgraph " << cidx::cli::kVersion << "\n";
      return 0;
    }
  }
  try {
    const CliArgs cli = parse_cli(raw);

    const std::string cache_dir = cidx::cli::resolve_cache_dir();
    const std::string index_path =
        cli.index_db ? *cli.index_db
                     : cidx::pathutil::join(cache_dir, "index.db");
    if (!file_exists(index_path))
      throw cidx::CidxError("cidx index not found at " + index_path +
                            " (run `cidx import` first, or pass --db)");
    // Same log sink as cidx: parse-failure flag dumps and toolchain probes
    // go to cidx.log, never the terminal.
    if (file_exists(cache_dir))
      cidx::Logger::root().set_file(
          cidx::pathutil::join(cache_dir, "cidx.log"));

    const std::string source = cidx::pathutil::abspath(cli.source);
    if (!file_exists(source))
      throw cidx::CidxError("source file not found: " + source);

    cidx::Storage db(index_path);
    const std::optional<cidx::File> rec = db.get_file(source);
    if (!rec)
      throw cidx::CidxError(
          source + " is not registered in the cidx index (" + index_path +
          "); run `cidx import <compile_commands.json>` for its project");

    // The exact `cidx index` options pipeline: re-sanitize stored args (G11),
    // then decode <label>/$VAR tokens against the index's aliases (v0.6.0).
    const std::vector<std::string> opts = cidx::CompileDb::resolve_options(
        cidx::CompileDb::sanitize(rec->compile_options
                                      ? *rec->compile_options
                                      : std::vector<std::string>{}),
        [&db](const std::string &n) { return db.get_alias(n); });

    cidx::Toolchain toolchain;
    cidx::Parser parser(toolchain);
    const cidx::ParsedTu tu = parser.parse(source, opts, rec->driver);

    cidx::astgraph::Options dump_opts;
    dump_opts.main_only = cli.main_only;
    const std::string default_name =
        cidx::pathutil::basename(source) + "." +
        cidx::astgraph::artifact_key(source, opts, rec->driver, dump_opts)
            .substr(0, 12) +
        ".db";
    const std::string out_path =
        cli.output_path
            ? cidx::pathutil::abspath(*cli.output_path)
            : cidx::pathutil::join(cli.out_dir ? *cli.out_dir : ".",
                                   default_name);
    const cidx::astgraph::DumpStats stats = cidx::astgraph::dump_tu(
        tu, out_path, dump_opts, source, opts, rec->driver);

    if (cli.analyze) {
      if (*cli.rule != "callgraph")
        throw cidx::CidxError("unsupported native rule: " + *cli.rule);
      const auto calls = cidx::astgraph::run_callgraph(out_path, cli.jobs);
      std::cout << cidx::json_out::dumps_indent2(
                       callgraph_json(source, out_path, calls))
                << "\n";
      return 0;
    }

    std::cout << out_path << ": " << stats.cursor_nodes << " cursor nodes, "
              << stats.type_nodes << " type nodes, " << stats.edges
              << " edges, " << stats.symbols << " symbols, " << stats.files
              << " files\n";
    return 0;
  } catch (const cidx::UsageError &e) {
    std::cerr << e.what();
    return e.exit_code();
  } catch (const cidx::CidxError &e) {
    std::cerr << "error: " << e.what() << "\n";
    return 1;
  } catch (const std::exception &e) {
    std::cerr << "error: " << e.what() << "\n";
    return 1;
  } catch (...) {
    return 1;
  }
}

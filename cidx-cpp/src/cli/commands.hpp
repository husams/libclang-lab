// Command handlers (design §5.9, §6.1-§6.3) — 1:1 port of the cmd_*
// functions in cli.py. Each handler returns the process exit code; output
// goes to Context streams so tests can capture full stdout/stderr strings.
//
// cmd_index (S08) runs the full §6.1 pipeline: target list (all pending via
// the md5-only skip, or resolved FILE args), per-file sanitize -> parse ->
// index_symbols -> index_headers -> mark_file_indexed -> per-file line,
// warning-count summary, exit 1 iff any file failed/unknown.
#pragma once

#include <ostream>
#include <string>

#include "cli/args.hpp"
#include "util/logger.hpp"

namespace cidx {
namespace cli {

// Cache-dir policy (analysis §1.3): $INDEXER_CACHE else ~/.cache/cidx,
// expanduser'd, NOT abspath'd (Python parity). All generated files live
// there: index.db + cidx.log, never the CWD.
std::string resolve_cache_dir();

struct Context {
  std::string cache_dir;       // resolved (caller created it, mkdir -p)
  std::string index_path;      // <cache_dir>/index.db
  Logger *logger = nullptr;    // Logger::root() in main; file sink lazy
  std::ostream *out = nullptr; // stdout
  std::ostream *err = nullptr; // stderr
};

int cmd_init(const ParsedArgs &args, Context &ctx);
int cmd_add_source(const ParsedArgs &args, Context &ctx);
int cmd_import(const ParsedArgs &args, Context &ctx);
int cmd_index(const ParsedArgs &args, Context &ctx);
int cmd_search(const ParsedArgs &args, Context &ctx);
int cmd_show_symbol(const ParsedArgs &args, Context &ctx);
int cmd_show_file(const ParsedArgs &args, Context &ctx);
int cmd_list_components(const ParsedArgs &args, Context &ctx);
int cmd_list_dirs(const ParsedArgs &args, Context &ctx);
int cmd_list_files(const ParsedArgs &args, Context &ctx);
int cmd_list_symbols(const ParsedArgs &args, Context &ctx);
int cmd_delete_component(const ParsedArgs &args, Context &ctx);
int cmd_delete_dir(const ParsedArgs &args, Context &ctx);
int cmd_delete_file(const ParsedArgs &args, Context &ctx);
int cmd_delete_symbol(const ParsedArgs &args, Context &ctx);
int cmd_file(const ParsedArgs &args, Context &ctx);
int cmd_dump_compile_commands(const ParsedArgs &args, Context &ctx);

// AST analysis commands (cidx ast dump|locals|conditions|cache …)
int cmd_ast_dump(const ParsedArgs &args, Context &ctx);
int cmd_ast_locals(const ParsedArgs &args, Context &ctx);
int cmd_ast_conditions(const ParsedArgs &args, Context &ctx);
int cmd_ast_cache(const ParsedArgs &args, Context &ctx);
int cmd_ast_cache_build(const ParsedArgs &args, Context &ctx);
int cmd_ast_cache_status(const ParsedArgs &args, Context &ctx);
int cmd_ast_cache_clear(const ParsedArgs &args, Context &ctx);

// Dispatch on args.command/args.what (args.help_text handled by the caller).
int run_command(const ParsedArgs &args, Context &ctx);

} // namespace cli
} // namespace cidx

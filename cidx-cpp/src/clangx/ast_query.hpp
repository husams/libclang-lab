// ast_query.hpp — on-demand AST walkers and emitters for `cidx ast` (M5).
// Byte-parity port of project/indexer/astcmd.py.
//
// Walk hierarchy:
//   for_file_cursors — pre-order, stops at function bodies (mirrors _file_cursors)
//   subtree          — full pre-order descent into bodies (cursor+depth+parent)
//
// JSON/text emission:
//   cursor_json, dump_text — mirrors _cursor_json / _dump_text
//
// Target resolution:
//   resolve_target — mirrors astcmd.resolve_target (all 3 target forms)
//
// Command handlers live in commands.cpp to keep this header library-neutral.
#pragma once

#include <cstdint>
#include <functional>
#include <optional>
#include <ostream>
#include <string>
#include <unordered_map>
#include <vector>

#include "clang-c/Index.h"

#include "astcache/astcache.hpp"
#include "cli/json_out.hpp"

namespace cidx {
namespace cli {
struct ParsedArgs;
struct Context;
} // namespace cli

// ---------------------------------------------------------------------------
// CursorKind classification sets (mirrors clang/ast.py)
// ---------------------------------------------------------------------------

// _FUNCTION_KINDS: cursor kinds that represent function-like entities.
// Used by cmd_locals / cmd_conditions to validate the focus cursor.
bool is_function_kind(CXCursorKind k);

// _COND_KINDS: statement kinds that represent control-flow conditionals.
// Used by cmd_conditions to find guarding statements.
bool is_cond_kind(CXCursorKind k);

// ---------------------------------------------------------------------------
// AST walkers
// ---------------------------------------------------------------------------

// Pre-order walk of top-level file cursors in `filename`, NOT descending into
// function bodies. Mirrors Python _file_cursors(tu, path).
// fn receives each cursor in order.
void for_file_cursors(CXTranslationUnit tu, const std::string &filename,
                      const std::function<void(CXCursor)> &fn);

// SubtreeNode: (cursor, depth, parent) yielded by subtree().
struct SubtreeNode {
  CXCursor cursor;
  int depth;
  CXCursor parent;
};

// Full pre-order walk descending into function bodies.
// Mirrors Python _subtree(cursor). Uses stashed children to work around
// the noexcept constraint on clang_visitChildren visitors.
std::vector<SubtreeNode> subtree(CXCursor root);

// ---------------------------------------------------------------------------
// Source location helper
// ---------------------------------------------------------------------------

// Python _loc(c): "basename:line:col" or "<no-location>" when file is null.
std::string cursor_loc(CXCursor c);

// ---------------------------------------------------------------------------
// Extent dict
// ---------------------------------------------------------------------------

// Python _extent_dict(c) → {"file":..., "start":[l,c], "end":[l,c]}.
json_out::Value extent_dict(CXCursor c);

// ---------------------------------------------------------------------------
// JSON / text emitters
// ---------------------------------------------------------------------------

// Python _cursor_json(c, depth, max_depth, want_tokens, want_types).
json_out::Value cursor_json(CXCursor c, int depth,
                            std::optional<int> max_depth, bool want_tokens,
                            bool want_types);

// Python _dump_text(c, depth, max_depth, want_tokens, want_types).
void dump_text(std::ostream &out, CXCursor c, int depth,
               std::optional<int> max_depth, bool want_tokens, bool want_types);

// ---------------------------------------------------------------------------
// Target resolution
// ---------------------------------------------------------------------------

// Python astcmd.resolve_target(args). Returns (AstTarget, exit_code).
// On failure: AstTarget is default-constructed (abspath empty), exit_code != 0.
// ctx.err receives the error message.
std::pair<std::optional<AstTarget>, int>
resolve_target(const cli::ParsedArgs &args, cli::Context &ctx);

// ---------------------------------------------------------------------------
// Command handlers declared here; implemented in commands.cpp
// ---------------------------------------------------------------------------

int cmd_ast_dump(const cli::ParsedArgs &args, cli::Context &ctx);
int cmd_ast_locals(const cli::ParsedArgs &args, cli::Context &ctx);
int cmd_ast_conditions(const cli::ParsedArgs &args, cli::Context &ctx);
int cmd_ast_cache(const cli::ParsedArgs &args, cli::Context &ctx);

} // namespace cidx

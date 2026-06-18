// graph/emit.hpp -- output emitters for the `graph` command group.
//
// Mirrors cli.py:_emit_edges/_emit_syms (cli.py:1054-1091).
// Both functions emit either human-readable tabular text or JSON (--json).
// All output goes to the provided ostream; callers pass *ctx.out.
#pragma once

#include <map>
#include <optional>
#include <ostream>
#include <string>
#include <vector>

#include "graph/query.hpp"
#include "graph/records.hpp"

namespace cidx {
namespace graph {

// emit_edges -- cli.py:1054-1069
// Emits a list of Edge results (human text or --json).
// R8: --json always re-queries sites via g.sites(e) (A8, limit 200); the
// eager e.sites are used for the text sample (1 site).
void emit_edges(GraphQuery &g, const std::vector<Edge> &edges, bool json_mode,
                std::ostream &out, const std::string &header);

// emit_syms -- cli.py:1072-1091
// Emits a list of Sym results (human text or --json).
// depths: optional {id -> depth} for walk output; when present each sym line
// gains "  d{n}" and JSON objects gain "depth" key last.
void emit_syms(const std::vector<Sym> &syms, bool json_mode, std::ostream &out,
               const std::string &header,
               const std::unordered_map<int64_t, int> *depths = nullptr);

} // namespace graph
} // namespace cidx

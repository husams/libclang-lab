// graph/query.cpp -- read-only graph traversal engine implementation.
//
// Mirrors indexer/query.py GraphQuery class (query.py:497-1393).
// All SQL is verbatim-equivalent to the Python reference; ORDER BY clauses and
// count-fallback logic (R2/R3) are copied exactly.
#include "graph/query.hpp"

#include <algorithm>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <vector>

#include "util/pathutil.hpp"

namespace cidx {
namespace graph {

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

GraphQuery::GraphQuery(Storage &db, std::string db_path)
    : db_(db), db_path_(std::move(db_path)) {}

GraphQuery GraphQuery::open(const std::string &db_path) {
  // Check existence before opening (mirrors query.py:509-516 NoIndexError).
  if (!std::filesystem::exists(db_path)) {
    throw NoIndexError(
        "no cidx index at " + format::py_repr_simple(db_path) +
        ". Build one with:\n"
        "    cd <repo> && cidx add-source --path . && cidx import "
        "--db <build> && cidx index && cidx resolve\n"
        "or pass --db PATH / set $INDEXER_CACHE.");
  }
  // We store the Storage by value internally via a unique_ptr pattern — but
  // GraphQuery::open is only used from command handlers that own the Storage.
  // The impl is: open() returns a GraphQuery that references a stack-local
  // Storage; commands.cpp owns the Storage and passes it by reference.
  // This overload is just a documentation stub — see commands.cpp _open_graph.
  throw std::logic_error("GraphQuery::open should not be called directly; "
                         "use the Storage& constructor from command handlers");
}

// ---------------------------------------------------------------------------
// Guards
// ---------------------------------------------------------------------------

int64_t GraphQuery::edge_count() { return db_.edge_count(); }

void GraphQuery::require_edges() {
  if (edge_count() == 0) {
    throw NoEdgesError(
        "index " + format::py_repr_simple(db_path_) +
        " has no graph edges -- it was built with "
        "`cidx index --no-graph`, or the graph was cleared. Re-run "
        "`cidx index` (without --no-graph) then `cidx resolve`.");
  }
}

bool GraphQuery::is_resolved() {
  if (!resolved_) {
    resolved_ = db_.graph_resolved();
  }
  return *resolved_;
}

// ---------------------------------------------------------------------------
// File cache: {file_id -> (abs_path, component_name)}
// Batch query mirrors query.py:_files() (query.py:587-604).
// ---------------------------------------------------------------------------

const std::unordered_map<int64_t, std::pair<std::string, std::optional<std::string>>> &
GraphQuery::files() {
  if (!file_cache_) {
    std::unordered_map<int64_t, std::pair<std::string, std::optional<std::string>>> cache;
    auto &raw = db_.raw_db();
    auto st = raw.prepare(
        "SELECT f.id AS fid, c.name AS cname, c.path AS root, "
        "       d.path AS rel, f.name AS name "
        "FROM file f JOIN directory d ON d.id = f.directory_id "
        "JOIN component c ON c.id = d.component_id");
    while (st.step()) {
      const int64_t fid = st.col_int64(0);
      const std::string cname = st.col_text(1);
      const std::string root = st.col_text(2);
      const std::string rel = st.col_text(3);
      const std::string name = st.col_text(4);
      std::string path;
      if (!rel.empty()) {
        path = pathutil::join(root, rel, name);
      } else {
        path = pathutil::join(root, name);
      }
      cache[fid] = {path, cname};
    }
    file_cache_ = std::move(cache);
  }
  return *file_cache_;
}

// ---------------------------------------------------------------------------
// Sym construction from storage rows
// ---------------------------------------------------------------------------

Sym GraphQuery::make_sym_from_symbol(const Symbol &sym) {
  const auto &fc = files();
  Sym s;
  s.id = sym.id;
  s.usr = sym.usr;
  s.spelling = sym.spelling;
  s.name = sym.qual_name ? *sym.qual_name : sym.spelling;
  s.kind = sym.kind;
  s.type_info = sym.type_info;
  s.is_definition = sym.is_definition;
  s.is_pure = sym.is_pure;
  s.is_static = sym.is_static;
  s.is_instantiation = sym.is_instantiation;
  s.access = sym.access;
  s.parent_usr = sym.parent_usr;
  s.resolved = sym.resolved;

  // Determine the best-known location (mirrors query.py:_sym, query.py:606-639)
  std::optional<int64_t> fid = sym.file_id;
  std::optional<int64_t> line = sym.line;
  std::optional<int64_t> col = sym.col;

  if (!fid) {
    // decl-only: fall back to decl site
    fid = sym.decl_file_id;
    line = sym.decl_line;
    col = sym.decl_col;
  }

  if (fid) {
    auto it = fc.find(*fid);
    if (it != fc.end()) {
      s.file = it->second.first;
      s.component = it->second.second;
    }
    // else: file_id doesn't exist in cache (unusual) -> file remains nullopt
    s.line = line;
    s.col = col;
    s.external = false;
  } else {
    // No registered location: may have decl_path (external/stub)
    s.file = sym.decl_path;
    s.line = sym.decl_line;
    s.col = sym.decl_col;
    s.external = sym.decl_path.has_value();
  }

  return s;
}

Sym GraphQuery::make_sym_from_row(const Storage::GraphEdgeRow &row) {
  // A6 row carries an embedded Symbol; reuse make_sym_from_symbol.
  return make_sym_from_symbol(row.sym);
}

// ---------------------------------------------------------------------------
// Site construction
// ---------------------------------------------------------------------------

Site GraphQuery::make_site(const Storage::EdgeSiteRow &row) {
  Site s;
  const auto &fc = files();
  if (row.file_id) {
    auto it = fc.find(*row.file_id);
    if (it != fc.end()) {
      s.file = it->second.first;
    }
  }
  s.line = row.line;
  s.col = row.col;
  s.conditional = row.conditional;
  s.args_sig = row.args_sig;
  s.recv_src_kind = row.recv_src_kind;
  s.recv_type_usr = row.recv_type_usr;
  s.recv_decl_usr = row.recv_decl_usr;
  s.recv_param_pos = row.recv_param_pos;
  s.recv_type_is_value = row.recv_type_is_value;
  return s;
}

// ---------------------------------------------------------------------------
// Symbol lookup
// ---------------------------------------------------------------------------

std::optional<Sym> GraphQuery::get_by_id(int64_t id) {
  auto sym = db_.graph_symbol_by_id(id);
  if (!sym) {
    return std::nullopt;
  }
  return make_sym_from_symbol(*sym);
}

std::optional<Sym> GraphQuery::get_by_usr(const std::string &usr) {
  auto sym = db_.graph_symbol_by_usr(usr);
  if (!sym) {
    return std::nullopt;
  }
  return make_sym_from_symbol(*sym);
}

std::vector<Sym> GraphQuery::find(const std::string &pattern,
                                  const std::optional<std::string> &kind,
                                  int limit) {
  auto syms = db_.find_symbols(pattern, kind, limit);
  std::vector<Sym> out;
  out.reserve(syms.size());
  for (auto &sym : syms) {
    out.push_back(make_sym_from_symbol(sym));
  }
  return out;
}

// ---------------------------------------------------------------------------
// Edge traversal
// ---------------------------------------------------------------------------

std::optional<std::vector<int64_t>>
GraphQuery::kind_ids(const std::optional<std::vector<std::string>> &kinds) {
  if (!kinds) {
    return std::nullopt;
  }
  const auto &km = edge_kinds_map();
  std::vector<int64_t> out;
  out.reserve(kinds->size());
  for (const std::string &k : *kinds) {
    auto it = km.find(k);
    if (it == km.end()) {
      // Build sorted valid list (matches query.py:652 sorted(EDGE_KINDS))
      std::vector<std::string> valid;
      valid.reserve(km.size());
      for (const auto &kv : km) {
        valid.push_back(kv.first);
      }
      std::sort(valid.begin(), valid.end());
      std::string msg = "unknown edge kind '" + k + "'; valid: [";
      for (std::size_t i = 0; i < valid.size(); ++i) {
        if (i != 0) {
          msg += ", ";
        }
        msg += "'" + valid[i] + "'";
      }
      msg += "]";
      throw std::invalid_argument(msg);
    }
    out.push_back(it->second);
  }
  if (out.empty()) {
    return std::nullopt;
  }
  return out;
}

std::vector<Edge>
GraphQuery::edges(int64_t sym_id, const std::string &direction,
                  const std::optional<std::vector<int64_t>> &kind_ids_opt,
                  int limit, bool with_sites) {
  const std::vector<int64_t> kv = kind_ids_opt ? *kind_ids_opt : std::vector<int64_t>{};
  const bool cr = is_resolved();
  auto rows = db_.graph_edges(sym_id, direction, kv, cr, limit);

  const auto &nm = edge_names_map();
  std::vector<Edge> out;
  out.reserve(rows.size());

  for (auto &row : rows) {
    Edge e;
    e.edge_id = row.eid;
    auto nit = nm.find(row.ekind);
    e.kind = (nit != nm.end()) ? nit->second : std::to_string(row.ekind);
    e.src_id = row.src_id;
    e.dst_id = row.dst_id;
    e.peer = make_sym_from_row(row);
    // count fallback (R3): 0 is falsy
    int64_t cnt = row.ecount;
    if (!cnt) {
      cnt = row.rawcount ? row.rawcount : 1;
    }
    e.count = cnt;
    e.base_access = row.base_access;
    e.is_virtual = row.is_virtual;
    out.push_back(std::move(e));
  }

  if (with_sites && !out.empty()) {
    std::vector<int64_t> eids;
    eids.reserve(out.size());
    for (const Edge &e : out) {
      eids.push_back(e.edge_id);
    }
    auto smap = sites_for(eids);
    for (Edge &e : out) {
      auto it = smap.find(e.edge_id);
      if (it != smap.end()) {
        e.sites = std::move(it->second);
      }
    }
  }
  return out;
}

std::vector<Edge>
GraphQuery::edges_in(int64_t sym_id,
                     const std::optional<std::vector<std::string>> &kinds,
                     int limit) {
  return edges(sym_id, "in", kind_ids(kinds), limit);
}

std::vector<Edge>
GraphQuery::edges_out(int64_t sym_id,
                      const std::optional<std::vector<std::string>> &kinds,
                      int limit) {
  return edges(sym_id, "out", kind_ids(kinds), limit);
}

std::vector<Edge> GraphQuery::references(int64_t sym_id, int limit) {
  return edges_in(sym_id, std::vector<std::string>{"calls", "uses"}, limit);
}

std::vector<Site> GraphQuery::sites(int64_t edge_id, int limit) {
  auto rows = db_.edge_sites_one(edge_id, limit);
  std::vector<Site> out;
  out.reserve(rows.size());
  for (const auto &row : rows) {
    out.push_back(make_site(row));
  }
  return out;
}

std::map<int64_t, std::vector<Site>>
GraphQuery::sites_for(const std::vector<int64_t> &edge_ids) {
  auto raw_map = db_.edge_sites_for(edge_ids);
  std::map<int64_t, std::vector<Site>> out;
  for (auto &kv : raw_map) {
    std::vector<Site> sv;
    sv.reserve(kv.second.size());
    for (const auto &row : kv.second) {
      sv.push_back(make_site(row));
    }
    out[kv.first] = std::move(sv);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

std::vector<Sym>
GraphQuery::peers(int64_t sym_id,
                  const std::optional<std::vector<std::string>> &kinds,
                  const std::string &direction, int limit) {
  auto edgs = edges(sym_id, direction, kind_ids(kinds), limit,
                    /*with_sites=*/false);
  std::vector<Sym> out;
  out.reserve(edgs.size());
  for (auto &e : edgs) {
    out.push_back(std::move(e.peer));
  }
  return out;
}

Traversal
GraphQuery::walk(int64_t start_id,
                 const std::optional<std::vector<std::string>> &kinds,
                 const std::string &direction, int depth, int max_nodes) {
  auto start_sym = get_by_id(start_id);
  if (!start_sym) {
    return Traversal{};
  }

  Traversal tr;
  tr.nodes_by_id[start_sym->id] = *start_sym;
  tr.depth_by_id[start_sym->id] = 0;
  tr.parent_by_id[start_sym->id] = std::nullopt;

  std::vector<int64_t> frontier = {start_sym->id};
  const auto kid_ids = kind_ids(kinds);

  for (int d = 1; d <= depth; ++d) {
    std::vector<int64_t> nxt;
    for (int64_t nid : frontier) {
      auto edgs = edges(nid, direction, kid_ids, max_nodes,
                        /*with_sites=*/false);
      for (const Edge &e : edgs) {
        if (tr.nodes_by_id.count(e.peer.id) == 0) {
          tr.nodes_by_id[e.peer.id] = e.peer;
          tr.depth_by_id[e.peer.id] = d;
          tr.parent_by_id[e.peer.id] = nid;
          nxt.push_back(e.peer.id);
          if (static_cast<int>(tr.nodes_by_id.size()) >= max_nodes) {
            return tr;
          }
        }
      }
    }
    if (nxt.empty()) {
      break;
    }
    frontier = std::move(nxt);
  }
  return tr;
}

std::optional<std::vector<Sym>>
GraphQuery::reaches(int64_t src_id, int64_t dst_id,
                    const std::optional<std::vector<std::string>> &kinds,
                    const std::string &direction, int max_depth) {
  auto s = get_by_id(src_id);
  auto t = get_by_id(dst_id);
  if (!s || !t) {
    return std::nullopt;
  }
  if (s->id == t->id) {
    return std::vector<Sym>{*s};
  }

  std::unordered_set<int64_t> seen;
  seen.insert(s->id);
  std::unordered_map<int64_t, int64_t> parent; // child -> parent id
  std::vector<int64_t> frontier = {s->id};
  const auto kid_ids = kind_ids(kinds);

  for (int iter = 0; iter < max_depth; ++iter) {
    std::vector<int64_t> nxt;
    for (int64_t nid : frontier) {
      auto ps = peers(nid, kinds, direction, 500);
      for (const Sym &peer : ps) {
        if (seen.count(peer.id)) {
          continue;
        }
        seen.insert(peer.id);
        parent[peer.id] = nid;
        if (peer.id == t->id) {
          // Reconstruct chain
          std::vector<int64_t> chain;
          chain.push_back(t->id);
          while (parent.count(chain.back())) {
            chain.push_back(parent.at(chain.back()));
          }
          std::reverse(chain.begin(), chain.end());
          std::vector<Sym> path;
          path.reserve(chain.size());
          for (int64_t cid : chain) {
            if (cid == s->id) {
              path.push_back(*s);
            } else if (cid == t->id) {
              path.push_back(*t);
            } else {
              auto sym = get_by_id(cid);
              if (sym) {
                path.push_back(*sym);
              }
            }
          }
          return path;
        }
        nxt.push_back(peer.id);
      }
    }
    if (nxt.empty()) {
      break;
    }
    frontier = std::move(nxt);
  }
  return std::nullopt;
}

// ---------------------------------------------------------------------------
// Hierarchy
// ---------------------------------------------------------------------------

std::vector<Sym> GraphQuery::bases(int64_t sym_id, bool direct) {
  if (direct) {
    return peers(sym_id, std::vector<std::string>{"inherits"}, "out");
  }
  auto tr = walk(sym_id, std::vector<std::string>{"inherits"}, "out", 16, 500);
  std::vector<Sym> out;
  for (const Sym &s : tr.nodes()) {
    if (s.id != sym_id) {
      out.push_back(s);
    }
  }
  return out;
}

std::vector<Sym> GraphQuery::subclasses(int64_t sym_id, bool direct) {
  if (direct) {
    return peers(sym_id, std::vector<std::string>{"inherits"}, "in");
  }
  auto tr = walk(sym_id, std::vector<std::string>{"inherits"}, "in", 16, 500);
  std::vector<Sym> out;
  for (const Sym &s : tr.nodes()) {
    if (s.id != sym_id) {
      out.push_back(s);
    }
  }
  return out;
}

std::vector<Sym>
GraphQuery::members(int64_t sym_id,
                    const std::optional<std::string> &access) {
  auto out_edgs = edges(sym_id, "out",
                        kind_ids(std::vector<std::string>{"contains"}), 500,
                        /*with_sites=*/false);
  auto in_edgs = edges(sym_id, "in",
                       kind_ids(std::vector<std::string>{"field_of", "method_of"}),
                       500, /*with_sites=*/false);

  std::unordered_set<int64_t> seen;
  std::vector<Sym> merged;

  auto add = [&](std::vector<Edge> &edgs) {
    for (auto &e : edgs) {
      if (!seen.count(e.peer.id)) {
        seen.insert(e.peer.id);
        merged.push_back(std::move(e.peer));
      }
    }
  };
  add(out_edgs);
  add(in_edgs);

  if (access && *access != "all") {
    std::vector<Sym> filtered;
    filtered.reserve(merged.size());
    for (auto &s : merged) {
      if (s.access && *s.access == *access) {
        filtered.push_back(std::move(s));
      }
    }
    return filtered;
  }
  return merged;
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

std::vector<Sym> GraphQuery::overrides_of(int64_t sym_id) {
  return peers(sym_id, std::vector<std::string>{"overrides"}, "out");
}

std::vector<Sym> GraphQuery::overridden_by(int64_t sym_id) {
  return peers(sym_id, std::vector<std::string>{"overrides"}, "in");
}

bool GraphQuery::is_virtual_method(int64_t sym_id) {
  auto m = get_by_id(sym_id);
  if (!m) {
    return false;
  }
  if (m->is_pure) {
    return true;
  }
  return !overridden_by(sym_id).empty() || !overrides_of(sym_id).empty();
}

std::vector<Sym> GraphQuery::dispatch_targets(int64_t sym_id) {
  auto root = get_by_id(sym_id);
  if (!root) {
    return {};
  }

  // Insertion-ordered vector + seen set (R4)
  std::vector<Sym> targets;
  std::unordered_set<int64_t> in_targets;
  std::unordered_set<int64_t> seen;

  if (!root->is_pure) {
    targets.push_back(*root);
    in_targets.insert(root->id);
  }
  seen.insert(root->id);
  std::vector<int64_t> frontier = {root->id};

  while (!frontier.empty()) {
    std::vector<int64_t> nxt;
    for (int64_t nid : frontier) {
      auto overriders = overridden_by(nid);
      for (const Sym &d : overriders) {
        if (seen.count(d.id)) {
          continue;
        }
        seen.insert(d.id);
        if (!d.is_pure) {
          targets.push_back(d);
          in_targets.insert(d.id);
        }
        nxt.push_back(d.id);
      }
    }
    frontier = std::move(nxt);
  }
  return targets;
}

// ---------------------------------------------------------------------------
// Internal namespace helpers (py_repr_simple for error messages)
// ---------------------------------------------------------------------------

namespace format {
std::string py_repr_simple(const std::string &s) {
  // Single-quote the string (Python repr() of a plain ASCII path).
  return "'" + s + "'";
}
} // namespace format

} // namespace graph
} // namespace cidx

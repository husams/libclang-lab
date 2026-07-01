// graph/records.hpp -- lightweight value types for the graph query layer.
//
// Sym/Edge/Site/Traversal mirror the Python query.py dataclasses (Sym/Edge/Site
// and Traversal). These are read-side only; writing uses storage/records.hpp.
//
// Key differences from storage::Symbol / storage::Edge / storage::EdgeSite:
//   - Sym carries the RESOLVED file path (not the raw file_id), component name,
//     and the `external` flag -- built from the file cache in GraphQuery.
//   - Edge carries the peer Sym (not just peer id) and a human kind string.
//   - Site carries the resolved file path, not the raw file_id.
//   - Traversal records BFS depth and parent for every reached node.
//
// to_dict() / loc() semantics are byte-identical to the Python counterparts
// (query.py:108-254, Traversal:1659-1697); key order is EXACT (R7).
#pragma once

#include <algorithm>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "cli/json_out.hpp"
#include "util/pathutil.hpp"

namespace cidx {
namespace graph {

// ---- Sym ------------------------------------------------------------------

struct Sym {
  int64_t id = -1;
  std::string usr;
  std::string spelling;
  std::string name;          // COALESCE(qual_name, spelling) -- displayed name
  std::string kind;
  std::optional<std::string> type_info;
  bool is_definition = false;
  bool is_pure = false;
  bool is_static = false;
  bool is_instantiation = false;
  std::optional<std::string> access;
  std::optional<std::string> parent_usr;
  bool resolved = false;
  std::optional<std::string> component;
  std::optional<std::string> file; // abs path of best-known location, or nullopt
  std::optional<int64_t> line;
  std::optional<int64_t> col;
  std::optional<int64_t> end_line; // v25: end of the symbol's own extent at
  std::optional<int64_t> end_col;  // (line, col); nullopt for decl-only / stubs
  bool external = false; // file is a raw path in an UNREGISTERED file

  // Python Sym.loc property (query.py:135-140)
  std::string loc() const {
    if (!file) {
      return "<no-location>";
    }
    const std::string base = pathutil::basename(*file);
    if (line && *line != 0) {
      return base + ":" + std::to_string(*line);
    }
    return base;
  }

  // Python Sym.span property: file:line-end_line, or nullopt when no end is
  // known -- the line range that slices the whole entity.
  std::optional<std::string> span() const {
    if (!file || !line || *line == 0 || !end_line || *end_line == 0) {
      return std::nullopt;
    }
    return pathutil::basename(*file) + ":" + std::to_string(*line) + "-" +
           std::to_string(*end_line);
  }

  // Python Sym.is_stub property (query.py:143-153)
  bool is_stub() const {
    return !resolved && (!file.has_value() || external);
  }

  // Python Sym.to_dict() -- key order EXACT (query.py:155-172, R7)
  json_out::Value to_dict() const {
    using namespace json_out;
    Object o;
    o.push_back({"id", Value::of(id)});
    o.push_back({"usr", Value::of(usr)});
    o.push_back({"spelling", Value::of(spelling)});
    o.push_back({"qual_name", Value::of(name)}); // COALESCE result
    o.push_back({"kind", Value::of(kind)});
    if (type_info) {
      o.push_back({"type_info", Value::of(*type_info)});
    } else {
      o.push_back({"type_info", Value::null()});
    }
    if (file) {
      o.push_back({"file", Value::of(*file)});
    } else {
      o.push_back({"file", Value::null()});
    }
    if (line) {
      o.push_back({"line", Value::of(*line)});
    } else {
      o.push_back({"line", Value::null()});
    }
    if (col) {
      o.push_back({"col", Value::of(*col)});
    } else {
      o.push_back({"col", Value::null()});
    }
    if (end_line) {
      o.push_back({"end_line", Value::of(*end_line)});
    } else {
      o.push_back({"end_line", Value::null()});
    }
    if (end_col) {
      o.push_back({"end_col", Value::of(*end_col)});
    } else {
      o.push_back({"end_col", Value::null()});
    }
    o.push_back({"is_definition", Value::of(is_definition)});
    o.push_back({"is_pure", Value::of(is_pure)});
    o.push_back({"is_static", Value::of(is_static)});
    o.push_back({"is_instantiation", Value::of(is_instantiation)});
    o.push_back({"is_stub", Value::of(is_stub())});
    return Value::obj(std::move(o));
  }
};

// ---- Site -----------------------------------------------------------------

struct Site {
  std::optional<std::string> file; // abs path (resolved from file cache)
  std::optional<int64_t> line;
  std::optional<int64_t> col;
  bool conditional = false;
  std::optional<std::string> args_sig;
  // Phase 2 provenance fields (present in DB, not serialized by graph output)
  std::optional<std::string> recv_src_kind;
  std::optional<std::string> recv_type_usr;
  std::optional<std::string> recv_decl_usr;
  std::optional<int64_t> recv_param_pos;
  std::optional<int64_t> recv_type_is_value;

  // Python Site.loc property (query.py:241-245)
  std::string loc() const {
    if (!file) {
      return "<no-location>";
    }
    const std::string base = pathutil::basename(*file);
    if (line && *line != 0) {
      return base + ":" + std::to_string(*line) + ":" +
             (col ? std::to_string(*col) : "");
    }
    return base;
  }

  // Python Site.to_dict() (query.py:247-254)
  json_out::Value to_dict() const {
    using namespace json_out;
    Object o;
    if (file) {
      o.push_back({"file", Value::of(*file)});
    } else {
      o.push_back({"file", Value::null()});
    }
    if (line) {
      o.push_back({"line", Value::of(*line)});
    } else {
      o.push_back({"line", Value::null()});
    }
    if (col) {
      o.push_back({"col", Value::of(*col)});
    } else {
      o.push_back({"col", Value::null()});
    }
    o.push_back({"conditional", Value::of(conditional)});
    if (args_sig) {
      o.push_back({"args_sig", Value::of(*args_sig)});
    } else {
      o.push_back({"args_sig", Value::null()});
    }
    return Value::obj(std::move(o));
  }
};

// ---- Edge -----------------------------------------------------------------

struct Edge {
  int64_t edge_id = -1;
  std::string kind; // edge_kind name (e.g. "calls")
  int64_t src_id = -1;
  int64_t dst_id = -1;
  Sym peer;         // the symbol at the other end
  int64_t count = 1;
  std::optional<int64_t> base_access; // inherits only
  std::optional<int64_t> is_virtual;  // inherits only (raw int)
  std::vector<Site> sites;            // eager-loaded reference sites

  // Python Edge.to_dict(sites) (query.py:196-216, R7)
  // `sites_override` is passed for --json re-query (R8).
  json_out::Value to_dict(const std::vector<Site> &sites_override) const {
    using namespace json_out;
    // Start with the peer's dict then append edge fields.
    Value pv = peer.to_dict();
    // pv is already an Object; extend it.
    pv.o.push_back({"edge_kind", Value::of(kind)});
    pv.o.push_back({"count", Value::of(count)});
    // base_access / is_virtual: only when non-null (R7 -- calls/uses MUST be absent)
    if (base_access) {
      pv.o.push_back({"base_access", Value::of(*base_access)});
    }
    if (is_virtual) {
      // is_virtual serialized as bool (R7)
      pv.o.push_back({"is_virtual", Value::of(static_cast<bool>(*is_virtual))});
    }
    Array sarr;
    for (const Site &s : sites_override) {
      sarr.push_back(s.to_dict());
    }
    pv.o.push_back({"sites", Value::arr(std::move(sarr))});
    return pv;
  }
};

// ---- Traversal ------------------------------------------------------------

struct Traversal {
  std::unordered_map<int64_t, Sym> nodes_by_id;
  std::unordered_map<int64_t, int> depth_by_id;
  std::unordered_map<int64_t, std::optional<int64_t>> parent_by_id;
  // BFS insertion order: ids in the order they were first discovered.
  // Required so that stable_sort by (depth, name) breaks same-key ties by
  // BFS discovery order (mirrors Python dict insertion order + sorted() stable).
  // Populated by GraphQuery::walk(); callers that build Traversal directly
  // should also append to this vector whenever they insert into nodes_by_id.
  std::vector<int64_t> insertion_order_;

  // Python Traversal.nodes property (query.py:1667-1673, R5):
  // stable_sort by (depth, name) where name = sym.name (COALESCE).
  // The initial vector is built in BFS insertion order so that stable_sort
  // preserves discovery order for same (depth, name) ties.
  std::vector<Sym> nodes() const {
    std::vector<Sym> out;
    out.reserve(insertion_order_.size());
    // Build in insertion order to get a deterministic stable_sort input.
    for (int64_t id : insertion_order_) {
      auto it = nodes_by_id.find(id);
      if (it != nodes_by_id.end()) {
        out.push_back(it->second);
      }
    }
    std::stable_sort(out.begin(), out.end(), [this](const Sym &a, const Sym &b) {
      const int da = depth_by_id.count(a.id) ? depth_by_id.at(a.id) : 0;
      const int db = depth_by_id.count(b.id) ? depth_by_id.at(b.id) : 0;
      if (da != db) {
        return da < db;
      }
      return a.name < b.name;
    });
    return out;
  }
};

} // namespace graph
} // namespace cidx

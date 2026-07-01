// graph/query.hpp -- read-only graph traversal engine over a cidx index.
//
// Mirrors Python indexer/query.py GraphQuery class (query.py:497-1393).
// Pure read path: no writes, no schema changes, no libclang at runtime.
// Opens the DB in read-write mode (via the existing Storage constructor which
// is the same file; graph reads go through the same SqliteDb handle to avoid
// requiring a separate read-only open).
//
// ADR-007: C++ graph port (M6). The query engine is a 1:1 port of query.py
// with the same SQL, the same traversal bounds, and byte-identical output.
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "graph/records.hpp"
#include "storage/storage.hpp"

namespace cidx {
namespace graph {

// Internal format helpers used in error message construction.
namespace format {
std::string py_repr_simple(const std::string &s);
} // namespace format

// ---- Error types ----------------------------------------------------------

class NoIndexError : public std::runtime_error {
public:
  explicit NoIndexError(const std::string &msg) : std::runtime_error(msg) {}
};

class NoEdgesError : public std::runtime_error {
public:
  explicit NoEdgesError(const std::string &msg) : std::runtime_error(msg) {}
};

// ---- EDGE_KINDS -----------------------------------------------------------
// edge_kind.id <-> name seeded identically by storage.py. Hard-coded to avoid
// a query and to validate any DB that disagrees.

inline const std::map<std::string, int64_t> &edge_kinds_map() {
  static const std::map<std::string, int64_t> m = {
      {"calls", 1},     {"inherits", 2}, {"contains", 3},
      {"specializes", 4}, {"instantiates", 5}, {"overrides", 6},
      {"uses", 7},      {"field_of", 8}, {"method_of", 9},
      // PR1 (v17): Layer-0 construction / destruction form edges
      {"construct-value", 10}, {"construct-temp", 11}, {"construct-heap", 12},
      {"construct-copy", 13}, {"construct-move", 14},
      {"factory-construct", 15}, {"destroy", 16},
      // PR2 (v17): Layer-0 friend declaration (rolled up to befriends)
      {"friend", 17},
      // Materialised virtual-dispatch caller edge (built by resolve)
      {"dispatch_calls", 18},
  };
  return m;
}

inline const std::map<int64_t, std::string> &edge_names_map() {
  static const std::map<int64_t, std::string> m = {
      {1, "calls"},     {2, "inherits"},     {3, "contains"},
      {4, "specializes"}, {5, "instantiates"}, {6, "overrides"},
      {7, "uses"},      {8, "field_of"},     {9, "method_of"},
      // PR1 (v17): Layer-0 construction / destruction form edges
      {10, "construct-value"}, {11, "construct-temp"}, {12, "construct-heap"},
      {13, "construct-copy"}, {14, "construct-move"},
      {15, "factory-construct"}, {16, "destroy"},
      // PR2 (v17): Layer-0 friend declaration (rolled up to befriends)
      {17, "friend"},
      // Materialised virtual-dispatch caller edge (built by resolve)
      {18, "dispatch_calls"},
  };
  return m;
}

// ---- GraphQuery -----------------------------------------------------------

class GraphQuery {
public:
  // Open or wrap an existing Storage. `db_path` is used only for error messages.
  explicit GraphQuery(Storage &db, std::string db_path = "");

  // Convenience: open from path (wraps a Storage opened at path).
  // Throws NoIndexError when the DB file does not exist.
  static GraphQuery open(const std::string &db_path);

  // Total number of edges. 0 means the graph layer is empty.
  int64_t edge_count();

  // Raise NoEdgesError unless edge_count() > 0.
  void require_edges();

  // ---- Symbol lookup -------------------------------------------------------

  std::optional<Sym> get_by_id(int64_t id);
  std::optional<Sym> get_by_usr(const std::string &usr);

  // Fuzzy qualified-name lookup (COALESCE(qual_name,spelling) LIKE pattern).
  // Mirrors query.py:find() (R1: uses find_symbols accessor, NOT search_symbols).
  std::vector<Sym> find(const std::string &pattern,
                        const std::optional<std::string> &kind = std::nullopt,
                        int limit = 50);

  // ---- Edge traversal ------------------------------------------------------

  // Incoming / outgoing edges of `kinds` (nullopt = all), up to `limit`.
  // with_sites=true: batch-load edge_site rows and attach them.
  std::vector<Edge> edges(int64_t sym_id, const std::string &direction,
                          const std::optional<std::vector<int64_t>> &kind_ids,
                          int limit, bool with_sites = true);

  std::vector<Edge> edges_in(int64_t sym_id,
                             const std::optional<std::vector<std::string>> &kinds,
                             int limit = 500);
  std::vector<Edge> edges_out(int64_t sym_id,
                              const std::optional<std::vector<std::string>> &kinds,
                              int limit = 500);

  // calls + uses inbound.
  std::vector<Edge> references(int64_t sym_id, int limit = 500);

  // Per-edge sites (A8, limit 200). Used by emitter for --json re-query (R8).
  std::vector<Site> sites(int64_t edge_id, int limit = 200);

  // ---- Navigation ----------------------------------------------------------

  // Internal: peer Syms with no site loading (BFS internal).
  std::vector<Sym> peers(int64_t sym_id,
                         const std::optional<std::vector<std::string>> &kinds,
                         const std::string &direction = "out", int limit = 500);

  // Parse kind spec string into kind_id vector. Throws std::invalid_argument on
  // unknown kind. Returns nullopt for null/empty (= all kinds).
  std::optional<std::vector<int64_t>>
  kind_ids(const std::optional<std::vector<std::string>> &kinds);

  // Bounded BFS (walk). Mirrors query.py:walk() (query.py:967-1003).
  Traversal walk(int64_t start_id,
                 const std::optional<std::vector<std::string>> &kinds,
                 const std::string &direction = "out", int depth = 3,
                 int max_nodes = 500);

  // Shortest path from src to dst. Mirrors query.py:reaches() (query.py:1005-1046).
  // Returns nullopt when unreachable.
  std::optional<std::vector<Sym>>
  reaches(int64_t src_id, int64_t dst_id,
          const std::optional<std::vector<std::string>> &kinds,
          const std::string &direction = "out", int max_depth = 8);

  // ---- Hierarchy -----------------------------------------------------------

  std::vector<Sym> bases(int64_t sym_id, bool direct = true);
  std::vector<Sym> subclasses(int64_t sym_id, bool direct = true);
  std::vector<Sym> members(int64_t sym_id,
                           const std::optional<std::string> &access = std::nullopt);

  // ---- Dispatch ------------------------------------------------------------

  std::vector<Sym> overrides_of(int64_t sym_id);    // outgoing overrides
  std::vector<Sym> overridden_by(int64_t sym_id);   // incoming overrides
  bool is_virtual_method(int64_t sym_id);
  // query.py:dispatch_targets (R4: insertion-ordered BFS, self first if !pure).
  std::vector<Sym> dispatch_targets(int64_t sym_id);

  // ---- Accessors -----------------------------------------------------------

  const std::string &db_path() const { return db_path_; }

private:
  Storage &db_;
  std::string db_path_;
  std::optional<bool> resolved_; // memoized _is_resolved
  std::optional<std::unordered_map<int64_t, std::pair<std::string, std::optional<std::string>>>>
      file_cache_; // {file_id -> (abs_path, component_name)}

  bool is_resolved();
  const std::unordered_map<int64_t, std::pair<std::string, std::optional<std::string>>> &
  files();

  Sym make_sym_from_row(const Storage::GraphEdgeRow &row);
  Sym make_sym_from_symbol(const Symbol &sym);

  // Batch-load sites for edge_ids.
  std::map<int64_t, std::vector<Site>>
  sites_for(const std::vector<int64_t> &edge_ids);

  // Resolve site file_id to abs path using the file cache.
  Site make_site(const Storage::EdgeSiteRow &row);
};

} // namespace graph
} // namespace cidx

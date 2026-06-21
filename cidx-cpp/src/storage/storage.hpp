// SQLite persistence layer — byte/semantics-compatible port of
// indexer/storage.py (schema v6, design §4/§5.3).
//
// Connection sequence (G19: migration BEFORE the schema script, because the
// schema's indexes reference migrated columns):
//   mkdir -p dirname(path)  [skipped for :memory:]
//   open -> PRAGMA foreign_keys = ON -> migrate() -> schema script
//
// Every public mutator commits unless inside a Transaction (the SQLite C API
// autocommits per statement, which is exactly Python's _commit()-unless-in-txn
// contract once Transaction issues an explicit BEGIN). The upsert SQL is
// ported character-for-character from storage.py — semantics frozen by
// tests/storage_smoke_test.cpp (the executable spec, G13/G14).
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <tuple>
#include <utility>
#include <vector>

#include "storage/records.hpp"
#include "storage/sqlite.hpp"

namespace cidx {

constexpr int kSchemaVersion = 17;

// Allowed symbol.kind values (storage.py SYMBOL_KINDS) — enforced by an
// application-side StorageError (§3.2). v16: kind is stored on disk as its
// CXCursorKind integer; these helpers convert name <-> stored int.
bool is_symbol_kind(std::string_view kind);
// name -> CXCursorKind int (-1 if unknown); int -> name (decimal string if
// unknown). Single source mirrored from storage.py SYMBOL_KIND_IDS.
int64_t symbol_kind_id(std::string_view name);
std::string symbol_kind_name(int64_t id);

class Storage;

// RAII transaction: BEGIN on construction, COMMIT on clean destruction,
// ROLLBACK when destroyed during exception unwind (Python _Transaction).
class Transaction {
public:
  explicit Transaction(Storage &db);
  ~Transaction();
  Transaction(const Transaction &) = delete;
  Transaction &operator=(const Transaction &) = delete;

  void commit();   // explicit early commit
  void rollback(); // explicit early rollback

private:
  Storage &db_;
  bool done_ = false;
  int uncaught_on_entry_;
};

class Storage {
public:
  explicit Storage(const std::string &path = ":memory:");

  // Batch many mutations into one commit (the documented 100x win):
  //   { auto txn = db.transaction(); ...; }   // commits at scope end
  Transaction transaction() { return Transaction(*this); }

  // -- components ------------------------------------------------------------
  int64_t add_component(const std::string &name, const std::string &path,
                        const std::string &kind = "repo",
                        const std::optional<std::string> &version =
                            std::nullopt);
  // Two-step lookup: first by stored BASE path, then by effective root.
  // Required because version-detection may split a trailing segment off the
  // registered path (see §2 hazard in portable_paths_contract.md).
  std::optional<Component> get_component(const std::string &path);
  std::optional<Component> get_component_by_name(const std::string &name);
  std::optional<Component> get_component_by_id(int64_t component_id);
  // Longest-prefix match computed app-side (G16); nested components resolve
  // to the deeper root. Uses resolved_root (base+version) for comparison.
  std::optional<Component> component_for_path(const std::string &abs_path);
  std::vector<Component>
  list_components(const std::optional<std::string> &name = std::nullopt,
                  const std::optional<std::string> &kind = std::nullopt);
  // Remove a component and everything derived from it: directories and files
  // via ON DELETE CASCADE, plus symbols indexed from those files (deleted
  // explicitly -- symbol file refs are ON DELETE SET NULL). For import --force.
  void delete_component(int64_t component_id);

  // v14: version management
  // Returns false when no component with that name exists.
  bool set_component_version(const std::string &name,
                             const std::optional<std::string> &version);
  // Stored effective root: version ? normpath(join(path, version)) : path.
  // NOT resolved (may contain $VAR). Static so callers can use it anywhere.
  static std::string effective_root(const Component &comp);

  // -- directories -----------------------------------------------------------
  int64_t add_directory(int64_t component_id, const std::string &path);
  std::optional<Directory> get_directory(int64_t component_id,
                                         const std::string &path);
  std::optional<Directory> get_directory_by_id(int64_t directory_id);
  // component.path / directory.path for a directory id, or nullopt.
  std::optional<std::string> directory_abs_path(int64_t directory_id);
  // Remove a directory, its files (ON DELETE CASCADE), and the symbols indexed
  // from those files (file refs are ON DELETE SET NULL, deleted explicitly).
  void delete_directory(int64_t directory_id);
  std::vector<std::pair<Directory, std::string>> // (row, component name)
  list_directories(const std::optional<int64_t> &component_id = std::nullopt,
                   const std::optional<std::string> &name = std::nullopt);

  // -- files -------------------------------------------------------------
  int64_t add_file(int64_t directory_id, const std::string &name,
                   const std::optional<double> &mtime = std::nullopt,
                   const std::optional<std::string> &md5 = std::nullopt,
                   const std::optional<std::vector<std::string>>
                       &compile_options = std::nullopt,
                   const std::optional<std::string> &driver = std::nullopt);
  // Throws StorageError when no component owns abs_path (add_component first).
  int64_t
  add_file_path(const std::string &abs_path,
                const std::optional<double> &mtime = std::nullopt,
                const std::optional<std::string> &md5 = std::nullopt,
                const std::optional<std::vector<std::string>> &compile_options =
                    std::nullopt,
                const std::optional<std::string> &driver = std::nullopt);
  std::optional<File> get_file(const std::string &abs_path);
  std::optional<File> get_file_by_id(int64_t file_id);
  std::optional<std::string> file_abs_path(int64_t file_id);
  // Remove a file and the symbols indexed from it (file refs are ON DELETE SET
  // NULL, so deleted explicitly to avoid file-less orphans).
  void delete_file(int64_t file_id);
  std::vector<std::pair<File, std::string>> // (row, reconstructed abs path)
  list_files(const std::optional<int64_t> &component_id = std::nullopt,
             const std::optional<std::string> &dir_path = std::nullopt,
             const std::optional<std::string> &name = std::nullopt,
             const std::optional<bool> &indexed = std::nullopt);
  void mark_file_indexed(int64_t file_id,
                         const std::optional<double> &mtime = std::nullopt);
  // Flip the indexed/pending flag in place; symbols are untouched.
  void set_file_indexed(int64_t file_id, bool indexed);
  // Replace a file's stored compile flags (and optionally its driver) and mark
  // it args_overridden=1 so a re-import (without --force) keeps the edit. Used
  // by `cidx file -set-flag/-unset-flag/-import-args`.
  void set_file_compile_options(
      int64_t file_id, const std::vector<std::string> &options,
      const std::optional<std::string> &driver = std::nullopt,
      bool update_driver = false);

  // Replace a file's stored compile flags WITHOUT setting args_overridden.
  // Used by `cidx realias`, which rewrites include paths to <label> tokens as
  // a portability transform (not a manual edit) — a later `import` should be
  // free to re-strip + re-alias these files.
  // Port of storage.py update_file_compile_options.
  void update_file_compile_options(int64_t file_id,
                                   const std::vector<std::string> &options);
  bool is_file_indexed(const std::string &abs_path,
                       const std::optional<double> &mtime = std::nullopt,
                       const std::optional<std::string> &md5 = std::nullopt);

  // -- diagnostics (v15) -------------------------------------------------
  // Replace a file's (TU's) stored parse diagnostics wholesale; called on
  // every (re)index so a now-clean file drops its stale rows. Rows are
  // inserted in order so their ids follow TU diagnostic order.
  void replace_diagnostics(int64_t file_id,
                           const std::vector<Diagnostic> &diags);
  // Stored parse diagnostics for a file, in insertion (TU) order.
  std::vector<Diagnostic> get_diagnostics(int64_t file_id);
  // Per-file diagnostic counts grouped by severity: {file_id: {severity: n}}.
  std::map<int64_t, std::map<int, int64_t>> diagnostic_counts();

  // -- symbols -----------------------------------------------------------
  // Upsert keyed by USR; throws StorageError on a bad kind. Definition wins
  // over a stored declaration; a declaration never downgrades a definition.
  int64_t add_symbol(const Symbol &sym);
  // Update named columns of the symbol with this USR; false when absent.
  // Throws StorageError on unknown columns or a bad kind value (smoke parity).
  bool
  update_symbol(const std::string &usr,
                const std::vector<std::pair<std::string, SqlValue>> &values);
  std::optional<Symbol> lookup_symbol(const std::string &usr);
  std::optional<Symbol> lookup_symbol_by_id(int64_t symbol_id);
  // Remove a single symbol row.
  void delete_symbol(int64_t symbol_id);
  std::vector<Symbol>
  lookup_symbols_by_name(const std::string &spelling,
                         const std::optional<std::string> &kind = std::nullopt);
  // Exact match on qual_name column; mirrors lookup_symbols_by_name but keyed
  // on qual_name instead of spelling. Used to recover a callee whose USR is
  // inconsistent (member function template in a dependent template body).
  std::vector<Symbol>
  lookup_symbols_by_qual_name(const std::string &qual_name,
                              const std::optional<std::string> &kind =
                                  std::nullopt);
  // '::'-segment fuzzy match on qual_name, ordered LENGTH(qual_name) first.
  std::vector<Symbol>
  search_symbols(const std::string &pattern,
                 const std::optional<std::string> &kind = std::nullopt);
  // Location scope matches definition OR declaration site (§3.5).
  std::vector<Symbol>
  list_symbols(const std::optional<int64_t> &component_id = std::nullopt,
               const std::optional<std::string> &dir_path = std::nullopt,
               const std::optional<int64_t> &file_id = std::nullopt,
               const std::optional<std::string> &name = std::nullopt,
               const std::optional<std::string> &kind = std::nullopt);
  std::vector<Symbol> symbols_in_file(int64_t file_id);
  std::vector<Symbol> unresolved_symbols();

  // -- graph layer (v7) ------------------------------------------------------
  // Mint a stub symbol row (resolved=0, kind='function') for an unknown USR.
  // The reference cursor is always in hand at the call site, so its name
  // travels with the USR: a stub is born NAMED -- essential for targets whose
  // definition is never indexed (stdlib calls, implicit template
  // instantiations, defaulted ctors), where no add_symbol ever backfills it.
  // The reference cursor's declaration location travels too: when it sits in an
  // indexed file the stub is born LOCATED (e.g. a defaulted ctor anchored to its
  // `struct` line), so chain::D::D resolves to chain.hpp:25 instead of
  // `@<no-location>`. decl_file_id is nullopt for targets in unregistered
  // (system/stdlib) headers, which correctly stay location-less.
  // An existing real row is kept intact; a repeat mint only UPGRADES an empty
  // name, never clobbers a real one, and fills the location only when still
  // absent. Returns the stable symbol.id either way.
  int64_t mint_symbol_id(const std::string &usr,
                         const std::string &spelling = "",
                         const std::string &qual_name = "",
                         const std::string &display_name = "",
                         const std::string &kind = "function",
                         const std::optional<int64_t> &decl_file_id =
                             std::nullopt,
                         const std::optional<int64_t> &decl_line = std::nullopt,
                         const std::optional<int64_t> &decl_col = std::nullopt,
                         const std::optional<std::string> &decl_path =
                             std::nullopt,
                         bool is_instantiation = false);

  // UNIQUE upsert on (src_id, dst_id, kind); increments count on conflict.
  // Returns the edge.id for edge_site linkage.
  int64_t add_edge(const Edge &e);

  // INSERT OR IGNORE: same site visited twice (e.g. re-parse) = no-op.
  void add_edge_site(const EdgeSite &s);

  // INSERT OR IGNORE a call_arg row (PK collision = same arg, harmless).
  void add_call_arg(const CallArg &a);

  // INSERT OR REPLACE keyed on (owner_id, position).
  void add_template_param(const TemplateParam &p);
  void add_template_arg(const TemplateArg &a);

  // Delete edges whose src is a symbol defined in this file (idempotent
  // re-index: edges cascade-delete their edge_site rows).
  void delete_edges_for_file(int64_t file_id);

  // -- entity_edge (v17) -------------------------------------------------------
  // Upsert an entity_edge row (idempotent re-materialise safe).
  void add_entity_edge(int64_t src_id, int64_t dst_id, int64_t kind,
                       int64_t count = 1,
                       std::optional<int64_t> via_member_id = std::nullopt,
                       int64_t multiplicity = 1, int64_t access = 0,
                       int64_t is_virtual = 0,
                       std::optional<int64_t> create_form = std::nullopt,
                       int64_t partial = 0);
  // Delete all entity_edge rows (pre-step for idempotent re-materialise).
  void clear_entity_edges();
  // Materialise all 11 entity relation kinds from the Layer-0 graph.
  // Called by resolve_pass() after rollup_edge_counts(). Pure DB pass.
  void materialise_entity_edges();

  // Resolve pass (DB-only, no parse): roll up edge.count from edge_site for
  // calls/uses, report remaining stubs. Returns count of still-unresolved
  // stub symbols.
  int resolve_pass();

  // Roll edge.count up to the true site count for calls (kind=1) and uses
  // (kind=7) — idempotent; COUNT(*) is the source of truth.
  void rollup_edge_counts();

  // Edges whose ends live in different components.
  std::vector<Edge> cross_repo_edges();

  Stats stats();

  // -- graph read-only accessors (M6 — query.py parity) ----------------------
  // A1: total edge count (query.py:558)
  int64_t edge_count();

  // A2: true once `cidx resolve` has rolled up edge counts (query.py:579-583)
  bool graph_resolved();

  // A3/A4: fetch one symbol by USR / numeric id (query.py:666-668)
  std::optional<Symbol> graph_symbol_by_usr(const std::string &usr);
  std::optional<Symbol> graph_symbol_by_id(int64_t id);

  // A5: fuzzy COALESCE(qual_name,spelling) lookup (query.py:707-738, R1).
  // Escapes ONLY % and _ (matching query.py:719 -- NOT storage escape_like).
  std::vector<Symbol> find_symbols(const std::string &pattern,
                                   const std::optional<std::string> &kind,
                                   int limit);

  // A6 result row: 8 edge columns + decoded symbol-from-offset (plan §A6).
  struct GraphEdgeRow {
    int64_t eid = -1;
    int64_t src_id = -1;
    int64_t dst_id = -1;
    int64_t ekind = 0;
    int64_t ecount = 0;
    int64_t rawcount = 0;
    std::optional<int64_t> base_access;
    std::optional<int64_t> is_virtual;
    Symbol sym; // decoded from cols 8..29 via symbol_from_offset
  };

  // A7 result row for batch site loading.
  struct EdgeSiteRow {
    int64_t edge_id = -1;
    std::optional<int64_t> file_id;
    std::optional<int64_t> line;
    std::optional<int64_t> col;
    bool conditional = false;
    std::optional<std::string> args_sig;
    std::optional<std::string> recv_src_kind;
    std::optional<std::string> recv_type_usr;
    std::optional<std::string> recv_decl_usr;
    std::optional<int64_t> recv_param_pos;
    std::optional<int64_t> recv_type_is_value;
  };

  // A6: typed-edge query (query.py:782-813)
  // direction "in"|"out"; kind_ids empty => no kind filter; count_resolved
  // controls which count expression is used (A6 plan §count_expr).
  std::vector<GraphEdgeRow>
  graph_edges(int64_t mine_id, const std::string &direction,
              const std::vector<int64_t> &kind_ids, bool count_resolved,
              int limit);

  // A7: batch-load edge_site rows for many edge_ids (query.py:839-870)
  std::map<int64_t, std::vector<EdgeSiteRow>>
  edge_sites_for(const std::vector<int64_t> &edge_ids);

  // A8: single-edge sites with LIMIT (query.py:884-906)
  std::vector<EdgeSiteRow> edge_sites_one(int64_t edge_id, int limit);

  // -- labels (v14) ----------------------------------------------------------
  // Upsert on name; returns the row id.
  int64_t add_label(const std::string &name, const std::string &path);
  // Returns false when name absent.
  bool remove_label(const std::string &name);
  // Returns stored path or nullopt when absent.
  std::optional<std::string> get_label(const std::string &name);
  // Sorted by name; returns (name, stored path) pairs.
  std::vector<std::pair<std::string, std::string>> list_labels();
  // Version-agnostic component alias map: name -> (base, max_version,
  // bumpable). Effective root is split into (base, version); rows grouped by
  // name; a name is kept only when all rows share ONE base (else ambiguous).
  // max_version = "" when none. bumpable = true only for a single row whose
  // stored path carries no embedded version (safe to set_component_version).
  // (Python Storage.component_alias_index.)
  std::map<std::string, std::tuple<std::string, std::string, bool>>
  component_alias_index();
  // Encode registry for include-path aliasing as (name, match_path, versioned)
  // triples: explicit labels (exact) PLUS components (version-stripped base,
  // version-agnostic). Labels win on a name collision; components with
  // conflicting bases are skipped. Decode mirror = get_alias. Sorted by name.
  // (Python Storage.list_alias_pairs.)
  std::vector<std::tuple<std::string, std::string, bool>> list_alias_pairs();
  // Decode an alias name: explicit label -> stored path; else a uniquely-based
  // component -> base joined with its highest version; nullopt otherwise.
  // (Python Storage.get_alias.)
  std::optional<std::string> get_alias(const std::string &name);

  // Raw connection — exposed for tests (schema assertions on :memory: DBs)
  // and future maintenance commands. Not part of the indexing flow.
  SqliteDb &raw_db() { return db_; }

  // %c%c% char-in-order LIKE pattern with '\ % _' escaping (G18); public
  // statics so fuzzy_match_test can pin them directly.
  static std::string fuzzy_like(std::string_view text);
  // WHERE fragment matching a directory and its whole subtree; root '' -> '%'
  // (G17). Appends the two LIKE args to `args`.
  static std::string dir_scope_sql(const std::string &dir_path,
                                   std::vector<SqlValue> &args);

private:
  friend class Transaction;

  void migrate(); // column-presence detection, §4.1
  void migrate_symbol_kind_to_int(); // v15 -> v16: rebuild symbol, kind->int
  // (component_id, relative dir, file name) for an absolute path; nullopt
  // when no component owns it.
  std::optional<std::tuple<int64_t, std::string, std::string>>
  split_path(const std::string &abs_path);

  SqliteDb db_;
  bool in_txn_ = false;
};

} // namespace cidx

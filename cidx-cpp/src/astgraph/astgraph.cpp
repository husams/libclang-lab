// astgraph.cpp — see astgraph.hpp for the schema/design contract.
//
// Shape of the dump:
//   1. intern the TU root cursor, then clang_visitChildren walks the tree
//      structurally (child edges, ord = sibling position).  --main-only
//      prunes header SUBTREES at the walk level only.
//   2. every interned cursor/type is queued; the drain loop emits its
//      cross-reference edges (references/definition/.../has_type and the
//      CXType graph).  Cross-refs INTERN their targets, so header decls
//      referenced from the main file appear as shallow nodes even under
//      --main-only.  Worklists (not recursion) keep the C++ stack flat.
//   3. everything runs inside one transaction; ids are handed out by plain
//      counters so 0 stays the universal "none" sentinel.
//
// libclang is called through the linked symbols directly (Amendment A1: the
// dlopen shim is gone; the LibClang facade exists only so PRE-A1 call sites
// compile unchanged — new code may call ::clang_* as the facade itself does).
#include "astgraph/astgraph.hpp"

#include <cstdint>
#include <cstdio>
#include <deque>
#include <exception>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "clang-c/Index.h"

#include "cli/args.hpp"       // kVersion (meta.generator provenance)
#include "cli/kind_names.hpp" // Python CursorKind.name table (cidx parity)
#include "storage/sqlite.hpp"
#include "util/errors.hpp"
#include "util/json_min.hpp"

namespace cidx {
namespace astgraph {
namespace {

// The <TU>.db DDL.  Every column is NOT NULL with a 0/'' sentinel — Soufflé's
// sqlite IO reads these tables verbatim and has no notion of NULL.  The edge
// UNIQUE clause makes re-emission (same cross-ref reachable twice) a no-op.
constexpr const char *kSchemaSql = R"sql(
PRAGMA journal_mode = MEMORY;
PRAGMA synchronous = OFF;
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE file (
  id      INTEGER PRIMARY KEY,
  path    TEXT NOT NULL UNIQUE,
  is_main INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE node_kind (
  id       INTEGER PRIMARY KEY,   -- CXCursorKind, or 1000+CXTypeKind
  name     TEXT NOT NULL,
  category TEXT NOT NULL          -- decl|ref|expr|stmt|attr|preproc|tu|other|type
);
CREATE TABLE relation_kind (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);
CREATE TABLE symbol (
  id      INTEGER PRIMARY KEY,
  usr     TEXT NOT NULL UNIQUE,   -- joins cidx index.db symbol.usr
  name    TEXT NOT NULL,
  kind_id INTEGER NOT NULL,
  linkage INTEGER NOT NULL DEFAULT 0  -- CXLinkageKind
);
CREATE TABLE node (
  id            INTEGER PRIMARY KEY,
  kind_id       INTEGER NOT NULL REFERENCES node_kind(id),
  symbol_id     INTEGER NOT NULL DEFAULT 0,  -- 0 = no USR
  type_id       INTEGER NOT NULL DEFAULT 0,  -- clang_getCursorType (0 = none
                                             -- or this row IS a type node)
  spelling      TEXT NOT NULL DEFAULT '',
  file_id       INTEGER NOT NULL DEFAULT 0,  -- 0 = no location (type nodes)
  line          INTEGER NOT NULL DEFAULT 0,
  col           INTEGER NOT NULL DEFAULT 0,
  end_line      INTEGER NOT NULL DEFAULT 0,
  end_col       INTEGER NOT NULL DEFAULT 0,
  is_definition INTEGER NOT NULL DEFAULT 0,
  access        INTEGER NOT NULL DEFAULT 0,  -- CX_CXXAccessSpecifier
  is_const      INTEGER NOT NULL DEFAULT 0,  -- type nodes only
  is_volatile   INTEGER NOT NULL DEFAULT 0,
  is_restrict   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE edge (
  src_id INTEGER NOT NULL,
  dst_id INTEGER NOT NULL,
  rel_id INTEGER NOT NULL REFERENCES relation_kind(id),
  ord    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (src_id, dst_id, rel_id, ord) ON CONFLICT IGNORE
);
CREATE INDEX idx_edge_src ON edge(src_id, rel_id);
CREATE INDEX idx_edge_dst ON edge(dst_id, rel_id);
CREATE INDEX idx_node_kind ON node(kind_id);
CREATE INDEX idx_node_symbol ON node(symbol_id);
)sql";

struct RelName {
  int id;
  const char *name;
};
constexpr RelName kRelNames[] = {
    {kRelChild, "child"},
    {kRelReferences, "references"},
    {kRelDefinition, "definition"},
    {kRelCanonical, "canonical"},
    {kRelSemanticParent, "semantic_parent"},
    {kRelLexicalParent, "lexical_parent"},
    {kRelSpecializes, "specializes"},
    {kRelOverrides, "overrides"},
    {kRelTypeDecl, "type_decl"},
    {kRelCanonicalType, "canonical_type"},
    {kRelPointee, "pointee"},
    {kRelElementType, "element_type"},
    {kRelResultType, "result_type"},
    {kRelArgType, "arg_type"},
    {kRelNamedType, "named_type"},
    {kRelUnderlyingType, "underlying_type"},
    {kRelTemplateArg, "template_arg"},
    {kRelClassType, "class_type"},
};

std::string cxs(CXString s) {
  const char *c = ::clang_getCString(s);
  std::string out = c != nullptr ? c : "";
  ::clang_disposeString(s);
  return out;
}

const char *cursor_category(CXCursorKind k) {
  if (k == CXCursor_TranslationUnit)
    return "tu";
  if (::clang_isDeclaration(k))
    return "decl";
  if (::clang_isReference(k))
    return "ref";
  if (::clang_isExpression(k))
    return "expr";
  if (::clang_isStatement(k))
    return "stmt";
  if (::clang_isAttribute(k))
    return "attr";
  if (::clang_isPreprocessing(k))
    return "preproc";
  return "other";
}

// CXType identity inside one TU: (kind, data[0], data[1]) — the same triple
// clang_equalTypes compares.  Valid only while the TU is alive, which is the
// whole lifetime of a dump.
struct TypeKey {
  int kind;
  const void *d0;
  const void *d1;
  bool operator==(const TypeKey &o) const {
    return kind == o.kind && d0 == o.d0 && d1 == o.d1;
  }
};
struct TypeKeyHash {
  std::size_t operator()(const TypeKey &k) const {
    std::size_t h = std::hash<int>()(k.kind);
    h ^= std::hash<const void *>()(k.d0) + 0x9e3779b97f4a7c15ULL + (h << 6);
    h ^= std::hash<const void *>()(k.d1) + 0x9e3779b97f4a7c15ULL + (h << 6);
    return h;
  }
};

class Dumper {
public:
  Dumper(const std::string &path, const Options &opts)
      : db_(path), main_only_(opts.main_only) {
    db_.exec(kSchemaSql);
    db_.exec("BEGIN");
  }

  void write_meta(const std::string &source,
                  const std::vector<std::string> &args,
                  const std::optional<std::string> &driver) {
    put_meta("schema_version", std::to_string(kSchemaVersion));
    put_meta("generator", std::string("cidx-astgraph ") + cli::kVersion);
    put_meta("source", source);
    put_meta("args", json_min::encode_string_array(args));
    put_meta("driver", driver ? *driver : "");
    put_meta("libclang", cxs(::clang_getClangVersion()));
    put_meta("main_only", main_only_ ? "1" : "0");
    put_meta("kind_scheme", "cursor=CXCursorKind; type=1000+CXTypeKind");
  }

  void seed_relation_kinds() {
    for (const RelName &r : kRelNames) {
      run("INSERT INTO relation_kind(id, name) VALUES (?, ?)",
          {static_cast<int64_t>(r.id), std::string(r.name)});
    }
  }

  // --- interning -----------------------------------------------------------

  int64_t intern_cursor(CXCursor c) {
    if (::clang_Cursor_isNull(c) != 0)
      return 0;
    const CXCursorKind kind = ::clang_getCursorKind(c);
    if (::clang_isInvalid(kind) != 0)
      return 0;
    auto &bucket = cursor_ids_[::clang_hashCursor(c)];
    for (const auto &[seen, id] : bucket) {
      if (::clang_equalCursors(seen, c) != 0)
        return id;
    }
    const int64_t id = next_node_++;
    bucket.emplace_back(c, id);
    seed_cursor_kind(kind);

    // Location: expansion coordinates of the extent (clang.cindex parity).
    int64_t file_id = 0;
    unsigned line = 0, col = 0, end_line = 0, end_col = 0;
    const CXSourceRange extent = ::clang_getCursorExtent(c);
    CXFile file = nullptr;
    ::clang_getExpansionLocation(::clang_getRangeStart(extent), &file, &line,
                                 &col, nullptr);
    ::clang_getExpansionLocation(::clang_getRangeEnd(extent), nullptr,
                                 &end_line, &end_col, nullptr);
    if (file != nullptr) {
      const bool is_main =
          ::clang_Location_isFromMainFile(::clang_getCursorLocation(c)) != 0;
      file_id = intern_file(file, is_main);
    }

    const std::string spelling = cxs(::clang_getCursorSpelling(c));
    int64_t symbol_id = 0;
    std::string usr = cxs(::clang_getCursorUSR(c));
    if (!usr.empty())
      symbol_id = intern_symbol(std::move(usr), spelling, c, kind);
    // The cursor's own type is a node PROPERTY (clang_getCursorType is a
    // cursor accessor in the libclang API), stored inline — not an edge.
    const int64_t type_id = intern_type(::clang_getCursorType(c));

    run("INSERT INTO node(id, kind_id, symbol_id, type_id, spelling, file_id, "
        "line, col, end_line, end_col, is_definition, access, is_const, "
        "is_volatile, is_restrict) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        {id, static_cast<int64_t>(kind), symbol_id, type_id, spelling, file_id,
         static_cast<int64_t>(line), static_cast<int64_t>(col),
         static_cast<int64_t>(end_line), static_cast<int64_t>(end_col),
         static_cast<int64_t>(::clang_isCursorDefinition(c) != 0 ? 1 : 0),
         static_cast<int64_t>(::clang_getCXXAccessSpecifier(c)),
         static_cast<int64_t>(0), static_cast<int64_t>(0),
         static_cast<int64_t>(0)});
    ++stats_.cursor_nodes;
    cursor_work_.emplace_back(c, id);
    return id;
  }

  int64_t intern_type(CXType t) {
    if (t.kind == CXType_Invalid)
      return 0;
    const TypeKey key{static_cast<int>(t.kind), t.data[0], t.data[1]};
    const auto it = type_ids_.find(key);
    if (it != type_ids_.end())
      return it->second;
    const int64_t id = next_node_++;
    type_ids_.emplace(key, id);
    seed_type_kind(t.kind);
    run("INSERT INTO node(id, kind_id, symbol_id, type_id, spelling, file_id, "
        "line, col, end_line, end_col, is_definition, access, is_const, "
        "is_volatile, is_restrict) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        {id, static_cast<int64_t>(kTypeKindBase + t.kind), int64_t{0},
         int64_t{0}, cxs(::clang_getTypeSpelling(t)), int64_t{0}, int64_t{0},
         int64_t{0}, int64_t{0}, int64_t{0}, int64_t{0}, int64_t{0},
         static_cast<int64_t>(::clang_isConstQualifiedType(t) != 0 ? 1 : 0),
         static_cast<int64_t>(::clang_isVolatileQualifiedType(t) != 0 ? 1 : 0),
         static_cast<int64_t>(::clang_isRestrictQualifiedType(t) != 0 ? 1
                                                                      : 0)});
    ++stats_.type_nodes;
    type_work_.emplace_back(t, id);
    return id;
  }

  void add_edge(int64_t src, int64_t dst, int rel, int64_t ord) {
    if (src == 0 || dst == 0)
      return;
    run("INSERT INTO edge(src_id, dst_id, rel_id, ord) VALUES (?,?,?,?)",
        {src, dst, static_cast<int64_t>(rel), ord});
    if (db_.changes() == 1)
      ++stats_.edges;
  }

  // --- walk policy / error stash (visitor is a noexcept C callback) --------

  bool skip_structural(CXCursor c) const {
    return main_only_ &&
           ::clang_Location_isFromMainFile(::clang_getCursorLocation(c)) == 0;
  }
  bool failed() const { return error_ != nullptr; }
  void set_failed(std::exception_ptr e) {
    if (error_ == nullptr)
      error_ = std::move(e);
  }
  void rethrow_if_failed() const {
    if (error_ != nullptr)
      std::rethrow_exception(error_);
  }

  // Drain the cross-reference worklists to fixpoint.  Processing may intern
  // new cursors/types, which re-feeds the queues; both are finite (bounded by
  // the TU's cursors and types), so this terminates.
  void drain() {
    while (!cursor_work_.empty() || !type_work_.empty()) {
      if (!cursor_work_.empty()) {
        const auto [c, id] = cursor_work_.front();
        cursor_work_.pop_front();
        process_cursor(c, id);
      } else {
        const auto [t, id] = type_work_.front();
        type_work_.pop_front();
        process_type(t, id);
      }
    }
  }

  DumpStats finish() {
    db_.exec("COMMIT");
    return stats_;
  }

private:
  // One cursor's semantic cross-references (the fixed kRel* catalog).
  void process_cursor(CXCursor c, int64_t id) {
    const CXCursorKind kind = ::clang_getCursorKind(c);

    const CXCursor ref = ::clang_getCursorReferenced(c);
    if (::clang_Cursor_isNull(ref) == 0 && ::clang_equalCursors(ref, c) == 0)
      add_edge(id, intern_cursor(ref), kRelReferences, 0);

    const CXCursor def = ::clang_getCursorDefinition(c);
    if (::clang_Cursor_isNull(def) == 0 && ::clang_equalCursors(def, c) == 0)
      add_edge(id, intern_cursor(def), kRelDefinition, 0);

    const CXCursor canon = ::clang_getCanonicalCursor(c);
    if (::clang_Cursor_isNull(canon) == 0 &&
        ::clang_equalCursors(canon, c) == 0)
      add_edge(id, intern_cursor(canon), kRelCanonical, 0);

    // Parent edges only for declarations: expressions/statements would just
    // duplicate the structural child chain with noisier endpoints.
    if (::clang_isDeclaration(kind) != 0) {
      add_edge(id, intern_cursor(::clang_getCursorSemanticParent(c)),
               kRelSemanticParent, 0);
      add_edge(id, intern_cursor(::clang_getCursorLexicalParent(c)),
               kRelLexicalParent, 0);
    }

    add_edge(id, intern_cursor(::clang_getSpecializedCursorTemplate(c)),
             kRelSpecializes, 0);

    CXCursor *overridden = nullptr;
    unsigned n_overridden = 0;
    ::clang_getOverriddenCursors(c, &overridden, &n_overridden);
    for (unsigned i = 0; i < n_overridden; ++i)
      add_edge(id, intern_cursor(overridden[i]), kRelOverrides,
               static_cast<int64_t>(i));
    if (overridden != nullptr)
      ::clang_disposeOverriddenCursors(overridden);

    if (kind == CXCursor_TypedefDecl || kind == CXCursor_TypeAliasDecl)
      add_edge(id, intern_type(::clang_getTypedefDeclUnderlyingType(c)),
               kRelUnderlyingType, 0);
  }

  // One type's edges in the CXType graph.
  void process_type(CXType t, int64_t id) {
    const CXCursor decl = ::clang_getTypeDeclaration(t);
    if (::clang_Cursor_isNull(decl) == 0 &&
        ::clang_getCursorKind(decl) != CXCursor_NoDeclFound)
      add_edge(id, intern_cursor(decl), kRelTypeDecl, 0);

    const CXType canon = ::clang_getCanonicalType(t);
    if (canon.kind != CXType_Invalid && ::clang_equalTypes(t, canon) == 0)
      add_edge(id, intern_type(canon), kRelCanonicalType, 0);

    add_edge(id, intern_type(::clang_getPointeeType(t)), kRelPointee, 0);
    add_edge(id, intern_type(::clang_getElementType(t)), kRelElementType, 0);
    add_edge(id, intern_type(::clang_getResultType(t)), kRelResultType, 0);

    const int n_args = ::clang_getNumArgTypes(t);
    for (int i = 0; i < n_args; ++i)
      add_edge(id, intern_type(::clang_getArgType(t, static_cast<unsigned>(i))),
               kRelArgType, i);

    if (t.kind == CXType_Elaborated)
      add_edge(id, intern_type(::clang_Type_getNamedType(t)), kRelNamedType,
               0);
    add_edge(id, intern_type(::clang_Type_getClassType(t)), kRelClassType, 0);

    const int n_targs = ::clang_Type_getNumTemplateArguments(t);
    for (int i = 0; i < n_targs; ++i)
      add_edge(id,
               intern_type(::clang_Type_getTemplateArgumentAsType(
                   t, static_cast<unsigned>(i))),
               kRelTemplateArg, i);
  }

  int64_t intern_file(CXFile file, bool is_main) {
    const std::string path = cxs(::clang_getFileName(file));
    if (path.empty())
      return 0;
    const auto it = file_ids_.find(path);
    int64_t id = 0;
    if (it != file_ids_.end()) {
      id = it->second;
    } else {
      id = next_file_++;
      file_ids_.emplace(path, id);
      run("INSERT INTO file(id, path, is_main) VALUES (?,?,?)",
          {id, path, int64_t{0}});
      ++stats_.files;
    }
    if (is_main && main_files_.insert(id).second)
      run("UPDATE file SET is_main = 1 WHERE id = ?", {id});
    return id;
  }

  int64_t intern_symbol(std::string usr, const std::string &name, CXCursor c,
                        CXCursorKind kind) {
    const auto it = symbol_ids_.find(usr);
    if (it != symbol_ids_.end())
      return it->second;
    const int64_t id = next_symbol_++;
    run("INSERT INTO symbol(id, usr, name, kind_id, linkage) VALUES "
        "(?,?,?,?,?)",
        {id, usr, name, static_cast<int64_t>(kind),
         static_cast<int64_t>(::clang_getCursorLinkage(c))});
    symbol_ids_.emplace(std::move(usr), id);
    ++stats_.symbols;
    return id;
  }

  void seed_cursor_kind(CXCursorKind k) {
    if (!seeded_kinds_.insert(static_cast<int>(k)).second)
      return;
    run("INSERT OR IGNORE INTO node_kind(id, name, category) VALUES (?,?,?)",
        {static_cast<int64_t>(k), std::string(cli::kind_name(k)),
         std::string(cursor_category(k))});
  }

  void seed_type_kind(CXTypeKind k) {
    if (!seeded_kinds_.insert(kTypeKindBase + static_cast<int>(k)).second)
      return;
    run("INSERT OR IGNORE INTO node_kind(id, name, category) VALUES (?,?,?)",
        {static_cast<int64_t>(kTypeKindBase + static_cast<int>(k)),
         cxs(::clang_getTypeKindSpelling(k)), std::string("type")});
  }

  void put_meta(const std::string &key, const std::string &value) {
    run("INSERT INTO meta(key, value) VALUES (?,?)", {key, value});
  }

  // Storage's documented pattern: prepare per call, no statement cache; the
  // surrounding BEGIN batches everything into one commit.
  void run(const char *sql, std::initializer_list<SqlValue> vals) {
    SqliteStmt stmt = db_.prepare(sql);
    int idx = 1;
    for (const SqlValue &v : vals)
      stmt.bind(idx++, v);
    stmt.step_done();
  }

  SqliteDb db_;
  bool main_only_ = false;
  DumpStats stats_;
  std::exception_ptr error_;

  int64_t next_node_ = 1;
  int64_t next_file_ = 1;
  int64_t next_symbol_ = 1;

  std::unordered_map<unsigned, std::vector<std::pair<CXCursor, int64_t>>>
      cursor_ids_;
  std::unordered_map<TypeKey, int64_t, TypeKeyHash> type_ids_;
  std::unordered_map<std::string, int64_t> file_ids_;
  std::unordered_map<std::string, int64_t> symbol_ids_;
  std::unordered_set<int> seeded_kinds_;
  std::unordered_set<int64_t> main_files_;
  std::deque<std::pair<CXCursor, int64_t>> cursor_work_;
  std::deque<std::pair<CXType, int64_t>> type_work_;
};

struct WalkFrame {
  Dumper *dumper;
  int64_t parent_id;
  int64_t ord;
};

// noexcept C callback (D23): exceptions are stashed on the Dumper and
// rethrown after clang_visitChildren returns.
CXChildVisitResult walk_visitor(CXCursor c, CXCursor /*parent*/,
                                CXClientData data) noexcept {
  auto *frame = static_cast<WalkFrame *>(data);
  Dumper *d = frame->dumper;
  try {
    if (d->skip_structural(c))
      return CXChildVisit_Continue;
    const int64_t id = d->intern_cursor(c);
    d->add_edge(frame->parent_id, id, kRelChild, frame->ord++);
    WalkFrame child{d, id, 0};
    ::clang_visitChildren(c, walk_visitor, &child);
  } catch (...) {
    d->set_failed(std::current_exception());
  }
  return d->failed() ? CXChildVisit_Break : CXChildVisit_Continue;
}

} // namespace

DumpStats dump_tu(const ParsedTu &tu, const std::string &out_db_path,
                  const Options &opts, const std::string &source_path,
                  const std::vector<std::string> &args,
                  const std::optional<std::string> &driver) {
  // The dump is a derived artifact regenerated wholesale: truncate any
  // previous run's DB (plus a stale rollback journal) before writing.
  std::remove(out_db_path.c_str());
  std::remove((out_db_path + "-journal").c_str());

  Dumper dumper(out_db_path, opts);
  dumper.write_meta(source_path, args, driver);
  dumper.seed_relation_kinds();

  const CXCursor root = ::clang_getTranslationUnitCursor(tu.tu);
  const int64_t root_id = dumper.intern_cursor(root);
  WalkFrame frame{&dumper, root_id, 0};
  ::clang_visitChildren(root, walk_visitor, &frame);
  dumper.rethrow_if_failed();
  dumper.drain();
  return dumper.finish();
}

} // namespace astgraph
} // namespace cidx

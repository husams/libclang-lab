// AST indexer — see ast.hpp. Line-level behavior is pinned to
// project/indexer/clang/ast.py (cited per function).
#include "clangx/ast.hpp"

#include <cstring>
#include <exception>
#include <unordered_set>
#include <vector>

#include <sys/stat.h>

#include "clangx/libclang.hpp"
#include "util/env.hpp"
#include "util/hashing.hpp"
#include "util/pathutil.hpp"

namespace cidx {
namespace {

// _KIND_MAP (ast.py:25-43): exactly 17 CursorKinds -> storage kinds; cursors
// of any other kind are ignored. The `macro` entry is unreachable today —
// parse() passes options=0, so no DETAILED_PREPROCESSING_RECORD, so
// MACRO_DEFINITION cursors never appear (G22/D19) — but it stays mapped for
// DB compatibility.
const char *kind_name(CXCursorKind kind) {
  switch (kind) {
  case CXCursor_ClassDecl:
    return "class";
  case CXCursor_StructDecl:
    return "struct";
  case CXCursor_UnionDecl:
    return "union";
  case CXCursor_FunctionDecl:
    return "function";
  case CXCursor_CXXMethod:
    return "method";
  case CXCursor_FieldDecl:
    return "member";
  case CXCursor_Constructor:
    return "constructor";
  case CXCursor_Destructor:
    return "destructor";
  case CXCursor_EnumDecl:
    return "enum";
  case CXCursor_EnumConstantDecl:
    return "enum-constant";
  case CXCursor_TypedefDecl:
    return "typedef";
  case CXCursor_TypeAliasDecl:
    return "type-alias";
  case CXCursor_ClassTemplate:
    return "class-template";
  case CXCursor_FunctionTemplate:
    return "function-template";
  case CXCursor_VarDecl:
    return "variable";
  case CXCursor_Namespace:
    return "namespace";
  case CXCursor_MacroDefinition:
    return "macro";
  default:
    return nullptr;
  }
}

// Symbol kind for a minted stub, taken from its reference cursor so a defaulted
// ctor stub is 'constructor', not the bare 'function' fallback (used when the
// cursor maps to no storage kind). Mirrors ast.py's _KIND_MAP.get(k,"function").
std::string stub_kind(LibClang &lib, CXCursor c) {
  const char *k = kind_name(lib.clang_getCursorKind(c));
  return k != nullptr ? std::string(k) : std::string("function");
}

// _FUNCTION_KINDS (ast.py:53-59): indexed themselves, but their bodies are
// NOT walked (locals, body-scoped types, and statements are not file-scope
// symbols).
bool is_function_like(CXCursorKind kind) {
  return kind == CXCursor_FunctionDecl || kind == CXCursor_CXXMethod ||
         kind == CXCursor_Constructor || kind == CXCursor_Destructor ||
         kind == CXCursor_FunctionTemplate;
}

// Python's Cursor.from_result turns the null/invalid cursor into None; the
// invalid-kind range is the C-API equivalent (cursor walks stop there).
bool is_invalid_kind(CXCursorKind kind) {
  return kind >= CXCursor_FirstInvalid && kind <= CXCursor_LastInvalid;
}

// cursor.location expansion site: file handle + 1-based line/column.
struct ExpansionLoc {
  CXFile file = nullptr;
  unsigned line = 0;
  unsigned col = 0;
};

ExpansionLoc cursor_location(LibClang &lib, CXCursor cursor) {
  ExpansionLoc loc;
  unsigned offset = 0;
  lib.clang_getExpansionLocation(lib.clang_getCursorLocation(cursor), &loc.file,
                                 &loc.line, &loc.col, &offset);
  return loc;
}

// Declaration location of a mint target's reference cursor. The target of a
// mint (callee/base/override/primary) carries a real source location even when
// its definition body is never separately indexed -- e.g. an implicit/defaulted
// ctor is anchored to its `struct` line. Recording it here is what lets
// chain::D::D resolve to chain.hpp:25 instead of `@<no-location>`.
//
// Lookup-only for the registered file id (db.get_file, never add_file_path). A
// target in a file no registered component owns -- system/stdlib headers -- has
// no file row, so `file_id` is nullopt; but the AST still knows where it is, so
// `path` carries the raw file path (with line/col). The stub then keeps that
// location instead of going `@<no-location>` (e.g. libstdc++
// __normal_iterator::operator* shows stl_iterator.h:NNNN). Only a cursor with no
// source location at all (implicit/builtin) yields all nullopt. Mirrors
// ast.py:_ref_decl_loc.
struct RefDeclLoc {
  std::optional<int64_t> file_id;
  std::optional<int64_t> line;
  std::optional<int64_t> col;
  std::optional<std::string> path; // raw path when file_id is unregistered
};

RefDeclLoc ref_decl_loc(LibClang &lib, Storage &db, CXCursor ref) {
  RefDeclLoc out;
  const ExpansionLoc loc = cursor_location(lib, ref);
  if (loc.file == nullptr) {
    return out;
  }
  const std::string fname = CxString(lib, lib.clang_getFileName(loc.file)).str();
  if (fname.empty()) {
    return out;
  }
  out.line = static_cast<int64_t>(loc.line);
  out.col = static_cast<int64_t>(loc.col);
  const auto row = db.get_file(fname);
  if (!row) {
    out.path = fname; // unregistered (system/stdlib) header: keep the raw path
    return out;
  }
  out.file_id = row->id;
  return out;
}

// Strip pointer/reference/array layers off `t` and return the declaration
// cursor of the named type it spells, or the null cursor when the type has no
// user declaration (builtins like int, function pointers, …). Single-level by
// design: resolves the type as WRITTEN (a typedef alias stays the alias),
// mirroring ast.py:_named_type_decl.
CXCursor named_type_decl(LibClang &lib, CXType t) {
  for (int i = 0; i < 32; ++i) {            // guard against pathological nesting
    const CXTypeKind tk = t.kind;
    if (tk == CXType_Pointer || tk == CXType_LValueReference ||
        tk == CXType_RValueReference) {
      t = lib.clang_getPointeeType(t);
    } else if (tk == CXType_ConstantArray || tk == CXType_IncompleteArray ||
               tk == CXType_VariableArray || tk == CXType_DependentSizedArray) {
      t = lib.clang_getArrayElementType(t);
    } else {
      break;
    }
  }
  const CXCursor decl = lib.clang_getTypeDeclaration(t);
  if (lib.clang_Cursor_isNull(decl) ||
      is_invalid_kind(lib.clang_getCursorKind(decl))) {
    return lib.clang_getNullCursor();
  }
  return decl;
}

// Emit a `uses` edge (kind=7) src -> the record/enum/typedef named by `ctype`
// (parameter, return, field, variable, or typedef-underlying type), grounded
// at `loc_cursor`'s location. Lookup-only like body descent (the type's symbol
// must already be indexed, so builtins/unindexed stdlib types create neither
// edges nor stubs); no self-edge. Mirrors ast.py:_emit_type_use.
void emit_type_use(LibClang &lib, Storage &db, int64_t src_id, CXType ctype,
                   int64_t file_id, CXCursor loc_cursor, int conditional) {
  const CXCursor decl = named_type_decl(lib, ctype);
  if (lib.clang_Cursor_isNull(decl)) {
    return;
  }
  const std::string usr = CxString(lib, lib.clang_getCursorUSR(decl)).str();
  if (usr.empty()) {
    return;
  }
  const auto dst = db.lookup_symbol(usr);
  if (!dst || dst->id == src_id) {
    return;
  }
  Edge e;
  e.src_id = src_id;
  e.dst_id = dst->id;
  e.kind = 7; // uses
  e.count = 1;
  const int64_t edge_id = db.add_edge(e);

  const ExpansionLoc loc = cursor_location(lib, loc_cursor);
  if (loc.line != 0) {
    EdgeSite site;
    site.edge_id = edge_id;
    site.file_id = file_id;
    site.line = static_cast<int64_t>(loc.line);
    site.col = static_cast<int64_t>(loc.col);
    site.conditional = conditional;
    db.add_edge_site(site);
  }
}

// _linkage (ast.py:77-79) via the explicit D13 table — the stored spellings
// are DB content shared with Python-written rows.
std::optional<std::string> linkage_name(CXLinkageKind linkage) {
  switch (linkage) {
  case CXLinkage_NoLinkage:
    return std::string("no-linkage");
  case CXLinkage_Internal:
    return std::string("internal");
  case CXLinkage_UniqueExternal:
    return std::string("unique-external");
  case CXLinkage_External:
    return std::string("external");
  case CXLinkage_Invalid:
  default:
    return std::nullopt; // INVALID -> NULL
  }
}

// _ACCESS (ast.py:45-49) via the D13 table; invalid/none -> NULL.
std::optional<std::string> access_name(CX_CXXAccessSpecifier access) {
  switch (access) {
  case CX_CXXPublic:
    return std::string("public");
  case CX_CXXProtected:
    return std::string("protected");
  case CX_CXXPrivate:
    return std::string("private");
  default:
    return std::nullopt;
  }
}

// '_qualified_name' (ast.py:82-91): 'ns::Class::name' built from SEMANTIC
// parents, so an out-of-line method definition is qualified by its class,
// not the file scope it sits in. Anonymous levels (empty spelling) are
// skipped (G25).
std::string qualified_name(LibClang &lib, CXCursor cursor) {
  std::vector<std::string> parts;
  CXCursor c = cursor;
  while (true) {
    const CXCursorKind kind = lib.clang_getCursorKind(c);
    if (is_invalid_kind(kind) || kind == CXCursor_TranslationUnit) {
      break;
    }
    std::string spelling = CxString(lib, lib.clang_getCursorSpelling(c)).str();
    if (!spelling.empty()) {
      parts.push_back(std::move(spelling));
    }
    c = lib.clang_getCursorSemanticParent(c);
  }
  std::string out;
  for (auto it = parts.rbegin(); it != parts.rend(); ++it) {
    if (!out.empty()) {
      out += "::";
    }
    out += *it;
  }
  return out;
}

// Visitor context for for_file_cursors. Callbacks into libclang are noexcept
// (D23): a C++ exception thrown by fn is stashed here, the walk is Break-ed,
// and the exception is rethrown after clang_visitChildren returns.
struct WalkCtx {
  LibClang *lib = nullptr;
  const std::string *filename = nullptr;
  const std::function<void(CXCursor)> *fn = nullptr;
  std::exception_ptr error;
};

CXChildVisitResult walk_visitor(CXCursor cursor, CXCursor /*parent*/,
                                CXClientData data) noexcept {
  auto *ctx = static_cast<WalkCtx *>(data);
  try {
    LibClang &lib = *ctx->lib;
    const ExpansionLoc loc = cursor_location(lib, cursor);
    if (loc.file == nullptr) {
      return CXChildVisit_Continue; // cursor from no file: skip subtree
    }
    // R11: compare the raw C string BEFORE constructing any std::string to
    // avoid a heap allocation per visited cursor (including pruned ones).
    // CxString RAII ensures clang_disposeString is called in all paths.
    CXString fname_cx = lib.clang_getFileName(loc.file);
    CxString fname_raii(lib, fname_cx);
    const char *raw = lib.clang_getCString(fname_cx);
    if (raw == nullptr || std::strcmp(raw, ctx->filename->c_str()) != 0) {
      return CXChildVisit_Continue; // cursor from another file: skip subtree
    }
    (*ctx->fn)(cursor);
    return is_function_like(lib.clang_getCursorKind(cursor))
               ? CXChildVisit_Continue // body not walked
               : CXChildVisit_Recurse;
  } catch (...) {
    ctx->error = std::current_exception();
    return CXChildVisit_Break;
  }
}

// Parent-aware variant of WalkCtx: passes (cursor, parent) to fn.
// Used by index_edges so CXX_BASE_SPECIFIER handlers can get the enclosing
// record from the walk parent (spec §1.4 gotcha: semantic_parent is NULL).
struct WalkCtxP {
  LibClang *lib = nullptr;
  const std::string *filename = nullptr;
  const std::function<void(CXCursor, CXCursor)> *fn = nullptr;
  std::exception_ptr error;
};

CXChildVisitResult walk_visitor_p(CXCursor cursor, CXCursor parent,
                                  CXClientData data) noexcept {
  auto *ctx = static_cast<WalkCtxP *>(data);
  try {
    LibClang &lib = *ctx->lib;
    const ExpansionLoc loc = cursor_location(lib, cursor);
    if (loc.file == nullptr) {
      return CXChildVisit_Continue;
    }
    CXString fname_cx = lib.clang_getFileName(loc.file);
    CxString fname_raii(lib, fname_cx);
    const char *raw = lib.clang_getCString(fname_cx);
    if (raw == nullptr || std::strcmp(raw, ctx->filename->c_str()) != 0) {
      return CXChildVisit_Continue;
    }
    (*ctx->fn)(cursor, parent);
    return is_function_like(lib.clang_getCursorKind(cursor))
               ? CXChildVisit_Continue
               : CXChildVisit_Recurse;
  } catch (...) {
    ctx->error = std::current_exception();
    return CXChildVisit_Break;
  }
}

// One transitive inclusion of the TU, copied out as plain data inside the
// (noexcept) inclusion visitor. The CXFile handle stays valid for the TU's
// lifetime and is only consulted while the ParsedTu is alive.
struct InclusionRec {
  CXFile file = nullptr;
  std::string name; // libclang's spelling of the included file (G23)
};

struct InclusionCtx {
  LibClang *lib = nullptr;
  std::vector<InclusionRec> inclusions;
  std::exception_ptr error;
};

void inclusion_visitor(CXFile included_file, CXSourceLocation * /*stack*/,
                       unsigned include_len, CXClientData data) noexcept {
  auto *ctx = static_cast<InclusionCtx *>(data);
  if (include_len == 0) {
    return; // the main file itself (cindex `depth > 0` parity)
  }
  try {
    InclusionRec rec;
    rec.file = included_file;
    rec.name =
        CxString(*ctx->lib, ctx->lib->clang_getFileName(included_file)).str();
    ctx->inclusions.push_back(std::move(rec));
  } catch (...) {
    ctx->error = std::current_exception();
  }
}

// _ignore_system_headers (ast.py:171-174): default true; the exact falsy set
// {0,false,no,off} (env.hpp) turns system-header indexing on.
bool default_ignore_system_headers() {
  const std::optional<std::string> val = get_env(kIgnoreSystemHeadersEnv);
  return !env_flag_false_headers(val ? val->c_str() : nullptr);
}

// _is_system_header (ast.py:177-180): per-TU via clang_getLocation(tu, file,
// 1, 1) — honors the -isystem/sysroot of THIS parse (G26).
bool is_system_header(LibClang &lib, CXTranslationUnit tu, CXFile file) {
  const CXSourceLocation loc = lib.clang_getLocation(tu, file, 1, 1);
  return lib.clang_Location_isInSystemHeader(loc) != 0;
}

// os.path.getmtime(path) if os.path.exists(path) else None (ast.py:220):
// float seconds = sec + nsec * 1e-9.
std::optional<double> file_mtime(const std::string &path) {
  struct stat st{};
  if (::stat(path.c_str(), &st) != 0) {
    return std::nullopt;
  }
#ifdef __APPLE__
  return static_cast<double>(st.st_mtimespec.tv_sec) +
         static_cast<double>(st.st_mtimespec.tv_nsec) * 1e-9;
#else
  return static_cast<double>(st.st_mtim.tv_sec) +
         static_cast<double>(st.st_mtim.tv_nsec) * 1e-9;
#endif
}

} // namespace

void AstIndexer::for_file_cursors(const ParsedTu &tu,
                                  const std::string &filename,
                                  const std::function<void(CXCursor)> &fn) {
  LibClang &lib = LibClang::instance();
  WalkCtx ctx;
  ctx.lib = &lib;
  ctx.filename = &filename;
  ctx.fn = &fn;
  lib.clang_visitChildren(lib.clang_getTranslationUnitCursor(tu.tu),
                          &walk_visitor, &ctx);
  if (ctx.error) {
    std::rethrow_exception(ctx.error);
  }
}

void AstIndexer::for_file_cursors_p(
    const ParsedTu &tu, const std::string &filename,
    const std::function<void(CXCursor, CXCursor)> &fn) {
  LibClang &lib = LibClang::instance();
  WalkCtxP ctx;
  ctx.lib = &lib;
  ctx.filename = &filename;
  ctx.fn = &fn;
  lib.clang_visitChildren(lib.clang_getTranslationUnitCursor(tu.tu),
                          &walk_visitor_p, &ctx);
  if (ctx.error) {
    std::rethrow_exception(ctx.error);
  }
}

std::optional<Symbol> AstIndexer::to_symbol(CXCursor cursor, int64_t file_id) {
  LibClang &lib = LibClang::instance();
  const char *kind = kind_name(lib.clang_getCursorKind(cursor));
  if (kind == nullptr) {
    return std::nullopt;
  }
  std::string usr = CxString(lib, lib.clang_getCursorUSR(cursor)).str();
  if (usr.empty()) {
    return std::nullopt; // no USR -> not indexable (ast.py:99-101)
  }

  // Parent USR: the semantic parent unless it is the TU (ast.py:102-105).
  std::optional<std::string> parent_usr;
  const CXCursor parent = lib.clang_getCursorSemanticParent(cursor);
  const CXCursorKind parent_kind = lib.clang_getCursorKind(parent);
  if (!is_invalid_kind(parent_kind) &&
      parent_kind != CXCursor_TranslationUnit) {
    std::string pu = CxString(lib, lib.clang_getCursorUSR(parent)).str();
    if (!pu.empty()) {
      parent_usr = std::move(pu);
    }
  }

  const bool is_def = lib.clang_isCursorDefinition(cursor) != 0;
  const ExpansionLoc loc = cursor_location(lib, cursor);

  Symbol sym;
  sym.usr = std::move(usr);
  sym.spelling = CxString(lib, lib.clang_getCursorSpelling(cursor)).str();
  sym.kind = kind;
  std::string qual = qualified_name(lib, cursor);
  if (!qual.empty()) {
    sym.qual_name = std::move(qual);
  }
  std::string display =
      CxString(lib, lib.clang_getCursorDisplayName(cursor)).str();
  if (!display.empty()) {
    sym.display_name = std::move(display);
  }
  std::string type_info =
      CxString(lib, lib.clang_getTypeSpelling(lib.clang_getCursorType(cursor)))
          .str();
  if (!type_info.empty()) {
    sym.type_info = std::move(type_info);
  }
  sym.file_id = file_id;
  sym.line = static_cast<int64_t>(loc.line);
  sym.col = static_cast<int64_t>(loc.col);
  // A declaration cursor records itself as the decl site too; the upsert
  // keeps it when the definition later takes file/line/col (ast.py:117-121).
  if (!is_def) {
    sym.decl_file_id = file_id;
    sym.decl_line = static_cast<int64_t>(loc.line);
    sym.decl_col = static_cast<int64_t>(loc.col);
  }
  sym.is_definition = is_def;
  sym.is_pure = lib.clang_CXXMethod_isPureVirtual(cursor) != 0;
  // C++ static member function. False for free functions and non-methods; a
  // file-scope `static` free function is captured by linkage='internal'.
  sym.is_static = lib.clang_CXXMethod_isStatic(cursor) != 0;
  sym.linkage = linkage_name(lib.clang_getCursorLinkage(cursor));
  sym.access = access_name(lib.clang_getCXXAccessSpecifier(cursor));
  sym.parent_usr = std::move(parent_usr);
  // A definition resolves the symbol; a bare declaration leaves it
  // unresolved until some TU provides the definition (ast.py:127-129).
  sym.resolved = is_def;
  return sym;
}

bool AstIndexer::store(const Symbol &sym) {
  const std::optional<Symbol> existing = db_.lookup_symbol(sym.usr);
  if (existing && existing->resolved) {
    // The definition is already stored, but this cursor may be the header
    // declaration of it — record the decl site if missing (G15).
    if (sym.decl_file_id && !existing->decl_file_id) {
      db_.update_symbol(sym.usr, {{"decl_file_id", SqlValue(*sym.decl_file_id)},
                                  {"decl_line", SqlValue(*sym.decl_line)},
                                  {"decl_col", SqlValue(*sym.decl_col)}});
    }
    return false; // skipped
  }
  db_.add_symbol(sym);
  return true;
}

// M4: txn-free inner work — caller MUST own an open transaction.
std::pair<int, int> AstIndexer::index_file_notxn(const ParsedTu &tu,
                                                 const std::string &filename,
                                                 int64_t file_id) {
  int stored = 0;
  int skipped = 0;
  for_file_cursors(tu, filename, [&](CXCursor cursor) {
    const std::optional<Symbol> sym = to_symbol(cursor, file_id);
    if (!sym) {
      return;
    }
    if (store(*sym)) {
      ++stored;
    } else {
      ++skipped;
    }
  });
  return {stored, skipped};
}

std::pair<int, int> AstIndexer::index_file(const ParsedTu &tu,
                                           const std::string &filename,
                                           int64_t file_id) {
  Transaction txn = db_.transaction(); // one txn per file (ast.py:142)
  const auto result = index_file_notxn(tu, filename, file_id);
  txn.commit(); // R2: explicit commit so a COMMIT failure is not swallowed
  return result;
}

int AstIndexer::index_symbols(const ParsedTu &tu, const std::string &filename,
                              int64_t file_id) {
  // ast.py:163-168 — the main file is tu.spelling (the path exactly as
  // passed to parse, G24); callers pass that same path.
  return index_file(tu, filename, file_id).first;
}

HeaderStats
AstIndexer::index_headers(const ParsedTu &tu,
                          const std::optional<bool> &ignore_system) {
  LibClang &lib = LibClang::instance();
  const bool ignore =
      ignore_system ? *ignore_system : default_ignore_system_headers();

  // tu.get_includes() parity: collect the transitive inclusion list as plain
  // data first (cindex does the same), then do all DB work outside the C
  // callback. A header included twice appears twice; dedupe below.
  InclusionCtx ctx;
  ctx.lib = &lib;
  lib.clang_getInclusions(tu.tu, &inclusion_visitor, &ctx);
  if (ctx.error) {
    std::rethrow_exception(ctx.error);
  }

  HeaderStats counts;
  std::unordered_set<std::string> seen;
  for (const InclusionRec &inc : ctx.inclusions) {
    const std::string path = pathutil::abspath(inc.name);
    if (!seen.insert(path).second) {
      continue;
    }
    if (ignore && is_system_header(lib, tu.tu, inc.file)) {
      ++counts.system;
      continue;
    }
    if (!db_.component_for_path(path)) {
      ++counts.unowned;
      continue;
    }
    const std::optional<std::string> md5 = md5_of(path);
    if (db_.is_file_indexed(path, std::nullopt, md5)) {
      ++counts.already;
      continue;
    }
    const std::optional<double> mtime = file_mtime(path);
    // Header file rows carry mtime + md5 but NULL options/driver (G20).
    const int64_t file_id = db_.add_file_path(path, mtime, md5);
    // M4: ONE transaction covers symbols + edges for this header (prevents
    // partial writes if graph extraction fails mid-header).
    std::pair<int, int> result;
    {
      Transaction txn = db_.transaction();
      // Extract this header's symbols out of THIS TU's AST (no separate
      // parse), matching cursors against the include SPELLING, not the
      // abspath (G23: cursors' location-file names agree with the spelling).
      result = index_file_notxn(tu, inc.name, file_id);
      // QD-1: extract declaration-level edges for this header from the SAME
      // TU's AST. Symbol rows must exist first (index_file_notxn runs above).
      if (graph_enabled_) {
        db_.delete_edges_for_file(file_id);
        index_edges_notxn(tu, inc.name, file_id);
      }
      txn.commit();
    }
    db_.mark_file_indexed(file_id, mtime);
    ++counts.indexed;
    counts.symbols += result.first;
  }
  return counts;
}

// ---- v7 graph extraction ----------------------------------------------------

namespace {

// CursorKinds whose descendant is a conditional branch for cond_depth tracking.
bool is_cond_cursor(CXCursorKind kind) {
  return kind == CXCursor_IfStmt || kind == CXCursor_ForStmt ||
         kind == CXCursor_WhileStmt || kind == CXCursor_DoStmt ||
         kind == CXCursor_SwitchStmt || kind == CXCursor_CaseStmt ||
         kind == CXCursor_ConditionalOperator;
}

// True when a STRUCT/CLASS_DECL whose clang_getSpecializedCursorTemplate is
// non-null is an explicit INSTANTIATION (`template class Foo<int>;`) rather
// than an explicit SPECIALIZATION (`template <> class Foo<bool> { ... };`).
// Both report is_definition()==true and a non-null specialized template, so the
// stable libclang C API cannot tell them apart directly -- the written syntax
// can. We tokenize the cursor's extent: the token immediately after the
// `template` keyword is `class`/`struct` for an instantiation (optionally after
// `extern`) and `<` for a specialization.
bool is_explicit_instantiation(LibClang &lib, CXCursor cursor) {
  CXTranslationUnit tu = lib.clang_Cursor_getTranslationUnit(cursor);
  if (tu == nullptr) {
    return false;
  }
  const CXSourceRange extent = lib.clang_getCursorExtent(cursor);
  CXToken *tokens = nullptr;
  unsigned n = 0;
  lib.clang_tokenize(tu, extent, &tokens, &n);
  if (tokens == nullptr) {
    return false;
  }
  bool result = false;
  for (unsigned i = 0; i < n; ++i) {
    const std::string s =
        CxString(lib, lib.clang_getTokenSpelling(tu, tokens[i])).str();
    if (s == "template") {
      if (i + 1 < n) {
        const std::string nxt =
            CxString(lib, lib.clang_getTokenSpelling(tu, tokens[i + 1])).str();
        result = (nxt == "class" || nxt == "struct");
      }
      break;
    }
    if (s == "class" || s == "struct") {
      break;
    }
  }
  lib.clang_disposeTokens(tu, tokens, n);
  return result;
}

// Context for the recursive body descent (calls + uses).
struct BodyDescentCtx {
  LibClang *lib = nullptr;
  Storage *db = nullptr;
  int64_t src_id = -1;
  int64_t file_id = -1;
  int cond_depth = 0;
  std::exception_ptr error;
};

// FirstChildCtx + first_child_visitor are shared by the Phase 2 helpers below
// and by recover_overloaded_callee (dependent-call recovery).
struct FirstChildCtx {
  CXCursor out;
  bool found = false;
};
CXChildVisitResult first_child_visitor(CXCursor c, CXCursor /*parent*/,
                                       CXClientData d) noexcept {
  auto *x = static_cast<FirstChildCtx *>(d);
  x->out = c;
  x->found = true;
  return CXChildVisit_Break; // callee is the FIRST child; args follow
}

// ---------------------------------------------------------------------------
// Phase 2: value-source classification helpers (mirrors ast.py)
// ---------------------------------------------------------------------------

// Peel implicit casts, parentheses, and address-of/dereference from `expr`
// to the underlying named sub-expression. At most 16 layers.
// Mirrors ast.py:_peel_expr.
CXCursor peel_expr(LibClang &lib, CXCursor expr) {
  for (int i = 0; i < 16; ++i) {
    const CXCursorKind k = lib.clang_getCursorKind(expr);
    // PAREN_EXPR (111), UNARY_OPERATOR (112), CSTYLE_CAST_EXPR (117)
    // UNEXPOSED_EXPR (0/1) covers implicit casts
    if (k == CXCursor_ParenExpr || k == CXCursor_UnaryOperator ||
        k == (CXCursorKind)117 /*CStyleCast*/ ||
        k == CXCursor_UnexposedExpr || k == (CXCursorKind)1) {
      FirstChildCtx fc{};
      lib.clang_visitChildren(expr, &first_child_visitor, &fc);
      if (fc.found) {
        expr = fc.out;
        continue;
      }
    }
    break;
  }
  return expr;
}

// Strip pointer/reference/cv-qualifiers from `t` and return the USR of the
// record declaration, or "" when there is none (builtins).
// Mirrors ast.py:_record_usr_of_type.
std::string record_usr_of_type(LibClang &lib, CXType t) {
  // clang_getCanonicalType is not wrapped in LibClang; call the C API directly.
  CXType canonical = ::clang_getCanonicalType(t);
  for (int i = 0; i < 8; ++i) {
    const CXTypeKind tk = canonical.kind;
    if (tk == CXType_Pointer || tk == CXType_LValueReference ||
        tk == CXType_RValueReference) {
      canonical = ::clang_getCanonicalType(lib.clang_getPointeeType(canonical));
    } else {
      break;
    }
  }
  const CXCursor decl = lib.clang_getTypeDeclaration(canonical);
  if (lib.clang_Cursor_isNull(decl) ||
      is_invalid_kind(lib.clang_getCursorKind(decl))) {
    return "";
  }
  return CxString(lib, lib.clang_getCursorUSR(decl)).str();
}

// Mirrors ast.py:_type_is_value. True iff `loc_type` holds `dispatch_record_usr`
// by value (exact, non-erased). Sound: pointer/ref fail the RECORD kind gate;
// a handle's wrapper USR never equals the dispatch USR.
bool type_is_value(LibClang &lib, CXType loc_type,
                   const std::string &dispatch_record_usr) {
  if (dispatch_record_usr.empty()) return false;
  CXType c = ::clang_getCanonicalType(loc_type);
  if (c.kind != CXType_Record) return false;
  const CXCursor decl = lib.clang_getTypeDeclaration(c);
  if (lib.clang_Cursor_isNull(decl) ||
      is_invalid_kind(lib.clang_getCursorKind(decl))) {
    return false;
  }
  return CxString(lib, lib.clang_getCursorUSR(decl)).str() == dispatch_record_usr;
}

// Mirrors ast.py:_decl_type_for_expr.
// Returns the DECLARED type of the value source, not the use-site expression type.
// libclang auto-derefs lvalue-references at the call-site: a field "B& br" presents
// as expression-type B.  For DECL_REF_EXPR / MEMBER_REF_EXPR we read
// getCursorType(getCursorReferenced(peeled)) which preserves the reference.
// For CALL_EXPR (call_result) we use getCursorResultType of the callee.
// Falls back to getCursorType(peeled) for anything else (safe).
static CXType decl_type_for_expr(LibClang &lib, CXCursor peeled) {
  const CXCursorKind k = lib.clang_getCursorKind(peeled);
  if (k == CXCursor_DeclRefExpr || k == CXCursor_MemberRefExpr) {
    const CXCursor ref = lib.clang_getCursorReferenced(peeled);
    if (lib.clang_Cursor_isNull(ref)) {
      return lib.clang_getCursorType(peeled);
    }
    return lib.clang_getCursorType(ref);
  }
  if (k == CXCursor_CallExpr || k == (CXCursorKind)128 /*CXXFunctionalCast*/) {
    const CXCursor ref = lib.clang_getCursorReferenced(peeled);
    if (lib.clang_Cursor_isNull(ref)) {
      return lib.clang_getCursorType(peeled);
    }
    return lib.clang_getCursorResultType(ref);
  }
  return lib.clang_getCursorType(peeled);
}

// Result of classify_value_source — mirrors the Python tuple.
struct ValueSource {
  std::string src_kind;               // local|construct|member|global|call_result|literal|this|unknown
  std::string type_usr;               // "" = none
  std::string decl_usr;               // "" = none
  std::string callee_usr;             // "" = none (call_result only)
};

// Classify the provenance of a value expression.
// Mirrors ast.py:_classify_value_source.
ValueSource classify_value_source(LibClang &lib, CXCursor expr) {
  const CXCursor peeled = peel_expr(lib, expr);
  const CXCursorKind k = lib.clang_getCursorKind(peeled);

  // CXXThisExpr (132)
  if (k == CXCursor_CXXThisExpr) {
    const std::string tu = record_usr_of_type(lib, lib.clang_getCursorType(peeled));
    return {"this", tu, tu, ""};
  }

  // DECL_REF_EXPR (101)
  if (k == CXCursor_DeclRefExpr) {
    const CXCursor ref = lib.clang_getCursorReferenced(peeled);
    if (lib.clang_Cursor_isNull(ref)) {
      return {"unknown", "", "", ""};
    }
    const CXCursorKind ref_kind = lib.clang_getCursorKind(ref);
    const std::string decl_usr = CxString(lib, lib.clang_getCursorUSR(ref)).str();
    const std::string type_usr = record_usr_of_type(lib, lib.clang_getCursorType(peeled));
    if (ref_kind == CXCursor_ParmDecl) {
      return {"local", type_usr, decl_usr, ""};
    }
    if (ref_kind == CXCursor_VarDecl) {
      const CXCursor parent = lib.clang_getCursorSemanticParent(ref);
      const CXCursorKind pk = lib.clang_getCursorKind(parent);
      if (pk == CXCursor_FunctionDecl || pk == CXCursor_CXXMethod ||
          pk == CXCursor_Constructor || pk == CXCursor_Destructor ||
          pk == (CXCursorKind)144 /*LambdaExpr*/) {
        return {"local", type_usr, decl_usr, ""};
      }
      return {"global", type_usr, decl_usr, ""};
    }
    return {"unknown", type_usr, decl_usr, ""};
  }

  // MEMBER_REF_EXPR (102)
  if (k == CXCursor_MemberRefExpr) {
    const CXCursor ref = lib.clang_getCursorReferenced(peeled);
    const std::string decl_usr = lib.clang_Cursor_isNull(ref) ? "" :
        CxString(lib, lib.clang_getCursorUSR(ref)).str();
    const std::string type_usr = record_usr_of_type(lib, lib.clang_getCursorType(peeled));
    return {"member", type_usr, decl_usr, ""};
  }

  // CALL_EXPR (103) or CXXFunctionalCastExpr (128)
  if (k == CXCursor_CallExpr || k == (CXCursorKind)128 /*CXXFunctionalCast*/) {
    const CXCursor ref = lib.clang_getCursorReferenced(peeled);
    if (!lib.clang_Cursor_isNull(ref)) {
      const CXCursorKind ref_kind = lib.clang_getCursorKind(ref);
      if (ref_kind == CXCursor_Constructor ||
          ref_kind == (CXCursorKind)26 /*ConversionFunction*/) {
        const std::string type_usr = record_usr_of_type(lib, lib.clang_getCursorType(peeled));
        return {"construct", type_usr, "", ""};
      }
    }
    const std::string type_usr = record_usr_of_type(lib, lib.clang_getCursorType(peeled));
    const std::string callee_usr = lib.clang_Cursor_isNull(ref) ? "" :
        CxString(lib, lib.clang_getCursorUSR(ref)).str();
    return {"call_result", type_usr, "", callee_usr};
  }

  // CXXNewExpr (134)
  if (k == (CXCursorKind)134) {
    const std::string type_usr = record_usr_of_type(lib, lib.clang_getCursorType(peeled));
    return {"construct", type_usr, "", ""};
  }

  // Literals: INTEGER_LITERAL(106) FLOATING_LITERAL(107) STRING_LITERAL(109)
  //           CHARACTER_LITERAL(110) CXXBoolLiteralExpr(130) CXXNullPtrLiteralExpr(131)
  //           GNUNullExpr(123)
  if (k == CXCursor_IntegerLiteral || k == CXCursor_FloatingLiteral ||
      k == CXCursor_StringLiteral || k == CXCursor_CharacterLiteral ||
      k == (CXCursorKind)130 /*CXXBoolLiteral*/ ||
      k == (CXCursorKind)131 /*CXXNullPtrLiteral*/ ||
      k == (CXCursorKind)123 /*GNUNullExpr*/) {
    return {"literal", "", "", ""};
  }

  return {"unknown", "", "", ""};
}

// Return the receiver sub-expression of a C++ member call (the base object),
// or a null cursor for free-function calls or no-receiver implicit-this calls.
// Mirrors ast.py:_receiver_subexpr.
CXCursor receiver_subexpr(LibClang &lib, CXCursor call) {
  FirstChildCtx fc{};
  lib.clang_visitChildren(call, &first_child_visitor, &fc);
  if (!fc.found) {
    return lib.clang_getNullCursor();
  }
  const CXCursor peeled_first = peel_expr(lib, fc.out);
  if (lib.clang_getCursorKind(peeled_first) == CXCursor_MemberRefExpr) {
    // The receiver is the MEMBER_REF_EXPR's first child
    FirstChildCtx mc{};
    lib.clang_visitChildren(peeled_first, &first_child_visitor, &mc);
    if (mc.found) {
      return mc.out;
    }
    // Implicit this — no explicit child
    return lib.clang_getNullCursor();
  }
  return lib.clang_getNullCursor();
}

// ---------------------------------------------------------------------------

// Emit a calls or uses edge_site for a cursor inside a body descent.
// kind_id: 1=calls, 7=uses. The edge is upserted (ON CONFLICT increments
// count); the site is OR IGNORE (same site visited twice is one row).
// Returns the stable edge.id for further linkage (call_arg rows).
int64_t emit_body_edge(BodyDescentCtx *ctx, LibClang &lib, CXCursor cursor,
                       int64_t dst_id, int kind_id) {
  Edge e;
  e.src_id = ctx->src_id;
  e.dst_id = dst_id;
  e.kind = kind_id;
  e.count = 1;
  const int64_t edge_id = ctx->db->add_edge(e);

  unsigned line = 0;
  unsigned col = 0;
  unsigned offset = 0;
  CXFile file_handle = nullptr;
  lib.clang_getExpansionLocation(lib.clang_getCursorLocation(cursor),
                                 &file_handle, &line, &col, &offset);
  EdgeSite site;
  site.edge_id = edge_id;
  site.file_id = ctx->file_id;
  site.line = static_cast<int64_t>(line);
  site.col = static_cast<int64_t>(col);
  site.conditional = ctx->cond_depth > 0 ? 1 : 0;
  ctx->db->add_edge_site(site);
  return edge_id;
}

// Emit a calls edge with Phase 2/3 receiver provenance on the edge_site.
// Returns the edge.id (needed for add_call_arg).
int64_t emit_call_edge(BodyDescentCtx *ctx, LibClang &lib, CXCursor cursor,
                       int64_t dst_id,
                       const std::string &recv_src_kind,
                       const std::string &recv_type_usr,
                       const std::string &recv_decl_usr,
                       std::optional<int64_t> recv_param_pos = std::nullopt,
                       std::optional<int64_t> recv_type_is_value = std::nullopt) {
  Edge e;
  e.src_id = ctx->src_id;
  e.dst_id = dst_id;
  e.kind = 1; // calls
  e.count = 1;
  const int64_t edge_id = ctx->db->add_edge(e);

  unsigned line = 0;
  unsigned col = 0;
  unsigned offset = 0;
  CXFile file_handle = nullptr;
  lib.clang_getExpansionLocation(lib.clang_getCursorLocation(cursor),
                                 &file_handle, &line, &col, &offset);
  EdgeSite site;
  site.edge_id = edge_id;
  site.file_id = ctx->file_id;
  site.line = static_cast<int64_t>(line);
  site.col = static_cast<int64_t>(col);
  site.conditional = ctx->cond_depth > 0 ? 1 : 0;
  if (!recv_src_kind.empty()) {
    site.recv_src_kind = recv_src_kind;
  }
  if (!recv_type_usr.empty()) {
    site.recv_type_usr = recv_type_usr;
  }
  if (!recv_decl_usr.empty()) {
    site.recv_decl_usr = recv_decl_usr;
  }
  if (recv_param_pos.has_value()) {
    site.recv_param_pos = recv_param_pos;
  }
  if (recv_type_is_value.has_value()) {
    site.recv_type_is_value = recv_type_is_value;
  }
  ctx->db->add_edge_site(site);
  return edge_id;
}

// Emit call_arg rows for all non-literal positional args of a CALL_EXPR.
// Mirrors the Phase 2 loop in ast.py:_body_descent.
void emit_call_args(BodyDescentCtx *ctx, LibClang &lib, CXCursor call,
                    int64_t edge_id, unsigned line, unsigned col) {
  const int nargs = lib.clang_Cursor_getNumArguments(call);
  for (int pos = 0; pos < nargs; ++pos) {
    const CXCursor arg = lib.clang_Cursor_getArgument(call, static_cast<unsigned>(pos));
    if (lib.clang_Cursor_isNull(arg)) {
      continue;
    }
    const ValueSource vs = classify_value_source(lib, arg);
    if (vs.src_kind == "literal") {
      continue;
    }
    CallArg ca;
    ca.edge_id = edge_id;
    ca.file_id = ctx->file_id;
    ca.line = static_cast<int64_t>(line);
    ca.col = static_cast<int64_t>(col);
    ca.position = static_cast<int64_t>(pos);
    ca.src_kind = vs.src_kind;
    if (!vs.type_usr.empty()) {
      ca.type_usr = vs.type_usr;
    }
    if (!vs.decl_usr.empty()) {
      ca.decl_usr = vs.decl_usr;
    }
    if (!vs.callee_usr.empty()) {
      ca.callee_usr = vs.callee_usr;
    }
    // Phase 3a: compute type_is_value for value-eligible arg kinds,
    // including "local" (to distinguish value-typed locals from param re-passing).
    // Use the declared type of the underlying decl (not the use-site expr type
    // which auto-derefs lvalue-references in libclang).
    if ((vs.src_kind == "member" || vs.src_kind == "global" ||
         vs.src_kind == "call_result" || vs.src_kind == "local") && !vs.type_usr.empty()) {
      const CXCursor peeled_arg = peel_expr(lib, arg);
      ca.type_is_value = type_is_value(lib, decl_type_for_expr(lib, peeled_arg),
                                       vs.type_usr) ? 1 : 0;
    }
    ctx->db->add_call_arg(ca);
  }
}

// --- dependent-call recovery ------------------------------------------------
// A call to a dependent/overloaded name inside a template body (e.g.
// `combine(a, b)` in Stack<T>::summary) has a null getCursorReferenced, even
// though the call IS present in the AST. The callee sub-expression still carries
// an OverloadedDeclRef listing the candidate declarations. When that set names
// exactly ONE declaration we recover the callee; ambiguous sets (stdlib
// to_string, etc.) are left unresolved so we never guess a wrong target.
// Note: FirstChildCtx + first_child_visitor defined earlier (above Phase 2 helpers).
struct OverloadRefCtx {
  LibClang *lib;
  CXCursor out;
  bool found = false;
};
CXChildVisitResult overload_ref_visitor(CXCursor c, CXCursor /*parent*/,
                                        CXClientData d) noexcept {
  auto *x = static_cast<OverloadRefCtx *>(d);
  if (x->lib->clang_getCursorKind(c) == CXCursor_OverloadedDeclRef) {
    x->out = c;
    x->found = true;
    return CXChildVisit_Break;
  }
  return CXChildVisit_Recurse;
}

// Returns the unique overloaded declaration, or a null cursor when the callee
// cannot be unambiguously recovered. Only the FIRST child (the callee position)
// is searched, so an argument that is itself an overloaded name is not mistaken
// for the callee.
CXCursor recover_overloaded_callee(LibClang &lib, CXCursor call) {
  FirstChildCtx fc{};
  lib.clang_visitChildren(call, &first_child_visitor, &fc);
  if (!fc.found) {
    return lib.clang_getNullCursor();
  }
  CXCursor odr = lib.clang_getNullCursor();
  if (lib.clang_getCursorKind(fc.out) == CXCursor_OverloadedDeclRef) {
    odr = fc.out;
  } else {
    OverloadRefCtx oc{&lib, {}, false};
    lib.clang_visitChildren(fc.out, &overload_ref_visitor, &oc);
    if (oc.found) {
      odr = oc.out;
    }
  }
  if (lib.clang_Cursor_isNull(odr)) {
    return lib.clang_getNullCursor();
  }
  if (lib.clang_getNumOverloadedDecls(odr) == 1) {
    return lib.clang_getOverloadedDecl(odr, 0);
  }
  return lib.clang_getNullCursor();
}

// ADR-004: mint the X<int> type node, write its template_arg rows, and add
// instantiates + method_of edges for an implicit template instantiation.
// Mirror of Python _mint_instantiation_nodes().
//
// ref             -- the callee reference cursor (X<int>::method)
// member_id       -- already-minted symbol id for X<int>::method (dst_id)
// prim_member_id  -- symbol id of the primary template method X::method
//
// Steps:
//   (b) instantiates(5): member_id -> prim_member_id
//   (c) mint X<int> TYPE node from ref's semantic parent
//   (d) method_of(9):    member_id -> type_id
//   (e) instantiates(5): type_id   -> class_primary_id (guarded)
//   (f) template_arg rows on the TYPE node via clang_Type_getTemplateArgumentAsType
//       (TYPE args only; method-template type API returns 0/null args — logged)
void mint_instantiation_nodes(LibClang &lib, Storage &db,
                              const CXCursor &ref,
                              int64_t member_id,
                              int64_t prim_member_id) {
  // (b) member instantiates -> primary method
  Edge inst_b;
  inst_b.src_id = member_id;
  inst_b.dst_id = prim_member_id;
  inst_b.kind = 5; // instantiates
  inst_b.count = 1;
  db.add_edge(inst_b);

  // (c) mint X<int> TYPE node from semantic parent of ref
  const CXCursor parent = lib.clang_getCursorSemanticParent(ref);
  if (lib.clang_Cursor_isNull(parent) ||
      is_invalid_kind(lib.clang_getCursorKind(parent))) {
    return;
  }
  const std::string type_usr =
      CxString(lib, lib.clang_getCursorUSR(parent)).str();
  if (type_usr.empty()) {
    return;
  }
  const std::string parent_spelling =
      CxString(lib, lib.clang_getCursorSpelling(parent)).str();
  const std::string parent_qual = qualified_name(lib, parent);
  const std::string parent_display =
      CxString(lib, lib.clang_getCursorDisplayName(parent)).str();
  const std::string parent_kind = stub_kind(lib, parent);
  const RefDeclLoc tdl = ref_decl_loc(lib, db, parent);
  const int64_t type_id = db.mint_symbol_id(
      type_usr, parent_spelling, parent_qual, parent_display,
      parent_kind, tdl.file_id, tdl.line, tdl.col, tdl.path,
      /*is_instantiation=*/true);

  // (d) method_of(9): member_id -> type_id
  Edge mo;
  mo.src_id = member_id;
  mo.dst_id = type_id;
  mo.kind = 9; // method_of
  mo.count = 1;
  db.add_edge(mo);

  // (e) instantiates(5): type_id -> class primary, guarded by primary indexed
  const CXCursor class_primary =
      lib.clang_getSpecializedCursorTemplate(parent);
  if (!lib.clang_Cursor_isNull(class_primary) &&
      !is_invalid_kind(lib.clang_getCursorKind(class_primary))) {
    const std::string class_prim_usr =
        CxString(lib, lib.clang_getCursorUSR(class_primary)).str();
    if (!class_prim_usr.empty() && class_prim_usr != type_usr) {
      const auto class_prim_sym = db.lookup_symbol(class_prim_usr);
      if (class_prim_sym) {
        Edge inst_e;
        inst_e.src_id = type_id;
        inst_e.dst_id = class_prim_sym->id;
        inst_e.kind = 5; // instantiates
        inst_e.count = 1;
        db.add_edge(inst_e);
      }
    }
  }

  // (f) template_arg rows on TYPE node via clang_Type_getTemplateArgumentAsType
  // (TYPE args only -- same as VAR_DECL B3 pattern at ast.cpp:1280).
  // For a method template the type API returns nargs <= 0; skip silently.
  const CXType parent_type = lib.clang_getCursorType(parent);
  const int nargs = lib.clang_Type_getNumTemplateArguments(parent_type);
  if (nargs <= 0) {
    // Method-template or type API unavailable: node+edges already emitted.
    // This is the ADR-004 §1b known limitation -- no token-extent parse here.
    return;
  }
  for (int ai = 0; ai < nargs; ++ai) {
    const CXType arg_type = lib.clang_Type_getTemplateArgumentAsType(
        parent_type, static_cast<unsigned>(ai));
    TemplateArg ta;
    ta.owner_id = type_id;
    ta.position = static_cast<int64_t>(ai);
    ta.arg_kind = 1; // TYPE (only kind available via type API)
    const std::string spelling =
        CxString(lib, lib.clang_getTypeSpelling(arg_type)).str();
    if (!spelling.empty()) {
      ta.literal = spelling;
    }
    // Try to resolve the arg type to an indexed symbol (ref_id FK).
    const CXCursor arg_decl = lib.clang_getTypeDeclaration(arg_type);
    if (!lib.clang_Cursor_isNull(arg_decl) &&
        !is_invalid_kind(lib.clang_getCursorKind(arg_decl))) {
      const std::string ref_usr =
          CxString(lib, lib.clang_getCursorUSR(arg_decl)).str();
      if (!ref_usr.empty()) {
        if (const auto rsym = db.lookup_symbol(ref_usr)) {
          ta.ref_id = rsym->id;
        }
      }
    }
    db.add_template_arg(ta);
  }
}

// Non-recursive entry point: visits all children via clang_visitChildren,
// capturing CALL_EXPR (calls) + DECL_REF_EXPR / MEMBER_REF_EXPR (uses)
// nodes and recursing depth-first.
CXChildVisitResult body_descent_visitor(CXCursor cursor, CXCursor /*parent*/,
                                        CXClientData data) noexcept {
  auto *ctx = static_cast<BodyDescentCtx *>(data);
  try {
    LibClang &lib = *ctx->lib;
    const CXCursorKind kind = lib.clang_getCursorKind(cursor);

    if (kind == CXCursor_CallExpr) {
      // Get callee USR via getCursorReferenced. Mint-stub if not yet indexed.
      CXCursor ref = lib.clang_getCursorReferenced(cursor);
      bool recovered = false;
      if (lib.clang_Cursor_isNull(ref)) {
        // Dependent/overloaded callee inside a template body (e.g.
        // `combine(a, b)` in Stack<T>::summary): recover it from the callee's
        // single-overload OverloadedDeclRef. See recover_overloaded_callee.
        ref = recover_overloaded_callee(lib, cursor);
        recovered = !lib.clang_Cursor_isNull(ref);
      }
      if (!lib.clang_Cursor_isNull(ref)) {
        const std::string callee_usr =
            CxString(lib, lib.clang_getCursorUSR(ref)).str();
        if (!callee_usr.empty()) {
          // Resolved calls mint a stub for an unindexed target; RECOVERED
          // (dependent) calls only link to an already-indexed target, so a
          // single-overload stdlib call never mints a stub.
          int64_t dst_id = -1;
          if (recovered) {
            if (const auto dst = ctx->db->lookup_symbol(callee_usr)) {
              dst_id = dst->id;
            }
          } else {
            const RefDeclLoc dl = ref_decl_loc(lib, *ctx->db, ref);
            // Pre-check: is this an instantiation member? Set is_instantiation=1
            // on the mint if the callee has a specialized parent.
            const CXCursor pre_primary =
                lib.clang_getSpecializedCursorTemplate(ref);
            const bool is_inst_member =
                !lib.clang_Cursor_isNull(pre_primary) &&
                !is_invalid_kind(lib.clang_getCursorKind(pre_primary)) &&
                [&]() {
                  const std::string pp_usr =
                      CxString(lib, lib.clang_getCursorUSR(pre_primary)).str();
                  return !pp_usr.empty() && pp_usr != callee_usr;
                }();
            dst_id = ctx->db->mint_symbol_id(
                callee_usr,
                CxString(lib, lib.clang_getCursorSpelling(ref)).str(),
                qualified_name(lib, ref),
                CxString(lib, lib.clang_getCursorDisplayName(ref)).str(),
                stub_kind(lib, ref), dl.file_id, dl.line, dl.col, dl.path,
                /*is_instantiation=*/is_inst_member);
          }
          if (dst_id >= 0) {
            // Phase 2: compute receiver provenance for member calls
            std::string recv_src_kind;
            std::string recv_type_usr;
            std::string recv_decl_usr;
            std::optional<int64_t> recv_param_pos;
            const CXCursor recv_expr = receiver_subexpr(lib, cursor);
            if (!lib.clang_Cursor_isNull(recv_expr)) {
              const ValueSource rv = classify_value_source(lib, recv_expr);
              recv_src_kind = rv.src_kind;
              recv_type_usr = rv.type_usr;
              recv_decl_usr = rv.decl_usr;
              // If receiver is a PARM_DECL, record its 0-based parameter
              // position so the Gamma engine can do position-indexed binding.
              if (recv_src_kind == "local" && !recv_decl_usr.empty()) {
                // Peel to the DECL_REF_EXPR to get the referenced ParmDecl.
                CXCursor peeled_recv = lib.clang_getCursorReferenced(recv_expr);
                // Handle nested peeling (implicit casts etc.)
                while (!lib.clang_Cursor_isNull(peeled_recv) &&
                       lib.clang_getCursorKind(peeled_recv) != CXCursor_ParmDecl &&
                       lib.clang_getCursorKind(peeled_recv) != CXCursor_VarDecl) {
                  peeled_recv = lib.clang_getCursorReferenced(peeled_recv);
                }
                if (!lib.clang_Cursor_isNull(peeled_recv) &&
                    lib.clang_getCursorKind(peeled_recv) == CXCursor_ParmDecl) {
                  // Find position by iterating parent function's parameters.
                  const CXCursor fn_parent =
                      lib.clang_getCursorSemanticParent(peeled_recv);
                  if (!lib.clang_Cursor_isNull(fn_parent)) {
                    const int nparams =
                        lib.clang_Cursor_getNumArguments(fn_parent);
                    const std::string parm_usr = recv_decl_usr; // already set
                    for (int pi = 0; pi < nparams; ++pi) {
                      const CXCursor p =
                          lib.clang_Cursor_getArgument(fn_parent,
                                                       static_cast<unsigned>(pi));
                      const std::string p_usr =
                          CxString(lib, lib.clang_getCursorUSR(p)).str();
                      if (p_usr == parm_usr) {
                        recv_param_pos = static_cast<int64_t>(pi);
                        break;
                      }
                    }
                  }
                }
              }
            } else {
              // Implicit this (no explicit receiver child)
              const CXCursorKind ref_kind = lib.clang_getCursorKind(ref);
              if (ref_kind == CXCursor_CXXMethod ||
                  ref_kind == CXCursor_Constructor ||
                  ref_kind == CXCursor_Destructor) {
                const CXCursor owner = lib.clang_getCursorSemanticParent(ref);
                if (!lib.clang_Cursor_isNull(owner)) {
                  const std::string owner_usr =
                      CxString(lib, lib.clang_getCursorUSR(owner)).str();
                  recv_src_kind = "this";
                  recv_type_usr = owner_usr;
                  recv_decl_usr = owner_usr;
                }
              }
            }
            // Phase 3a: compute recv_type_is_value for value-eligible src_kinds.
            std::optional<int64_t> recv_type_is_value_opt;
            if ((recv_src_kind == "member" || recv_src_kind == "global" ||
                 recv_src_kind == "call_result") &&
                !lib.clang_Cursor_isNull(recv_expr)) {
              // dispatch_usr = USR of the class owning the virtual method.
              std::string dispatch_usr;
              const CXCursorKind ref_kind2 = lib.clang_getCursorKind(ref);
              if (ref_kind2 == CXCursor_CXXMethod ||
                  ref_kind2 == CXCursor_Constructor ||
                  ref_kind2 == CXCursor_Destructor ||
                  ref_kind2 == CXCursor_ConversionFunction) {
                const CXCursor owner2 = lib.clang_getCursorSemanticParent(ref);
                if (!lib.clang_Cursor_isNull(owner2)) {
                  dispatch_usr =
                      CxString(lib, lib.clang_getCursorUSR(owner2)).str();
                }
              }
              // Use the DECLARED type of the underlying decl (not the use-site
              // expression type, which auto-derefs references in libclang).
              const CXCursor peeled_recv = peel_expr(lib, recv_expr);
              recv_type_is_value_opt =
                  type_is_value(lib, decl_type_for_expr(lib, peeled_recv),
                                dispatch_usr) ? 1 : 0;
            }
            unsigned call_line = 0, call_col = 0, call_off = 0;
            CXFile call_fh = nullptr;
            lib.clang_getExpansionLocation(lib.clang_getCursorLocation(cursor),
                                           &call_fh, &call_line, &call_col, &call_off);
            const int64_t edge_id = emit_call_edge(
                ctx, lib, cursor, dst_id,
                recv_src_kind, recv_type_usr, recv_decl_usr, recv_param_pos,
                recv_type_is_value_opt);
            emit_call_args(ctx, lib, cursor, edge_id, call_line, call_col);

            // B3 instantiates (kind=5): when the callee is a template
            // specialization, emit an edge to the primary template symbol.
            // clang_getSpecializedCursorTemplate returns the primary (or a
            // partial specialization) for both function and class templates.
            // For a recovered primary template this is a no-op (no parent).
            const CXCursor primary =
                lib.clang_getSpecializedCursorTemplate(ref);
            if (!lib.clang_Cursor_isNull(primary) &&
                !is_invalid_kind(lib.clang_getCursorKind(primary))) {
              const std::string prim_usr =
                  CxString(lib, lib.clang_getCursorUSR(primary)).str();
              if (!prim_usr.empty() && prim_usr != callee_usr) {
                // Only emit when primary is already indexed (no stubs for
                // stdlib templates — prevents inflating the stub count for
                // std::vector, std::move, etc.).
                const auto prim_sym = ctx->db->lookup_symbol(prim_usr);
                if (prim_sym) {
                  Edge inst;
                  inst.src_id = ctx->src_id;
                  inst.dst_id = prim_sym->id;
                  inst.kind = 5; // instantiates
                  inst.count = 1;
                  ctx->db->add_edge(inst);
                  // ADR-004 instantiation-member promotion block.
                  // Runs alongside the existing caller->primary edge above.
                  mint_instantiation_nodes(lib, *ctx->db, ref, dst_id,
                                           prim_sym->id);
                }
              }
            }
          }
        }
      }
    } else if (kind == CXCursor_DeclRefExpr || kind == CXCursor_MemberRefExpr) {
      // B2 uses: DECL_REF_EXPR references a non-function indexed symbol
      // (variable, field, enum-constant, etc.).  Only emit for symbols
      // already in the DB (lookup, no stub) — prevents creating stubs for
      // every standard-library constant touched in the body.
      const CXCursor ref = lib.clang_getCursorReferenced(cursor);
      if (!lib.clang_Cursor_isNull(ref)) {
        const CXCursorKind ref_kind = lib.clang_getCursorKind(ref);
        // Exclude function-like (those produce calls edges above); include
        // member fields, variables, enum-constants, etc.
        if (!is_function_like(ref_kind) && ref_kind != CXCursor_CXXMethod &&
            ref_kind != CXCursor_Constructor &&
            ref_kind != CXCursor_Destructor) {
          const std::string ref_usr =
              CxString(lib, lib.clang_getCursorUSR(ref)).str();
          if (!ref_usr.empty()) {
            const auto dst_sym = ctx->db->lookup_symbol(ref_usr);
            if (dst_sym) {
              emit_body_edge(ctx, lib, cursor, dst_sym->id, 7 /* uses */);
            }
          }
        }
      }
    } else if (kind == CXCursor_VarDecl) {
      // B2 uses: a LOCAL variable's declared type names a record/enum/typedef
      // -> uses edge (src=enclosing fn). `Conf local;` counts as the function
      // using Conf even when no method is called on it.
      emit_type_use(lib, *ctx->db, ctx->src_id, lib.clang_getCursorType(cursor),
                    ctx->file_id, cursor, ctx->cond_depth > 0 ? 1 : 0);
      // B3 class-template instantiates (kind=5): when a variable's type is a
      // class-template instantiation, emit instantiates (src=enclosing fn,
      // dst=primary template) + template_arg rows.
      // Only emit when the primary is already indexed (no stubs for stdlib
      // types — prevents inflating stub count for std::string, std::vector).
      const CXType var_type = lib.clang_getCursorType(cursor);
      const int nargs = lib.clang_Type_getNumTemplateArguments(var_type);
      if (nargs > 0) {
        const CXCursor type_decl = lib.clang_getTypeDeclaration(var_type);
        if (!lib.clang_Cursor_isNull(type_decl) &&
            !is_invalid_kind(lib.clang_getCursorKind(type_decl))) {
          const CXCursor primary =
              lib.clang_getSpecializedCursorTemplate(type_decl);
          if (!lib.clang_Cursor_isNull(primary) &&
              !is_invalid_kind(lib.clang_getCursorKind(primary))) {
            const std::string prim_usr =
                CxString(lib, lib.clang_getCursorUSR(primary)).str();
            if (!prim_usr.empty()) {
              const auto prim_sym = ctx->db->lookup_symbol(prim_usr);
              if (prim_sym) {
                // instantiates edge: fn -> primary template
                Edge inst;
                inst.src_id = ctx->src_id;
                inst.dst_id = prim_sym->id;
                inst.kind = 5; // instantiates
                inst.count = 1;
                ctx->db->add_edge(inst);

                // template_arg rows: owner_id = src_id (the using function),
                // recording which types this instantiation uses.
                for (int ai = 0; ai < nargs; ++ai) {
                  const CXType arg_type =
                      lib.clang_Type_getTemplateArgumentAsType(
                          var_type, static_cast<unsigned>(ai));
                  TemplateArg ta;
                  ta.owner_id = ctx->src_id;
                  ta.position = static_cast<int64_t>(ai);
                  ta.arg_kind = 1; // TYPE
                  // Always store the type spelling so the binding is
                  // distinguishable even for builtins with no declaration.
                  const std::string spelling =
                      CxString(lib, lib.clang_getTypeSpelling(arg_type)).str();
                  if (!spelling.empty()) {
                    ta.literal = spelling;
                  }
                  // Try to resolve the arg type to an indexed symbol.
                  const CXCursor arg_decl =
                      lib.clang_getTypeDeclaration(arg_type);
                  if (!lib.clang_Cursor_isNull(arg_decl) &&
                      !is_invalid_kind(lib.clang_getCursorKind(arg_decl))) {
                    const std::string ref_usr =
                        CxString(lib, lib.clang_getCursorUSR(arg_decl)).str();
                    if (!ref_usr.empty()) {
                      if (const auto rsym = ctx->db->lookup_symbol(ref_usr)) {
                        ta.ref_id = rsym->id;
                      }
                    }
                  }
                  ctx->db->add_template_arg(ta);
                }
              }
            }
          }
        }
      }
    }

    // Recurse into children, tracking cond_depth.
    const bool is_cond = is_cond_cursor(kind);
    if (is_cond) {
      ++ctx->cond_depth;
    }
    lib.clang_visitChildren(cursor, &body_descent_visitor, ctx);
    if (is_cond) {
      --ctx->cond_depth;
    }
  } catch (...) {
    ctx->error = std::current_exception();
    return CXChildVisit_Break;
  }
  return CXChildVisit_Continue; // children already visited recursively above
}

} // namespace

void AstIndexer::body_descent(CXCursor fn_cursor, int64_t src_id,
                              int64_t file_id) {
  LibClang &lib = LibClang::instance();
  BodyDescentCtx ctx;
  ctx.lib = &lib;
  ctx.db = &db_;
  ctx.src_id = src_id;
  ctx.file_id = file_id;
  ctx.cond_depth = 0;
  lib.clang_visitChildren(fn_cursor, &body_descent_visitor, &ctx);
  if (ctx.error) {
    std::rethrow_exception(ctx.error);
  }
}

// M4: txn-free inner work — caller MUST own an open transaction.
// graph_enabled_ check and edge deletion are ALSO done by caller (index_edges).
void AstIndexer::index_edges_notxn(const ParsedTu &tu,
                                   const std::string &filename,
                                   int64_t file_id) {
  LibClang &lib = LibClang::instance();

  // B1: declaration-level edges. Use the parent-aware walk so that
  // CXX_BASE_SPECIFIER handlers can get the enclosing record from the walk
  // parent (spec §1.4: semantic_parent and lexical_parent are both NULL on
  // that cursor kind — probed in geometry.hpp Circle:Shape).
  for_file_cursors_p(tu, filename, [&](CXCursor cursor, CXCursor walk_parent) {
    const CXCursorKind ck = lib.clang_getCursorKind(cursor);

    // -- contains (kind=3): namespace/record → child symbol ---------------
    // Emitted FIRST so it fires regardless of which specific handler runs
    // below (each handler may early-return before reaching the end).
    // src = the enclosing namespace or record; dst = this cursor.
    // Covers: NAMESPACE_DECL → any indexed child,
    //         record/class_template → nested type/enum/typedef/union.
    // Does NOT duplicate field_of (members) or method_of (methods) —
    // those emit child→parent, while contains emits parent→child.
    {
      const CXCursorKind pk = lib.clang_getCursorKind(walk_parent);
      const bool parent_is_ns = (pk == CXCursor_Namespace);
      const bool parent_is_record =
          (pk == CXCursor_ClassDecl || pk == CXCursor_StructDecl ||
           pk == CXCursor_ClassTemplate || pk == CXCursor_UnionDecl);
      // For a namespace parent: any indexable child qualifies.
      // For a record parent: only nested types/enums/typedefs qualify
      //   (fields + methods are covered by field_of/method_of).
      const bool child_is_nested_type =
          (ck == CXCursor_ClassDecl || ck == CXCursor_StructDecl ||
           ck == CXCursor_UnionDecl || ck == CXCursor_EnumDecl ||
           ck == CXCursor_TypedefDecl || ck == CXCursor_TypeAliasDecl ||
           ck == CXCursor_ClassTemplate || ck == CXCursor_FunctionTemplate);
      const bool emit = parent_is_ns || (parent_is_record && child_is_nested_type);
      if (emit) {
        const std::string child_usr =
            CxString(lib, lib.clang_getCursorUSR(cursor)).str();
        const std::string parent_usr =
            CxString(lib, lib.clang_getCursorUSR(walk_parent)).str();
        if (!child_usr.empty() && !parent_usr.empty()) {
          const auto child_sym = db_.lookup_symbol(child_usr);
          const auto parent_sym = db_.lookup_symbol(parent_usr);
          if (child_sym && parent_sym) {
            Edge e;
            e.src_id = parent_sym->id;
            e.dst_id = child_sym->id;
            e.kind = 3; // contains
            e.count = 1;
            db_.add_edge(e);
          }
        }
      }
    }

    // -- uses (kind=7): TYPE references in signatures / fields / vars ------
    // A class named only as a parameter, return, field, variable, or
    // typedef-underlying type never appears as a body DECL_REF_EXPR, so the
    // body-descent `uses` pass misses it. Emit those signature-level uses
    // here. Emitted alongside contains (before any handler's early return).
    // Local-variable types inside bodies are handled in body_descent_visitor;
    // the walk here does not descend into bodies.
    if (is_function_like(ck)) {
      const std::string fn_usr =
          CxString(lib, lib.clang_getCursorUSR(cursor)).str();
      if (!fn_usr.empty()) {
        const auto fn_sym = db_.lookup_symbol(fn_usr);
        if (fn_sym) {
          // return type (constructors/destructors have none worth recording)
          if (ck != CXCursor_Constructor && ck != CXCursor_Destructor) {
            emit_type_use(lib, db_, fn_sym->id,
                          lib.clang_getCursorResultType(cursor), file_id,
                          cursor, 0);
          }
          const int nargs = lib.clang_Cursor_getNumArguments(cursor);
          for (int ai = 0; ai < nargs; ++ai) {
            const CXCursor arg =
                lib.clang_Cursor_getArgument(cursor, static_cast<unsigned>(ai));
            emit_type_use(lib, db_, fn_sym->id, lib.clang_getCursorType(arg),
                          file_id, arg, 0);
          }
        }
      }
    } else if (ck == CXCursor_FieldDecl || ck == CXCursor_VarDecl) {
      // FIELD_DECL: field type. VAR_DECL: file-scope variable type (locals are
      // reached via body_descent). src = the field/variable symbol itself.
      const std::string sym_usr =
          CxString(lib, lib.clang_getCursorUSR(cursor)).str();
      if (!sym_usr.empty()) {
        const auto sym = db_.lookup_symbol(sym_usr);
        if (sym) {
          emit_type_use(lib, db_, sym->id, lib.clang_getCursorType(cursor),
                        file_id, cursor, 0);
        }
      }
    } else if (ck == CXCursor_TypedefDecl || ck == CXCursor_TypeAliasDecl) {
      const std::string td_usr =
          CxString(lib, lib.clang_getCursorUSR(cursor)).str();
      if (!td_usr.empty()) {
        const auto td_sym = db_.lookup_symbol(td_usr);
        if (td_sym) {
          emit_type_use(lib, db_, td_sym->id,
                        lib.clang_getTypedefDeclUnderlyingType(cursor),
                        file_id, cursor, 0);
        }
      }
    }

    // -- CXX_BASE_SPECIFIER: inherits ----------------------------------
    // Derived class is the enclosing record from the walk parent, NOT from
    // semantic_parent (which is NULL — spec §1.4 gotcha).
    if (ck == CXCursor_CXXBaseSpecifier) {
      // walk_parent is the CLASS_DECL / STRUCT_DECL cursor that contains this
      // base-specifier; its USR is the derived class.
      const CXCursorKind pk = lib.clang_getCursorKind(walk_parent);
      if (pk != CXCursor_ClassDecl && pk != CXCursor_StructDecl) {
        return; // unexpected parent; skip
      }
      const std::string derived_usr =
          CxString(lib, lib.clang_getCursorUSR(walk_parent)).str();
      if (derived_usr.empty()) {
        return;
      }
      const CXCursor base_ref = lib.clang_getCursorReferenced(cursor);
      if (lib.clang_Cursor_isNull(base_ref)) {
        return;
      }
      const std::string base_usr =
          CxString(lib, lib.clang_getCursorUSR(base_ref)).str();
      if (base_usr.empty()) {
        return;
      }
      const auto src_sym = db_.lookup_symbol(derived_usr);
      if (!src_sym) {
        return;
      }
      const RefDeclLoc base_dl = ref_decl_loc(lib, db_, base_ref);
      const int64_t dst_id = db_.mint_symbol_id(
          base_usr,
          CxString(lib, lib.clang_getCursorSpelling(base_ref)).str(),
          qualified_name(lib, base_ref),
          CxString(lib, lib.clang_getCursorDisplayName(base_ref)).str(),
          stub_kind(lib, base_ref), base_dl.file_id, base_dl.line, base_dl.col,
          base_dl.path);
      Edge e;
      e.src_id = src_sym->id;
      e.dst_id = dst_id;
      e.kind = 2; // inherits
      e.count = 1;
      const CX_CXXAccessSpecifier acc = lib.clang_getCXXAccessSpecifier(cursor);
      if (acc == CX_CXXPublic) {
        e.base_access = 1;
      } else if (acc == CX_CXXProtected) {
        e.base_access = 2;
      } else if (acc == CX_CXXPrivate) {
        e.base_access = 3;
      }
      e.is_virtual = static_cast<int64_t>(lib.clang_isVirtualBase(cursor));
      db_.add_edge(e);
      return;
    }

    // -- FIELD_DECL: field_of ------------------------------------------
    if (ck == CXCursor_FieldDecl) {
      const std::string member_usr =
          CxString(lib, lib.clang_getCursorUSR(cursor)).str();
      if (member_usr.empty()) {
        return;
      }
      const CXCursor owner = lib.clang_getCursorSemanticParent(cursor);
      if (lib.clang_Cursor_isNull(owner) ||
          is_invalid_kind(lib.clang_getCursorKind(owner))) {
        return;
      }
      const std::string owner_usr =
          CxString(lib, lib.clang_getCursorUSR(owner)).str();
      if (owner_usr.empty()) {
        return;
      }
      const auto src_sym = db_.lookup_symbol(member_usr);
      const auto dst_sym = db_.lookup_symbol(owner_usr);
      if (!src_sym || !dst_sym) {
        return;
      }
      Edge e;
      e.src_id = src_sym->id;
      e.dst_id = dst_sym->id;
      e.kind = 8; // field_of
      e.count = 1;
      db_.add_edge(e);
      return;
    }

    // -- CXX_METHOD/CONSTRUCTOR/DESTRUCTOR: method_of ------------------
    if (ck == CXCursor_CXXMethod || ck == CXCursor_Constructor ||
        ck == CXCursor_Destructor) {
      const std::string method_usr =
          CxString(lib, lib.clang_getCursorUSR(cursor)).str();
      if (method_usr.empty()) {
        return;
      }
      const CXCursor owner = lib.clang_getCursorSemanticParent(cursor);
      if (lib.clang_Cursor_isNull(owner) ||
          is_invalid_kind(lib.clang_getCursorKind(owner))) {
        return;
      }
      const std::string owner_usr =
          CxString(lib, lib.clang_getCursorUSR(owner)).str();
      if (owner_usr.empty()) {
        return;
      }
      const auto src_sym = db_.lookup_symbol(method_usr);
      const auto dst_sym = db_.lookup_symbol(owner_usr);
      if (!src_sym || !dst_sym) {
        return;
      }
      Edge e;
      e.src_id = src_sym->id;
      e.dst_id = dst_sym->id;
      e.kind = 9; // method_of
      e.count = 1;
      db_.add_edge(e);

      // -- overrides (CXX_METHOD only): emit for each overridden method --
      if (ck == CXCursor_CXXMethod) {
        CxOverriddenCursors overridden(lib, cursor);
        for (unsigned oi = 0; oi < overridden.size(); ++oi) {
          const std::string ov_usr =
              CxString(lib, lib.clang_getCursorUSR(overridden[oi])).str();
          if (ov_usr.empty()) {
            continue;
          }
          const RefDeclLoc ov_dl = ref_decl_loc(lib, db_, overridden[oi]);
          const int64_t dst_ov = db_.mint_symbol_id(
              ov_usr,
              CxString(lib, lib.clang_getCursorSpelling(overridden[oi])).str(),
              qualified_name(lib, overridden[oi]),
              CxString(lib, lib.clang_getCursorDisplayName(overridden[oi])).str(),
              stub_kind(lib, overridden[oi]), ov_dl.file_id, ov_dl.line,
              ov_dl.col, ov_dl.path);
          Edge oe;
          oe.src_id = src_sym->id;
          oe.dst_id = dst_ov;
          oe.kind = 6; // overrides
          oe.count = 1;
          db_.add_edge(oe);
        }
      }
      return;
    }

    // -- CLASS_TEMPLATE/FUNCTION_TEMPLATE: template_param --
    if (ck == CXCursor_ClassTemplate || ck == CXCursor_FunctionTemplate) {
      const std::string tmpl_usr =
          CxString(lib, lib.clang_getCursorUSR(cursor)).str();
      if (tmpl_usr.empty()) {
        return;
      }
      const auto tmpl_sym = db_.lookup_symbol(tmpl_usr);
      if (!tmpl_sym) {
        return;
      }
      // Enumerate template parameters (TEMPLATE_TYPE_PARAMETER,
      // TEMPLATE_NON_TYPE_PARAMETER, TEMPLATE_TEMPLATE_PARAMETER children).
      struct ParamCtx {
        LibClang *lib = nullptr;
        Storage *db = nullptr;
        int64_t owner_id = -1;
        int64_t pos = 0;
      };
      ParamCtx pctx;
      pctx.lib = &lib;
      pctx.db = &db_;
      pctx.owner_id = tmpl_sym->id;
      pctx.pos = 0;
      lib.clang_visitChildren(
          cursor,
          [](CXCursor c, CXCursor /*parent*/,
             CXClientData d) noexcept -> CXChildVisitResult {
            auto *pc = static_cast<ParamCtx *>(d);
            LibClang &l = *pc->lib;
            const CXCursorKind pk = l.clang_getCursorKind(c);
            int64_t param_kind = 0;
            if (pk == CXCursor_TemplateTypeParameter) {
              param_kind = 1;
            } else if (pk == CXCursor_NonTypeTemplateParameter) {
              param_kind = 2;
            } else if (pk == CXCursor_TemplateTemplateParameter) {
              param_kind = 3;
            } else {
              return CXChildVisit_Continue;
            }
            TemplateParam p;
            p.owner_id = pc->owner_id;
            p.position = pc->pos++;
            p.param_kind = param_kind;
            const std::string nm =
                CxString(l, l.clang_getCursorSpelling(c)).str();
            if (!nm.empty()) {
              p.name = nm;
            }
            pc->db->add_template_param(p);
            return CXChildVisit_Continue;
          },
          &pctx);
      return;
    }

    // -- STRUCT_DECL/CLASS_DECL: specializes (when it is a specialization) --
    if ((ck == CXCursor_StructDecl || ck == CXCursor_ClassDecl) &&
        lib.clang_isCursorDefinition(cursor)) {
      const CXCursor primary = lib.clang_getSpecializedCursorTemplate(cursor);
      if (!lib.clang_Cursor_isNull(primary) &&
          !is_invalid_kind(lib.clang_getCursorKind(primary))) {
        const std::string spec_usr =
            CxString(lib, lib.clang_getCursorUSR(cursor)).str();
        const std::string prim_usr =
            CxString(lib, lib.clang_getCursorUSR(primary)).str();
        if (!spec_usr.empty() && !prim_usr.empty() && spec_usr != prim_usr) {
          const auto spec_sym = db_.lookup_symbol(spec_usr);
          if (spec_sym) {
            const RefDeclLoc prim_dl = ref_decl_loc(lib, db_, primary);
            const int64_t prim_id = db_.mint_symbol_id(
                prim_usr,
                CxString(lib, lib.clang_getCursorSpelling(primary)).str(),
                qualified_name(lib, primary),
                CxString(lib, lib.clang_getCursorDisplayName(primary)).str(),
                stub_kind(lib, primary), prim_dl.file_id, prim_dl.line,
                prim_dl.col, prim_dl.path);
            // An explicit instantiation (`template class Foo<int>;`) is a
            // concrete INSTANCE of the template, not a specialization of it:
            // record it as `instantiates` (kind=5, instance -> primary) so it
            // surfaces under ClassTemplate.instantiations(). A true explicit
            // specialization (`template <> class Foo<bool> {...}`) stays
            // `specializes` (kind=4).
            Edge e;
            e.src_id = spec_sym->id;
            e.dst_id = prim_id;
            e.kind = is_explicit_instantiation(lib, cursor) ? 5 : 4;
            e.count = 1;
            db_.add_edge(e);

            // template_arg rows for the specialization's arguments. For TYPE
            // args we always store the type spelling in `literal` (e.g. 'bool',
            // 'int') so the binding is distinguishable even when the arg is a
            // builtin with no declaration to resolve a ref_id from.
            const int nargs = lib.clang_Cursor_getNumTemplateArguments(cursor);
            for (int ai = 0; ai < nargs; ++ai) {
              const enum CXTemplateArgumentKind tak =
                  lib.clang_Cursor_getTemplateArgumentKind(
                      cursor, static_cast<unsigned>(ai));
              TemplateArg ta;
              ta.owner_id = spec_sym->id;
              ta.position = static_cast<int64_t>(ai);
              if (tak == CXTemplateArgumentKind_Type) {
                ta.arg_kind = 1;
                const CXType arg_type =
                    lib.clang_Cursor_getTemplateArgumentType(
                        cursor, static_cast<unsigned>(ai));
                const std::string spelling =
                    CxString(lib, lib.clang_getTypeSpelling(arg_type)).str();
                if (!spelling.empty()) {
                  ta.literal = spelling;
                }
                const CXCursor arg_decl =
                    lib.clang_getTypeDeclaration(arg_type);
                if (!lib.clang_Cursor_isNull(arg_decl) &&
                    !is_invalid_kind(lib.clang_getCursorKind(arg_decl))) {
                  const std::string ref_usr =
                      CxString(lib, lib.clang_getCursorUSR(arg_decl)).str();
                  if (!ref_usr.empty()) {
                    if (const auto rsym = db_.lookup_symbol(ref_usr)) {
                      ta.ref_id = rsym->id;
                    }
                  }
                }
              } else if (tak == CXTemplateArgumentKind_Integral) {
                ta.arg_kind = 2;
                ta.literal =
                    std::to_string(lib.clang_Cursor_getTemplateArgumentValue(
                        cursor, static_cast<unsigned>(ai)));
              } else {
                ta.arg_kind = static_cast<int64_t>(tak);
              }
              db_.add_template_arg(ta);
            }
          }
        }
      }
      return;
    }

  });

  // B2: body descent for calls + uses — recurse into each function-like
  // definition whose enclosing file matches filename.
  for_file_cursors(tu, filename, [&](CXCursor cursor) {
    if (!is_function_like(lib.clang_getCursorKind(cursor))) {
      return;
    }
    if (!lib.clang_isCursorDefinition(cursor)) {
      return;
    }
    const std::string fn_usr =
        CxString(lib, lib.clang_getCursorUSR(cursor)).str();
    if (fn_usr.empty()) {
      return;
    }
    const auto fn_sym = db_.lookup_symbol(fn_usr);
    if (!fn_sym) {
      return;
    }
    body_descent(cursor, fn_sym->id, file_id);
  });
}

void AstIndexer::index_edges(const ParsedTu &tu, const std::string &filename,
                             int64_t file_id) {
  if (!graph_enabled_) {
    return;
  }
  // Delete stale edges from a previous index of this file (idempotent
  // re-index).
  db_.delete_edges_for_file(file_id);

  Transaction txn = db_.transaction();
  index_edges_notxn(tu, filename, file_id);
  txn.commit();
}

} // namespace cidx

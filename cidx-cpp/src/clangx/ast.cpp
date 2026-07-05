// AST indexer — see ast.hpp. Line-level behavior is pinned to
// project/indexer/clang/ast.py (cited per function).
#include "clangx/ast.hpp"

#include <algorithm>
#include <cctype>
#include <cstring>
#include <exception>
#include <fstream>
#include <optional>
#include <set>
#include <string>
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

// End of cursor.extent as an expansion site -- the closing '}' of a function or
// method definition, or the full extent of a class/struct/union/typedef decl.
// Uses clang_getExpansionLocation to match clang.cindex's extent.end.line/column
// (cindex resolves SourceLocation line/column via the expansion location).
ExpansionLoc cursor_extent_end(LibClang &lib, CXCursor cursor) {
  ExpansionLoc loc;
  unsigned offset = 0;
  lib.clang_getExpansionLocation(
      lib.clang_getRangeEnd(lib.clang_getCursorExtent(cursor)), &loc.file,
      &loc.line, &loc.col, &offset);
  return loc;
}

// Start of cursor.extent as an expansion site -- NOT cursor.location (which is
// the identifying spelling location, e.g. the class/function NAME). extent.start
// includes the leading class/struct/union/enum keyword and, for a function/
// method, its return type (and out-of-line qualifier, e.g. `Circle::`), so
// (line, col)..(end_line, end_col) slices the WHOLE declaration (ast.py mirror).
ExpansionLoc cursor_extent_start(LibClang &lib, CXCursor cursor) {
  ExpansionLoc loc;
  unsigned offset = 0;
  lib.clang_getExpansionLocation(
      lib.clang_getRangeStart(lib.clang_getCursorExtent(cursor)), &loc.file,
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

// v26: cursor kinds that establish an enclosing SYMBOL for a namespace
// reference -- the nearest such ancestor of a NAMESPACE_REF is the uses-edge
// source. Mirrors ast.py _SCOPE_KINDS.
bool is_scope_kind(CXCursorKind ck) {
  switch (ck) {
  case CXCursor_FunctionDecl:
  case CXCursor_CXXMethod:
  case CXCursor_Constructor:
  case CXCursor_Destructor:
  case CXCursor_FunctionTemplate:
  case CXCursor_ClassDecl:
  case CXCursor_StructDecl:
  case CXCursor_UnionDecl:
  case CXCursor_ClassTemplate:
  case CXCursor_Namespace:
  case CXCursor_VarDecl:
  case CXCursor_FieldDecl:
  case CXCursor_EnumDecl:
  case CXCursor_TypedefDecl:
  case CXCursor_TypeAliasDecl:
    return true;
  default:
    return false;
  }
}

// Collect a cursor's IMMEDIATE children (CXChildVisit_Continue = no recursion),
// so ns_uses_descend can recurse manually with a per-node enclosing id.
struct NsChildCollector {
  std::vector<CXCursor> *out = nullptr;
};
CXChildVisitResult ns_collect_visitor(CXCursor c, CXCursor /*parent*/,
                                      CXClientData data) noexcept {
  static_cast<NsChildCollector *>(data)->out->push_back(c);
  return CXChildVisit_Continue;
}

// Recursive descent for namespace `uses` edges -- mirrors ast.py
// _emit_namespace_uses' inner descend(). Tracks the nearest enclosing indexed
// symbol id (-1 = none) and, for each main-file NAMESPACE_REF to an indexed
// namespace, emits a uses(7) edge enclosing->namespace + one edge_site. DESCENDS
// INTO BODIES (unlike for_file_cursors), so a `geo::` qualifier inside a
// function body is attributed to that function. Only main-file refs to an
// INDEXED namespace are recorded (bare `std::` -> unindexed c:@N@std -> lookup
// miss -> skipped), mirroring the lookup-only discipline of the other passes.
void ns_uses_descend(LibClang &lib, Storage &db, CXCursor cursor,
                     int64_t enclosing_id, const std::string &filename,
                     int64_t file_id) {
  std::vector<CXCursor> children;
  NsChildCollector cc;
  cc.out = &children;
  lib.clang_visitChildren(cursor, &ns_collect_visitor, &cc);
  for (const CXCursor &child : children) {
    const ExpansionLoc loc = cursor_location(lib, child);
    if (loc.file == nullptr) {
      continue; // from no file: skip subtree
    }
    CXString fname_cx = lib.clang_getFileName(loc.file);
    CxString fname_raii(lib, fname_cx);
    const char *raw = lib.clang_getCString(fname_cx);
    if (raw == nullptr || std::strcmp(raw, filename.c_str()) != 0) {
      continue; // from another file: skip subtree
    }
    const CXCursorKind ck = lib.clang_getCursorKind(child);
    if (ck == CXCursor_NamespaceRef) {
      if (enclosing_id >= 0) {
        const CXCursor ref = lib.clang_getCursorReferenced(child);
        if (!lib.clang_Cursor_isNull(ref)) {
          const std::string nusr =
              CxString(lib, lib.clang_getCursorUSR(ref)).str();
          if (!nusr.empty()) {
            const auto nsym = db.lookup_symbol(nusr);
            if (nsym && nsym->id != enclosing_id) {
              Edge e;
              e.src_id = enclosing_id;
              e.dst_id = nsym->id;
              e.kind = 7; // uses
              e.count = 1;
              const int64_t edge_id = db.add_edge(e);
              if (loc.line != 0) {
                EdgeSite site;
                site.edge_id = edge_id;
                site.file_id = file_id;
                site.line = static_cast<int64_t>(loc.line);
                site.col = static_cast<int64_t>(loc.col);
                site.conditional = 0;
                db.add_edge_site(site);
              }
            }
          }
        }
      }
      continue; // NAMESPACE_REF has no children worth walking
    }
    int64_t new_enclosing = enclosing_id;
    if (is_scope_kind(ck)) {
      const std::string usr = CxString(lib, lib.clang_getCursorUSR(child)).str();
      if (!usr.empty()) {
        const auto s = db.lookup_symbol(usr);
        if (s) {
          new_enclosing = s->id;
        }
      }
    }
    ns_uses_descend(lib, db, child, new_enclosing, filename, file_id);
  }
}

// B3 driver: mirror ast.py _emit_namespace_uses(db, tu, filename, file_id).
void emit_namespace_uses(LibClang &lib, Storage &db, const ParsedTu &tu,
                         const std::string &filename, int64_t file_id) {
  ns_uses_descend(lib, db, lib.clang_getTranslationUnitCursor(tu.tu), -1,
                  filename, file_id);
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
  const CXCursorKind ck = lib.clang_getCursorKind(cursor);
  const CXType info_type =
      (ck == CXCursor_TypedefDecl || ck == CXCursor_TypeAliasDecl)
          ? lib.clang_getTypedefDeclUnderlyingType(cursor)
          : lib.clang_getCursorType(cursor);
  std::string type_info = CxString(lib, lib.clang_getTypeSpelling(info_type)).str();
  if (!type_info.empty()) {
    sym.type_info = std::move(type_info);
  }
  sym.file_id = file_id;
  // Start of this cursor's own extent -- NOT `loc` (cursor.location, the
  // identifying spelling location) -- so (line, col)..(end_line, end_col)
  // slices the WHOLE declaration (ast.py:_to_symbol).
  const ExpansionLoc start = cursor_extent_start(lib, cursor);
  sym.line = static_cast<int64_t>(start.line);
  sym.col = static_cast<int64_t>(start.col);
  // End of this cursor's own extent, paired with (line, col) so
  // (line..end_line) slices the whole entity (ast.py:_to_symbol). The upsert
  // moves end_line/end_col in lockstep with line/col.
  const ExpansionLoc end = cursor_extent_end(lib, cursor);
  sym.end_line = static_cast<int64_t>(end.line);
  sym.end_col = static_cast<int64_t>(end.col);
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
  // Always upsert (ast.py mirror): add_symbol's own CASE-WHEN/COALESCE logic
  // never lets a lesser declaration cursor downgrade an already-stored
  // definition's location/extent — it only fills gaps (e.g. the decl site,
  // G15) and refreshes fields the row didn't carry yet (e.g. end_line/
  // end_col backfilled by a schema migration). A prior version skipped the
  // write entirely for an already-resolved symbol, so add_symbol (the only
  // place that writes end_line/end_col) was never reached again on reindex.
  const std::optional<Symbol> existing = db_.lookup_symbol(sym.usr);
  db_.add_symbol(sym);
  return !(existing && existing->resolved); // true = counted as "stored"
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

HeaderStats AstIndexer::index_headers(
    const ParsedTu &tu, const std::optional<bool> &ignore_system,
    const std::optional<std::vector<std::string>> &header_options,
    const std::optional<std::string> &header_driver) {
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

  // Two passes over this TU's headers. A header may reference a symbol
  // declared in a header it includes (which appears LATER in include order)
  // -- e.g. a function template whose body calls a member function template in
  // a deeper header. That call is dependent/recovered and only LINKS to an
  // already-indexed target (no stub is minted), so the target symbol must
  // already exist. Pass 1 mints symbols for every not-yet-indexed header;
  // pass 2 then extracts edges with all header symbols present.
  struct PendingHeader {
    std::string inc_name;   // inclusion spelling (for cursor matching, G23)
    int64_t file_id = -1;
    std::optional<double> mtime;
    int stored = 0;
  };

  HeaderStats counts;
  std::unordered_set<std::string> seen;
  std::vector<PendingHeader> pending;

  // Pass 1: mint symbols for every not-yet-indexed header.
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
    // Stamp the header with the including TU's (encoded) options + driver so
    // it is standalone-reparseable with full -I/-std/-D context, mirroring TU
    // rows (decoded at parse time).
    const int64_t file_id =
        db_.add_file_path(path, mtime, md5, header_options, header_driver);
    // Extract this header's symbols out of THIS TU's AST (no separate
    // parse), matching cursors against the include SPELLING, not the
    // abspath (G23: cursors' location-file names agree with the spelling).
    std::pair<int, int> result;
    {
      Transaction txn = db_.transaction();
      result = index_file_notxn(tu, inc.name, file_id);
      txn.commit();
    }
    pending.push_back({inc.name, file_id, mtime, result.first});
  }

  // Pass 2: extract edges for those headers, now that every header symbol is
  // in the DB (QD-1). Symbol rows must all exist before edge extraction begins.
  for (const PendingHeader &ph : pending) {
    {
      Transaction txn = db_.transaction();
      if (graph_enabled_) {
        db_.delete_edges_for_file(ph.file_id);
        db_.delete_definitions_for_file(ph.file_id); // v27: cascades def_edge
        index_edges_notxn(tu, ph.inc_name, ph.file_id);
      }
      txn.commit();
    }
    db_.mark_file_indexed(ph.file_id, ph.mtime);
    ++counts.indexed;
    counts.symbols += ph.stored;
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

// Write template_arg rows for a FUNCTION/METHOD template specialization from the
// cursor-level libclang API. Returns the number of args written -- 0 means the
// cursor exposed none (notably every METHOD-template specialization, where
// clang_Cursor_getNumTemplateArguments returns -1; use the token fallback).
// Mirrors ast.py:_index_cursor_template_args and the explicit-instantiation
// handler's TYPE/INTEGRAL branch.
std::optional<int64_t>
resolve_template_arg_ref_id(LibClang &lib, Storage &db,
                            const std::optional<std::string> &literal,
                            CXCursor scope_cursor);

std::optional<size_t> matching_template_close(const std::string &text,
                                              size_t start) {
  int depth = 0;
  for (size_t i = start; i < text.size(); ++i) {
    if (text[i] == '<') {
      ++depth;
    } else if (text[i] == '>') {
      --depth;
      if (depth == 0) {
        return i;
      }
    }
  }
  return std::nullopt;
}

std::string render_callable_template_display_name(
    const std::string &display_name, const std::vector<std::string> &literals) {
  std::string rendered_args = "<";
  for (size_t i = 0; i < literals.size(); ++i) {
    if (i != 0) {
      rendered_args += ", ";
    }
    rendered_args += literals[i];
  }
  rendered_args += ">";

  const size_t start = display_name.find('<');
  const size_t params = display_name.find('(');
  if (start != std::string::npos &&
      (params == std::string::npos || start < params)) {
    if (const auto end = matching_template_close(display_name, start)) {
      return display_name.substr(0, start) + rendered_args +
             display_name.substr(*end + 1);
    }
  }

  if (params != std::string::npos) {
    return display_name.substr(0, params) + rendered_args +
           display_name.substr(params);
  }
  return display_name + rendered_args;
}

void update_callable_template_display_name(
    Storage &db, int64_t owner_id, const std::vector<std::string> &literals) {
  if (literals.empty()) {
    return;
  }
  if (std::find(literals.begin(), literals.end(), "?") != literals.end()) {
    return;
  }
  const std::optional<Symbol> sym = db.lookup_symbol_by_id(owner_id);
  if (!sym || !sym->display_name || sym->display_name->empty()) {
    return;
  }
  const std::string display =
      render_callable_template_display_name(*sym->display_name, literals);
  if (display != *sym->display_name) {
    db.update_symbol(sym->usr, {{"display_name", display}});
  }
}

int index_cursor_template_args(LibClang &lib, Storage &db, int64_t owner_id,
                               CXCursor cursor) {
  const int nargs = lib.clang_Cursor_getNumTemplateArguments(cursor);
  if (nargs <= 0) {
    return 0;
  }
  std::vector<std::string> display_args;
  for (int ai = 0; ai < nargs; ++ai) {
    const enum CXTemplateArgumentKind tak =
        lib.clang_Cursor_getTemplateArgumentKind(cursor,
                                                 static_cast<unsigned>(ai));
    TemplateArg ta;
    ta.owner_id = owner_id;
    ta.position = static_cast<int64_t>(ai);
    if (tak == CXTemplateArgumentKind_Type) {
      ta.arg_kind = 1;
      const CXType arg_type = lib.clang_Cursor_getTemplateArgumentType(
          cursor, static_cast<unsigned>(ai));
      const std::string spelling =
          CxString(lib, lib.clang_getTypeSpelling(arg_type)).str();
      if (!spelling.empty()) {
        ta.literal = spelling;
      }
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
      if (!ta.ref_id) {
        ta.ref_id = resolve_template_arg_ref_id(lib, db, ta.literal, cursor);
      }
      display_args.push_back(ta.literal.value_or("?"));
    } else if (tak == CXTemplateArgumentKind_Integral) {
      ta.arg_kind = 2;
      ta.literal = std::to_string(lib.clang_Cursor_getTemplateArgumentValue(
          cursor, static_cast<unsigned>(ai)));
      display_args.push_back(*ta.literal);
    } else if (tak == CXTemplateArgumentKind_Declaration ||
               tak == CXTemplateArgumentKind_NullPtr ||
               tak == CXTemplateArgumentKind_Expression) {
      ta.arg_kind = 2;
      display_args.push_back("?");
    } else if (tak == CXTemplateArgumentKind_Template ||
               tak == CXTemplateArgumentKind_TemplateExpansion) {
      ta.arg_kind = 3;
      display_args.push_back("?");
    } else if (tak == CXTemplateArgumentKind_Pack) {
      ta.arg_kind = 4;
      display_args.push_back("?");
    } else {
      continue;
    }
    db.add_template_arg(ta);
  }
  update_callable_template_display_name(db, owner_id, display_args);
  return nargs;
}

// Best-effort template_arg.arg_kind for a token-derived method-template arg:
// a bare numeric / char / bool / nullptr literal is a non-type value (2), else a
// type (1). Reads the literal itself, not a mangled format. Mirrors
// ast.py:_method_targ_kind_from_literal.
int64_t method_targ_kind_from_literal(const std::string &text) {
  if (text == "true" || text == "false" || text == "nullptr") {
    return 2;
  }
  if (!text.empty() && (text.front() == '\'' || text.front() == '"')) {
    return 2;
  }
  std::string head = text;
  while (!head.empty() && (head.front() == '+' || head.front() == '-')) {
    head.erase(head.begin());
  }
  if (!head.empty() &&
      std::isdigit(static_cast<unsigned char>(head.front())) != 0) {
    return 2;
  }
  return 1;
}

// Minimal spacing when re-joining tokens into a type spelling: keep
// identifiers/keywords apart (`const int`) but hug punctuation (`int*`,
// `Pair<int,char>`). Mirrors ast.py:_needs_space.
bool targ_needs_space(const std::string &a, const std::string &b) {
  if (a.empty() || b.empty()) {
    return false;
  }
  const char al = a.back();
  const char bf = b.front();
  const bool a_word =
      std::isalnum(static_cast<unsigned char>(al)) != 0 || al == '_';
  const bool b_word =
      std::isalnum(static_cast<unsigned char>(bf)) != 0 || bf == '_';
  return a_word && b_word;
}

std::string trim_copy(std::string s) {
  const auto not_space = [](unsigned char c) { return std::isspace(c) == 0; };
  s.erase(s.begin(), std::find_if(s.begin(), s.end(), not_space));
  s.erase(std::find_if(s.rbegin(), s.rend(), not_space).base(), s.end());
  return s;
}

bool starts_with(const std::string &s, const std::string &prefix) {
  return s.size() >= prefix.size() &&
         s.compare(0, prefix.size(), prefix) == 0;
}

bool ends_with(const std::string &s, const std::string &suffix) {
  return s.size() >= suffix.size() &&
         s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

std::string template_arg_base_name(std::string text) {
  std::string s = trim_copy(std::move(text));
  for (const std::string prefix :
       {"typename ", "class ", "struct ", "enum "}) {
    if (starts_with(s, prefix)) {
      s = trim_copy(s.substr(prefix.size()));
      break;
    }
  }
  bool changed = true;
  while (changed) {
    changed = false;
    for (const std::string prefix : {"const ", "volatile "}) {
      if (starts_with(s, prefix)) {
        s = trim_copy(s.substr(prefix.size()));
        changed = true;
      }
    }
    for (const std::string suffix : {" const", " volatile"}) {
      if (ends_with(s, suffix)) {
        s = trim_copy(s.substr(0, s.size() - suffix.size()));
        changed = true;
      }
    }
    for (const std::string suffix : {"&&", "&", "*"}) {
      if (ends_with(s, suffix)) {
        s = trim_copy(s.substr(0, s.size() - suffix.size()));
        changed = true;
      }
    }
  }
  for (size_t i = 0; i < s.size(); ++i) {
    if (s[i] == '<') {
      s = trim_copy(s.substr(0, i));
      break;
    }
  }
  if (starts_with(s, "::")) {
    s = s.substr(2);
  }
  return s;
}

bool type_arg_symbol_kind(const std::string &kind) {
  return kind == "class" || kind == "struct" || kind == "union" ||
         kind == "enum" || kind == "typedef" || kind == "type-alias" ||
         kind == "class-template";
}

std::vector<std::string> cursor_scope_qual_names(LibClang &lib,
                                                 CXCursor cursor) {
  std::vector<std::string> out;
  std::set<std::string> seen;
  if (lib.clang_Cursor_isNull(cursor)) {
    return out;
  }
  CXCursor c = lib.clang_getCursorSemanticParent(cursor);
  while (!lib.clang_Cursor_isNull(c)) {
    const CXCursorKind kind = lib.clang_getCursorKind(c);
    if (is_invalid_kind(kind) || kind == CXCursor_TranslationUnit) {
      break;
    }
    const std::string qn = qualified_name(lib, c);
    if (!qn.empty() && seen.insert(qn).second) {
      out.push_back(qn);
    }
    c = lib.clang_getCursorSemanticParent(c);
  }
  return out;
}

std::vector<Symbol> type_arg_symbol_candidates(Storage &db,
                                               const std::string &name,
                                               bool qualified) {
  const std::vector<Symbol> hits =
      qualified ? db.lookup_symbols_by_qual_name(name)
                : db.lookup_symbols_by_name(name);
  std::vector<Symbol> out;
  std::set<int64_t> seen;
  for (const Symbol &sym : hits) {
    if (type_arg_symbol_kind(sym.kind) && seen.insert(sym.id).second) {
      out.push_back(sym);
    }
  }
  return out;
}

std::optional<int64_t> pick_template_arg_symbol(
    const std::vector<Symbol> &candidates) {
  if (candidates.empty()) {
    return std::nullopt;
  }
  std::vector<Symbol> non_inst;
  for (const Symbol &sym : candidates) {
    if (!sym.is_instantiation) {
      non_inst.push_back(sym);
    }
  }
  const std::vector<Symbol> &pool =
      !non_inst.empty() ? non_inst : candidates;
  if (pool.size() == 1) {
    return pool[0].id;
  }
  return std::nullopt;
}

std::optional<int64_t>
resolve_template_arg_ref_id(LibClang &lib, Storage &db,
                            const std::optional<std::string> &literal,
                            CXCursor scope_cursor) {
  if (!literal || literal->empty()) {
    return std::nullopt;
  }
  const std::string base = template_arg_base_name(*literal);
  if (base.empty()) {
    return std::nullopt;
  }
  std::vector<std::string> names;
  if (base.find("::") != std::string::npos) {
    names.push_back(base);
  }
  for (const std::string &scope : cursor_scope_qual_names(lib, scope_cursor)) {
    names.push_back(scope + "::" + base);
  }
  names.push_back(base);
  std::set<std::string> seen_names;
  for (const std::string &name : names) {
    if (!seen_names.insert(name).second) {
      continue;
    }
    const auto ref_id =
        pick_template_arg_symbol(type_arg_symbol_candidates(db, name, true));
    if (ref_id) {
      return ref_id;
    }
  }
  const size_t pos = base.rfind("::");
  const std::string tail = pos == std::string::npos ? base : base.substr(pos + 2);
  return pick_template_arg_symbol(type_arg_symbol_candidates(db, tail, false));
}

// Token fallback for METHOD-template explicit args (`obj.m<T,...>()`). libclang's
// cursor API returns -1 for methods, so recover the EXPLICIT `<...>` arguments
// from the call tokens and store each as its literal source spelling -- as
// written; TYPE args get a best-effort ref_id when their spelling resolves to an
// indexed type in scope. Top-level commas split args; `<`/`>`
// depth tracking (incl. `>>` closers) keeps nested args whole. Deduced calls
// (no explicit `<...>`) yield nothing. Mirrors
// ast.py:_index_method_template_args_from_tokens.
void index_method_template_args_from_tokens(LibClang &lib, Storage &db,
                                            int64_t owner_id,
                                            CXCursor call_cursor,
                                            const std::string &method_name) {
  CXTranslationUnit tu = lib.clang_Cursor_getTranslationUnit(call_cursor);
  if (tu == nullptr) {
    return;
  }
  const CXSourceRange extent = lib.clang_getCursorExtent(call_cursor);
  CXToken *tokens = nullptr;
  unsigned n = 0;
  lib.clang_tokenize(tu, extent, &tokens, &n);
  if (tokens == nullptr) {
    return;
  }
  std::vector<std::string> toks;
  toks.reserve(n);
  for (unsigned i = 0; i < n; ++i) {
    toks.push_back(CxString(lib, lib.clang_getTokenSpelling(tu, tokens[i])).str());
  }
  lib.clang_disposeTokens(tu, tokens, n);

  size_t ni = toks.size();
  for (size_t i = 0; i < toks.size(); ++i) {
    if (toks[i] == method_name) {
      ni = i;
      break;
    }
  }
  if (ni == toks.size() || ni + 1 >= toks.size() || toks[ni + 1] != "<") {
    return;  // no explicit template arguments at the call site
  }
  int depth = 0;
  std::vector<std::vector<std::string>> groups(1);
  for (size_t i = ni + 1; i < toks.size(); ++i) {
    const std::string &tok = toks[i];
    const int opens = static_cast<int>(std::count(tok.begin(), tok.end(), '<'));
    const int closes = static_cast<int>(std::count(tok.begin(), tok.end(), '>'));
    if (opens > 0) {
      const int before = depth;
      depth += opens;
      if (before == 0) {  // outermost '<' opens the list, don't record it
        if (opens > 1) {
          groups.back().push_back(std::string(static_cast<size_t>(opens - 1), '<'));
        }
        continue;
      }
      groups.back().push_back(tok);
      continue;
    }
    if (closes > 0) {
      const int before = depth;
      depth -= closes;
      if (depth <= 0) {  // '>>' can close a nested bracket AND the outer list
        const int inner = before - 1;
        if (inner > 0) {
          groups.back().push_back(std::string(static_cast<size_t>(inner), '>'));
        }
        break;
      }
      groups.back().push_back(tok);
      continue;
    }
    if (depth == 1 && tok == ",") {
      groups.emplace_back();
      continue;
    }
    groups.back().push_back(tok);
  }
  int64_t pos = 0;
  std::vector<std::string> display_args;
  for (const auto &g : groups) {
    if (g.empty()) {
      continue;
    }
    std::string literal;
    for (size_t i = 0; i < g.size(); ++i) {
      if (i != 0 && targ_needs_space(g[i - 1], g[i])) {
        literal += ' ';
      }
      literal += g[i];
    }
    // .strip() -- trim surrounding whitespace to match Python's join().strip()
    const auto not_space = [](unsigned char c) { return std::isspace(c) == 0; };
    literal.erase(literal.begin(),
                  std::find_if(literal.begin(), literal.end(), not_space));
    literal.erase(std::find_if(literal.rbegin(), literal.rend(), not_space).base(),
                  literal.end());
    if (literal.empty()) {
      continue;
    }
    TemplateArg ta;
    ta.owner_id = owner_id;
    ta.position = pos++;
    ta.arg_kind = method_targ_kind_from_literal(literal);
    ta.literal = literal;
    if (ta.arg_kind == 1) {
      ta.ref_id = resolve_template_arg_ref_id(lib, db, ta.literal, call_cursor);
    }
    db.add_template_arg(ta);
    display_args.push_back(literal);
  }
  update_callable_template_display_name(db, owner_id, display_args);
}

// Stage 2/3 core: mint a NAMED template instance `X<B>` from a CXType that is a
// class-template specialization. `X<B>` is named -- by an alias, a member, or a
// variable -- but a plain parse mints no `X<B>` symbol; only the primary `X` and
// any explicit `X<int>` decls exist. Mint the `X<B>` instance as its own entity
// (is_named_instance=1) so the roll-up can give it composes/aggregates/
// associates B (T->B substituted into the primary's members). Emits the symbol,
// an instantiates(5) edge X<B> -> X, and template_arg rows (TYPE args, T->B).
// Shared by three call sites that each name a concrete `X<B>`:
//   - `using Y = X<B>;` / `typedef X<B> Y;` (alias underlying type)  [Stage 2]
//   - a member `X<B> field;`                (FieldDecl type)         [Stage 3]
//   - a variable/local `X<B> v;`            (VarDecl type)           [Stage 3]
// Gated to a NON-system primary (a `std::vector<F>` member/alias/local is left
// to collapse onto the primary). Mirrors ast.py:_mint_instance_from_type.
void mint_instance_from_type(LibClang &lib, Storage &db, CXType type_obj) {
  // Stage 4: peel pointer / reference / array wrappers so an `X<B>* m_;` /
  // `X<B>& m_;` member mints the SAME instance as a by-value `X<B> m_;`. Mirrors
  // named_type_decl's stripping so emit_type_use (which peels the same way) and
  // minting agree on the spec decl USR. A `std::vector<X<B>>` is NOT a wrapper
  // kind here (it is a specialization whose primary is a system template) -- it
  // is left to collapse onto std::vector, never peeled to the inner X<B>, so no
  // std:: explosion and no inner mint (deferred, see plan).
  for (int i = 0; i < 32; ++i) {
    const CXTypeKind tk = type_obj.kind;
    if (tk == CXType_Pointer || tk == CXType_LValueReference ||
        tk == CXType_RValueReference) {
      type_obj = lib.clang_getPointeeType(type_obj);
    } else if (tk == CXType_ConstantArray || tk == CXType_IncompleteArray ||
               tk == CXType_VariableArray || tk == CXType_DependentSizedArray) {
      type_obj = lib.clang_getArrayElementType(type_obj);
    } else {
      break;
    }
  }
  const CXCursor decl = lib.clang_getTypeDeclaration(type_obj);
  if (lib.clang_Cursor_isNull(decl) ||
      is_invalid_kind(lib.clang_getCursorKind(decl))) {
    return;
  }
  const CXCursor primary = lib.clang_getSpecializedCursorTemplate(decl);
  if (lib.clang_Cursor_isNull(primary) ||
      is_invalid_kind(lib.clang_getCursorKind(primary))) {
    return; // type is not a class-template specialization
  }
  // Skip std:: / system templates -- keep them collapsed onto the primary.
  if (lib.clang_Location_isInSystemHeader(
          lib.clang_getCursorLocation(primary)) != 0) {
    return;
  }
  const std::string inst_usr =
      CxString(lib, lib.clang_getCursorUSR(decl)).str();
  const std::string prim_usr =
      CxString(lib, lib.clang_getCursorUSR(primary)).str();
  if (inst_usr.empty() || prim_usr.empty() || inst_usr == prim_usr) {
    return;
  }

  const RefDeclLoc inst_dl = ref_decl_loc(lib, db, decl);
  const int64_t inst_id = db.mint_symbol_id(
      inst_usr, CxString(lib, lib.clang_getCursorSpelling(decl)).str(),
      qualified_name(lib, decl),
      CxString(lib, lib.clang_getTypeSpelling(type_obj)).str(),  // 'X<B>'
      stub_kind(lib, decl), inst_dl.file_id, inst_dl.line, inst_dl.col,
      inst_dl.path, /*is_instantiation=*/true, /*is_named_instance=*/true);

  const RefDeclLoc prim_dl = ref_decl_loc(lib, db, primary);
  const int64_t prim_id = db.mint_symbol_id(
      prim_usr, CxString(lib, lib.clang_getCursorSpelling(primary)).str(),
      qualified_name(lib, primary),
      CxString(lib, lib.clang_getCursorDisplayName(primary)).str(),
      stub_kind(lib, primary), prim_dl.file_id, prim_dl.line, prim_dl.col,
      prim_dl.path);
  Edge e;
  e.src_id = inst_id;
  e.dst_id = prim_id;
  e.kind = 5; // instantiates: X<B> -> X
  e.count = 1;
  db.add_edge(e);

  // template_arg rows on the instance. The Type API exposes TYPE args only,
  // which is all the roll-up needs (T->B); non-type args are skipped.
  const int nargs = lib.clang_Type_getNumTemplateArguments(type_obj);
  for (int ai = 0; ai < nargs; ++ai) {
    const CXType arg_type = lib.clang_Type_getTemplateArgumentAsType(
        type_obj, static_cast<unsigned>(ai));
    TemplateArg ta;
    ta.owner_id = inst_id;
    ta.position = static_cast<int64_t>(ai);
    ta.arg_kind = 1;
    const std::string spelling =
        CxString(lib, lib.clang_getTypeSpelling(arg_type)).str();
    if (!spelling.empty()) {
      ta.literal = spelling;
    }
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
    if (!ta.ref_id) {
      ta.ref_id = resolve_template_arg_ref_id(lib, db, ta.literal,
                                              lib.clang_getNullCursor());
    }
    db.add_template_arg(ta);
  }
}

// Stage 2: mint the `X<B>` instance named by a `using`/typedef alias. Thin
// wrapper over mint_instance_from_type using the alias's underlying type.
// Mirrors ast.py:_mint_named_instance.
void mint_named_instance(LibClang &lib, Storage &db, CXCursor cursor) {
  mint_instance_from_type(lib, db,
                          lib.clang_getTypedefDeclUnderlyingType(cursor));
}

// Context for the recursive body descent (calls + uses).
struct BodyDescentCtx {
  LibClang *lib = nullptr;
  Storage *db = nullptr;
  int64_t src_id = -1;
  int64_t file_id = -1;
  int cond_depth = 0;
  std::string owner_usr; // USR of the enclosing method's owning record (empty
                         // for free fns); self-owner skip in TYPE_REF branch.
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

// Return the Layer-0 edge kind for a constructor call: copy(13)/move(14)/value(10).
// Mirrors ast.py:_ctor_form_kind.
// Inspects the ctor declaration's single parameter type spelling:
//   "&&"  -> move (14); "&" alone -> copy (13); else -> value (10).
static int ctor_form_kind(LibClang &lib, CXCursor ctor_cursor) {
  // Collect PARM_DECL children of the ctor declaration.
  struct ParmCtx {
    LibClang *lib;
    std::string first_param_type;
    int count = 0;
  } pctx;
  pctx.lib = &lib;
  lib.clang_visitChildren(
      ctor_cursor,
      [](CXCursor c, CXCursor /*parent*/, CXClientData data) {
        auto *ctx = static_cast<ParmCtx *>(data);
        if (ctx->lib->clang_getCursorKind(c) == CXCursor_ParmDecl) {
          ++ctx->count;
          if (ctx->count == 1) {
            ctx->first_param_type =
                CxString(*ctx->lib,
                         ctx->lib->clang_getTypeSpelling(
                             ctx->lib->clang_getCursorType(c)))
                    .str();
          }
        }
        return CXChildVisit_Continue;
      },
      &pctx);
  if (pctx.count == 1) {
    const std::string &pt = pctx.first_param_type;
    if (pt.find("&&") != std::string::npos) {
      return 14; // construct-move
    }
    if (pt.find('&') != std::string::npos) {
      return 13; // construct-copy
    }
  }
  return 10; // construct-value
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

// All candidate declarations of a dependent/overloaded callee, or empty when
// the CALL_EXPR's first child carries no OverloadedDeclRef. Only the FIRST child
// (the callee position) is searched, so an argument that is itself an overloaded
// name is not mistaken for the callee. Mirror of Python
// _overload_set_candidates().
std::vector<CXCursor> overload_set_candidates(LibClang &lib, CXCursor call) {
  FirstChildCtx fc{};
  lib.clang_visitChildren(call, &first_child_visitor, &fc);
  if (!fc.found) {
    return {};
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
    return {};
  }
  const unsigned n = lib.clang_getNumOverloadedDecls(odr);
  std::vector<CXCursor> out;
  out.reserve(n);
  for (unsigned i = 0; i < n; ++i) {
    out.push_back(lib.clang_getOverloadedDecl(odr, i));
  }
  return out;
}

// Returns the unique overloaded declaration, or a null cursor when the callee
// cannot be unambiguously recovered (ambiguous sets are handled by
// emit_overloaded_calls instead). Mirror of Python _recover_overloaded_callee().
CXCursor recover_overloaded_callee(LibClang &lib, CXCursor call) {
  const auto cands = overload_set_candidates(lib, call);
  return cands.size() == 1 ? cands[0] : lib.clang_getNullCursor();
}

// Emit `calls` edges for a dependent call whose overload set has MORE THAN one
// candidate (e.g. an overloaded member function template `cache.set(...)`
// invoked inside another template body). libclang cannot say which overload is
// selected, so the site is linked to every overload of that name -- a sound
// over-approximation for find-references / call-graph navigation. Each candidate
// USR is TU-invariant by contract, so a candidate not yet in the DB is given a
// USR-keyed stub (backfilled when its defining TU is indexed later), making the
// call order-independent. True system/stdlib candidates (ADL overload sets never
// separately indexed) are skipped so they do not become permanent unresolved
// externals. No receiver/argument provenance is recorded: that feeds
// virtual-dispatch devirt, and a function-template call is never a virtual
// dispatch. Mirror of Python _emit_overloaded_calls().
void emit_overloaded_calls(BodyDescentCtx *ctx, LibClang &lib, CXCursor call) {
  const auto cands = overload_set_candidates(lib, call);
  if (cands.size() < 2) {
    return;
  }
  std::set<int64_t> dst_ids; // ordered + deduped
  for (const CXCursor &cand : cands) {
    const std::string usr = CxString(lib, lib.clang_getCursorUSR(cand)).str();
    if (usr.empty()) {
      continue;
    }
    if (const auto s = ctx->db->lookup_symbol(usr)) {
      dst_ids.insert(s->id);
      continue;
    }
    // Not yet indexed: mint a USR-keyed stub so a later index backfills it,
    // but skip true system/stdlib overloads.
    if (lib.clang_Location_isInSystemHeader(
            lib.clang_getCursorLocation(cand)) != 0) {
      continue;
    }
    const RefDeclLoc dl = ref_decl_loc(lib, *ctx->db, cand);
    dst_ids.insert(ctx->db->mint_symbol_id(
        usr, CxString(lib, lib.clang_getCursorSpelling(cand)).str(),
        qualified_name(lib, cand),
        CxString(lib, lib.clang_getCursorDisplayName(cand)).str(),
        stub_kind(lib, cand), dl.file_id, dl.line, dl.col, dl.path));
  }
  if (dst_ids.empty()) {
    // Nothing resolved or minted (e.g. all candidates system/USR-less): fall
    // back to the shared qualified name + kind over indexed symbols.
    const CXCursor first = cands[0];
    const std::string qn = qualified_name(lib, first);
    if (!qn.empty()) {
      const std::string sk = stub_kind(lib, first);
      for (const auto &s : ctx->db->lookup_symbols_by_qual_name(qn, sk)) {
        dst_ids.insert(s.id);
      }
    }
  }
  if (dst_ids.empty()) {
    return;
  }
  unsigned line = 0;
  unsigned col = 0;
  unsigned offset = 0;
  CXFile file_handle = nullptr;
  lib.clang_getExpansionLocation(lib.clang_getCursorLocation(call), &file_handle,
                                 &line, &col, &offset);
  for (const int64_t dst_id : dst_ids) {
    Edge e;
    e.src_id = ctx->src_id;
    e.dst_id = dst_id;
    e.kind = 1; // calls
    e.count = 1;
    const int64_t edge_id = ctx->db->add_edge(e);
    EdgeSite site;
    site.edge_id = edge_id;
    site.file_id = ctx->file_id;
    site.line = static_cast<int64_t>(line);
    site.col = static_cast<int64_t>(col);
    site.conditional = ctx->cond_depth > 0 ? 1 : 0;
    ctx->db->add_edge_site(site);
  }
}

// Link a callable specialization and, when applicable, mint its owner type.
// Mirror of Python _mint_instantiation_nodes().
//
// Always emits callable-specialization -> primary-template. For a class-template
// member instantiation (X<int>::method), also mints X<int>, attaches method_of to
// it, links X<int> -> X, and stores TYPE args on X<int>. For a method-template
// specialization on a non-template class (Context::register<MyType>), attaches
// method_of to the existing owner class without marking that class as an
// instantiation. Free-function specializations have no method_of edge.
void mint_instantiation_nodes(LibClang &lib, Storage &db,
                              const CXCursor &ref,
                              int64_t member_id,
                              int64_t prim_member_id) {
  // Callable specialization -> primary function/method template.
  Edge inst_b;
  inst_b.src_id = member_id;
  inst_b.dst_id = prim_member_id;
  inst_b.kind = 5; // instantiates
  inst_b.count = 1;
  db.add_edge(inst_b);

  const CXCursorKind ref_kind = lib.clang_getCursorKind(ref);
  if (ref_kind != CXCursor_CXXMethod &&
      ref_kind != CXCursor_Constructor &&
      ref_kind != CXCursor_Destructor &&
      ref_kind != CXCursor_ConversionFunction) {
    return;
  }

  const CXCursor parent = lib.clang_getCursorSemanticParent(ref);
  if (lib.clang_Cursor_isNull(parent) ||
      is_invalid_kind(lib.clang_getCursorKind(parent))) {
    return;
  }
  const CXCursorKind parent_cursor_kind = lib.clang_getCursorKind(parent);
  if (parent_cursor_kind != CXCursor_ClassDecl &&
      parent_cursor_kind != CXCursor_StructDecl &&
      parent_cursor_kind != CXCursor_UnionDecl &&
      parent_cursor_kind != CXCursor_ClassTemplate &&
      parent_cursor_kind != CXCursor_ClassTemplatePartialSpecialization) {
    return;
  }
  const std::string type_usr =
      CxString(lib, lib.clang_getCursorUSR(parent)).str();
  if (type_usr.empty()) {
    return;
  }

  const CXCursor class_primary =
      lib.clang_getSpecializedCursorTemplate(parent);
  if (lib.clang_Cursor_isNull(class_primary) ||
      is_invalid_kind(lib.clang_getCursorKind(class_primary))) {
    if (const auto owner_sym = db.lookup_symbol(type_usr)) {
      Edge mo;
      mo.src_id = member_id;
      mo.dst_id = owner_sym->id;
      mo.kind = 9; // method_of
      mo.count = 1;
      db.add_edge(mo);
    }
    return;
  }
  const std::string class_prim_usr =
      CxString(lib, lib.clang_getCursorUSR(class_primary)).str();
  if (class_prim_usr.empty() || class_prim_usr == type_usr) {
    if (const auto owner_sym = db.lookup_symbol(type_usr)) {
      Edge mo;
      mo.src_id = member_id;
      mo.dst_id = owner_sym->id;
      mo.kind = 9; // method_of
      mo.count = 1;
      db.add_edge(mo);
    }
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
  const auto class_prim_sym = db.lookup_symbol(class_prim_usr);
  if (class_prim_sym) {
    Edge inst_e;
    inst_e.src_id = type_id;
    inst_e.dst_id = class_prim_sym->id;
    inst_e.kind = 5; // instantiates
    inst_e.count = 1;
    db.add_edge(inst_e);
  }

  // (f) template_arg rows on TYPE node via clang_Type_getTemplateArgumentAsType
  // (TYPE args only -- same as VAR_DECL B3 pattern at ast.cpp:1280).
  // For a method template on a non-template owner we returned above; its explicit
  // args are stored on the callable specialization itself.
  const CXType parent_type = lib.clang_getCursorType(parent);
  const int nargs = lib.clang_Type_getNumTemplateArguments(parent_type);
  if (nargs <= 0) {
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
    if (!ta.ref_id) {
      ta.ref_id = resolve_template_arg_ref_id(lib, db, ta.literal, parent);
    }
    db.add_template_arg(ta);
  }
}

// Non-recursive entry point: visits all children via clang_visitChildren,
// capturing CALL_EXPR (calls) + DECL_REF_EXPR / MEMBER_REF_EXPR (uses)
// nodes and recursing depth-first.
CXChildVisitResult body_descent_visitor(CXCursor cursor, CXCursor parent,
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
        if (lib.clang_Cursor_isNull(ref)) {
          // Multi-candidate dependent overload set (e.g. an overloaded member
          // function template `cache.set(...)` called from another template
          // body): the single-overload recovery above declines it. Link the
          // site to every indexed overload. See emit_overloaded_calls.
          emit_overloaded_calls(ctx, lib, cursor);
        }
      }
      if (!lib.clang_Cursor_isNull(ref)) {
        const std::string callee_usr =
            CxString(lib, lib.clang_getCursorUSR(ref)).str();
        if (!callee_usr.empty()) {
          // Resolved calls mint a stub for an unindexed target. A RECOVERED
          // (dependent) call does the same -- a USR is TU-invariant by
          // contract, so a USR-keyed stub is backfilled when its defining TU is
          // indexed later, making the call order-independent. (An earlier
          // belief that libclang emits an inconsistent USR for dependent member
          // templates was a parse artifact: a fatal builtin-header miss
          // truncated `std::string` to a fallback type. With a complete parse
          // the call-site USR matches the declaration.)
          int64_t dst_id = -1;
          if (recovered) {
            if (const auto dst = ctx->db->lookup_symbol(callee_usr)) {
              dst_id = dst->id;
            }
            // USR not yet indexed: try the stable qualified name + kind first
            // (links to an already-present symbol when unambiguous), else mint a
            // USR-keyed stub for a later index to backfill.
            if (dst_id < 0) {
              const std::string qn = qualified_name(lib, ref);
              if (!qn.empty()) {
                const std::string sk = stub_kind(lib, ref);
                const auto cands =
                    ctx->db->lookup_symbols_by_qual_name(qn, sk);
                if (cands.size() == 1) {
                  dst_id = cands[0].id;
                }
              }
            }
            if (dst_id < 0 && lib.clang_Location_isInSystemHeader(
                                  lib.clang_getCursorLocation(ref)) == 0) {
              // Skip true system/stdlib targets (e.g. one-arg std::move) -- they
              // are never separately indexed, so a stub would be a permanent
              // unresolved external.
              const RefDeclLoc dl = ref_decl_loc(lib, *ctx->db, ref);
              dst_id = ctx->db->mint_symbol_id(
                  callee_usr,
                  CxString(lib, lib.clang_getCursorSpelling(ref)).str(),
                  qualified_name(lib, ref),
                  CxString(lib, lib.clang_getCursorDisplayName(ref)).str(),
                  stub_kind(lib, ref), dl.file_id, dl.line, dl.col, dl.path);
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
            // Function/method template specialization: capture the concrete
              // template arguments. Free-function specs expose them via the cursor
              // API (incl. non-type + nested args); METHOD specs return -1 there
              // (libclang gap), so fall back to the explicit `<...>` call tokens,
              // stored as-written with best-effort type ref_id linking.
            if (is_inst_member && dst_id >= 0) {
              const int wrote =
                  index_cursor_template_args(lib, *ctx->db, dst_id, ref);
              if (wrote == 0 &&
                  lib.clang_getCursorKind(ref) == CXCursor_CXXMethod) {
                index_method_template_args_from_tokens(
                    lib, *ctx->db, dst_id, cursor,
                    CxString(lib, lib.clang_getCursorSpelling(ref)).str());
              }
            }
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
      // PR1 Layer-0: emit construction form edges (10/11/13/14) and
      // factory-construct (15) when the callee is a known constructor or a
      // make_unique / make_shared factory. Lookup-only: only when B is indexed.
      // parent-kind context: `parent` (the cursor arg) is the parent of cursor.
      if (!lib.clang_Cursor_isNull(ref)) {
        const CXCursorKind ref_kind = lib.clang_getCursorKind(ref);
        if (ref_kind == CXCursor_Constructor) {
          // Skip if the immediate parent is CXXNewExpr (134): the construct-heap
          // branch below handles that case so we avoid emitting both 12 and 10.
          const CXCursorKind parent_kind = lib.clang_getCursorKind(parent);
          if (parent_kind != (CXCursorKind)134 /* CXXNewExpr */) {
            const std::string type_usr =
                record_usr_of_type(lib, lib.clang_getCursorType(cursor));
            if (!type_usr.empty()) {
              if (const auto dst_sym = ctx->db->lookup_symbol(type_usr)) {
                int form;
                if (parent_kind == CXCursor_VarDecl) {
                  form = ctor_form_kind(lib, ref);
                } else {
                  // Standalone temporary (Widget{} / Widget(x) not in a var).
                  int sig = ctor_form_kind(lib, ref);
                  form = (sig == 13 || sig == 14) ? sig : 11; // construct-temp
                }
                Edge fe;
                fe.src_id = ctx->src_id;
                fe.dst_id = dst_sym->id;
                fe.kind = form;
                fe.count = 1;
                ctx->db->add_edge(fe);
              }
            }
          }
        } else if (ref_kind == CXCursor_FunctionDecl) {
          // Factory: make_unique<B> / make_shared<B> from system headers.
          const std::string callee_sp =
              CxString(lib, lib.clang_getCursorSpelling(ref)).str();
          if ((callee_sp == "make_unique" || callee_sp == "make_shared") &&
              lib.clang_Location_isInSystemHeader(
                  lib.clang_getCursorLocation(ref)) != 0) {
            const CXType result_canonical =
                ::clang_getCanonicalType(lib.clang_getCursorType(cursor));
            const int nargs =
                lib.clang_Type_getNumTemplateArguments(result_canonical);
            if (nargs > 0) {
              const CXType arg0 =
                  lib.clang_Type_getTemplateArgumentAsType(result_canonical, 0);
              const std::string fact_usr = record_usr_of_type(lib, arg0);
              if (!fact_usr.empty()) {
                if (const auto fact_sym = ctx->db->lookup_symbol(fact_usr)) {
                  Edge fe;
                  fe.src_id = ctx->src_id;
                  fe.dst_id = fact_sym->id;
                  fe.kind = 15; // factory-construct
                  fe.count = 1;
                  ctx->db->add_edge(fe);
                }
              }
            }
          }
        }
      }
    } else if (kind == (CXCursorKind)134 /* CXXNewExpr */) {
      // PR1 Layer-0: construct-heap (12). The new expression type is the
      // pointer to the allocated record (e.g. Widget*).
      const std::string heap_usr =
          record_usr_of_type(lib, lib.clang_getCursorType(cursor));
      if (!heap_usr.empty()) {
        if (const auto heap_sym = ctx->db->lookup_symbol(heap_usr)) {
          Edge fe;
          fe.src_id = ctx->src_id;
          fe.dst_id = heap_sym->id;
          fe.kind = 12; // construct-heap
          fe.count = 1;
          ctx->db->add_edge(fe);
        }
      }
    } else if (kind == (CXCursorKind)135 /* CXXDeleteExpr */) {
      // PR1 Layer-0: destroy (16). First child's type pointee names the
      // destroyed record.
      struct FirstDelCtx {
        LibClang *lib;
        std::string usr;
      } fdc;
      fdc.lib = &lib;
      lib.clang_visitChildren(
          cursor,
          [](CXCursor c, CXCursor /*p*/, CXClientData data) {
            auto *ctx2 = static_cast<FirstDelCtx *>(data);
            const CXType ct = ctx2->lib->clang_getCursorType(c);
            ctx2->usr = record_usr_of_type(*ctx2->lib, ct);
            return CXChildVisit_Break; // first child only
          },
          &fdc);
      if (!fdc.usr.empty()) {
        if (const auto del_sym = ctx->db->lookup_symbol(fdc.usr)) {
          Edge fe;
          fe.src_id = ctx->src_id;
          fe.dst_id = del_sym->id;
          fe.kind = 16; // destroy
          fe.count = 1;
          ctx->db->add_edge(fe);
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
    } else if (kind == CXCursor_TypeRef || kind == CXCursor_TemplateRef) {
      // B2 uses: a bare type NAME in expression/statement position
      // (Color::Red, MyClass::instance(), sizeof(T), static_cast<T>, new T).
      // PARENT-KIND GUARD: `parent` is the enclosing cursor. Signature / field
      // / var-decl / typedef type-refs are already emitted by the declaration
      // paths (emit_type_use, template_arg rows) and have a *declaration*
      // parent, so skip those. Only type-names under expression/statement
      // nodes survive. Mirrors ast.py:_body_descent TYPE_REF branch.
      const CXCursorKind pk = lib.clang_getCursorKind(parent);
      const bool parent_is_decl =
          pk == CXCursor_VarDecl || pk == CXCursor_ParmDecl ||
          pk == CXCursor_FieldDecl || pk == CXCursor_FunctionDecl ||
          pk == CXCursor_CXXMethod || pk == CXCursor_Constructor ||
          pk == CXCursor_Destructor || pk == CXCursor_FunctionTemplate ||
          pk == CXCursor_TypedefDecl || pk == CXCursor_TypeAliasDecl;
      if (!parent_is_decl) {
        const CXCursor ref = lib.clang_getCursorReferenced(cursor);
        if (!lib.clang_Cursor_isNull(ref)) {
          const std::string usr =
              CxString(lib, lib.clang_getCursorUSR(ref)).str();
          // Lookup-only, NO stubs; skip self-edge and the enclosing method's
          // own owning record (redundant with method_of).
          if (!usr.empty() && usr != ctx->owner_usr) {
            const auto dst = ctx->db->lookup_symbol(usr);
            if (dst && dst->id != ctx->src_id) {
              emit_body_edge(ctx, lib, cursor, dst->id, 7 /* uses */);
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
      // Stage 3: a LOCAL `X<B> v;` mints the X<B> instance entity (its own
      // composes/aggregates/associates via T->B). The file-cursor walk in
      // index_edges does not descend bodies, so locals are minted here;
      // file-scope vars + members are minted there. (Order matches the Python
      // body-descent arm: emit_type_use THEN mint -- locals have no owning
      // record, so the FIELD_DECL/VAR_DECL ordering fix is not needed here.)
      mint_instance_from_type(lib, *ctx->db, lib.clang_getCursorType(cursor));
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
                  if (!ta.ref_id) {
                    ta.ref_id = resolve_template_arg_ref_id(
                        lib, *ctx->db, ta.literal, cursor);
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

// v27: walk a static member variable's INITIALIZER, recording each call it
// makes as a def_edge off this backend's definition. Mirrors
// ast.py:_emit_static_init_def_edges. A variable has no body descent, so
// `int C::x = seed();` would otherwise drop the `seed` dependency.
struct StaticInitCtx {
  LibClang *lib;
  Storage *db;
  int64_t def_id;
};

CXChildVisitResult static_init_visitor(CXCursor cursor, CXCursor /*parent*/,
                                       CXClientData data) noexcept {
  auto *ctx = static_cast<StaticInitCtx *>(data);
  try {
    LibClang &lib = *ctx->lib;
    if (lib.clang_getCursorKind(cursor) == CXCursor_CallExpr) {
      const CXCursor ref = lib.clang_getCursorReferenced(cursor);
      if (!lib.clang_Cursor_isNull(ref)) {
        const std::string usr =
            CxString(lib, lib.clang_getCursorUSR(ref)).str();
        if (!usr.empty()) {
          const auto sym = ctx->db->lookup_symbol(usr);
          if (sym) {
            // A variable does not *call*; its initializer USES (kind 7) the
            // functions it references. Mirrors _emit_static_init_def_edges.
            ctx->db->add_def_edge(ctx->def_id, sym->id, 7); // uses (not a call)
          }
        }
      }
    }
  } catch (...) {
    // Swallow: initializer def_edges are best-effort, like the Python walk.
  }
  return CXChildVisit_Recurse;
}

void emit_static_init_def_edges(LibClang &lib, Storage &db, CXCursor var_cursor,
                                int64_t def_id) {
  StaticInitCtx ctx{&lib, &db, def_id};
  lib.clang_visitChildren(var_cursor, &static_init_visitor, &ctx);
}

// v28: the initializer source text of a variable definition, per backend:
// `int C::x = seed_a();` -> "seed_a()", `= 5` -> "5". Reads the cursor's own
// source extent from the file and returns the text after the first '=', stripped
// with a trailing ';' removed (no '=' -> nullopt). Exact source slice so Python
// and C++ agree byte-for-byte. Mirrors _static_var_init_text.
std::optional<std::string> static_var_init_text(LibClang &lib, CXCursor cursor) {
  const CXSourceRange ext = lib.clang_getCursorExtent(cursor);
  CXFile sfile = nullptr;
  unsigned soff = 0, u = 0;
  lib.clang_getExpansionLocation(lib.clang_getRangeStart(ext), &sfile, &u, &u,
                                 &soff);
  CXFile efile = nullptr;
  unsigned eoff = 0;
  lib.clang_getExpansionLocation(lib.clang_getRangeEnd(ext), &efile, &u, &u,
                                 &eoff);
  if (sfile == nullptr || eoff <= soff) {
    return std::nullopt;
  }
  const std::string path = CxString(lib, lib.clang_getFileName(sfile)).str();
  if (path.empty()) {
    return std::nullopt;
  }
  std::ifstream in(path, std::ios::binary);
  if (!in) {
    return std::nullopt;
  }
  in.seekg(static_cast<std::streamoff>(soff));
  std::string raw(eoff - soff, '\0');
  in.read(raw.data(), static_cast<std::streamsize>(eoff - soff));
  raw.resize(static_cast<std::size_t>(in.gcount()));
  const auto eq = raw.find('=');
  if (eq == std::string::npos) {
    return std::nullopt;
  }
  const auto strip = [](const std::string &s) {
    const char *ws = " \t\r\n\f\v";
    const auto b = s.find_first_not_of(ws);
    if (b == std::string::npos) {
      return std::string();
    }
    const auto e = s.find_last_not_of(ws);
    return s.substr(b, e - b + 1);
  };
  std::string val = strip(raw.substr(eq + 1));
  while (!val.empty() && val.back() == ';') {
    val.pop_back();
  }
  val = strip(val);
  if (val.empty()) {
    return std::nullopt;
  }
  return val;
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
  // Owner USR for the self-owner skip in the TYPE_REF branch: when fn_cursor is
  // a method, its semantic parent is the owning record; record that USR so a
  // method naming its own class does not emit a redundant uses edge.
  const CXCursor owner = lib.clang_getCursorSemanticParent(fn_cursor);
  if (!lib.clang_Cursor_isNull(owner)) {
    const CXCursorKind ok = lib.clang_getCursorKind(owner);
    if (ok == CXCursor_ClassDecl || ok == CXCursor_StructDecl ||
        ok == CXCursor_UnionDecl || ok == CXCursor_ClassTemplate ||
        ok == CXCursor_ClassTemplatePartialSpecialization) {
      ctx.owner_usr = CxString(lib, lib.clang_getCursorUSR(owner)).str();
    }
  }
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
      //
      // Stage 3/4: a member/file-scope `X<B>` mints the X<B> instance entity
      // (its own composes/aggregates/associates via T->B), exactly like an
      // alias. Minted FIRST -- before the uses-emit below -- so the member
      // reliably gets a structural uses(7) edge -> the X<B> instance (keyed on
      // the spec USR), order-independent within the TU. Stage 4's
      // cpp_materialise_field_relations reads that edge to give the owning
      // record `A composes/associates X<B>` (the un-collapsed instance).
      mint_instance_from_type(lib, db_, lib.clang_getCursorType(cursor));
      const std::string sym_usr =
          CxString(lib, lib.clang_getCursorUSR(cursor)).str();
      if (!sym_usr.empty()) {
        const auto sym = db_.lookup_symbol(sym_usr);
        if (sym) {
          emit_type_use(lib, db_, sym->id, lib.clang_getCursorType(cursor),
                        file_id, cursor, 0);
          // v27: an out-of-line static DATA MEMBER definition
          // (`int C::x = ...;`) is a per-backend body -- redefined in each
          // backend. Record its `definition` row (so it counts toward
          // multi_def / "list redefined") and its initializer's calls.
          if (ck == CXCursor_VarDecl &&
              lib.clang_isCursorDefinition(cursor) != 0) {
            const CXCursor sp = lib.clang_getCursorSemanticParent(cursor);
            const CXCursorKind spk = lib.clang_getCursorKind(sp);
            if (spk == CXCursor_ClassDecl || spk == CXCursor_StructDecl ||
                spk == CXCursor_UnionDecl || spk == CXCursor_ClassTemplate) {
              const ExpansionLoc vstart = cursor_extent_start(lib, cursor);
              const ExpansionLoc vend = cursor_extent_end(lib, cursor);
              const int64_t vdef_id = db_.get_or_create_definition(
                  sym->id, file_id, static_cast<int64_t>(vstart.line),
                  static_cast<int64_t>(vstart.col),
                  static_cast<int64_t>(vend.line),
                  static_cast<int64_t>(vend.col),
                  static_var_init_text(lib, cursor));
              emit_static_init_def_edges(lib, db_, cursor, vdef_id);
            }
          }
        }
      }
    } else if (ck == CXCursor_TypedefDecl || ck == CXCursor_TypeAliasDecl) {
      // Stage 2: a named alias of a class-template specialization mints the
      // X<B> instance entity (own composes/aggregates/associates via T->B).
      // Minted FIRST -- before the uses-emit below -- so an alias OF a template
      // instance (`using IntBox = Box<int>;`) reliably gets a structural uses(7)
      // edge -> the X<B> instance (keyed on the spec USR), exactly like a
      // `Box<int> field;` member (FIELD_DECL, above). Without this order the
      // instance is not yet minted when emit runs, so the alias would resolve to
      // no underlying target at all.
      mint_named_instance(lib, db_, cursor);
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
      // CRTP / template base: also link the specialization instance to its
      // primary template via instantiates(5).  A template used AS A BASE CLASS
      // (`class Cache : public Singleton<Cache>`) is the one instantiation site
      // not covered by the variable/member/call/using paths, so without this
      // the entity roll-up never sees `Singleton<Cache> instantiates Singleton`.
      const CXCursor base_primary =
          lib.clang_getSpecializedCursorTemplate(base_ref);
      if (!lib.clang_Cursor_isNull(base_primary) &&
          !is_invalid_kind(lib.clang_getCursorKind(base_primary))) {
        const std::string base_prim_usr =
            CxString(lib, lib.clang_getCursorUSR(base_primary)).str();
        if (!base_prim_usr.empty() && base_prim_usr != base_usr) {
          const auto base_prim_sym = db_.lookup_symbol(base_prim_usr);
          if (base_prim_sym) {
            Edge inst;
            inst.src_id = dst_id;
            inst.dst_id = base_prim_sym->id;
            inst.kind = 5; // instantiates
            inst.count = 1;
            db_.add_edge(inst);
          }
        }
      }
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

    // -- FRIEND_DECL: friend (Layer-0 kind 17 -> befriends entity_edge) --
    // Mirrors ast.py FRIEND_DECL handler. `friend class B;` inside record A;
    // the friend target B is a child TYPE_REF whose referenced declaration is
    // the friended record. Lookup-only (no stub), record-friends only.
    if (ck == CXCursor_FriendDecl) {
      const CXCursor owner = lib.clang_getCursorSemanticParent(cursor);
      if (lib.clang_Cursor_isNull(owner)) {
        return;
      }
      const CXCursorKind owner_kind = lib.clang_getCursorKind(owner);
      if (owner_kind != CXCursor_ClassDecl &&
          owner_kind != CXCursor_StructDecl) {
        return;
      }
      const std::string owner_usr =
          CxString(lib, lib.clang_getCursorUSR(owner)).str();
      if (owner_usr.empty()) {
        return;
      }
      const auto src_sym = db_.lookup_symbol(owner_usr);
      if (!src_sym) {
        return;
      }
      // Collect direct TYPE_REF children (the friended type references).
      struct FriendCtx {
        LibClang *lib;
        std::vector<CXCursor> type_refs;
      } fctx;
      fctx.lib = &lib;
      lib.clang_visitChildren(
          cursor,
          [](CXCursor c, CXCursor /*parent*/, CXClientData data) {
            auto *ctx = static_cast<FriendCtx *>(data);
            if (ctx->lib->clang_getCursorKind(c) == CXCursor_TypeRef) {
              ctx->type_refs.push_back(c);
            }
            return CXChildVisit_Continue;
          },
          &fctx);
      for (const CXCursor &tref : fctx.type_refs) {
        const CXCursor friend_decl = lib.clang_getCursorReferenced(tref);
        if (lib.clang_Cursor_isNull(friend_decl)) {
          continue;
        }
        const CXCursorKind fk = lib.clang_getCursorKind(friend_decl);
        if (fk != CXCursor_ClassDecl && fk != CXCursor_StructDecl &&
            fk != CXCursor_ClassTemplate) {
          continue;
        }
        const std::string friend_usr =
            CxString(lib, lib.clang_getCursorUSR(friend_decl)).str();
        if (friend_usr.empty()) {
          continue;
        }
        const auto dst_sym = db_.lookup_symbol(friend_usr);
        if (!dst_sym) {
          continue;
        }
        Edge e;
        e.src_id = src_sym->id;
        e.dst_id = dst_sym->id;
        e.kind = 17; // friend
        e.count = 1;
        db_.add_edge(e);
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
      // A member function template (FUNCTION_TEMPLATE whose semantic parent is
      // a record/class-template) is a method too, but the CXX_METHOD method_of
      // block above never sees it (its cursor kind is FUNCTION_TEMPLATE), so it
      // would lack a method_of edge. Emit method_of here for this case.
      if (ck == CXCursor_FunctionTemplate) {
        const CXCursor owner = lib.clang_getCursorSemanticParent(cursor);
        if (!lib.clang_Cursor_isNull(owner) &&
            !is_invalid_kind(lib.clang_getCursorKind(owner))) {
          const CXCursorKind ok = lib.clang_getCursorKind(owner);
          if (ok == CXCursor_ClassDecl || ok == CXCursor_StructDecl ||
              ok == CXCursor_ClassTemplate ||
              ok == CXCursor_ClassTemplatePartialSpecialization) {
            const std::string owner_usr =
                CxString(lib, lib.clang_getCursorUSR(owner)).str();
            if (!owner_usr.empty()) {
              const auto owner_sym = db_.lookup_symbol(owner_usr);
              if (owner_sym) {
                Edge mo;
                mo.src_id = tmpl_sym->id;
                mo.dst_id = owner_sym->id;
                mo.kind = 9; // method_of
                mo.count = 1;
                db_.add_edge(mo);
              }
            }
          }
        }
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
                if (!ta.ref_id) {
                  ta.ref_id =
                      resolve_template_arg_ref_id(lib, db_, ta.literal, cursor);
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
    // v27: this function's body in THIS file is a per-backend definition.
    // Create its `definition` row, descend, then snapshot the calls/uses it just
    // emitted into `def_edge` -- immune to a later TU wiping `edge`.
    const ExpansionLoc dstart = cursor_extent_start(lib, cursor);
    const ExpansionLoc dend = cursor_extent_end(lib, cursor);
    const int64_t def_id = db_.get_or_create_definition(
        fn_sym->id, file_id, static_cast<int64_t>(dstart.line),
        static_cast<int64_t>(dstart.col), static_cast<int64_t>(dend.line),
        static_cast<int64_t>(dend.col));
    body_descent(cursor, fn_sym->id, file_id);
    db_.copy_body_edges_to_def_edge(def_id, fn_sym->id);
  });

  // B3: namespace uses -- qualifiers / using-directives / using-declarations.
  emit_namespace_uses(lib, db_, tu, filename, file_id);
}

void AstIndexer::index_edges(const ParsedTu &tu, const std::string &filename,
                             int64_t file_id) {
  if (!graph_enabled_) {
    return;
  }
  // Delete stale edges from a previous index of this file (idempotent
  // re-index).
  db_.delete_edges_for_file(file_id);
  db_.delete_definitions_for_file(file_id); // v27: cascades this file's def_edge

  Transaction txn = db_.transaction();
  index_edges_notxn(tu, filename, file_id);
  txn.commit();
}

} // namespace cidx

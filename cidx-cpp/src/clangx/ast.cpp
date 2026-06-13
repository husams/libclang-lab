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

std::pair<int, int> AstIndexer::index_file(const ParsedTu &tu,
                                           const std::string &filename,
                                           int64_t file_id) {
  int stored = 0;
  int skipped = 0;
  Transaction txn = db_.transaction(); // one txn per file (ast.py:142)
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
  txn.commit(); // R2: explicit commit so a COMMIT failure is not swallowed
  return {stored, skipped};
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
    // Extract this header's symbols out of THIS TU's AST (no separate
    // parse), matching cursors against the include SPELLING, not the
    // abspath (G23: cursors' location-file names agree with the spelling).
    const std::pair<int, int> result = index_file(tu, inc.name, file_id);
    db_.mark_file_indexed(file_id, mtime);
    ++counts.indexed;
    counts.symbols += result.first;
  }
  return counts;
}

} // namespace cidx

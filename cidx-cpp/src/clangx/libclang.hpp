// LibClang facade — Amendment A1 (spec/02-design.md §12, 2026-06-12).
//
// D1 SUPERSEDED: cidx now links libclang at build time (CMake finds the
// library and records its path in CIDX_LIBCLANG_PATH).  The dlopen/dlsym shim
// is gone.  This class stays as a thin facade so every call site of the form
//   lib.clang_getCursorUSR(c)
// compiles unchanged; each member forwards directly to the linked symbol.
//
// ENV CONTRACT CHANGE (A1.3):
//   CIDX_LIBCLANG at *runtime* is ignored with a one-shot WARNING logged via
//   the cidx logger.  It is now a configure-time CMake hint only.  A stale
//   export must not break runs — we warn, not error.  CIDX_RESOURCE_DIR is
//   still honoured (unchanged).
//
// VENDORED HEADERS (D24 unchanged):
//   third_party/clang-c/ provides all the CX* types.  Functions come from the
//   linker, not dlsym.  The headers are type-only (ABI-stable 18→21) and
//   remove the need for an installed clang-dev package at build time.
#pragma once

#include <optional>
#include <string>

#include "clang-c/CXCompilationDatabase.h"
#include "clang-c/Index.h"

namespace cidx {

class LibClang {
public:
  // Process singleton.
  static LibClang &instance();

  // Default-constructible (tests may build local instances for isolation).
  LibClang() = default;
  LibClang(const LibClang &) = delete;
  LibClang &operator=(const LibClang &) = delete;

  // A1: load() is a no-op kept for call-site compatibility (§6.1, parse.cpp).
  // It emits a one-shot WARNING when CIDX_LIBCLANG is set in the environment
  // (A1.3: the env var is now build-time only).
  void load();

  // Always true — the binary cannot be built without a resolved libclang.
  bool loaded() const noexcept { return true; }

  // Parsed clang major version (D12): clang_getClangVersion() → regex
  // "version (\d+)" → int; 0 on no match (P12).  Cached.
  int major();

  // Build-time path baked in via CIDX_LIBCLANG_PATH compile definition (A1.3).
  // Used by Toolchain (S04) to derive the resource-dir search path.
  std::string library_path() const noexcept {
    return std::string(CIDX_LIBCLANG_PATH);
  }

  // The regex half of major(), exposed for unit tests (P12).
  static int parse_clang_major(const std::string &version_string);

  // ---------------------------------------------------------------------------
  // Forwarding methods — same names as the former function pointers so every
  // call site `lib.clang_foo(args...)` compiles unchanged.  Inline, zero
  // overhead, no indirection through a pointer.
  // ---------------------------------------------------------------------------

  // clang_getClangVersion / string helpers
  CXString clang_getClangVersion() const {
    return ::clang_getClangVersion();
  }
  const char *clang_getCString(CXString s) const {
    return ::clang_getCString(s);
  }
  void clang_disposeString(CXString s) const { ::clang_disposeString(s); }

  // Index + TU lifecycle
  CXIndex clang_createIndex(int excludeDeclarationsFromPCH,
                            int displayDiagnostics) const {
    return ::clang_createIndex(excludeDeclarationsFromPCH,
                               displayDiagnostics);
  }
  void clang_disposeIndex(CXIndex idx) const { ::clang_disposeIndex(idx); }
  CXErrorCode
  clang_parseTranslationUnit2(CXIndex idx, const char *source_filename,
                              const char *const *command_line_args,
                              int num_command_line_args,
                              struct CXUnsavedFile *unsaved_files,
                              unsigned num_unsaved_files,
                              unsigned options,
                              CXTranslationUnit *out_TU) const {
    return ::clang_parseTranslationUnit2(
        idx, source_filename, command_line_args, num_command_line_args,
        unsaved_files, num_unsaved_files, options, out_TU);
  }
  void clang_disposeTranslationUnit(CXTranslationUnit tu) const {
    ::clang_disposeTranslationUnit(tu);
  }

  // TU memory accounting (observability — there is no allocator hook in the
  // libclang C API). Used by the parser's CIDX_MEM report. The returned
  // CXTUResourceUsage owns a buffer freed by clang_disposeCXTUResourceUsage.
  CXTUResourceUsage clang_getCXTUResourceUsage(CXTranslationUnit tu) const {
    return ::clang_getCXTUResourceUsage(tu);
  }
  void clang_disposeCXTUResourceUsage(CXTUResourceUsage usage) const {
    ::clang_disposeCXTUResourceUsage(usage);
  }
  const char *clang_getTUResourceUsageName(CXTUResourceUsageKind kind) const {
    return ::clang_getTUResourceUsageName(kind);
  }

  // Diagnostics
  unsigned clang_getNumDiagnostics(CXTranslationUnit tu) const {
    return ::clang_getNumDiagnostics(tu);
  }
  CXDiagnostic clang_getDiagnostic(CXTranslationUnit tu, unsigned idx) const {
    return ::clang_getDiagnostic(tu, idx);
  }
  void clang_disposeDiagnostic(CXDiagnostic d) const {
    ::clang_disposeDiagnostic(d);
  }
  CXDiagnosticSeverity clang_getDiagnosticSeverity(CXDiagnostic d) const {
    return ::clang_getDiagnosticSeverity(d);
  }
  CXString clang_getDiagnosticSpelling(CXDiagnostic d) const {
    return ::clang_getDiagnosticSpelling(d);
  }
  CXSourceLocation clang_getDiagnosticLocation(CXDiagnostic d) const {
    return ::clang_getDiagnosticLocation(d);
  }

  // Cursor navigation
  CXCursor clang_getTranslationUnitCursor(CXTranslationUnit tu) const {
    return ::clang_getTranslationUnitCursor(tu);
  }
  unsigned clang_visitChildren(CXCursor parent, CXCursorVisitor visitor,
                               CXClientData client_data) const {
    return ::clang_visitChildren(parent, visitor, client_data);
  }

  // Cursor properties
  CXCursorKind clang_getCursorKind(CXCursor c) const {
    return ::clang_getCursorKind(c);
  }
  CXString clang_getCursorUSR(CXCursor c) const {
    return ::clang_getCursorUSR(c);
  }
  CXString clang_getCursorSpelling(CXCursor c) const {
    return ::clang_getCursorSpelling(c);
  }
  CXString clang_getCursorDisplayName(CXCursor c) const {
    return ::clang_getCursorDisplayName(c);
  }
  CXSourceLocation clang_getCursorLocation(CXCursor c) const {
    return ::clang_getCursorLocation(c);
  }
  void clang_getExpansionLocation(CXSourceLocation loc, CXFile *file,
                                  unsigned *line, unsigned *column,
                                  unsigned *offset) const {
    ::clang_getExpansionLocation(loc, file, line, column, offset);
  }
  CXString clang_getFileName(CXFile f) const { return ::clang_getFileName(f); }
  CXFile clang_getFile(CXTranslationUnit tu, const char *file_name) const {
    return ::clang_getFile(tu, file_name);
  }
  CXSourceLocation clang_getLocation(CXTranslationUnit tu, CXFile file,
                                     unsigned line, unsigned column) const {
    return ::clang_getLocation(tu, file, line, column);
  }
  int clang_Location_isInSystemHeader(CXSourceLocation loc) const {
    return ::clang_Location_isInSystemHeader(loc);
  }
  unsigned clang_isCursorDefinition(CXCursor c) const {
    return ::clang_isCursorDefinition(c);
  }
  unsigned clang_CXXMethod_isPureVirtual(CXCursor c) const {
    return ::clang_CXXMethod_isPureVirtual(c);
  }
  unsigned clang_CXXMethod_isStatic(CXCursor c) const {
    return ::clang_CXXMethod_isStatic(c);
  }
  CXLinkageKind clang_getCursorLinkage(CXCursor c) const {
    return ::clang_getCursorLinkage(c);
  }
  CX_CXXAccessSpecifier clang_getCXXAccessSpecifier(CXCursor c) const {
    return ::clang_getCXXAccessSpecifier(c);
  }
  CXCursor clang_getCursorSemanticParent(CXCursor c) const {
    return ::clang_getCursorSemanticParent(c);
  }
  CXType clang_getCursorType(CXCursor c) const {
    return ::clang_getCursorType(c);
  }
  CXString clang_getTypeSpelling(CXType t) const {
    return ::clang_getTypeSpelling(t);
  }
  void clang_getInclusions(CXTranslationUnit tu,
                           CXInclusionVisitor visitor,
                           CXClientData client_data) const {
    ::clang_getInclusions(tu, visitor, client_data);
  }

  // -- v7 graph layer forwarding methods -------------------------------------

  // Returns the cursor that a reference/call/base-spec points to. Returns the
  // null cursor when there is no referenced entity.
  CXCursor clang_getCursorReferenced(CXCursor c) const {
    return ::clang_getCursorReferenced(c);
  }

  // 1 when the cursor is a virtual-base CXX_BASE_SPECIFIER; 0 otherwise.
  unsigned clang_isVirtualBase(CXCursor c) const {
    return ::clang_isVirtualBase(c);
  }

  // 1 when the CXXMethod cursor is declared virtual.
  unsigned clang_CXXMethod_isVirtual(CXCursor c) const {
    return ::clang_CXXMethod_isVirtual(c);
  }

  // For a specialization cursor, returns the primary template.
  CXCursor clang_getSpecializedCursorTemplate(CXCursor c) const {
    return ::clang_getSpecializedCursorTemplate(c);
  }

  // Overloaded-declaration set carried by an OverloadedDeclRef cursor. Used to
  // recover the callee of a dependent CALL_EXPR inside a template body (the
  // call's getCursorReferenced is null, but the callee sub-expression still
  // names the candidate set here).
  unsigned clang_getNumOverloadedDecls(CXCursor c) const {
    return ::clang_getNumOverloadedDecls(c);
  }
  CXCursor clang_getOverloadedDecl(CXCursor c, unsigned i) const {
    return ::clang_getOverloadedDecl(c, i);
  }

  // Overridden cursors — caller MUST release with clang_disposeOverriddenCursors.
  void clang_getOverriddenCursors(CXCursor cursor,
                                  CXCursor **overridden,
                                  unsigned *num_overridden) const {
    ::clang_getOverriddenCursors(cursor, overridden, num_overridden);
  }

  void clang_disposeOverriddenCursors(CXCursor *overridden) const {
    ::clang_disposeOverriddenCursors(overridden);
  }

  // Number of template arguments on a cursor (e.g. a specialization decl).
  // Returns -1 when the cursor has no template arguments.
  int clang_Cursor_getNumTemplateArguments(CXCursor c) const {
    return ::clang_Cursor_getNumTemplateArguments(c);
  }

  // Kind of the i-th template argument (CXTemplateArgumentKind_Type, etc.).
  enum CXTemplateArgumentKind
  clang_Cursor_getTemplateArgumentKind(CXCursor c, unsigned i) const {
    return ::clang_Cursor_getTemplateArgumentKind(c, i);
  }

  // Type of a type template argument.
  CXType clang_Cursor_getTemplateArgumentType(CXCursor c, unsigned i) const {
    return ::clang_Cursor_getTemplateArgumentType(c, i);
  }

  // Value of an integral template argument.
  long long clang_Cursor_getTemplateArgumentValue(CXCursor c, unsigned i) const {
    return ::clang_Cursor_getTemplateArgumentValue(c, i);
  }

  // Declaration cursor for a type (e.g. the class decl behind a specialization
  // type). May return null cursor when there is no declaration.
  CXCursor clang_getTypeDeclaration(CXType t) const {
    return ::clang_getTypeDeclaration(t);
  }

  // Number of template arguments on a type (e.g. vector<int> -> 1).
  int clang_Type_getNumTemplateArguments(CXType t) const {
    return ::clang_Type_getNumTemplateArguments(t);
  }

  // i-th type template argument of a type.
  CXType clang_Type_getTemplateArgumentAsType(CXType t, unsigned i) const {
    return ::clang_Type_getTemplateArgumentAsType(t, i);
  }

  // Canonical cursor (dedup): collapses multiple declarations to one.
  CXCursor clang_getCanonicalCursor(CXCursor c) const {
    return ::clang_getCanonicalCursor(c);
  }

  // -- tokenization (distinguish explicit instantiation vs specialization) ---
  CXTranslationUnit clang_Cursor_getTranslationUnit(CXCursor c) const {
    return ::clang_Cursor_getTranslationUnit(c);
  }
  CXSourceRange clang_getCursorExtent(CXCursor c) const {
    return ::clang_getCursorExtent(c);
  }
  void clang_tokenize(CXTranslationUnit tu, CXSourceRange range,
                      CXToken **tokens, unsigned *num_tokens) const {
    ::clang_tokenize(tu, range, tokens, num_tokens);
  }
  CXString clang_getTokenSpelling(CXTranslationUnit tu, CXToken t) const {
    return ::clang_getTokenSpelling(tu, t);
  }
  void clang_disposeTokens(CXTranslationUnit tu, CXToken *tokens,
                           unsigned num_tokens) const {
    ::clang_disposeTokens(tu, tokens, num_tokens);
  }

  // -- type navigation (signature/field/variable `uses` extraction) ---------

  // Pointee of a pointer/reference type (Conf * -> Conf).
  CXType clang_getPointeeType(CXType t) const {
    return ::clang_getPointeeType(t);
  }
  // Element of an array type (Conf[] -> Conf).
  CXType clang_getArrayElementType(CXType t) const {
    return ::clang_getArrayElementType(t);
  }
  // Result (return) type of a function/method cursor.
  CXType clang_getCursorResultType(CXCursor c) const {
    return ::clang_getCursorResultType(c);
  }
  // Number of formal arguments of a function/method cursor (-1 if not a fn).
  int clang_Cursor_getNumArguments(CXCursor c) const {
    return ::clang_Cursor_getNumArguments(c);
  }
  // i-th formal-argument cursor of a function/method.
  CXCursor clang_Cursor_getArgument(CXCursor c, unsigned i) const {
    return ::clang_Cursor_getArgument(c, i);
  }
  // Underlying type behind a typedef/type-alias declaration cursor.
  CXType clang_getTypedefDeclUnderlyingType(CXCursor c) const {
    return ::clang_getTypedefDeclUnderlyingType(c);
  }

  // Null cursor guard.
  CXCursor clang_getNullCursor() const { return ::clang_getNullCursor(); }
  int clang_Cursor_isNull(CXCursor c) const { return ::clang_Cursor_isNull(c); }

  // Cursor equality.
  unsigned clang_equalCursors(CXCursor a, CXCursor b) const {
    return ::clang_equalCursors(a, b);
  }

  // CompilationDatabase
  CXCompilationDatabase
  clang_CompilationDatabase_fromDirectory(const char *dir,
                                         CXCompilationDatabase_Error *err) const {
    return ::clang_CompilationDatabase_fromDirectory(dir, err);
  }
  void clang_CompilationDatabase_dispose(CXCompilationDatabase db) const {
    ::clang_CompilationDatabase_dispose(db);
  }
  CXCompileCommands
  clang_CompilationDatabase_getAllCompileCommands(CXCompilationDatabase db) const {
    return ::clang_CompilationDatabase_getAllCompileCommands(db);
  }
  void clang_CompileCommands_dispose(CXCompileCommands cmds) const {
    ::clang_CompileCommands_dispose(cmds);
  }
  unsigned clang_CompileCommands_getSize(CXCompileCommands cmds) const {
    return ::clang_CompileCommands_getSize(cmds);
  }
  CXCompileCommand
  clang_CompileCommands_getCommand(CXCompileCommands cmds, unsigned i) const {
    return ::clang_CompileCommands_getCommand(cmds, i);
  }
  CXString clang_CompileCommand_getDirectory(CXCompileCommand cmd) const {
    return ::clang_CompileCommand_getDirectory(cmd);
  }
  CXString clang_CompileCommand_getFilename(CXCompileCommand cmd) const {
    return ::clang_CompileCommand_getFilename(cmd);
  }
  unsigned clang_CompileCommand_getNumArgs(CXCompileCommand cmd) const {
    return ::clang_CompileCommand_getNumArgs(cmd);
  }
  CXString clang_CompileCommand_getArg(CXCompileCommand cmd,
                                       unsigned i) const {
    return ::clang_CompileCommand_getArg(cmd, i);
  }

private:
  std::optional<int> major_;
};

// RAII over CXString (D23 unchanged): disposes via the facade's
// clang_disposeString so call sites compile unchanged.
class CxString {
public:
  CxString(const LibClang &lib, CXString s) : lib_(&lib), s_(s) {}
  ~CxString() { lib_->clang_disposeString(s_); }
  CxString(const CxString &) = delete;
  CxString &operator=(const CxString &) = delete;

  // clang_getCString, ""-safe on a null inner pointer.
  std::string str() const {
    const char *c = lib_->clang_getCString(s_);
    return c != nullptr ? std::string(c) : std::string();
  }

private:
  const LibClang *lib_;
  CXString s_;
};

// RAII for clang_getOverriddenCursors (mirrors CxString).
// Holds an owned array of overridden cursors; frees it on destruction.
class CxOverriddenCursors {
public:
  CxOverriddenCursors(const LibClang &lib, CXCursor cursor) : lib_(&lib) {
    lib_->clang_getOverriddenCursors(cursor, &cursors_, &num_);
  }
  ~CxOverriddenCursors() {
    if (cursors_ != nullptr) {
      lib_->clang_disposeOverriddenCursors(cursors_);
    }
  }
  CxOverriddenCursors(const CxOverriddenCursors &) = delete;
  CxOverriddenCursors &operator=(const CxOverriddenCursors &) = delete;

  unsigned size() const noexcept { return num_; }
  // Index access; caller must check i < size().
  CXCursor operator[](unsigned i) const { return cursors_[i]; }

private:
  const LibClang *lib_;
  CXCursor *cursors_ = nullptr;
  unsigned num_ = 0;
};

} // namespace cidx

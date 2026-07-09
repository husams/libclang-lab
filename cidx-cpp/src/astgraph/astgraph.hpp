// astgraph — dump one TU's libclang AST into a per-TU SQLite graph DB for
// Datalog (Soufflé) reasoning.  Design decisions (2026-07-09, user-approved):
//
//   * UNIFIED node space: cursors AND types are rows of `node`; every
//     relation is a row of `edge(src,dst,rel,ord)`.  node_kind ids are
//     namespaced: 1..999 = CXCursorKind value, 1000+k = CXTypeKind value k.
//   * NO NULLs anywhere — Soufflé's sqlite IO reads plain tables, so every
//     "absent" cell is a 0 sentinel (real ids start at 1) or ''.
//   * relation_kind is a FIXED catalog (kRel* below) grounded in the libclang
//     cursor/type cross-reference API — see seed_relation_kinds().
//   * symbol is deduped by USR and joins against cidx index.db symbol.usr.
//
// The tool shares cidx's configuration: compile args + driver for the TU come
// from the cidx index.db `file` row (main.cpp), and the parse itself goes
// through the same Toolchain/Parser as `cidx index` (builtin headers, driver
// replication, -ferror-limit=0).
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "clangx/parse.hpp"

namespace cidx {
namespace astgraph {

// On-disk schema version of <TU>.db (meta key "schema_version").
constexpr int kSchemaVersion = 1;

// CXTypeKind k is stored in node_kind / node.kind_id as kTypeKindBase + k so
// the two libclang enum spaces never collide (CXCursorKind tops out < 1000).
constexpr int kTypeKindBase = 1000;

// The fixed relation catalog.  1-8 relate cursor-nodes, 9 crosses cursor→type,
// 10 crosses type→cursor, 11-19 relate type-nodes.  `ord` carries sibling /
// argument / template-argument position where noted, else 0.
enum RelKind : int {
  kRelChild = 1,          // structural AST containment (ord = child position)
  kRelReferences = 2,     // clang_getCursorReferenced
  kRelDefinition = 3,     // clang_getCursorDefinition
  kRelCanonical = 4,      // clang_getCanonicalCursor
  kRelSemanticParent = 5, // clang_getCursorSemanticParent (decls only)
  kRelLexicalParent = 6,  // clang_getCursorLexicalParent (decls only)
  kRelSpecializes = 7,    // clang_getSpecializedCursorTemplate
  kRelOverrides = 8,      // clang_getOverriddenCursors (ord = list position)
  kRelHasType = 9,        // clang_getCursorType (cursor -> type node)
  kRelTypeDecl = 10,      // clang_getTypeDeclaration (type -> cursor node)
  kRelCanonicalType = 11, // clang_getCanonicalType
  kRelPointee = 12,       // clang_getPointeeType
  kRelElementType = 13,   // clang_getElementType
  kRelResultType = 14,    // clang_getResultType
  kRelArgType = 15,       // clang_getArgType (ord = parameter position)
  kRelNamedType = 16,     // clang_Type_getNamedType (elaborated sugar)
  kRelUnderlyingType = 17,// clang_getTypedefDeclUnderlyingType (cursor -> type)
  kRelTemplateArg = 18,   // clang_Type_getTemplateArgumentAsType (ord = pos)
  kRelClassType = 19,     // clang_Type_getClassType (member pointers)
};

struct Options {
  // Restrict the STRUCTURAL walk to main-file cursors; header entities still
  // appear as shallow nodes when referenced (references/definition/... edges).
  bool main_only = false;
};

struct DumpStats {
  int64_t cursor_nodes = 0;
  int64_t type_nodes = 0;
  int64_t edges = 0;
  int64_t symbols = 0;
  int64_t files = 0;
};

// Create (truncating any existing file) `out_db_path` and dump `tu` into it.
// `source_path` / `args` / `driver` are recorded in `meta` for provenance.
// Throws StorageError / CidxError on failure.
DumpStats dump_tu(const ParsedTu &tu, const std::string &out_db_path,
                  const Options &opts, const std::string &source_path,
                  const std::vector<std::string> &args,
                  const std::optional<std::string> &driver);

} // namespace astgraph
} // namespace cidx

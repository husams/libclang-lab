# Lab Progress

## Part 1 — Foundations
- [ ] 1.1 — What libclang is
- [ ] 1.2 — Pointing Python at libclang (clang_args gotcha)
- [ ] 1.3 — Index & TranslationUnit
- [ ] 1.4 — The Cursor
- [ ] 1.5 — Walking the tree

## Part 2 — Navigating the AST
- [ ] 2.1 — CursorKind (the node taxonomy)
- [ ] 2.2 — Names (spelling vs displayname vs get_usr())
- [ ] 2.3 — Locations & extents
- [ ] 2.4 — Filtering to your file (decl-vs-definition + main-file gotcha)
- [ ] 2.5 — Tokens
- [ ] 2.6 — Build a reusable AST dumper (deliverable)

## Part 3 — Types & Semantics
- [ ] 3.1 — The Type object & TypeKind
- [ ] 3.2 — Canonical types, pointers, arrays, qualifiers
- [ ] 3.3 — Function signatures
- [ ] 3.4 — Records (struct/union)
- [ ] 3.5 — Typedefs & enums
- [ ] 3.6 — Semantic links
- [ ] 3.7 — C++ semantics

## Part 4 — Preprocessor, Diagnostics & Flags
- [ ] 4.1 — Diagnostics
- [ ] 4.2 — Parse options
- [ ] 4.3 — Compiler arguments
- [ ] 4.4 — Macros & inclusions
- [ ] 4.5 — compile_commands.json & CompilationDatabase

## Part 5 — Building Real Tools
- [ ] 5.1 — Symbol extractor -> JSON
- [ ] 5.2 — Find all references via USR
- [ ] 5.3 — Naming-convention linter
- [ ] 5.4 — Call-graph extraction
- [ ] 5.5 — Code metrics

## Part 6 — Advanced & Production
- [ ] 6.1 — Unsaved files
- [ ] 6.2 — reparse
- [ ] 6.3 — Serialized ASTs (PCH-style)
- [ ] 6.3b — PCH as a prefix (precompile header, reuse on include)
- [ ] 6.4 — Code completion
- [ ] 6.5 — Parsing at scale
- [ ] 6.6 — Limits of libclang
- [ ] 6.7 — CAPSTONE: mini semantic indexer

## Part 7 — Capstone Project: `cidx` (symbol indexer + PCH builder)
- [ ] M1 — Index one project in-memory (no PCH, no parallelism)
- [ ] M2 — Persist index to disk + query (def/refs/list)
- [ ] M3 — PCH builder + `-include-pch` acceleration
- [ ] M4 — Scale via multiprocessing (data-not-cursors)
- [ ] M5 — Polish (macros/includes, stats, incremental reindex)

## Part 8 — Compilation Databases in Depth (reference)
- [ ] 8.1 — DB schema (`command` vs `arguments`, `directory` resolution)
- [ ] 8.2 — Generating a DB (CMake/Bear/Ninja/Meson/Bazel)
- [ ] 8.3 — Full flag-strip rule set + `clang_args()` merge
- [ ] 8.4 — Getting flags for headers (includer / sibling / default resolver)

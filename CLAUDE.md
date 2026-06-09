# libclang Lab — Agent Guide

## How to Present This Lab

Present the lab **interactively, section by section**. Do NOT dump entire parts at once.

### Flow
1. Check `docs/PROGRESS.md` to see where the user left off
2. Show ONE section at a time
3. After each section with a runnable script:
   - Show the script's purpose and what output to expect
   - Ask if the user wants to run it
   - Offer to execute it (from the repo root) if the user agrees
   - Wait for confirmation before proceeding
4. After completing a part, update `docs/PROGRESS.md` and ask to continue

### Before Making Changes
Always:
1. Show what will be run (or changed) and explain why
2. Show the command/output the user should expect
3. Confirm success before moving on

### Progress Tracking
Update `docs/PROGRESS.md` after each completed section.
Mark sections with `[x]` when complete.

## Lab Structure

```
libclang-lab/
├── CLAUDE.md                          ← This agent guide
├── docs/
│   ├── README.md                      ← TOC, environment, quick nav
│   ├── PROGRESS.md                    ← Section-by-section checklist
│   ├── part_1_foundations.md          ← Part 1 — Foundations
│   ├── part_2_navigating_ast.md       ← Part 2 — Navigating the AST
│   ├── part_3_types_semantics.md      ← Part 3 — Types & Semantics
│   ├── part_4_preprocessor_diagnostics.md  ← Part 4 — Preprocessor, Diagnostics & Flags
│   ├── part_5_building_tools.md       ← Part 5 — Building Real Tools
│   ├── part_6_advanced_production.md  ← Part 6 — Advanced & Production
│   ├── part_7_capstone_project.md     ← Part 7 — Capstone Project brief (cidx: indexer + PCH builder; no scripts)
│   └── part_8_compile_db_headers.md   ← Part 8 — Compilation Databases in Depth + getting flags for headers (reference; no scripts)
├── manifests/                         ← Sample C/C++ sources + a multi-file project
│   ├── shapes.h                       ← Struct/enum/typedef decls + function prototypes (Parts 1–5)
│   ├── shapes.c                       ← The primary C sample: definitions for shapes.h (Parts 1–6)
│   ├── geometry.hpp                   ← C++ header: namespace, classes, inheritance, templates (Part 3)
│   ├── geometry.cpp                   ← C++ sample using geometry.hpp (Parts 3, 6)
│   ├── calls.c                        ← Functions calling each other (AST dumper + call-graph: Parts 2, 5)
│   ├── messy.c                        ← Bad naming + nested control flow (linter + metrics: Part 5)
│   ├── macros.c                       ← #define / #include sample for preprocessing (Part 4)
│   ├── compile_commands.json          ← Top-level compilation DB referencing the manifests samples
│   └── project/                       ← Two-TU project for cross-TU USR work (Parts 4–6)
│       ├── mathlib.h                  ← Header: multiply / square prototypes
│       ├── mathlib.c                  ← Definitions for mathlib.h
│       ├── app.c                      ← Caller TU using mathlib (cross-TU references)
│       └── compile_commands.json      ← Compilation DB for the project (CompilationDatabase source)
└── scripts/                           ← All lab scripts (run from repo root)
    ├── _helpers.py                    ← Shared module: clang_args/parse/walk/loc/in_main_file/top_level/fatal_diagnostics
    ├── _smoke_test.py                 ← Ground-truth API check — every API the lab teaches, asserted
    │
    ├── p1_load_and_args.py            ← 1.2 Point Python at libclang; broken-then-fixed clang_args parse
    ├── p1_translation_unit.py         ← 1.3 Index.create() / index.parse() / tu.spelling
    ├── p1_first_cursor.py             ← 1.4 tu.cursor root + main-file top-level children
    ├── p1_walk.py                     ← 1.5 get_children() + walk(): indented top-level dump of shapes.c
    │
    ├── p2_cursor_kinds.py             ← 2.1 CursorKind taxonomy of shapes.c top-level cursors
    ├── p2_names.py                    ← 2.2 spelling vs displayname vs get_usr()
    ├── p2_locations.py                ← 2.3 SourceLocation / SourceRange (extent.start/.end)
    ├── p2_main_file.py                ← 2.4 in_main_file() + declaration-vs-definition (shapes_total_area)
    ├── p2_tokens.py                   ← 2.5 get_tokens() / TokenKind over shape_translate
    ├── p2_ast_dump.py                 ← 2.6 Reusable indented AST dumper (run on calls.c)
    │
    ├── p3_types.py                    ← 3.1 cursor.type / type.kind / type.spelling
    ├── p3_canonical.py                ← 3.2 get_canonical / get_pointee / arrays / qualifiers
    ├── p3_functions.py                ← 3.3 result_type / get_arguments vs argument_types / variadic
    ├── p3_records.py                  ← 3.4 struct Shape fields via type.get_fields()
    ├── p3_typedefs_enums.py           ← 3.5 underlying_typedef_type + ENUM_CONSTANT_DECL.enum_value
    ├── p3_semantics.py                ← 3.6 referenced / get_definition / canonical / get_usr / parents
    ├── p3_cpp.py                      ← 3.7 C++ semantics: access, namespaces, inheritance, templates
    │
    ├── p4_diagnostics.py              ← 4.1 tu.diagnostics: severity / spelling / location / fixits
    ├── p4_parse_options.py            ← 4.2 PARSE_* options (SKIP_FUNCTION_BODIES demo)
    ├── p4_compiler_args.py            ← 4.3 -I / -D / -std change the AST (same source, different AST)
    ├── p4_preprocessing.py           ← 4.4 DETAILED_PREPROCESSING_RECORD: macros & inclusions (macros.c)
    ├── p4_compiledb.py                ← 4.5 CompilationDatabase + stripping driver/source/-c/-o args
    │
    ├── p5_symbols.py                  ← 5.1 Symbol extractor → sorted JSON
    ├── p5_find_refs.py                ← 5.2 Find all references via USR across both project TUs
    ├── p5_linter.py                   ← 5.3 Naming-convention linter over messy.c
    ├── p5_callgraph.py                ← 5.4 Call-graph extraction over calls.c
    ├── p5_metrics.py                  ← 5.5 Code metrics (LOC, nesting, branches) over messy.c
    │
    ├── p6_unsaved.py                  ← 6.1 unsaved_files: parse an in-memory buffer
    ├── p6_reparse.py                  ← 6.2 tu.reparse(): in-place update after a buffer edit
    ├── p6_pch.py                      ← 6.3 tu.save() + TranslationUnit.from_ast_file() (PCH-style)
    ├── p6_pch_header.py               ← 6.3b precompile header (-x c-header) + reuse via -include-pch
    ├── p6_complete.py                 ← 6.4 tu.codeComplete() member completion at 's->'
    ├── p6_scale.py                    ← 6.5 multiprocessing.Pool over project files (extract DATA, not cursors)
    ├── p6_limits.py                   ← 6.6 Limits of libclang (template-instantiation visibility)
    └── p6_index.py                    ← 6.7 CAPSTONE: mini semantic indexer (symbol table + xref map)
```

## Environment

| Component | Details |
|-----------|---------|
| Platform | macOS, Apple Silicon |
| Python | 3.14 |
| libclang | pip `libclang` 18.1.1 (bundles the native dylib — `import clang.cindex` just works) |
| System clang | `/usr/bin/clang` (provides builtin headers + SDK path via `xcrun`) |
| Repo root | `/Users/husam/workspace/qemu-vms` |
| Lab root | `/Users/husam/workspace/qemu-vms/libclang-lab` |
| Shared module | `scripts/_helpers.py` — `clang_args() / parse() / walk() / loc() / in_main_file() / top_level() / fatal_diagnostics()` |

### Prerequisites

```bash
# 1. Install the libclang bindings + bundled dylib
pip install libclang

# 2. Ensure command-line tools (system clang, SDK, builtin headers) are present
xcode-select --install   # no-op if already installed

# 3. Confirm the bindings load
python3 -c "import clang.cindex; print('clang.cindex OK')"
```

**Canonical run command** — always run scripts from the repo root so the bundled samples resolve:

```bash
cd /Users/husam/workspace/qemu-vms
python3 libclang-lab/scripts/<name>.py
```

Optional sanity check before starting: `python3 libclang-lab/scripts/_smoke_test.py` exercises every API the lab teaches and asserts the results.

## The three featured gotchas

Each gotcha has a single **home section** where it is demonstrated broken-then-fixed:

1. **Builtin-headers fatal / `clang_args()`** — the pip `libclang` wheel ships the dylib but NOT Clang's builtin headers, so a bare `args=[]` parse emits a fatal `stddef.h file not found` that **silently truncates the AST**. Fixed by `clang_args()` (adds `-isysroot <SDK>` + `-I <clang resource-dir>/include`).
   Home: **§1.2** → `docs/part_1_foundations.md` (cross-referenced from §4.1).
2. **Declaration-vs-definition + main-file filtering** — parsing pulls in headers; an `#include`d prototype and the `.c` definition are two cursors with the same spelling. Filter with `in_main_file()` and resolve with `is_definition()` / `get_definition()`.
   Home: **§2.4** → `docs/part_2_navigating_ast.md`.
3. **Stripping compile-command args** — raw `compile_commands.json` arguments contain the driver token (`cc`), the source filename, and `-c` / `-o` output pairs that `libclang.parse` does NOT want; strip them before feeding args into `parse()`.
   Home: **§4.5** → `docs/part_4_preprocessor_diagnostics.md`.

# libclang Lab

A hands-on lab for learning Python libclang (`clang.cindex`) by building everything from scratch.

You parse real C/C++ with Clang's stable C API, walk the AST, resolve types and
semantics, drive the preprocessor and diagnostics, and end by assembling small,
real tools — a symbol extractor, a cross-TU reference finder, a linter, a
call-graph builder, and a mini semantic indexer that echoes a production
`cpp-mcp`/`cpp-indexer` pattern.

## Environment

| | |
|---|---|
| **Machine** | macOS, Apple Silicon (arm64) |
| **Python** | 3.14 (`python3`) |
| **libclang** | pip `libclang` 18.1.1 (bundles `libclang.dylib`; the `clang.cindex` bindings ship with it) |
| **clang / SDK** | system `clang` at `/usr/bin/clang` (Apple clang 17) for builtin headers; macOS SDK via `xcrun --show-sdk-path` |
| **Sample sources** | `manifests/` — `shapes.c`/`shapes.h`, `calls.c`, `macros.c`, `messy.c`, `geometry.cpp`/`geometry.hpp`, and a multi-file `project/` (`mathlib.c`, `app.c`, `mathlib.h`) with `compile_commands.json` |

The shared module `scripts/_helpers.py` provides
`clang_args()` / `parse()` / `walk()` / `loc()` / `in_main_file()` /
`top_level()` / `fatal_diagnostics()` — every lab script imports from it.

## Lab Parts

| #   | Part                                                                    | Topics                                                                                                                                                                                                                                                                   |
| --- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | [Foundations](part_1_foundations.md)                                    | Where libclang sits in the Clang/LLVM stack; libclang vs LibTooling vs the clang CLI; loading the dylib (`Config.set_library_file`); the `clang_args()` builtin-headers gotcha; `Index`, `TranslationUnit`, `Cursor`; walking the AST                                    |
| 2   | [Navigating the AST](part_2_navigating_ast.md)                          | `CursorKind` taxonomy; spelling vs displayname vs `get_usr()`; `SourceLocation` & `SourceRange` extents; main-file filtering and declaration-vs-definition; tokens (`TokenKind`); a reusable AST dumper                                                                  |
| 3   | [Types & Semantics](part_3_types_semantics.md)                          | `Type` & `TypeKind`; canonical types, pointers, arrays, qualifiers; function signatures & variadics; records/fields; typedefs & enums; semantic links (`referenced`, `get_definition`, `canonical`); C++ access specifiers, namespaces, inheritance, virtual & templates |
| 4   | [Preprocessor, Diagnostics & Flags](part_4_preprocessor_diagnostics.md) | The diagnostics API (severity, fixits); parse options (`PARSE_DETAILED_PROCESSING_RECORD`, `SKIP_FUNCTION_BODIES`); how `-I`/`-D`/`-std` reshape the AST; macros & inclusions; `CompilationDatabase` + `compile_commands.json`                                           |
| 5   | [Building Real Tools](part_5_building_tools.md)                         | Symbol extractor → JSON; find-all-references via USR across TUs; naming-convention linter; call-graph extraction; code metrics (LOC, nesting, branches)                                                                                                                  |
| 6   | [Advanced & Production](part_6_advanced_production.md)                  | Unsaved (in-memory) files; `reparse`; serialized ASTs (PCH-style save/reload); code completion; parsing at scale (cursor lifetime, one Index/process, multiprocessing); limits of libclang; capstone mini semantic indexer                                               |
| 7   | [Capstone Project: `cidx`](part_7_capstone_project.md)                  | Project brief (no shipped scripts): build a real CLI symbol indexer over `compile_commands.json` + a PCH builder to accelerate it — composing §4.5, §6.7, §6.3b, §6.5. Architecture, milestones, layout, and a definition-of-done backlog                               |
| 8   | [Compilation Databases in Depth](part_8_compile_db_headers.md)          | Reference: what `compile_commands.json` really contains (`command` vs `arguments`, `directory` resolution); how to generate it (CMake/Bear/Ninja/Meson/Bazel); the full flag-strip rule set; and **how to get flags for headers**, which the DB never lists           |

## Prerequisites

1. **Install the libclang wheel** (bundles the native dylib + `clang.cindex` bindings):

   ```sh
   pip install libclang
   ```

2. **System clang for builtin headers + SDK.** The pip wheel ships the dylib but
   **not** Clang's builtin headers (`stddef.h`, `stdarg.h`, …) or the macOS SDK,
   so the lab borrows them from the system toolchain. Install the Command Line
   Tools if you don't have them:

   ```sh
   xcode-select --install
   ```

   `clang_args()` discovers these for you via `clang -print-resource-dir` and
   `xcrun --show-sdk-path`.

3. **Confirm the bindings load:**

   ```sh
   python3 -c "import clang.cindex"
   ```

   No output (and exit 0) means libclang loaded. If it raises, see Part 1 §1.2
   for `Config.set_library_file` / `set_library_path`.

## The libclang gotchas

Three traps account for most "why is my AST empty / wrong?" pain. Each has a
home section that demonstrates it broken-then-fixed:

- **Builtin headers / `clang_args()`** — owned by **Part 1 §1.2.** The pip wheel
  ships the dylib but not Clang's builtin headers, so a bare `parse(args=[])` of
  `shapes.c` emits a **fatal** `'stddef.h' file not found` that *silently
  truncates the AST*. `clang_args()` adds `-isysroot <SDK>` and
  `-I <clang resource-dir>/include` to fix it.
- **Declaration vs definition** — owned by **Part 2 §2.4.** An `#include`d
  prototype and the `.c` definition are *two distinct cursors* with the same
  spelling; a naive `walk()`+`next()` grabs the prototype (no body). Use
  `is_definition()` / `get_definition()` / `canonical`.
- **Main-file filtering** — owned by **Part 2 §2.4.** Parsing pulls in every
  `#include`d header (for C++, thousands of libc++ nodes). Filter with
  `in_main_file()` / `top_level()` before you do anything.

## Conventions

- Inline code (`like_this`) marks identifiers, API names, flags, and file paths.
- Activity markers in the lesson text:
  - 🔍 **Explore** — read / inspect, no changes.
  - ✏️ **Write** — create or edit a script.
  - ✅ **Check** — run it and confirm the expected output.
- ⚠️ **CAUTION** — a gotcha or footgun; read before you run.
- Scripts run from the repo root `/Users/husam/workspace/qemu-vms`:

  ```sh
  python3 libclang-lab/scripts/<name>.py
  ```

  Output is kept deterministic (sorted, basenamed, main-file-filtered) so runs
  are reproducible.

## Quick start

From `/Users/husam/workspace/qemu-vms`, confirm the toolchain, then start Part 1:

```sh
python3 libclang-lab/scripts/_smoke_test.py   # all checks PASS → you're ready
```

Then open [Part 1 — Foundations](part_1_foundations.md).

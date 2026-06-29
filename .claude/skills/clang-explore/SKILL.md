---
name: clang-explore
description: >-
  Explore, query, and reason about a C/C++ codebase by parsing it live with
  libclang (clang.cindex) through a bundled Python API (clang_explore), instead
  of reading or grepping source. Use to find symbols, dump an AST, inspect types
  and signatures, list a function's callees/callers, resolve declaration vs
  definition, follow references by USR across files, read parse diagnostics, or
  pull compile flags from compile_commands.json. Needs no prebuilt index — it
  parses files on demand. (For repo-scale graph queries over a prebuilt index,
  use the cidx-graph skill instead.)
---

# clang-explore — parse C/C++ live, reason on the AST

This skill bundles a self-contained Python module, **`clang_explore`**, that wraps
the libclang Python bindings (`clang.cindex`). It lets a coding agent **answer
structural questions about C/C++ by parsing the real AST** — symbols, types,
calls, references, diagnostics — rather than reading whole files or grepping for
text. You write a short snippet against the API, **execute it**, and read back a
handful of facts grounded in `file:line`.

No prebuilt index is required: every query parses the file(s) on demand. That
makes it correct on freshly-edited code and on repos that were never indexed.

## When to use this vs. cidx-graph

- **clang-explore (this skill)** — live, file-scoped, no setup. The AST ground
  truth for *this* file or a few files: exact types, signatures, AST shape,
  macro expansion, declaration-vs-definition, diagnostics, what a function calls.
- **cidx-graph** — a *prebuilt* whole-repo graph (`index.db`). Use it for
  repo-scale questions: all callers of a symbol across the codebase, full class
  hierarchy, reachability, impact analysis. Pure-libclang repo scans are slow;
  don't reimplement the graph here.

## Operating rules — read before answering

1. **Don't read source files or grep to answer a structural question** (symbols,
   types, signatures, callees, references, hierarchy, diagnostics). Parse and
   query the AST. That is the whole point of this skill.
2. **Generate code, execute it, reason on the result.** Write a snippet using the
   API, run it with Bash, look at the small printed output. Don't hold the AST in
   your head.
3. **ALWAYS pass real compile flags.** Use `clang_args(std=...)` (handles the
   macOS SDK + Clang builtin headers in C++-correct order), or `Repo` which pulls
   them from `compile_commands.json`. A bare `parse(path)` with no args reproduces
   the #1 gotcha — a fatal "stddef.h not found" that **silently truncates the
   AST** and gives you wrong answers.
4. **Check `fatal_diagnostics(tu)` after every parse.** Non-empty ⇒ the AST may
   be incomplete; fix the flags (usually a missing `-I` or wrong `-std`) before
   trusting any result. Say so if you can't get a clean parse.
5. **Filter to the main file** with `in_main_file()` / `top_level()` /
   `main_only=True`. Parsing pulls in every `#include`; an included prototype and
   the `.c` definition are two different cursors with the same spelling. But when
   a symbol is *declared in a header* (most structs/enums/typedefs/class decls),
   pass `main_only=False` or it won't be found.
6. **Ground every claim in a site.** When you assert "X calls Y" or "T has field
   F", cite the `loc(cursor)` → `path:line:col` the API returns.
7. **Keep queries bounded.** Scope `Repo.find(files=[...])`, cap depth in
   `dump_ast(max_depth=...)`, return counts/representative rows — never dump a
   whole AST or every node.
8. **Read-only.** This skill never edits source or writes an index.

## Setup

The module lives next to this file. Add it to `sys.path` (or set `PYTHONPATH`)
and import. It needs `pip install libclang` (bundles the native dylib).

```python
import sys
sys.path.insert(0, "/Users/husam/workspace/qemu-vms/libclang-lab/.claude/skills/clang-explore")
import clang_explore as ce
```

## Basic usage

```python
import clang_explore as ce
import clang.cindex as cx

# --- single file -------------------------------------------------------------
tu = ce.parse("src/shapes.c", args=ce.clang_args(std="c11"))
assert not ce.fatal_diagnostics(tu)          # rule 4: trust nothing until clean

# symbols (glob on spelling, optional kind filter, main-file only by default)
for c in ce.find_symbols(tu, "*", kinds=[cx.CursorKind.FUNCTION_DECL]):
    print(c.kind.name, c.displayname, ce.loc(c))

# what a function calls, grounded in sites
print(ce.callees_of(tu, "shapes_total_area"))      # [(name, usr, "file:line"), ...]
print(ce.callers_of(tu, "shape_area"))             # callers WITHIN this TU

# AST shape (bounded!)
print(ce.dump_ast(tu, max_depth=3))

# types / signatures — read cursors directly
fn = ce.find_symbols(tu, "shape_area")[0]
print(fn.result_type.spelling, [a.type.spelling for a in fn.get_arguments()])

# diagnostics
print(ce.diagnostics(tu))                          # [{severity, spelling, location}]

# --- a project (flags from compile_commands.json) ----------------------------
repo = ce.open_repo("/path/to/repo")               # finds compile_commands.json
print(repo.compile_args("src/foo.cpp"))            # stripped, parse-ready flags
hits = repo.find("multiply", files=["src/a.c", "src/b.c"])   # scope the parse!

# --- references across files by USR (the stable cross-TU key) -----------------
tus = [repo.parse("src/a.c"), repo.parse("src/b.c")]
usr = ce.find_symbols(tus[0], "multiply")[0].get_usr()
print(ce.references_to(tus, usr))                   # [(kind, "file:line"), ...]
```

Quick spot-check from the shell (no snippet needed):

```bash
export PYTHONPATH=/Users/husam/workspace/qemu-vms/libclang-lab/.claude/skills/clang-explore
python3 -m clang_explore symbols src/shapes.c --kind FUNCTION_DECL
python3 -m clang_explore ast     src/shapes.c --depth 3
python3 -m clang_explore callees src/shapes.c shapes_total_area
python3 -m clang_explore diag    src/geometry.cpp --std c++17
python3 -m clang_explore find    .   '*Widget*' --kind CLASS_DECL
```

## The API at a glance (full reference: `references/api.md`)

- **Parse & flags** — `clang_args(std, project_includes, defines, extra)`,
  `parse(path, args, options, unsaved_files)`, `Repo`/`open_repo(root)` with
  `.compile_args(file)` / `.parse(file)` / `.sources()` / `.find(...)`.
- **Traverse & locate** — `walk(cursor)`, `loc(cursor)`, `in_main_file(cursor)`,
  `top_level(tu)`, `dump_ast(tu, max_depth, main_only)`.
- **Query symbols** — `find_symbols(tu, pattern, kinds, main_only)`; then read
  cursor attributes: `.spelling/.displayname/.get_usr()/.kind/.type/
  .result_type/.get_arguments()/.is_definition()/.get_definition()/.referenced/
  .access_specifier/.underlying_typedef_type/.enum_value`.
- **Calls & references** — `callees_of(tu, func)`, `callers_of(tu, func)`
  (within the TU), `references_to(tus, usr)` (cross-file by USR).
- **Diagnostics** — `fatal_diagnostics(tu)`, `diagnostics(tu, min_severity)`.

## The three featured gotchas (full: `references/gotchas.md`)

1. **Header resolution** — bare `parse(path, args=[])` → fatal `stddef.h not
   found` that silently truncates the AST. Fix: `clang_args()` (adds `-isysroot`
   + libc++ + Clang resource dir in C++-correct order) or `Repo` flags.
2. **Declaration vs definition + main-file filtering** — filter with
   `in_main_file()`, resolve with `is_definition()` / `get_definition()`.
3. **Stripping compile-command args** — raw `compile_commands.json` entries carry
   the driver token, the source filename, and `-c`/`-o` pairs that
   `index.parse()` rejects. `Repo.compile_args()` strips them for you.

## References — load only when you need them

- `references/api.md` — every function + the cursor/type attributes you read off
  the AST, with return shapes and when to use each.
- `references/recipes.md` — task → ready-to-run snippet (signatures, call tree,
  struct fields, enum values, typedef chains, macros, cross-file refs, unsaved
  in-memory parsing, reparse).
- `references/gotchas.md` — the three gotchas, broken-then-fixed.

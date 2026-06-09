# Part 7 — Capstone Project: `cidx` (a real symbol indexer + PCH builder)

[← Part 6 — Advanced & Production](part_6_advanced_production.md) | [Part 8 — Compilation Databases in Depth →](part_8_compile_db_headers.md) | [Lab Index →](README.md)

> **Read this first.** Parts 1–6 are a guided lab — one runnable script per
> section. Part 7 is different: it is a **project brief**, not a lesson. It does
> not ship code under `scripts/`. It describes a real tool you build *yourself*
> once the lab APIs are second nature, and points at exactly which lab section
> each piece leans on. Treat the checklists below as your backlog.

## What You're Building

`cidx` ("clang index") — a small but genuinely useful command-line tool with two
cooperating halves:

1. **Indexer** — point it at a project's `compile_commands.json`; it parses every
   translation unit, harvests every symbol (functions, types, globals, macros),
   and builds a persistent, queryable index: *where is X defined?* and *who uses
   X?* across the whole codebase.
2. **PCH builder** — precompile the project's hot headers once (`-x c-header` →
   `tu.save()`), then reuse them via `-include-pch` so the indexer (and repeat
   runs) skip reparsing the same `<vector>`/`shapes.h` on every TU.

The two halves are the point: a naive indexer reparses every header in every TU
and is slow on real codebases; the PCH builder is what makes it fast. This is the
same shape as the production `cpp-mcp` / `cpp-indexer` pattern, scoped down to
something one person finishes in a weekend.

### Why this project

| Lab capstone (§6.7) | This project (`cidx`) |
|---|---|
| Two hardcoded TUs (`mathlib.c`, `app.c`) | Any project via `compile_commands.json` |
| In-memory maps, printed once | Persistent index on disk, re-queryable |
| No caching | PCH-accelerated parsing |
| One script | A CLI with subcommands (`index`, `query`, `pch`) |

Everything you need is already in the lab. Part 7 only asks you to *assemble* it.

---

## Building Blocks (where each piece comes from)

You are not learning new APIs here — you are composing ones you already used.

| `cidx` capability | Lab home | Key API |
|---|---|---|
| Discover TUs + per-file flags | [§4.5](part_4_preprocessor_diagnostics.md) | `CompilationDatabase`, strip driver/source/`-c`/`-o` |
| Correct parse flags (builtin headers) | [§1.2](part_1_foundations.md) | `clang_args()` |
| Walk + filter to the user's code | [§2.4](part_2_navigating_ast.md) | `walk()`, `in_main_file()`, `is_definition()` |
| Cross-TU identity for symbols | [§2.2](part_2_navigating_ast.md), [§5.2](part_5_building_tools.md) | `get_usr()` |
| Symbol table + xref map | [§6.7](part_6_advanced_production.md) | `FUNCTION_DECL`/`CALL_EXPR` + USR keying |
| Macros + includes as symbols | [§4.4](part_4_preprocessor_diagnostics.md) | `PARSE_DETAILED_PREPROCESSING_RECORD` |
| Don't choke on broken files | [§4.1](part_4_preprocessor_diagnostics.md) | `tu.diagnostics`, `fatal_diagnostics()` |
| Precompile headers, reuse them | [§6.3 / §6.3b](part_6_advanced_production.md) | `-x c-header`, `tu.save()`, `-include-pch` |
| Index a codebase fast | [§6.5](part_6_advanced_production.md) | `multiprocessing.Pool`, extract **data** not cursors |

If any row is unfamiliar, go back and re-run that section before starting.

---

## Architecture

```
                 compile_commands.json
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
       cidx pch  ─────────▶  *.pch (precompiled hot headers)
       (build once)                  │
                                     │ -include-pch
                                     ▼
   cidx index ──▶ Pool of workers ──▶ one Index/TU ──▶ extract DATA
        │              (§6.5)                │  (usr, name, kind, loc, refs)
        │                                    ▼
        │                          merge on USR (§6.7)
        ▼                                    │
   index.json / index.sqlite  ◀─────────────┘   (persist symbol table + xref map)
        │
        ▼
   cidx query  ──▶  "where is X" / "who calls X" / "list symbols"
```

Two data structures, both USR-keyed (exactly §6.7, persisted):

```
symbols : usr -> {name, kind, file, line, col, signature?}
xrefs   : usr -> [ {file, line, col}, ... ]      # every use site
```

### The cursor-lifetime rule (the one that bites)

When you fan out over a `Pool` ([§6.5](part_6_advanced_production.md)), **never**
return a `Cursor` from a worker. A cursor is valid only while its
`TranslationUnit` is alive; pickling it across the process boundary returns
garbage or crashes. Each worker must reduce its TU to plain
`(usr, name, kind, "file:line:col")` tuples *before* returning. This is the most
common production libclang bug — design for it from line one.

---

## CLI Surface (target)

```
cidx pch     --db compile_commands.json [--headers shapes.h,geometry.hpp] --out .cidx/pch
cidx index   --db compile_commands.json [--pch .cidx/pch] [--jobs N] --out .cidx/index.json
cidx query   --index .cidx/index.json  def   <symbol>      # where is it defined?
cidx query   --index .cidx/index.json  refs  <symbol>      # who uses it?
cidx query   --index .cidx/index.json  list  [--kind func] # dump the symbol table
cidx stats   --index .cidx/index.json                      # counts, timings
```

Keep output deterministic (sorted, basenamed where sensible) — same discipline as
the rest of the lab, so runs are reproducible and diffable.

---

## Milestones

Build it in vertical slices; each milestone is runnable on its own.

### M1 — Index one project, in-memory (no PCH, no parallelism)
- [ ] Load `compile_commands.json` via `CompilationDatabase` ([§4.5](part_4_preprocessor_diagnostics.md)); strip driver/source/`-c`/`-o` from each command.
- [ ] For each TU: `parse()` with stripped args + `clang_args()`; check `fatal_diagnostics()` and skip (with a warning) any TU that fails fatally.
- [ ] Walk, filter `in_main_file()`, record `FUNCTION_DECL`/`VAR_DECL`/record/typedef/enum **definitions** into `symbols` keyed by `get_usr()`.
- [ ] Record every `CALL_EXPR`/`DECL_REF_EXPR` whose `.referenced` has a USR into `xrefs`.
- [ ] Print the symbol table + answer "who calls X" — i.e. reproduce §6.7 over a *real* `--db`.
- **Test against:** `manifests/project/compile_commands.json` (you know the expected answers from §6.7).

### M2 — Persist + query
- [ ] Serialize `symbols` + `xrefs` to `index.json` (sets → sorted lists).
- [ ] Implement `cidx query def/refs/list` reading the JSON back — no reparsing.
- [ ] Decide name→USR resolution: a name may map to several USRs (overloads, statics in different files); return all, grouped.

### M3 — PCH builder + acceleration
- [ ] `cidx pch`: for each requested header, parse alone with `-x c-header` + `PARSE_INCOMPLETE`, `tu.save()` to `.pch` ([§6.3b](part_6_advanced_production.md)).
- [ ] `cidx index --pch`: prepend `-include-pch <file>` to each TU's args.
- [ ] **Mind the gotcha:** the consuming parse must use the *same* `clang_args()` (sysroot/`-std`) or libclang rejects the PCH as incompatible. Surface that as a clear error, not a silent miss.
- [ ] Report before/after wall-clock so the speedup is visible.

### M4 — Scale (multiprocessing)
- [ ] Move per-TU work into a **top-level** worker function (spawn re-imports the module on macOS/3.14).
- [ ] One `Index` per worker; compute `clang_args()` **once** in the parent and pass it down.
- [ ] Return only picklable data tuples; merge + dedupe on USR in the parent ([§6.5](part_6_advanced_production.md)).

### M5 — Polish
- [ ] Macros + `#include` graph as first-class symbols (`PARSE_DETAILED_PREPROCESSING_RECORD`, [§4.4](part_4_preprocessor_diagnostics.md)).
- [ ] `cidx stats`: symbol counts by kind, TUs indexed/skipped, parse time.
- [ ] Incremental reindex: only reparse TUs whose source mtime changed since the last index.

---

## Suggested Layout

```
cidx/                       ← new top-level project (sibling of libclang-lab, or its own repo)
├── README.md
├── cidx/
│   ├── __init__.py
│   ├── cli.py              ← argparse subcommands: pch / index / query / stats
│   ├── db.py               ← CompilationDatabase load + arg stripping  (§4.5)
│   ├── parse.py            ← clang_args() + parse() + fatal check       (§1.2, §4.1)
│   ├── extract.py          ← walk → (usr,name,kind,loc,refs) tuples     (§2.4, §6.7)
│   ├── pch.py              ← build + apply precompiled headers          (§6.3b)
│   ├── index_store.py      ← in-memory maps + JSON (or sqlite) persistence
│   └── worker.py           ← top-level Pool worker                      (§6.5)
└── tests/
    └── test_against_project.py   ← assert §6.7 answers over manifests/project
```

You may lift `clang_args()` / `walk()` / `loc()` / `in_main_file()` /
`top_level()` / `fatal_diagnostics()` straight out of
`libclang-lab/scripts/_helpers.py` — that module *is* your starting toolkit.

---

## Gotchas to Design Around (don't rediscover these the hard way)

1. **Builtin headers** — always merge `clang_args()` into the stripped DB args, or
   every TU silently truncates on `stddef.h not found` ([§1.2](part_1_foundations.md)).
2. **Declaration vs definition** — a header prototype and the `.c` body share a
   spelling and (often) a USR; only store the one where `is_definition()` is true
   as the *definition*, and treat the rest as references ([§2.4](part_2_navigating_ast.md)).
3. **Strip the compile command** — the driver token (`cc`), the source filename,
   and `-c`/`-o <out>` are not parse args ([§4.5](part_4_preprocessor_diagnostics.md)).
4. **PCH flag match** — produce and consume the PCH with identical sysroot/`-std`
   ([§6.3b](part_6_advanced_production.md)).
5. **No cursors across processes** — reduce to data inside the worker ([§6.5](part_6_advanced_production.md)).
6. **libclang's limits** — templates/implicit nodes are a partial view; don't
   promise template-instantiation precision `cidx` can't deliver. If you need it,
   that's the LibTooling boundary ([§6.6](part_6_advanced_production.md)).

---

## Definition of Done

| Capability | Acceptance check |
|---|---|
| Indexes a real `--db` | `cidx index --db manifests/project/compile_commands.json` produces a JSON index |
| Cross-TU `def`/`refs` | `cidx query refs multiply` returns the call sites from **both** `app.c` and `mathlib.c` (matches §6.7) |
| Survives bad input | A TU with a fatal diagnostic is skipped with a warning, not a crash |
| PCH speedup | `cidx index --pch …` is measurably faster than without, same results |
| Parallel | `--jobs N` uses N processes, returns identical merged output to `--jobs 1` |
| Persistent | `query` reads the saved index with **zero** reparsing |

When all six pass against `manifests/project/`, point `cidx` at a larger real C
project (anything with a `compile_commands.json` from CMake's
`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`) and watch it index the whole thing.

---

## Stretch Goals

- **SQLite backend** instead of JSON — `WHERE kind='func'`, indexed USR lookups, scales past a few thousand symbols.
- **`cidx serve`** — wrap the query layer as an MCP server (the real `cpp-mcp` shape), exposing `get_definition` / `get_references` as tools.
- **Call graph export** — DOT/Graphviz from `xrefs` ([§5.4](part_5_building_tools.md)).
- **Dead-code hint** — defined symbols with zero entries in `xrefs` (with the obvious caveats about external linkage / entry points).
- **Watch mode** — re-run incremental index on file change.

---

## Checkpoint

| You'll have proven you can | By |
|---|---|
| Drive libclang from a real compilation DB | `cidx index` over `compile_commands.json` |
| Build a persistent cross-TU symbol index | USR-keyed `symbols` + `xrefs` on disk |
| Make parsing fast and correct at scale | PCH reuse + multiprocessing, data-not-cursors |
| Ship a tool, not a script | a `cidx` CLI with `pch` / `index` / `query` / `stats` |

That is the whole lab, turned into something you'd actually keep on your `PATH`.

---

[← Part 6 — Advanced & Production](part_6_advanced_production.md) | [Part 8 — Compilation Databases in Depth →](part_8_compile_db_headers.md) | [Lab Index →](README.md)

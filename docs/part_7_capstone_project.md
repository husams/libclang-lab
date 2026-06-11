# Part 7 — Capstone Project: `cidx` (a symbol indexer + call-graph builder)

[← Part 6 — Advanced & Production](part_6_advanced_production.md) | [Part 8 — Compilation Databases in Depth →](part_8_compile_db_headers.md) | [Lab Index →](README.md)

> **Read this first.** Parts 1–6 are a guided lab — one runnable script per
> section. Part 7 is different: it is a **project brief**, not a lesson. It does
> not ship code under `scripts/`. It describes a real tool you build *yourself*
> once the lab APIs are second nature, and points at exactly which lab section
> each piece leans on. Treat the checklists below as your backlog.

## What You're Building

`cidx` ("clang index") — a small but genuinely useful command-line tool with two
cooperating halves:

1. **Symbol indexer** — point it at a project's `compile_commands.json`; it
   parses every translation unit, harvests every symbol (functions, types,
   globals), and persists a queryable **SQLite** index: *where is X defined?*
   and *who uses X?* across the whole codebase.
2. **Call-graph builder** — from the same walk, record every caller→callee edge
   (USR-keyed, so edges resolve **across** translation units), persist the graph,
   and answer the questions an indexer alone can't: *who can reach X?*, *what
   does X transitively call?*, *is there a path from `main` to `multiply`?*,
   *which functions are never called?*, *where are the cycles?*

The two halves are the point: `xrefs` tells you *that* a symbol is used at some
file:line; the call graph tells you *who* uses it and lets you walk the
relationship transitively. One AST walk feeds both. This is the same shape as
the production `cpp-mcp` / `cpp-indexer` pattern (`get_definition` /
`get_references` / graph queries), scoped down to something one person finishes
in a weekend.

### Why this project

| Lab version                                    | This project (`cidx`)                                            |
| ---------------------------------------------- | ---------------------------------------------------------------- |
| §6.7: two hardcoded TUs (`mathlib.c`, `app.c`) | Any project via `compile_commands.json`                          |
| §5.4: call graph by **name**, single TU        | Call graph by **USR**, cross-TU                                  |
| In-memory maps, printed once                   | Index + graph persisted in SQLite, re-queryable                  |
| Flat "who calls X" answer                      | Transitive callers/callees, paths, cycles, dead code, DOT export |
| One script                                     | A CLI with subcommands (`index`, `query`, `calls`, `stats`)      |

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
| Symbol table + xref map | [§6.7](part_6_advanced_production.md) | `FUNCTION_DECL`/`DECL_REF_EXPR` + USR keying |
| Call edges (caller → callee) | [§5.4](part_5_building_tools.md) | track enclosing `FUNCTION_DECL`, `CALL_EXPR.referenced` |
| Don't choke on broken files | [§4.1](part_4_preprocessor_diagnostics.md) | `tu.diagnostics`, `fatal_diagnostics()` |
| Index a codebase fast | [§6.5](part_6_advanced_production.md) | `multiprocessing.Pool`, extract **data** not cursors |

If any row is unfamiliar, go back and re-run that section before starting.

---

## Architecture

```
                 compile_commands.json
                          │
                          ▼
   cidx index ──▶ Pool of workers ──▶ one Index/TU ──▶ extract DATA
        │              (§6.5)                │   symbols: (usr, name, kind, loc)
        │                                    │   xrefs:   (usr, use-site)
        │                                    │   edges:   (caller_usr, callee_usr, call-site)
        │                                    ▼
        │                          merge on USR (§6.7 + §5.4)
        ▼                                    │
   .cidx/index.db (SQLite) ◀─────────────────┘   (symbol table + xref map + call graph)
        │
        ├──▶  cidx query  ──▶  "where is X defined" / "who uses X" / "list symbols"
        └──▶  cidx calls  ──▶  callers/callees (transitive) / path A→B / cycles / dead code / DOT
```

Three tables, all USR-keyed (§6.7's two maps plus §5.4's edges), stored with the
stdlib `sqlite3` module — no dependencies:

```sql
CREATE TABLE symbols (
    usr   TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    kind  TEXT NOT NULL,             -- 'function', 'struct', 'var', ...
    file  TEXT NOT NULL,
    line  INTEGER NOT NULL,
    col   INTEGER NOT NULL,
    signature TEXT                   -- e.g. 'int multiply(int, int)'
);
CREATE TABLE xrefs (                 -- every use site
    usr   TEXT NOT NULL,
    file  TEXT NOT NULL,
    line  INTEGER NOT NULL,
    col   INTEGER NOT NULL
);
CREATE TABLE calls (                 -- every call edge
    caller_usr TEXT NOT NULL,
    callee_usr TEXT NOT NULL,
    file  TEXT NOT NULL,
    line  INTEGER NOT NULL
);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_xrefs_usr    ON xrefs(usr);
CREATE INDEX idx_calls_caller ON calls(caller_usr);
CREATE INDEX idx_calls_callee ON calls(callee_usr);   -- the reverse map IS this index
```

Note what the indexes buy you: `idx_calls_callee` *is* the reverse
(callee → callers) map — you never build or store it separately. Add a small
`meta(key, value)` table for the DB path, schema version, and parse timings
(`cidx stats` reads it).

### The two rules that make the graph correct

1. **Track the enclosing function during the walk.** §5.4 did this in one TU:
   when you descend into a `FUNCTION_DECL` that `is_definition()`, remember its
   USR; every `CALL_EXPR` found below it is an edge *from* that USR. Recursion in
   your walker (a function defined inside no other function in C) keeps this a
   single variable, not a stack.
2. **Resolve the callee semantically, never by spelling.** `CALL_EXPR.referenced`
   (§3.6) gives the declaration actually being called; `get_usr()` on *that* is
   the edge target. Two static functions named `helper` in different files get
   different USRs — a name-keyed graph (the §5.4 shortcut) silently merges them.

### The cursor-lifetime rule (the one that bites)

When you fan out over a `Pool` ([§6.5](part_6_advanced_production.md)), **never**
return a `Cursor` from a worker. A cursor is valid only while its
`TranslationUnit` is alive; pickling it across the process boundary returns
garbage or crashes. Each worker must reduce its TU to plain
`(usr, name, kind, "file:line:col")` and `(caller_usr, callee_usr, "file:line")`
tuples *before* returning. This is the most common production libclang bug —
design for it from line one.

---

## CLI Surface (target)

```
cidx index   --db compile_commands.json [--jobs N] --out .cidx/index.db
cidx query   --index .cidx/index.db  def     <symbol>      # where is it defined?
cidx query   --index .cidx/index.db  refs    <symbol>      # who uses it (any use)?
cidx query   --index .cidx/index.db  list    [--kind func] # dump the symbol table
cidx calls   --index .cidx/index.db  callers <fn> [--depth N]   # who (transitively) calls fn?
cidx calls   --index .cidx/index.db  callees <fn> [--depth N]   # what does fn (transitively) call?
cidx calls   --index .cidx/index.db  path    <from> <to>        # one call path, or "no path"
cidx calls   --index .cidx/index.db  cycles                     # recursion / mutual recursion
cidx calls   --index .cidx/index.db  dead    [--roots main]     # defined, never reached from roots
cidx calls   --index .cidx/index.db  dot     [--root <fn>] --out graph.dot
cidx stats   --index .cidx/index.db                             # counts, edges, timings
```

Two distinct inputs, two flags: `--db` is always the project's
`compile_commands.json` (what to parse); `--index` is always the SQLite file
(what was parsed). Only `cidx index` touches libclang — everything downstream is
pure SQL + graph walking.

Keep output deterministic (sorted, basenamed where sensible) — same discipline as
the rest of the lab, so runs are reproducible and diffable.

---

## Milestones

Build it in vertical slices; each milestone is runnable on its own.

### M1 — Index one project, in-memory (symbols + xrefs)
- [ ] Load `compile_commands.json` via `CompilationDatabase` ([§4.5](part_4_preprocessor_diagnostics.md)); strip driver/source/`-c`/`-o` from each command.
- [ ] For each TU: `parse()` with stripped args + `clang_args()`; check `fatal_diagnostics()` and skip (with a warning) any TU that fails fatally.
- [ ] Walk, filter `in_main_file()`, record `FUNCTION_DECL`/`VAR_DECL`/record/typedef/enum **definitions** into `symbols` keyed by `get_usr()`.
- [ ] Record every `CALL_EXPR`/`DECL_REF_EXPR` whose `.referenced` has a USR into `xrefs`.
- [ ] Print the symbol table + answer "who uses X" — i.e. reproduce §6.7 over a *real* `--db`.
- **Test against:** `manifests/project/compile_commands.json` (you know the expected answers from §6.7).

### M2 — Call edges (the graph half)
- [ ] During the same walk, track the enclosing function definition's USR; on each `CALL_EXPR`, resolve `.referenced.get_usr()` and append a `(caller, callee, site)` edge.
- [ ] Merge edges across TUs on USR — `main` (app.c) → `multiply` (mathlib.c) must come out as **one** edge even though the two cursors live in different TUs.
- [ ] Handle the misses honestly: a `CALL_EXPR` whose `.referenced` is `None` (call through a function pointer) gets logged to an `unresolved` list, not dropped silently.
- **Test against:** `manifests/project/` — exactly three edges: `square→multiply`, `main→square`, `main→{add,multiply,printf}` (decide and document whether you keep system-header callees like `printf`). Then `manifests/calls.c` — `recurse→recurse` must appear as a self-edge.

### M3 — Persist to SQLite + query
- [ ] Create the schema above in `index_store.py` (stdlib `sqlite3`); bulk-insert with `executemany` inside **one** transaction — row-at-a-time autocommit is 100× slower.
- [ ] Write atomically: build `.cidx/index.db.tmp`, then `os.replace()` over the old DB — a crashed index run must not leave a half-written index.
- [ ] Implement `cidx query def/refs/list` and flat (depth-1) `cidx calls callers/callees` as plain SQL reads — no reparsing, no loading whole tables into memory.
- [ ] Name→USR resolution is a query, not a dict: `SELECT usr FROM symbols WHERE name = ?` may return several rows (overloads, statics in different files); return all, grouped.
- [ ] Keep output deterministic: every query ends in `ORDER BY` — SQLite gives no row order for free.

### M4 — Graph algorithms
- [ ] `callers/callees --depth N`: BFS where each frontier expansion is one indexed query (`SELECT caller_usr FROM calls WHERE callee_usr IN (...)`); mark visited by USR or recursion loops you forever. (A recursive CTE — `WITH RECURSIVE` — is the all-SQL alternative; try it after the Python version works.)
- [ ] `path <from> <to>`: BFS with parent links; print one witness path as `main → square → multiply`.
- [ ] `cycles`: find strongly-connected components of size >1 *plus* self-edges (`recurse` in `calls.c` is the test case).
- [ ] `dead --roots main`: reachability from the roots; everything defined-but-unreached is a candidate (state the caveats: external linkage, function pointers, callbacks).
- [ ] `dot`: emit Graphviz, names as labels, USRs as node ids (names collide; USRs don't).
- **Test against:** `manifests/calls.c` — `callers leaf_a --depth 9` ⇒ `mid, compute, main`; `path main leaf_b` ⇒ `main → compute → mid → leaf_b`; `cycles` ⇒ `recurse`.

### M5 — Scale (multiprocessing)
- [ ] Move per-TU work into a **top-level** worker function (spawn re-imports the module on macOS/3.14).
- [ ] One `Index` per worker; compute `clang_args()` **once** in the parent and pass it down.
- [ ] Return only picklable data tuples (symbols, xrefs, edges); merge + dedupe on USR in the parent ([§6.5](part_6_advanced_production.md)).
- [ ] `cidx stats`: symbol counts by kind, edge count, TUs indexed/skipped, unresolved-call count, parse time.

---

## Suggested Layout

```
cidx/                       ← new top-level project (sibling of libclang-lab, or its own repo)
├── README.md
├── cidx/
│   ├── __init__.py
│   ├── cli.py              ← argparse subcommands: index / query / calls / stats
│   ├── db.py               ← CompilationDatabase load + arg stripping  (§4.5)
│   ├── parse.py            ← clang_args() + parse() + fatal check       (§1.2, §4.1)
│   ├── extract.py          ← walk → symbol/xref/edge tuples             (§2.4, §5.4, §6.7)
│   ├── graph.py            ← BFS over calls table, paths, SCC/cycles, dead, DOT
│   ├── index_store.py      ← SQLite schema + bulk insert + query helpers (stdlib sqlite3)
│   └── worker.py           ← top-level Pool worker                      (§6.5)
└── tests/
    ├── test_index.py       ← assert §6.7 answers over manifests/project
    └── test_callgraph.py   ← assert edges/paths/cycles over manifests/project + calls.c
```

You may lift `clang_args()` / `walk()` / `loc()` / `in_main_file()` /
`top_level()` / `fatal_diagnostics()` straight out of
`libclang-lab/scripts/_helpers.py` — that module *is* your starting toolkit.

---

## Gotchas to Design Around (don't rediscover these the hard way)

1. **Builtin headers** — always merge `clang_args()` into the stripped DB args, or
   every TU silently truncates on `stddef.h not found` ([§1.2](part_1_foundations.md)) —
   and a truncated AST means *silently missing call edges*, which is worse than a crash.
2. **Declaration vs definition** — a header prototype and the `.c` body share a
   spelling and (often) a USR; only store the one where `is_definition()` is true
   as the *definition*, and only treat a function as a *caller* when you are inside
   its definition ([§2.4](part_2_navigating_ast.md)).
3. **Strip the compile command** — the driver token (`cc`), the source filename,
   and `-c`/`-o <out>` are not parse args ([§4.5](part_4_preprocessor_diagnostics.md)).
4. **Name-keyed graphs lie** — two `static` functions with the same name in
   different files are different nodes. Key every node and edge by USR; render
   names only at display time ([§2.2](part_2_navigating_ast.md), [§5.2](part_5_building_tools.md)).
5. **Function pointers & macros hide edges** — a call through a function pointer
   has no resolvable `.referenced`; a macro that expands to a call attributes the
   edge to the expansion site. Count these in `unresolved` / document the
   behavior — don't pretend the graph is complete ([§6.6](part_6_advanced_production.md)).
6. **No cursors across processes** — reduce to data inside the worker ([§6.5](part_6_advanced_production.md)).
   The same rule covers SQLite: workers never open the DB; they return tuples and
   the **parent alone** writes, in one transaction. Concurrent writers are
   SQLite's weak spot — don't go there.
7. **libclang's limits** — template instantiations and implicit calls (C++
   ctors/dtors/operators) are a partial view; don't promise precision `cidx`
   can't deliver. If you need it, that's the LibTooling boundary ([§6.6](part_6_advanced_production.md)).

---

## Definition of Done

| Capability | Acceptance check |
|---|---|
| Indexes a real `--db` | `cidx index --db manifests/project/compile_commands.json` produces `.cidx/index.db`, inspectable with the `sqlite3` shell |
| Cross-TU `def`/`refs` | `cidx query refs multiply` returns the use sites from **both** `app.c` and `mathlib.c` (matches §6.7) |
| Cross-TU call edges | `cidx calls callers multiply` returns `square` (mathlib.c) **and** `main` (app.c) |
| Transitive queries | `cidx calls callers multiply --depth 9` adds `main` via the `square` chain; `path main multiply` prints a witness path |
| Cycles | `cidx calls cycles` over `calls.c` reports `recurse` |
| Dead code | `cidx calls dead --roots main` over `calls.c` reports nothing; removing `compute`'s call to `recurse` would surface `recurse` |
| Survives bad input | A TU with a fatal diagnostic is skipped with a warning, not a crash; unresolved calls are counted, not dropped |
| Parallel | `--jobs N` uses N processes, returns identical merged output to `--jobs 1` |
| Persistent | `query`/`calls` read the saved index with **zero** reparsing |

When all pass against `manifests/project/` and `manifests/calls.c`, point `cidx`
at a larger real C project (anything with a `compile_commands.json` from CMake's
`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`) and render its call graph.

---

## Stretch Goals

- **PCH acceleration** — precompile hot headers (`-x c-header` → `tu.save()`,
  [§6.3b](part_6_advanced_production.md)), reuse via `-include-pch` (`cidx pch`),
  and report the before/after wall-clock. Mind the flag-match gotcha: produce and
  consume with identical sysroot/`-std`. Part 8 covers getting header flags right.
- **JSON export** — `cidx export --json` dumping the tables (sorted) for diffing and `jq`; SQLite stays the source of truth.
- **Macros + `#include` graph** as first-class symbols (`PARSE_DETAILED_PREPROCESSING_RECORD`, [§4.4](part_4_preprocessor_diagnostics.md)) — a fourth table.
- **Incremental reindex** — only reparse TUs whose source mtime changed since the last index; per-file `DELETE` + reinsert is easy now that every row carries `file`.
- **`cidx serve`** — wrap the query layer as an MCP server (the real `cpp-mcp` shape), exposing `get_definition` / `get_references` / `get_callers` as tools.
- **Watch mode** — re-run incremental index on file change.

---

## Checkpoint

| You'll have proven you can | By |
|---|---|
| Drive libclang from a real compilation DB | `cidx index` over `compile_commands.json` |
| Build a persistent cross-TU symbol index | USR-keyed `symbols` + `xrefs` tables in SQLite |
| Build a *correct* cross-TU call graph | USR-keyed edges, resolved via `.referenced`, honest about unresolved calls |
| Answer graph questions, not just lookups | transitive callers/callees, paths, cycles, dead code, DOT export |
| Make parsing fast and correct at scale | multiprocessing, data-not-cursors |
| Ship a tool, not a script | a `cidx` CLI with `index` / `query` / `calls` / `stats` |

That is the whole lab, turned into something you'd actually keep on your `PATH`.

---

[← Part 6 — Advanced & Production](part_6_advanced_production.md) | [Part 8 — Compilation Databases in Depth →](part_8_compile_db_headers.md) | [Lab Index →](README.md)

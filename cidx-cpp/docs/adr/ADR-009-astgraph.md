# ADR-009 — cidx-astgraph: per-TU AST → SQLite graph for Soufflé reasoning

Date: 2026-07-09 · Status: accepted · Version: cidx 0.52.0 (schema v2)

## Context

The cidx index (`index.db`) stores a *resolved, symbol-level* graph (schema
v28). For Datalog reasoning over the **raw C++ AST** of one translation unit —
every cursor, every type, every structural/semantic relation, as libclang sees
them — we need a denser, per-TU fact base that Soufflé can read directly
(`IO=sqlite`), without a resolve phase and without touching the shared index.

## Decision

A separate binary **`cidx-astgraph`** (src/astgraph/, linked from `cidx_core`)
dumps one TU into **`<TU filename>.db`** (e.g. `shapes.c.db`).

Configuration is **shared with cidx**: the source file must be registered in
the cidx `index.db` (`cidx import`); its stored `compile_options` + `driver`
run through the exact `cidx index` pipeline (`CompileDb::sanitize` →
`resolve_options` → `Toolchain`/`Parser`). `--db PATH` overrides the index;
`INDEXER_CACHE` is honoured via `resolve_cache_dir()`.

### Schema (user-approved 2026-07-09, `meta.schema_version = 2`)

**Unified node space** — cursors AND types are rows of `node`; every relation
is a row of `edge`. All columns NOT NULL: `0`/`''` are the "none" sentinels
(Soufflé's sqlite reader has no NULL concept). Real ids start at 1.

| table | columns | notes |
|---|---|---|
| `meta` | key, value | schema_version, generator, source, args (JSON), driver, libclang, main_only, kind_scheme |
| `file` | id, path, is_main | every file that contributes locations |
| `node_kind` | id, name, category | **1..999 = CXCursorKind** (name = Python `CursorKind.name` via `cli::kind_name`, category = decl/ref/expr/stmt/attr/preproc/tu/other), **1000+k = CXTypeKind k** (name = `clang_getTypeKindSpelling`, category = `type`). Seeded on first use; the id scheme is the fixed catalog |
| `relation_kind` | id, name | fixed 19-row catalog (below) |
| `symbol` | id, usr UNIQUE, name, kind_id, linkage | deduped by USR; **joins `index.db` symbol.usr** |
| `node` | id, kind_id, symbol_id, **type_id**, spelling, file_id, line, col, end_line, end_col, is_definition, access, is_const, is_volatile, is_restrict | one row per distinct cursor (clang_equalCursors) or type (kind,data0,data1); `type_id` = the cursor's own type as a node PROPERTY (schema v2 — mirrors `clang_getCursorType` being a cursor accessor); type nodes have no location/symbol/type_id |
| `edge` | src_id, dst_id, rel_id, ord | UNIQUE(...) ON CONFLICT IGNORE; `ord` = child/arg/template-arg position |

### Fixed relation catalog (astgraph.hpp `RelKind`)

1 child · 2 references · 3 definition · 4 canonical · 5 semantic_parent ·
6 lexical_parent · 7 specializes · 8 overrides · *(9 has_type RETIRED in
schema v2 — the cursor's type is the `node.type_id` column)* ·
10 type_decl (type→cursor) · 11 canonical_type · 12 pointee · 13 element_type ·
14 result_type · 15 arg_type · 16 named_type · 17 underlying_type ·
18 template_arg · 19 class_type

Each is grounded 1:1 in a libclang API (`clang_getCursorReferenced`,
`clang_getOverriddenCursors`, `clang_Type_getTemplateArgumentAsType`, …).
Higher-level relations (calls, inherits, …) are deliberately NOT materialized —
they are one Datalog rule away (see `cidx-souffle/astgraph_example.dl`).

### Coverage

Default = the **full TU** including headers (sound closure; geometry.cpp ≈
196k nodes / 553k edges in seconds). `--main-only` prunes header *subtrees*
at the structural walk; header decls referenced from the main file still
appear as shallow nodes via cross-ref edges (geometry.cpp → 372 nodes).

## Consequences

* C++-only **by explicit user decision** (like model.py's Python-only status,
  this is exempt from the Py↔C++ parity rule).
* `<TU>.db` is a derived artifact: rerunning truncates and rewrites it.
* Soufflé reads the tables in place — table names/arities match `.decl`s, no
  adapter views (unlike `cidx_views.sql` over index.db). Gotchas: `ord` and
  `contains` are reserved Soufflé identifiers — rename them in `.decl`s only.
* Tests: `tests/astgraph_test.cpp` (ctest `astgraph_clang_test`, label
  "clang", SKIP-77).

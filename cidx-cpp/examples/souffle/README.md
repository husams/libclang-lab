# Soufflé examples over cidx-astgraph dumps

Datalog reasoning recipes over a per-TU AST dump (`<TU name>.db`, schema v2 —
see `docs/adr/ADR-009-astgraph.md`). Each example is self-contained and covers
one family of use cases; all share `ast_prelude.dl` (EDB declarations, kind
macros, containment/type-reach/call closures).

## Run

```bash
cidx-astgraph path/to/file.c        # -> file.c.db  (source must be `cidx import`ed)
cp file.c.db ast.db                 # Soufflé dbnames are fixed strings
souffle 01_basics.dl                # any example; add -j8 for parallel eval
```

C examples were validated on `manifests/shapes.c`; the OOP example needs a
C++ dump (`manifests/geometry.cpp`).

## The examples

| File | Use cases covered |
|------|-------------------|
| `01_basics.dl` | inventory: main-file functions with locations, record decls, globals **with their `type_id` type**, included headers |
| `02_callgraph.dl` | derive `calls` from AST primitives (CALL_EXPR + references), transitive **reach**, impact-of-one-function, `main`'s live cone, recursion detection, entry points, fan-out counts |
| `03_types.dl` | type graph: decl→type listing, **type-based impact** ("every function touching `Shape` through any pointer/const/typedef sugar"), functions returning pointers, pointer depth, typedef de-sugaring, readonly (`const`-pointee) params |
| `04_deadcode.dl` | negation: dead functions, unused params/vars, unused typedefs, prototypes with no definition — all **TU-scoped** (join `symbol.usr` against index.db for repo-wide truth) |
| `05_oop.dl` | C++: direct + transitive class hierarchy (CXX_BASE_SPECIFIER→references), methods per class via `semantic_parent` (finds out-of-line definitions), override pairs + leaf overriders, who-constructs-what, template `specializes` links |
| `06_metrics.dl` | aggregation: AST body size, param count, fan-in/out, branchiness (if/switch/loops), line span — pipe through `sort -t$'\t' -k2 -nr` for rankings |
| `07_writeback.dl` | **materialize** `calls`/`reach` back into `ast.db` via `.output IO=sqlite`, then consume with plain SQL: reason once, query cheap forever |

## Cheat sheet

- Node kinds: `node_kind.id` = CXCursorKind (1–999) or `1000 + CXTypeKind`;
  the macros in `ast_prelude.dl` name the common ones. When unsure:
  `sqlite3 ast.db 'SELECT * FROM node_kind ORDER BY id;'`
- Relations: 1 child · 2 references · 3 definition · 4 canonical ·
  5 semantic_parent · 6 lexical_parent · 7 specializes · 8 overrides ·
  (9 retired → `node.type_id` column) · 10 type_decl · 11 canonical_type ·
  12 pointee · 13 element_type · 14 result_type · 15 arg_type · 16 named_type ·
  17 underlying_type · 18 template_arg · 19 class_type
- `node.type_id` points at a type node (`kind_id >= 1000`) in the SAME node
  table; 0 = none. All columns are NOT NULL (0/`''` sentinels) — that's what
  lets Soufflé read the tables without adapter views.
- Reserved Soufflé identifiers: **`ord`**, **`contains`** — don't name
  attributes/relations after them (use `pos`, `descendant`, …).
- Cross-TU / repo-wide questions: take `symbol.usr` from the dump and join
  `~/.cache/cidx/index.db` (`ATTACH` in SQLite, or `cidx graph callers …`).

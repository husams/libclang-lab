# cidx-souffle — Soufflé query/reasoning layer over the cidx graph

A **Datalog reasoning layer** over the graph cidx builds. Soufflé
([UPL-1.0](https://github.com/souffle-lang/souffle), permissive) reasons over the cidx
graph with recursive rules and writes the derived relations **back into the same `index.db`**.

**No copy, no separate database.** The index can be multi-GiB / millions of symbols (a
unified index over several repos). This layer creates lightweight integer **VIEWs directly
in `index.db`** and Soufflé reads/writes that same file — it never copies it and never loads
the millions of `symbol` rows (the views touch only the `edge`/`entity_edge` tables).

This is the engine tier of the planned two-language design (see
`~/workspace/wiki/pages/planning/cidx-concept-relation-dsl.md`): a future ANTLR **definition
language** lowers to rules like these; a controlled-English **query language** lowers to the
`SELECT`s shown below.

## Files
| File | Role |
|------|------|
| `cidx_views.sql` | lightweight integer VIEWs created **in `index.db`** (raw `src_id,dst_id` — no `symbol` join) + reset of the output/seed tables |
| `cidx_base.dl`   | **reusable prelude** — all types, edge relations, common derived predicates. `#include` it from any reasoning script |
| `cidx.dl`        | example reasoning script (`#include "cidx_base.dl"` + `.output`s) — copy as a template |
| `run.sh`         | create views + seed in place → run Soufflé (writes results into the same `index.db`) |
| `query.sh`       | **per-symbol** front-end for the three canonical questions — **read-only SQL** (recursive CTEs over the existing `edge`/`entity_edge`), no Soufflé, no writes |
| `astgraph_example.dl` | reasoning over a **`cidx-astgraph` per-TU dump** (`ast.db`, raw libclang AST — cursors, types, child/references/… edges) instead of the resolved index. No adapter views needed: astgraph writes NULL-free tables whose names match the `.decl`s. `cidx-astgraph --output ast.db <source> && souffle astgraph_example.dl` |

## Run
```bash
./run.sh                                 # uses ~/.cache/cidx/index.db, in place
./run.sh /path/to/index.db               # a specific index
./run.sh /path/to/index.db rd_kafka_produceva   # also SEED reach() from matching symbols
```
Soufflé's sqlite directive needs the file named `index.db` in its working dir, so `run.sh`
runs it through a **symlink** in `build/` pointing at the real file — the file is never
moved or copied. The result tables (`subtype`, `edep`, `reach`) and a `seed` table are the
only rows added to `index.db`; drop them anytime to remove all trace.

## Three canonical questions (`query.sh`)
`query.sh` answers the three things you usually want about one symbol — **without Soufflé**.
The edges are already in `edge`/`entity_edge`; a SQLite **recursive CTE anchored on the
symbol** walks the bounded closure directly. It is **read-only**: no `seed` table, no result
tables, nothing written back (the only DB objects are the read-only name views, created once
if missing). Names are the annotated names below.
```bash
# 1. methods reachable FROM a method (transitive callees, the calls+ set)
./query.sh reachable 'app::exercise_cache()'

# 2. generate a callgraph for a function/method — Graphviz DOT (out=callees, in=callers, both)
./query.sh callgraph 'app::exercise_cache()' out          # what it calls
./query.sh callgraph 'app::Cache::get(const std::string &)' in   # who calls it (blast radius)
./query.sh callgraph 'main()' both | dot -Tpng -o cg.png  # render with graphviz

# 3. find classes related to a given class (hierarchy, both directions)
./query.sh classes 'geo::Shape'         # -> ancestors (super), <- descendants (sub)
```
Use `-d /path/to/index.db` for a non-default index. An unknown exact name prints LIKE
candidates so you can find the right annotated spelling.

The recursion runs on indexed integer columns (`edge.src_id`/`dst_id`, `kind`) and resolves
names only at the boundaries, so each query is bounded to the symbol's cone — fast on a large
index without materializing anything. Edge kinds used: calls = `edge.kind 1`; hierarchy =
`edge.kind 2` (inherits) ∪ `entity_edge.kind 1,2` (generalizes/implements).

## query.sh vs. the Soufflé engine — when to use which
The three per-symbol questions don't need a Datalog engine: they're single bounded transitive
closures the DB does itself (above). **Soufflé earns its place for the GLOBAL work** — the
batch `run.sh` materializes whole-graph relations once, and the eventual DSL needs multi-rule
reasoning / negation / stratification. So: **`query.sh` (read-only SQL) for targeted lookups,
Soufflé (`run.sh`) for global materialization and the DSL.**

## What the Soufflé engine computes (`run.sh`, global)
- **`reach(a, b)`** — **seeded** transitive call reachability (`calls+`) from the `seed` table.
- **`subtype(sub, super)`** — transitive class hierarchy (all ancestors), over raw `inherits`
  **and** design-level `generalizes`/`implements`.
- **`cg_out` / `cg_in`** — seeded callgraph EDGES (forward/reverse cone) kept as a renderable
  graph.
- **`edep(a, b)`** — transitive dependency closure over the **entity graph**
  (`uses`/`creates`/`composes`/`aggregates`/`associates`) — architecture-altitude "who
  depends on whom".

## Query it (the "query language" target, hand-written for now)
Relations and results are keyed by **annotated names**, so you query in names directly —
overloads and template instances stay distinct:
```sql
-- a specific overload's callees
SELECT b FROM calls WHERE a='app::scale(int)';

-- what a function transitively reaches (seed it first with run.sh)
SELECT DISTINCT b FROM reach WHERE a='rd_kafka_produceva';

-- check a specific edge holds (1 = yes)
SELECT count(*) FROM reach WHERE a='rd_kafka_produceva' AND b='__builtin_object_size';

-- all ancestors of a class
SELECT super FROM subtype WHERE sub='chain::D';

-- design-level dependencies of an entity (a template instance stays distinct)
SELECT b FROM edep WHERE a='cont::Wrapper<int>';
```

### Annotated names (overloads & template instances)
Each node is keyed by a readable name built in three layers, so distinct symbols never merge:
1. `qual_name` + the signature/template-arg suffix from libclang's `display_name`.
2. for a member of a template **instance**, the owner's instance args are spliced in.
3. if a name still collides, a readable ` @file:line` tiebreaker (and, only for genuinely
   indistinguishable rows, a final ` [n]`).

| symbol | annotated name |
|--------|----------------|
| overloaded `scale` | `app::scale(int)`, `app::scale(double)`, `app::scale(int, int)` |
| ctor overloads | `Widget()`, `Widget(int)`, `Widget(Widget &&)` |
| template instances | `cont::Wrapper<int>`, `cont::Wrapper<bool>`, `cont::Wrapper<T>` |
| instance methods | `cont::Wrapper<bool>::label()`, `geo::Box<T>::get()` |
| const overload / cross-TU | `geo::Box::get() @box.hpp:46`, `main() @app.c:8` |

No two distinct symbols ever share a node, so reasoning stays sound.

## Add your own reasoning (reuse the prelude)
1. Write a new `.dl` and `#include "cidx_base.dl"` — you get every type/edge/predicate for free.
2. Add your rules + `.output yourrel(IO=sqlite, dbname="index.db")`.
3. Run `souffle yourscript.dl` from `build/` (after `run.sh` has created the views). **No
   re-running `run.sh`** unless the index changed.

```prolog
#include "cidx_base.dl"

// "a BusinessRule is a function whose qualified name lives under namespace `rules`"
// (name-prefix approximation; real Lang-1 will use richer structural predicates)
.decl business_rule(r:Sym)
business_rule(r) :- ... .                 // bind to your project's convention

// "which callers reach a business rule" (seed reach() with the candidate callers)
.decl invokes(caller:Sym, rule:Sym)
invokes(c, r) :- reach(c, r), business_rule(r).
.output invokes(IO=sqlite, dbname="index.db")
```

Edge kinds in the prelude — base: 1 calls, 2 inherits, 6 overrides, 7 uses, 8 field_of,
9 method_of; entity: 1 generalizes, 2 implements, 4 composes, 5 aggregates, 6 associates,
7 creates, 8 uses, 9 destroys. Need another kind? Add one VIEW to `cidx_views.sql` and a
matching `.decl`/`.input` to `cidx_base.dl`.

## Notes
- Reasoning is over integer `symbol.id`s; names resolve by a plain join in the same DB.
- Re-run `run.sh` after a `cidx index`/`resolve` to refresh (the views are live, but `reach`
  needs re-seeding and re-running).
- Validated on **librdkafka** (8,786 symbols / 52,852 edges): views + Soufflé in ~0.15 s,
  `reach` from `rd_kafka_produceva` = 439, `edep` = 589, `subtype` = 26.

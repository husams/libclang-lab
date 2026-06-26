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

## What it computes
- **`reach(a, b)`** — **seeded** transitive call reachability (`calls+`) from the symbols in
  the `seed` table. The full closure over millions of symbols is a space bomb, so it expands
  only from what you ask about. The core of *who calls X* / *impact of changing X*.
- **`subtype(sub, super)`** — transitive class hierarchy (all ancestors), over raw `inherits`
  **and** the design-level `generalizes`/`implements` entity edges.
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
Each node is keyed by `qual_name` + the signature/template-arg suffix from libclang's
`display_name`, so distinct symbols never merge:
| symbol | annotated name |
|--------|----------------|
| overloaded `scale` | `app::scale(int)`, `app::scale(double)`, `app::scale(int, int)` |
| ctor overloads | `Widget()`, `Widget(int)`, `Widget(Widget &&)` |
| template instances | `cont::Wrapper<int>`, `cont::Wrapper<bool>`, `cont::Wrapper<T>` |

Where a name still collides across distinct symbols — const/ref-qualified overloads (not in
`display_name`), methods of different template instances, or same-named functions in
different TUs — a ` #<id>` suffix is appended to the colliding names only (e.g. `main() #132`),
so reasoning stays sound.

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

# cidx-souffle — Soufflé query/reasoning layer over the cidx graph

A **Datalog reasoning layer** over the graph cidx builds. Soufflé
([UPL-1.0](https://github.com/souffle-lang/souffle), permissive) reasons over the cidx
graph with recursive rules. The index can be **multi-GiB / millions of symbols** (e.g. a
unified index over several repos), so this layer **never copies the index** — it reads it
**read-only** and projects just the small integer edge tables into a sidecar Soufflé runs on.

This is the engine tier of the planned two-language design (see
`~/workspace/wiki/pages/planning/cidx-concept-relation-dsl.md`): a future ANTLR **definition
language** lowers to rules like these; a controlled-English **query language** lowers to the
`SELECT`s shown below.

## Files
| File | Role |
|------|------|
| `project.sql`  | reads the index **read-only**, projects ONLY the integer edge tables into `build/graph.db` (no whole-DB copy, no 12M-row symbol load) |
| `cidx_base.dl` | **reusable prelude** — all types, edge relations, and common derived predicates. `#include` it from any reasoning script |
| `cidx.dl`      | example reasoning script (`#include "cidx_base.dl"` + `.output`s) — copy as a template |
| `run.sh`       | project edges read-only → run Soufflé over integer ids → report counts |

## Run
```bash
./run.sh                                 # uses ~/.cache/cidx/index.db (read-only, never mutated)
./run.sh /path/to/index.db               # a specific index
./run.sh /path/to/index.db Shape         # also SEED reach() from symbols matching "Shape"
```
**Why no copy.** Soufflé only needs the edge triples, so `project.sql` copies just those
(integer `symbol.id`s) into a small `build/graph.db`. The 12M-row name table never enters
Soufflé — names are joined back onto the small result rows afterwards, read-only. Reasoning
over integers is faster and smaller than over USR strings.

Result tables land in `build/graph.db` (integer-id): `subtype`, `edep`, `reach`.

## What it computes
- **`reach(a, b)`** — **seeded** transitive call reachability (`calls+`) from the symbols in
  the `seed` table. The full closure over millions of symbols is a space bomb, so it is
  expanded only from what you ask about. The core of *who calls X* / *impact of changing X*.
- **`subtype(sub, super)`** — transitive class hierarchy (all ancestors), over raw `inherits`
  **and** the design-level `generalizes`/`implements` entity edges.
- **`edep(a, b)`** — transitive dependency closure over the **entity graph**
  (`uses`/`creates`/`composes`/`aggregates`/`associates`) — architecture-altitude "who depends
  on whom".

## Query it (the "query language" target, hand-written for now)
```sql
-- read names by joining the (read-only) index onto the small result rows
ATTACH 'file:/path/to/index.db?immutable=1' AS src;

-- what a function transitively reaches (seed it first with run.sh)
SELECT DISTINCT sb.qual_name FROM reach r JOIN src.symbol sb ON sb.id=r.b
WHERE r.a=(SELECT id FROM src.symbol WHERE qual_name='main');

-- all ancestors of a class
SELECT sp.qual_name FROM subtype t JOIN src.symbol sp ON sp.id=t.super
WHERE t.sub=(SELECT id FROM src.symbol WHERE qual_name='chain::D');

-- design-level dependencies of an entity
SELECT sb.qual_name FROM edep e JOIN src.symbol sb ON sb.id=e.b
WHERE e.a=(SELECT id FROM src.symbol WHERE qual_name='app::Dashboard');
```

## Add your own reasoning (reuse the prelude)
1. Write a new `.dl` and `#include "cidx_base.dl"` — you get every type/edge/predicate for free.
2. Add your rules + `.output yourrel(IO=sqlite, dbname="graph.db")`.
3. Run `souffle yourscript.dl` from `build/`. **No re-projection** — reuse `graph.db` until the
   index changes (re-run `run.sh` only after a `cidx index`/`resolve`).

```prolog
#include "cidx_base.dl"

// "a BusinessRule is a function whose qualified name lives under namespace `rules`"
// (name-prefix approximation here; real Lang-1 will use richer structural predicates)
.decl business_rule(r:Sym)
business_rule(r) :- ... .                 // bind to your project's convention

// "which callers reach a business rule" (seed reach() with the candidate callers)
.decl invokes(caller:Sym, rule:Sym)
invokes(c, r) :- reach(c, r), business_rule(r).
.output invokes(IO=sqlite, dbname="graph.db")
```

Edge kinds available in the prelude — base: 1 calls, 2 inherits, 6 overrides, 7 uses,
8 field_of, 9 method_of; entity: 1 generalizes, 2 implements, 4 composes, 5 aggregates,
6 associates, 7 creates, 8 uses, 9 destroys. Need another kind? Add one `CREATE TABLE`
line to `project.sql` and a matching `.decl`/`.input` to `cidx_base.dl`.

## Notes
- Reasoning is over integer `symbol.id`s; names are resolved only on result rows, read-only.
- The canonical index is opened `immutable=1` — never written, safe to run while indexing.
- Re-run `run.sh` after a `cidx index`/`resolve` to refresh the projected edges.

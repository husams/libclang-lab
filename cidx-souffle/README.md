# cidx-souffle — Soufflé query/reasoning layer over the cidx graph (v0)

First version of a **Datalog reasoning layer** over the graph cidx builds. Soufflé
([UPL-1.0](https://github.com/souffle-lang/souffle), permissive) reads the cidx graph
**straight from SQLite**, reasons over it with recursive rules, and writes the derived
relations **back into the same SQLite file** — no export step, cidx and Soufflé share one DB.

This is the engine tier of the planned two-language design (see
`~/workspace/wiki/pages/planning/cidx-concept-relation-dsl.md`): a future ANTLR **definition
language** lowers to rules like these; a controlled-English **query language** lowers to the
`SELECT`s shown below.

## Files
| File | Role |
|------|------|
| `cidx_views.sql` | adapter VIEWs re-keying cidx's integer edges by USR → Soufflé input relations |
| `cidx.dl`        | the Soufflé program: base relations + recursive reasoning + materialized output |
| `run.sh`         | copy index.db → `build/graph.db`, apply views, run Soufflé, report counts |

## Run
```bash
./run.sh                       # uses ~/.cache/cidx/index.db (copied, never mutated)
./run.sh /path/to/index.db     # or a specific index
```
Materialized tables land in `build/graph.db`: `reachable`, `subtype_named`, `entity_depends`.

## What it computes
- **`reachable(src, src_name, dst, dst_name)`** — transitive call reachability (`calls+`). The
  core of *who calls X* and *impact of changing X*.
- **`subtype_named(...)`** — transitive class hierarchy (all ancestors), over raw `inherits`
  **and** the design-level `generalizes`/`implements` entity edges.
- **`entity_depends(...)`** — transitive dependency closure over the **entity graph**
  (`uses`/`creates`/`composes`/`aggregates`/`associates`) — architecture-altitude "who depends
  on whom".

## Query it (this is the "query language" target, hand-written for now)
```sql
-- who transitively calls a function (impact / blast radius)
SELECT DISTINCT src_name FROM reachable WHERE dst_name = 'geo::Shape::Shape';

-- what does a function transitively reach (its dependencies)
SELECT DISTINCT dst_name FROM reachable WHERE src_name = 'main';

-- all ancestors of a class
SELECT super_name FROM subtype_named WHERE sub_name = 'chain::D';

-- design-level dependencies of an entity
SELECT b_name FROM entity_depends WHERE a_name = 'app::Dashboard';
```

## Extending (add your own reasoning)
1. Need a base edge not yet exposed? Add a VIEW in `cidx_views.sql` (edge kinds: 1 calls,
   2 inherits, 3 contains, 4 specializes, 5 instantiates, 6 overrides, 7 uses, 8 field_of,
   9 method_of, 17 friend; entity_edge kinds: 1 generalizes, 2 implements, 3 specializes,
   4 composes, 5 aggregates, 6 associates, 7 creates, 8 uses, 9 destroys, 10 befriends,
   11 instantiates).
2. Add `.decl` + `.input` for it in `cidx.dl`.
3. Write rules; `.output` the named result with `IO=sqlite`.

### Defining a custom concept (the Lang-1 pattern, by hand today)
```prolog
// "a BusinessRule is a function whose name qualifies into namespace `rules`"
// (here approximated by name prefix; real Lang-1 will use richer predicates)
.decl business_rule(r:symbol)
business_rule(r) :- symname(r, n), match("rules::.*", n).

// "which services-ish callers reach a business rule"
.decl invokes(caller:symbol, rule:symbol)
invokes(c, r) :- reach(c, r), business_rule(r).
```

## Limitations (v0)
- Symbols keyed by **USR** (correct, unique) but joins/closure are unindexed beyond what
  Soufflé does in-memory — fine at this corpus size; revisit for very large indexes.
- Closure relations can be large (`reachable` is the full transitive set); for huge graphs,
  prefer demand/seed patterns or filter at the SQL layer.
- No parameterized queries yet — Soufflé materializes the full relation; you filter with SQL.
  The future controlled-English layer generates those `SELECT`s.
- Runs on a **copy** by design; re-run `run.sh` after a `cidx index`/`resolve` to refresh.

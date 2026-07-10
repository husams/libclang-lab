# Souffle examples over the application-wide cidx index

These examples query `index.db`, the resolved graph produced by `cidx index` and
`cidx resolve`. They cover relationships between symbols across every indexed
translation unit and repository component.

They are deliberately separate from
`cidx-cpp/examples/souffle/`, whose input is a single-TU `cidx-astgraph` AST
artifact. Use the AST examples for syntax, local control flow, and expression
structure; use these examples for whole-application calls, references, class
hierarchies, templates, and design-level dependencies.

## Run

```bash
cidx-cpp/examples/souffle-index/run.sh 01_inventory.dl
cidx-cpp/examples/souffle-index/run.sh \
  --seed 'app::main()' 02_callgraph.dl /path/to/index.db
```

When no database path is supplied, the runner uses `~/.cache/index.db`.

The suite is self-contained: `cidx_base.dl` declares the cidx relations and
`cidx_views.sql` maps the native `symbol`, `edge`, `edge_site`, `template_arg`,
and `entity_edge` tables into NULL-free Souffle inputs. The runner applies those
views directly to the selected `index.db`, changes into the database's directory,
and runs Souffle there. It creates no database copy or symbolic link. Examples
01-09 print to stdout.
Symbol-oriented examples 02-08 (except the aggregate inventory) and 10 require
an exact collision-safe symbol name through `--seed`. This bounds evaluation
and output to one symbol, call/hierarchy cone, or entity dependency cone instead
of constructing an all-pairs closure or printing the whole application graph.
The runner stores that selection as a one-row `query_seed` view in the same
database; no external fact file is created. Example 10 intentionally
materializes a new table and additionally
requires `--write`:

```bash
cidx-cpp/examples/souffle-index/run.sh --write \
  --seed 'rules::apply()' 10_writeback.dl /path/to/index.db
```

## Profile and tune

Generate Souffle's JSON profile while running a representative seeded query:

```bash
cidx-cpp/examples/souffle-index/run.sh \
  --seed 'app::main()' \
  --profile /tmp/cidx-callgraph-profile.json \
  --jobs auto \
  02_callgraph.dl
```

`--jobs N` controls evaluation parallelism (`auto` by default). Souffle's
`--compile` mode is intentionally not exposed here: with the SQLite connector it
resolves the relative `dbname="index.db"` from its private compilation directory,
which conflicts with this suite's direct, no-copy/no-symlink database contract.

The seed is the primary large-codebase optimization. Without it, call
reachability or reverse impact may contain O(symbols²) result pairs, and no
worker count can compensate for constructing and printing that result.

## Examples

| File | Whole-application question |
|---|---|
| `01_inventory.dl` | How many symbols of each kind are indexed? |
| `02_callgraph.dl` | From one seeded function, what is transitively reachable and where are calls made? |
| `03_references.dl` | Who calls or uses one seeded symbol, and is it unreferenced? |
| `04_hierarchy.dl` | What is the seeded class ancestry or method override chain? |
| `05_architecture.dl` | What is the seeded entity's transitive design dependency cone? |
| `06_impact.dl` | What is the reverse transitive blast radius of one seeded symbol? |
| `07_metrics.dl` | What are one seeded symbol's fan-in, fan-out, and direct dependency counts? |
| `08_templates.dl` | What does one seeded concrete instance instantiate, and with what arguments? |
| `09_cross_file.dl` | Which indexed files depend semantically on others, aggregated by file pair? |
| `10_writeback.dl` | How can a derived relation be materialized into `index.db`? |

All identity-bearing relations use the collision-safe annotated names produced
by `cidx_views.sql`; overloads and template instances therefore remain distinct.

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
cidx-cpp/examples/souffle-index/run.sh 02_callgraph.dl /path/to/index.db
```

When no database path is supplied, the runner uses `~/.cache/index.db`.

The suite is self-contained: `cidx_base.dl` declares the cidx relations and
`cidx_views.sql` maps the native `symbol`, `edge`, `edge_site`, `template_arg`,
and `entity_edge` tables into NULL-free Souffle inputs. The runner applies those
views directly to the selected `index.db`, changes into the database's directory,
and runs Souffle there. It creates no database copy or symbolic link. Examples
01-09 print to stdout.
Example 10 intentionally materializes new tables and must be requested with
`--write`:

```bash
cidx-cpp/examples/souffle-index/run.sh --write 10_writeback.dl /path/to/index.db
```

## Examples

| File | Whole-application question |
|---|---|
| `01_inventory.dl` | What symbols and symbol kinds are indexed? |
| `02_callgraph.dl` | Who calls whom, what is transitively reachable, and where are calls made? |
| `03_references.dl` | Which symbols use other symbols, and which definitions are unreferenced? |
| `04_hierarchy.dl` | What are the full class ancestry and method override chains? |
| `05_architecture.dl` | Which design entities compose, aggregate, associate with, create, or use others? |
| `06_impact.dl` | What is the reverse transitive blast radius of changing a symbol? |
| `07_metrics.dl` | Which symbols have the highest fan-in/fan-out and which entities have most dependencies? |
| `08_templates.dl` | Which concrete instances instantiate which templates and with what arguments? |
| `09_cross_file.dl` | Which indexed files depend semantically on other files through calls? |
| `10_writeback.dl` | How can a derived relation be materialized into `index.db`? |

All identity-bearing relations use the collision-safe annotated names produced
by `cidx_views.sql`; overloads and template instances therefore remain distinct.

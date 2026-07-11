# Souffle `index.db` experiments — documentation

These chapters explain the runner, shared database adapter, shared Datalog
prelude, and every numbered experiment. Each script chapter explains what the
script does, how the code works, and how to run it.

## Table of contents

### Infrastructure

1. [Runner: `run.sh`](01-runner.md)
2. [Database adapter: `cidx_views.sql`](02-database-views.md)
3. [Base relations: `cidx_base.dl`](03-base-relations.md)

### Experiments

4. [Symbol inventory: `01_inventory.dl`](04-inventory.md)
5. [Seeded call graph: `02_callgraph.dl`](05-callgraph.md)
6. [Incoming references: `03_references.dl`](06-references.md)
7. [Hierarchy and overrides: `04_hierarchy.dl`](07-hierarchy.md)
8. [Architecture dependencies: `05_architecture.dl`](08-architecture.md)
9. [Change impact: `06_impact.dl`](09-impact.md)
10. [Local graph metrics: `07_metrics.dl`](10-metrics.md)
11. [Template instances: `08_templates.dl`](11-templates.md)
12. [Cross-file dependencies: `09_cross_file.dl`](12-cross-file.md)
13. [Materialized impact: `10_writeback.dl`](13-writeback.md)
14. [All call-path edges: `11_all_paths.dl`](14-all-paths.md)

## Common model

The suite queries the resolved application graph in `index.db`; it does not
parse source code. Layer-0 relations describe symbols and mechanical edges such
as `calls`, `uses`, and `inherits`. Layer-1 relations describe materialized
entities and design relationships such as composition, aggregation, creation,
and association.

Most experiments require an exact annotated seed. Seeding bounds recursive
evaluation to one symbol's graph cone and prevents an all-pairs closure from
growing toward O(symbols²). Annotated names retain signatures, template
arguments, and location suffixes where required, so overloads do not collapse.

The runner creates SQLite views directly inside `index.db`. It does not copy
the database or create a symbolic link. Only the write-back experiment stores a
derived result table, and it requires `--write`.

Before running chapter commands that call `sqlite3` directly, resolve the same
database path as the runner:

```bash
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
```

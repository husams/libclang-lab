# Souffle examples over cidx `index.db`

This directory contains application-wide Souffle experiments over the graph
stored by cidx in `index.db`. These examples are separate from `../souffle/`,
which operates on per-translation-unit `cidx-astgraph` dumps.

## Quick start

```bash
# Uses ~/.cache/cidx/index.db.
./run.sh 01_inventory.dl

# Select a real caller from this index, then run its call graph.
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
SEED=$(sqlite3 "$DB" 'SELECT a FROM calls ORDER BY a LIMIT 1;')
./run.sh --seed "$SEED" 02_callgraph.dl
```

Requirements: `souffle` with SQLite support, `sqlite3`, and a resolved database
named `index.db`. The runner reads `~/.cache/cidx/index.db` unless another
`/path/to/index.db` is supplied as the final argument or through `CIDX_DB`.

## Documentation

The documentation is split into chapters under [`docs/`](docs/README.md):

- [`run.sh`](docs/01-runner.md)
- [`cidx_views.sql`](docs/02-database-views.md)
- [`cidx_base.dl`](docs/03-base-relations.md)
- one chapter for every numbered experiment

Start with the [documentation table of contents](docs/README.md).

## Validate every example

```bash
./validate.sh
```

The validator selects exact seeds from the database, runs examples 01-09,
generates a callgraph profile, and runs example 10 only against a disposable
copy. Pass a database explicitly with `./validate.sh /path/to/index.db`.

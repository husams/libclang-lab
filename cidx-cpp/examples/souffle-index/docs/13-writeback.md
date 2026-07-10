# Materialized impact: `10_writeback.dl`

## What the script does

This experiment computes the reverse impact cone of one selected symbol and
materializes it into the `souffle_impact` table in `index.db`. It demonstrates
the "reason once, query cheaply later" pattern.

## Explain the code

The two `souffle_impact` rules mirror `06_impact.dl`: the base rule finds direct
callers of `query_seed`, and the recursive rule walks callers transitively.

The difference is the output directive:

```prolog
.output souffle_impact(IO=sqlite, dbname="index.db")
```

Souffle writes a real table instead of stdout. Because this mutates the
database, `run.sh` requires both `--write` and `--seed`. Re-running replaces the
materialized relation according to Souffle's SQLite output behavior.

## How to run it

Test on a disposable database first:

```bash
mkdir -p /tmp/cidx-writeback
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
cp "$DB" /tmp/cidx-writeback/index.db

sqlite3 /tmp/cidx-writeback/index.db \
  ".read cidx-cpp/examples/souffle-index/cidx_views.sql"
sqlite3 /tmp/cidx-writeback/index.db \
  'SELECT DISTINCT b FROM calls ORDER BY b LIMIT 30;'

SEED=$(sqlite3 /tmp/cidx-writeback/index.db 'SELECT DISTINCT b FROM calls ORDER BY b LIMIT 1;')
./run.sh \
  --write \
  --seed "$SEED" \
  10_writeback.dl /tmp/cidx-writeback/index.db
```

Query the result:

```bash
sqlite3 /tmp/cidx-writeback/index.db \
  'SELECT changed, caller FROM souffle_impact ORDER BY caller LIMIT 50;'
```

Remove the materialized table:

```bash
sqlite3 /tmp/cidx-writeback/index.db \
  'DROP TABLE IF EXISTS souffle_impact;'
```

On a canonical index, use the same command without the final database argument
only after deciding that persistent write-back is intended.

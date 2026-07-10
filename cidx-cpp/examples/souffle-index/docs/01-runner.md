# Runner: `run.sh`

## What the script does

`run.sh` is the supported entry point for every experiment. It validates the
command, selects an `index.db`, installs the adapter views, configures an exact
seed when needed, and launches Souffle from the database directory.

It uses an explicit final database argument first, then `CIDX_DB`, then
`~/.cache/cidx/index.db`. It creates no database copy, fact export, temporary
symlink, or source parse. It refuses `10_writeback.dl` unless the caller
supplies `--write`.

## Explain the code

The option loop accepts:

| Option | Effect |
|---|---|
| `--seed SYMBOL` | exact collision-safe symbol used by bounded experiments |
| `--profile FILE` | writes Souffle's JSON execution profile |
| `--jobs N` | selects a worker count or `auto` |
| `--write` | authorizes the materializing experiment |

After parsing, the script verifies that the rule, database, `souffle`, and
`sqlite3` exist. The database basename must be `index.db` because every Datalog
SQLite input declares `dbname="index.db"`.

The script applies `cidx_views.sql`, then checks whether the rule requires a
seed. A supplied seed is SQL-escaped and installed as a one-row `query_seed`
view. If the exact name is absent, the runner prints substring candidates and
exits instead of silently returning an empty result.

Finally it assembles `-I`, `-j`, and optional `-p` arguments, changes into the
database directory, and starts Souffle. Changing directory lets the SQLite
connector open the real database directly.

## How to run it

```bash
# Global inventory using ~/.cache/cidx/index.db.
./run.sh 01_inventory.dl

# Resolve the database exactly as the runner does.
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}

# Seeded experiment using a real caller from this index.
SEED=$(sqlite3 "$DB" 'SELECT a FROM calls ORDER BY a LIMIT 1;')
./run.sh --seed "$SEED" 02_callgraph.dl

# Another database; it must be named index.db.
./run.sh --seed "$SEED" \
  02_callgraph.dl /work/product/index.db

# Profile a fixed seed using one worker.
/usr/bin/time -p ./run.sh \
  --seed "$SEED" \
  --jobs 1 \
  --profile /tmp/callgraph-profile.json \
  02_callgraph.dl >/tmp/callgraph.out
```

For a worker-count experiment, hold the database, seed, and output destination
fixed:

```bash
for jobs in 1 2 4 auto; do
  /usr/bin/time -p ./run.sh \
    --seed "$SEED" --jobs "$jobs" \
    --profile "/tmp/callgraph-j${jobs}.json" \
    02_callgraph.dl >/dev/null
done
```

More workers can be slower for small cones because SQLite input and scheduling
overhead dominate. Compare profiles rather than assuming `auto` is optimal.

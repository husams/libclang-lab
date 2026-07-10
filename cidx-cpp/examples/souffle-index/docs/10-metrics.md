# Local graph metrics: `07_metrics.dl`

## What the script does

This experiment calculates direct fan-in, fan-out, and design dependency count
for one selected symbol or entity. It avoids the large output and global
aggregation cost of ranking every symbol in the database.

## Explain the code

`fan_in` counts `calls(_, seed)`. `fan_out` counts `calls(seed, _)`.

`direct_entity_dependency` unions five Layer-1 relationships: uses, creates,
composes, aggregates, and associates. `dependency_count` counts distinct direct
targets for the seed.

The count aggregates are seed-bound. A function can legitimately have zero
entity dependencies, and a class can have zero call fan-in/fan-out; the
experiment exposes both graph layers without requiring both to be populated.

## How to run it

For a function:

```bash
./run.sh 01_inventory.dl
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
sqlite3 "$DB" \
  'SELECT name FROM symdisp ORDER BY name LIMIT 30;'

SEED=$(sqlite3 "$DB" 'SELECT name FROM symdisp ORDER BY name LIMIT 1;')
./run.sh --seed "$SEED" 07_metrics.dl
```

For an entity:

```bash
ENTITY_SEED=$(sqlite3 "$DB" 'SELECT a FROM e_uses UNION SELECT a FROM e_creates UNION SELECT a FROM e_composes UNION SELECT a FROM e_aggregates UNION SELECT a FROM e_associates ORDER BY 1 LIMIT 1;')
./run.sh --seed "$ENTITY_SEED" 07_metrics.dl
```

Use a small shell loop to compare a curated set of symbols rather than removing
the seed and printing metrics for the entire application:

```bash
FIRST=$(sqlite3 "$DB" 'SELECT name FROM symdisp ORDER BY name LIMIT 1 OFFSET 0;')
SECOND=$(sqlite3 "$DB" 'SELECT name FROM symdisp ORDER BY name LIMIT 1 OFFSET 1;')
[ -n "$SECOND" ] || SECOND="$FIRST"
for symbol in "$FIRST" "$SECOND"; do
  echo "=== $symbol"
  ./run.sh --seed "$symbol" 07_metrics.dl
done
```

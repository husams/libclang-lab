	# Change impact: `06_impact.dl`

## What the script does

This experiment walks the call graph backward from one changed function or
method. It lists every transitive caller and reports the seed's structural blast
radius.

## Explain the code

The base `impacts` rule joins `query_seed(changed)` to callers with a direct
`calls(caller, changed)` edge. The recursive rule repeatedly finds callers of
already impacted symbols.

`blast_radius` counts the distinct impacted callers. This is structural
reachability, not a claim that each caller will fail or require modification.
The analysis is context-insensitive and inherits the completeness limits of the
stored call graph.

## How to run it

```bash
./run.sh 01_inventory.dl
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
sqlite3 "$DB" \
  'SELECT DISTINCT b FROM calls ORDER BY b LIMIT 30;'

SEED=$(sqlite3 "$DB" 'SELECT DISTINCT b FROM calls ORDER BY b LIMIT 1;')
./run.sh --seed "$SEED" 06_impact.dl
```

Compare two candidate changes:

```bash
FIRST=$(sqlite3 "$DB" 'SELECT DISTINCT b FROM calls ORDER BY b LIMIT 1 OFFSET 0;')
SECOND=$(sqlite3 "$DB" 'SELECT DISTINCT b FROM calls ORDER BY b LIMIT 1 OFFSET 1;')
[ -n "$SECOND" ] || SECOND="$FIRST"
for symbol in "$FIRST" "$SECOND"; do
  echo "=== $symbol"
  ./run.sh --seed "$symbol" 06_impact.dl
done
```

Profile a widely used function with fixed conditions:

```bash
/usr/bin/time -p ./run.sh \
  --seed "$SEED" \
  --jobs 1 \
  --profile /tmp/impact.json \
  06_impact.dl >/tmp/impact.out
```

Runtime is proportional to the reverse call cone, which can vary dramatically
between seeds.

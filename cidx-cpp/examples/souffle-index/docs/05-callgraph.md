# Seeded call graph: `02_callgraph.dl`

## What the script does

This experiment computes the forward call cone of one function or method. It
reports graph edges, the flattened reachable set, recursion of the seed, and
source locations for calls in the cone.

## Explain the code

`query_seed` is loaded from the one-row SQLite view prepared by `run.sh`.

- `call_edge` starts with direct calls from the seed and recursively adds calls
  from every discovered callee. It preserves graph structure.
- `reachable` computes `(seed, target)` reachability pairs.
- `recursive` succeeds when the seed reaches itself.
- `call_location` joins reachable edges to `call_site_fact` for file, line and
  conditional metadata.

The seed appears in the recursive base rules, so evaluation is bounded to its
cone instead of constructing a whole-application all-pairs closure. The result
is a structural, context-insensitive call graph, not an execution trace or CFG.

## How to run it

```bash
./run.sh 01_inventory.dl
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
sqlite3 "$DB" \
  'SELECT DISTINCT a FROM calls ORDER BY a LIMIT 30;'

SEED=$(sqlite3 "$DB" 'SELECT DISTINCT a FROM calls ORDER BY a LIMIT 1;')
./run.sh --seed "$SEED" 02_callgraph.dl
```

For an overload, include the complete annotated signature returned by
`symdisp` or `calls`; do not copy the illustrative names from documentation.

```bash
sqlite3 "$DB" \
  "SELECT name FROM symdisp WHERE name LIKE '%Cache%' ORDER BY name LIMIT 30;"
```

Profile the fixpoint and output:

```bash
/usr/bin/time -p ./run.sh \
  --seed "$SEED" \
  --profile /tmp/callgraph.json \
  --jobs 1 \
  02_callgraph.dl >/tmp/callgraph.out

wc -l /tmp/callgraph.out
```

Run once to a file and once to `/dev/null` to separate reasoning cost from
serialization cost.

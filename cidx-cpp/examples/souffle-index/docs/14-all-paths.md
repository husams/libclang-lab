# All call-path edges: `11_all_paths.dl`

## What the script does

This experiment finds the complete finite call subgraph between one source
function/method and one target function/method. It outputs every node and call
edge that belongs to at least one source-to-target route, the full signatures
of both endpoints, and source locations for those edges.

It does not enumerate each route as an ordered list. A cyclic call graph can
contain infinitely many walks, and enumerating all simple paths can grow
exponentially. The finite path subgraph preserves all routing alternatives and
terminates in the presence of recursion.

## Explain the code

The runner loads the source into `query_seed` and the destination into
`query_target`.

`from_source` computes the forward call closure from the source. `to_target`
computes the reverse closure of everything capable of reaching the target.
Their intersection defines the internal `on_path` node set.

`route_edge(a,b)` keeps a call edge when `a` is reachable from the source and
`b` can reach the target. Therefore every retained edge participates in at
least one source-to-target route. The output relations join `callable_fact` so
`path_node`, `path_edge`, and `path_exists` contain both collision-safe identity
and canonical full signature. `path_location` joins edges to source locations.
`path_exists` is emitted only when the target is forward-reachable.

Example signature values are `int app::exercise_cache()` and
`double geo::Circle::area() const`. The graph still joins by annotated identity,
so adding presentation signatures does not change seed or overload semantics.

For a graph with cycles, an edge in a reachable cycle is included when the
cycle can still reach the target. Each edge appears once because Datalog uses
set semantics.

## How to run it

Initialize the views and select a guaranteed-connected direct pair:

```bash
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
./run.sh 01_inventory.dl

IFS=$'\t' read -r SOURCE TARGET <<EOF
$(sqlite3 -separator $'\t' "$DB" \
  'SELECT a,b FROM calls
   WHERE a IN (SELECT name FROM callable_fact)
     AND b IN (SELECT name FROM callable_fact)
   ORDER BY a,b LIMIT 1;')
EOF

./run.sh \
  --seed "$SOURCE" \
  --target "$TARGET" \
  11_all_paths.dl
```

For application-specific endpoints, discover exact annotated names:

```bash
sqlite3 "$DB" \
  "SELECT name FROM symdisp WHERE name LIKE '%handler%' ORDER BY name LIMIT 30;"
```

Then set `SOURCE` and `TARGET` to two exact returned names and run the same
command shown above. The initial direct-pair example is fully automatic and can
always be copied as-is when the index contains at least one call edge.

An empty `path_exists`, `path_node`, and `path_edge` result means no stored call
route connects the selected endpoints. Profile a large path subgraph with:

```bash
./run.sh \
  --seed "$SOURCE" \
  --target "$TARGET" \
  --profile /tmp/all-paths.json \
  11_all_paths.dl >/tmp/all-paths.out
```

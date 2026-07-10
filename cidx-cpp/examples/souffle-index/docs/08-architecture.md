# Architecture dependencies: `05_architecture.dl`

## What the script does

This experiment computes the design-level dependency cone of one entity. It
answers which entities the seed composes, aggregates, associates with, creates,
uses, or destroys, directly and transitively.

## Explain the code

Six Layer-1 inputs are normalized into
`dependency(source, relation, target)`, retaining a readable relation label.

`depends_transitively` starts at `query_seed` and recursively follows dependency
targets. It produces the flattened reachable set. `dependency_edge` follows the
same cone but preserves each typed hop, making it suitable for graph rendering
or architecture inspection.

These facts were already classified and materialized by `cidx resolve`; the
Souffle rule does not re-derive ownership semantics from raw AST nodes.

## How to run it

```bash
./run.sh 01_inventory.dl
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
sqlite3 "$DB" \
  'SELECT a FROM e_uses UNION SELECT a FROM e_creates UNION SELECT a FROM e_composes UNION SELECT a FROM e_aggregates UNION SELECT a FROM e_associates ORDER BY 1 LIMIT 30;'

SEED=$(sqlite3 "$DB" 'SELECT a FROM e_uses UNION SELECT a FROM e_creates UNION SELECT a FROM e_composes UNION SELECT a FROM e_aggregates UNION SELECT a FROM e_associates ORDER BY 1 LIMIT 1;')
./run.sh --seed "$SEED" 05_architecture.dl
```

Profile entities with different graph shapes:

```bash
FIRST=$(sqlite3 "$DB" 'SELECT a FROM e_uses UNION SELECT a FROM e_creates UNION SELECT a FROM e_composes UNION SELECT a FROM e_aggregates UNION SELECT a FROM e_associates ORDER BY 1 LIMIT 1 OFFSET 0;')
SECOND=$(sqlite3 "$DB" 'SELECT a FROM e_uses UNION SELECT a FROM e_creates UNION SELECT a FROM e_composes UNION SELECT a FROM e_aggregates UNION SELECT a FROM e_associates ORDER BY 1 LIMIT 1 OFFSET 1;')
[ -n "$SECOND" ] || SECOND="$FIRST"
for entity in "$FIRST" "$SECOND"; do
  safe=$(printf '%s' "$entity" | tr '/: ()&' '_')
  /usr/bin/time -p ./run.sh \
    --seed "$entity" \
    --profile "/tmp/architecture-${safe}.json" \
    05_architecture.dl >"/tmp/architecture-${safe}.out"
done
```

Different seeds measure different workloads. Always report the output tuple
count with the runtime.

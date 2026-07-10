# Incoming references: `03_references.dl`

## What the script does

This experiment finds direct callers and semantic users of one selected symbol.
It also reports when the selected symbol is a definition with no incoming
`calls` or `uses` edge.

## Explain the code

`incoming_reference` has two rules. One projects incoming `calls` edges with
kind `"calls"`; the other projects incoming `uses` edges with kind `"uses"`.
Both constrain the destination to `query_seed`.

`unreferenced_definition` joins the seed to `symbol_fact`, requires
`is_definition=1`, and uses stratified negation to check that no incoming
reference exists.

"Unreferenced" is an index-level statement, not proof of runtime deadness.
Reflection, dynamic loading, generated registries, unresolved indirect calls,
or incomplete indexing can hide real runtime use.

## How to run it

```bash
./run.sh 01_inventory.dl
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
sqlite3 "$DB" \
  'SELECT DISTINCT b FROM calls UNION SELECT DISTINCT b FROM uses ORDER BY 1 LIMIT 30;'

SEED=$(sqlite3 "$DB" 'SELECT b FROM calls UNION SELECT b FROM uses ORDER BY 1 LIMIT 1;')
./run.sh --seed "$SEED" 03_references.dl
```

Compare a public entry point and a suspected dead helper:

```bash
FIRST=$(sqlite3 "$DB" 'SELECT b FROM calls UNION SELECT b FROM uses ORDER BY 1 LIMIT 1 OFFSET 0;')
SECOND=$(sqlite3 "$DB" 'SELECT b FROM calls UNION SELECT b FROM uses ORDER BY 1 LIMIT 1 OFFSET 1;')
[ -n "$SECOND" ] || SECOND="$FIRST"
for symbol in "$FIRST" "$SECOND"; do
  echo "=== $symbol"
  ./run.sh --seed "$symbol" 03_references.dl
done
```

The work is bounded to direct indexed edges around one destination symbol.

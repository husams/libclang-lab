# Hierarchy and overrides: `04_hierarchy.dl`

## What the script does

For a class seed, this experiment returns all transitive bases and interfaces.
For a method seed, it returns the transitive chain of methods that it overrides.

## Explain the code

`ancestor` begins at the seed and combines three direct relations:

- raw `inherits` edges;
- entity-level `e_generalizes` edges;
- entity-level `e_implements` edges.

Three recursive rules continue upward through those same relations.
`override_chain` begins with the seed's direct override and recursively follows
each base method's override edge.

One output relation is normally empty: a seed is usually either a class or a
method. Layer-1 ancestry requires a current `cidx resolve` result.

## How to run it

List real derived classes and choose an exact name:

```bash
./run.sh 01_inventory.dl
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
sqlite3 "$DB" \
  'SELECT DISTINCT a FROM inherits UNION SELECT DISTINCT a FROM e_generalizes UNION SELECT DISTINCT a FROM e_implements ORDER BY 1 LIMIT 30;'

CLASS_SEED=$(sqlite3 "$DB" 'SELECT a FROM inherits UNION SELECT a FROM e_generalizes UNION SELECT a FROM e_implements ORDER BY 1 LIMIT 1;')
./run.sh --seed "$CLASS_SEED" 04_hierarchy.dl
```

Method override chain:

```bash
sqlite3 "$DB" \
  'SELECT DISTINCT a FROM overrides ORDER BY a LIMIT 30;'

METHOD_SEED=$(sqlite3 "$DB" 'SELECT DISTINCT a FROM overrides ORDER BY a LIMIT 1;')
./run.sh --seed "$METHOD_SEED" 04_hierarchy.dl
```

If output is unexpectedly empty, search `symdisp` for the exact annotated seed
and inspect the `inherits`, `e_generalizes`, `e_implements`, or `overrides`
views directly.

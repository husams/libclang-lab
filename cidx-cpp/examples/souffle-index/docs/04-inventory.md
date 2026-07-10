# Symbol inventory: `01_inventory.dl`

## What the script does

This experiment counts indexed symbols by cidx/libclang symbol kind. It is a
global health check that produces a small aggregate instead of printing every
symbol in the application.

## Explain the code

The rule includes `cidx_base.dl` and reads `symbol_fact`. The outer
`symbol_fact` atom binds each distinct kind; the `count` aggregate then counts
all rows of that kind. The result relation is
`symbols_by_kind(kind, total)`.

No seed is needed. Work is linear in the number of symbols, while output is
bounded by the number of symbol kinds. Unexpectedly low counts usually indicate
an incomplete or stale index rather than a Datalog error.

## How to run it

```bash
./run.sh 01_inventory.dl
```

Against another index:

```bash
./run.sh 01_inventory.dl /work/product/index.db
```

For a baseline experiment, record database size and graph counts beside the
output:

```bash
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
du -h "$DB"
sqlite3 "$DB" \
  'SELECT count(*) FROM symbol; SELECT count(*) FROM edge;'
./run.sh 01_inventory.dl
```

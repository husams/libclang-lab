# Template instances: `08_templates.dl`

## What the script does

This experiment identifies the primary template instantiated by one concrete
instance and reports its ordered template arguments.

## Explain the code

`selected_instantiation` constrains the `instantiates(instance, primary)` edge
to `query_seed(instance)`.

Four `template_argument` rules decode `template_arg_fact.arg_kind`:

| ID | Label | Value source |
|---:|---|---|
| 1 | `type` | referenced symbol |
| 2 | `value` | stored literal |
| 3 | `template` | referenced template symbol |
| 4 | `pack` | stored pack representation |

`position` is zero-based. Empty output means the exact seed has no corresponding
stored instantiation/argument rows; it does not prove the source uses no
templates elsewhere.

## How to run it

```bash
./run.sh 01_inventory.dl
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}
sqlite3 "$DB" \
  'SELECT DISTINCT a FROM instantiates ORDER BY a LIMIT 30;'

SEED=$(sqlite3 "$DB" 'SELECT DISTINCT a FROM instantiates ORDER BY a LIMIT 1;')
./run.sh --seed "$SEED" 08_templates.dl
```

Find candidate instances first:

```bash
./run.sh 01_inventory.dl
sqlite3 "$DB" \
  "SELECT name FROM symdisp WHERE name LIKE '%<%' ORDER BY name LIMIT 40;"
```

Compare multiple concrete instances:

```bash
FIRST=$(sqlite3 "$DB" 'SELECT DISTINCT a FROM instantiates ORDER BY a LIMIT 1 OFFSET 0;')
SECOND=$(sqlite3 "$DB" 'SELECT DISTINCT a FROM instantiates ORDER BY a LIMIT 1 OFFSET 1;')
[ -n "$SECOND" ] || SECOND="$FIRST"
for instance in "$FIRST" "$SECOND"; do
  ./run.sh --seed "$instance" 08_templates.dl
done
```

The cost is a direct lookup around one template owner.

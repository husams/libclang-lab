# Database adapter: `cidx_views.sql`

## What the script does

`cidx_views.sql` exposes native cidx tables as NULL-free relations that
Souffle's SQLite connector can read. It keeps the graph in `index.db` and adds
only views; it does not copy graph rows.

It also converts numeric symbol IDs into readable, collision-safe annotated
names so rules can distinguish overloads, template instances, and same-named
file-local symbols.

## Explain the code

`symdisp` constructs identity in three layers:

1. qualified name plus signature or template suffix;
2. owner template arguments spliced into member names;
3. `@file:line` and an ordinal when names still collide.

The fact views are `symbol_fact`, `callable_fact`, `template_arg_fact`,
`call_site_fact`, `query_seed`, and `query_target`. Layer-0 edge views map
`edge.kind` values to `calls`, `inherits`,
`overrides`, `instantiates`, `uses`, `field_of`, and `method_of`. Layer-1 views
map `entity_edge.kind` to generalization, implementation, composition,
aggregation, association, creation, usage, and destruction relations.

The `COALESCE` expressions are important: Souffle requires concrete SQLite
values, while the native schema legitimately stores NULL for unavailable
metadata. `query_seed` and `query_target` are empty by default and replaced by
`run.sh` for bounded source-to-target analysis.

`callable_fact(name, signature)` keeps graph identity separate from signature
presentation. It combines `qual_name` with `type_info` to include return type,
parameters, and stored cv/ref/`noexcept` qualifiers. Constructors and
destructors omit the artificial `void` return type reported by libclang.

## How to run it

Normally `run.sh` applies it automatically. To inspect the adapter manually:

```bash
DB=${CIDX_DB:-$HOME/.cache/cidx/index.db}

sqlite3 "$DB" \
  ".read cidx-cpp/examples/souffle-index/cidx_views.sql"

sqlite3 "$DB" \
  "SELECT name FROM symdisp WHERE name LIKE '%Cache%' LIMIT 20;"

sqlite3 "$DB" \
  'SELECT a AS caller, b AS callee FROM calls LIMIT 20;'

sqlite3 "$DB" \
  'SELECT name, signature FROM callable_fact LIMIT 20;'
```

Reapplying the script is safe for these views because it drops and recreates
them. Concurrent seeded runs against the same writable database are unsafe:
they share and replace the `query_seed` and `query_target` views.

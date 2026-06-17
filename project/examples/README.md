# cidx graph-query examples

Runnable, heavily-commented scripts showing how to drive the **read-only**
`GraphQuery` API (`indexer.query`) to inspect a cidx code graph from Python —
instead of reading or grepping source.

These scripts are the Python-library counterpart of the `cidx graph …` CLI
subcommands. Anything the CLI prints, you can compute (and post-process) here.

## Prerequisites

1. A built index at the standard path (`~/.cache/cidx/index.db`, or
   `$INDEXER_CACHE/index.db`). Build one with:
   ```sh
   cidx import <compile_commands.json>
   cidx index          # extracts symbols AND graph edges (omit --no-graph)
   cidx resolve        # rolls up edge counts + cross-repo links
   ```
   The **edges** (calls / uses / inherits / …) only exist if you indexed
   *without* `--no-graph` and ran `resolve`. If `g.edge_count()` is ~0, the
   navigation examples return nothing — regenerate the graph first:
   ```sh
   cidx set pending=True   # flip every file (sources + headers) pending
   cidx index              # re-parse → edges emitted
   cidx resolve
   ```

2. Run from the `project/` directory so `import indexer` resolves, or use uv:
   ```sh
   cd project && python examples/01_basics.py
   #   or, from the repo root:
   uv run --project project python project/examples/01_basics.py
   ```

## The files

| Script | Shows |
|--------|-------|
| `01_basics.py`            | open the index, `stats()`, look symbols up (`find`/`by_name`/`get`), read `Sym` fields |
| `02_references.py`        | `callers` / `callees` / `references`, raw `edges_in/out`, call `sites` (file:line grounding) |
| `03_navigation.py`        | `neighbors` (+ `with_kind=True` for relation types), bounded `walk` (BFS), `reaches`, `path_to` |
| `04_hierarchy_dispatch.py`| class `bases` / `subclasses` / `members` (+ `access=` filter); virtual `overrides` / `dispatch_targets` (C++) |
| `05_json_export.py`       | `.to_dict()` → stable JSON for piping into other tools / languages |
| `06_model_layer.py`       | the high-level model: typed entities (`Function`/`Method`/`Class`/…) with semantic properties instead of graph verbs |
| `07_devirtualization.py`  | two-phase devirtualized callgraph: `Method.dispatch_selection()` selection map + `Callable.devirtualized_callgraph(prune=True)` Γ type-pruning |

## The data model in one paragraph

A **`Sym`** is one declaration/definition (function, class, method, variable,
…) keyed by its clang **USR**. An **`Edge`** is a typed relationship between two
syms — one of **9 kinds**: `calls`, `inherits`, `contains`, `specializes`,
`instantiates`, `overrides`, `uses`, `field_of`, `method_of`. Edges are
*collapsed* (one row per src→dst→kind, with a `count`); each concrete
occurrence is a **`Site`** carrying `file:line:col`. Stub symbols
(`Sym.is_stub` is `True`) are call targets that were referenced but never
indexed (libc, not-yet-indexed repos).

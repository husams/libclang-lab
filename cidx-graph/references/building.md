# Building / refreshing a cidx index

The skill is read-only — it queries an existing `index.db`. If `g.stats()` shows
0 files, or the symbol you need is missing, build or refresh the index with the
`cidx` CLI (separate, explicit step). Default DB: `~/.cache/cidx/index.db`
(override with `$INDEXER_CACHE`).

## First-time index of a repo

```bash
cd /path/to/repo
cidx add-source --path .        # register the repo as a component
cidx import                     # ingest its compile_commands.json (compile args)
cidx index                      # parse files -> symbols + graph edges (ON by default)
cidx resolve                    # roll up edge counts, flag cross-repo edges
```

- Needs a `compile_commands.json` (CMake: `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`;
  or `bear -- make`). Without it, `import` has no flags and headers won't resolve.
- `cidx add-source --no-git --path <dir>` uses the path as-is (skips git-root
  promotion) — handy for a subtree or an external header set.
- `cidx index --no-graph` builds only the symbol table (no edges) — don't use it
  if you need calls/dispatch/navigation.

## Refresh after code changes
```bash
cd /path/to/repo
cidx index        # re-walks changed files (incremental, md5/mtime-gated)
cidx resolve      # re-roll counts
```

## Custom / cross toolchains (work devcontainers, /opt/* compilers)
```bash
export CIDX_LIBCLANG=/opt/llvm-21.1.1/lib/libclang.so   # libclang > pip's 18.x
# the file's compile-command driver (argv[0]) is introspected at parse time so
# a self-contained g++ cross-toolchain's own sysroot/includes are replicated.
```
Parse warnings/diagnostics go to `$INDEXER_CACHE/cidx.log` (logger `cidx.clang`),
not the terminal — check there if symbols/edges look thin.

## Pointing the skill at a specific DB
```python
from cidx_graph import open_graph
g = open_graph("/path/to/repo/.cidx/index.db")   # explicit
```
or set `$INDEXER_CACHE` before `open_graph()`. `python -m cidx_graph` honors
`$CIDX_GRAPH_DB`.

## Sanity after building
```bash
python3 -m cidx_graph stats        # expect files_indexed > 0, edges_by_kind populated
```

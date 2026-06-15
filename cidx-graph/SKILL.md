---
name: cidx-graph
description: >-
  Ground answers about a C/C++ codebase in the prebuilt cidx code graph instead
  of reading or grepping source. Use when asked who calls/uses a symbol, what a
  function calls, class hierarchy, impact of a change, call paths/reachability,
  or which concrete methods a virtual (dynamic-dispatch) call can reach. Drives a
  Python graph API (cidx_graph) that you query with small snippets and execute;
  reserve the LLM for final reasoning over the compact results.
---

# cidx-graph — reason over a code graph, don't read the code

A `cidx` index (`index.db`, SQLite schema v7) is a graph of every symbol in a
C/C++ codebase and the typed edges between them (calls, inherits, contains,
overrides, uses, …). This skill lets you **answer structural questions by
querying that graph**, not by pulling source files into your context.

The graph is large (tens of thousands of edges). The whole point is to keep it
out of your context: you write a short Python snippet against the `cidx_graph`
API, **execute it**, and read back a handful of symbols and `file:line` sites.
You only spend tokens on the final reasoning, not on traversal.

## Operating rules — read before answering

1. **Do not read source files or `grep`/`rg` the codebase to answer a structural
   question** (callers, callees, references, hierarchy, dispatch, impact,
   reachability). Query the graph. Reading source to answer these is the failure
   mode this skill exists to prevent.
2. **Generate code, execute it, reason on the result.** Write a snippet using the
   API, run it with the Bash tool, look at the small printed output. Don't try to
   hold graph state in your head or reconstruct it from memory.
3. **Never dump the graph.** Every traversal is bounded — pass `limit=` and
   `depth=`, return counts and a few representative rows. If a result set is huge,
   summarize it in the snippet (counts, group-by) rather than printing all rows.
4. **Ground every claim in sites.** When you assert "X calls Y", back it with the
   `file:line` from `g.sites(edge)`. The API returns real locations; cite them.
5. **Check the index first.** Run `g.stats()` once. If `files_indexed` is 0, the
   edge counts are tiny, or the symbol isn't found, the index may be missing or
   stale — say so and offer to (re)build it (see References → building an index)
   rather than guessing or silently falling back to grep.
6. **libclang is the last resort.** The graph answers almost everything. Only
   drop to `cidx_graph.live` (libclang) for what the static graph cannot capture:
   template instantiations, exact resolved types, macro-expanded code, or a file
   edited since the last index. See `references/libclang.md`.
7. **Read-only.** This skill never modifies the index. Building/refreshing an
   index is a separate, explicit step you run via the `cidx` CLI.

## Basic usage

The module lives next to this file. Add it to `sys.path`, open the graph (it
auto-discovers `$INDEXER_CACHE/index.db` or `~/.cache/cidx/index.db`), query.

```python
import sys
sys.path.insert(0, "/Users/husam/.claude/skills/cidx-graph")
from cidx_graph import open_graph

g = open_graph()                       # or open_graph("/path/to/index.db")
print(g.stats())                       # sanity: components, files, symbols, edges

fn = g.find("rd_kafka_new")[0]         # fuzzy lookup by qualified name
print("callers:", g.callers(fn))       # who calls it      (incoming `calls`)
print("callees:", g.callees(fn))       # what it calls      (outgoing `calls`)

for e in g.references(fn):             # incoming calls + uses, with grounding
    print(e, g.sites(e)[:3])

cls = g.find("ConfImpl", kind="class")[0]
print("subclasses:", g.subclasses(cls))
print("bases:", g.bases(cls, direct=False))

m = g.find("Handle::name", kind="method")[0]
print("dispatch targets:", g.dispatch_targets(m))   # virtual -> real callees
```

Run it from the shell:

```bash
python3 /tmp/q.py            # or pipe a heredoc
# quick spot-check CLI (needs the skill dir on the path):
PYTHONPATH=/Users/husam/.claude/skills/cidx-graph python3 -m cidx_graph stats
```

### The four API groups (full reference: `references/api.md`)

- **Lookup symbols** — `find(pattern, kind=, limit=)` (fuzzy qualified name),
  `by_name(spelling, kind=)` (exact), `get(id|usr)`, `symbols_in_file(substr)`.
- **Lookup references** — `references(sym)` (who references this),
  `callers(sym)` / `callees(sym)`, `edges_in/edges_out(sym, kinds=)`,
  `sites(edge)` (the `file:line` grounding).
- **Navigation** — `neighbors(sym, kinds=, direction=)`, `walk(start, kinds,
  direction, depth)` (bounded BFS → `Traversal` with `path_to()`),
  `reaches(a, b, kinds)` (shortest path / "does A reach B"), `bases` /
  `subclasses` / `members` (class hierarchy).
- **Discover dynamic dispatch** — `dispatch_targets(method)` (a virtual call's
  real run-time targets: the method + all transitive overriders),
  `overrides` / `overridden_by`, `is_virtual_method`, `virtual_callees(fn)`.

Escape hatch for queries the helpers don't cover: `g.sql("SELECT …")`
(read-only). Schema in `references/schema.md`.

## References — load only when you need them

- `references/api.md` — every method: arguments, return shape, when to use it.
- `references/recipes.md` — task → ready-to-run snippet (impact analysis, call
  tree, dispatch expansion, dead-code candidates, cross-repo edges, …).
- `references/schema.md` — the v7 tables and edge kinds, for custom `g.sql()`.
- `references/libclang.md` — the `live` submodule: when and how to drop to the
  AST, and what the graph can't see.
- `references/building.md` — `cidx` CLI to build/refresh an index when it's
  missing or stale (read this only when `stats()` shows the index is empty/thin).

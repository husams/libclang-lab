# libclang escape hatch (`cidx_graph.live`)

**Use the graph first.** The static graph answers callers/callees/references/
hierarchy/dispatch/reachability/impact without parsing anything. Only reach for
libclang when the question genuinely needs the live AST.

## When the graph is NOT enough — drop to libclang

- **Template instantiations** the indexer doesn't materialize (a known cidx
  limit): the concrete types at a specific instantiation, members of an
  instantiated class template.
- **Exact resolved / canonical types** at a particular cursor (the graph stores
  `type_info` spelling, not the full type machinery).
- **Macro-expanded code** — what a macro actually expands to at a call site.
- **A file edited since the last `cidx index`** — the graph is stale for it;
  parse the current file directly (or, better, re-index).
- **Anything cursor-level** the schema doesn't record (default args, attributes,
  exact token ranges, comments).

If the question is none of these, it's a graph question — go back to the API.

## How to use it

```python
import sys
sys.path.insert(0, "/Users/husam/.claude/skills/cidx-graph")
from cidx_graph import live

# Parses ONE file. Compile args come from the cidx DB's stored compile_options
# when available (most faithful), else the default toolchain args.
tu = live.parse_file("/abs/path/to/foo.cpp")

import clang.cindex as cx
for cur in live.walk(tu.cursor):
    if cur.kind == cx.CursorKind.CALL_EXPR:
        ref = cur.referenced
        print(cur.spelling, "->", ref.spelling if ref else "?",
              "@", cur.location.line)
```

`live.walk(cursor)` is a depth-first iterator over a cursor's subtree.
`live.overridden(cursor)` returns the base cursors a method overrides (live
cross-check for `Graph.overrides`).

## Requirements & knobs

- **libclang must be importable.** The lab uses the pip `libclang` wheel on
  macOS; Linux/custom toolchains set `CIDX_LIBCLANG=/path/to/libclang.so`
  (e.g. `/opt/llvm-21.1.1/lib/libclang.so`). `live` honors it.
- **The cidx indexer's parser is reused** for correct builtin-header / custom-
  toolchain handling. It's found via `CIDX_REPO`, an explicit
  `parse_file(..., indexer_path=...)`, or the default
  `~/workspace/qemu-vms/libclang-lab/project`. If it can't be imported,
  `parse_file` raises with the reason.
- **Scope tightly.** Parsing a TU is orders of magnitude slower than a graph
  query and pulls a whole translation unit into memory. Parse one file, extract
  exactly what you need, summarize, discard.

## Better than parsing: refresh the index

If the graph is stale or incomplete for a whole area, the durable fix is to
re-index, not to parse file-by-file:

```bash
cd <repo>
cidx index            # re-walk changed files, refresh symbols + edges
cidx resolve          # roll up edge counts, mark cross-repo edges
```

Then reopen the graph — every helper is current again.

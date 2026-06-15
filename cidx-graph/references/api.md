# cidx_graph API reference

All access is through a `Graph`, opened with `open_graph()`. Methods return the
compact value types below or lists of them. Everything is read-only and bounded.

```python
from cidx_graph import open_graph, Graph, Sym, Edge, Site, Traversal
g = open_graph(db_path=None)   # None -> $INDEXER_CACHE/index.db or ~/.cache/cidx/index.db
```

## Value types

### `Sym` — a symbol (declaration/definition)
| field | meaning |
|-------|---------|
| `id` | integer primary key (use to address symbols cheaply) |
| `usr` | clang Unified Symbol Resolution (stable cross-TU identity) |
| `spelling` | unqualified name (`set`) |
| `name` | qualified name (`RdKafka::ConfImpl::set`), falls back to spelling |
| `kind` | `class`/`struct`/`function`/`method`/`member`/`enum`/`namespace`/… |
| `type_info` | the cursor's type spelling, if recorded |
| `is_definition` | a body/definition was seen (vs. a bare prototype) |
| `is_pure` | C++ pure-virtual (`= 0`): no own body can ever exist |
| `access` | `public`/`protected`/`private` (C++) |
| `parent_usr` | semantic parent (class/namespace) USR |
| `resolved` | False on minted stubs for never-indexed targets |
| `component`, `file`, `line`, `col` | best-known location (def site, else decl) |
| `.loc` | `"file.cpp:88"` convenience string |
| `.is_stub` | True for a placeholder (spelling empty, unresolved) |

### `Edge` — a typed relationship; `.peer` is the symbol at the other end
`edge_id`, `kind` (name), `src_id`, `dst_id`, `peer: Sym`, `count` (call/use
multiplicity after `cidx resolve`), `base_access`, `is_virtual`.

### `Site` — one concrete occurrence of an edge (the grounding)
`file`, `line`, `col`, `conditional` (inside `#if`/template that may not
compile), `args_sig`, `.loc`.

### `Traversal` — result of `walk()`
`.nodes` (reached `Sym`s, shallowest first), `.path_to(sym|id)` (rebuild the
discovery path), `len()`.

---

## 1. Lookup symbols

| method | returns | notes |
|--------|---------|-------|
| `find(pattern, kind=None, limit=50)` | `list[Sym]` | **fuzzy** on qualified name; `::` segments must appear in order (`find("conf::set")` → `RdKafka::ConfImpl::set`). Shortest names first. Start here. |
| `by_name(spelling, kind=None)` | `list[Sym]` | **exact** spelling; overloads/statics give several rows. |
| `get(id \| usr \| Sym)` | `Sym \| None` | resolve one symbol by id, USR, or pass-through. |
| `symbols_in_file(path_substr, limit=500)` | `list[Sym]` | enumerate a file's symbols without opening it. |

`kind=` accepts any `symbol.kind`: `class struct union function method member
constructor destructor enum enum-constant typedef type-alias class-template
function-template variable namespace macro`.

## 2. Lookup references

| method | returns | meaning |
|--------|---------|---------|
| `references(sym, limit=500)` | `list[Edge]` | **who references `sym`** — incoming `calls` + `uses`. `.peer` is the referrer, `.count` the multiplicity. |
| `callers(sym, limit=500)` | `list[Sym]` | symbols that call `sym` (incoming `calls`). |
| `callees(sym, limit=500)` | `list[Sym]` | symbols `sym` calls (outgoing `calls`). |
| `edges_in(sym, kinds=None, limit=500)` | `list[Edge]` | all incoming edges, optionally filtered by edge-kind names. |
| `edges_out(sym, kinds=None, limit=500)` | `list[Edge]` | all outgoing edges. |
| `sites(edge \| edge_id, limit=200)` | `list[Site]` | exact `file:line:col` of an edge — **use this to ground claims**. |

`kinds=` is a list of edge-kind names: `calls inherits contains specializes
instantiates overrides uses field_of method_of`.

## 3. Navigation

| method | returns | meaning |
|--------|---------|---------|
| `neighbors(sym, kinds=None, direction="out", limit=500)` | `list[Sym]` | one hop; `direction` is `"out"` or `"in"`. |
| `walk(start, kinds, direction="out", depth=3, max_nodes=500)` | `Traversal` | bounded BFS; reconstruct paths with `.path_to()`. |
| `reaches(src, dst, kinds=("calls",), direction="out", max_depth=8)` | `list[Sym] \| None` | shortest edge path src→dst, or None. "Does A ever reach B?" |
| `bases(sym, direct=True)` | `list[Sym]` | base classes (outgoing `inherits`); `direct=False` = whole ancestry. |
| `subclasses(sym, direct=True)` | `list[Sym]` | derived classes (incoming `inherits`); `direct=False` = whole subtree. |
| `members(sym)` | `list[Sym]` | record/namespace children (`contains`/`field_of`/`method_of`). |

## 4. Discover dynamic dispatch

A single `calls` edge to a virtual method understates reality — at run time the
call can land on any override. These resolve that.

| method | returns | meaning |
|--------|---------|---------|
| `dispatch_targets(method)` | `list[Sym]` | **the real run-time target set**: `method` itself (unless pure-virtual) + every method overriding it, transitively down the hierarchy. |
| `overridden_by(method)` | `list[Sym]` | methods that directly override `method` (incoming `overrides`). |
| `overrides(method)` | `list[Sym]` | base methods `method` overrides (outgoing `overrides`). |
| `is_virtual_method(method)` | `bool` | participates in dispatch (pure, overrides, or is overridden). |
| `virtual_callees(fn)` | `list[Sym]` | callees of `fn` that are virtual — the dispatch points inside `fn`. Expand each with `dispatch_targets`. |

## Escape hatch & introspection

| method | returns | meaning |
|--------|---------|---------|
| `sql(query, params=())` | `list[sqlite3.Row]` | arbitrary **read-only** `SELECT`/`WITH` for what the helpers don't cover. Schema in `references/schema.md`. |
| `stats()` | `dict` | components, files_indexed, symbols, stubs, edges, edges_by_kind, resolved_at — check before trusting results. |

## Gotchas

- **Stubs**: targets referenced but never indexed appear as `Sym.is_stub` (empty
  spelling, `resolved=False`). They're real edge endpoints; skip them when
  reporting human-readable names, or note "unindexed target".
- **`count` vs sites**: `Edge.count` is rolled up by `cidx resolve`. If
  `stats()["resolved_at"]` is None, counts may be raw — trust `len(g.sites(e))`.
- **Decl-only symbols** have `file=None` for the def site; the API already falls
  back to the declaration location, but `is_definition=False` tells you no body
  was indexed.
- **C, not C++**: in a C codebase `inherits/overrides/specializes/template_*`
  are empty — dispatch questions don't apply (no virtuals).

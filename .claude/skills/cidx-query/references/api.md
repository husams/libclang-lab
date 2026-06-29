# cidx-query — API reference (the repo's `indexer` query APIs)

These are the indexer's own shipped APIs (`project/indexer/{query,model,
entity_graph}.py`), re-exported by `cidx_query`. Nothing here is reimplemented.

```python
from cidx_query import open_query, open_codebase, open_entity_graph
```

---

## Data types (low level)

### `Sym` — a symbol (declaration/definition)
Fields: `id, usr, spelling, name` (qualified name), `kind, type_info,
is_definition, is_pure, access` (public/protected/private), `parent_usr,
resolved, component, file, line, col, external, is_static, is_instantiation`.
Property `Sym.loc` → `"basename:line"`. `Sym.is_stub` → minted placeholder for a
target never indexed. `Sym.to_dict()` → stable JSON view.

### `Edge` — a typed relationship
Fields: `edge_id, kind` (calls/inherits/contains/uses/overrides/method_of/…),
`src_id, dst_id, peer` (the `Sym` at the other end), `count, base_access,
is_virtual, sites` (eager-loaded `Site`s). `Edge.to_dict()` → peer + edge meta.

### `Site` — a concrete source location (the grounding)
Fields: `file, line, col, conditional, args_sig`, plus receiver-provenance for
virtual dispatch (`recv_*`). Property `Site.loc` → `"basename:line:col"`.

---

## Layer 1 — `GraphQuery` (low-level graph) — `open_query(db_path=None, require_edges=False)`

**Lookup**
- `find(pattern, kind=None, limit=50) -> [Sym]` — qualified-name lookup
  (exact → prefix → fuzzy `::`-segmented infix). The everyday entry point.
- `by_name(spelling, kind=None) -> [Sym]` — exact spelling.
- `by_qual_or_spelling(*names, limit=200) -> [Sym]`.
- `get(ident) -> Sym|None` — by id or USR.
- `records_by_name(...)`, `symbols_in_file(path_substr, limit=500) -> [Sym]`.

**References & edges**
- `references(sym, limit=500) -> [Edge]` — incoming calls + uses, grounded.
- `callers(sym, limit=500, include_instantiations=False) -> [Sym]` (or
  `[CallerWithContext]` when `include_instantiations=True`).
- `callees(sym, limit=500, include_instantiations=False) -> [Sym]`.
- `edges_in(sym, kinds=…)`, `edges_out(sym, kinds=…) -> [Edge]`.
- `sites(edge, limit=200) -> [Site]` — the `file:line` grounding for an edge.

**Navigation**
- `neighbors(sym, kinds=…, direction=…) -> [Sym]`.
- `walk(start, kinds, direction, depth) -> Traversal` — bounded BFS;
  `Traversal.nodes()` and `Traversal.path_to(ident)`.
- `reaches(a, b, kinds) -> [Sym]` — shortest path / "does A reach B".
- `bases(sym, direct=True)`, `subclasses(sym, direct=True)`,
  `members(sym, access=None) -> [Sym]`.

**Templates**
- `template_params(sym)`, `template_args(sym)`, `instantiations(sym)`,
  `template_of(sym)`, `template_of_member(inst_member)`.

**Virtual dispatch**
- `overrides(method)`, `overridden_by(method)`, `is_virtual_method(method)`.
- `dispatch_targets(method) -> [Sym]` — a virtual call's real run-time targets
  (the method + all transitive overriders).
- `virtual_callees(fn)`, `dispatch_selection(...)`, `virtual_call_sites(fn)`.

**Provenance / misc**: `call_args(edge_id)`, `call_args_at(...)`,
`call_sites_into(callee)`, `receiver_provenance(...)`, `stats() -> dict`,
`edge_count()`, `require_edges()`, `close()`. Escape hatch: `g.sql("SELECT …")`
(read-only).

---

## Layer 2 — `CodeBase` (typed OO entities) — `open_codebase(db_path=None, require_edges=False)`

`CodeBase` wraps `GraphQuery` and returns typed `Entity` subclasses (`Function`,
`Method`, `Constructor`, `Destructor`, `Record`/`Class`/`Struct`/`Union`,
`Field`, `Enum`, `EnumConstant`, `Typedef`, `Namespace`, `Variable`, `Macro`,
`FunctionTemplate`, `ClassTemplate`).

**Selectors** (each returns a list of typed entities)
- `find(pattern, kind=None, limit=50)`, `by_name(spelling, kind=None)`,
  `symbols_in_file(path_substr, limit=500)`, `get(ident)`.
- `function(name, …)`, `method(name, …)`, `function_template(name, …)` —
  signature-aware (accept the calling conventions resolved by `_make_signature`).
- `klass(name)`, `struct(name)`, `record(name)`, `interface(name)`,
  `abstract_class(name)`, `class_template(name)`, `instance(name=None, args=…)`.
- `stats() -> dict`, `wrap(sym) -> Entity`, `close()`.

> **Property vs method on this layer:** simple attributes are `@property` (NO
> parens); graph traversals are methods (parens). Get it wrong and you'll hit
> `'bool' object is not callable` / `'method' object is not iterable`.

**Entity** (base) — properties: `.name .spelling .kind .usr .id .is_definition
.is_instantiation .is_stub .location .definition .declaration`; methods:
`.template_of()`, `.references(limit=500)`. `Location` has a `.loc` property.

**Callable** (`Function`/`Method`/…) — properties: `.signature .return_type
.arguments`; methods: `.callers(...)`, `.callees(...)`,
`.callgraph(depth=None, *, fanout=500)` — lazy generator yielding
`(callee, depth)` in call-sequence order,
`.devirtualized_callgraph(depth=None, *, expand_virtual=False, prune=False,
assume_closed_world=False)` — yields `CallStep` carrying dispatch info.

**Method** extras — properties: `.owner .access .is_pure .is_static .is_virtual`;
methods: `.overrides()`, `.overridden_by()`, `.dispatch_targets()`,
`.dispatch_selection(close_subtypes=False)`.

---

## Layer 3 — `EntityGraph` (design-entity / UML-ER graph) — `open_entity_graph(db_path=None, require_edges=False)`

Reads the materialized Layer-1 `entity_edge` graph: one node per design entity
(class/struct/union/enum + template variants), one edge per UML/ER relation.

**EntityGraph methods**
- `entities() -> Iterator[EntityNode]` — every design-entity node.
- `entity(ident) -> EntityNode|None`, `find(pattern, limit=50) -> [EntityNode]`.
- `edges(src=None, dst=None, kind=None) -> Iterator[EntityEdge]` — all edges,
  optionally filtered by endpoint/kind.
- `by_kind(kind) -> Iterator[EntityEdge]`, `kinds() -> [EdgeKind]`,
  `stats() -> dict`, `model` (the underlying `CodeBase`), `close()`.
- **Fluent `EntityQuery`** entry points: `query(*start)`, `klass(name)`,
  `struct(name)`, `record(name)`, `template(name)`, `instance(name)`,
  `abstract_class(name)`, `interface(name)`.

**EntityNode** — properties: `.id .name .display .entity_type .class_kind
.is_abstract .is_interface .spelling .usr .kind .symbol_kind .component
.location .sym`; methods: `.as_model()`, `.out_edges(kind=None)`,
`.in_edges(kind=None)` → `EntityEdge` iterators.

**EntityEdge** — `.src` and `.dst` (`EntityNode`s), `.kind` (`EdgeKind`),
`.multiplicity()`, `.access()`, `.create_form()`, `.via_member()`, `.to_dict()`.
`EdgeKind` has `.verb`, `.inverse_verb`, `.is_structural`.

`EdgeKind` members (use these exact names): `GENERALIZES, IMPLEMENTS,
SPECIALIZES, COMPOSES, AGGREGATES, ASSOCIATES, CREATES, USES, DESTROYS,
BEFRIENDS, INSTANTIATES` (verbs: generalizes/realizes(implements)/…).

**EntityQuery** (fluent, lazy; chain then terminate):
- Traversal steps return a new query: `bases(transitive=False)`,
  `derived(transitive=False)`, `implements()`, `implementors()`, `uses()`,
  `used_by()`, `composes()`, `composed_in()`, `creates()`, `created_by()`,
  `friends()`, `befriended_by()`, `instantiates()`, `instances()`,
  `specializes()`, `specialized_by()`, `aggregates()`, `aggregated_in()`,
  `associates()`, `associated_with()`, `destroys()`, `destroyed_by()`.
- Filters: `where(pred)`, `of_kind(*EntityKind)`, `of_class_kind(*ClassKind)`,
  `interfaces()`, `abstract()`, `concrete()`, `named(substr)`, `exclude(*others)`.
- Terminals: `nodes()`, `names()`, `displays()`, `edges()`, `first()`,
  `count()`, `to_dict()`.

---

## Choosing a layer

- "who calls / what calls / references / a path between two symbols / sites" →
  **GraphQuery**.
- "the typed signature, the call tree, virtual dispatch of a method, class
  members" → **CodeBase**.
- "which class composes/realizes/uses/creates which design entity" →
  **EntityGraph**.

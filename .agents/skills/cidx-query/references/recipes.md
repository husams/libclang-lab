# cidx-query — recipes (task → snippet)

Each snippet assumes:

```python
import sys
sys.path.insert(0, "/Users/husam/workspace/qemu-vms/libclang-lab/.claude/skills/cidx-query")
from cidx_query import open_query, open_codebase, open_entity_graph
```

Always `print(g.stats())` once to confirm the index is present and non-trivial.
Keep output small (counts + a few rows). Ground claims in `.loc`.

---

## Sanity-check the index

```python
g = open_query()
s = g.stats()
print(s["files_indexed"], "files,", s["symbols"], "symbols,", s["edges"], "edges")
assert s["files_indexed"] and s["edges"], "index missing/empty — rebuild with `indexer index`"
```

## Who calls a function? (impact of changing it)

```python
g = open_query()
fn = g.find("shape_area")[0]
for c in g.callers(fn):
    print(c.name, c.loc)
```

## What does a function call? Grounded in sites

```python
g = open_query()
fn = g.find("shapes_total_area")[0]
for e in g.edges_out(fn, kinds=["calls"]):
    print("->", e.peer.name, [s.loc for s in g.sites(e)[:3]])
```

## All references to a symbol (calls + uses)

```python
g = open_query()
sym = g.find("multiply")[0]
for e in g.references(sym):
    print(e.kind, e.peer.name, [s.loc for s in g.sites(e)[:2]])
```

## Call tree from a function (bounded, typed)

```python
cb = open_codebase()
f = cb.function("shapes_total_area")[0]
for callee, depth in f.callgraph(depth=3):
    print("  " * depth, callee.name)
```

## Does A reach B? Shortest path

```python
g = open_query()
a = g.find("main")[0]; b = g.find("shape_area")[0]
path = g.reaches(a, b, kinds=["calls"])
print(" -> ".join(p.name for p in path) if path else "not reachable")
```

## Class hierarchy

```python
g = open_query()
cls = g.find("Shape", kind="class")[0]
print("bases     :", [b.name for b in g.bases(cls, direct=False)])
print("subclasses:", [s.name for s in g.subclasses(cls, direct=False)])
print("members   :", [m.name for m in g.members(cls)])
```

## Virtual dispatch — what can this call actually reach?

```python
cb = open_codebase()
m = cb.method("area")[0]
print("virtual?", m.is_virtual)            # property — no parens
print("overrides   :", [o.name for o in m.overrides()])
print("overridden  :", [o.name for o in m.overridden_by()])
print("dispatch    :", [t.name for t in m.dispatch_targets()])
```

## Devirtualized call graph (Phase-2/3 type pruning)

```python
cb = open_codebase()
f = cb.function("run")[0] if cb.function("run") else cb.find("main")[0]
for step in f.devirtualized_callgraph(depth=2, expand_virtual=True):
    print(step)
```

## Symbols in a file

```python
g = open_query()
for s in g.symbols_in_file("shapes.c"):
    print(s.kind, s.name, s.loc)
```

## Design-entity (UML/ER) relations of a class

```python
eg = open_entity_graph()
node = next(n for n in eg.entities() if n.display == "geo::Circle")
for e in node.out_edges():                     # EntityEdge: node --verb--> dst
    print(node.display, e.kind.verb, e.dst.display)
for e in node.in_edges():                      # who relates TO this entity
    print(e.src.display, e.kind.verb, node.display)
```

## All design-entity edges of one kind (e.g. who implements/realizes)

```python
import indexer                                  # for EdgeKind
eg = open_entity_graph()
for e in eg.by_kind(indexer.EdgeKind.IMPLEMENTS):
    print(e.src.display, "realizes", e.dst.display)
# EdgeKind: GENERALIZES IMPLEMENTS SPECIALIZES COMPOSES AGGREGATES ASSOCIATES
#           CREATES USES DESTROYS BEFRIENDS INSTANTIATES
```

## Fluent entity queries (the EntityQuery builder)

```python
eg = open_entity_graph()
# klass() selects a CONCRETE class by name (qualified, spelling, or display).
print("Circle uses     :", eg.klass("Circle").uses().displays())
# An abstract base (pure-virtual) won't match klass() — grab the node, then walk:
shape = next(n for n in eg.entities() if n.display == "geo::Shape")
print("Shape subclasses:", eg.query(shape).derived(transitive=True).displays())
print("# concrete nodes:", eg.query().concrete().count())
```

> Heads-up: `find("Shape")` ranks by shortest name and may match an unrelated
> same-named symbol (e.g. a C `struct Shape` vs C++ `geo::Shape`). When the exact
> entity matters, filter `eg.entities()` by `.display`, or use the qualified name.

## Escape hatch — custom read-only SQL

```python
g = open_query()
rows = g.sql("SELECT kind, COUNT(*) FROM edge GROUP BY kind ORDER BY 2 DESC")
for k, n in rows:
    print(k, n)
```

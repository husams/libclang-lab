# cidx_graph recipes

Task → snippet. Each is self-contained: prepend the bootstrap, run with
`python3`, read the small output. Adjust names/limits. Print **summaries**, not
whole result sets.

```python
import sys
sys.path.insert(0, "/Users/husam/.claude/skills/cidx-graph")
from cidx_graph import open_graph
g = open_graph()
```

## Resolve an ambiguous name first
Most questions start with picking the right symbol among overloads/matches.

```python
for s in g.find("set", kind="method", limit=20):
    print(s.id, s.name, s.loc)
# then operate on the specific s by id: g.get(<id>)
```

## Who calls this function? (with grounding)
```python
fn = g.find("rd_kafka_new")[0]
for e in g.references(fn):                      # incoming calls + uses
    sites = g.sites(e)
    print(f"{e.peer.name:40} x{e.count}  e.g. {sites[0].loc if sites else '?'}")
```

## What does this function call? (one level / a tree)
```python
fn = g.find("rd_kafka_new")[0]
print("direct:", [c.name for c in g.callees(fn)])

t = g.walk(fn, kinds=("calls",), direction="out", depth=3, max_nodes=200)
print(f"reachable within 3 hops: {len(t)} functions")
for leaf in t.nodes[:20]:
    print("  ", "→".join(s.spelling for s in t.path_to(leaf)))
```

## Impact analysis: what breaks if I change X?
Everything that transitively calls/uses X (reverse reachability).
```python
x = g.find("Storage::add_symbol", kind="method")[0]
t = g.walk(x, kinds=("calls", "uses"), direction="in", depth=4, max_nodes=400)
callers = [s for s in t.nodes if s.id != x.id]
print(f"{len(callers)} symbols depend on {x.name} (≤4 hops)")
# group by file to scope the blast radius
from collections import Counter
print(Counter(s.file and s.file.split('/')[-1] for s in callers).most_common(10))
```

## Does A ever reach B? (security/control-flow reachability)
```python
src = g.find("handle_request")[0]
sink = g.find("system", kind="function")[0]   # or strcpy, exec, etc.
path = g.reaches(src, sink, kinds=("calls",), max_depth=10)
print("REACHABLE:" , [s.spelling for s in path] if path else "no path")
```

## Dynamic dispatch: what can this virtual call actually run?
```python
m = g.find("Reporter::report", kind="method")[0]
print("declared:", m.name, "pure" if m.is_pure else "")
for t in g.dispatch_targets(m):                # method + all overriders
    print("  could run:", t.name, "@", t.loc)
```

## Find the dispatch points inside a function, then expand them
```python
fn = g.find("run_tests")[0]
for v in g.virtual_callees(fn):
    print(v.name, "->", [t.spelling for t in g.dispatch_targets(v)])
```

## Class hierarchy
```python
c = g.find("ConfImpl", kind="class")[0]
print("bases:", [b.name for b in g.bases(c, direct=False)])
print("subclasses:", [s.name for s in g.subclasses(c, direct=False)])
print("members:", [m.spelling for m in g.members(c)][:30])
```

## Enumerate a file's API without opening it
```python
for s in g.symbols_in_file("conf_impl.cpp"):
    if s.access in (None, "public"):
        print(s.kind, s.name, s.loc)
```

## Leaf / likely-dead functions (no callers)
Heuristic — entrypoints, virtuals, and externally-linked symbols also have no
internal callers, so treat as candidates, not proof.
```python
rows = g.sql("""
    SELECT s.id FROM symbol s
    WHERE s.kind IN ('function','method') AND s.is_definition = 1
      AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.dst_id = s.id AND e.kind = 1)
    LIMIT 100
""")
for r in rows:
    s = g.get(r["id"])
    if not g.is_virtual_method(s):
        print("no callers:", s.name, s.loc)
```

## Cross-repo edges (where one component calls into another)
```python
rows = g.sql("""
    SELECT e.src_id, e.dst_id, e.kind FROM edge e
    JOIN symbol a ON a.id=e.src_id JOIN file fa ON fa.id=a.file_id
    JOIN directory da ON da.id=fa.directory_id
    JOIN symbol b ON b.id=e.dst_id JOIN file fb ON fb.id=b.file_id
    JOIN directory db ON db.id=fb.directory_id
    WHERE da.component_id != db.component_id LIMIT 50
""")
for r in rows:
    print(g.get(r["src_id"]).name, "->", g.get(r["dst_id"]).name)
```

## Hottest call edges (most frequent calls)
```python
for e in sorted(g.edges_out(g.find("main")[0], ("calls",)),
                key=lambda e: -e.count)[:10]:
    print(e.count, e.peer.name)
```

## Sanity-check the index before trusting an answer
```python
st = g.stats()
print(st["files_indexed"], "files,", st["symbols"], "symbols,", st["edges"], "edges")
print("by kind:", st["edges_by_kind"], "| resolved:", st["resolved_at"])
if st["files_indexed"] == 0:
    print("INDEX EMPTY — (re)build before answering")
```

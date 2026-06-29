---
name: cidx-query
description: >-
  Explore, query, and reason about this C/C++ codebase using the repo's OWN
  indexer Python query APIs over the prebuilt cidx index (index.db) — not by
  reading or grepping source, and not by reinventing parsing. Use to look up
  symbols/entities, find callers/callees and references, walk the call graph,
  inspect class hierarchy and members, resolve virtual dispatch, or read the
  design-entity (UML/ER) graph. Drives three shipped layers: GraphQuery
  (low-level graph), CodeBase (typed OO entities), and EntityGraph
  (design-entity relations).
---

# cidx-query — reason over the codebase via the indexer's own query APIs

This repo ships a real, tested code-query library in the **`indexer`** package
(`project/indexer/`). It reads a prebuilt SQLite index (`index.db`) of every
symbol in the codebase and the typed edges between them. **Use those APIs** to
answer structural questions — do not reimplement parsing, and do not read/grep
source to answer them.

There are three layers, lowest → highest. Pick the lowest one that answers the
question:

| Opener | Object | Use for |
|---|---|---|
| `open_query()` | `GraphQuery` | low-level symbol graph: find, callers/callees, references, sites, neighbors, walk, reaches, bases/subclasses/members, dispatch_targets, stats |
| `open_codebase()` | `CodeBase` | typed OO view: `Function`/`Method`/`Class`/`Struct`/`Namespace`/… with `.callers()/.callees()/.callgraph()/.devirtualized_callgraph()/.members()/.overrides()` |
| `open_entity_graph()` | `EntityGraph` | design-entity (UML/ER) graph: `ClassNode`/`InterfaceNode`/… and edges (generalizes, realizes, composes, aggregates, associates, creates, uses, …) |

All three read the same `index.db`; `CodeBase` and `EntityGraph` are built *on
top of* `GraphQuery`.

## Operating rules — read before answering

1. **Use the indexer APIs; don't grep/read source** to answer structural
   questions (callers, callees, references, hierarchy, dispatch, reachability,
   entity relations). That is what this index exists for.
2. **Don't reinvent.** These APIs are the supported interface. Don't write your
   own libclang parser or your own SQL graph walk unless the API genuinely can't
   express the question (then use `g.sql("SELECT …")`, read-only).
3. **Generate code, execute it, reason on the result.** Write a short snippet
   against the API, run it with Bash, read the small printed output.
4. **Check the index first.** Run `stats()` once. If `files_indexed` is 0, edge
   counts are tiny, or your symbol isn't found, the index is missing/stale — say
   so and offer to rebuild (`indexer index …`) rather than guessing or grepping.
5. **Ground every claim in a site.** When you assert "X calls Y", cite the
   `file:line` from `Site.loc` (`g.sites(edge)`) or `Sym.loc`.
6. **Keep traversals bounded.** Pass `limit=` / `depth=`; return counts and a few
   representative rows. Never dump the whole graph.
7. **Pick the right layer.** Hierarchy/dispatch/typed-signatures → `CodeBase`.
   Raw edges/paths/grounding → `GraphQuery`. "Which class composes/realizes/uses
   which" → `EntityGraph`.
8. **Read-only.** This skill never writes the index. Building it is a separate
   explicit `indexer` CLI step.

## Setup

Put this skill dir on `sys.path` and import — the bootstrap finds the repo's
`indexer` package automatically (override with `CIDX_PROJECT_DIR` if needed):

```python
import sys
sys.path.insert(0, "/Users/husam/workspace/qemu-vms/libclang-lab/.claude/skills/cidx-query")
from cidx_query import open_query, open_codebase, open_entity_graph
```

(The `indexer` package is also importable directly when the venv's editable
install is active: `sys.path.insert(0, "<repo>/project"); import indexer`. The
bootstrap just removes the path guesswork.)

## Basic usage

```python
from cidx_query import open_query, open_codebase, open_entity_graph

# ---- low-level graph -------------------------------------------------------
g = open_query()                       # reads $INDEXER_CACHE/index.db or ~/.cache/cidx/index.db
print(g.stats())                       # sanity: components/files/symbols/edges

fn = g.find("shape_area")[0]           # fuzzy qualified-name lookup -> [Sym]
print("callers:", g.callers(fn))       # incoming `calls`  -> [Sym]
print("callees:", g.callees(fn))       # outgoing `calls`  -> [Sym]
for e in g.references(fn):             # incoming calls+uses as Edge, with grounding
    print(e.kind, e.peer.name, [s.loc for s in g.sites(e)[:3]])

cls = g.find("Shape", kind="class")    # [] if not a class in this index

# ---- typed OO entities -----------------------------------------------------
cb = open_codebase()
f = cb.function("shapes_total_area")[0]         # -> Function
print(f.signature, f.location.loc)              # signature/location are PROPERTIES
print([(c.name, d) for c, d in f.callgraph(depth=2)])   # callgraph() is a method
ms = cb.method("area")                          # -> [Method]
if ms:
    m = ms[0]
    print("virtual?", m.is_virtual, "overrides:", [o.name for o in m.overrides()])
    print("dispatch targets:", [t.name for t in m.dispatch_targets()])

# ---- design-entity (UML/ER) graph -----------------------------------------
eg = open_entity_graph()
for e in eg.edges():                            # every EntityEdge: src --verb--> dst
    print(e.src.display, e.kind.verb, e.dst.display)
# fluent: klass() matches a CONCRETE class by name (use record()/abstract_class()
# /interface() for those). For an abstract base, grab the node then traverse:
print("Circle uses:", eg.klass("Circle").uses().displays())
shape = next(n for n in eg.entities() if n.display == "geo::Shape")
print("Shape subclasses:", eg.query(shape).derived(transitive=True).displays())
```

Run a snippet from the shell:

```bash
python3 /tmp/q.py
# or quick stats without a file:
python3 -c "import sys; sys.path.insert(0,'/Users/husam/workspace/qemu-vms/libclang-lab/.claude/skills/cidx-query'); from cidx_query import open_query; print(open_query().stats())"
```

## Relationship to the `cidx-graph` skill

`cidx-graph` (user-global) wraps a *standalone copy* of the low-level graph
reader. **`cidx-query` (this skill) uses the repo's `indexer` package directly**
and covers all three layers — including the typed `CodeBase` entity model and the
`EntityGraph` design-entity graph that `cidx-graph` does not. Prefer this skill
when working inside this repo.

## References — load only when you need them

- `references/api.md` — every method of `GraphQuery`, `CodeBase`, and
  `EntityGraph`: arguments, return shape, when to use it; plus the `Sym`/`Edge`/
  `Site` field reference.
- `references/recipes.md` — task → ready-to-run snippet (impact analysis, call
  tree, dispatch expansion, hierarchy, entity relations, cross-file refs).

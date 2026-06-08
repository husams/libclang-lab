	# Part 5 — Building Real Tools

[← Part 4 — Preprocessor, Diagnostics & Flags](part_4_preprocessor_diagnostics.md) | [Part 6 — Advanced & Production →](part_6_advanced_production.md)

## What You'll Learn

- A symbol extractor that emits deterministic JSON (`top_level`, `displayname`, `get_usr`)
- Find-all-references across translation units by matching USRs
- A naming-convention linter built from cursor kind + `semantic_parent` predicates
- Call-graph extraction from `is_definition` + `CALL_EXPR.referenced` (and the edges it misses)
- Code metrics (LOC, nesting depth, branch count) — and why they're AST approximations, not a CFG
- The determinism discipline (sort, `loc()`, main-file filter) that makes tool output reproducible

This is the capstone track. Parts 1–4 taught the API in isolation; here each
section ships one small, complete, runnable tool. Nothing new about libclang is
introduced — instead you wire the primitives (walk, cursor kinds, USR, extent,
`referenced`) into programs that produce something a real codebase tool would
emit: a symbol index, a cross-file reference finder, a linter, a call graph, and
a metrics report.

Two disciplines carry through every tool, because real tools are run on machines
you don't control and against SDKs that drift:

- **Determinism.** Sort every collection before printing, print locations via
  `loc()` (`basename:line:col`, never absolute paths), and filter to the main
  file so system/libc headers never leak into output.
- **Reuse.** Every script imports from `_helpers` — `parse`, `clang_args`,
  `walk`, `loc`, `in_main_file`, `top_level`, `fatal_diagnostics`. No script
  re-implements a parse or a walk.

| Section | Tool | Sample | Key API |
|---|---|---|---|
| 5.1 | Symbol extractor → JSON | `shapes.c` | `top_level`, `displayname`, `get_usr` |
| 5.2 | Find references by USR | `project/` | `referenced.get_usr()` across TUs |
| 5.3 | Naming linter | `messy.c` | `kind`, `semantic_parent`, `spelling` |
| 5.4 | Call-graph extraction | `calls.c` | `is_definition`, `CALL_EXPR.referenced` |
| 5.5 | Code metrics | `messy.c` | `extent`, nesting/branch counting |

---

## 5.1 Symbol extractor → JSON

### Why

The first thing most code-intelligence tools do is build a **symbol table**:
for every top-level declaration, record its name, what kind of thing it is,
where it lives, its signature, and a stable identity. Emitting that as JSON makes
it consumable by anything downstream (an editor, a diff tool, a doc generator).
This section is the smallest honest version of that: walk the main file's
top-level declarations and serialize them.

### What to Do

`top_level(tu)` yields only the TU's direct children that originate in the file
we parsed — so `#include`d declarations (everything in `shapes.h`) are excluded
automatically. For each function/struct/enum/typedef we capture:

- `displayname` — the signature form (`shape_area(const Shape *)`), richer than
  `spelling` (which is just `shape_area`).
- `get_usr()` — the **Unified Symbol Resolution** string, a stable content-based
  identity used in 5.2.

We sort by `(kind, name)` so the JSON is byte-identical on every run.

```python
for c in top_level(tu):
    kind = KINDS.get(c.kind)
    if kind is None or not c.spelling:
        continue
    symbols.append({
        "name": c.spelling, "kind": kind, "location": loc(c),
        "signature": c.displayname, "usr": c.get_usr(),
    })
symbols.sort(key=lambda s: (s["kind"], s["name"]))
```

> **Heads-up — why no structs/enums/typedefs appear below.** The tool *handles*
> all four kinds, but `shapes.c`'s `struct`, `enum`, and `typedef` declarations
> live in `shapes.h`, which is an `#include`. Main-file filtering (`top_level`,
> which uses `in_main_file`) deliberately excludes them — otherwise output would
> depend on every header transitively pulled in and stop being deterministic.
> This is the main-file-filtering gotcha; its full explanation lives at
> [Part 2 §2.4](part_2_navigating_ast.md). Point the tool at `shapes.h` (or
> a `.c` that *defines* the types) and the struct/enum/typedef rows appear.

### Verify

```bash
python3 libclang-lab/scripts/p5_symbols.py
```

### Expected

```
[
  {
    "name": "average",
    "kind": "function",
    "location": "shapes.c:39:8",
    "signature": "average(int, ...)",
    "usr": "c:@F@average"
  },
  {
    "name": "circle_area",
    "kind": "function",
    "location": "shapes.c:8:15",
    "signature": "circle_area(double)",
    "usr": "c:shapes.c@F@circle_area"
  },
  {
    "name": "shape_area",
    "kind": "function",
    "location": "shapes.c:12:8",
    "signature": "shape_area(const Shape *)",
    "usr": "c:@F@shape_area"
  },
  {
    "name": "shape_translate",
    "kind": "function",
    "location": "shapes.c:25:6",
    "signature": "shape_translate(Shape *, double, double)",
    "usr": "c:@F@shape_translate"
  },
  {
    "name": "shapes_total_area",
    "kind": "function",
    "location": "shapes.c:30:8",
    "signature": "shapes_total_area(const Shape *, size_t)",
    "usr": "c:@F@shapes_total_area"
  }
]
```

Notice the USR difference: `circle_area` is `static` (file-local), so its USR is
namespaced to the file — `c:shapes.c@F@circle_area` — while the externally-linked
functions get `c:@F@<name>`. That file scoping is exactly what makes USRs safe to
match across translation units in the next section.

---

## 5.2 Find all references via USR

### Why

"Find all references" is the killer feature USRs exist for. A name like
`multiply` can be declared once and called from many files; grep can't tell a
real reference from a comment or a same-named local. libclang can: every
reference cursor has a `.referenced` decl, and that decl's `get_usr()` is the
**same string in every translation unit**. So you index each TU independently,
then match references by USR — no shared parser state required. That is the
cross-TU power grep never had.

```
mathlib.c  ──parse──▶  TU₁ ──┐
                             ├──▶ refs where referenced.get_usr() == USR(multiply)
app.c      ──parse──▶  TU₂ ──┘        │
                                      ▼
                        app.c:6:20 , mathlib.c:12:12
```

### What to Do

`manifests/project/` is a tiny two-file project (`mathlib.c` defines `multiply`,
`app.c` calls it through `mathlib.h`). We parse both, find the USR of the
`multiply` declaration, then scan every TU for cursors whose `referenced` resolves
to that USR.

```python
usr = next((u for u in (find_target_usr(t) for t in tus) if u), None)
sites = sorted({site for tu in tus for site in refs_in(tu, usr)})
```

We scan both `CALL_EXPR` and `DECL_REF_EXPR` for generality, but a `CALL_EXPR`
already *contains* a `DECL_REF_EXPR` to its callee **at the same location** — so
keying the final set on location alone collapses each physical call to one site.
The `extra_includes=[PROJECT]` argument to `clang_args` lets each `.c` resolve
its `#include "mathlib.h"`.

### Verify

```bash
python3 libclang-lab/scripts/p5_find_refs.py
```

### Expected

```
target: multiply  usr: c:@F@multiply
call sites: 2
  app.c:6:20
  mathlib.c:12:12
```

Two call sites in two different files, matched by one USR — `mathlib.c:12`
(`square` calling `multiply` within the same TU) and `app.c:6` (`main` calling it
across the TU boundary). The USR `c:@F@multiply` is identical in both, which is
the whole point.

---

## 5.3 Naming-convention linter

### Why

A linter is a walk plus a set of predicates. Once you can identify the *kind* of
a declaration and read its `spelling`, you can enforce house style — and produce
the `loc: RULE: message` output every linter the world over emits. `messy.c` is
seeded with three violations to catch.

### What to Do

Walk the main file and apply three rules, each keyed on cursor kind:

| Rule | Applies to | Flags |
|---|---|---|
| `NAME_FUNC` | `FUNCTION_DECL` | name not `^[a-z][a-z0-9_]*$` |
| `NAME_GLOBAL` | `VAR_DECL` whose `semantic_parent` is the TU | global not snake_case |
| `PARAM_SHORT` | `PARM_DECL` | single-letter parameter name |

The `semantic_parent.kind == TRANSLATION_UNIT` check is what separates a
file-scope **global** from a local variable or a parameter — only globals are
direct children of the TU. Findings are sorted before printing.

```python
elif (c.kind == cx.CursorKind.VAR_DECL
      and c.semantic_parent.kind == cx.CursorKind.TRANSLATION_UNIT
      and not SNAKE.match(c.spelling)):
    findings.append((loc(c), "NAME_GLOBAL", ...))
```

### Verify

```bash
python3 libclang-lab/scripts/p5_linter.py
```

### Expected

```
messy.c:3:5: NAME_GLOBAL: global 'GlobalCounter' is not snake_case
messy.c:5:28: PARAM_SHORT: parameter 'A' is a single letter
messy.c:5:35: PARAM_SHORT: parameter 'B' is a single letter
messy.c:5:5: NAME_FUNC: function 'BadlyNamedFunction' is not snake_case
--- 4 issue(s) found
```

All three intended targets fire — the PascalCase global `GlobalCounter`, the
PascalCase function `BadlyNamedFunction`, and its single-letter params `A`/`B`.
The well-behaved `ok_function`, `value`, and `Result` (a local, not a global) are
correctly silent. (The sort is on the `loc` *string*, so `5:5` lands after
`5:28`/`5:35` lexicographically — deterministic, just not numeric.)

---

## 5.4 Call-graph extraction

### Why

A call graph — who calls whom — is the backbone of dead-code analysis, impact
analysis, and recursion detection. Building one is two nested ideas you already
have: iterate function **definitions** (only definitions have a body to scan),
and within each, collect `CALL_EXPR` cursors and read `.referenced.spelling` to
name the callee.

### What to Do

```python
for c in tu.cursor.get_children():
    if c.kind == cx.CursorKind.FUNCTION_DECL and c.is_definition() and in_main_file(c):
        graph[c.spelling] = callees(c)   # callees = sorted unique referenced names
```

`is_definition()` filters out forward declarations (which have no body). For each
definition we walk its subtree, and for every `CALL_EXPR` with a resolvable
`.referenced`, record the callee's `spelling`. We print a sorted
`caller -> [callees]` adjacency list.

### Verify

```bash
python3 libclang-lab/scripts/p5_callgraph.py
```

### Expected

```
compute -> ['mid', 'recurse']
leaf_a -> []
leaf_b -> []
main -> ['compute', 'printf']
mid -> ['leaf_a', 'leaf_b']
recurse -> ['recurse']
```

Two details worth seeing: `recurse -> ['recurse']` — the self-edge of a recursive
function is captured naturally, because the recursive call is just another
`CALL_EXPR` resolving back to the same decl. And `main -> [..., 'printf']` — calls
into `<stdio.h>` show up too, because `.referenced` resolves through the include
even though `printf`'s *definition* is outside the main file. The leaves
(`leaf_a`, `leaf_b`) call nothing and get empty lists.

### Edges this misses (and why)

This is an **AST approximation**, not a sound call graph. It records *syntactic*
calls only:

- Calls through function pointers (`fp()`) have no static callee decl → missed.
- A `CALL_EXPR` whose `.referenced` is `None` (unresolved) is skipped.
- It does not distinguish reachable from unreachable code.

For learning and for many practical tools that's fine; for a sound graph you'd
need a real control-flow/points-to analysis on top.

---

## 5.5 Code metrics

### Why

LOC, nesting depth, and branch count are the cheapest signals for "which function
needs attention." All three fall out of cursors you already understand: `extent`
gives line span, walking the subtree gives the rest. The point of this section is
																																										as much about the *caveat* as the numbers — these are AST approximations, not a
control-flow graph.

### What to Do

Per function definition in the main file:

| Metric | How |
|---|---|
| **LOC** | `extent.end.line - extent.start.line + 1` |
| **max nest** | deepest chain of `IF_STMT`/`FOR_STMT`/`WHILE_STMT` (function body = level 0) |
| **branch** | count of `IF`/`FOR`/`WHILE`/`DO`/`CASE`/`CONDITIONAL_OPERATOR` |

Nesting is computed by a recursive descent that increments only when it enters a
control statement:

```python
def nesting_depth(cursor, level=0):
    here = level + 1 if cursor.kind in NESTERS else level
    deepest = here
    for child in cursor.get_children():
        deepest = max(deepest, nesting_depth(child, here))
    return deepest
```

**We count control statements, not braces.** A `COMPOUND_STMT` (`{ ... }`) is the
body of an `if`/`for`/`while`, so counting it *as well* would double every braced
level — `messy.c`'s 4-deep `if` nest would report 8. The source even annotates
the deepest line with `/* nesting depth 4 */`; counting only the control
statements reproduces that 4.

### Verify

```bash
python3 libclang-lab/scripts/p5_metrics.py
```

### Expected

```
function                loc  nest  branch
BadlyNamedFunction       15     4       4
ok_function               3     0       0
```

`BadlyNamedFunction` spans 15 lines, nests 4 `if`s deep (matching the source
marker), and has 4 decision points (the four `if`s — the `else` is part of an
`if`, not a separate branch). `ok_function` is flat: 3 lines, no nesting, no
branches.

### These are approximations, not a CFG

- **branch** counts *syntactic* decision nodes; it is not cyclomatic complexity
  (which would also count `&&`/`||` short-circuits and `goto`).
- **nest** is structural depth, blind to whether a deep branch is reachable.
- **LOC** is a raw line span — blank lines and comments inside the body count.

A faithful complexity tool builds a control-flow graph; libclang gives you a fast,
good-enough proxy from the AST alone.

---

## Checkpoint

| Concept | What You Proved |
|---|---|
| Symbol extraction | Walked `top_level` decls and serialized name/kind/loc/signature/USR to deterministic JSON |
| USR identity | One symbol's USR is stable across TUs, enabling cross-file find-references |
| Reference finding | Matched `referenced.get_usr()` to locate every call site of `multiply` in two files |
| Linting | Combined cursor kind + `semantic_parent` + `spelling` predicates into `loc: RULE: msg` output |
| Call graphs | Built a `caller -> [callees]` adjacency from `is_definition` + `CALL_EXPR.referenced`, including the recursion self-edge |
| Code metrics | Derived LOC/nesting/branch from `extent` and subtree walks — and learned why they're AST approximations, not a CFG |
| Determinism | Every tool sorts, basenames via `loc()`, and filters to the main file so output is reproducible across machines |

---

[← Part 4 — Preprocessor, Diagnostics & Flags](part_4_preprocessor_diagnostics.md) | [Part 6 — Advanced & Production →](part_6_advanced_production.md)

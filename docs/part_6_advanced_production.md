# Part 6 — Advanced & Production

[← Part 5 — Building Real Tools](part_5_building_tools.md) | [Lab Index →](README.md)

## What You'll Learn

- Unsaved (in-memory) files and `reparse` — the basis of every interactive feature
- Serialized ASTs: `save()` / `from_ast_file()` (PCH-style) to skip reparsing
- Code completion via `codeComplete` and TypedText chunks
- Parsing at scale — one `Index` per process, extract data not cursors, multiprocessing
- The limits of libclang (templates/implicit nodes) and when to drop to LibTooling
- CAPSTONE: a USR-keyed cross-TU semantic indexer (symbol table + xref map)

Parts 1–5 parsed files on disk and read the AST. Part 6 covers the APIs you reach
for when libclang stops being a one-shot parser and becomes the engine inside a
real tool: in-memory editing (unsaved files, reparse), caching parsed state
(serialized ASTs), interactive queries (code completion), running over a whole
codebase (multiprocessing), and knowing when libclang is the wrong tool. The
capstone wires it together into a tiny cross-file semantic indexer — the same
shape as the `cpp-mcp` / `cpp-indexer` pattern, in ~50 lines of `clang.cindex`.

Every API here is verified working on this machine's libclang 18.1.1 (see
`scripts/_smoke_test.py`).

| Section | Concept | Script |
|---------|---------|--------|
| 6.1 | Unsaved files (in-memory buffers) | `p6_unsaved.py` |
| 6.2 | reparse (in-place TU update) | `p6_reparse.py` |
| 6.3 | Serialized ASTs (PCH-style save/reload) | `p6_pch.py` |
| 6.3b | PCH as a prefix (precompile header, reuse on include) | `p6_pch_header.py` |
| 6.4 | Code completion | `p6_complete.py` |
| 6.5 | Parsing at scale (multiprocessing) | `p6_scale.py` |
| 6.6 | Limits of libclang | `p6_limits.py` |
| 6.7 | CAPSTONE: mini semantic indexer | `p6_index.py` |

---

## 6.1 Unsaved files — parse a buffer that never touches disk

### Why

An editor or language server holds the file you are editing in a memory buffer.
By the time you have typed three characters, the on-disk copy is already stale.
Re-writing the buffer to a temp file before every parse would be slow and racy.
libclang instead accepts **unsaved files**: a list of `(name, source)` pairs. The
`name` is what you ask it to parse; any reference to that name (including
`#include`s of it) reads from `source` rather than the filesystem. This is the
foundation every interactive feature in this Part builds on.

### What to Do

Pass `unsaved_files=[(name, source)]` to `parse()`. The helper forwards it
straight to `Index.parse`. The `name` need not exist on disk at all.

```python
name = "virtual.c"
source = "int answer(void) { return 42; }\n..."
tu = parse(name, args=["-std=c11"], unsaved_files=[(name, source)])
```

Then filter to `top_level(tu)` and list the functions, exactly as in Part 2.

### Verify

```
python3 libclang-lab/scripts/p6_unsaved.py
```

### Expected

```
parsed in-memory buffer: virtual.c (123 bytes, never on disk)
functions found:
  answer
  half
  helper
```

The three functions came entirely from the in-memory string; no `virtual.c`
exists on disk.

---

## 6.2 reparse — update a TU in place after an edit

### Why

When the buffer changes, you *could* call `parse()` again and build a brand-new
TranslationUnit. But that throws away all the front-end work (preamble,
`#include` resolution). `tu.reparse()` mutates the **same** TU object in place,
reusing a cached **preamble** so only the changed region is re-analyzed. For an
editor firing on every keystroke, this is the difference between usable and not.

### What to Do

Parse once with `options=TranslationUnit.PARSE_PRECOMPILED_PREAMBLE` so a
preamble gets cached, then call `tu.reparse(unsaved_files=...)` with the new
buffer. The cursor tree reflects the edit afterward.

```python
tu = parse(name, args=["-std=c11"], unsaved_files=[(name, v1)],
           options=cx.TranslationUnit.PARSE_PRECOMPILED_PREAMBLE)
# ... user appends a function ...
tu.reparse(unsaved_files=[(name, v2)])   # same tu object, now updated
```

Flow:

```
parse(v1) → tu ──reparse(v2)──▶ same tu, tree now includes the new fn
            └─ cached preamble reused, not rebuilt
```

### Verify

```
python3 libclang-lab/scripts/p6_reparse.py
```

### Expected

```
before edit: ['answer']
after edit:  ['answer', 'doubled']
cursor tree changed; new function(s): ['doubled']
```

Same `tu` object before and after; `reparse` picked up the appended `doubled`.

---

## 6.3 Serialized ASTs — save once, reload without reparsing

### Why

Parsing C++ with real system headers is expensive (the libc++ headers alone are
thousands of nodes). If many tool invocations need the same TU, parse it **once**
and serialize the result. `tu.save(path)` writes a binary AST — the identical
format `clang -emit-ast` produces, and the same mechanism behind precompiled
headers (PCH). A later process reloads it with `TranslationUnit.from_ast_file()`
and skips the front end entirely: no source file, no compiler flags, no headers.

### What to Do

```python
tu = parse(src, args=clang_args())
tu.save(ast_path)                                   # binary AST on disk
index = cx.Index.create()
reloaded = cx.TranslationUnit.from_ast_file(ast_path, index)  # no reparse
```

> The exact byte size of the saved AST drifts across libclang/SDK versions, so
> the script prints a qualitative `> 100 KB` check rather than a raw count — keep
> volatile numbers out of deterministic output.

### Verify

```
python3 libclang-lab/scripts/p6_pch.py
```

### Expected

```
saved binary AST: True   (> 100 KB: True)
reloaded WITHOUT reparsing: True
main-file functions match after reload: True
functions:
  average
  circle_area
  shape_area
  shape_translate
  shapes_total_area
```

The reloaded TU's main-file functions match the original parse exactly — the AST
round-tripped through disk with no source present at reload time.

---

## 6.3b PCH as a prefix — precompile a header, reuse it on `#include`

### Why

§6.3 froze a whole `.c` TU and reloaded it *standalone*. The classic
precompiled-header (PCH) use case is different and more powerful: precompile a
**header alone**, then have that AST loaded as a prefix for *other* files that
need it — so the header is parsed **once** and never reparsed, no matter how many
TUs include it. This is exactly how a build system's PCH or an editor's preamble
avoids re-chewing the same `<vector>`/`<windows.h>` on every file.

The mechanism is compiler-args, not `from_ast_file()`: build the PCH with
`tu.save()`, then pass `-include-pch <file>` to later parses.

### What to Do

```python
# 1) Precompile the HEADER ALONE. -x c-header is what makes it a reusable PCH.
hdr_tu = index.parse("shapes.h", args=clang_args() + ["-x", "c-header"],
                     options=cx.TranslationUnit.PARSE_INCOMPLETE)
hdr_tu.save("shapes.pch")

# 2) A file that uses `Shape` WITHOUT #including it — resolved from the PCH:
tu = index.parse("probe.c", args=clang_args() + ["-include-pch", "shapes.pch"],
                 unsaved_files=[("probe.c", "double bbox(const Shape *s){...}")])

# 3) A file that DOES #include "shapes.h": the include guard (already defined by
#    the prepended PCH) makes the on-disk re-include a no-op — loaded, not reparsed.
tu = index.parse("shapes.c", args=clang_args() + ["-include-pch", "shapes.pch"])
```

Three gotchas, all of which this script exercises:

| Gotcha | Consequence |
|--------|-------------|
| Omit `-x c-header` | clang treats the `.h` as ordinary source; no reusable PCH. |
| Different `clang_args()` on consume | libclang **rejects** the PCH as incompatible — sysroot/`-std` must match. |
| No include guard in the header | the on-disk `#include` would reparse it anyway, defeating the PCH. |

### Verify

```
python3 libclang-lab/scripts/p6_pch_header.py
```

### Expected

```
header precompiled alone (-x c-header): True
PCH saved on disk: True   (> 10 KB: True)
probe.c uses Shape with NO #include, 0 fatals: True
  Shape resolved from PCH -> arg type: const Shape *
shapes.c (#includes the header) parses with PCH, 0 fatals: True
shapes.c functions:
  average
  circle_area
  shape_area
  shape_translate
  shapes_total_area
```

The probe buffer never `#include`s anything yet resolves `const Shape *` — proof
the type came from the prepended PCH. And `shapes.c`, which *does* include the
header, parses cleanly with the header loaded from the PCH rather than reparsed.

---

## 6.4 Code completion — what can follow `s->`?

### Why

Completion is the canonical interactive query: given a cursor position in a
buffer, "what symbols are valid here?". libclang answers it semantically (it
knows `s` is a `const Shape *`, so it offers the struct's members), which is why
editors prefer it to text-based guessing. It runs against an unsaved buffer, so
it works mid-edit before anything is saved.

### What to Do

Call `tu.codeComplete(file, line, col, unsaved_files=...)`. Each result is a
`CompletionString` made of typed **chunks**; the one you would actually type is
the `TypedText` chunk (`chunk.isKindTypedText()`) — the others are the result
type, punctuation, etc. Place the cursor right after `s->` (line 3, column 8 of
the probe buffer) to complete `Shape` members.

```python
results = tu.codeComplete(name, 3, 8, unsaved_files=[(name, source)])
for r in results.results:
    for chunk in r.string:
        if chunk.isKindTypedText():
            members.append(chunk.spelling)
            break
```

### Verify

```
python3 libclang-lab/scripts/p6_complete.py
```

### Expected

```
completing 's->' at probe.c:3:8
candidate members:
  dimensions
  kind
  name
  origin
```

These are exactly the four fields of `struct Shape` (`shapes.h`) — resolved
semantically through the pointer's pointee type, then sorted for determinism.

---

## 6.5 Parsing at scale — multiprocessing, and the cursor-lifetime trap

### Why

libclang parsing is CPU-bound and single-threaded per TU, and a Python `Index`
is not something you parallelize *within* a process. To index a codebase you fan
out across **processes**, one TU per worker. Two rules make this safe, and one of
them is the most common libclang bug in production tools:

| Rule | Why |
|------|-----|
| One `Index` per process | The `Index` and its TUs are not shareable across process boundaries; each worker creates its own. |
| Extract **data**, not cursors | A `Cursor` is only valid while its `TranslationUnit` is alive. Stash `(name, "file:line:col")` strings, never the cursor — return values get pickled across the process boundary, and a cursor pointing at a dead TU is a crash or garbage. |
| Compute `clang_args()` once | It shells out to `xcrun`/`clang`; do it in the parent and pass the resolved list down. |

On macOS + Python 3.14 the pool start method is **spawn**, so the worker
function must be a top-level (importable) function, and its return value must be
picklable — `(str, str)` tuples are; cursors are not.

> This section's filter-to-the-main-file discipline (and why C++ TUs pull in
> thousands of libc++ nodes) has its home in
> [Part 2 §2.4 — declarations vs definitions & main-file filtering](part_2_navigating_ast.md).

### What to Do

```python
def extract_functions(job):          # top-level: spawn re-imports the module
    path, args = job
    index = cx.Index.create()        # this worker's own Index
    tu = index.parse(str(path), args=list(args))
    out = []
    for c, _ in walk(tu.cursor):
        if c.kind == cx.CursorKind.FUNCTION_DECL and in_main_file(c) and c.is_definition():
            out.append((c.spelling, loc(c)))   # DATA, extracted inside TU scope
    return out                       # picklable; no cursors escape

with mp.Pool(processes=2) as pool:   # under __main__ guard
    per_file = pool.map(extract_functions, jobs)
merged = sorted({item for sub in per_file for item in sub})
```

Flow:

```
parent: clang_args() once ─┬─▶ worker A: own Index → parse mathlib.c → [(name,loc)]
                           └─▶ worker B: own Index → parse app.c    → [(name,loc)]
                                          merge + sort in parent ─────▶ result
```

### Verify

```
python3 libclang-lab/scripts/p6_scale.py
```

### Expected

```
parsed 2 files across 2 worker processes
functions (merged, sorted):
  add        mathlib.c:3:5
  main       app.c:4:5
  multiply   mathlib.c:7:5
  square     mathlib.c:11:5
```

Each worker parsed one file in its own process and returned plain tuples; the
parent merged and sorted them. No cursor ever crossed the process boundary.

---

## 6.6 Limits of libclang — what the stable C API does not expose

### Why

libclang is a deliberately **stable subset** of Clang's C++ AST. Choosing it
means accepting that some things are invisible. Knowing the boundary up front
saves you from chasing nodes that are not there:

- **Implicit nodes** (compiler-generated constructors, conversions) and many
  **template instantiations** are only partially modeled.
- There is **no full Sema / typing** access — you cannot ask arbitrary
  type-system questions the way a Clang plugin can.
- The API is frozen for ABI stability, so newer Clang AST detail lags or never
  surfaces in libclang.

The illustration: `geometry.cpp` calls the function template `max_of<double>`.
libclang resolves the call to a synthesized `FUNCTION_DECL` (the instantiation),
**not** the `FUNCTION_TEMPLATE` it came from — and the template/instantiation
relationship itself is not fully exposed.

> Why a whole-TU walk of `geometry.cpp` is dominated by libc++ template noise,
> and how main-file filtering tames it, is covered in
> [Part 2 §2.4](part_2_navigating_ast.md). Here we only read stable facts.

### When to drop to LibTooling / AST Matchers

| | libclang (Python) | LibTooling / AST Matchers (C++) |
|---|---|---|
| Language | Python (or any C-FFI host) | C++ only |
| Build | `pip install`, no LLVM build | recompiled against a specific LLVM |
| AST view | stable subset; no implicit-node / full template detail | full Clang AST, Sema, type system |
| Use when | indexing, navigation, refactor hints, MCP servers | precise template/implicit analysis, custom diagnostics, codegen |

Stay in libclang for indexing and navigation tools (this lab's whole premise).
Reach for LibTooling only when you genuinely need the full AST or Sema.

### Verify

```
python3 libclang-lab/scripts/p6_limits.py
```

### Expected

```
main-file template decls in geometry.cpp: []
call 'max_of' at geometry.cpp:22:16:
  referenced kind : FUNCTION_DECL  (NOT FUNCTION_TEMPLATE)
  is_definition   : True
  exposed children: ['PARM_DECL', 'PARM_DECL', 'COMPOUND_STMT']
takeaway: implicit/instantiated nodes are a partial view; for full
template-aware analysis drop to LibTooling / AST Matchers (C++).
```

The template `max_of` is defined in `geometry.hpp`, so the main-file view of
`geometry.cpp` shows no template decls. At the call site, `.referenced` is a
plain `FUNCTION_DECL` instantiation with a body but no template parameters — the
generic origin is not reachable from here.

---

## 6.7 CAPSTONE: a mini semantic indexer

### Why

This ties the whole lab together. A semantic indexer answers "where is X
defined?" and "who uses X?" **across** translation units — the core of any
code-navigation tool. The trick is the **USR** (Unified Symbol Resolution): a
string libclang assigns to each entity that is *stable across TUs*, so
`multiply` defined in `mathlib.c` and called from `app.c` share one key. We build
two USR-keyed maps and answer queries against them:

```
symbol table : usr → {name, kind, defined_at}
xref map      : usr → {use locations}
```

This is a tiny `clang.cindex` echo of the `cpp-mcp` / `cpp-indexer` pattern:
parse each TU, harvest definitions and call sites, merge on USR, query.

### What to Do

Index both project TUs (`mathlib.c`, `app.c` — flags come from their
`compile_commands.json` directory via `clang_args(extra_includes=[proj])`).
Record every `FUNCTION_DECL` that `is_definition()` into the symbol table keyed
by `get_usr()`, and every `CALL_EXPR` whose `.referenced` USR into the xref map.
Then answer the two queries by USR.

```python
if c.kind == cx.CursorKind.FUNCTION_DECL and c.is_definition():
    symbols[c.get_usr()] = {"name": c.spelling, "kind": ..., "defined_at": loc(c)}
elif c.kind == cx.CursorKind.CALL_EXPR and c.referenced:
    xrefs.setdefault(c.referenced.get_usr(), set()).add(loc(c))
```

Because the key is the USR, a call in `app.c` and a call in `mathlib.c` resolve
to the **same** `multiply` even though they live in different files.

### Verify

```
python3 libclang-lab/scripts/p6_index.py
```

### Expected

```
symbol table (sorted by name):
  add        FUNCTION_DECL  mathlib.c:3:5
  main       FUNCTION_DECL  app.c:4:5
  multiply   FUNCTION_DECL  mathlib.c:7:5
  square     FUNCTION_DECL  mathlib.c:11:5

query 1: where is 'multiply' defined?
  mathlib.c:7:5

query 2: who calls 'multiply'?
  app.c:6:20
  mathlib.c:12:12

query 2b: who calls 'square'?
  app.c:5:13
```

`multiply` is defined once (`mathlib.c:7:5`) yet called from **both** TUs —
`app.c:6:20` and `mathlib.c:12:12` — because the USR ties the cross-file call
sites to the single definition. That cross-TU join is exactly what a real
indexer does.

---

## Checkpoint

| Concept | What You Proved |
|---------|-----------------|
| Unsaved files | Parsed a `(name, source)` buffer with no file on disk; got its functions |
| reparse | Updated the **same** TU in place after a buffer edit; tree gained `doubled` |
| Serialized ASTs | `save()` → `from_ast_file()` round-tripped a TU through disk; functions matched, no reparse |
| PCH as a prefix | Precompiled `shapes.h` alone (`-x c-header`); reused via `-include-pch` so a probe with no `#include` still resolved `Shape` |
| Code completion | `codeComplete` at `s->` returned the four `Shape` members via TypedText chunks |
| Parsing at scale | Fanned out over a `Pool`, each worker its own `Index`, returning **data** not cursors |
| Limits of libclang | A template call resolves to a `FUNCTION_DECL` instantiation, not the `FUNCTION_TEMPLATE`; stable C API is a subset |
| Mini indexer (capstone) | Built USR-keyed symbol + xref maps across two TUs; answered "defined where / called by whom" |

---

[← Part 5 — Building Real Tools](part_5_building_tools.md) | [Lab Index →](README.md)

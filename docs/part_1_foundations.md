# Part 1 — Foundations — the Clang/libclang stack and your first parse

[← Lab Index](README.md) | [Part 2 — Navigating the AST →](part_2_navigating_ast.md)

## What You'll Learn

- Where libclang sits in the Clang/LLVM stack, and how it differs from LibTooling and the in-tree C++ AST library
- What the Python `clang.cindex` bindings are and how to load libclang
- `Index`, `TranslationUnit`, and the `Cursor` — the three objects everything else builds on
- How to walk an AST

## The Big Picture

Clang is a compiler frontend. When it parses a source file it builds an **Abstract Syntax Tree (AST)** — a fully typed, semantically-resolved tree of every declaration, statement, and expression in the file (plus everything it `#include`s). That AST is Clang's internal C++ data structure, and it changes shape with every LLVM release.

**libclang** is a *stable C API* layered over a deliberately chosen **subset** of that AST. Instead of exposing the thousands of internal C++ `Decl`/`Stmt`/`Expr` classes, it exposes a handful of uniform handle types — **cursors**, **types**, and **tokens** — that stay source-compatible across LLVM versions. You trade some detail for an API that does not break when you upgrade Clang.

**`clang.cindex`** is the Python package that ships with this lab's `libclang` wheel. It is a thin `ctypes` binding: every Python call funnels into the libclang C functions inside the bundled `libclang.dylib`.

```
C/C++ source
    │
    ▼
[ Clang parse ]           <- frontend: preprocess, parse, type-check
    │
    ▼
Translation Unit          <- one compiled file (source + all #includes)
    │
    ▼
Cursor tree               <- the AST as libclang exposes it
    ├──► Types            (what each node's type is)
    └──► Tokens           (the raw lexical spelling)
```

Three layers, three audiences:

| Layer | What it is | You use it when |
|-------|-----------|-----------------|
| Clang C++ AST library | The real in-tree `Decl`/`Stmt`/`Expr` classes | You compile a C++ tool *into* Clang and need full fidelity |
| LibTooling / AST Matchers | A C++ framework over that AST | You write a refactoring/lint tool in C++, recompiled per LLVM |
| **libclang + clang.cindex** | Stable C API + Python bindings | **You script source analysis in Python (this lab)** |

---

## 1.1 What libclang is

**Why.** Before pointing Python at anything, understand *which* layer of Clang you are talking to. People reach for "the Clang AST" and end up confused because there are three different things with that name, each with different stability, language coverage, and effort-to-bind.

**What to Do.** Internalize the trade-off. libclang is the *stable, scriptable, language-agnostic* layer. It is not the most detailed one — for some deep C++ template introspection you eventually hit its limits and must drop to LibTooling. For this lab (C and a slice of C++) libclang is exactly right.

| Property | libclang (clang.cindex) | LibTooling / AST Matchers | raw `clang` CLI |
|----------|-------------------------|---------------------------|-----------------|
| API stability | **Stable C ABI** across LLVM versions | Unstable C++ — recompile per LLVM | N/A (text in, text out) |
| Detail exposed | Curated subset (cursors/types/tokens) | **Full** C++ AST | None programmatically |
| Languages | C, C++, Objective-C | C, C++, Objective-C | all Clang languages |
| Binding ease | **Trivial** — Python `ctypes` wheel | Must write & compile C++ | Shell out, parse text |
| Best for | Scripted analysis, IDE-style tooling | Heavy C++ refactoring engines | Compiling code |

The one-line takeaway: **libclang buys you stability and Python at the cost of some AST detail.**

**Verify.**

```
python3 -c "import clang.cindex; print('OK')"
```

(Run from the repo root `/Users/husam/workspace/qemu-vms`. This only proves the package imports and the native dylib loads — §1.2 is where we actually parse something.)

**Expected.**

```
OK
```

---

## 1.2 Pointing Python at libclang

> **Gotcha home:** This section is the canonical home of the *missing Clang builtin headers* gotcha. Other parts link here rather than re-explaining it.

**Why.** `import clang.cindex` is pure Python; it does nothing until it can load the native `libclang.dylib`. And even once loaded, a *naive* parse silently produces a **truncated AST** because the wheel ships the library but not Clang's builtin headers. Both failures are quiet — you get a result object, not an exception — so you must know how to detect them.

**What to Do.**

**Loading the library.** The pip `libclang` wheel bundles `libclang.dylib` next to the Python package, so `clang.cindex` finds it automatically — nothing extra to configure here. If you instead want a *specific* libclang (say a Homebrew LLVM), set it **once, before creating any `Index`**:

```python
import clang.cindex as cx
cx.Config.set_library_file("/opt/homebrew/opt/llvm/lib/libclang.dylib")
# or, to point at a directory:
# cx.Config.set_library_path("/opt/homebrew/opt/llvm/lib")
```

Confirm the load the *robust* way: call `cx.Index.create()`. If the dylib is bound it returns an index; if not, it raises on import or here. Don't print the dylib's path — it is machine-specific and won't reproduce.

**The headline gotcha — a bare parse silently truncates the AST.** The wheel does **not** ship Clang's builtin headers (`stddef.h`, `stdarg.h`, …). `shapes.h` does `#include <stddef.h>` (for `size_t`). So a parse with `args=[]` emits a **fatal** diagnostic — `'stddef.h' file not found` — and then keeps going with a damaged AST. The result object looks normal; the damage is buried.

The danger is that the **surface looks fine**. Compare broken vs fixed:

```
source        broken (args=[])              fixed (args=clang_args())
──────────────────────────────────────────────────────────────────────
fatal diags   1  ('stddef.h' not found)     0
func list     5 identical names ✔           5 identical names ✔   <- looks the same!
total body    shapes_total_area: 11 nodes   shapes_total_area: 37 nodes
```

The five top-level function names are **identical** in both parses — which is exactly why this bug is dangerous: a quick "did my functions show up?" check passes. The corruption hides *inside* `shapes_total_area`, whose signature is `(const Shape *, size_t count)`. With `size_t` undefined the body fails to type-check and its subtree collapses (37 → 11 nodes). `shape_area`, which uses no `size_t`, is unaffected — so the truncation is *partial* and easy to miss.

> Node counts are illustrative and can drift across libclang versions. The **version-stable** signal is `fatal_diagnostics(tu)`: non-empty after a parse means the AST may be truncated. **Always check it.**

**What `clang_args()` adds, and why.** The helper returns flags that let libclang resolve both system and builtin headers:

| Flag | Supplies | Fixes |
|------|----------|-------|
| `-std=c11` | The language dialect | Correct parsing rules |
| `-isysroot <SDK>` | The macOS SDK (`<stdio.h>`, `<math.h>`, …) | System headers |
| `-I <clang resource-dir>/include` | Clang's **builtin** headers (`stddef.h`, `stdarg.h`) | **The gotcha** |
| `-I <manifests>` | The lab's own headers (`shapes.h`) | Project headers |

`clang_args()` discovers the SDK via `xcrun --show-sdk-path` and the resource dir via `clang -print-resource-dir`, so it degrades gracefully on Linux (where those are usually already on the default search path).

**Verify.**

```
python3 libclang-lab/scripts/p1_load_and_args.py
```

**Expected.**

```
libclang loaded: OK
clang_args() flags: ['-I', '-isysroot', '-std=c11']
========================================================
BROKEN parse (args=[])
  fatal diagnostics: 1
   - 'stddef.h' file not found
  main-file functions: ['average', 'circle_area', 'shape_area', 'shape_translate', 'shapes_total_area']
  shapes_total_area subtree nodes: 11
FIXED parse (args=clang_args())
  fatal diagnostics: 0
  main-file functions: ['average', 'circle_area', 'shape_area', 'shape_translate', 'shapes_total_area']
  shapes_total_area subtree nodes: 37
```

(The flag *values* are machine-specific absolute paths, so the script prints only the flag *names*. The rule for the rest of the lab: **pass `clang_args()` to every parse, and check `fatal_diagnostics()`.**)

---

## 1.3 Index & TranslationUnit

**Why.** Every parse goes through two objects. An **`Index`** is a parsing *session* — a container that owns one or more translation units (and lets them share state, e.g. for cross-file work later). A **`TranslationUnit` (TU)** is the result of compiling *one* file: the main source plus everything it `#include`s, fully preprocessed and type-checked. The TU is the root from which the entire AST hangs.

**What to Do.**

```python
index = cx.Index.create()                    # a parsing session
tu = index.parse(str(src), args=clang_args())  # compile one file -> a TU
```

`index.parse()` runs the Clang frontend (preprocess → parse → semantic analysis) but stops before codegen — no `.o` file is produced. Key TU surface:

| Attribute | Meaning |
|-----------|---------|
| `tu.spelling` | Absolute path of the file the TU represents (basename it before printing) |
| `tu.cursor` | The root cursor — the whole TU as an AST node (see §1.4) |
| `tu.diagnostics` | Warnings/errors from the parse (`fatal_diagnostics()` filters to severe ones) |

The lab's `parse()` helper wraps exactly these two calls, so day-to-day you write `parse(path, args=clang_args())`.

**Verify.**

```
python3 libclang-lab/scripts/p1_translation_unit.py
```

**Expected.**

```
index: Index
tu type: TranslationUnit
tu.spelling (basename): shapes.c
fatal diagnostics: 0
```

---

## 1.4 The Cursor

**Why.** A **cursor** is the single handle type you use to inspect *any* AST node — a function, a parameter, a struct, a `return` statement, an expression. libclang's design choice is that everything in the tree is the same Python type (`Cursor`); you distinguish nodes by their `.kind`. Think of a cursor as a **typed pointer into the AST**: it tells you *what* the node is, *what it's called*, and *where* it lives.

**What to Do.** The root of every TU is `tu.cursor`, whose kind is `TRANSLATION_UNIT`. The three attributes you reach for constantly:

| Attribute | Returns | Example |
|-----------|---------|---------|
| `cursor.kind` | A `CursorKind` enum — *what* the node is | `FUNCTION_DECL` |
| `cursor.spelling` | The node's name (may be empty) | `shape_area` |
| `cursor.location` | A `SourceLocation` — *where* it is | `shapes.c:12:8` |

Note the root cursor has **no source file of its own**, so `loc(root)` reports `<builtin>`.

To enumerate a cursor's immediate children, use `get_children()`. Here we use the `top_level(tu)` helper, which yields the TU's direct children *that originate in the main file* — filtering out the hundreds of declarations pulled in by `#include <math.h>`, `<stddef.h>`, and friends. (That declaration/definition + main-file filtering story is owned by [Part 2 §2.4](part_2_navigating_ast.md).)

**Verify.**

```
python3 libclang-lab/scripts/p1_first_cursor.py
```

**Expected.**

```
root.kind: CursorKind.TRANSLATION_UNIT
root.location: <builtin>
========================================================
KIND                   SPELLING               LOCATION
FUNCTION_DECL          circle_area            shapes.c:8:15
FUNCTION_DECL          shape_area             shapes.c:12:8
FUNCTION_DECL          shape_translate        shapes.c:25:6
FUNCTION_DECL          shapes_total_area      shapes.c:30:8
FUNCTION_DECL          average                shapes.c:39:8
```

(All five are `FUNCTION_DECL`s defined in `shapes.c`. The typedefs/structs/enums live in `shapes.h`, so they are *not* main-file top-level here — they would appear if we parsed the header directly.)

---

## 1.5 Walking the tree

**Why.** Almost every analysis is "visit every node and check a condition." `get_children()` gives you one level; to reach the whole tree you recurse. The lab's `walk()` helper does this for you and tracks depth, so you can both visit and *render* the structure.

**What to Do.** `walk(cursor)` yields `(cursor, depth)` pairs in **pre-order** (parent before children), starting at the cursor you pass:

```python
def walk(cursor, depth=0):
    yield cursor, depth
    for child in cursor.get_children():
        yield from walk(child, depth + 1)
```

Two discipline points the script demonstrates:

- **Do not sort a tree dump.** `get_children()` already yields children in **source order**, which is deterministic. The indentation *is* the structure — sorting would destroy it. (Sorting is for flat *name lists*, like the function list in §1.2 / §1.4.)
- **Filter to the main file.** A raw walk of the whole TU descends into every `#include`d header (thousands of nodes). We keep only `in_main_file(cursor)` nodes, and cap the depth so the shape stays readable — the deeper statement/expression nodes are Part 2's subject.

**Verify.**

```
python3 libclang-lab/scripts/p1_walk.py
```

**Expected.**

```
KIND (indented by depth)           SPELLING @ LOCATION
FUNCTION_DECL                      circle_area @ shapes.c:8:15
  PARM_DECL                        radius @ shapes.c:8:34
  COMPOUND_STMT                    - @ shapes.c:8:42
FUNCTION_DECL                      shape_area @ shapes.c:12:8
  PARM_DECL                        s @ shapes.c:12:32
  COMPOUND_STMT                    - @ shapes.c:12:35
FUNCTION_DECL                      shape_translate @ shapes.c:25:6
  PARM_DECL                        s @ shapes.c:25:29
  PARM_DECL                        dx @ shapes.c:25:39
  PARM_DECL                        dy @ shapes.c:25:50
  COMPOUND_STMT                    - @ shapes.c:25:54
FUNCTION_DECL                      shapes_total_area @ shapes.c:30:8
  PARM_DECL                        shapes @ shapes.c:30:39
  PARM_DECL                        count @ shapes.c:30:54
  COMPOUND_STMT                    - @ shapes.c:30:61
FUNCTION_DECL                      average @ shapes.c:39:8
  PARM_DECL                        n @ shapes.c:39:20
  COMPOUND_STMT                    - @ shapes.c:39:28
```

Each `FUNCTION_DECL` has its parameters (`PARM_DECL`) and a body (`COMPOUND_STMT`, no name → `-`) as children — the recursive structure of the AST in miniature. Descending *into* each `COMPOUND_STMT` is exactly what Part 2 does.

---

## Checkpoint

| Concept | What You Proved |
|---------|-----------------|
| libclang's place in the stack | It is a stable C API over a subset of the Clang AST; `clang.cindex` is the Python `ctypes` binding (§1.1) |
| Loading the library | `import clang.cindex` + `Index.create()` binds the bundled `libclang.dylib`; `Config.set_library_file/path` overrides it (§1.2) |
| The builtin-headers gotcha | A bare `args=[]` parse emits a fatal `'stddef.h' file not found` and silently truncates the AST; `clang_args()` + `fatal_diagnostics()` is the fix-and-detect pair (§1.2) |
| Index & TranslationUnit | `Index.create()` → `index.parse()` → a TU = one compiled file; `tu.spelling`, `tu.cursor`, `tu.diagnostics` (§1.3) |
| The Cursor | `tu.cursor` is the `TRANSLATION_UNIT` root; every node is a `Cursor` with `.kind` / `.spelling` / `.location` (§1.4) |
| Walking the tree | `get_children()` + recursive `walk()` yields the AST pre-order in source order; filter to the main file, never sort a tree (§1.5) |

---

[← Lab Index](README.md) | [Part 2 — Navigating the AST →](part_2_navigating_ast.md)

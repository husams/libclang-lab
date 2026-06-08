# Part 4 — Preprocessor, Diagnostics & Flags

[← Part 3 — Types & Semantics](part_3_types_semantics.md) | [Part 5 — Building Real Tools →](part_5_building_tools.md)

## What You'll Learn

- The diagnostics API — severity, category, location, and fix-its
- Parse options (`PARSE_SKIP_FUNCTION_BODIES`, `PARSE_DETAILED_PROCESSING_RECORD`) and what's *not* bound
- How `-I` / `-D` / `-std` reshape the AST — the args *are* the parse
- Macros and `#include`s as cursors via the detailed preprocessing record
- Reading real build flags from `compile_commands.json` through `CompilationDatabase` (the arg-stripping gotcha home)

Parts 1–3 treated the parse as a black box: feed in `clang_args()`, get a clean
AST. This part opens the box. You will read the parser's **diagnostics**, change
**what** it builds with `PARSE_*` options, change the **AST itself** with compiler
flags, see the **preprocessor** (macros and `#include`s) as AST nodes, and drive
real parses from a `compile_commands.json` **CompilationDatabase** — the same
input that powers clangd and clang-tidy.

All scripts run from the repo root:

```bash
cd /Users/husam/workspace/qemu-vms
python3 libclang-lab/scripts/<name>.py
```

---

## 4.1 Diagnostics

### Why

A parse rarely fails outright — it *degrades*. A missing header, a typo, a wrong
`-std`: libclang keeps going and builds a partial AST. The only way to know the
tree you are walking is trustworthy is to read `tu.diagnostics`. A tool that
ignores diagnostics silently produces wrong answers on broken input.

### What to Do

`tu.diagnostics` is an iterable of `Diagnostic` objects. The fields you care about:

| Field | Meaning |
|-------|---------|
| `.severity` | int: `Ignored=0`, `Note=1`, `Warning=2`, `Error=3`, `Fatal=4` (constants on `cx.Diagnostic`) |
| `.spelling` | the human-readable message |
| `.location` | a `SourceLocation` (`.file`, `.line`, `.column`) — `.file` is `None` for command-line diagnostics |
| `.category_name` | clang's category bucket, e.g. `'Parse Issue'`, `'Lexical or Preprocessor Issue'` |
| `.fixits` | iterable of `FixIt`; each has `.value` (replacement text) and `.range` |

`fatal_diagnostics(tu)` from `_helpers` is just `[d for d in tu.diagnostics if d.severity >= cx.Diagnostic.Error]`.

The script shows two failures:

- **A — a real C syntax error.** A snippet missing a `;` is parsed in-memory via
  `unsaved_files`. Clang emits an `Error` *and* a `fix-it` — it knows the exact
  repair.
- **B — the truncating fatal.** Parsing `shapes.c` with `args=[]` strips the
  builtin-header path, so `<stddef.h>` (pulled in by `shapes.h`) is not found.
  That is a `Fatal`, and **everything below it in the AST is silently dropped**.
  This is the lab's #1 gotcha; its broken-then-fixed home is
  [§1.2 in Part 1](part_1_foundations.md). Here we only *read* the diagnostic
  that announces it.

> Determinism note: diagnostics are sorted before printing, and locations use a
> `basename:line:col` form — never absolute paths.

### Verify

```bash
python3 libclang-lab/scripts/p4_diagnostics.py
```

### Expected

```
A. missing-semicolon snippet (parsed in-memory):
  [Error] virtual.c:1:30: expected ';' at end of declaration
        category='Parse Issue'
        fix-it: insert ';'
B. shapes.c parsed with args=[] (truncating fatal):
  [Fatal] shapes.h:4:10: 'stddef.h' file not found
        category='Lexical or Preprocessor Issue'
```

The `Error` carries a fix-it (`insert ';'`); the `Fatal` does not. Fatals are the
ones that truncate — always check for them before trusting an AST.

---

## 4.2 Parse options

### Why

The `options` bitmask passed to `index.parse()` changes *what the parser builds*,
trading completeness for speed or detail. For a code indexer that only needs
declarations, parsing function bodies is wasted work — `PARSE_SKIP_FUNCTION_BODIES`
drops them and can dramatically speed up a whole-codebase pass.

### What to Do

The options exposed by the Python binding (libclang 18.1.1), as constants on
`cx.TranslationUnit`:

| Option | Value | Effect |
|--------|-------|--------|
| `PARSE_NONE` | 0 | default |
| `PARSE_DETAILED_PROCESSING_RECORD` | 1 | retain preprocessor entities (see §4.4 — note the spelling: **PROCESSING**, not PREPROCESSING) |
| `PARSE_INCOMPLETE` | 2 | the TU is a fragment (e.g. a header parsed alone); suppresses some "missing main" semantics |
| `PARSE_SKIP_FUNCTION_BODIES` | 64 | parse signatures, drop bodies |

Combine with bitwise OR: `opt = A | B`.

> `PARSE_KEEP_GOING` (the C flag `CXTranslationUnit_KeepGoing = 0x200`, which makes
> the parser continue past a fatal instead of truncating) is **not bound** in this
> Python `cindex` — `hasattr(cx.TranslationUnit, "PARSE_KEEP_GOING")` is `False`.
> If you need it you would pass the raw integer `0x200`. We don't here; the lab
> avoids fatals up front with `clang_args()`.

The script parses `shapes.c` twice — once normally, once with
`PARSE_SKIP_FUNCTION_BODIES` — and compares. Signatures survive both parses; the
`CALL_EXPR` nodes that live inside bodies vanish from the skipped parse.

### Verify

```bash
python3 libclang-lab/scripts/p4_parse_options.py
```

### Expected

```
PARSE_SKIP_FUNCTION_BODIES = 64

function signatures (full parse): ['average', 'circle_area', 'shape_area', 'shape_translate', 'shapes_total_area']
function signatures (skip parse): ['average', 'circle_area', 'shape_area', 'shape_translate', 'shapes_total_area']

CALL_EXPR nodes, full parse: 4
CALL_EXPR nodes, skip parse: 0

Same signatures, no bodies -> fast declaration/definition indexing.
```

Identical signature lists, but `4 → 0` call expressions. The declaration index is
intact; the body-level detail is gone. That is the trade you are buying.

---

## 4.3 Compiler arguments

### Why

The `args` list is not configuration around the parse — it **is** the parse. The
same source bytes produce a different AST under different flags, because the flags
control the preprocessor (`-D`), the include search path (`-I`), and the language
dialect (`-std`). Get the args wrong and you analyze a different program than the
one that actually builds. This is *the* reason §4.5 exists: real tools read the
exact build flags from `compile_commands.json`.

### What to Do

The script demonstrates two flags changing the AST of one source:

- **`-D` toggles a preprocessor branch.** A snippet wrapped in
  `#ifdef ENABLE_LOG / #else / #endif` defines `logging_on()` in one branch and
  `logging_off()` in the other. The disabled branch is never tokenized into the
  tree — so `-DENABLE_LOG` literally changes which function exists.
  (`macros.c` also has an `IS_DEBUG` `#if`, but its `IS_DEBUG` is `#define`d
  *in-source*, which overrides any command-line `-D`; an unsaved snippet is the
  clean way to show the toggle.)
- **`-I` controls header resolution.** A snippet `#include "shapes.h"` then
  `Shape g;`. Without `-I<manifests>` the header is not found (a fatal), and the
  unknown type `Shape` degrades to `int` under clang's error recovery. With
  `clang_args()` (which adds `-I <manifests>`) it resolves to `Shape`.

`-std` belongs to the same family: it selects the language and standard library,
which is why Part 3's C++ samples use `clang_args(std="c++17")`.

### Verify

```bash
python3 libclang-lab/scripts/p4_compiler_args.py
```

### Expected

```
-D toggles which branch is compiled:
  args=[]            -> funcs: ['logging_off']
  args=[-DENABLE_LOG]-> funcs: ['logging_on']

-I controls whether the included type resolves:
  without -I -> type of `g`: 'int'    fatal=True
  with    -I -> type of `g`: 'Shape'  fatal=False

args ARE the parse: same bytes + different flags = different AST.
```

`-DENABLE_LOG` flips `logging_off → logging_on`; `-I` flips the type of `g` from
the error-recovery `int` to the real `Shape`. Same source, three different ASTs.

---

## 4.4 Macros & inclusions

### Why

By default libclang **discards** preprocessor entities — there are no cursors for
`#define` or `#include`, because the preprocessor runs before the AST is built. A
refactoring tool, a header-dependency analyzer, or a macro auditor needs them
back. `PARSE_DETAILED_PROCESSING_RECORD` retains them as cursors.

### What to Do

With the option set, three new cursor kinds appear (`cx.CursorKind.*`):

| Kind | What it is |
|------|------------|
| `MACRO_DEFINITION` | a `#define` — both object-like (`VERSION`) and function-like (`ADD(a,b)`) |
| `MACRO_INSTANTIATION` | a macro *expansion* at a use site (this is the binding's name for what clang calls a macro expansion) |
| `INCLUSION_DIRECTIVE` | an `#include` line; `cursor.get_included_file()` gives the resolved `File` |

The script parses `macros.c`, which defines `VERSION`, `GREETING`, `ADD`,
`IS_DEBUG`, `LOG` and includes `<stdint.h>` and `"shapes.h"`. Everything is
filtered to the main file (so the thousands of macros from system headers do not
appear) and sorted.

> Note `LOG` is reported at line 12: with `IS_DEBUG` defined to `0`, the
> `#else` branch wins, so the empty `#define LOG(x)` is the definition that
> survives — the preprocessor record reflects the branch actually taken.

### Verify

```bash
python3 libclang-lab/scripts/p4_preprocessing.py
```

### Expected

```
MACRO_DEFINITION (object- and function-like macros):
  macros.c:4:9  VERSION
  macros.c:5:9  GREETING
  macros.c:6:9  ADD
  macros.c:7:9  IS_DEBUG
  macros.c:12:9  LOG

MACRO_INSTANTIATION (expansions in code):
  macros.c:9:5  IS_DEBUG
  macros.c:16:13  ADD
  macros.c:16:17  VERSION

INCLUSION_DIRECTIVE (#include lines):
  macros.c:1:1  stdint.h
  macros.c:2:1  shapes.h
```

Line 16 (`int v = ADD(VERSION, 1);`) shows two instantiations — `ADD` and the
`VERSION` nested inside it. Line 9 (`#if IS_DEBUG`) shows `IS_DEBUG` expanded by
the preprocessor's conditional evaluation.

---

## 4.5 compile_commands.json & CompilationDatabase

### Why

You cannot guess a real project's compiler flags — they live in
`compile_commands.json`, emitted by CMake/Bear/etc. libclang reads that file
through `CompilationDatabase`, the same mechanism clangd and clang-tidy use. This
is how you analyze a codebase *exactly* as it is built.

### What to Do — and the gotcha (HOME)

```
fromDirectory(dir) ──▶ CompilationDatabase
       │
       ├─ getAllCompileCommands() ──▶ [CompileCommand, ...]
       └─ getCompileCommands(file) ──▶ [CompileCommand, ...]   (None if file absent)

CompileCommand: .directory   .filename   .arguments
```

`.arguments` yields the **full driver invocation**, e.g.:

```
cc -I. -c app.c -o app.o
```

**This is the gotcha (its home section):** `index.parse()` already knows the
source file and only wants the *flags*. A raw compile command also contains tokens
`parse()` does NOT want:

| Token to strip | Why |
|----------------|-----|
| `cc` (argv[0]) | the driver executable, not a flag |
| `-c` | "compile only" — meaningless to a parser |
| `-o app.o` | output file (the flag **and** its argument) |
| `app.c` | the source filename — `parse()` supplies it separately |
| `--` | arg separator that `getCompileCommands` may inject |

Keep the real flags (`-I`, `-D`, `-std`, …). Relative `-I.` paths must be resolved
against `cmd.directory`, since `parse()` does not run from that directory. The
script's `strip_for_libclang()` does exactly this — it is what every production
indexer does. After stripping, it appends `clang_args()[1:]` (the macOS sysroot +
builtin-header `-I`, minus the leading `-std`) so the parse stays clean on this
machine.

### Verify

```bash
python3 libclang-lab/scripts/p4_compiledb.py
```

### Expected

```
getAllCompileCommands(): 2 entries
  app.c: raw args = ['cc', '-I.', '-c', 'app.c', '-o', 'app.o']
  mathlib.c: raw args = ['cc', '-I.', '-c', 'mathlib.c', '-o', 'mathlib.o']

getCompileCommands('app.c'): 1 entry
  stripped (clang-ready) = ['-I.']

parsing each TU from its compile command:
  app.c: funcs=['main'] fatals=0
  mathlib.c: funcs=['add', 'multiply', 'square'] fatals=0
```

The raw `cc -I. -c app.c -o app.o` becomes just `['-I.']`; both translation units
then parse clean (`fatals=0`) with the right top-level functions. You are now
parsing the project the way its build system does — the foundation for Part 5's
tools.

---

## Checkpoint

| Concept | What You Proved |
|---------|-----------------|
| Diagnostics API | Read `.severity` / `.spelling` / `.location` / `.category_name` / `.fixits`; distinguished a recoverable `Error` (with fix-it) from a truncating `Fatal` |
| Parse options | `PARSE_SKIP_FUNCTION_BODIES` keeps signatures but drops bodies (`CALL_EXPR` 4 → 0); knew `PARSE_KEEP_GOING` is unbound in this `cindex` |
| Compiler arguments | Same source + different flags = different AST: `-D` toggled a branch, `-I` decided whether a type resolved |
| Macros & inclusions | `PARSE_DETAILED_PROCESSING_RECORD` surfaced `MACRO_DEFINITION` / `MACRO_INSTANTIATION` / `INCLUSION_DIRECTIVE`, filtered to the main file |
| CompilationDatabase | Loaded `compile_commands.json`, read `.arguments` / `.directory` / `.filename`, **stripped** the driver / `-c` / `-o` / source tokens, and fed clean flags into `parse()` |

---

[← Part 3 — Types & Semantics](part_3_types_semantics.md) | [Part 5 — Building Real Tools →](part_5_building_tools.md)

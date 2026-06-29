# clang_explore — the three featured gotchas

These are the failure modes that make libclang give *wrong* answers silently.
Each is shown broken, then fixed.

---

## 1. Header resolution — a bare parse silently truncates the AST

The pip `libclang` wheel ships the native dylib but **not** Clang's builtin
headers (`stddef.h`, `stdarg.h`, …), and on macOS the SDK path is not on the
default search path. So a flag-less parse emits a *fatal* "`'stddef.h' file not
found`" — and libclang returns a **partial AST** anyway. Your query then finds
half the symbols and you never notice.

```python
tu = ce.parse("src/shapes.c", args=[])         # BROKEN
print(len(ce.fatal_diagnostics(tu)))           # >= 1  -> AST is truncated
```

Fix — always pass real flags:

```python
tu = ce.parse("src/shapes.c", args=ce.clang_args(std="c11"))   # FIXED
assert not ce.fatal_diagnostics(tu)            # clean
```

`clang_args()` adds `-isysroot <SDK>` and the Clang resource-dir include. **For
C++ the search order matters**: it must be `-isysroot` → libc++
(`<SDK>/usr/include/c++/v1`) → Clang builtins, all via `-isystem`. Putting the
builtin dir first (a plain `-I resource-dir`) breaks libc++'s `<cstddef>`
`include_next` chain — another fatal that truncates the C++ AST. `clang_args()`
gets this right automatically when `std` starts with `c++`/`gnu++`. When a
`compile_commands.json` exists, prefer `Repo` — it uses the project's own flags.

**Rule: check `fatal_diagnostics(tu)` after every parse.** Non-empty means don't
trust the results.

---

## 2. Declaration vs definition + main-file filtering

Parsing pulls in every `#include`. An `#include`d prototype and the `.c`
definition are **two different cursors with the same spelling**. If you don't
filter, you double-count and may report the header location instead of the code.

```python
tu = ce.parse("src/shapes.c", args=ce.clang_args())
both = ce.find_symbols(tu, "shapes_total_area", main_only=False)
# -> may include the prototype from shapes.h AND the definition from shapes.c
```

Fix — filter to the parsed file, resolve the real definition:

```python
defs = [c for c in both if c.is_definition()]              # the real body
defn = ce.find_symbols(tu, "shapes_total_area")[0].get_definition()
# find_symbols(..., main_only=True) (the default) already drops #include cursors
```

`in_main_file(cursor)` / `top_level(tu)` are the primitives; `main_only=True`
(default on `find_symbols`/`dump_ast`/`Repo.find`) applies them for you.

---

## 3. Stripping compile_commands.json arguments

Raw entries in a `compile_commands.json` look like:

```json
{ "command": "cc -I. -DMAX=64 -c src/foo.c -o build/foo.o", "file": "src/foo.c" }
```

You **cannot** feed those tokens straight to `index.parse()`: the driver token
(`cc`), the source filename, and the `-c` / `-o build/foo.o` pair are not parse
arguments — they make libclang choke or misbehave. Keep `-I`, `-D`, `-std`,
`-isystem`, etc.; drop the rest.

```python
repo = ce.open_repo("/path/to/repo")
print(repo.compile_args("src/foo.c"))    # ['-I.', '-DMAX=64', '-std=c11'] — stripped
tu = repo.parse("src/foo.c")             # parsed with clean, project-correct flags
```

`Repo.compile_args()` does the stripping (`_strip_compile_command`): drop the
first token (driver), drop `-c`, drop `-o <next>`, drop any bare token ending in
the source filename, keep everything else.

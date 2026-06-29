# clang_explore — recipes (task → snippet)

Each snippet assumes:

```python
import sys; sys.path.insert(0, "/Users/husam/workspace/qemu-vms/libclang-lab/.claude/skills/clang-explore")
import clang_explore as ce
import clang.cindex as cx
```

Always `assert not ce.fatal_diagnostics(tu)` (or print and bail) before trusting
results. Keep output small.

---

## List every function with its signature

```python
tu = ce.parse("src/shapes.c", args=ce.clang_args(std="c11"))
for c in ce.find_symbols(tu, "*", kinds=[cx.CursorKind.FUNCTION_DECL]):
    if c.is_definition():
        ret = c.result_type.spelling
        params = ", ".join(a.type.spelling for a in c.get_arguments())
        print(f"{ret} {c.spelling}({params})  @ {ce.loc(c)}")
```

## What does a function call? (call tree, one level)

```python
tu = ce.parse("src/calls.c", args=ce.clang_args())
for callee, usr, site in ce.callees_of(tu, "process"):
    print(f"  process -> {callee}  @ {site}")
```

## Declaration vs definition for one symbol

```python
tu = ce.parse("src/shapes.c", args=ce.clang_args())
for c in ce.find_symbols(tu, "shapes_total_area", main_only=False):
    tag = "def" if c.is_definition() else "decl"
    print(tag, ce.loc(c))
defn = ce.find_symbols(tu, "shapes_total_area")[0].get_definition()
print("definition:", ce.loc(defn) if defn else "not in this TU")
```

## Struct fields and their types

> Types/structs/enums/typedefs are usually declared in a **header**, so pass
> `main_only=False` — the default `main_only=True` only sees the parsed `.c`.

```python
tu = ce.parse("src/shapes.c", args=ce.clang_args())
s = ce.find_symbols(tu, "Shape", kinds=[cx.CursorKind.STRUCT_DECL],
                    main_only=False)[0]
print("declared @", ce.loc(s))                 # e.g. shapes.h:23:16
for f in s.type.get_fields():
    print(f"{f.type.spelling:20} {f.spelling}")
```

## Enum constants and their values

```python
tu = ce.parse("src/shapes.c", args=ce.clang_args())
for c in ce.find_symbols(tu, "*", kinds=[cx.CursorKind.ENUM_CONSTANT_DECL],
                         main_only=False):
    print(c.spelling, "=", c.enum_value)
```

## Resolve a typedef / canonical type

```python
tu = ce.parse("src/shapes.c", args=ce.clang_args())
td = ce.find_symbols(tu, "ShapeKind", kinds=[cx.CursorKind.TYPEDEF_DECL],
                     main_only=False)[0]
print("underlying:", td.underlying_typedef_type.spelling)
print("canonical :", td.underlying_typedef_type.get_canonical().spelling)
```

## Pointer / const inspection

```python
fn = ce.find_symbols(tu, "shape_area")[0]
ptype = list(fn.get_arguments())[0].type          # const Shape *
print(ptype.kind, "->", ptype.get_pointee().spelling,
      "const?", ptype.get_pointee().is_const_qualified())
```

## C++: classes, methods, access, virtuals, namespaces, templates

```python
tu = ce.parse("src/geometry.cpp", args=ce.clang_args(std="c++17"))
for m in ce.find_symbols(tu, "*", kinds=[cx.CursorKind.CXX_METHOD]):
    print(m.access_specifier.name, m.displayname,
          "virtual" if m.is_virtual_method() else "", ce.loc(m))
ns   = ce.find_symbols(tu, "*", kinds=[cx.CursorKind.NAMESPACE])
tmpl = ce.find_symbols(tu, "*", kinds=[cx.CursorKind.CLASS_TEMPLATE,
                                       cx.CursorKind.FUNCTION_TEMPLATE])
print("namespaces:", [n.spelling for n in ns])
print("templates :", [t.spelling for t in tmpl])
```

## Macros and #includes (detailed preprocessing record)

```python
tu = ce.parse("src/macros.c", args=ce.clang_args(),
              options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
for c, _ in ce.walk(tu.cursor):
    if not ce.in_main_file(c):
        continue
    if c.kind == cx.CursorKind.MACRO_DEFINITION:
        print("#define", c.spelling, ce.loc(c))
    elif c.kind == cx.CursorKind.INCLUSION_DIRECTIVE:
        print("#include", c.spelling, ce.loc(c))
```

## Cross-file references by USR (stable key)

```python
repo = ce.open_repo("/path/to/repo")
files = ["src/mathlib.c", "src/app.c"]
tus = [repo.parse(f) for f in files]
usr = ce.find_symbols(tus[0], "multiply")[0].get_usr()
for kind, site in ce.references_to(tus, usr):
    print(kind, site)
```

## Pull flags from compile_commands.json (no guessing)

```python
repo = ce.open_repo("/path/to/repo")          # finds compile_commands.json
print(repo.compile_args("src/foo.cpp"))       # stripped, parse-ready
tu = repo.parse("src/foo.cpp")                # parsed with the project's real flags
assert not ce.fatal_diagnostics(tu)
```

## Parse an in-memory buffer (no file on disk)

```python
src = "int answer(void){ return 42; }\n"
tu = ce.parse("virtual.c", args=ce.clang_args(),
              unsaved_files=[("virtual.c", src)])
print([c.spelling for c in ce.find_symbols(tu, "*",
       kinds=[cx.CursorKind.FUNCTION_DECL])])
```

## Reparse after an edit (incremental)

```python
tu = ce.parse("a.c", args=ce.clang_args())
tu.reparse(unsaved_files=[("a.c", "int x(void){return 1;}\n")])
print([c.spelling for c in ce.find_symbols(tu, "*",
       kinds=[cx.CursorKind.FUNCTION_DECL])])
```

## Diagnostics for a file

```python
tu = ce.parse("src/bad.c", args=ce.clang_args())
for d in ce.diagnostics(tu):                   # warnings + errors
    print(f"{d['severity']:8} {d['spelling']}  @ {d['location']}")
```

## Group symbols by kind across a (scoped) repo

```python
repo = ce.open_repo("/path/to/repo")
from collections import Counter
hits = repo.find("*", files=repo.sources()[:20])   # cap how many TUs you parse
print(Counter(h["kind"] for h in hits))
```

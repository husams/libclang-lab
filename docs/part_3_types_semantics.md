# Part 3 — Types & Semantics

[← Part 2 — Navigating the AST](part_2_navigating_ast.md) | [Part 4 — Preprocessor, Diagnostics & Flags →](part_4_preprocessor_diagnostics.md)

## What You'll Learn

- The `Type` object and `TypeKind` — a vocabulary independent of `CursorKind`
- Canonical types, pointers, arrays, and qualifiers — peeling typedef sugar
- Function signatures: `result_type`, named arguments vs. argument types, variadics
- Records (`get_fields()`, byte offsets, `get_size()`), typedefs, and enums
- The semantic graph: `referenced`, `get_definition`, `canonical`, `get_usr`, lexical vs. semantic parent
- C++ semantics — access control, namespaces, inheritance, virtual/pure, and templates

Parts 1–2 treated the AST as a tree of **cursors** — nodes with a kind, a name, and a location. That gets you the *structure* of the code. This part adds the other half: the **type system** and the **semantic graph** that libclang threads through that tree.

Two ideas drive everything here:

- A cursor is a node; its `.type` is a separate object describing *what that node denotes*. `cursor.kind` (`FUNCTION_DECL`, `FIELD_DECL`, …) and `type.kind` (`POINTER`, `CONSTANTARRAY`, …) are two different vocabularies — §3.1.
- The AST is not just a tree. A *use* of a symbol points back to its *declaration* (`referenced`); a declaration points to its *definition* (`get_definition`); and every declaration carries a stable identity (`get_usr`) that survives across files — §3.6.

Sample sources: `manifests/shapes.c`, `manifests/shapes.h` (C), and `manifests/geometry.cpp` / `manifests/geometry.hpp` (C++).

> Parse-target note: the struct/enum/typedef declarations live in `shapes.h`, not `shapes.c` (the `.c` only `#include`s them). So the record/typedef/enum sections parse `shapes.h` directly — that keeps `top_level()` clean. Sections about function *bodies* parse `shapes.c`.

---

## 3.1 The Type object & TypeKind

### Why

`cursor.kind` tells you *what kind of node* you're looking at. It does not tell you the node's **type**. A `FUNCTION_DECL` node denotes a *function type*; a `FIELD_DECL` denotes the field's value type. To reason about signatures, pointers, arrays, or struct layout you must cross from the cursor world into the type world via `cursor.type`.

### What to Do

For each top-level function in `shapes.c`, read `cursor.type` and print its `type.kind` and `type.spelling`. Notice that every function node has `type.kind == FUNCTIONPROTO` even though the cursor kind is `FUNCTION_DECL` — the two vocabularies are orthogonal.

| Expression | Meaning |
|---|---|
| `cursor.kind` | the node's role in the AST (`FUNCTION_DECL`) |
| `cursor.type` | the `Type` object the node denotes |
| `type.kind` | a `TypeKind` enum value (`FUNCTIONPROTO`, `POINTER`, …) |
| `type.spelling` | the type rendered as source text (`double (const Shape *)`) |

### Verify

```bash
python3 libclang-lab/scripts/p3_types.py
```

### Expected

```
cursor (node)        type.kind      type.spelling
------------------------------------------------------------
average              FUNCTIONPROTO  double (int, ...)
circle_area          FUNCTIONPROTO  double (double)
shape_area           FUNCTIONPROTO  double (const Shape *)
shape_translate      FUNCTIONPROTO  void (Shape *, double, double)
shapes_total_area    FUNCTIONPROTO  double (const Shape *, size_t)
```

---

## 3.2 Canonical types, pointers, arrays, qualifiers

### Why

Source-level types are full of **sugar**: typedefs, `const`, `volatile`, pointer/array layers. `Shape` is really a typedef for `struct Shape`. When you compare or analyze types you usually want to look *through* the sugar — that is what `get_canonical()` does. The other navigators (`get_pointee`, `get_array_element_type`) let you peel one structural layer at a time.

```
const Shape *                  POINTER
  └─ get_pointee()  ->  const Shape           ELABORATED  (typedef sugar, is_const_qualified=True)
       └─ get_canonical()  ->  const struct Shape   RECORD  (sugar removed)
```

| Method (on `Type`) | Returns |
|---|---|
| `get_canonical()` | the type with all typedef sugar stripped |
| `get_pointee()` | for a pointer, the pointed-to type |
| `get_array_element_type()` | for an array, the element type |
| `element_count` | for a constant array, the element count (int) |
| `is_const_qualified()` / `is_volatile_qualified()` | the qualifier flags at *this* level |

### What to Do

Parse `shapes.h`. Take `shape_area`'s parameter type (`const Shape *`): `get_pointee()` gives `const Shape` (still a typedef, so `ELABORATED`), and `get_canonical()` peels that to `const struct Shape` (`RECORD`). Then read the struct's `double dimensions[3]` field (array element type + count) and `const char *name` (a `const`-qualified pointee).

### Verify

```bash
python3 libclang-lab/scripts/p3_canonical.py
```

### Expected

```
=== pointer + canonical (shape_area's 'const Shape *') ===
param type      : const Shape *  [POINTER]
get_pointee()   : const Shape  [ELABORATED]  const=True
get_canonical() : const struct Shape  [RECORD]

=== array field 'double dimensions[3]' ===
type            : double[3]  [CONSTANTARRAY]
element type    : double
element count   : 3

=== qualifiers on 'const char *name' ===
type            : const char *  [POINTER]
pointee const=True volatile=False
```

> Note `is_const_qualified()` reports the qualifier at *this* level only — `const char *` is a (non-const) pointer to `const char`, so the const is on the **pointee**, not the pointer.

---

## 3.3 Function signatures

### Why

Two different APIs describe a function's parameters, and they are not interchangeable:

- `get_arguments()` is **cursor-level** — it yields the `PARM_DECL` cursors, so you get the *named* parameters (spelling + type + location).
- `type.argument_types()` is **type-level** — it yields just the `Type` of each parameter, and per libclang it **never includes the variadic `...` slot**.

For a variadic function like `average(int n, ...)`, `get_arguments()` yields the one named param `n`, and `argument_types()` yields the one fixed type `int`. The `...` shows up only via `is_function_variadic()`.

### What to Do

For `shape_area`, `shapes_total_area`, and `average`, print `result_type`, the variadic flag, the named arguments, and the type-level argument types side by side.

### Verify

```bash
python3 libclang-lab/scripts/p3_functions.py
```

### Expected

```
shape_area  ->  returns double  variadic=False
  get_arguments()   (named) : [('s', 'const Shape *')]
  argument_types()  (type)  : ['const Shape *']
shapes_total_area  ->  returns double  variadic=False
  get_arguments()   (named) : [('shapes', 'const Shape *'), ('count', 'size_t')]
  argument_types()  (type)  : ['const Shape *', 'size_t']
average  ->  returns double  variadic=True
  get_arguments()   (named) : [('n', 'int')]
  argument_types()  (type)  : ['int']
```

---

## 3.4 Records (struct / union)

### Why

To understand a `struct` you need its **fields**: name, type, and (for layout-aware tools) byte offset and total size. `type.get_fields()` yields the `FIELD_DECL` cursors in declaration order; the layout numbers come from the type and follow the platform ABI.

| Method | Returns |
|---|---|
| `struct_type.get_fields()` | iterator of `FIELD_DECL` cursors |
| `field.type` | the field's type |
| `field.get_field_offsetof()` | offset **in bits** (divide by 8 for bytes) |
| `struct_type.get_size()` | `sizeof` the record, in bytes |

### What to Do

Parse `shapes.h`, find `struct Shape`, and print `sizeof` plus each field's byte offset, name, and type. (You could equally walk the `STRUCT_DECL`'s children filtering for `FIELD_DECL` — `get_fields()` is the shortcut.)

### Verify

```bash
python3 libclang-lab/scripts/p3_records.py
```

### Expected

```
struct Shape  (sizeof = 56 bytes)
offset  field        type
----------------------------------------
     0  kind         ShapeKind
     8  origin       Point
    24  dimensions   double[3]
    48  name         const char *
```

> The offsets and `sizeof` are real for this machine's LP64 ABI — they are layout-dependent, not universal. `get_field_offsetof()` returns **bits**; the script divides by 8.

---

## 3.5 Typedefs & enums

### Why

A `typedef` is an alias: `TYPEDEF_DECL.underlying_typedef_type` gives you what it aliases (still sugared — see §3.2 for peeling further). An `enum` is a small declaration whose children are the constants: each `ENUM_CONSTANT_DECL` carries an integer `.enum_value`, which the compiler fills in even when the source doesn't write it out.

| Expression | Returns |
|---|---|
| `typedef_cursor.underlying_typedef_type` | the aliased `Type` |
| `enum_cursor.get_children()` | the `ENUM_CONSTANT_DECL` cursors |
| `constant.enum_value` | the constant's integer value |

### What to Do

Parse `shapes.h`. Print each typedef's underlying type (`Point` → `struct Point`, `ShapeKind` → `enum ShapeKind`, `Shape` → `struct Shape`), then list the `ShapeKind` enum constants with their auto-assigned values.

### Verify

```bash
python3 libclang-lab/scripts/p3_typedefs_enums.py
```

### Expected

```
typedef  ->  underlying_typedef_type
--------------------------------------------
Point      ->  struct Point  [ELABORATED]
Shape      ->  struct Shape  [ELABORATED]
ShapeKind  ->  enum ShapeKind  [ELABORATED]

enum ShapeKind constants:
  SHAPE_CIRCLE     = 0
  SHAPE_RECTANGLE  = 1
  SHAPE_TRIANGLE   = 2
```

---

## 3.6 Semantic links

### Why

The AST is more than a tree — libclang threads a **semantic graph** through it. This is what turns "a call expression named `shape_area`" into "a call that resolves to the function declared at this exact location". Four links matter:

| Link (on a `Cursor`) | Direction | Meaning |
|---|---|---|
| `referenced` | use → decl | a use of a symbol resolves to its declaration |
| `get_definition()` | decl → defn | a declaration resolves to its definition (or `None`) |
| `canonical` | any decl → first decl | the *first* declaration (e.g. the prototype) of a symbol |
| `get_usr()` | decl → id | a stable, location-independent symbol identity |

Two more links describe *where a declaration lives*:

- `lexical_parent` — where the cursor is **written** in the source.
- `semantic_parent` — where it **logically belongs**.

In C, file-scope functions have both parents equal to the translation unit. They **diverge** for out-of-line C++ method definitions — that case is shown in §3.7.

### What to Do

Parse `shapes.c`. Find the `CALL_EXPR` to `shape_area` inside `shapes_total_area`'s body, then follow the links: `referenced` lands on the definition (`shapes.c:12`), `get_definition()` confirms it, and `canonical` points back to the *first* declaration — the prototype in `shapes.h:31`. The prototype and the definition share one USR, which is exactly how Part 5 will link symbols **across files**.

```
CALL_EXPR 'shape_area'  --referenced-->  FUNCTION_DECL (definition)  shapes.c:12
                                              |--get_definition--> (itself, it IS the defn)
                                              |--canonical-------> prototype  shapes.h:31
                                              |--get_usr---------> c:@F@shape_area
```

### Verify

```bash
python3 libclang-lab/scripts/p3_semantics.py
```

### Expected

```
CALL_EXPR 'shape_area' @ shapes.c:33:18
  referenced      -> shape_area @ shapes.c:12:8  [FUNCTION_DECL]
  get_definition  -> shape_area @ shapes.c:12:8
  canonical       -> @ shapes.h:31:8  (the first declaration / prototype)
  get_usr         -> c:@F@shape_area
  USR(defn)==USR(ref): True

shape_area lexical_parent  : TRANSLATION_UNIT
shape_area semantic_parent : TRANSLATION_UNIT
```

---

## 3.7 C++ semantics

### Why

C++ adds structure the C samples can't show: namespaces, access control, inheritance, virtual/pure-virtual methods, and templates. libclang exposes all of it — but with two caveats this section makes concrete:

1. **Filtering.** The TU is `geometry.cpp`, but the declarations live in `geometry.hpp`, and `#include <string>`/`<vector>` drag in *thousands* of libc++ nodes. `in_main_file()` would drop everything in the header, so here we filter by a basename set `{geometry.cpp, geometry.hpp}` instead. (Main-file filtering and the decl-vs-definition duplicate-cursor gotcha are owned by [Part 2 §2.4](part_2_navigating_ast.md) — see there for the why.)
2. **Templates show the pattern, not instantiations.** libclang gives you the template *definition* (`max_of`, `Box`), but has limited visibility into instantiations. That limit is explored in [Part 6 — Advanced & Production](part_6_advanced_production.md).

| Concept | API |
|---|---|
| namespace | `CursorKind.NAMESPACE` |
| inheritance | `CXX_BASE_SPECIFIER` children of a `CLASS_DECL`; base `access_specifier` |
| access control | `cursor.access_specifier` on `CXX_METHOD` / `FIELD_DECL` |
| virtual / pure-virtual | `is_virtual_method()` / `is_pure_virtual_method()` |
| templates | `FUNCTION_TEMPLATE`, `CLASS_TEMPLATE` |

### What to Do

Parse `geometry.cpp` with `clang_args(std="c++17")`, filter to the basename set, and report: the namespace, the `Circle : public Shape` inheritance edge, each class method's access + virtual/pure flags, the field access specifiers, and the two templates. Finally, the out-of-line `Circle::area` definition (written in `geometry.cpp`) demonstrates the §3.6 parent divergence: its **lexical** parent is namespace `geo` (where it's written) but its **semantic** parent is class `Circle` (where it belongs).

### Verify

```bash
python3 libclang-lab/scripts/p3_cpp.py
```

### Expected

```
NAMESPACE        : ['geo']
INHERITANCE      :
  class Circle   bases=[('Shape', 'PUBLIC')]
  class Shape    bases=[]
METHODS (in-class declarations):
  Shape::area   access=PUBLIC    virtual=True pure=True
  Circle::area   access=PUBLIC    virtual=True pure=False
  Shape::name   access=PUBLIC    virtual=False pure=False
FIELDS           :
  name_    std::string  access=PROTECTED
  radius_  double       access=PRIVATE
  value_   T            access=PRIVATE
TEMPLATES (libclang shows the PATTERN, not instantiations -> Part 6):
  CLASS_TEMPLATE    Box  @ geometry.hpp:43:7
  FUNCTION_TEMPLATE max_of  @ geometry.hpp:37:3
OUT-OF-LINE Circle::area @ geometry.cpp:14:16:
  lexical_parent =NAMESPACE 'geo'  semantic_parent=CLASS_DECL 'Circle'
```

`Shape::area` is `virtual=True pure=True` (it's `= 0`, the abstract base); `Circle::area` overrides it (`virtual=True pure=False`). `value_` has type `T` because it's a member of the `Box<T>` template *pattern* — no concrete type yet.

---

## Checkpoint

| Concept | What You Proved |
|---|---|
| 3.1 cursor vs type | A node's `cursor.kind` and its `type.kind` are independent vocabularies; `cursor.type` crosses between them. |
| 3.2 canonical / structure | `get_canonical()` strips typedef sugar; `get_pointee` / `get_array_element_type` / `element_count` / `is_const_qualified` peel structural layers. |
| 3.3 signatures | `get_arguments()` (named, cursor-level) differs from `type.argument_types()` (type-level, no `...`); variadics surface only via `is_function_variadic()`. |
| 3.4 records | `type.get_fields()` yields fields with type, byte offset (`get_field_offsetof()/8`), and `get_size()`. |
| 3.5 typedefs / enums | `underlying_typedef_type` gives the aliased type; `ENUM_CONSTANT_DECL.enum_value` gives each constant's integer value. |
| 3.6 semantic links | `referenced` (use→decl), `get_definition` (decl→defn), `canonical` (→first decl), and a shared `get_usr()` form the semantic graph; lexical/semantic parents coincide in C. |
| 3.7 C++ semantics | Access, namespaces, inheritance (`CXX_BASE_SPECIFIER`), virtual/pure flags, and templates are exposed; out-of-line method defs split lexical vs semantic parent; templates expose the pattern, not instantiations. |

---

[← Part 2 — Navigating the AST](part_2_navigating_ast.md) | [Part 4 — Preprocessor, Diagnostics & Flags →](part_4_preprocessor_diagnostics.md)

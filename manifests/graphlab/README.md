# graphlab — a C++ test project for the cidx code graph

A small, self-contained C++17 project whose code deliberately exercises every
relationship the cidx graph extracts: inheritance (incl. multiple), templates,
specializations, dynamic dispatch, and deep/repeated call chains.

## Files

| File | Purpose |
|------|---------|
| `shapes.hpp` / `shapes.cpp` | Abstract `Shape` (pure virtual) + `Circle`, `Rectangle` overrides. Decl-in-header / def-in-.cpp split. |
| `creatures.hpp` / `creatures.cpp` | **Multiple inheritance**: `Amphibian : public Walker, public Swimmer`. Abstract-param functions `make_it_walk/swim`. |
| `chain.hpp` / `chain.cpp` | **Inheritance chain** `A ← B ← C ← D` (single inheritance, 4 levels) + a parallel `rank()` override chain. |
| `nested.hpp` / `nested.cpp` | **Nested namespaces** (`org::project::util`, classic blocks + C++17 `org::project::net` compact form) with cross-namespace calls. |
| `containers.hpp` | **Class & function templates** + their **explicit specializations**; a class-template method that calls another template. |
| `pipeline.hpp` / `pipeline.cpp` | **Deep call graph** + **repeated call sites**; `measure(const Shape&)` (abstract param + dynamic dispatch). |
| `instantiations.cpp` | Explicit template instantiations (and the libclang limitation note). |
| `main.cpp` | Drives everything so the indexer sees real *uses*, not just declarations. |
| `compile_commands.json` | Compilation database (`c++ -std=c++17 -I.`). |

## Feature → where it lives → what the graph shows

| Requested feature | Code | Graph edge(s) captured |
|---|---|---|
| Multiple inheritance | `Amphibian` | `inherits` Amphibian→Walker, Amphibian→Swimmer ✅ |
| Inheritance chain A←B←C←D | `chain::{A,B,C,D}` | `inherits` chain B→A→…, parallel `overrides` chain; transitive `ancestors`/`children` ✅ |
| Class template defined & used | `Wrapper<T>`, `Stack<T>` | symbols `class-template`; `instantiates` ✅ |
| Function template defined & used | `describe<T>`, `combine<T>`, `twice<T>` | symbols `function-template` ✅ |
| Function overload defined & used | `scale(int)`, `scale(double)`, `scale(int,int)` | 3 distinct symbols, each its own `calls` edge from `main` ✅ |
| `new` / `delete` (heap alloc) | `make_shape` (`new Circle`), `consume` (`delete s`) | `new`: `calls` make_shape→`Circle::Circle` ✅. `delete`: destructor edge **not** captured ⚠️ (see note). |
| Nested namespaces | `org::project::util`, `org::project::net` | fully-qualified names + `contains` edges ns→child; cross-ns `calls` ✅ |
| Class-template method calls another template | `Wrapper<bool>::label → describe<bool>` | `calls` ✅ (via **specialization**; see note) |
| Deep call graph | `main→run_pipeline→stage_process→transform→normalize` | `calls` chain, depth 5 ✅ |
| Same function, multiple call sites | `transform→normalize` ×2; `run_pipeline→stage_process` ×2 | `calls` with `count=2` + two `edge_site`s ✅ |
| Dynamic dispatch | `measure→Shape::area`, base-pointer loop | `calls`→virtual; `dispatch_targets(Shape::area)` = {Circle::area, Rectangle::area} ✅ |
| Function takes abstract class, calls method | `measure(const geo::Shape&)` | `calls` Shape::area/perimeter ✅ |
| Specialization defined & used (calls/called) | `describe<bool>`, `Wrapper<bool>` | `specializes` Wrapper<bool>→Wrapper; `describe<bool>` called by `main` + `Wrapper<bool>::label` ✅ |

## Template call capture (and its remaining limit)

Calls *inside primary template bodies* ARE captured: cidx recovers a dependent
callee from its single-overload `OverloadedDeclRef` when `CALL_EXPR.referenced`
is null. So `Stack<T>::summary → combine`, `Wrapper<T>::label → describe`, and
`twice<T> → combine` are real `calls` edges to the primary templates
(`indexer/clang/ast.py:_recover_overloaded_callee`, mirrored in cidx-cpp).

What still cannot be captured: libclang does **not** expose the bodies of
*instantiated* template methods (even with explicit instantiation), so there is
no per-instantiation (`Stack<int>::summary`) call edge — only the template-level
one above. Ambiguous overload sets (e.g. stdlib `to_string`) are deliberately
left unresolved so cidx never guesses a wrong target. Explicit **class**
instantiations still link to their primary via `specializes`
(`Wrapper<int> → Wrapper`); explicit **function** instantiations produce no
libclang cursor at all, so they are invisible.

A second extraction gap: the call extractor (`indexer/clang/ast.py`) emits a
`calls` edge only for `CALL_EXPR` cursors. A `new`-expression's constructor call
*is* a `CALL_EXPR` (so `make_shape → Circle::Circle` is captured), but a
`delete`-expression is a `CXX_DELETE_EXPR` with an *implicit* destructor call, so
`consume → Shape::~Shape` is **not** recorded. Capturing it would require teaching
the extractor to handle `CXX_DELETE_EXPR` (an indexer change).

## Reindex

```bash
cd <repo>/libclang-lab/project
python -m indexer init --force
python -m indexer add-source --path ../manifests/graphlab --name graphlab --no-git
python -m indexer import --db ../manifests/graphlab
python -m indexer index
python -m indexer resolve
```

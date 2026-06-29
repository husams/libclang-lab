# clang_explore — API reference

Import as `import clang_explore as ce` (after putting the skill dir on
`sys.path`) plus `import clang.cindex as cx` for the enums (`CursorKind`,
`TypeKind`, `AccessSpecifier`, `TokenKind`, `TranslationUnit.PARSE_*`).

Everything returns plain Python or raw `clang.cindex` cursors/types — you read
attributes off those objects. The module never caches a global; you own the TUs.

---

## Parse & compile flags

### `clang_args(std="c11", project_includes=(), defines=(), extra=()) -> list[str]`
Compiler flags that make libclang resolve system + builtin headers. Search-path
order is C++-correct (`-isysroot` → libc++ → Clang resource dir, via `-isystem`);
for `std="c++NN"` the libc++ leg is added automatically.
- `std` — `"c11"`, `"c17"`, `"c++17"`, `"c++20"`, `"gnu++20"`, …
- `project_includes` — your repo's include roots (added as `-I`).
- `defines` — e.g. `("DEBUG", "MAX=64")` → `-DDEBUG -DMAX=64`.
- `extra` — any raw flags appended verbatim.

### `parse(path, args=None, options=0, unsaved_files=None) -> TranslationUnit`
Parse a file (or in-memory buffer). **Always pass `args=clang_args(...)`** —
`args=[]` reproduces the truncation gotcha.
- `options` — bitmask, e.g. `cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD`
  (capture macro defs + inclusion directives), `PARSE_SKIP_FUNCTION_BODIES`
  (fast decl-only scan), `PARSE_INCOMPLETE`.
- `unsaved_files` — `[("name.c", source_str)]` to parse a buffer with no file on
  disk (also how you reparse after an edit: `tu.reparse(unsaved_files=[...])`).

### `Repo(root, std="c++17", project_includes=(), compdb_dir=None)` / `open_repo(root, **kw)`
A project root that resolves per-file flags from `compile_commands.json` when one
exists (searched in `root`, or `compdb_dir`), else falls back to `clang_args()`.
- `.compile_args(file) -> list[str]` — parse-ready flags for `file`: the stripped
  compdb entry (driver token, source filename, `-c`/`-o` removed) or `clang_args`.
- `.parse(file, options=0, unsaved_files=None) -> TranslationUnit`.
- `.sources(pattern="**/*") -> list[Path]` — translation-unit sources under root.
- `.find(name_pattern, kinds=None, files=None, main_only=True, limit=200)
  -> list[dict]` — search symbols. **Pass `files=[...]`** to scope the parse on a
  large repo (otherwise it parses every source). Each dict:
  `{spelling, kind, usr, displayname, loc}`.

---

## Traverse & locate

- `walk(cursor, depth=0)` → yields `(cursor, depth)` pre-order for the subtree.
- `loc(cursor) -> "path:line:col"` (or `"<builtin>"`). Use to ground claims.
- `in_main_file(cursor) -> bool` — True if the cursor is in the parsed file, not
  a pulled-in `#include`.
- `top_level(tu)` → direct children of the TU that originate in the main file.
- `dump_ast(tu_or_cursor, max_depth=6, main_only=True) -> str` — indented
  `KIND 'spelling' [type] loc` dump. Keep `max_depth` small.

---

## Query symbols

### `find_symbols(tu, pattern="*", kinds=None, main_only=True) -> list[Cursor]`
Cursors whose `.spelling` matches the glob `pattern`, optionally filtered to
`kinds` (iterable of `cx.CursorKind`). Then read off each cursor:

| attribute | meaning |
|---|---|
| `.spelling` | the identifier (e.g. `shape_area`) |
| `.displayname` | with signature (e.g. `shape_area(const Shape *)`) |
| `.kind` | a `cx.CursorKind` (`.name` for the string) |
| `.get_usr()` | Unified Symbol Resolution — stable key across files |
| `.type` | the cursor's `Type` (see below) |
| `.result_type` | function return `Type` |
| `.get_arguments()` | parameter cursors (`a.type.spelling` for each) |
| `.is_definition()` | True for the definition, False for a forward decl |
| `.get_definition()` | the defining cursor (across the TU) or None |
| `.referenced` | for a use/call: the declaration it refers to |
| `.semantic_parent` / `.lexical_parent` | enclosing scope |
| `.access_specifier` | C++ `PUBLIC`/`PROTECTED`/`PRIVATE` |
| `.underlying_typedef_type` | for a `TYPEDEF_DECL` |
| `.enum_value` | for an `ENUM_CONSTANT_DECL` |
| `.is_static_method()` / `.is_virtual_method()` / `.is_pure_virtual_method()` | C++ method flags |
| `.get_tokens()` | lexical tokens (`t.spelling`, `t.kind` is a `TokenKind`) |
| `.extent` | `SourceRange` (`.start.line` … `.end.line`) |

### Types (`cursor.type`, `result_type`, argument types, `get_pointee()`, …)
| attribute | meaning |
|---|---|
| `.kind` | a `cx.TypeKind` (`POINTER`, `RECORD`, `FUNCTIONPROTO`, `ELABORATED`, …) |
| `.spelling` | the type as written (e.g. `const Shape *`) |
| `.get_canonical()` | strip typedefs/aliases to the underlying type |
| `.get_pointee()` | element type of a pointer/reference |
| `.get_array_element_type()` / `.get_array_size()` | array decomposition |
| `.is_const_qualified()` / `.is_volatile_qualified()` | qualifiers |
| `.get_fields()` | for a record type: field cursors |
| `.is_function_variadic()` | for a function type |
| `.argument_types()` | function parameter types |
| `.get_declaration()` | cursor that declares this type |

---

## Calls & references

- `callees_of(tu, func_name) -> [(callee_spelling, callee_usr, "file:line"), …]`
  — every `CALL_EXPR` inside the definition of `func_name`.
- `callers_of(tu, func_name) -> [(enclosing_func, "file:line"), …]` — calls TO
  `func_name`, **within this TU only**. For repo-wide callers, parse every TU and
  match by USR (`references_to`), or use the **cidx-graph** skill.
- `references_to(tus, usr) -> [(kind_name, "file:line"), …]` — every cursor in
  the given TUs that references `usr` (plus the `DEFINITION`). USR is the correct
  cross-file key: it is TU-invariant for the same entity.

---

## Diagnostics

- `fatal_diagnostics(tu) -> list[Diagnostic]` — severity ≥ ERROR. **Empty list ==
  trustworthy AST.** Check after every parse.
- `diagnostics(tu, min_severity=cx.Diagnostic.Warning) -> list[dict]` —
  `{severity, spelling, location}` for everything at/above the level.
- Raw access: `tu.diagnostics` (iterable of `Diagnostic` with `.severity`,
  `.spelling`, `.location`, `.fixits`).

---

## Escape hatch

Anything the helpers don't cover, do directly on the raw `clang.cindex` objects
returned by `parse()` / `find_symbols()` — the full bindings are available. The
helpers are conveniences, not a wall.

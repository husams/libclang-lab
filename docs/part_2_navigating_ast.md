	# Part 2 — Navigating the AST — cursors, names, locations, tokens

[← Part 1 — Foundations](part_1_foundations.md) | [Part 3 — Types & Semantics →](part_3_types_semantics.md)

## What You'll Learn

- The `CursorKind` taxonomy and how to test exact kinds vs. categories
- The three names on every cursor — `spelling`, `displayname`, and `get_usr()`
- `SourceLocation` and `SourceRange` extents — pointing precisely at code
- Main-file filtering and the declaration-vs-definition duplicate-cursor gotcha (the home of both)
- Tokens (`TokenKind`) — the lexical view beneath the cursor tree
- How to build a reusable, main-file-filtered AST dumper

Part 1 got a clean `TranslationUnit` and walked it. Now we learn the four things you read off **every** node: its **kind** (what it is), its **names** (what it's called), its **location** (where it lives), and its **tokens** (the raw text underneath). Then we build a reusable AST dumper.

Everything here filters to the **main file**. Parsing `shapes.c` pulls in `shapes.h`, `<stddef.h>`, `<math.h>` and more — over a thousand nodes you didn't write. Section 2.4 is the home of that gotcha (and its evil twin: declaration-vs-definition duplicate cursors).

| Section | Concept | Script |
|---------|---------|--------|
| 2.1 | CursorKind — the node taxonomy | `p2_cursor_kinds.py` |
| 2.2 | Names — spelling / displayname / USR | `p2_names.py` |
| 2.3 | Locations & extents | `p2_locations.py` |
| 2.4 | Filtering to your file (gotcha home) | `p2_main_file.py` |
| 2.5 | Tokens — the lexical view | `p2_tokens.py` |
| 2.6 | A reusable AST dumper (deliverable) | `p2_ast_dump.py` |

---

## 2.1 CursorKind — the node taxonomy

**Why.** Every `Cursor` has a `.kind` — a `CursorKind` enum value that tells you *what* the node is: a `FUNCTION_DECL`, a `CALL_EXPR`, an `INTEGER_LITERAL`. Almost all AST work is "find the cursors of kind X." You need to know how to read and test a kind.

**What to do.** Two patterns:

- **Exact test** — compare against a constant: `cursor.kind == CursorKind.FUNCTION_DECL`. This is how you select nodes.
- **Category test** — the broad bucket a kind falls in. These helpers live on the **`CursorKind`**, not the cursor: `cursor.kind.is_declaration()`, `.is_reference()`, `.is_expression()`, `.is_statement()`. Use them when you want "any declaration" rather than one specific kind.

The script enumerates the top-level cursors of `shapes.c` (via `top_level()`, already main-file-filtered) and labels each with its kind and its category.

```
CursorKind:  what a node IS         ──  cursor.kind == CursorKind.FUNCTION_DECL
category:    which bucket it's in   ──  cursor.kind.is_declaration()
```

**Verify**

```
python3 libclang-lab/scripts/p2_cursor_kinds.py
```

**Expected**

```
Top-level cursors in shapes.c (kind | category | spelling @ loc):
  FUNCTION_DECL  declaration  average            shapes.c:39:8
  FUNCTION_DECL  declaration  circle_area        shapes.c:8:15
  FUNCTION_DECL  declaration  shape_area         shapes.c:12:8
  FUNCTION_DECL  declaration  shape_translate    shapes.c:25:6
  FUNCTION_DECL  declaration  shapes_total_area  shapes.c:30:8

FUNCTION_DECL count (== test): 5
function names: ['average', 'circle_area', 'shape_area', 'shape_translate', 'shapes_total_area']
```

Every top-level node in `shapes.c` is a `FUNCTION_DECL` and the category helper agrees it is a `declaration`. The `typedef`/`struct`/`enum`/`#define` declarations live in `shapes.h`, so the main-file filter correctly drops them here — that distinction is the subject of 2.4.

---

## 2.2 Names — spelling vs displayname vs get_usr()

**Why.** "The name of a cursor" is three different things, and picking the wrong one costs you. `spelling` is the bare identifier. `displayname` adds the signature (essential once C++ overloading enters in Part 3). `get_usr()` is a **Unified Symbol Resolution** string — a stable identity for the *symbol* that is the same across translation units. You match on USRs when you want cross-file identity (Part 5 builds a call graph on exactly this).

**What to do.** Read all three off each `FUNCTION_DECL` in `shapes.c`.

| Accessor | What it gives you | `shape_area` |
|----------|-------------------|--------------|
| `cursor.spelling` | identifier as written | `shape_area` |
| `cursor.displayname` | identifier + parameter signature | `shape_area(const Shape *)` |
| `cursor.get_usr()` | stable cross-TU symbol id | `c:@F@shape_area` |

Note the USR encodes *linkage*: external functions get `c:@F@name`, but a `static` (internal-linkage) function is namespaced by file. `circle_area` is `static`, so its USR is `c:shapes.c@F@circle_area` — it can never collide with a `circle_area` in another file. That's what "stable cross-TU identity" buys you (→ [Part 5 — Building Real Tools](part_5_building_tools.md)).

**Verify**

```
python3 libclang-lab/scripts/p2_names.py
```

**Expected**

```
Three names libclang attaches to every cursor:
  spelling     = the identifier as written
  displayname  = identifier + signature (disambiguates overloads)
  get_usr()    = Unified Symbol Resolution: stable cross-TU identity

spelling     : average
displayname  : average(int, ...)
usr          : c:@F@average

spelling     : circle_area
displayname  : circle_area(double)
usr          : c:shapes.c@F@circle_area

spelling     : shape_area
displayname  : shape_area(const Shape *)
usr          : c:@F@shape_area

spelling     : shape_translate
displayname  : shape_translate(Shape *, double, double)
usr          : c:@F@shape_translate

spelling     : shapes_total_area
displayname  : shapes_total_area(const Shape *, size_t)
usr          : c:@F@shapes_total_area
```

---

## 2.3 Locations & extents

**Why.** To report a finding ("unused function at line 30") or slice source text, you need positions. libclang gives two objects: a `SourceLocation` (a single point) and a `SourceRange` (a span). Get them right and your tool can point precisely at code.

**What to do.**x§x§x§

- `cursor.location` → a **`SourceLocation`** with `.file`, `.line`, `.column`, and `.offset` (byte offset from the start of the file). The lab's `loc()` helper formats `basename:line:col` from this — always print locations through `loc()`, never raw absolute paths.
- `cursor.extent` → a **`SourceRange`** with `.start` and `.end`, each a `SourceLocation`. For a function *definition*, the extent spans the whole thing — from the return type to the closing `}`.

```
SourceRange (extent)
  .start ─────────────────────────────► .end
  shape_area:  12:1  ──────────────────► 23:2
               (return type)       (closing brace)
```

The script prints each function's start `loc()` and its full extent, then breaks out the four `SourceLocation` fields for `shape_area`.

**Verify**

```
python3 libclang-lab/scripts/p2_locations.py
```

**Expected**

```
Each function's start location and full extent (start -> end):
  function           loc()                  extent (line:col -> line:col)
  average            shapes.c:39:8          39:1 -> 48:2
  circle_area        shapes.c:8:15          8:1 -> 10:2
  shape_area         shapes.c:12:8          12:1 -> 23:2
  shape_translate    shapes.c:25:6          25:1 -> 28:2
  shapes_total_area  shapes.c:30:8          30:1 -> 36:2

SourceLocation fields for shape_area:
  file   : shapes.c
  line   : 12
  column : 8
  offset : 247  (bytes from start of file)
```

Note the start `loc()` column (12:**8**) points at the *name* `shape_area`, while the extent start (12:**1**) points at the start of the declaration (`double`). The cursor's `.location` is the identifier; the `.extent` is the whole construct.

---

## 2.4 Filtering to your file

**← This section is the home of two related gotchas. Other parts link here instead of re-explaining.**

**Why.** When you parse `shapes.c`, libclang parses *everything it includes too*. A raw walk of the TU includes `shapes.h`, `<stddef.h>`, `<math.h>`, and the Clang builtins. For a C++ file that `#include`s `<string>` that's **thousands** of libc++ nodes. The exact header total even varies by SDK version — which is precisely why printing or counting a raw whole-TU walk gives non-deterministic, noise-filled output. **You must filter to the main file**, and you never print the whole-TU total.

**What to do — the filter.** A cursor belongs to the main file when its source file equals the file you asked to parse:

```python
cursor.location.file.name == cursor.translation_unit.spelling
```

That's exactly what the helpers `in_main_file(cursor)` and `top_level(tu)` do. Use them on every collection you print.

**The second gotcha — declaration vs definition.** `shapes_total_area` is **declared** (a prototype, no body) in `shapes.h` and **defined** (with a body) in `shapes.c`. After the include is expanded, the TU contains **two `FUNCTION_DECL` cursors with the same spelling**. A naive `walk()` + `next()` that grabs the first match can land on the **prototype** — and then `get_children()` shows no body and you wonder where the function went.

Three accessors untangle this:

| Accessor | Answers | Returns |
|----------|---------|---------|
| `cursor.is_definition()` | does *this* cursor have a body? | `True` for the `.c` one, `False` for the prototype |
| `cursor.get_definition()` | give me the one true definition | the `.c` cursor, from *any* redeclaration |
| `cursor.canonical` | give me a stable anchor | the *first* declaration (here, the `.h` prototype) — same for both |

`get_definition()` jumps to the body; `canonical` collapses every redeclaration to one representative so you can dedupe.

```
shapes.h:37   prototype  ──get_definition()──►  shapes.c:30  definition (has body)
     │                                                │
     └────────────── .canonical ──────────────────────┘
                  (both → shapes.h:37, the first decl)
```

**Verify**

```
python3 libclang-lab/scripts/p2_main_file.py
```

**Expected**

```
Filtering to the main file:
  tu.spelling           : shapes.c
  nodes in main file     : 186  (in_main_file == True)
  test = (cursor.location.file.name == cursor.translation_unit.spelling)
  (the rest of the TU comes from headers; that total varies by SDK)

Two cursors named 'shapes_total_area' (decl in .h, def in .c):
  shapes.c:30:8          definition               body_present=True
  shapes.h:37:8          declaration (prototype)  body_present=False

Following the links from the prototype cursor:
  prototype at            : shapes.h:37:8
  get_definition() ->      : shapes.c:30:8  (the .c body)
  proto.canonical at       : shapes.h:37:8  (first decl seen)
  defn.canonical at        : shapes.h:37:8  (same anchor)
  same canonical?          : True
```

`shapes.c` contributes 186 nodes; everything else in the TU comes from `shapes.h`, `<stddef.h>`, `<math.h>` and the builtins — and that header total shifts with the SDK, which is exactly why you filter and never print it. The two `shapes_total_area` cursors, by contrast, are real and stable: one has a body, one doesn't, and the link accessors let you always reach the right one.

---

## 2.5 Tokens — the lexical view

**Why.** Cursors are the **syntactic** view: the tree the parser built (a `BINARY_OPERATOR` with two operand subtrees). Tokens are the **lexical** view: the flat stream of words the lexer produced before any parsing — every keyword, identifier, punctuation mark, literal, and comment, in source order. You reach for tokens when you care about *exact text*: formatting, comment extraction, or spotting something the parser folded away.

**What to do.** `cursor.get_tokens()` lexes the source over that cursor's extent and yields `Token` objects. Each has `.spelling` (the text) and `.kind` (a `TokenKind`, whose `.name` is one of **KEYWORD, IDENTIFIER, PUNCTUATION, LITERAL, COMMENT**). The script tokenizes `shape_translate` and tallies the kinds.

| View | API | Granularity |
|------|-----|-------------|
| Syntactic | `cursor.get_children()` | parsed tree nodes |
| Lexical | `cursor.get_tokens()` | raw words in source order |

**Verify**

```
python3 libclang-lab/scripts/p2_tokens.py
```

**Expected**

```
Tokens of shape_translate (31 total), in source order:
  #   kind         spelling
  0   KEYWORD      void
  1   IDENTIFIER   shape_translate
  2   PUNCTUATION  (
  3   IDENTIFIER   Shape
  4   PUNCTUATION  *
  5   IDENTIFIER   s
  6   PUNCTUATION  ,
  7   KEYWORD      double
  8   IDENTIFIER   dx
  9   PUNCTUATION  ,
  10  KEYWORD      double
  11  IDENTIFIER   dy
  12  PUNCTUATION  )
  13  PUNCTUATION  {
  14  IDENTIFIER   s
  15  PUNCTUATION  ->
  16  IDENTIFIER   origin
  17  PUNCTUATION  .
  18  IDENTIFIER   x
  19  PUNCTUATION  +=
  20  IDENTIFIER   dx
  21  PUNCTUATION  ;
  22  IDENTIFIER   s
  23  PUNCTUATION  ->
  24  IDENTIFIER   origin
  25  PUNCTUATION  .
  26  IDENTIFIER   y
  27  PUNCTUATION  +=
  28  IDENTIFIER   dy
  29  PUNCTUATION  ;
  30  PUNCTUATION  }
```

```
Count by TokenKind:
  IDENTIFIER   13
  KEYWORD      3
  PUNCTUATION  15
```

The tokens are the raw text `s -> origin . x += dx`; the parser would instead give you a `MEMBER_REF_EXPR` inside a `COMPOUND_ASSIGNMENT_OPERATOR`. Same code, two lenses.

---

## 2.6 Build a reusable AST dumper (deliverable)

**Why.** You now have all four pieces — kind, spelling, location, and the main-file filter. Combine them into a tool you'll reuse for the rest of the lab: an **indented, main-file-filtered tree printer** with an optional kind filter. Seeing the tree printed is the fastest way to understand any unfamiliar source.

**What to do.** `p2_ast_dump.py` defines `dump(cursor, depth, kind_filter)` that recurses over `get_children()`, **skips anything not `in_main_file`** (so headers never leak in), and prints `kind | spelling @ loc()` indented by depth. An optional CLI argument names a `CursorKind` to filter to (recursion still descends through unmatched nodes, so nesting is preserved). The script runs it on `calls.c`.

```
dump(cursor):
  for child in cursor.get_children():
    if not in_main_file(child): skip          # headers never leak in
    if kind_filter and child.kind not in it: don't print (but still recurse)
    print indent · kind · spelling · loc()
    dump(child, depth+1)
```

**Verify**

```
python3 libclang-lab/scripts/p2_ast_dump.py
```

**Expected**

```
AST dump of calls.c (full tree):
FUNCTION_DECL        leaf_a         @ calls.c:3:12
  PARM_DECL            x              @ calls.c:3:23
  COMPOUND_STMT        <anon>         @ calls.c:3:26
    RETURN_STMT          <anon>         @ calls.c:3:28
      BINARY_OPERATOR      <anon>         @ calls.c:3:35
        UNEXPOSED_EXPR       x              @ calls.c:3:35
          DECL_REF_EXPR        x              @ calls.c:3:35
        INTEGER_LITERAL      <anon>         @ calls.c:3:39
FUNCTION_DECL        leaf_b         @ calls.c:4:12
  PARM_DECL            x              @ calls.c:4:23
  COMPOUND_STMT        <anon>         @ calls.c:4:26
    RETURN_STMT          <anon>         @ calls.c:4:28
      BINARY_OPERATOR      <anon>         @ calls.c:4:35
        UNEXPOSED_EXPR       x              @ calls.c:4:35
          DECL_REF_EXPR        x              @ calls.c:4:35
        INTEGER_LITERAL      <anon>         @ calls.c:4:39
FUNCTION_DECL        mid            @ calls.c:6:12
  PARM_DECL            x              @ calls.c:6:20
  COMPOUND_STMT        <anon>         @ calls.c:6:23
    RETURN_STMT          <anon>         @ calls.c:7:5
      BINARY_OPERATOR      <anon>         @ calls.c:7:12
        CALL_EXPR            leaf_a         @ calls.c:7:12
          UNEXPOSED_EXPR       leaf_a         @ calls.c:7:12
            DECL_REF_EXPR        leaf_a         @ calls.c:7:12
          UNEXPOSED_EXPR       x              @ calls.c:7:19
            DECL_REF_EXPR        x              @ calls.c:7:19
        CALL_EXPR            leaf_b         @ calls.c:7:24
          UNEXPOSED_EXPR       leaf_b         @ calls.c:7:24
            DECL_REF_EXPR        leaf_b         @ calls.c:7:24
          UNEXPOSED_EXPR       x              @ calls.c:7:31
            DECL_REF_EXPR        x              @ calls.c:7:31
FUNCTION_DECL        recurse        @ calls.c:10:12
  PARM_DECL            n              @ calls.c:10:24
  COMPOUND_STMT        <anon>         @ calls.c:10:27
    IF_STMT              <anon>         @ calls.c:11:5
      BINARY_OPERATOR      <anon>         @ calls.c:11:9
        UNEXPOSED_EXPR       n              @ calls.c:11:9
          DECL_REF_EXPR        n              @ calls.c:11:9
        INTEGER_LITERAL      <anon>         @ calls.c:11:14
      RETURN_STMT          <anon>         @ calls.c:11:17
        INTEGER_LITERAL      <anon>         @ calls.c:11:24
    RETURN_STMT          <anon>         @ calls.c:12:5
      BINARY_OPERATOR      <anon>         @ calls.c:12:12
        UNEXPOSED_EXPR       n              @ calls.c:12:12
          DECL_REF_EXPR        n              @ calls.c:12:12
        CALL_EXPR            recurse        @ calls.c:12:16
          UNEXPOSED_EXPR       recurse        @ calls.c:12:16
            DECL_REF_EXPR        recurse        @ calls.c:12:16
          BINARY_OPERATOR      <anon>         @ calls.c:12:24
            UNEXPOSED_EXPR       n              @ calls.c:12:24
              DECL_REF_EXPR        n              @ calls.c:12:24
            INTEGER_LITERAL      <anon>         @ calls.c:12:28
FUNCTION_DECL        compute        @ calls.c:15:5
  PARM_DECL            x              @ calls.c:15:17
  COMPOUND_STMT        <anon>         @ calls.c:15:20
    DECL_STMT            <anon>         @ calls.c:16:5
      VAR_DECL             r              @ calls.c:16:9
        CALL_EXPR            mid            @ calls.c:16:13
          UNEXPOSED_EXPR       mid            @ calls.c:16:13
            DECL_REF_EXPR        mid            @ calls.c:16:13
          UNEXPOSED_EXPR       x              @ calls.c:16:17
            DECL_REF_EXPR        x              @ calls.c:16:17
    COMPOUND_ASSIGNMENT_OPERATOR <anon>         @ calls.c:17:5
      DECL_REF_EXPR        r              @ calls.c:17:5
      CALL_EXPR            recurse        @ calls.c:17:10
        UNEXPOSED_EXPR       recurse        @ calls.c:17:10
          DECL_REF_EXPR        recurse        @ calls.c:17:10
        UNEXPOSED_EXPR       x              @ calls.c:17:18
          DECL_REF_EXPR        x              @ calls.c:17:18
    RETURN_STMT          <anon>         @ calls.c:18:5
      UNEXPOSED_EXPR       r              @ calls.c:18:12
        DECL_REF_EXPR        r              @ calls.c:18:12
FUNCTION_DECL        main           @ calls.c:21:5
  COMPOUND_STMT        <anon>         @ calls.c:21:16
    CALL_EXPR            printf         @ calls.c:22:5
      UNEXPOSED_EXPR       printf         @ calls.c:22:5
        DECL_REF_EXPR        printf         @ calls.c:22:5
      UNEXPOSED_EXPR       <anon>         @ calls.c:22:12
        UNEXPOSED_EXPR       <anon>         @ calls.c:22:12
          STRING_LITERAL       "%d\n"         @ calls.c:22:12
      CALL_EXPR            compute        @ calls.c:22:20
        UNEXPOSED_EXPR       compute        @ calls.c:22:20
          DECL_REF_EXPR        compute        @ calls.c:22:20
        INTEGER_LITERAL      <anon>         @ calls.c:22:28
    RETURN_STMT          <anon>         @ calls.c:23:5
      INTEGER_LITERAL      <anon>         @ calls.c:23:12
```

The full tree is dense. Because `dump()` takes a kind filter, you can ask one question at a time. Pass a `CursorKind` name to see only matching nodes (recursion still descends, so indentation marks where each match sits):

```
python3 libclang-lab/scripts/p2_ast_dump.py CALL_EXPR
```

```
AST dump of calls.c (filter=CALL_EXPR):
        CALL_EXPR            leaf_a         @ calls.c:7:12
        CALL_EXPR            leaf_b         @ calls.c:7:24
        CALL_EXPR            recurse        @ calls.c:12:16
        CALL_EXPR            mid            @ calls.c:16:13
      CALL_EXPR            recurse        @ calls.c:17:10
    CALL_EXPR            printf         @ calls.c:22:5
      CALL_EXPR            compute        @ calls.c:22:20
```

That filtered list — every call site in `calls.c` — is the seed of the call-graph builder in Part 5. Notice `recurse` calling itself at 12:16 (the self-recursion) and `compute` nested inside the `printf` argument at 22:20.

---

## Checkpoint

| Concept | What You Proved |
|---------|-----------------|
| CursorKind | Selected nodes with `kind == CursorKind.X` and bucketed them with `kind.is_declaration()` / `is_expression()` / etc. |
| Names | Distinguished `spelling`, `displayname` (with signature), and `get_usr()` (stable, linkage-aware cross-TU id). |
| Locations & extents | Read `SourceLocation` (file/line/column/offset) and a function's `SourceRange` extent spanning its definition. |
| Main-file filtering | Isolated the 186 main-file nodes from the SDK-dependent header noise; used `in_main_file()` so headers never pollute output. |
| Decl vs definition | Found two same-spelling cursors for `shapes_total_area` and reached the body via `is_definition()` / `get_definition()` / `canonical`. |
| Tokens | Tokenized `shape_translate` into KEYWORD/IDENTIFIER/PUNCTUATION — the lexical view vs the syntactic cursor tree. |
| AST dumper | Built a reusable, main-file-filtered, indented tree printer with an optional kind filter and ran it on `calls.c`. |

---

[← Part 1 — Foundations](part_1_foundations.md) | [Part 3 — Types & Semantics →](part_3_types_semantics.md)

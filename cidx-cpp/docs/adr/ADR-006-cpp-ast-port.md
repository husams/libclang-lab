# ADR-006: C++ parity port of the `cidx ast` command group (M5)

Status: accepted
Date: 2026-06-18
Scope: M5 of the cidx AST-analysis feature ([[pages/planning/cidx-ast-analysis]]).
Pioneers the C++ read/query surface. No Python changes. Schema stays v13;
product version stays 0.3.0 (already bumped in M3 — do NOT touch).
Branch: `feat/cidx-cpp-ast`.

Reference implementation (mirror EXACTLY, byte-identical output):
`project/indexer/astcmd.py`, `project/indexer/astcache.py`, the `ast` subparser +
`_ast_common`/`_cache_toggle` in `project/indexer/cli.py:1699-1812`, and
`docs/adr/ADR-005-ast-cache.md` (the accepted cache design).

## Context — forces & constraints

`cidx-cpp` currently has **no read/query surface at all**: `graph`, `query.py`,
`model.py`, and the symbol-selector resolver are Python-only (memory:
[[cidx-graph-query-layer]] "C++ port pending"). M5 builds the first C++
read-side command group. It must reproduce three things byte-for-byte:

1. **The argparse tree** (`args.cpp` engine already does this for 13 commands —
   §2 extends it; the `ast` group adds the first command with *two* nesting
   levels: `ast {dump,locals,conditions,cache}` and `ast cache {build,status,clear}`).
2. **`json.dumps(indent=2)` output** — nested objects, arrays-of-ints expanded
   one-int-per-line, `null`, Python string escaping. The existing `json_min`
   is **strings-only / compact** (`src/util/json_min.hpp:1-9`) and cannot do
   this; a new emitter is required (§3). **This is the single biggest parity
   risk.**
3. **`CursorKind.name`** — Python emits e.g. `FUNCTION_DECL`, `COMPOUND_STMT`,
   `CASE_STMT`. libclang's `clang_getCursorKindSpelling` returns the DIFFERENT
   spelling `FunctionDecl`/`CompoundStmt`, and Python's name table is
   **hand-maintained with irregularities** (`121 StmtExpr`,
   `251 OMP_PARALLELFORSIMD_DIRECTIVE`) — no mechanical transform exists.
   The C++ side must embed the exact `int → name` table generated from the
   same libclang bindings (§5.4). **Second-biggest parity risk.**

The same-libclang pinning that `parity_check.sh` already does
(`CIDX_LIBCLANG_LIB` from CMakeCache exported to Python — `scripts/parity_check.sh:57-90`)
is essential: USRs, extents and the kind table are all libclang-version-specific.

The cache scheme is fixed by ADR-005: key `sha1(abspath \0 flags [\0drv\0 driver])`,
sidecar `{abspath, flags_hash, src_mtime, libclang_version}`, reparse-on-any-failure.
The two implementations must be **interchangeable on the same box** (a `.ast`
written by Python loads in C++ and vice-versa), so the key bytes and sidecar
fields must be identical down to the `\0` separators.

## Decision

Add a `clangx/ast_query.{hpp,cpp}` walker layer, an `astcache/astcache.{hpp,cpp}`
module, a `cli/json_out.{hpp,cpp}` pretty-emitter, a vendored SHA-1
(`util/hashing` extension), the `ast` argparse sub-tree in `args.{hpp,cpp}`, the
six handlers in `commands.{hpp,cpp}`, and the `run_command()` dispatch. Extend
`parity_check.sh` with an `ast` block and add `ast_query_test` /
`astcache_test` doctest suites. No Storage schema change; ADD read accessors
only where the Python resolver needs one the C++ Storage lacks (§4 — it turns
out **all needed accessors already exist**).

---

## 1. New + modified files

**New files**

| File | Purpose | Mirrors |
|---|---|---|
| `src/cli/json_out.hpp` / `.cpp` | `json.dumps(indent=2)` byte-replica builder (§3) | `json.dumps(..., indent=2)` |
| `src/clangx/ast_query.hpp` / `.cpp` | free-standing cursor walkers that descend into bodies; resolver helpers; cursor→json/text | `astcmd.py` `_file_cursors`/`_subtree`/`_cursor_json`/`_dump_text`/`_loc`/`_extent_dict` |
| `src/astcache/astcache.hpp` / `.cpp` | key/sidecar/version-guard/`load_or_parse`/cache subcmds | `astcache.py` |
| `src/util/sha1.h` / `sha1.c` (vendored) | public-domain SHA-1 (RFC 3174), same pattern as `util/md5/md5.h` | `hashlib.sha1` |
| `src/cli/kind_names.hpp` / `.cpp` | full `CXCursorKind → Python-enum-name` table (§5.4) | `clang.cindex` registered names |
| `tests/ast_query_test.cpp` | resolver + walkers + json_out + kind-table doctest | new |
| `tests/astcache_test.cpp` | cache key/sidecar/version-guard/load_or_parse (mirrors `test_astcache.py`) | `project/tests/test_astcache.py` |
| `scripts/gen_kind_names.py` | regenerates `kind_names.cpp` from the pinned `clang.cindex` (build-time guard, §5.4) | — |

**Modified files**

| File | Change |
|---|---|
| `src/cli/args.hpp` | `ParsedArgs`: add the `ast` fields (§2.1) |
| `src/cli/args.cpp` | `ast` usage strings + leaf specs + the `ast`/`ast cache` parse sub-tree (§2.2) |
| `src/cli/commands.hpp` / `.cpp` | six handlers + `run_command()` dispatch (§5, §7) |
| `src/clangx/libclang.hpp` | bind 8 missing C-API functions (§5.4 list) |
| `src/util/hashing.hpp` / `.cpp` | `sha1_hex(const std::string&)` + over a `(abspath,flags,driver)` shape |
| `src/cli/format.hpp` / `.cpp` | `group_thousands(int64_t)` for cache `{:,}` sizes (§6.4) |
| `CMakeLists.txt` | add the 4 new `.cpp` + 2 vendored C sources to `cidx_core`; add `clang-c/Documentation`? no — only Index.h |
| `tests/CMakeLists.txt` | register `ast_query_test` + `astcache_test` (default + clang suites, §8) |
| `scripts/parity_check.sh` | append the `ast` parity block (§8) |

---

## 2. `ParsedArgs` + the `ast` parse sub-tree

### 2.1 `ParsedArgs` extensions (args.hpp:29-66)

Add after `op`:

```cpp
// -- ast command group (M5) ------------------------------------------------
// args.command=="ast"; args.what in {dump,locals,conditions,cache}.
std::string cache_action;            // ast cache {build,status,clear}
std::optional<std::string> ast_usr;  // --usr  (separate from delete's `usr`)
std::optional<int64_t> ast_id;       // --id   (separate from delete's del_id)
// `name` (existing field) is reused for --name; `kind` (existing) for --kind.
bool first = false;                  // --first
// `target` (existing) reused for the FILE|COMPONENT://PATH positional.
// `op` (existing REMAINDER vector) reused for the `-- <flags>` tail.
bool ast_json = false;               // --json
bool use_cache = true;               // --cache (default) / --no-cache
// dump-only
int depth = 0;                       // --depth (default 0 = unlimited)
bool tokens = false;                 // --tokens
bool types = false;                  // --types
// locals-only
bool params = false;                 // --params
// conditions-only
bool cond_ast = false;               // --ast
// `index_db` (existing) reused for --db (dest graph_db in Python; here just
// the index-path override, same as set/file/dump-cc).
```

> Reusing `name`/`kind`/`target`/`op`/`index_db` keeps the struct lean and
> matches how Python reuses the same `args` namespace. `--usr`/`--id` get
> dedicated fields because `delete` already binds `usr`/`del_id` with different
> types/semantics.

**Python `--db` mapping (subtle, must replicate):** in Python `_ast_common`
declares `--db dest=graph_db`, and `cli.py:1816-1819` sets
`args.index = index_path(); if graph_db: args.index = abspath(expanduser(graph_db))`.
The resolver then opens `Storage(args.index)`. In C++ the global `Context.index_path`
is already overridden by `parsed.index_db` in `main.cpp:62-64` for set/file/
dump-cc; route `ast`'s `--db` into the SAME `index_db` field so the existing
main.cpp override path applies unchanged. (`expanduser` is applied in main? —
no: Python `abspath(expanduser(...))`. main.cpp assigns `index_db` verbatim. For
ast `--db` parity, apply `pathutil::expanduser` + abspath in the handler when
opening Storage, OR in main.cpp. **Decision: do it in main.cpp's `index_db`
branch** — but only when `command=="ast"` to avoid changing set/file/dump-cc
behaviour. Keep it localized.)

### 2.2 Usage strings + leaf specs (args.cpp)

Transcribe the verbatim argparse output (capture with
`COLUMNS=80 python3 -m indexer ast -h`, `... ast dump -h`, etc., Python 3.14).
The `ast` group adds these `Spec`s and usage/help constants, following the
existing `show`/`list` two-level pattern plus a third level for `cache`:

```
kAstUsage / kAstHelp                 cidx ast {dump,locals,conditions,cache} ...
kAstDumpUsage / kAstDumpHelp         cidx ast dump   [--depth N][--tokens][--types]
                                       <_ast_common opts> [--cache|--no-cache] [target] [-- FLAGS]
kAstLocalsUsage / kAstLocalsHelp     cidx ast locals [--params] <common> [cache-toggle] [target] [--FLAGS]
kAstConditionsUsage / ...Help        cidx ast conditions [--ast] <common> [cache-toggle] [target] [--FLAGS]
kAstCacheUsage / kAstCacheHelp       cidx ast cache {build,status,clear} ...
kAstCacheBuildUsage/Help             cidx ast cache build  <_ast_common opts> [target] [-- FLAGS]
kAstCacheStatusUsage/Help            cidx ast cache status <_ast_common opts> [target] [-- FLAGS]
kAstCacheClearUsage/Help             cidx ast cache clear  <_ast_common opts> [target] [-- FLAGS]
```

**`_ast_common` opt set** (shared, declared in Python option-add order so error
text matches; the order also fixes the `usage` token order):

```cpp
// shared across dump/locals/conditions and cache build/status/clear.
// NOTE: REMAINDER (`rest=true` on the spec) + a single optional `target`
// positional, exactly like kFileSpec but the positional is nargs="?" (NOT
// required) because cache status/clear and a pure-selector dump have no target.
const std::vector<OptSpec> kAstCommonOpts = {
  {"--usr",  '\0', ValueKind::kString, "--usr",  nullptr, 0},
  {"--id",   '\0', ValueKind::kInt,    "--id",   nullptr, 0},
  {"--name", '\0', ValueKind::kString, "--name", nullptr, 0},
  {"--kind", '\0', ValueKind::kString, "--kind", &kSymbolKinds, 0},
  {"--first",'\0', ValueKind::kNone,   "--first",nullptr, 0},
  {"--db",   '\0', ValueKind::kString, "--db",   nullptr, 0},
  {"--json", '\0', ValueKind::kNone,   "--json", nullptr, 0},
};
```

**Per-command leaf specs** prepend the command-specific opts, then append
`kAstCommonOpts`, then (for dump/locals/conditions only) the cache toggle pair.
Crucial detail: the target positional must be **optional** (Python `nargs="?"`)
and the spec must set `remainder=true` so the `-- <flags>` tail is captured into
`st.rest`. The existing engine handles `nargs="?"` by listing `target` in
`positionals` but NOT in `required` (cf. kListComponentsSpec which has
`{"pattern"}` positional, empty `required`). REMAINDER + optional positional is a
**new combination** — verify the engine's REMAINDER guard
(`args.cpp:653`: `st.positionals.size() >= spec.positionals.size()`) fires
correctly when the optional positional is absent: with `spec.positionals={"target"}`
and no target given, the first `--`-or-flag token after options would be the
REMAINDER trigger. **This matches Python**: argparse REMAINDER with a preceding
optional positional consumes the target if a bare token appears, else the `--`
starts the remainder. Add a doctest pinning `ast dump -- -std=c11`
(target absent, flags present) and `ast dump foo.c -- -std=c11`.

#### The `--cache / --no-cache` toggle — a NEW engine concept

Python: `add_mutually_exclusive_group(); --cache store_true default=True;
--no-cache store_false dest=cache`. The result is `args.cache: bool` defaulting
True. The existing engine has store_true flags (default false) and mutex groups,
but **not a store_true with default=True paired with a store_false to the same
dest**. Model it as two `kNone` flags in the SAME mutex group, then resolve in
the handler:

```cpp
{"--cache",    '\0', ValueKind::kNone, "--cache",    nullptr, 2},
{"--no-cache", '\0', ValueKind::kNone, "--no-cache", nullptr, 2},
// ... in handler:
bool use_cache = !st.flags.count("--no-cache");  // default true; --cache is a no-op-but-allowed
```

The mutex group id `2` makes `--cache --no-cache` together fail with argparse's
"not allowed with argument" message (engine `args.cpp:692-699`). The group is
**not** required (`required_mutex` stays empty), so absence → default true.
`--cache` alone sets the `--cache` flag (harmless, ignored). This yields exactly
Python's three states. The `cache build/status/clear` specs OMIT this pair
(per ADR-005 §4: cache subcmds carry no toggle).

#### Dispatch (args.cpp `parse_args`)

Add an `else if (pa.command == "ast")` branch after `delete`:

```cpp
} else if (pa.command == "ast") {
  CommandScan what = scan_command(argv, i, extras);   // dump|locals|conditions|cache
  if (what.help) { pa.help_text = kAstHelp; return pa; }
  if (!what.command) fail(kAstUsage, "cidx ast", "the following arguments are required: what");
  if (!contains(kAstWhats, *what.command))            // {dump,locals,conditions,cache}
    fail(kAstUsage, "cidx ast", "argument what: invalid choice: '" + *what.command +
         "' (choose from " + join(kAstWhats, ", ") + ")");
  pa.what = *what.command;
  if (pa.what == "cache") {
    CommandScan act = scan_command(argv, what.next, extras);  // build|status|clear
    if (act.help) { pa.help_text = kAstCacheHelp; return pa; }
    if (!act.command) fail(kAstCacheUsage, "cidx ast cache", "the following arguments are required: cache_action");
    if (!contains(kAstCacheActions, *act.command))
      fail(kAstCacheUsage, "cidx ast cache", "argument cache_action: invalid choice: '" +
           *act.command + "' (choose from " + join(kAstCacheActions, ", ") + ")");
    pa.cache_action = *act.command;
    const Spec *spec = /* build/status/clear spec */;
    ParseState st = parse_leaf(*spec, argv, act.next, extras);
    if (st.help) { pa.help_text = /* leaf help */; return pa; }
    bind_ast_common(pa, st);   // target/op/usr/id/name/kind/first/json/db
  } else {
    const Spec *spec = /* dump/locals/conditions spec */;
    ParseState st = parse_leaf(*spec, argv, what.next, extras);
    if (st.help) { pa.help_text = /* leaf help */; return pa; }
    bind_ast_common(pa, st);
    pa.use_cache = (st.flags.count("--no-cache") == 0);
    if (pa.what == "dump") {
      pa.depth = int_value(st, "--depth", 0);
      pa.tokens = st.flags.count("--tokens") != 0;
      pa.types  = st.flags.count("--types")  != 0;
    } else if (pa.what == "locals") {
      pa.params = st.flags.count("--params") != 0;
    } else { // conditions
      pa.cond_ast = st.flags.count("--ast") != 0;
    }
  }
}
```

`bind_ast_common` fills target (`st.positionals.empty() ? "" : st.positionals[0]`),
`op = st.rest`, `ast_usr/ast_id/name/kind/first/ast_json` from the opts, and
`index_db` from `--db`.

The `kVersion` line at args.hpp:27 stays `"0.3.0"` — **do not bump**.

---

## 3. The JSON emitter (`cli/json_out`) — `json.dumps(indent=2)` byte-replica

`json_min` is strings-only/compact and rejected for this (it cannot nest or
pretty-print — `src/util/json_min.hpp:1-9`). New module exposes a small value
tree + a serializer that reproduces CPython `json.dumps(obj, indent=2)` exactly.

### Value model

```cpp
namespace cidx::json_out {
struct Value;
using Array  = std::vector<Value>;
using Member = std::pair<std::string, Value>;   // insertion-ordered
using Object = std::vector<Member>;             // NOT a map — preserves order
struct Value {
  enum class T { Null, Bool, Int, Str, Arr, Obj } t = T::Null;
  bool b=false; long long i=0; std::string s; Array a; Object o;
  static Value null();           static Value of(bool);
  static Value of(long long);    static Value of(std::string);
  static Value arr(Array);       static Value obj(Object);
};
std::string dumps_indent2(const Value &v);   // top-level entry; appends '\n'? NO — see below
}
```

### Exact formatting rules (verified against `/tmp/cidx-m5-goldens/`)

CPython `json.dumps(obj, indent=2)` with default separators
(`item_sep=","`, `kv_sep=": "` when indent is set — the item separator loses its
trailing space because each item goes on its own line):

1. **Indent**: 2 spaces per nesting level. The opening `[`/`{` stays on the
   current line; each element/member starts on a new line indented to
   `2*(depth+1)`; the closing `]`/`}` is on its own line at `2*depth`.
2. **Empty container**: `[]` and `{}` on one line (no inner newline). (Not hit by
   the goldens but required for `children`-less leaves where we OMIT the key
   instead — see §5.3 — and for `"calls": []` if it ever occurs.)
3. **Object member**: `"<key>": <value>`. Key/value separator is exactly `": "`.
4. **Item separator**: `,` immediately after a value, then newline (NO trailing
   space — this is why `indent` mode drops the `", "` space).
5. **`null`** for Null, lowercase `true`/`false`, integers bare.
6. **Arrays of ints expand one-per-line** — `"start": [\n  3,\n  1\n]` — confirmed
   in `dump_leaf_a.json` (`"start": [ 3, 1 ]` rendered as three lines). This falls
   straight out of rule 1 (no special "inline short arrays" — CPython never
   inlines under `indent`).
7. **Strings**: `"` + escaped body + `"`. Escaping = CPython `ensure_ascii=True`
   default:
   - `\"` `\\` `\b`(0x08) `\f`(0x0c) `\n` `\r` `\t` get the short escapes;
   - any other control char `< 0x20` → `\u00XX` (lowercase hex);
   - **non-ASCII (≥ 0x80) → `\uXXXX`** (surrogate-pair for ≥ 0x10000). USRs and
     spellings here are ASCII in practice, but implement the full rule so a UTF-8
     identifier never diverges. (Decode the UTF-8 byte stream to code points, emit
     `\uXXXX`.) Mirror `json_min`'s existing UTF-8 decode if present;
     `json_min.cpp` already round-trips `\uXXXX` on the read side — reuse that
     codec direction-reversed.
8. **No trailing whitespace** on any line. The top-level `print(json.dumps(...))`
   in Python adds exactly one `\n` after the closing bracket — the **handler**
   (not `dumps_indent2`) appends that newline, matching the other commands'
   `*ctx.out << ... << "\n"` convention. `dumps_indent2` returns the bracketed
   text with no trailing newline.

### Worked example — must reproduce `/tmp/cidx-m5-goldens/dump_leaf_a.json`

For the `leaf_a` FUNCTION_DECL the handler builds (in this member order):

```
Object{
  {"kind", Str("FUNCTION_DECL")},
  {"spelling", Str("leaf_a")},
  {"usr", Str("c:calls.c@F@leaf_a")},
  {"extent", Object{
     {"file", Str("calls.c")},
     {"start", Arr{Int(3), Int(1)}},
     {"end",   Arr{Int(3), Int(43)}}}},
  {"type", Str("int (int)")},        // present because --types
  {"children", Arr{ /* PARM_DECL x, COMPOUND_STMT{ RETURN_STMT } */ }}
}
```

`dumps_indent2` over `Arr{thatObject}` yields, byte-for-byte:

```
[
  {
    "kind": "FUNCTION_DECL",
    "spelling": "leaf_a",
    "usr": "c:calls.c@F@leaf_a",
    "extent": {
      "file": "calls.c",
      "start": [
        3,
        1
      ],
      "end": [
        3,
        43
      ]
    },
    "type": "int (int)",
    "children": [
      ...
    ]
  }
]
```

Member order is the construction order — `Object` is a `vector<Member>`, never a
sorted map (`json.dumps` does NOT sort unless `sort_keys=True`, which Python
never passes). The `type`/`children`/`tokens` keys are appended in the same
conditional order as `_cursor_json` (astcmd.py:254-271): `kind, spelling, usr,
extent`, then `type` iff `want_types`, then `tokens` iff `want_tokens`, then
`children` iff non-empty and within depth.

A doctest asserts `dumps_indent2(built_tree) + "\n" == read("/tmp/.../dump_leaf_a.json")`
— copy the three goldens into `tests/fixtures/m5/` so the unit test is hermetic
(no `/tmp` dependency), and keep `/tmp/cidx-m5-goldens` only for the parity gate.

---

## 4. Storage accessors + the fuzzy `--name` match

**All needed accessors already exist** (storage.hpp):
`lookup_symbol(usr)` :144, `lookup_symbol_by_id(id)` :145,
`search_symbols(pattern, kind)` :152-154, `get_component_by_name(name)` :67,
`get_file(abs_path)` :109, `get_file_by_id(id)` :110, `file_abs_path(id)` :111.
The `Symbol`/`File` records carry `qual_name`, `spelling`, `kind`, `usr`,
`file_id`, `decl_file_id`, `compile_options`, `driver` (records.hpp:40-68, 27-38).
**No net-new Storage method is required.** (This is why M5 is "pioneering the
read surface" yet cheap on the storage side — the index-time port already laid
the accessors down.)

**Fuzzy `--name` ordering (parity-critical for the disambiguation list).**
Python `search_symbols` (storage.py:1016-1037) builds
`LIKE '%' + '%'.join(seg for seg in pattern.split('::') if seg) + '%' ESCAPE '\'`
(escaping `%` and `_` per segment) and orders
`ORDER BY LENGTH(qual_name), qual_name`. The C++ `search_symbols`
(storage.cpp) is a documented byte-parity port (`fuzzy_match_test` pins it) — so
the C++ resolver gets the **same ordered list for free** by calling
`db.search_symbols(name, kind)`. The disambiguation block (astcmd.py:76-87)
prints `hits[:25]` and `"... and N more"` — replicate the slice and the line
format:

```
error: --name 'foo' matches 3 symbols; disambiguate with --usr/--id (or pass --first):
  #<id>  <kind padded to 14>  <qual_name or spelling>  [<usr>]
```

`kind:<14` is `format::ljust(kind, 14)`; the `!r` in the Python message
(`--name {args.name!r}`) is `format::py_repr(name)` (format.hpp:31). Mirror the
`(kind X)` suffix on the zero-match message exactly (astcmd.py:69-74).

---

## 5. Resolver + the three handlers + walkers (`clangx/ast_query`)

### 5.1 `resolve_target` (mirror astcmd.py:91-192)

```cpp
struct AstTarget {
  std::string abspath;
  std::vector<std::string> flags;
  std::optional<std::string> driver;
  std::optional<std::string> focus_usr;   // indexed targets
  std::optional<std::string> focus_name;  // ad-hoc spelling match
  std::optional<Symbol> symbol;
  bool whole_file() const { return !focus_usr && !focus_name; }
};
// Returns nullopt + sets rc; prints the SAME error strings to ctx.err.
std::optional<AstTarget> resolve_target(const ParsedArgs&, Context&, int& rc);
```

Replicate the exact decision tree, in order:

1. `adhoc_flags = args.op`; if `adhoc_flags[0] == "--"` drop it (Python
   astcmd.py:100-102 — argparse REMAINDER keeps the literal `--`).
2. **target present** (`!args.target.empty()`):
   - contains `"://"` → split component/rel, `get_component_by_name`, build
     `abspath = normpath(join(comp.path, lstrip(rel,'/')))` (use
     `pathutil::join`/`normpath`), `get_file(abspath)`; flags from
     `rec.compile_options`, driver `rec.driver`; focus = usr/name. Error strings:
     `no component named '<c>'`, `not in index database: <abspath>`.
   - else `abspath = abspath(expanduser(target))`:
     - if `adhoc_flags` non-empty → ad-hoc target with those flags (no index).
     - else open Storage, `get_file(abspath)` → indexed flags; if absent but
       file exists → the `warning: ... parsing with defaults` line + empty flags;
       else `error: no such file and not in index: <target>`, rc 1.
3. **no target** → require a selector (`--usr/--id/--name`), else the rc-2
   "need a symbol selector..." message. Then `_resolve_symbol` (§4), pick the
   symbol's `file_id ?? decl_file_id`; `get_file_by_id` + `file_abs_path`;
   focus_usr = sym.usr. Error strings copied verbatim.

`expanduser`/`abspath`/`normpath` — `pathutil` already has `expanduser`/`join`;
add `normpath`/`abspath` helpers if missing (check `pathutil.hpp`; the import
flow already abspaths paths elsewhere — reuse).

### 5.2 The parse entry (cache routing)

```cpp
ParsedTu* load_target_tu(const AstTarget& t, bool use_cache, Context&);
// delegates to astcache::load_or_parse(t, use_cache) — §6.
```

Because `ParsedTu` is move-only and owns its `CXIndex`, the cache returns it by
value (`std::optional<ParsedTu>`); `nullptr`/`nullopt` → handler returns 1.

### 5.3 Cursor walkers

`for_file_cursors(tu, abspath, fn)` — re-implement astcmd.py `_file_cursors`
(clang/ast.py:133-145) as a free function (the AstIndexer's private
`for_file_cursors` stops at function bodies — same semantics, but it's private
and tied to the indexer; expose a standalone twin in `ast_query`). It yields
top-level + nested cursors **in `abspath`**, NOT descending into function
bodies — used by `_find_focus` to locate the focus decl.

`subtree(cursor, fn)` — mirror `_subtree` (astcmd.py:224-235): pre-order,
descends FULLY into the subtree including bodies, yielding `(cursor, depth,
parent)`. Implemented with `clang_visitChildren` recursion; because the visitor
is C-ABI, collect children into a `std::vector<CXCursor>` per level (the existing
ast.cpp visitors already use this stash pattern) and recurse, tracking depth +
parent.

`find_focus(tu, t)` — mirror `_find_focus` (astcmd.py:214-221): walk
`for_file_cursors`; return the first cursor whose `get_usr()==focus_usr` (indexed)
or `spelling==focus_name` (ad-hoc). `clang_equalCursors`-free; compares strings.

**`cursor_json(c, depth, max_depth, want_tokens, want_types) -> json_out::Value`**
— mirror `_cursor_json` (astcmd.py:251-271) member-for-member (§3 worked
example). `extent_dict(c)` mirrors `_extent_dict` (astcmd.py:241-248):
`{file: basename(start.file) or null, start:[line,col], end:[line,col]}`. Reads
extent via `clang_getCursorExtent` → `clang_getRangeStart`/`getRangeEnd` →
`clang_getExpansionLocation` (the start file basename only; Python uses
`os.path.basename(sf.name)`). `spelling or None`, `usr or None`, `type` =
`type.spelling or None` when `want_types`.

**`dump_text(...)`** — mirror `_dump_text` (astcmd.py:274-289): the
`f"{indent}{kind:<26} {name}{typ}  @ {loc}"` line (kind ljust 26, `name or
"<anon>"`, ` : <type>` when types+type, `  @ basename:line:col`), the
` ` "` " tokens line when `--tokens`, recursion bounded by `max_depth`.

**`loc(c)`** — mirror `_loc` (astcmd.py:207-211): `basename(file):line:col` or
`<no-location>`.

### 5.4 `kind_name(CXCursorKind) -> const char*` — the irregular table

Python `CursorKind.name` comes from `clang.cindex`'s hand-registered names, NOT
`clang_getCursorKindSpelling` (which gives `FunctionDecl`, not `FUNCTION_DECL`).
The table has irregularities (`121 StmtExpr`, `251 OMP_PARALLELFORSIMD_DIRECTIVE`,
`264 OMP_TARGET_PARALLELFOR_DIRECTIVE`) — **no algorithm reproduces it**. Embed
the exact `int → name` map generated from the SAME pinned bindings:

- `scripts/gen_kind_names.py` enumerates `clang.cindex.CursorKind` and emits
  `kind_names.cpp` (a `switch` or sorted array on the int value). Committed
  output; the script is the regenerator + a build-time **drift guard**: a doctest
  (`ast_query_test`) asserts a spot-set (`8→FUNCTION_DECL`, `10→PARM_DECL`,
  `202→COMPOUND_STMT`, `203→CASE_STMT`, `214→RETURN_STMT`, `121→StmtExpr`) and
  the parity gate catches any real divergence over the corpus.
- Unknown int (future libclang adds a kind) → emit the raw int as a decimal
  string? No — Python would raise `ValueError` on an unregistered kind via
  `CursorKind.from_id`. In practice the dump only ever sees kinds the pinned
  bindings know. **Decision:** fall back to `clang_getCursorKindSpelling`-derived
  text is WRONG (different casing). Instead return `"<UNKNOWN_KIND_<n>>"` and let
  the parity gate flag it — it cannot occur with matched libclang, and a loud
  marker beats a silent mis-spelling. Document this.

**libclang functions to ADD to `libclang.hpp`** (all currently MISSING — verified
by grep): `clang_getCursorKindSpelling` (NOT used for names, but handy for
diagnostics), `clang_getRangeStart`, `clang_getRangeEnd`, `clang_hashCursor`
(for the conditions parent-map keying — §5.7), `clang_isExpression` (for
`_condition_child`), `clang_saveTranslationUnit`, `clang_createTranslationUnit`
(cache load/save — §6), and `clang_getTranslationUnitSpelling` (optional). Each
is a one-line inline wrapper following the existing pattern (libclang.hpp:67-379).

### 5.5 `cmd_ast_dump` (mirror cmd_dump, astcmd.py:295-329)

Resolve → load TU → `max_depth = depth>0 ? depth : -1` (unlimited).
- whole-file: roots = top-level children of `tu.cursor` whose
  `location.file.name == abspath` (astcmd.py:307-311 — note: matches the parse
  path string exactly, G24).
- else: `find_focus`; missing → `error: could not locate <sel> in <basename>`, 1.
- `--json`: `Arr{cursor_json(c,...) for c in roots}` → `dumps_indent2` → print
  `+ "\n"`. else `dump_text` each root.

### 5.6 `cmd_ast_locals` (mirror cmd_locals, astcmd.py:357-389)

`focus_function` (astcmd.py:332-354): whole_file → the "needs a function" error;
focus missing → locate error; focus kind ∉ `_FUNCTION_KINDS` → "is a <KIND>, not a
function". Then walk `subtree(focus)`, collect `VAR_DECL` (+ `PARM_DECL` when
`--params`) into rows `{name, type, kind:"param"|"local", loc}`. JSON via
`dumps_indent2`; text via the `f"  {tag:<6} {type or '?':<24} {name}  @ {loc}"`
lines + the `f"{spelling}: {n} variable(s)"` header. Verified against
`locals_badly.json` (A param @5:28, B param @5:35, Result local @6:9).

`_FUNCTION_KINDS` (clang/ast.py:122-130) = {FunctionDecl, CXXMethod, Constructor,
Destructor, FunctionTemplate} — a `CXCursorKind` set in C++.

### 5.7 `cmd_ast_conditions` (mirror cmd_conditions, astcmd.py:407-462)

The subtlest handler. Walk `subtree(focus)` building:
- `parent_of`: a map keyed by **`clang_hashCursor(c)`** (Python uses `c.hash`)
  → parent cursor. Add `clang_hashCursor` to the binding (§5.4).
- `calls`: every `CALL_EXPR` with a non-empty spelling, in walk order.

Then for each call, climb `parent_of` from the call until a cursor in
`_COND_KINDS` (clang/ast.py:382-392 = {IF, FOR, WHILE, DO, SWITCH, CASE,
CONDITIONAL_OPERATOR}) or the focus is reached. First guard found, dedup by
`hashCursor(guard)` in a `seen` set. For each unique guard:
- `cond = condition_child(guard)` — mirror `_condition_child` (astcmd.py:392-404):
  the **first child whose kind `is_expression()`** (Python `ch.kind.is_expression()`
  → `clang_isExpression(kind)` — but note: Python's `CursorKind.is_expression`
  checks `100 <= value < 200`; the C-API `clang_isExpression` does the same. Bind
  `clang_isExpression` and use it).
- `cond_toks` = space-joined token spellings over `cond`'s extent (tokenize like
  ast.cpp:646-669), or `""` when cond is null.
- `guarded` = sorted unique spellings of calls that are `guarded_by` this guard
  (mirror `_guarded_by` astcmd.py:465-473: climb the call's parents to see if the
  guard is an ancestor before reaching focus). Sort is Python `sorted(set(...))` =
  lexicographic on the spelling strings — use `std::set<std::string>`.
- row = `{control: kind_name(guard.kind), loc: loc(guard), condition: cond_toks,
  calls: [guarded...]}`; when `--ast` and cond present, add
  `condition_ast: cursor_json(cond, 0, -1, false, true)`.

JSON via `dumps_indent2`; text via the
`f"  {control:<20} @ {loc}" / "    cond: {condition}" / "    -> calls: {', '.join}"`
lines + the `f"{spelling}: {n} conditional(s) guarding calls"` header. Verified
against `conditions_shape_area.json` (one CASE_STMT @ shapes.c:14:9, condition
`SHAPE_CIRCLE`, calls `[circle_area]`).

> **Token-order subtlety**: Python joins `tok.spelling for tok in
> cond.get_tokens()`. The C-API `clang_tokenize` over the cond extent yields the
> same tokens in source order. For a single DECL_REF_EXPR like `SHAPE_CIRCLE` the
> extent covers just that identifier → one token `SHAPE_CIRCLE`. The golden
> confirms `"condition": "SHAPE_CIRCLE"`.

---

## 6. The C++ AST cache (`astcache/astcache`) — ADR-005 byte-parity

One-to-one with `astcache.py`. Same key bytes, same sidecar JSON, same
reparse-on-failure, same parse counter for tests.

| Python (`astcache.py`) | C++ (`astcache.cpp`) | Notes |
|---|---|---|
| `cache_dir()` | reuse `cli::resolve_cache_dir()` | already `$INDEXER_CACHE` or `~/.cache/cidx`, expanduser, NOT abspath (commands.cpp:277-282) — byte-identical |
| `files_dir()` | `join(resolve_cache_dir(), "files")` | |
| `flags_hash(t)` | `sha1_hex("\0".join(flags) [+ "\0drv\0"+driver])` | §6.1 |
| `cache_key(t)` | `sha1_hex(abspath + "\0" + "\0".join(flags) [+ "\0drv\0"+driver])` | §6.1 — bytes must match Python exactly |
| `libclang_version()` | `CxString(lib, clang_getClangVersion()).str()` | already read in libclang.cpp:47; expose a cached getter |
| `is_valid(t, side)` | same 5 checks, same order | src stat; flags_hash; src_mtime; libclang_version; abspath |
| `_read_sidecar` | parse the 4-field JSON | reuse a tiny reader (NOT json_min — needs floats/objects); a minimal hand parser or json_out-read |
| `_load_ast(path)` | `clang_createTranslationUnit(Index.create(), path)` wrapped → nullopt on failure | the version safety net |
| `_try_save(tu, ...)` | `clang_saveTranslationUnit(tu, ast_path, opts)` then write sidecar | save AST first, sidecar after; remove half-written .ast on failure |
| `load_or_parse(t, use_cache)` | identical control flow (ADR-005 §3) | returns `optional<ParsedTu>` |
| `cmd_build/status/clear` | §6.3 | |
| `_PARSE_COUNT`/`_parse_count`/`_reset_parse_count` | a process-static counter + accessors | testability hook |

### 6.1 Key bytes — the interchange contract

Python (`astcache.py:53-76`) feeds the SHA-1:
`abspath.encode()`, then `b"\0"`, then `"\0".join(flags).encode()`, then if
driver: `b"\0drv\0"` + `driver.encode()`. C++ must concatenate the **same byte
string** and SHA-1 it. `flags_hash` is the SAME minus the `abspath + "\0"`
prefix. A doctest pins a known (abspath, flags, driver) triple's hex against the
Python output (compute once with `python3 -c "import hashlib;..."` and freeze).
`sha1_hex` is the new vendored SHA-1 (RFC 3174), wired like md5
(`util/hashing.cpp:6` vendors `md5/md5.h`).

`src_mtime`: Python stores `os.stat().st_mtime` (a float, full sub-second
precision). C++ must serialize the **same float repr** in the sidecar so a
Python-written sidecar validates under C++ and vice-versa. **Risk**: Python
`json.dump` writes `1718700000.123456` (repr of the float); C++ must match
`json.dumps`'s float formatting (shortest round-trip repr, Python uses
`float.__repr__`). Use `clang`/`stat` `st_mtim` → `double` and format with
`%.17g`-then-shorten, OR — simpler and exact — store mtime as the raw value and
compare **numerically** with a tolerance of 0 by reading the sidecar's number
back as a double (both sides parse the JSON number to a `double` and compare
bit-equal). Since validity compares `sidecar.src_mtime != stat.st_mtime`, do the
comparison on parsed doubles, not on string repr — that sidesteps the
float-formatting parity problem for *validation*. For **interchange** (a
Python-written sidecar read by C++), C++ parses the JSON number to double and
compares to its own `stat` double — also fine. The only place repr matters is
the human-readable sidecar file content, which is not diffed by the parity gate
(only dump/locals/conditions stdout is). **Decision: compare mtime as double;
write mtime with a round-trip-safe `%.17g`.** Document that the sidecar text is
not part of the byte-parity contract — only the AST `.ast` interchange + the
command stdout are.

### 6.2 `load_or_parse` (ADR-005 §3) — control flow

`mkdir -p files_dir()`; `key=cache_key(t)`; `ast=key+".ast"`, `side=key+".json"`.
If `use_cache`: read sidecar → if valid && ast exists → `_load_ast` → return on
success (else fall through). `_reparse` (++counter, `Parser::parse`, catch
`ClangParseError` → print `error: <e>` to stderr, return nullopt). If parsed &&
`use_cache` → `_try_save`. Return the TU.

### 6.3 cache subcommands (mirror astcache.py:264-486)

- `cmd_build`: resolve (target required → resolver's rc-2 message if absent);
  force `_reparse` + `_try_save`; print `cached: <ast_path>  (<size> bytes)` with
  `<size>` thousands-grouped (§6.4), or the `warning: AST save failed` stderr line.
- `cmd_status`: per-target (full `is_valid`) or bulk (enumerate `*.json`,
  mtime+version check only, `valid(flags?)`/`STALE`/`STALE(ver)`/`orphan-sidecar`),
  `--json` shape EXACTLY as astcache.py:288-432 (`{entries:[{key,status,size,
  abspath}], total_entries, total_bytes}`; per-target
  `{key,present,valid,size,abspath}` / `{key,present:false,abspath}`). Text columns
  + the trailing `N entr(y|ies), B bytes total` + the bulk note line. Sizes
  thousands-grouped.
- `cmd_clear`: per-target remove `<key>.{ast,json}` (count+bytes), or clear-all
  over the dir. Failure-tolerant.

> `cache status/build/clear` **text** output is NOT in the golden set
> (`/tmp/cidx-m5-goldens` is dump/locals/conditions only). It IS still diffed by
> the parity gate `ast` block (§8), so build it to byte-match Python anyway —
> the thousands-grouping and column widths are the only fiddly parts.

### 6.4 `group_thousands(int64_t)` (format.hpp)

Python `f"{n:,}"` → `1,234,567`. Add to `cli/format`:
`std::string group_thousands(int64_t)`. Negative handled (sign then groups).
Pin with a doctest (`0→"0"`, `1234→"1,234"`, `1234567→"1,234,567"`).

---

## 7. `run_command()` dispatch wiring (commands.cpp:1393)

Add before the final `// list` fallthrough:

```cpp
if (args.command == "ast") {
  if (args.what == "dump")       return cmd_ast_dump(args, ctx);
  if (args.what == "locals")     return cmd_ast_locals(args, ctx);
  if (args.what == "conditions") return cmd_ast_conditions(args, ctx);
  // cache
  return cmd_ast_cache(args, ctx);   // dispatches on args.cache_action
}
```

`cmd_ast_cache` mirrors astcmd.py:476-488 (status/clear/build dispatch + the
`unknown cache action` rc-2 fallback). Handlers open `Storage` only when the
resolver needs it (COMPONENT:// or indexed target) — exactly as the resolver
already gates it; a pure ad-hoc `path -- <flags>` never opens the index.

Declarations added to commands.hpp:33-49.

---

## 8. Test plan

### 8.1 ctest unit suites (doctest)

**`ast_query_test.cpp`** (two registrations like `ast_test`: hermetic "default"
+ real-parse "clang"):
- *default*: `json_out::dumps_indent2` exact-string cases incl. the three
  goldens copied to `tests/fixtures/m5/`; the kind-name table spot-set + the
  `StmtExpr`/`OMP_PARALLELFORSIMD_DIRECTIVE` irregulars; `group_thousands`;
  the argparse sub-tree (every `ast`/`ast cache` usage/error/exit-2 path,
  `--cache`+`--no-cache` mutex error, options-before-target, `-- <flags>`
  REMAINDER with and without a target, no-prefix-abbreviation).
- *clang* (real libclang over `CIDX_MANIFESTS_DIR`): parse `calls.c` and assert
  `cmd_ast_dump --name leaf_a --depth 2 --types --json` == `dump_leaf_a.json`;
  `messy.c` `locals --name BadlyNamedFunction --params --json` ==
  `locals_badly.json`; `shapes.c` `conditions --name shape_area --json` ==
  `conditions_shape_area.json`. (Capture stdout via the `Context` stream seam.)

**`astcache_test.cpp`** (mirror `project/tests/test_astcache.py`, "clang" suite —
needs real parses; set `INDEXER_CACHE` to a temp dir so `~/.cache` is untouched):
1. cold miss parses once (counter==1, `.ast`+sidecar exist);
2. warm hit avoids reparse (counter unchanged);
3. `--no-cache` always reparses;
4. src-mtime bump invalidates (+1, sidecar rewritten);
5. different flags → different key → second entry;
6. **libclang-version mismatch** (poke the sidecar's `libclang_version`) →
   reparse, no crash;
7. corrupt `.ast` (truncate) + valid sidecar → `_load_ast` nullopt → reparse;
8. **interchange**: a sidecar/`.ast` pair the cache wrote loads back (round-trip);
9. `cache_key`/`flags_hash` hex == frozen Python value for a known triple.

### 8.2 `parity_check.sh` extension (the byte-identical Py↔C++ gate)

Append an `ast` block to `run_script` (after the M3 graphlab block, before the
`file`/delete blocks so the index state it relies on is intact). The harness
already pins both tools to the SAME libclang (`CIDX_LIBCLANG_LIB`→Python
`CIDX_LIBCLANG`; unset for C++ — `parity_check.sh:57-90,128-154`), which is
mandatory because AST USRs/extents/kind-table are version-specific. Use
**ad-hoc** targets (`-- -std=c11` / `-std=c++17`) so the block is independent of
the indexed fixture and matches the goldens' invocation convention (options
BEFORE the positional, `-- <flags>` last):

```bash
# --- M5: ast dump/locals/conditions (text + --json), ad-hoc + indexed ---
MAN="$LAB_ROOT/manifests"
run_one ... -- ast dump --name leaf_a --depth 2 --types "$MAN/calls.c" -- -std=c11
run_one ... -- ast dump --name leaf_a --depth 2 --types --json "$MAN/calls.c" -- -std=c11
run_one ... -- ast dump --tokens "$MAN/calls.c" -- -std=c11        # whole-file, tokens
run_one ... -- ast locals --name BadlyNamedFunction --params "$MAN/messy.c" -- -std=c11
run_one ... -- ast locals --name BadlyNamedFunction --params --json "$MAN/messy.c" -- -std=c11
run_one ... -- ast conditions --name shape_area "$MAN/shapes.c" -- -std=c11
run_one ... -- ast conditions --name shape_area --json "$MAN/shapes.c" -- -std=c11
run_one ... -- ast conditions --name shape_area --ast --json "$MAN/shapes.c" -- -std=c11
# error paths (exit codes + stderr byte-match): ambiguous --name, missing focus,
# non-function for locals, no selector + no target, bad --kind choice, --cache+--no-cache.
run_one ... -- ast locals --name nope "$MAN/messy.c" -- -std=c11
run_one ... -- ast dump --cache --no-cache "$MAN/calls.c" -- -std=c11
run_one ... -- ast dump -h
run_one ... -- ast cache -h
run_one ... -- ast            # required-what error
# --- M5: ast cache lifecycle (own INDEXER_CACHE per tool already isolates) ---
run_one ... -- ast cache status                 # empty (or post-build) listing
run_one ... -- ast cache build "$MAN/calls.c" -- -std=c11
run_one ... -- ast cache status "$MAN/calls.c" -- -std=c11
run_one ... -- ast cache clear "$MAN/calls.c" -- -std=c11
```

Two caveats the block must neutralize so the transcript diffs clean:
- `ast cache build/status` print **sizes** (`<N> bytes`). The `.ast` byte size is
  produced by the SAME libclang on the SAME source for both tools, so sizes are
  deterministic and equal — keep them. If they ever drift, add a `sed` to mask
  ` (NNN bytes)` → ` ({SZ} bytes)` like the existing `{TS}`/`{CACHE}` masks
  (`parity_check.sh:146-148`). **Decision: start unmasked (assert equality);
  mask only if a real size delta appears.**
- `ast cache status` keys are `sha1[:12]` of `abspath+flags+driver`; the abspath
  is identical for both tools (same `$MAN` paths), so the keys match. The
  per-tool `INDEXER_CACHE` differs but is already normalized to `{CACHE}` and the
  key is NOT the cache dir, so no masking needed.

Register nothing new in `tests/CMakeLists.txt` for parity (the existing
`parity_check` test already runs the whole `run_script`). Add the two doctest
exes to `CIDX_DEFAULT_TESTS`-style registration with the two-suite (default +
clang/SKIP-77) pattern used by `ast_test` (`tests/CMakeLists.txt:78-96`).

---

## 9. Risk list + sequencing

**Risks (highest first)**

1. **`json.dumps(indent=2)` byte-replica** (§3). Mitigation: build `json_out`
   first, unit-test it against the three goldens before any libclang work —
   that's the earliest end-to-end parity signal and isolates the formatter from
   the walker. The arrays-of-ints-one-per-line and `": "`/`,`-no-space rules are
   the easy-to-miss parts.
2. **`CursorKind.name` irregular table** (§5.4). Mitigation: generate it
   mechanically from the pinned `clang.cindex` (`gen_kind_names.py`), never
   hand-type it; drift-guard doctest + parity gate. Do NOT use
   `clang_getCursorKindSpelling` (wrong casing).
3. **Cache-key byte parity / interchange** (§6.1). Mitigation: freeze a Python
   hex for a known triple in a doctest; keep the `\0`/`\0drv\0` separators
   exact. Compare `src_mtime` as parsed doubles (sidestep float-repr parity).
4. **AST-file version pinning** (ADR-005 §2, [[cidx-toolchain-support]]):
   wheel-18 (mac) vs clang-21 (toolchain boxes). The sidecar version check +
   `_load_ast`-fails-→-reparse is the guard; the parity gate pins BOTH tools to
   ONE libclang so the gate itself is unaffected. Test #6 covers the skew path
   without a second clang.
5. **REMAINDER + optional positional** (§2.2): a new engine combination. Pin
   `ast dump -- -std=c11` (no target) and `ast dump foo.c -- -std=c11` in the
   argparse doctest before trusting it.
6. **`subtree` C-ABI recursion**: the visitor is `noexcept`; stash children per
   level (existing ast.cpp pattern) and recurse in C++ — don't recurse inside the
   callback. Reuse the established stash idiom.

**Sequencing (each step has an independent green signal)**

1. `json_out` + `group_thousands` + the kind-name table & generator →
   `ast_query_test` *default* suite green against the copied goldens. (No
   libclang.)
2. Bind the 8 missing libclang functions + vendor SHA-1; `sha1_hex` doctest
   (frozen Python hex).
3. `ast_query` walkers (`for_file_cursors`/`subtree`/`find_focus`/`cursor_json`/
   `dump_text`) + resolver → `ast_query_test` *clang* suite reproduces the three
   goldens. **First real end-to-end parity signal.**
4. `astcache` module + `astcache_test` (all 9 cases).
5. `args.cpp` `ast` sub-tree + `ParsedArgs` fields + argparse doctests.
6. Handlers + `run_command` dispatch + `commands.hpp` decls.
7. Extend `parity_check.sh`; run the `parity` gate → byte-identical Py↔C++ over
   the corpus. **Final acceptance.**

Build the heavy real-parse steps on pve01/pve02 if the local box is constrained
(global build-resource rule); the doctest *default* suites and `json_out` work
are hermetic and cheap locally.

## Alternatives considered

- **Extend `json_min` for nesting** instead of a new `json_out`. Rejected:
  `json_min` is contractually strings-only/compact (`json_min.hpp:1-9`,
  read-compat for `compile_options`); overloading it with a pretty nested mode
  would muddy that frozen contract and risk the existing parity. A separate
  builder is cleaner and independently testable.
- **Derive kind names from `clang_getCursorKindSpelling`** (algorithmic
  UPPER_SNAKE transform). Rejected: the C-API spelling is `FunctionDecl` and
  Python's table is irregular (`StmtExpr`, `OMP_PARALLELFORSIMD_DIRECTIVE`) — no
  transform reproduces it; only the embedded table is correct.
- **Key the cache by `file_id`** (ADR-005 option b). Rejected upstream
  (ADR-005 §"Alternatives"): ad-hoc files have no `file_id`, and `file_id`
  collides across flag sets. C++ inherits the uniform-hash decision unchanged.
- **Add Storage read accessors.** Considered, found unnecessary — every selector
  the resolver needs already exists (§4). (No viable alternative needed: the
  port reuses the index-time accessors.)

## References

- [[pages/planning/cidx-ast-analysis]] — authoritative spec (M5 + AST-cache).
- `docs/adr/ADR-005-ast-cache.md` — accepted cache design (mirrored here).
- Python reference: `project/indexer/astcmd.py`, `project/indexer/astcache.py`,
  `project/indexer/cli.py:1699-1819`, `project/indexer/clang/ast.py:122-145,382-403`,
  `project/indexer/storage.py:1016-1037` (fuzzy `--name`).
- C++ surfaces: `cidx-cpp/src/cli/args.{hpp,cpp}`, `commands.{hpp,cpp}:1393`,
  `storage/storage.hpp:67-162`, `storage/records.hpp:40-68`,
  `clangx/parse.hpp`, `clangx/ast.cpp:646-669` (tokenize idiom),
  `clangx/libclang.hpp:67-379`, `util/json_min.hpp`, `util/hashing.{hpp,cpp}:6`,
  `cli/format.hpp:22-31`, `scripts/parity_check.sh:57-90`, `tests/CMakeLists.txt:78-96`.
- Memory: [[cidx-python-cpp-parity]] (read-side exemption — but M5 deliberately
  achieves parity), [[cidx-version-bump-rule]], [[cidx-toolchain-support]]
  (version gotcha), [[cidx-graph-query-layer]] (the pending sibling C++ read port).

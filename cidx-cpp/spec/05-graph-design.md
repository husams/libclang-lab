# 05 — Graph Layer Design (schema v6 → v7)

Status: design-ready (libclang-grounded). Implements the locked plan in
`~/workspace/wiki/pages/planning/cidx-graph-layer.md` (cited below as PLAN §N).
All cursor facts in this document are confirmed by probe scripts under
`libclang-lab/scripts/probe_graph_*.py` — pasted output is reproduced inline.

Scope: both `cidx-cpp` (C++) and the Python `cidx` reference, kept at golden
parity (`parity_check.sh` dumps both DBs and diffs them). Schema text and every
new SQL statement must be byte-identical between `kSchema`/`_SCHEMA` and between
the two storage method bodies, exactly as the existing port maintains.

---

## 0. What is locked (do not re-litigate)

From the dispatch + PLAN §OQ resolutions:

1. Schema bump v6 → v7, **same SQLite file** (FK cascade requires it — PLAN §5).
2. Edge extraction runs **inside `cidx index`, ON by default**, with `--no-graph`
   to skip (PLAN §7).
3. **`cidx resolve`** = separate DB-only subcommand: flip `symbol.resolved`,
   finalize cross-repo edges, roll up `edge.count` (PLAN §4/§7).
4. **Stub-minting**: `INSERT OR IGNORE INTO symbol(usr, resolved) VALUES(:usr,0)`
   mints a stable id; the real def later UPDATEs the same USR row, sets
   `resolved=1`. `resolved` lives on `symbol`, never on `edge` (PLAN §4).
5. Tables: `edge_kind`, `edge`, `edge_site`, `template_param`, `template_arg`
   (PLAN §2/§6).

---

## 1. Confirmed cursor → edge mappings (probe evidence)

The walk facts below are the load-bearing inputs to the extraction model in §6.
Each is reproduced from a probe run (`cd ~/workspace/qemu-vms && python3
libclang-lab/scripts/probe_graph_<x>.py`).

### 1.1 Bodies are NOT walked today — `calls`/`uses` need a separate descent

`walk_visitor` returns `CXChildVisit_Continue` for function-like cursors
(`ast.cpp:185-187`), so statement-level cursors inside a body are never visited.
Probe `probe_graph_calls.py` reproduces the production `_file_cursors` logic and
counts `CALL_EXPR`:

```
== fact 1: production walk never visits CALL_EXPR ==
CALL_EXPR seen by production-style walk: 0
```

A **separate body descent** is therefore required for `calls` (and `uses`),
recursing INTO each defining function-like cursor, keyed by the enclosing
function's USR. The same descent feeds `edge_site.conditional`. Same probe,
body-descent enabled (calls.c):

```
total CALL_EXPR sites via body descent: 7
  L7:12 cond=0  leaf_a    caller=c:calls.c@F@mid     callee=c:calls.c@F@leaf_a
  L7:24 cond=0  leaf_b    caller=c:calls.c@F@mid     callee=c:calls.c@F@leaf_b
  L12:16 cond=0 recurse   caller=c:calls.c@F@recurse callee=c:calls.c@F@recurse
  L16:13 cond=0 mid       caller=c:@F@compute        callee=c:calls.c@F@mid
  L17:10 cond=0 recurse   caller=c:@F@compute        callee=c:calls.c@F@recurse
  L22:5  cond=0 printf    caller=c:@F@main           callee=c:@F@printf
  L22:20 cond=0 compute   caller=c:@F@main           callee=c:@F@compute
```

Ground-truth count for `calls.c`: **7 `calls` edge-sites**, collapsing to 7
distinct `(src,dst,calls)` edges (the self-recursive `recurse→recurse` is one
edge). `printf` is an unindexed (system-header) target → its dst USR
`c:@F@printf` becomes a **stub symbol** (§6).

### 1.2 `CALL_EXPR` → caller USR + callee USR, stable across unindexed TUs

`probe_graph_calls.py` above already shows callee USR is emitted via
`clang_getCursorReferenced` → `clang_getCursorUSR`. `probe_graph_xtu.py`
confirms the callee USR is emitted even when the callee's **definition lives in
a different, unindexed TU** (`app.c` calls `mathlib.c`'s functions):

```
== fact 2: app.c calls multiply()/add()/square(); defs are in mathlib.c ==
  call square     -> callee_usr=c:@F@square
  call add        -> callee_usr=c:@F@add
  call multiply   -> callee_usr=c:@F@multiply
  call printf     -> callee_usr=c:@F@printf
```

### 1.3 USR stability across TUs (fact 7)

```
== fact 7: USR stability — multiply()'s USR ==
  multiply DEF usr (mathlib.c TU): c:@F@multiply
  multiply CALL usr (app.c TU):    c:@F@multiply
  STABLE ACROSS TUs: True
```

This is what makes stub-minting correct: the call site in `app.c` mints
`c:@F@multiply` as a stub (resolved=0); indexing `mathlib.c` later UPDATEs the
same USR row to the real definition (resolved=1) — the edge needs no rewrite.

### 1.4 Inheritance — `CXX_BASE_SPECIFIER` (fact 3)

`probe_graph_cpp.py` on `geometry.hpp`:

```
== fact 3: CXX_BASE_SPECIFIER (inherits) ==
  derived=Circle base=Shape access=public virtual=0
     derived_usr=c:@N@geo@S@Circle
     base_usr   =c:@N@geo@S@Shape
```

Mapping: `edge(src=derived, dst=base, kind=inherits)`, `base_access` from
`clang_getCXXAccessSpecifier(base_specifier_cursor)`, `is_virtual` from
`clang_isVirtualBase(base_specifier_cursor)`.

**Gotcha (confirmed):** `clang_getCursorSemanticParent` on the
`CXX_BASE_SPECIFIER` cursor returns the **null/invalid** cursor in this binding
— the derived class must be taken from the **lexically enclosing record** during
the walk, NOT from the base-specifier's semantic parent. The base class is
`clang_getCursorReferenced(base_specifier)` (fallback
`clang_getCursorType → clang_getTypeDeclaration`).

### 1.5 Membership — `FIELD_DECL`/`CXX_METHOD` (fact 4)

```
== fact 4: FIELD_DECL/CXX_METHOD membership (dst=owning record) ==
  method_of area    owner=Shape   access=public    is_virtual=1
  method_of name    owner=Shape   access=public    is_virtual=0
  field_of  name_   owner=Shape   access=protected  is_virtual=0
  method_of area    owner=Circle  access=public    is_virtual=1
  field_of  radius_ owner=Circle  access=private    is_virtual=0
  method_of get     owner=Box     access=public    is_virtual=0
  field_of  value_  owner=Box     access=private    is_virtual=0
```

Mapping: `field_of` (FIELD_DECL) / `method_of` (CXX_METHOD/CONSTRUCTOR/
DESTRUCTOR), `src=member symbol`, `dst=owning record` via
`clang_getCursorSemanticParent`. Member access goes on **`symbol.access`** (the
indexer already sets this — `ast.cpp:330`), **not** on the edge (PLAN §1/§6).

### 1.6 Templates — params, `specializes`, `instantiates` (fact 5)

Primary-template params (`probe_graph_cpp.py`):

```
== fact 5a: CLASS_TEMPLATE params ==
  FUNCTION_TEMPLATE max_of: params=[('T','TEMPLATE_TYPE_PARAMETER')]
  CLASS_TEMPLATE   Box:     params=[('T','TEMPLATE_TYPE_PARAMETER')]
```

Instantiated types (`get_num_template_arguments`/`get_template_argument_type`):

```
== fact 5b: instantiations in geometry.cpp ==
  inst-type='const std::vector<double>' -> template-decl='vector'
     decl_kind=CLASS_DECL nargs=1 args=['double']
```

`specializes` + explicit instantiation (`probe_graph_spec.py`):

```
== SPECIALIZES: specialization decl -> primary template ==
  STRUCT_DECL 'Holder' ('Holder<int>')    specializes -> 'Holder'
     spec_usr=c:@S@Holder>#I   prim_usr=c:@ST>1#T@Holder
  STRUCT_DECL 'Holder' ('Holder<double>') specializes -> 'Holder'
     spec_usr=c:@S@Holder>#d   prim_usr=c:@ST>1#T@Holder
```

Mappings:
- `specializes`: `edge(src=specialization-decl, dst=primary-template)` via
  `clang_getSpecializedCursorTemplate`. Each specialization has a **distinct
  USR** (`...>#I`, `...>#d`) — safe for stub-minting and the edge UNIQUE key.
- `instantiates`: `edge(src=instantiated-type-decl, dst=primary-template)` — the
  `vector<double>` use-type → `vector<T>`. The type's `nargs`/arg types feed
  `template_arg` (PLAN §6).
- `template_param` rows come from the **primary template decl's**
  TEMPLATE_*_PARAMETER children; `template_arg` rows from the
  specialization/instantiation's template arguments.

### 1.7 Overrides (fact, supports edge_kind 6)

```
== OVERRIDES: Derived::f overrides Base::f ==
  Derived::f overrides [('f', 'c:@S@Base@F@f#')]   src_usr=c:@S@Derived@F@f#
```

Mapping: `edge(src=overriding-method, dst=overridden-method, kind=overrides)`
via `clang_getOverriddenCursors` (returns an owned array — must call
`clang_disposeOverriddenCursors`). `vtable_slot` is left NULL (no stable C-API
accessor; reserved column).

### 1.8 Conditional call detection (fact 6)

`probe_graph_spec.py` (`pick` calls itself inside an `if`):

```
== conditional CALL_EXPR (call inside IF_STMT) ==
  call pick cond=1 (caller=pick)
```

Detection: during body descent, maintain a `cond_depth` counter incremented when
entering `IF_STMT | FOR_STMT | WHILE_STMT | DO_STMT | SWITCH_STMT |
CASE_STMT | CONDITIONAL_OPERATOR`; a `CALL_EXPR` reached with `cond_depth>0` sets
`edge_site.conditional = 1`. (In `calls.c` no call is inside a branch, hence all
`cond=0`; the recursion's call is in the fallthrough statement, correctly 0.)

### 1.9 Mapping summary table

| CursorKind / API | edge_kind | src | dst | scalars set | site? |
|---|---|---|---|---|---|
| `CALL_EXPR` (body descent) | 1 `calls` | enclosing fn USR | `getCursorReferenced`→USR | — | yes (line/col/conditional/args_sig) |
| `CXX_BASE_SPECIFIER` | 2 `inherits` | enclosing record | `getCursorReferenced`→USR | `base_access`, `is_virtual` | no |
| NAMESPACE→child decl; record→nested type/enum/typedef | 3 `contains` | parent USR | child USR | — | no |
| spec decl (`getSpecializedCursorTemplate`) | 4 `specializes` | spec USR | primary USR | — | no |
| instantiated type (`get_num_template_arguments>0`) | 5 `instantiates` | inst-type decl USR | primary USR | — | no |
| `clang_getOverriddenCursors` | 6 `overrides` | overriding method | overridden method | `vtable_slot`(NULL) | no |
| reference site / type use (body descent + type) | 7 `uses` | enclosing fn USR | referenced symbol USR | — | yes |
| `FIELD_DECL` | 8 `field_of` | field USR | owning record USR | — | no |
| `CXX_METHOD`/`CONSTRUCTOR`/`DESTRUCTOR` | 9 `method_of` | method USR | owning record USR | — | no |

Confirmed counts on the manifests: `calls.c` → 7 `calls` edges; `geometry.hpp` →
1 `inherits` (Circle→Shape, public, non-virtual), 4 `method_of`, 3 `field_of`,
2 `template_param` (max_of T, Box T); `geometry.cpp` → ≥1 `instantiates`
(`vector<double>`→`vector`); synthetic spec probe → 2 `specializes`, 1
`overrides`.

---

## 2. v7 schema SQL (append to `kSchema` and `_SCHEMA`)

Appended to the end of `kSchema` (`storage.cpp`) **before** the `INSERT OR IGNORE
INTO meta` line, and identically into Python `_SCHEMA`. All `CREATE TABLE IF NOT
EXISTS` + `INSERT OR IGNORE` so the schema script is idempotent on both fresh and
migrated DBs (the existing pattern). The `schema_version` meta seed bumps to `7`.

```sql
-- ---- v7 graph layer (PLAN §2/§6) -----------------------------------------

CREATE TABLE IF NOT EXISTS edge_kind (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
INSERT OR IGNORE INTO edge_kind (id, name) VALUES
  (1,'calls'), (2,'inherits'), (3,'contains'), (4,'specializes'),
  (5,'instantiates'), (6,'overrides'), (7,'uses'),
  (8,'field_of'), (9,'method_of');

CREATE TABLE IF NOT EXISTS edge (
    id          INTEGER PRIMARY KEY,
    src_id      INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    dst_id      INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    kind        INTEGER NOT NULL REFERENCES edge_kind(id),
    count       INTEGER NOT NULL DEFAULT 1,
    base_access INTEGER,   -- inherits: public/protected/private of the base
    is_virtual  INTEGER,   -- inherits: virtual base
    vtable_slot INTEGER,   -- overrides: reserved (NULL today)
    UNIQUE (src_id, dst_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src_id, kind);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst_id, kind);

CREATE TABLE IF NOT EXISTS edge_site (
    edge_id     INTEGER NOT NULL REFERENCES edge(id) ON DELETE CASCADE,
    file_id     INTEGER REFERENCES file(id) ON DELETE SET NULL,
    line        INTEGER,
    col         INTEGER,
    conditional INTEGER NOT NULL DEFAULT 0,
    args_sig    TEXT,
    PRIMARY KEY (edge_id, file_id, line, col)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS template_param (
    owner_id    INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    param_kind  INTEGER NOT NULL,  -- 1=type 2=non-type 3=template-template 4=pack
    name        TEXT,
    default_txt TEXT,
    PRIMARY KEY (owner_id, position)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS template_arg (
    owner_id  INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    arg_kind  INTEGER NOT NULL,  -- 1=type 2=non-type value 3=template-template 4=pack
    ref_id    INTEGER REFERENCES symbol(id) ON DELETE SET NULL,
    literal   TEXT,
    PRIMARY KEY (owner_id, position)
) WITHOUT ROWID;
```

### 2.1 `edge_site.file_id` in the PK — a hazard

`edge_site` PK is `(edge_id, file_id, line, col)` and `file_id` is the only
nullable PK column. SQLite permits NULL in a `WITHOUT ROWID` PK column and treats
distinct NULLs as distinct, so two sites with NULL file_id at the same line/col
would both insert (no dedup). For `calls`/`uses` the `file_id` is always known
(the call site is in the file being indexed), so this is acceptable; the design
**requires** the extractor to always pass a real `file_id` for site rows. This is
noted as a parity hazard (§8). The collapsed `edge` is the dedup authority; sites
are detail.

---

## 3. `migrate()` v6 → v7

The new graph objects are **new tables**, not `ALTER`s on existing tables, so the
migration is "create-if-absent" and is in fact already performed by the schema
script (`CREATE TABLE IF NOT EXISTS` + `INSERT OR IGNORE`). The only extra work
`migrate()` does is bump `schema_version` from `'6'` to `'7'` on a pre-existing
DB so `cidx`/tools can detect the upgraded layout. Keep the established sequence
(`migrate()` BEFORE the schema script — G19).

### 3.1 Constant changes

- C++ `storage.hpp`: `constexpr int kSchemaVersion = 6;` → `= 7;`
- Python `storage.py`: `SCHEMA_VERSION = 6` → `7` (the f-string seed in `_SCHEMA`
  and the `migrate()` UPDATE both read it).

### 3.2 `migrate()` diff (C++)

The existing `migrate()` already ends with: if any v2–v6 column was added,
`changed=true` and it UPDATEs `schema_version` to `kSchemaVersion`. v7 adds **no
columns**, so for a v6 DB with all columns present, `changed` stays false and the
version would NOT bump. Add an explicit version-stamp step that is idempotent and
independent of column adds:

```cpp
// After the column-add block, before the closing `if (changed)`:
// v6 -> v7: the graph tables are created by the schema script (CREATE TABLE IF
// NOT EXISTS + INSERT OR IGNORE edge_kind). No symbol/file ALTER is needed, so
// detect the version from meta and bump it directly. Idempotent.
{
  auto st = db_.prepare("SELECT value FROM meta WHERE key = 'schema_version'");
  if (st.step()) {
    const std::string v = st.col_text(0);
    if (!v.empty() && v != std::to_string(kSchemaVersion)) {
      changed = true;   // forces the meta UPDATE below to write '7'
    }
  }
}
```

Then the existing `if (changed) { UPDATE meta ... }` writes `'7'`. (Equivalent
Python: read `schema_version` from `meta`; if present and != `str(SCHEMA_VERSION)`
set `changed = True`.) This keeps the meta bump correct whether the prior DB was
v2 (column adds also fire) or v6 (only the version stamp fires).

The migration is **destructive-free**: a v6 DB opened by a v7 binary gains the
empty graph tables and a bumped version; existing symbol/file/component rows are
untouched. Graph data appears only after the next `index` run (and `resolve`).

---

## 4. New `records.hpp` structs (+ Python dataclass parity)

C++ (`records.hpp`), value structs mirroring the column order, `id=-1` sentinel
like `Symbol`. Optional columns use `std::optional`.

```cpp
struct Edge {
    int64_t src_id = -1;
    int64_t dst_id = -1;
    int64_t kind = 0;                       // edge_kind.id
    int64_t count = 1;
    std::optional<int64_t> base_access;     // inherits
    std::optional<int64_t> is_virtual;      // inherits (0/1)
    std::optional<int64_t> vtable_slot;     // overrides (reserved)
    int64_t id = -1;
};

struct EdgeSite {
    int64_t edge_id = -1;
    std::optional<int64_t> file_id;
    std::optional<int64_t> line;
    std::optional<int64_t> col;
    int64_t conditional = 0;
    std::optional<std::string> args_sig;
};

struct TemplateParam {
    int64_t owner_id = -1;
    int64_t position = 0;
    int64_t param_kind = 0;
    std::optional<std::string> name;
    std::optional<std::string> default_txt;
};

struct TemplateArg {
    int64_t owner_id = -1;
    int64_t position = 0;
    int64_t arg_kind = 0;
    std::optional<int64_t> ref_id;
    std::optional<std::string> literal;
};
```

Python (`storage.py`) dataclasses, field order identical (defaults match):

```python
@dataclass
class Edge:
    src_id: int
    dst_id: int
    kind: int
    count: int = 1
    base_access: Optional[int] = None
    is_virtual: Optional[int] = None
    vtable_slot: Optional[int] = None
    id: Optional[int] = None

@dataclass
class EdgeSite:
    edge_id: int
    file_id: Optional[int] = None
    line: Optional[int] = None
    col: Optional[int] = None
    conditional: int = 0
    args_sig: Optional[str] = None

@dataclass
class TemplateParam:
    owner_id: int
    position: int
    param_kind: int
    name: Optional[str] = None
    default_txt: Optional[str] = None

@dataclass
class TemplateArg:
    owner_id: int
    position: int
    arg_kind: int
    ref_id: Optional[int] = None
    literal: Optional[str] = None
```

`Stats` gains `int64_t edges = 0;` / `"edges"` plus optional
`edges_by_kind` (a `SELECT k.name, COUNT(*) ... JOIN edge_kind` — mirror the
`symbols_by_kind` pattern) so `cidx` summaries and golden dumps cover edges.

---

## 5. New `Storage` methods

All keyed on integer `symbol.id` (never USR strings on the edge — PLAN §2). The
extractor resolves USR→id through the stub-mint helper before calling `add_edge`.

### 5.1 `mint_symbol_id(usr, spelling, qual_name, display_name, kind)` — stub-mint (PLAN §4)

```cpp
// Returns the id of the symbol with this USR, minting a stub (resolved=0) if
// absent. The reference cursor is always in hand at the call site, so its name
// AND kind travel with the USR: the stub is born NAMED and correctly typed (a
// defaulted-ctor stub is 'constructor', not the bare 'function' fallback). This
// is essential for targets whose definition is never indexed (stdlib calls,
// implicit template instantiations, defaulted ctors) -- no add_symbol ever
// backfills those, so the minted name/kind are all the graph will have. A
// repeat mint UPGRADES an unnamed stub (name+kind together) but never clobbers
// a real one; the follow-up SELECT returns the stable id either way.
int64_t Storage::mint_symbol_id(const std::string &usr,
                                const std::string &spelling = "",
                                const std::string &qual_name = "",
                                const std::string &display_name = "",
                                const std::string &kind = "function");
```

Body: `INSERT INTO symbol(usr, spelling, qual_name, display_name, kind,
resolved) VALUES(?,?,?,?,?,0) ON CONFLICT(usr) DO UPDATE SET kind = CASE WHEN
symbol.spelling='' THEN excluded.kind ELSE symbol.kind END, spelling = CASE WHEN
symbol.spelling='' THEN excluded.spelling ELSE symbol.spelling END, qual_name =
COALESCE(symbol.qual_name, excluded.qual_name), display_name =
COALESCE(symbol.display_name, excluded.display_name)` then `SELECT id FROM
symbol WHERE usr=?`. Empty `qual_name`/`display_name` bind as SQL NULL. `kind`
upgrades in lockstep with `spelling` (an empty spelling marks a not-yet-named
stub), so a real symbol's kind is never overwritten.

The four call sites (`calls`, `inherits`, `overrides`, `specializes`) each hold
a libclang cursor (`ref`/`base_ref`/`overridden[i]`/`primary`) and pass its
spelling + `qualified_name(cursor)` + display name + `stub_kind(cursor)`
(`kind_name(kind)` with a `"function"` fallback; mirrors Python's
`_KIND_MAP.get(k, "function")`). In Python, cursors from the C-array
`clang_getOverriddenCursors` lack the binding's `_tu` backref, so the helper
attaches it and `_qualified_name` swallows `AttributeError` defensively.

> **Schema CHECK hazard (must resolve before coding):** `symbol.spelling` is
> `NOT NULL` and `symbol.kind` has a CHECK constraint over the 17 kinds. A stub
> minted from a bare USR has no known spelling/kind. The locked PLAN text shows
> `INSERT OR IGNORE INTO symbol(usr, resolved)` which would violate `spelling
> NOT NULL`. **Decision for this design:** mint with `spelling=''` and a
> sentinel `kind`. `''` satisfies NOT NULL; `kind` must be a CHECK-valid value.
> Use **`'function'`** as the stub sentinel (callees/most stub targets are
> functions) — OR (preferred, parity-safe) add no new kind and rely on the real
> def's upsert to overwrite `kind` via `excluded.kind` (it always does —
> `storage.cpp:889`). The stub's wrong kind is transient. This is the single
> open schema question flagged in §8; it does **not** require an ALTER.

### 5.2 `add_edge(edge)` — UNIQUE upsert + count increment

```cpp
int64_t Storage::add_edge(const Edge &e);
```

```sql
INSERT INTO edge (src_id, dst_id, kind, count, base_access, is_virtual,
                  vtable_slot)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(src_id, dst_id, kind) DO UPDATE SET
  count       = edge.count + excluded.count,
  base_access = COALESCE(excluded.base_access, edge.base_access),
  is_virtual  = COALESCE(excluded.is_virtual,  edge.is_virtual),
  vtable_slot = COALESCE(excluded.vtable_slot, edge.vtable_slot)
RETURNING id
```

`count` accumulates so re-indexing one TU twice does **not** double-count when
combined with the resolve-time rollup (§7) — but note: the per-TU extractor
should pass `count=1` per **distinct** collapsed edge it emits, with the true
site count derived from `edge_site` at resolve time. To keep `add_edge`
idempotent under re-index, the extractor deletes this TU's edges first (§6.3).

### 5.3 `add_edge_site(site)`

```sql
INSERT OR IGNORE INTO edge_site (edge_id, file_id, line, col, conditional,
                                 args_sig)
VALUES (?, ?, ?, ?, ?, ?)
```

`OR IGNORE`: the same call site visited twice (e.g. header re-walk) is one row.

### 5.4 `add_template_param(p)` / `add_template_arg(a)`

```sql
INSERT OR REPLACE INTO template_param
  (owner_id, position, param_kind, name, default_txt) VALUES (?,?,?,?,?);
INSERT OR REPLACE INTO template_arg
  (owner_id, position, arg_kind, ref_id, literal)     VALUES (?,?,?,?,?);
```

`OR REPLACE` on the `(owner_id, position)` PK — re-indexing the same template
overwrites in place (idempotent), parity-safe.

### 5.5 Resolve-pass queries (DB-only, §7)

```cpp
// Roll edge.count up to the true number of sites (calls/uses), once, post-index.
void Storage::rollup_edge_counts();
// Edges whose ends live in different components (the cross-repo set).
std::vector<Edge> Storage::cross_repo_edges();
// Symbols still stub (resolved=0 and no definition row) — for the report.
// (Reuse existing unresolved_symbols(); add a "stub" notion = spelling=='' .)
```

`rollup_edge_counts()`:
```sql
UPDATE edge SET count = (
  SELECT COUNT(*) FROM edge_site WHERE edge_site.edge_id = edge.id
)
WHERE kind IN (1, 7)            -- calls, uses (only these carry sites)
  AND EXISTS (SELECT 1 FROM edge_site WHERE edge_site.edge_id = edge.id);
```

`cross_repo_edges()` (a join, no special storage — PLAN §4):
```sql
SELECT e.* FROM edge e
  JOIN symbol s1 ON s1.id = e.src_id
  JOIN symbol s2 ON s2.id = e.dst_id
  JOIN file f1 ON f1.id = s1.file_id  JOIN directory d1 ON d1.id=f1.directory_id
  JOIN file f2 ON f2.id = s2.file_id  JOIN directory d2 ON d2.id=f2.directory_id
WHERE d1.component_id <> d2.component_id;
```

Read helpers for `cidx show` / tooling (mirror `kSymbolCols` explicit lists):
`edges_from(src_id, kind?)`, `edges_to(dst_id, kind?)`, `edge_sites(edge_id)`,
`template_params(owner_id)`, `template_args(owner_id)`.

---

## 6. Per-TU extraction data model

The graph extraction is an **extension of the existing single AST walk** (PLAN
§7) — no second parse. It runs in `AstIndexer` alongside `index_symbols` /
`index_headers`, inside the **same one-transaction-per-file** envelope
(`index_file` already opens `db_.transaction()` — `ast.cpp:359`). Gated by a
member flag set from `--no-graph`.

### 6.1 Two passes over the one AST, one transaction

`index_file` currently does pass A (symbols). Add pass B (edges) **within the
same `Transaction`**, after symbols are stored so that USR→id lookups for
in-file symbols hit real rows before stubs are minted:

```
Transaction txn = db_.transaction();
  // Pass A: symbols (unchanged) — for_file_cursors → to_symbol → store
  // Pass B: edges (new, only if graph_enabled_)
  //   B1. declaration-level edges from the SAME filtered cursor stream:
  //       inherits, contains, field_of, method_of, specializes,
  //       instantiates, overrides, template_param, template_arg
  //   B2. body descent (separate recursion INTO function-like definitions
  //       located in `filename`): calls, uses + edge_site rows
txn.commit();
```

B1 reuses `for_file_cursors` (same main-file filter). B2 needs a **new**
descent because `for_file_cursors` deliberately prunes bodies (§1.1); it
recurses through ALL children of each defining function-like cursor (no
file-filter inside the body — locals are fine; only the *enclosing* fn must be
in `filename`).

### 6.2 USR-keyed accumulation, then mint-on-flush

The extractor works in USR space, then resolves to ids at write time:

1. For each discovered relation, compute `src_usr` and `dst_usr` (both via
   `clang_getCursorUSR`, dst via `clang_getCursorReferenced` /
   `clang_getSpecializedCursorTemplate` / `clang_getOverriddenCursors` first).
2. Resolve each USR to an id: `src_id` is almost always an in-file symbol just
   stored in pass A (real row); `dst_id` may be unindexed → `mint_symbol_id`
   mints a stub (resolved=0) and returns its stable id (§1.2/§1.3 prove the USR
   matches the eventual def).
3. `add_edge(src_id, dst_id, kind, ...)`; for `calls`/`uses` also
   `add_edge_site(edge_id, file_id, line, col, conditional, args_sig)`.
4. Template owners (specializations/instantiations/primary decls) get
   `template_param`/`template_arg` rows keyed by the owner's id.

Minting happens lazily at step 2 — never a separate staging table (PLAN §4).
Because src and dst are both minted/looked-up inside the file transaction, a
crash mid-file rolls back symbols AND edges together (the ACID guarantee, PLAN
§2/§5).

### 6.3 Idempotent re-index of one file

Re-indexing a changed file must not leave stale or doubled edges. Before pass B,
**delete this file's outgoing edges**: every edge whose `src_id` is a symbol
defined in `file_id`. Edge rows cascade-delete their `edge_site` rows
(`ON DELETE CASCADE`). Stub dst rows are left (other edges may point at them;
they're harmless and get resolved later).

```sql
DELETE FROM edge WHERE src_id IN (SELECT id FROM symbol WHERE file_id = :fid);
```

This makes a per-file re-index deterministic and keeps `edge.count` honest
(re-added with count=1 per distinct edge, rolled up at resolve time). Document
this as the edge analogue of the symbol upsert's "definition wins" rule.

### 6.4 Header handling

`index_headers` calls `index_file` per included header (`ast.cpp:425`), so pass
B runs for header files too — `inherits`/`method_of`/`field_of`/`contains` and
template rows for class decls that live in headers are captured there. Body
descent for inline methods defined in a header also runs (the enclosing method
cursor is in the header file). No special-casing needed.

### 6.5 New `LibClang` facade methods required

The facade (`libclang.hpp`) currently wraps only the symbol-walk subset. The
vendored `third_party/clang-c/Index.h` already **declares** every API below
(grep-confirmed at the cited lines) so they link with no header changes; each
needs a thin forwarding method added to `LibClang` (matching the existing
inline-forward style) so call sites read `lib.clang_xxx(...)`:

| C API (Index.h line) | used for |
|---|---|
| `clang_getCursorReferenced` (4079) | calls/uses/inherits dst cursor |
| `clang_isVirtualBase` (3700) | inherits.is_virtual |
| `clang_CXXMethod_isVirtual` (4462) | (optional) virtual-method flagging |
| `clang_getSpecializedCursorTemplate` (4623) | specializes dst |
| `clang_getOverriddenCursors` / `clang_disposeOverriddenCursors` (2717) | overrides |
| `clang_Cursor_getNumTemplateArguments` (3125) | template_arg count |
| `clang_Cursor_getTemplateArgumentKind` (3145) | template_arg.arg_kind |
| `clang_Cursor_getTemplateArgumentType` (3165) | template_arg type → ref_id |
| `clang_Cursor_getTemplateArgumentValue` (3186) | non-type template_arg.literal |
| `clang_getTypeDeclaration` / `clang_Type_getNumTemplateArguments` / `clang_Type_getTemplateArgumentAsType` (3685) | instantiates from a use-type |
| `clang_getCanonicalCursor` (4141) | dedup canonical decl (optional) |
| `clang_getNullCursor` / `clang_Cursor_isNull` | guard null returns (base/spec/override) |

`clang_getCXXAccessSpecifier` (inherits.base_access) is **already wrapped**
(`libclang.hpp:181`). The `getOverriddenCursors` array must be released with
`clang_disposeOverriddenCursors` (RAII wrapper recommended, mirroring `CxString`).

---

## 7. `cidx resolve` algorithm (DB-only, no parse)

New subcommand (PLAN §7), mirroring the Rust cpp-indexer's
`cxg-resolve-cross-repo` split ([[pages/code/cpp-indexer]] Phase 5
`EXTERNAL_REF`; [[pages/planning/codexgraph-cpp-libclang-rust]]). It re-reads
nothing from source — it is a sequence of SQL passes on the existing DB, run as
one transaction:

1. **Flip `symbol.resolved`.** Stub rows whose USR now matches a real definition
   are *already* resolved in place by the symbol upsert during indexing (the real
   def UPDATEs the same USR row, setting `resolved=1` via
   `MAX(excluded.resolved, symbol.resolved)` — `storage.cpp:909`). `resolve`
   needs no rewrite of edges (ids are stable — PLAN §4). The pass only
   *reports* remaining stubs (`spelling=''` AND `resolved=0` → never-defined
   externals like `printf`), and leaves them as legitimate external nodes.
2. **Finalize cross-repo edges.** No mutation required — a cross-repo edge is
   `cross_repo_edges()` (the component-mismatch join, §5.5). `resolve` counts
   and reports them; optionally stamps `meta('graph_resolved_at', now)`.
3. **Roll up `edge.count`.** `rollup_edge_counts()` (§5.5): for `calls`/`uses`,
   set `count = COUNT(edge_site)` so "how many call sites" is exact after
   multi-TU indexing (a callee called from N TUs accumulates N site rows; the
   collapsed edge's count reflects them).
4. Print a summary (`resolved: X, still-stub: Y, cross-repo edges: Z, rolled
   up: W edges`) — parity-frozen line, same in Python and C++.

`resolve` is **idempotent**: re-running recomputes counts from `edge_site` (not
`+=`) and re-derives the cross-repo set by join. Rebuilding the whole graph
without re-parsing = `DELETE FROM edge;` then `resolve` (PLAN §5/§7) — though
counts then need a re-index because `edge_site` cascades away with the edges; for
a count-only refresh, run `resolve` alone.

### 7.1 CLI wiring (parity with existing argparse tree)

- `args.hpp` `ParsedArgs`: add `bool no_graph = false;` (for `index`) and accept
  a new top-level command `"resolve"`. Update the hand-rolled `parse_args`
  grammar + the verbatim usage/help text (D6) and the Python argparse tree in
  lockstep (golden tests capture both).
- `commands.cpp`: `cmd_index` sets `indexer`'s graph flag from `!args.no_graph`;
  add `cmd_resolve(args, ctx)` and a `run_command` branch
  `if (args.command == "resolve") return cmd_resolve(...)`. Python `cli.py`
  mirrors exactly.

---

## 8. Open risks / parity hazards

1. **Stub-mint vs `spelling NOT NULL` + `kind` CHECK (highest).** PLAN's
   `INSERT OR IGNORE INTO symbol(usr, resolved)` cannot satisfy `spelling NOT
   NULL` and the `kind` CHECK. Resolved in this design by minting
   `spelling=''`, `kind='function'` (CHECK-valid sentinel), relying on the real
   def's upsert to overwrite `kind`/`spelling`. Must be implemented identically
   in both languages and pinned by a smoke test, or DBs diverge. No ALTER needed.
2. **Body-descent parity (high).** The new B2 recursion is *additional* AST
   traversal not present in the frozen `_file_cursors`. The C++ descent and the
   Python descent must visit children in the same order and apply the same
   `cond_depth` rule, or `edge`/`edge_site` row sets differ and
   `parity_check.sh` fails. Recommend a shared, tightly-specified descent
   contract + a dedicated smoke test on `calls.c` asserting the 7 sites above.
3. **`edge_site` NULL-file_id PK non-dedup (medium).** `(edge_id, file_id,
   line, col)` with nullable `file_id` won't dedup when `file_id` is NULL.
   Mitigation: extractor MUST pass a real `file_id` for every site (always
   available for call/use sites). Enforce in code; document the invariant.
4. **`instantiates` over-generation from system headers (medium).** The
   `vector<double>` probe also surfaced `__wrap_iter`, `basic_string`, etc. from
   libc++ internals. With default `INDEXER_IGNORE_SYSTEM_HEADERS=true`, src
   symbols in system headers are not indexed, so most of these never get a real
   `src_id` — but the *use-type* instantiation is encountered in user code.
   Decision: only emit `instantiates` when the instantiated-type **declaration**
   resolves to an indexed (non-system) or already-present symbol; otherwise the
   dst still mints a stub but the edge is legitimate (cross-repo to an external
   component). Pin behavior with a test to avoid Py/C++ drift in the filter.
5. **`overrides` array lifetime (low).** `clang_getOverriddenCursors` returns an
   owned array; forgetting `clang_disposeOverriddenCursors` leaks. Wrap in RAII
   (mirror `CxString`).
6. **`count` semantics during incremental re-index (low).** `add_edge`'s
   `count = count + excluded.count` plus the per-file edge delete (§6.3) plus the
   resolve-time rollup must compose to an exact site count. The rollup
   (`count = COUNT(edge_site)`) is the source of truth; `add_edge`'s increment is
   only a pre-resolve estimate. Document that `cidx resolve` is required for
   accurate counts.
7. **Schema-version detection on v6 DBs (low).** v7 adds no columns, so the
   existing `changed`-gated meta bump would skip the version stamp; §3.2 adds an
   explicit version-compare to force it. Without it, a v6→v7 DB keeps reporting
   `schema_version='6'` despite having the graph tables.

---

## 9. Cross-references

- PLAN: `~/workspace/wiki/pages/planning/cidx-graph-layer.md` (§1 edge taxonomy,
  §2 schema, §4 stub-mint, §5 same-file, §6 template tables, §7 tooling split).
- Resolve-split prior art: [[pages/code/cpp-indexer]] (Phase 5 `EXTERNAL_REF`,
  `cxg-resolve-cross-repo`), [[pages/planning/codexgraph-cpp-libclang-rust]].
- Compact storage rationale (integer ids, 1-byte kind):
  [[pages/planning/compact-cpp-graph-storage]].
- Current code: `storage.cpp`/`storage.hpp`/`records.hpp`,
  `clangx/ast.cpp`/`ast.hpp`, `clangx/libclang.hpp`, `cli/commands.cpp`,
  `cli/args.hpp`; Python `project/indexer/storage.py`,
  `project/indexer/clang/ast.py`.
- Probes (evidence): `libclang-lab/scripts/probe_graph_calls.py`,
  `probe_graph_xtu.py`, `probe_graph_cpp.py`, `probe_graph_spec.py`.
```

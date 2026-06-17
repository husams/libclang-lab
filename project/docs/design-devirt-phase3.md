# cidx Devirtualized Callgraph — Phase 3 Technical Design

Status: design (senior engineer), 2026-06-17 — **the Developer's contract**
Branch: `feat/devirt-callgraph-phase3`
Scope: **Phase 3** — close the two precision gaps ADR-003 accepted:
* **3a** value-typed member / global / by-value-return receivers & args narrow Γ
  to the EXACT singleton `{static-type USR}` (additive boolean columns, schema
  **v10 → v11**, Py/C++ parity).
* **3b** cross-TU param Γ via the existing `resolve` roll-up, gated behind a new
  `assume_closed_world` kwarg (default **False** → byte-identical/sound;
  pure query/model, Python-only, NO storage change).

Accepted decision: `project/docs/adr-003-devirt-phase3-precision.md`.
Spec (blackboard): `~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md`
§ "Phase 3 — Architecture Decision (2026-06-17)".
Phase-2 contract this mirrors: `project/docs/design-devirt-phase2.md`.
Phase-2 storage ADR: `project/docs/adr-002-devirt-phase2-provenance-storage.md`.

All code anchors are against `main` @ `21c204a` (schema v10).

> **Senior-engineer finding (carried to the contract, does NOT change course).**
> ADR-003 §3a.1 says `call_result.type_usr` is "today NULL". It is **not**:
> `_classify_value_source` already returns `type_usr = _record_usr_of_type(peeled.type)`
> for the `call_result` branch (`ast.py:631-633`), and `_body_descent` already
> stores it in both `call_arg.type_usr` and `edge_site.recv_type_usr`
> (`ast.py:810-837`). The real gap is purely engine-side: `_resolve_source`
> returns `TOP` for `call_result` unconditionally (`model.py:626-635`). Phase 3
> therefore adds **only** the value flag + the engine seeding; it does **not**
> need to add any new `type_usr` population for `call_result`. Flagged to
> team-lead 2026-06-17.

---

## 0. Phase 3 contract (firm)

1. **Sound, monotone.** 3a and 3b only ever *shrink* the candidate set Phase 1
   produced (a subset of Phase 2's already-sound output). Every information-poor
   branch returns `KEEP_ALL` (TOP receiver, non-value location, unprunable site,
   empty intersection, k-limit, unknown/absent caller, in-flight cycle).
2. **Defaults byte-identical.** `devirtualized_callgraph(prune=False)` is
   unchanged. With `prune=True`, `assume_closed_world=False` (the default) the
   output is **byte-identical to Phase 2** — 3b never fires. 3a fires under
   `prune=True` because it only narrows on *provably exact* value locations,
   which Phase 2 left at ⊤; this is the intended precision gain, gated by
   `prune=True` (still opt-in, default OFF).
3. **Parity.** Storage + extraction (`storage.py`/`ast.py` ⇄
   `storage.{hpp,cpp}`/`records.hpp`/`ast.cpp`) land in BOTH ports on this
   branch; `#18 parity_check` is the gate. `query.py`/`model.py` are
   Python-only and EXEMPT (the Γ engine + 3b are Python-only).
4. **Schema bump 10 → 11, additive only** (ALTER ADD COLUMN + flip `changed`),
   matching the v9→v10 precedent. v10 DBs upgrade in place; the new columns are
   empty until reindex.

---

## 1. Storage delta (v10 → v11)

Two additive INTEGER columns carry the per-location value-ness boolean. Read
join-free by the engine; `call_result` value-ness is determinable **at the call
site** so no cross-TU return-type resolution is needed (the reason a boolean
column beats a symbol-level flag + join — ADR-003 §3a.2).

| column | type | meaning |
|---|---|---|
| `call_arg.type_is_value` | INTEGER | `1` = arg location holds its static record type **by value** (exact, non-erased); `0`/`NULL` = no / unknown → ⊤ |
| `edge_site.recv_type_is_value` | INTEGER | same, for a virtual call's receiver |

**INTEGER 0/1/NULL semantics.** `1` ⇒ engine may seed the exact singleton
`{type_usr}`. `0` **and** `NULL` are both treated as "not provably value" ⇒ ⊤
(the engine tests `if recv_type_is_value:` / `if a.type_is_value:`, so SQLite
`NULL`→falsy and `0`→falsy are identical). Old v10 rows (column absent →
backfilled `NULL` by ALTER) therefore behave exactly as Phase 2 (⊤). The
extractor writes an explicit `0` for non-value record locations and `1` for
value ones; it omits the column (→ NULL) for non-record args, which is also ⊤.

### 1.1 `_SCHEMA` edits (`storage.py:160-204`)

Add one column to each CREATE TABLE (additive, end of column list, before the
PK):

```sql
-- edge_site (after recv_param_pos, storage.py:170)
    recv_param_pos INTEGER,
    recv_type_is_value INTEGER,          -- v11: receiver held by value (1) else 0/NULL
    PRIMARY KEY (edge_id, file_id, line, col)

-- call_arg (after callee_usr, storage.py:201)
    callee_usr TEXT,
    type_is_value INTEGER,               -- v11: arg held by value (1) else 0/NULL
    PRIMARY KEY (edge_id, file_id, line, col, position)
```

Bump `SCHEMA_VERSION = 11` (`storage.py:32`).

### 1.2 `_migrate()` v10 → v11 block (`storage.py`, after the v10 block at :375-397)

Mirror the v9→v10 `recv_param_pos` probe exactly:

```python
# v10 -> v11: value-ness booleans for exact-singleton Gamma narrowing.
# No backfill -- reindex repopulates; old rows read as NULL == not-value == TOP.
if "edge_site" in tables and "recv_type_is_value" not in escols:
    self._conn.execute(
        "ALTER TABLE edge_site ADD COLUMN recv_type_is_value INTEGER"
    )
    changed = True
cacols = (
    {r[1] for r in self._conn.execute("PRAGMA table_info(call_arg)")}
    if "call_arg" in tables
    else set()
)
if "call_arg" in tables and "type_is_value" not in cacols:
    self._conn.execute("ALTER TABLE call_arg ADD COLUMN type_is_value INTEGER")
    changed = True
```

`escols` is already computed at :377; reuse it. The trailing
`int(v) < SCHEMA_VERSION` bump at :411 then writes `11`.

### 1.3 Write-API signature changes (`storage.py`)

- `add_edge_site(...)` (:1162): append `recv_type_is_value: Optional[int] = None`
  after `recv_param_pos`. Widen the INSERT column list + VALUES placeholder +
  tuple (one more `?`). Existing callers unaffected (default `None`→NULL→⊤).
- `add_call_arg(...)` (:1195): append `type_is_value: Optional[int] = None`
  after `callee_usr`; widen the INSERT identically.

### 1.4 C++ mirror

| file | change |
|---|---|
| `records.hpp` | `EdgeSite` (+`std::optional<int64_t> recv_type_is_value;` after `recv_param_pos`, :88); `CallArg` (+`std::optional<int64_t> type_is_value;` after `callee_usr`, :100) |
| `storage.hpp` | `kSchemaVersion = 11` (:29) |
| `storage.cpp` | schema string: add `recv_type_is_value INTEGER` to `edge_site` (after :134) and `type_is_value INTEGER` to `call_arg` (after :165); `INSERT … schema_version`,'11' (:170); migrate block (after :533, mirror §1.2 with `has_col(escols,…)` + a new `cacols = table_columns("call_arg")`); widen `add_edge_site` INSERT (:1423, `bind_opt(st, 11, s.recv_type_is_value)`) and `add_call_arg` INSERT (:1442, `bind_opt(st, 10, a.type_is_value)`) |

INTEGER 0/1/NULL maps to `std::optional<int64_t>`: `std::nullopt`→NULL→⊤,
`0`→⊤, `1`→value. `bind_opt` already binds `nullopt`→NULL.

---

## 2. Extractor changes (`ast.py` + `ast.cpp`) — PARITY

### 2.1 The value discriminator (new helper)

Add `_type_is_value` next to `_record_usr_of_type` (`ast.py:545`) and the C++
twin next to `record_usr_of_type` (`ast.cpp:724`). It is the ADR-003 §3a.1
discriminator, denylist-free, sound. **It must NOT reuse the stripping
`_record_usr_of_type`**: stripping turns `B*`/`B&` into `B`, hiding the very
thing we test. The kind gate runs on the **un-stripped** canonical type.

Python:
```python
def _type_is_value(loc_type: cx.Type, dispatch_record_usr: Optional[str]) -> bool:
    """True iff loc_type holds `dispatch_record_usr` BY VALUE (exact, non-erased).

    Sound: pointer / lvalue-ref / rvalue-ref / builtin fail the RECORD kind gate;
    a smart-pointer/handle (shared_ptr<B>, IntrusiveRefCntPtr<B>, …) is canonical
    RECORD but its decl USR is the *wrapper*, never the dispatch type, so the
    USR-equality clause rejects it with no denylist.  typedef/using is stripped
    by get_canonical()."""
    if not dispatch_record_usr:
        return False
    c = loc_type.get_canonical()              # strips typedef/using; KEEPS ref/ptr/cv
    if c.kind != cx.TypeKind.RECORD:          # POINTER / L/RVALUEREF / builtin -> not value
        return False
    decl = c.get_declaration()
    if decl is None or decl.kind.value <= 0:
        return False
    return (decl.get_usr() or None) == dispatch_record_usr
```

C++ (anonymous namespace, near `record_usr_of_type`):
```cpp
// Mirrors ast.py:_type_is_value. True iff `loc_type` holds `dispatch_record_usr`
// by value (exact, non-erased). Sound: pointer/ref fail the RECORD kind gate;
// a handle's wrapper USR never equals the dispatch USR.
bool type_is_value(LibClang &lib, CXType loc_type,
                   const std::string &dispatch_record_usr) {
  if (dispatch_record_usr.empty()) return false;
  CXType c = ::clang_getCanonicalType(loc_type);
  if (c.kind != CXType_Record) return false;
  const CXCursor decl = lib.clang_getTypeDeclaration(c);
  if (lib.clang_Cursor_isNull(decl) ||
      is_invalid_kind(lib.clang_getCursorKind(decl))) {
    return false;
  }
  return CxString(lib, lib.clang_getCursorUSR(decl)).str() == dispatch_record_usr;
}
```

**Choice of `dispatch_record_usr` (the comparison key) is what makes smart
pointers sound — it must be the *dispatch / pointee* type `B`, never the
location's own record USR:**

- **Receiver** (computed in `_body_descent`, where the dispatched method `ref`
  is in hand): `dispatch_record_usr = USR(ref.semantic_parent)` — the class that
  declares the virtual method (`B` for `B::rank`). `loc_type` =
  `_peel_expr(recv_expr).type` (the receiver's *declared* type: `B` / `B*` /
  `B&` / `shared_ptr<B>`). Value `B` → RECORD usr `B == B` → **1**; `shared_ptr<B>`
  → RECORD usr `shared_ptr<B> != B` → **0**; `B*`/`B&` → kind gate → **0**.
- **Argument** (computed in the `add_call_arg` loop): the dispatch type the
  param will eventually dispatch on is not known at the call site, so use the
  arg's own *stripped* record USR `_record_usr_of_type(arg.type)` (== the
  `type_usr` already computed). Value `B` → **1**; `B*`/`B&` → kind gate → **0**;
  `shared_ptr<B>` → **1** (spurious **but sound**: it seeds the param Γ =
  `{shared_ptr<B>}`, which never matches `B::rank`'s `selecting_type` →
  empty-intersection guard → `KEEP_ALL`). See risk **R-3a-2**.

### 2.2 Where to write `recv_type_is_value` (`ast.py:783-820`)

In the CALL_EXPR branch, after the existing receiver classification (:788-809)
and before `db.add_edge_site(...)` (:810), compute the flag — only for the three
value-eligible kinds; `local`/`this`/`construct`/`None` stay `0`:

```python
recv_type_is_value: Optional[int] = None
if recv_src_kind in ("member", "global", "call_result") and recv_expr is not None:
    dispatch_usr = None
    if ref is not None and ref.kind in (
        cx.CursorKind.CXX_METHOD, cx.CursorKind.CONSTRUCTOR,
        cx.CursorKind.DESTRUCTOR, cx.CursorKind.CONVERSION_FUNCTION,
    ):
        owner = ref.semantic_parent
        dispatch_usr = owner.get_usr() if owner is not None else None
    recv_type_is_value = 1 if _type_is_value(
        _peel_expr(recv_expr).type, dispatch_usr
    ) else 0
```

Pass `recv_type_is_value=recv_type_is_value` to `add_edge_site` (extend the call
at :810-820).

### 2.3 Where to write `call_arg.type_is_value` (`ast.py:821-837`)

In the arg loop, after `_classify_value_source` (:823-825), for non-literal args:

```python
a_is_value: Optional[int] = None
if a_kind in ("member", "global", "call_result") and a_type_usr:
    a_is_value = 1 if _type_is_value(arg_cursor.type, a_type_usr) else 0
db.add_call_arg(
    edge_id, file_id, loc.line, loc.column, pos, a_kind,
    type_usr=a_type_usr, decl_usr=a_decl_usr, callee_usr=a_callee_usr,
    type_is_value=a_is_value,
)
```

`arg_cursor.type` is the arg expression's declared type; `a_type_usr` is its
stripped record USR (the §2.1 arg-key). `local`/`this`/`construct` args keep
`type_is_value=None` (construct is already exact via `type_usr`; `local`/`this`
flow through the call context, not 3a).

### 2.4 C++ mirror (`ast.cpp`)

- `emit_call_edge` (:891): add a `recv_type_is_value` param
  (`std::optional<int64_t> = std::nullopt`); set `site.recv_type_is_value`
  before `add_edge_site` (:928).
- CALL_EXPR site (:1060-1129): mirror §2.2 — after the receiver `ValueSource`
  (:1069), when `rv.src_kind ∈ {member,global,call_result}` compute
  `type_is_value(lib, clang_getCursorType(peel_expr(lib, recv_expr)), owner_usr)`
  where `owner_usr = USR(clang_getCursorSemanticParent(ref))`; pass it through
  `emit_call_edge`.
- `emit_call_args` (:934): mirror §2.3 — when `vs.src_kind ∈
  {member,global,call_result} && !vs.type_usr.empty()`, set
  `ca.type_is_value = type_is_value(lib, clang_Cursor_getCursorType(arg),
  vs.type_usr) ? 1 : 0`.

`#18 parity_check` reindexes graphlab (incl. the §7 fixtures) and golden-compares
Py vs C++ — both ports MUST emit identical `recv_type_is_value` /
`type_is_value`.

---

## 3. Query layer (`query.py`, Python-only)

### 3.1 New read fields

- `Site` (:217): add `recv_type_is_value: Optional[int] = None` (after
  `recv_param_pos`). The four edge_site SELECTs that fetch `recv_*` already list
  the columns — extend each (`:784`, `:827`, `:1285`, `:1317`) with
  `recv_type_is_value` and set it in the four `Site(...)` constructions
  (`:797`, `:839`, `:1299`, `:1330`). `to_dict()` is unchanged (it does not emit
  the `recv_*` provenance today; keep that).
- `CallArg` (:253): add `type_is_value: Optional[int] = None` (after
  `callee_usr`). Extend `call_args` (:1230) and `call_args_at` (:1252) SELECTs +
  constructions.

### 3.2 `call_sites_into` + `CallContext` (NEW — 3b consumes this)

Phase 2 did **not** add `call_sites_into` (confirmed: no such symbol on `main`).
Add both:

```python
@dataclass(frozen=True)
class CallContext:
    """One incoming `calls` edge to a callee, with its argument provenance —
    the unit the closed-world param-Γ union consumes (3b)."""
    caller: Sym
    edge_id: int
    site: Optional[Site]
    args: list[CallArg]

class GraphQuery:
    def call_sites_into(self, callee: Sym) -> list[CallContext]:
        """Every incoming `calls` edge to `callee`, each with caller + per-arg
        provenance. Built on edges_in(callee, kinds=('calls',)) + call_args(edge).
        Cross-TU/cross-repo callers appear automatically once the index is
        `resolve`d (stub->def edges point at the real callee)."""
        out: list[CallContext] = []
        for edge in self.edges_in(callee, kinds=("calls",), limit=10_000):
            site = edge.sites[0] if edge.sites else None
            out.append(CallContext(
                caller=edge.peer, edge_id=edge.edge_id, site=site,
                args=self.call_args(edge.edge_id),
            ))
        return out
```

`edges_in` (:705) already returns `Edge` objects carrying `peer` (the caller),
`edge_id`, and `sites`. No new SQL beyond reusing `call_args`.

---

## 4. Γ engine (`model.py`, Python-only)

### 4.1 3a — seed the exact singleton (`_resolve_source` :607, `gamma_for_site` :723)

**`_resolve_source`** gains a `type_is_value` parameter and a value shortcut for
`member`/`global`/`call_result`:

```python
def _resolve_source(self, ctx, src_kind, type_usr, decl_usr, callee_usr,
                    type_is_value=None):
    if src_kind in (None, "literal", "unknown"):
        return TOP
    if src_kind == "construct":
        return frozenset({type_usr}) if type_usr else TOP
    if src_kind in ("member", "global"):
        if type_is_value and type_usr:          # 3a: exact value singleton
            return frozenset({type_usr})
        if decl_usr is None:
            return TOP
        val = self._gamma.get((ctx, decl_usr))
        return val if val is not None else TOP
    if src_kind in ("local", "this"):           # unchanged (call-context flow)
        if decl_usr is None:
            return TOP
        val = self._gamma.get((ctx, decl_usr))
        return val if val is not None else TOP
    if src_kind == "call_result":
        if type_is_value and type_usr:          # 3a: by-value return singleton
            return frozenset({type_usr})
        return TOP                              # was unconditional TOP (Phase 2)
    return TOP
```

Update the two call-sites in `_bind_and_visit` (:689, :704) and the
`_seed_locals` loop (:591-605) to pass `a.type_is_value`.

**`gamma_for_site`** gains a value shortcut as the FIRST branch (highest
precedence — it is the *exact* type):

```python
# 3a: value member/global/call_result receiver -> exact singleton.
if site.recv_type_is_value and site.recv_src_kind in (
    "member", "global", "call_result"
) and site.recv_type_usr:
    return frozenset({site.recv_type_usr})
```

Soundness: a singleton seeded here is the **provably exact** dynamic type of the
location (value ⇒ slicing ⇒ no derived object can live there), so intersecting
it directly in `decide()` (which does NOT subtype-expand the receiver set —
`model.py:770-775`) can only drop *infeasible* targets. The empty-intersection
guard (`decide` :793) still backstops any USR mismatch → `KEEP_ALL`.

### 4.2 3b — closed-world cross-TU param union

**Plumbing.** `_GammaEngine.__init__` (:538) gains
`assume_closed_world: bool = False`, stored as `self._cw`. `_devirt_prune`
(:974) constructs `_GammaEngine(self._cb, assume_closed_world=...)`.
`devirtualized_callgraph` (:890) passes it through (§5).

**The union (new method).** When `assume_closed_world` is set and a virtual
site's receiver is an *unbound param* (`recv_src_kind == "local"`,
`recv_param_pos is not None`, and the normal lookups in `gamma_for_site` all
miss), consult the callers instead of returning ⊤. Add as the final fallback in
`gamma_for_site`, just before `return TOP`:

```python
if self._cw and site.recv_src_kind == "local" and site.recv_param_pos is not None:
    cw = self._closed_world_param(ctx[0], site.recv_param_pos, frozenset())
    if cw is not None:
        return cw
return TOP
```

```python
def _closed_world_param(self, callee_usr, pos, visited):
    """Monotone join of resolve_source(arg_pos) over ALL visible callers of
    `callee_usr`. Returns a frozenset (narrowed) or TOP. Sound only because the
    caller asserted assume_closed_world (the index is whole-program + resolved)."""
    if callee_usr in visited:                    # in-flight cycle -> TOP
        return TOP
    if (callee_usr, pos) in self._cw_memo:
        return self._cw_memo[(callee_usr, pos)]
    callee = self._g.get(callee_usr)
    if callee is None:
        return TOP
    self._cw_memo[(callee_usr, pos)] = TOP       # mark in-flight (breaks recursion)
    union = frozenset()
    saw_caller = False
    for cc in self._g.call_sites_into(callee):
        saw_caller = True
        arg = next((a for a in cc.args if a.position == pos), None)
        if arg is None:                          # caller passes nothing knowable -> TOP
            union = TOP; break
        ts = self._resolve_source(
            (cc.caller.usr, ()), arg.src_kind, arg.type_usr, arg.decl_usr,
            arg.callee_usr, arg.type_is_value,
        )
        # transitive: a caller's arg is itself an unbound param -> recurse
        if ts is TOP and arg.src_kind == "local" and arg.decl_usr:
            t2 = self._closed_world_param_for_decl(
                cc.caller, arg.decl_usr, visited | {callee_usr})
            ts = t2 if t2 is not None else TOP
        if ts is TOP:
            union = TOP; break
        union = union | ts
    result = TOP if (union is TOP or not saw_caller) else union
    self._cw_memo[(callee_usr, pos)] = result
    return result
```

- `not saw_caller` → TOP: a function with **no visible caller** cannot be
  narrowed even closed-world (it is an entry / unreachable in-index) → sound.
- **Any ⊤ caller defeats the union** (`union = TOP; break`) — the sound join, not
  an average (ADR-003 §3b.1).
- **Termination:** `self._cw_memo` (keyed `(callee_usr, pos)`, seeded to TOP
  in-flight) + the `visited` set + finite symbols. `_closed_world_param_for_decl`
  is a thin helper that maps a caller's param-arg `decl_usr` to its own position
  and recurses (bounded by `visited`); the transitive depth is additionally
  capped by the existing per-callable `K_LIMIT` accounting.
- Add `self._cw_memo: dict[tuple, "_Top|frozenset[str]"] = {}` to `__init__`.

**Soundness rail (both 3a & 3b).** Γ still holds only *exact* concrete USRs and
`decide()` intersects directly with no subtype expansion. 3a seeds `{T}` only on
a provably by-value location; 3b unions only types a visible caller *provably*
passes and collapses to ⊤ on the first unknown. Neither can introduce a USR the
true dynamic type set excludes → no legitimate target is ever dropped.

---

## 5. API surface (`model.py`, Python-only)

```python
def devirtualized_callgraph(self, depth=None, *, fanout=500,
                            expand_virtual=False, prune=False,
                            assume_closed_world=False) -> "Iterator[CallStep]":
```

- `prune=False` (DEFAULT): unchanged, byte-identical Phase-1.
- `prune=True, assume_closed_world=False` (DEFAULT for closed-world):
  byte-identical to **Phase 2**, plus the 3a value-singleton narrowing (which
  Phase 2 left at ⊤). 3b never fires.
- `prune=True, assume_closed_world=True`: also runs the §4.2 cross-TU union;
  **user precondition** — the index MUST be whole-program AND `resolve`d, else
  narrowing is unsound. Document loudly in the docstring.
- **Validation:** `assume_closed_world=True` with `prune=False` →
  `raise ValueError("assume_closed_world requires prune=True")` (closed-world
  narrowing is only meaningful under pruning; consistent with the existing
  `prune=True, expand_virtual=False` ValueError at :913). Plumb the flag to
  `_devirt_prune` → `_GammaEngine(..., assume_closed_world=...)`.

**No new `CallStep` fields.** Phase-2's `pruned_candidates` + `gamma_receiver`
(:254-255) already carry everything a 3a/3b narrowing needs (a smaller
`pruned_candidates`, a non-None `gamma_receiver` singleton). The docstring notes
that under closed-world a param receiver may now yield a non-None
`gamma_receiver`.

---

## 6. Fixtures (`manifests/graphlab/`)

Placement: **extend the existing `manifests/graphlab` corpus** so the `#18
parity_check` (which already reindexes graphlab) covers extraction parity with no
new ctest wiring. Reuse graphlab's `A`/`B`/`C`/`D : rank()` hierarchy and its
`int top_rank(A& a){ return a.rank(); }` (the Phase-2 motivating function).
Add two new translation units + one shared header, and append all three to
`manifests/graphlab/compile_commands.json`.

### 6.1 `devirt3.hpp` (declarations) + `devirt3.cpp` (3a cases, single TU)

```cpp
// devirt3.hpp
#pragma once
#include <memory>
#include "graphlab.hpp"            // A,B,C,D + rank()
namespace graphlab {
struct HolderV { B b;  int via();         };   // value member   -> {B::rank}
struct HolderR { B& br; HolderR(B& x):br(x){} int via(); }; // ref member  -> TOP
struct HolderP { B* bp = nullptr;  int via(); };            // ptr member  -> TOP
struct HolderS { std::shared_ptr<B> sp; int via(); };       // smart-ptr   -> TOP
B  g_b;                                          // value global  -> {B::rank}
B  make_b();                                     // by-value return -> {B::rank}
B* make_bp();                                    // ptr return     -> TOP
int use_global();
int use_ret();
int use_ret_ptr();
}
```

```cpp
// devirt3.cpp
#include "devirt3.hpp"
namespace graphlab {
int HolderV::via()   { return b.rank();        }  // POSITIVE value member
int HolderR::via()   { return br.rank();       }  // NEGATIVE ref member
int HolderP::via()   { return bp->rank();      }  // NEGATIVE ptr member
int HolderS::via()   { return sp->rank();      }  // NEGATIVE smart-ptr member
int use_global()     { return g_b.rank();      }  // POSITIVE value global
int use_ret()        { return make_b().rank(); }  // POSITIVE by-value return
int use_ret_ptr()    { return make_bp()->rank();} // NEGATIVE ptr return
}
```

Expected extraction (both ports): the `b.rank()` / `g_b.rank()` /
`make_b().rank()` sites get `recv_src_kind ∈ {member,global,call_result}`,
`recv_type_usr = USR(B)`, `recv_type_is_value = 1`; the `br`/`bp`/`sp`/`make_bp()`
sites get `recv_type_is_value = 0`.

### 6.2 `devirt3_caller.cpp` + the existing TU (3b cross-TU param)

`devirt3.hpp` adds `int dispatch_param(A& a);`. Definition lives in
`devirt3.cpp` (`int dispatch_param(A& a){ return a.rank(); }` — `a.rank()` has
`recv_src_kind=local`, `recv_param_pos=0`, no in-TU caller). The **only** caller
lives in a separate TU so narrowing is cross-TU:

```cpp
// devirt3_caller.cpp
#include "devirt3.hpp"
namespace graphlab {
void run_cross_tu() { B b; dispatch_param(b); }   // sole caller, passes B by value
}
```

After `resolve`, `call_sites_into(dispatch_param)` enumerates `run_cross_tu`'s
edge; the arg-0 provenance is `construct`/`local` `B`. Under
`assume_closed_world=True` the `a.rank()` site narrows to `{B::rank}`; under
`False` it stays ⊤. Both TUs are added to `compile_commands.json`; both ports
index them (parity) and a `resolve` pass links them.

---

## 7. Test plan (numbered) — `project/tests/test_devirt_phase3.py`

Baselines that must stay green and grow: **Python 237 passed**, **C++ 18/18
(incl. `#18 parity_check`)**. Hermetic Γ tests seed SQLite via the Storage write
API (`add_symbol`/`add_edge`/`add_edge_site`/`add_call_arg` with the new
`recv_type_is_value`/`type_is_value`) — NO libclang, exactly like
`test_devirt_phase2.py`.

### 7.1 Storage / migration (parity-relevant)

- **P3-01** `SCHEMA_VERSION == 11`; a fresh DB has `edge_site.recv_type_is_value`
  + `call_arg.type_is_value`.
- **P3-02** v10→v11 migration: open a v10 DB (drop the two columns), reopen,
  assert both columns exist, `schema_version == 11`, no data lost, and a stored
  v10 row reads `recv_type_is_value/type_is_value == None`.
- **P3-03** widened `add_edge_site`/`add_call_arg` round-trip via
  `Site.recv_type_is_value` / `CallArg.type_is_value` (incl. explicit `0` and
  `1`).

### 7.2 3a Γ-engine units (hermetic, seed value provenance)

Seed the A/B/C/D rank hierarchy (reuse Phase-2 `_seed_chain`) + the value flags:

- **P3-04 value member → singleton.** `recv_src_kind=member`, `recv_type_usr=B`,
  `recv_type_is_value=1` ⇒ `gamma_receiver == {B}`, `pruned_candidates ==
  [B::rank]`; `A/C/D::rank` absent from the walk.
- **P3-05 value global → singleton.** Same with `recv_src_kind=global`.
- **P3-06 call_result → singleton.** Same with `recv_src_kind=call_result`,
  `recv_type_usr=B`, `recv_type_is_value=1` (proves the Phase-2
  unconditional-TOP path is fixed).
- **P3-07 ref member NEGATIVE → ⊤.** `recv_src_kind=member`,
  `recv_type_is_value=0` ⇒ `gamma_receiver is None`, full `{A,B,C,D}::rank`.
- **P3-08 ptr member NEGATIVE → ⊤.** Same posture, `recv_type_is_value=0`.
- **P3-09 smart-ptr member NEGATIVE → ⊤.** `recv_type_is_value=0` (extractor sets
  0 because `shared_ptr<B>` USR ≠ `B`).
- **P3-10 value arg → param singleton.** A `call_arg` with `src_kind=member`,
  `type_usr=B`, `type_is_value=1` bound into a callee param ⇒ the callee's
  `param.rank()` prunes to `{B::rank}` (3a flowing through the call context).
- **P3-11 NULL flag == legacy ⊤.** `recv_type_is_value=None` on a member site ⇒
  identical to Phase 2 (⊤, KEEP_ALL) — guards the 0/NULL equivalence.

### 7.3 3b closed-world cross-TU units (hermetic)

Seed `dispatch_param(A& a){ a.rank() }` with `recv_src_kind=local`,
`recv_param_pos=0`, and one or more incoming `calls` edges + `call_arg` rows:

- **P3-12 narrow under closed-world.** Single caller passes `construct` `B` ⇒
  with `assume_closed_world=True`, `a.rank()` → `{B::rank}`.
- **P3-13 stays ⊤ when open-world.** Same seed, `assume_closed_world=False`
  (default) ⇒ ⊤, KEEP_ALL — **byte-identical to Phase 2**.
- **P3-14 any-⊤ caller defeats the union.** Two callers, one passes `B`, one
  passes `unknown` ⇒ even closed-world → ⊤ (monotone join).
- **P3-15 union of two concretes.** Two callers pass `B` and `C` ⇒ closed-world
  Γ = `{B,C}` ⇒ prunes to `{B::rank, C::rank}` (drops `A`,`D`).
- **P3-16 no visible caller → ⊤.** `dispatch_param` with zero incoming edges,
  closed-world ⇒ ⊤ (sound; cannot narrow an entry).
- **P3-17 transitive cross-TU.** caller→param→param chain narrows to the leaf
  concrete under closed-world.
- **P3-18 cycle termination.** A recursive `f(A& a){ a.rank(); f(a); }`
  closed-world ⇒ returns (no hang), in-flight cycle → ⊤.
- **P3-19 validation.** `devirtualized_callgraph(prune=False,
  assume_closed_world=True)` raises `ValueError`.

### 7.4 Regression / default-unchanged

- **P3-20** `devirtualized_callgraph(prune=False)` byte-identical to Phase 1
  (CallStep stream; `pruned_candidates`/`gamma_receiver` None everywhere).
- **P3-21** `devirtualized_callgraph(prune=True, assume_closed_world=False)`
  byte-identical to **Phase 2** over the chain fixture (3a only narrows the new
  value sites, which did not exist pre-Phase-3 — assert the Phase-2 sites are
  unchanged).
- **P3-22** `callgraph()` / `callees()` byte-identical (reuse Phase-2 asserts).
- Full suite stays green: **237 + P3-01…P3-22 (≈ +22)**. Run:
  `.venv/bin/python -m pytest project/tests -q`.

### 7.5 Real-parse acceptance (non-hermetic, gated like other index tests)

- **P3-23** Build the graphlab index over the §6 fixtures; assert the extractor
  wrote `recv_type_is_value=1` for `HolderV::via`/`use_global`/`use_ret` and
  `0` for `HolderR`/`HolderP`/`HolderS`/`use_ret_ptr`.
- **P3-24** `HolderV::via` / `use_global` / `use_ret` devirtualized-with-prune
  reach only `B::rank`; the four negatives keep `{A,B,C,D}::rank`.
- **P3-25** `resolve` the index, then `dispatch_param.devirtualized_callgraph(
  prune=True, assume_closed_world=True)` reaches only `B::rank`; with
  `assume_closed_world=False` it keeps all four.
- **P3-26** `#18 parity_check`: `cmake --build . -j4 && ctest
  --output-on-failure` stays **18/18** — proves Py vs C++ emit identical
  `recv_type_is_value` / `type_is_value` over the new fixtures.
- Measurement: `/tmp/phase3_prune_measure.py <db>` shows before→after devirt
  headroom on graphlab (the 3 new positive sites narrow; 7 Phase-2 sites
  unchanged).

---

## 8. File-change summary

| File | Change | Parity |
|---|---|---|
| `project/indexer/storage.py` | `SCHEMA_VERSION=11`; `edge_site.recv_type_is_value` + `call_arg.type_is_value`; v10→v11 migration; widen `add_edge_site`/`add_call_arg` | **C++ mirror required** |
| `project/indexer/clang/ast.py` | `_type_is_value`; compute `recv_type_is_value` (`_body_descent` receiver) + `type_is_value` (arg loop) | **C++ mirror required** |
| `cidx-cpp/src/storage/records.hpp` | `EdgeSite.recv_type_is_value`; `CallArg.type_is_value` | mirror of storage.py |
| `cidx-cpp/src/storage/storage.{hpp,cpp}` | `kSchemaVersion=11`; schema string; v10→v11 migration; widen `add_edge_site`/`add_call_arg` | mirror of storage.py |
| `cidx-cpp/src/clangx/ast.cpp` | `type_is_value`; `emit_call_edge`/CALL_EXPR receiver flag; `emit_call_args` arg flag | mirror of ast.py |
| `project/indexer/query.py` | `Site.recv_type_is_value`; `CallArg.type_is_value`; `CallContext` + `call_sites_into` | Python-only (EXEMPT) |
| `project/indexer/model.py` | `_resolve_source`/`gamma_for_site` value singleton (3a); `_closed_world_param` + plumbing (3b); `assume_closed_world` kwarg + ValueError | Python-only (EXEMPT) |
| `manifests/graphlab/devirt3.{hpp,cpp}`, `devirt3_caller.cpp`, `compile_commands.json` | 3a + 3b fixtures (positives + negatives) | fixture (both ports reindex) |
| `project/tests/test_devirt_phase3.py` | P3-01…P3-26 hermetic + real-parse | Python tests |

## 9. Open risks (carried from ADR-003)

- **R-3a-1** (receiver value-flag misclassification via `operator->`/conversion).
  A smart-pointer receiver `sp->rank()` may surface in libclang as a
  CXXOperatorCall whose result type is `B`, so a naïve receiver-expr type would
  read `B` (→ spurious value). **Mitigation in this design:** the receiver
  discriminator compares the *declared* receiver type against the **method
  owner** USR (`ref.semantic_parent`), and the §7.2 P3-09 / §7.5 P3-23 tests pin
  the smart-ptr member to `0`. Any unclassifiable receiver → `0` → ⊤ → KEEP_ALL
  (sound).
- **R-3a-2** (smart-pointer *argument* spurious value flag). An arg
  `shared_ptr<B>` may get `type_is_value=1` (no dispatch USR available at the
  call site). **Sound** — it seeds the param Γ = `{shared_ptr<B>}`, which never
  matches a `B::rank` `selecting_type`, so `decide()`'s empty-intersection guard
  returns KEEP_ALL. Precision-only, never unsound. Documented; revisit if
  measured to matter.
- **R-3b-1** (closed-world is a *user-asserted* precondition). Asserting
  `assume_closed_world=True` on a partial or un-`resolve`d index yields unsound
  narrowing (a hidden caller could pass any type). Mitigation: default OFF; loud
  docstring; `ValueError` when combined with `prune=False`; `not saw_caller` →
  ⊤.
- **R-3b-2** (cross-TU union cost / termination). On-demand union over
  `call_sites_into` could blow up on highly-called functions. Mitigation:
  `_cw_memo` keyed `(callee_usr, pos)` + in-flight-cycle → ⊤ + the existing
  `K_LIMIT`; measure on the self-index after landing. Revisit a precomputed
  `resolve_pass` summary (a v11 table + C++ parity) only if it is a hotspot.

## 10. References

- ADR-003: `project/docs/adr-003-devirt-phase3-precision.md`.
- Phase-2 contract: `project/docs/design-devirt-phase2.md`; ADR-002:
  `project/docs/adr-002-devirt-phase2-provenance-storage.md`.
- Spec/blackboard: `~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md`.
- Code anchors (v10 @ `21c204a`): `storage.py:32/160/375/1162/1195`;
  `ast.py:545/569/654/681/734/783-837`; `query.py:217/253/705/784/1230/1252`;
  `model.py:493/526/607/648/723/758/890/967`; `records.hpp:77/91`;
  `storage.cpp:124/170/520/1423/1442`; `storage.hpp:29`; `ast.cpp:724/754/838/
  879/932/1060-1129`.
- Cognee: `task:devirt-callgraph-phase3`, `role:senior-developer`.
</content>
</invoke>

# cidx Devirtualized Callgraph — Phase 2 Technical Design

Status: design (senior engineer), 2026-06-16 — **the Developer's contract**
Branch: `feat/devirt-callgraph-phase2`
Scope: **Phase 2** — argument/receiver provenance extraction (schema + Py/C++
parity) + a pure query/model-layer type-environment (Γ) propagation pass that
**PRUNES** infeasible virtual-dispatch branches Phase 1 marked `prunable=True`.
Spec (blackboard): `~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md`
Phase 1 design: `project/docs/design-devirt-phase1.md`
Phase 1 tests (hermetic seeding pattern to copy): `project/tests/test_devirt_phase1.py`

---

## 0. Phase 2 contract (firm)

1. **Sound, monotone pruning.** Phase 2 only *removes* edges Phase 1 added, never
   adds. If Γ[receiver] is ⊤ (unknown / non-finite) OR the site is unprunable →
   keep the FULL Phase-1 candidate set. The join is flow-insensitive
   (`Γ[v] |= Γ[w]`); we never *kill* a binding.
2. **Default unchanged.** `Callable.devirtualized_callgraph(prune=False)` is the
   default and stays byte-identical to Phase 1. `callgraph()` / `callees()` are
   untouched. New behaviour is opt-in via `prune=True` (which forces
   `expand_virtual=True`).
3. **C++/Python parity.** Every schema + extraction change in `storage.py` /
   `ast.py` lands in the C++ mirror (`storage.cpp`+`records.hpp` /
   `ast.cpp`+`parse.cpp`) in this same branch. `model.py` and `query.py` are
   Python-only and EXEMPT (the Γ pass is Python-only).
4. **Schema bump 9 → 10, additive only.** Old DBs upgrade safely (CREATE TABLE IF
   NOT EXISTS + ALTER ADD COLUMN), matching the existing `_migrate()` pattern.

---

## 1. Storage option (settled)

The Phase-1 gating decision ("extend `edge_site` vs side table vs recompute") is
settled as **`edge_site` columns for the receiver + a `call_arg` side table for
per-argument provenance**. Rationale: the `edge_site` PK `(edge_id, file_id,
line, col)` stays stable (no widening), arguments are a 1-row-per-position child
of a call site (natural side table), and it is purely additive so v9 DBs upgrade
without rebuild (the new columns/table are simply empty until a reindex).

### 1.1 `edge_site` — two new nullable columns (receiver provenance)

For a virtual `calls` edge_site, record the receiver's provenance:

| column | type | meaning |
|---|---|---|
| `recv_src_kind` | TEXT NULL | source kind of the call's receiver: `local` \| `construct` \| `member` \| `global` \| `call_result` \| `this` \| `unknown` (NULL for a non-virtual / receiver-less call) |
| `recv_type_usr` | TEXT NULL | USR of the receiver's **static** record type (keys the selection map; e.g. `c:@S@A` for `a.rank()`). NULL when not a record receiver. |
| `recv_decl_usr` | TEXT NULL | USR of the local/param/field the receiver *names* when `recv_src_kind in (local,member,global,this)` — the Γ-location key (e.g. the `a` PARM_DECL's USR). NULL otherwise. |

`recv_type_usr` is the static-type fallback; `recv_decl_usr` is what lets Γ flow
a *call-site* binding (`top_rank(b)` → param `a`) into the dispatch decision.

### 1.2 `call_arg` — new side table (per-argument provenance)

One row per positional argument of a `calls` edge_site:

```sql
CREATE TABLE IF NOT EXISTS call_arg (
    edge_id   INTEGER NOT NULL REFERENCES edge(id) ON DELETE CASCADE,
    file_id   INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    line      INTEGER NOT NULL,
    col       INTEGER NOT NULL,
    position  INTEGER NOT NULL,            -- 0-based argument index
    src_kind  TEXT NOT NULL,              -- local|construct|literal|member|global|call_result|unknown
    type_usr  TEXT,                        -- USR of the arg's static record type (NULL for non-class/builtin)
    decl_usr  TEXT,                        -- USR of the named local/param/field when src_kind in (local,member,global)
    callee_usr TEXT,                       -- USR of the callee whose return type seeds Γ when src_kind=call_result
    PRIMARY KEY (edge_id, file_id, line, col, position)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_call_arg_edge ON call_arg(edge_id);
```

The `(edge_id, file_id, line, col)` prefix is exactly the `edge_site` PK, so a
call site's args join 1:1 to its site row. `construct` rows put the constructed
record's USR in `type_usr` (e.g. `top_rank(B{})` → `type_usr = c:@S@B`,
`src_kind=construct`). `local` rows put the named var's decl USR in `decl_usr`.

### 1.3 SCHEMA_VERSION + migration (Python `storage.py`)

- `SCHEMA_VERSION = 10`.
- In `_SCHEMA`: add the three `edge_site` columns to the CREATE TABLE, add the
  `call_arg` CREATE TABLE + index, after the existing `template_arg` block.
- In `_migrate()` (mirror the v8→v9 `decl_path` block ~line 330): after the
  `decl_path` check, add an `edge_site` column probe and a `call_arg` table
  probe:

  ```python
  escols = {r[1] for r in self._conn.execute("PRAGMA table_info(edge_site)")} \
           if "edge_site" in tables else set()
  if "edge_site" in tables and "recv_src_kind" not in escols:
      # v9 -> v10: receiver provenance for virtual dispatch. No backfill --
      # reindex repopulates from the AST.
      self._conn.execute("ALTER TABLE edge_site ADD COLUMN recv_src_kind TEXT")
      self._conn.execute("ALTER TABLE edge_site ADD COLUMN recv_type_usr TEXT")
      self._conn.execute("ALTER TABLE edge_site ADD COLUMN recv_decl_usr TEXT")
      changed = True
  if "edge_site" in tables and "call_arg" not in tables:
      # v9 -> v10: per-argument provenance side table (schema script CREATEs it
      # on the next open; nothing to backfill -- edges are re-derived).
      changed = True
  ```

  The `call_arg` table itself is created by `_SCHEMA` (CREATE TABLE IF NOT
  EXISTS), run after `_migrate()`, so the migration only needs to flip `changed`
  to bump the version — identical to how the v6→v7 graph tables are handled.

### 1.4 EdgeSite write API (`storage.py`)

- Extend `add_edge_site(...)` with three new optional kwargs (default None),
  appended after `args_sig` so existing callers are unaffected:
  `recv_src_kind=None, recv_type_usr=None, recv_decl_usr=None`. Widen the INSERT
  column list + tuple accordingly.
- New mutator `add_call_arg(self, edge_id, file_id, line, col, position,
  src_kind, type_usr=None, decl_usr=None, callee_usr=None) -> None` — an
  `INSERT OR IGNORE` mirroring `add_edge_site` (PK collision = same arg, harmless).
- New `Site` read fields are added in `query.py` (§3), NOT a new dataclass here.

---

## 2. Extractor changes (`ast.py` + C++ `ast.cpp`/`parse.cpp`) — PARITY

All changes are in the body-descent CALL_EXPR branch (`_body_descent`,
`ast.py:520-554`; `body_descent_visitor`, `ast.cpp:781-844`). The descent already
has `edge_id`, `file_id`, and the call cursor `loc`. We add a single provenance
pass per CALL_EXPR, right after `add_edge_site(...)`.

### 2.1 Receiver provenance (virtual dispatch)

A CALL_EXPR that is a C++ member call has a receiver sub-expression. Compute it
and classify:

```
recv = receiver_subexpr(call)          # the object expr of a MemberRefExpr callee
if recv is None:                       # free function / static method -> no receiver
    recv_src_kind = recv_type_usr = recv_decl_usr = None
else:
    static_type = recv.type            # canonical, strip ref/cv/pointer
    recv_type_usr = USR(static_type.record_decl) or None
    (recv_src_kind, recv_decl_usr) = classify_value_source(recv)
```

`classify_value_source(expr)` (shared by receiver + args), by cursor kind of the
*peeled* expression (peel implicit casts, parens, `unary *`/`&`, member-base):

| peeled expr kind | src_kind | decl_usr / type_usr |
|---|---|---|
| DECL_REF_EXPR → VAR_DECL (local/static) | `local` | decl_usr = referenced VAR_DECL USR |
| DECL_REF_EXPR → PARM_DECL | `local` | decl_usr = the PARM_DECL USR |
| CXXThisExpr | `this` | decl_usr = enclosing method's owner-class USR-as-this (see §4.6) |
| CALL_EXPR to a constructor / CXXTemporaryObjectExpr / CXXNewExpr | `construct` | type_usr = constructed record USR |
| MEMBER_REF_EXPR → FIELD_DECL | `member` | decl_usr = field USR; type_usr = field record USR |
| DECL_REF_EXPR → global/namespace VAR_DECL | `global` | decl_usr = var USR |
| CALL_EXPR to a function/method (non-ctor) | `call_result` | callee_usr = callee USR (its return type seeds Γ) |
| INTEGER/FLOATING/STRING/CHAR literal, builtin | `literal` | n/a (no record) |
| anything else | `unknown` | — |

Write the receiver fields by replacing the existing `add_edge_site(...)` call in
the CALL_EXPR branch with one that passes `recv_src_kind/recv_type_usr/
recv_decl_usr` (None for non-member calls).

### 2.2 Argument provenance

After emitting the edge_site, iterate the call's argument cursors
(`cx.Cursor.get_arguments()` in Python; `clang_Cursor_getArgument` /
`clang_Cursor_getNumArguments` in C++) and for each position run
`classify_value_source(arg)`; emit one `add_call_arg(edge_id, file_id, loc.line,
loc.column, position, src_kind, type_usr, decl_usr, callee_usr)`.

Only emit a `call_arg` row when `src_kind != 'literal'` and the arg has any
record/decl/callee info — a pure `literal`/builtin arg contributes nothing to Γ
and is skipped (keeps the table to the rows the Γ pass actually reads). Record
`construct`/`local`/`member`/`global`/`call_result`/`unknown` rows.

### 2.3 Helper placement

Add a private helper `_classify_value_source(db, expr) -> (src_kind, type_usr,
decl_usr, callee_usr)` near `_named_type_decl` in `ast.py`, and the C++ twin
`classify_value_source(LibClang&, Storage&, CXCursor) -> ValueSource` (a small
struct in the `ast.cpp` anonymous namespace) near `emit_type_use`. Receiver
extraction `receiver_subexpr(call)` is a second small helper: the object of a
member-call is the first child of the CALL_EXPR whose callee child is a
`MEMBER_REF_EXPR` (in libclang the receiver is the MEMBER_REF_EXPR's first
child). Reuse the existing implicit-cast/paren peeling already needed by
`classify_value_source`.

### 2.4 C++ mirror (`ast.cpp` / `parse.cpp` / `records.hpp` / `storage.cpp`)

- `records.hpp`: add `recv_src_kind`/`recv_type_usr`/`recv_decl_usr`
  (`std::optional<std::string>`) to `struct EdgeSite`; add a new `struct CallArg`
  mirroring the `call_arg` columns.
- `storage.cpp`: bump the schema literal `('schema_version', '10')`; add the
  three `edge_site` columns + the `call_arg` CREATE TABLE/index to the schema
  string; add the v9→v10 migration block (mirror §1.3) in the migrate function;
  widen `add_edge_site` INSERT; add `Storage::add_call_arg(const CallArg&)`.
- `storage.hpp`: `kSchemaVersion = 10`; declare `add_call_arg`.
- `ast.cpp`: in `body_descent_visitor` CALL_EXPR branch, after `emit_body_edge`,
  compute receiver provenance (set the EdgeSite fields BEFORE `add_edge_site` —
  this means `emit_body_edge` grows a receiver-provenance out-param or is
  split so the site fields can be set) and iterate `clang_Cursor_getArgument`
  emitting `CallArg` rows. Keep the `emit_type_use`/instantiates logic intact.
- `parse.cpp`: no logic change expected (it drives `body_descent`); only touch if
  it constructs EdgeSite directly.

The `#18 parity_check` ctest re-indexes and golden-compares Py vs C++ output, so
it validates this extractor parity end-to-end — both ports MUST emit identical
`edge_site` receiver fields and `call_arg` rows.

---

## 3. Query-layer reads (`query.py`, Python-only)

Phase 2 reads the new provenance; no schema logic here.

- Extend `Site` (frozen dataclass) with `recv_src_kind`, `recv_type_usr`,
  `recv_decl_usr` (default None) and add them to `to_dict()`. Update the two
  edge_site SELECTs (`_sites_for` ~line 682, `sites` ~line 713) to fetch the new
  columns.
- New frozen value `CallArg(position, src_kind, type_usr, decl_usr, callee_usr)`
  + `GraphQuery.call_args(edge) -> list[CallArg]` (SELECT from `call_arg` by
  edge_id, ORDER BY file_id,line,col,position).
- New `GraphQuery.call_sites_into(callee_sym) -> list[CallContext]` where
  `CallContext` bundles `(caller_sym, edge_id, site, args: list[CallArg])` for
  every incoming `calls` edge to `callee_sym`. This is what the Γ param-binding
  step consumes (callers of a function, with their argument provenance).

---

## 4. The Γ propagation algorithm (model layer, NO libclang)

Lives in `model.py` as a new internal engine `_GammaEngine`, driven by
`Callable.devirtualized_callgraph(prune=True)`. Pure reads over `query.py`.

### 4.1 Types

- A **TypeSet** is either `TOP` (the sentinel, unknown/non-finite) or a frozenset
  of record USRs (concrete static types). `TOP` joined with anything = `TOP`.
- **Γ** maps an abstract location key → TypeSet. Location key = a record/var USR
  string. `Γ[k]` absent ⇒ treated as ⊤ on read (sound default).
- A **context** is `(callee_id, param_sig)` where `param_sig` is a hashable
  signature of the bound parameter TypeSets (the tuple of frozensets/⊤ per
  param). Used for the context-sensitivity cache + k-limit.

### 4.2 Subtype closure

`closed(typeset)` expands each USR in the set with its concrete subclasses
(`graph.subclasses(sym, direct=False)`), because a receiver typed `B` may
dynamically be any `B`-subtype. Intersection at a dispatch site is done against
this closure so `Γ[a]={B}` keeps `B::rank` and (if present) `C/D` selections
inheriting through B. `⊤` stays `⊤`.

### 4.3 Initialization (Γ seeds)

Seeded per analysed callable from the provenance of its body + its call context:

- `construct` arg/receiver `B{}` / `new B` → `{B}`.
- `local` receiver/arg naming var `v` → `Γ[decl_usr(v)]` (looked up; seeded by the
  var's own construction site if present in body, else ⊤).
- a PARM_DECL `p` → bound from the *call context* (§4.5); ⊤ if analysed without a
  caller (root with class-typed params).
- `member`/`global` loc → `Γ` of that loc if a construction is visible, else ⊤.
- `call_result` → return-type set of `callee_usr` closed over subtypes, else ⊤.
- `literal`/`unknown` → contributes nothing / ⊤.

### 4.4 Flow-insensitive join

Assignments/initializers seen in provenance contribute `Γ[v] |= Γ[w]` (set union;
never kill). We do not track program order — the cheap sound first cut. Any
reassignment from an unknown source forces `Γ[v] = TOP`.

### 4.5 Param binding (context-sensitive, cached, k-limited)

```
K_LIMIT = 3                         # default cloning depth bound

def analyse(callable_id, gamma_params, k):
    ctx = (callable_id, param_sig(gamma_params))
    if ctx in cache:                # cycle/recursion/repeat -> hit, terminate
        return cache[ctx]
    cache[ctx] = PENDING            # mark in-flight (breaks recursion soundly)
    gamma = init_gamma(callable_id, gamma_params)     # §4.3/§4.4 from provenance
    result = SiteDecisions()
    for site in virtual_call_sites(callable_id):       # §4.7 prune decision
        result.add(decide(site, gamma))
    for (callee, args) in outgoing_calls(callable_id):
        if k >= K_LIMIT:
            bound = {param_i: TOP for all params}      # k-limit -> conservative
        else:
            bound = bind_params(callee, args, gamma)   # map arg provenance -> param TypeSet
        analyse(callee, bound, k + 1)                  # descend (result merged via walk)
    cache[ctx] = result
    return result
```

- `bind_params`: for each arg position, `local`→`Γ[decl_usr]`, `construct`→
  `{type_usr}`, `call_result`→return set, `member/global`→Γ-of-loc-or-⊤,
  `literal`→irrelevant (non-class param), `unknown`→⊤. The callee's param USR
  becomes a Γ key in the callee's own init.
- **k-limit**: once recursion depth `k` reaches `K_LIMIT`, stop cloning — bind all
  params to ⊤ (sound: the callee falls back to its static behaviour). Default
  `K_LIMIT=3`; exposed as a constant in `model.py` so it can be tuned.
- **Termination**: the `cache` keyed by `(callee_id, param_sig)` makes any
  repeated context (cycle, recursion, diamond) a hit; combined with the k-limit
  the analysis is finite. `param_sig` is canonicalised (sorted frozensets, ⊤
  sentinel) so equal contexts collide.

### 4.6 `this` receiver

`recv_src_kind='this'` keys Γ by the enclosing method's owner-class USR. When the
method is reached through a bound context whose owner type is narrowed (e.g. the
object was `construct`ed `B`), `Γ[this] = {B}` and the inner virtual call prunes;
otherwise ⊤ (sound).

### 4.7 Prune decision at a dispatch site

```
def decide(site, gamma):
    ds = dispatch_selection(site.declared_target, close_subtypes=True)
    if not ds.prunable:                         # unprunable -> keep full set
        return KEEP_ALL(ds)
    g = gamma_for_receiver(site, gamma)         # local key -> Γ[decl]; else Γ[type_usr]; else TOP
    if g is TOP:                                # no type info -> keep full set
        return KEEP_ALL(ds)
    keep = closed(g)                            # close over subtypes
    kept = [s for s in ds.candidates if s.selecting_type.usr in keep]
    if not kept:                                # intersection empty -> unsound to drop all
        return KEEP_ALL(ds)                     # fall back (Γ disjoint from hierarchy)
    return KEEP(kept)                           # PRUNE the rest (the only narrowing)
```

`gamma_for_receiver`: if the site's `recv_decl_usr` is a Γ key, use `Γ[decl]`
(the call-site-flowed binding — this is what carries `b`'s `{B}` into `top_rank`'s
`a`); else if `recv_type_usr` is set and `Γ[recv_type_usr]` known, use it; else
`TOP`. Empty-intersection guard (`if not kept`) keeps the result sound when Γ and
the hierarchy are disjoint (never drop every edge).

### 4.8 Soundness summary

Every branch that lacks information returns `KEEP_ALL` (⊤ receiver, unprunable
site, empty intersection, k-limit exhaustion, missing provenance). The only place
edges are removed is §4.7's `KEEP(kept)` with a non-empty, finite, closed
intersection — strictly a subset of Phase 1. Monotone by construction.

---

## 5. New CallStep field(s) + API surface (`model.py`, Python-only)

### 5.1 CallStep gains pruning metadata (additive, default None)

```python
@dataclass(frozen=True)
class CallStep:
    callee: "Entity"
    depth: int
    dispatch_site: "Optional[DispatchSiteModel]" = None
    pruned_candidates: "Optional[list[SelectionModel]]" = None  # Phase 2: kept subset
    gamma_receiver: "Optional[frozenset[str]]" = None           # Phase 2: Γ[receiver] USRs (None == TOP)
```

- `pruned_candidates`: the surviving selections after Γ pruning at this virtual
  hop. `None` when `prune=False` (Phase-1 default) OR the site was kept-all (then
  it equals `dispatch_site.selections`); a strict subset only when an actual
  prune happened. (A non-virtual hop keeps both fields None.)
- `gamma_receiver`: the receiver TypeSet that drove the decision (`None` ⇒ ⊤),
  for explainability/tests.

### 5.2 `Callable.devirtualized_callgraph(...)`

```python
def devirtualized_callgraph(self, depth=None, *, fanout=500,
                            expand_virtual=False, prune=False
                            ) -> "Iterator[CallStep]":
```

- `prune=False` (DEFAULT): **unchanged** — byte-identical Phase-1 behaviour
  (`pruned_candidates`/`gamma_receiver` stay None; node set + order identical).
- `prune=True`: implies `expand_virtual=True` (we walk the superset, then narrow).
  Runs the `_GammaEngine` (§4) seeded at `self`, and at each virtual hop attaches
  `pruned_candidates` + `gamma_receiver`; the walk descends only into kept targets
  (the pruned subset), so a pruned target is NOT visited (this is where the graph
  actually shrinks vs `expand_virtual=True, prune=False`).
- Composition with `depth`/`fanout`: unchanged — `depth` still bounds levels,
  `fanout` still caps callees expanded per node; pruning happens *before* a
  virtual callee's targets are pushed, so depth/fanout apply to the pruned set.
- If `prune=True` but `expand_virtual=False` is passed explicitly → raise
  `ValueError` (pruning requires the superset walk).

`Method.dispatch_selection()` is unchanged; the engine calls it with
`close_subtypes=True` internally.

---

## 6. Fixture change (required for the motivating e2e case)

The motivating case needs `void f() { B b; top_rank(b); }`. Currently
`manifests/graphlab/chain.cpp` has only `top_rank` and the call site lives in
`main.cpp` as `top_rank(d)` (a `D`, not the spec's `B`). Add to the graphlab
fixture (so the parity_check / e2e reindex sees it):

- `chain.hpp`: add prototype `void f();` inside `namespace chain`.
- `chain.cpp`: add `void f() { B b; top_rank(b); }`.

This yields: `Γ[b]={B}` at `f`; bound into `top_rank`'s `a` (`recv_decl_usr` of
`a.rank()` == `a`'s PARM USR, `call_arg[0].decl_usr` == `b`'s VAR USR); the
dispatch at `a.rank()` prunes to `{B::rank}` only. Keep the existing
`top_rank(d)` call in main.cpp (a second context `{D}` → prunes to `D::rank`,
exercising context sensitivity).

---

## 7. Test plan

All Γ-propagation unit tests are **hermetic**: seed SQLite via the Storage write
API (`add_symbol`/`add_edge`/`add_edge_site`/**`add_call_arg`**), NO libclang —
exactly like `test_devirt_phase1.py`. New file:
`project/tests/test_devirt_phase2.py`.

### 7.1 Storage / migration (parity-relevant)

- `SCHEMA_VERSION == 10`; a fresh DB has `call_arg` + the three `edge_site`
  columns.
- v9→v10 migration: open an old-schema DB (build via a v9 `_SCHEMA` snapshot or
  drop the columns), reopen, assert columns/table exist and `schema_version==10`,
  and that no data was lost.
- `add_call_arg` + widened `add_edge_site` round-trip via `GraphQuery.call_args`
  / `Site.recv_*`.

### 7.2 Γ-engine unit tests (hermetic, seed provenance)

Extend `_seed_chain` with `f`/`b` symbols + a `call_arg` row (`top_rank(b)`,
position 0, `src_kind=local`, `decl_usr=b`) and the `a.rank()` edge_site
receiver fields (`recv_src_kind=local`, `recv_decl_usr=a`, `recv_type_usr=A`):

- **GP-01 motivating case**: `f.devirtualized_callgraph(prune=True)` yields the
  `a.rank()` CallStep with `pruned_candidates == [B::rank]`, `gamma_receiver ==
  {B}`; `A/C/D::rank` are NOT in the walk's node set.
- **GP-02 construct**: `g(){ top_rank(B{}); }` → same prune to `{B::rank}`.
- **GP-03 second context (D)**: `top_rank(d)` context prunes to `{D::rank}`;
  proves context sensitivity (two contexts of `top_rank`, different results).
- **GP-04 sound fallback ⊤**: an unknown-source arg (`src_kind=unknown` or a
  param with no caller) → `gamma_receiver is None`, `pruned_candidates` == full
  `{A,B,C,D}::rank` (KEEP_ALL).
- **GP-05 unprunable site**: seed a `target-stub` so `prunable=False` → KEEP_ALL
  even with a finite Γ.
- **GP-06 empty intersection guard**: `Γ[a]={X}` disjoint from the hierarchy →
  KEEP_ALL (never drop all).
- **GP-07 subtype closure**: `Γ[a]={B}` with `E:B` (no own override) keeps the
  inherited `E→B::rank` selection (close_subtypes path).
- **GP-08 k-limit / recursion**: a recursive/cyclic call chain terminates and
  caps cloning at `K_LIMIT`; assert it returns (no hang) and falls back to ⊤
  past the limit.
- **GP-09 `this` receiver**: a method body calling `this->rank()` reached via a
  `construct`ed `{B}` context prunes to `{B::rank}`.

### 7.3 Regression / default-unchanged (the 194 guarantee)

- **GP-10**: `devirtualized_callgraph(prune=False)` output (CallStep stream:
  callee ids, depths, dispatch_site presence) is byte-identical to Phase 1 over
  the chain fixture, with `pruned_candidates is None` everywhere.
- **GP-11**: `callgraph()` and `callees()` byte-identical before/after (reuse the
  Phase-1 regression assertions).
- Full suite stays green: **194 passed** + the new GP-01…GP-11 (≈ +12–15).
  Run: `.venv/bin/python -m pytest project/tests -q`.

### 7.4 End-to-end (real index, parity)

- After the fixture change (§6) + extractor changes, the C++ `#18 parity_check`
  ctest reindexes graphlab and golden-compares — it must stay green, proving the
  `edge_site` receiver fields + `call_arg` rows are emitted identically by both
  ports. Run: `cmake --build . -j4 && ctest --output-on-failure` (BASELINE
  18/18).
- An e2e Python test (non-hermetic, gated like other index-building tests) builds
  the graphlab index and asserts `chain::f` devirtualized-with-prune reaches only
  `B::rank` at the `top_rank` hop.

---

## 8. File-change summary

| File | Change | Parity |
|---|---|---|
| `project/indexer/storage.py` | `SCHEMA_VERSION=10`; `edge_site` +3 cols; `call_arg` table; migration; `add_edge_site` kwargs; `add_call_arg` | C++ mirror required |
| `project/indexer/clang/ast.py` | `_classify_value_source` + receiver extraction; emit receiver fields + `call_arg` rows in CALL_EXPR branch | C++ mirror required |
| `cidx-cpp/src/storage/records.hpp` | `EdgeSite` +3 fields; new `CallArg` | mirror of storage.py |
| `cidx-cpp/src/storage/storage.{hpp,cpp}` | `kSchemaVersion=10`; schema string; migration; `add_edge_site`; `add_call_arg` | mirror of storage.py |
| `cidx-cpp/src/clangx/ast.cpp` (+`parse.cpp` if needed) | receiver + arg provenance in body descent | mirror of ast.py |
| `project/indexer/query.py` | `Site.recv_*`; `CallArg`; `call_args`; `call_sites_into` | Python-only (EXEMPT) |
| `project/indexer/model.py` | `_GammaEngine`; `CallStep.pruned_candidates`/`gamma_receiver`; `devirtualized_callgraph(prune=...)` | Python-only (EXEMPT) |
| `manifests/graphlab/chain.{hpp,cpp}` | add `f(){ B b; top_rank(b); }` | fixture (both ports reindex it) |
| `project/tests/test_devirt_phase2.py` | GP-01…GP-11 hermetic + e2e | Python tests |

## 9. Open risks

- **R1**: receiver-subexpr extraction from libclang member calls is fiddly
  (implicit `this`, conversion operators). Mitigation: any unclassifiable
  receiver → `unknown` → ⊤ → KEEP_ALL (sound). Validate against graphlab in the
  parity_check.
- **R2**: cross-TU — a param with no indexed caller starts ⊤. Same posture as
  Phase 1 (`assume_closed_world` is out of Phase-2 scope; resolve roll-up later).
- **R3**: flow-insensitive join over-approximates after reassignment; documented,
  sound (it can only *keep* extra edges).
- **R4**: `call_arg` table size on large indexes — bounded by skipping `literal`
  args and indexing by edge_id; measure on the self-index after landing.

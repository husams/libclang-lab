# ADR-003: Phase-3 devirtualization precision (value-typed Γ + cross-TU resolve roll-up)

Status: accepted
Date: 2026-06-17
Branch: (to-be-cut) feat/devirt-callgraph-phase3
Supersedes/relates: ADR-002 (Phase-2 provenance storage); spec
`~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md`; Phase-2 contract
`project/docs/design-devirt-phase2.md`.

## Context

Phase 2 (schema v10, merged PR #22) stores per-arg / per-receiver value-source
provenance and prunes `prunable` virtual dispatch with a pure-Python Γ engine
(`model.py:_GammaEngine`). Two precision gaps remain, both verified against the
v10 code on `main` (@ `21c204a`):

1. **Value-typed member / global / by-value return collapse to ⊤.**
   `_GammaEngine._resolve_source` (`model.py:607`) maps `member`/`global` →
   `Γ[decl_usr]` (which has no cross-function construction visible → ⊤) and
   `call_result` → ⊤ unconditionally (`model.py:626-635`). But a **value**
   (non-ref/non-ptr/non-erased) field `struct Holder{ B b; }`, global `B g_b;`,
   or by-value return `B make_b();` has a dynamic type **exactly equal** to its
   static type — slicing makes it impossible to store a derived object through
   that location. Γ may soundly be the singleton `{static-type USR}`. Today all
   three are ⊤ → no prune.

2. **Cross-TU params start ⊤.** A function whose callers live in other TUs has no
   in-TU `call_arg` binding, so its class-typed params seed ⊤ (KEEP_ALL). cidx
   already has a `resolve` roll-up (`storage.py:resolve_pass` / `cmd_resolve`)
   that links stub→def edges and counts cross-repo edges; the stored Phase-2
   provenance is exactly what a roll-up needs to thread caller-side Γ across TUs.

### Engine invariant that constrains both decisions (verified `model.py:decide` ~758)

Γ holds **exact concrete** record USRs and `decide()` intersects them **directly**
against each candidate's `selecting_type` with **NO subtype expansion of the
receiver set**. That is why a `construct`/value `B b;` prunes `a.rank()` to exactly
`{B::rank}`. **Consequence (the soundness rail for both 3a and 3b):** seeding Γ with
anything other than the *provably exact dynamic type* of the location is UNSOUND —
it would silently drop legitimate subtype targets. So 3a may only seed `{T}` when
the location provably holds a `T` **by value**, and 3b may only union types that
visible callers provably pass.

### Forces / constraints

- `this` is **out of scope for 3a**: `*this`'s dynamic type is not its static type
  (the object may be a derived instance). Leave `this` → ⊤.
- Reference / pointer / smart-pointer / type-erased members/globals/returns
  (`B&`, `B*`, `std::shared_ptr<B>`, `std::function`, …) CAN hold a derived object
  → MUST stay ⊤.
- HARD parity rule: any `storage.py`/`ast.py` change lands in the C++ mirror
  (`storage.{hpp,cpp}`+`records.hpp` / `ast.cpp`) on the same branch.
  `model.py`/`query.py` are Python-only and EXEMPT.
- Migration must be additive (v10 DBs exist at `~/.cache/cidx/index.db`); the
  established pattern is CREATE-IF-NOT-EXISTS + ALTER-ADD-COLUMN + flip `changed`.
- Default behaviour must stay byte-identical: `prune=False` unchanged;
  `assume_closed_world=False` default.

## Decision

### 3a — value-ness is an additive boolean on the provenance rows, classified locally at the call site

1. **Where value-ness is determined (libclang, extractor).** A denylist-free,
   sound discriminator computed locally at extraction time:

   ```
   def type_is_value(loc_type, static_record_usr) -> bool:
       c = loc_type.get_canonical()            # strips typedef/using; keeps cv + ref/ptr
       if c.kind != TypeKind.RECORD:           # POINTER / LVALUE/RVALUE-REF / builtin → not value
           return False
       return usr(c.get_declaration()) == static_record_usr
   ```

   The USR-equality clause is the discriminator: a value `B b` has canonical
   `RECORD` whose decl USR == the receiver's static record USR (`B`); a
   `std::shared_ptr<B>` is *also* canonical `RECORD` but its decl USR is
   `shared_ptr<B>` ≠ `B` (the dispatch type comes from the pointee, not the field
   type) → rejected with no smart-pointer denylist. `B*`/`B&` fail the `kind ==
   RECORD` gate. Typedef/`using` aliases are handled by `get_canonical()`.
   **Empirically verified** (2026-06-17, libclang 18.1.1): `B b`→RECORD/usr=B;
   `shared_ptr<B>`→RECORD/usr=shared_ptr; `unique_ptr<B>`→RECORD/usr=unique_ptr;
   `B*`→POINTER; `B&`→LVALUEREFERENCE; `using BRef=B&`→LVALUEREFERENCE.
   - **member**: classify the MEMBER_REF_EXPR's FIELD_DECL `.type`.
   - **global**: classify the DECL_REF_EXPR's VAR_DECL `.type`.
   - **call_result**: classify the CALL_EXPR's own `.type` (the return type, known
     locally even cross-TU because the prototype is visible) against the return
     record USR; also populate `type_usr` = the return record USR (today NULL for
     `call_result`) so the engine has a singleton to seed.
   - **Conservative default**: anything not provably an exact value record (null
     type, non-record, USR mismatch, unclassifiable) → `0` → ⊤.

2. **Where value-ness is stored — additive boolean columns on the provenance
   rows (chosen over a symbol-level flag + join).** Schema bumps **v10 → v11**,
   additive only:
   - `call_arg.type_is_value   INTEGER`  (1 = exact value record, 0/NULL = no/unknown)
   - `edge_site.recv_type_is_value INTEGER`
   The Γ engine reads them with **no extra join**, and — decisively — `call_result`
   value-ness is determinable **at the call site** (the call-expr type), so it
   needs no cross-TU resolution of the callee's return-type symbol (which the
   engine cannot do today; that is exactly why `call_result` → ⊤). A symbol-level
   flag would require a join AND, for `call_result`, resolving a possibly-stub
   callee across TUs. Mirrors the Phase-2 additive-column precedent.

3. **How Γ narrows (engine, Python-only).**
   - `_resolve_source`: for `member`/`global` with `type_is_value` and `type_usr`
     → return `frozenset({type_usr})` (exact singleton) **before** the Γ-of-loc
     lookup; for `call_result` with `type_is_value` and `type_usr` → `{type_usr}`.
     Without the flag → unchanged (Γ-of-loc-or-⊤ / ⊤).
   - `gamma_for_site`: for a receiver with `recv_type_is_value` and
     `recv_src_kind ∈ {member, global, call_result}` → return
     `frozenset({recv_type_usr})`. The singleton is the EXACT type — consistent
     with the no-subtype-expansion `decide()` invariant.
   - `this` untouched (→ ⊤).

### 3b — cross-TU param Γ via the resolve roll-up, gated behind an explicit closed-world opt-in

1. **THE soundness gate.** Unioning only the *visible* callers and pruning is
   sound **only under a closed-world assumption** (the index is the whole, fully
   `resolve`d program). On a partial/open index a hidden caller could pass any
   type → narrowing would be unsound. Therefore cross-TU param narrowing is gated
   behind a new explicit opt-in **`assume_closed_world: bool = False`** on
   `Callable.devirtualized_callgraph(...)`. Default **OFF** → unbound param stays
   ⊤ → sound, byte-identical to today. `assume_closed_world=True` is a **user
   precondition** asserting the index is whole-program and resolved.
   - **Union semantics (monotone join):** for an unbound class-typed param `p_i`,
     `Γ[p_i] = ⋃` over **all** visible incoming call sites of `resolve_source(arg_i)`.
     If **any** visible caller contributes ⊤ → the union is ⊤ → `p_i` stays ⊤ →
     KEEP_ALL. (One unknown caller defeats the narrowing — this is the sound join,
     not an average.)
   - **Transitive case** (a caller's arg is itself a param): bounded by the
     existing `(callee_usr, param_sig)` cache + `K_LIMIT`; a context already
     in-flight (cycle) contributes ⊤ and terminates.

2. **Shape — pure query/model layer, on-demand (NO precompute, NO storage
   change, parity-EXEMPT).** Extend `_GammaEngine`: when a param is unbound
   in-context and `assume_closed_world` is asserted, consult incoming callers via
   a new **`GraphQuery.call_sites_into(callee) -> list[CallContext]`** (built on
   the existing `edges_in(callee, kinds=("calls",))` + `call_args(edge_id)`) and
   union each incoming arg's Γ. **On-demand is recommended over a precomputed
   per-function param-Γ summary** in `resolve_pass`: a precompute WOULD be a
   storage/parity change (a new table + C++ mirror) for a cross-TU summary that is
   already bounded by the in-memory `(callee_usr, param_sig)` cache + `K_LIMIT`;
   on-demand keeps Phase 3 a Python-only change. Memoize the cross-TU param-Γ by
   `callee_usr` with a visited-set; cycle → ⊤.
   - **Termination:** finite symbols × `K_LIMIT` depth bound × cache hits on
     repeated `(callee_usr, param_sig)` contexts × in-flight cycle → ⊤.

3. **Cross-repo composition.** This rides on `resolve`'s existing cross-repo edge
   roll-up with **no libclang**: once `resolve_pass` has linked stub→def edges,
   a foreign caller's `calls` edge points at the real callee symbol, so
   `edges_in(callee)` naturally enumerates cross-repo callers, and each caller's
   `call_arg` rows (stored per-TU, surviving in the merged DB) supply the arg Γ.
   Closed-world narrowing therefore **requires the index to have been resolved**;
   that is part of the user's `assume_closed_world=True` precondition.

## Alternatives considered

- **3a storage — symbol-level value flag (on field/global/return symbol), read
  via join.** Rejected: needs a join in the hot Γ path, and for `call_result`
  needs the callee's return-type symbol *resolved* — impossible for a cross-TU
  stub at the call site. The call-site boolean is locally determinable for all
  three kinds (member/global/return-by-value) and join-free.
- **3a discriminator — smart-pointer name denylist** (`shared_ptr`/`unique_ptr`/
  `weak_ptr`/`function`/…). Rejected as the *primary* test: misses custom handles
  (`boost::intrusive_ptr`, `gsl::not_null`, `llvm::IntrusiveRefCntPtr`) → unsound.
  The USR-equality test (`canonical record USR == receiver static record USR`)
  catches every handle generically because a handle's field type USR never equals
  the pointee's USR. (A denylist could be a redundant secondary guard, not needed.)
- **3b — always narrow cross-TU when callers are visible (no gate).** Rejected:
  unsound on any partial index (hidden caller passes an unmodelled type). The
  closed-world opt-in defaulting OFF is the only sound default.
- **3b — precompute per-function param-Γ in `resolve_pass` (a v11 summary
  table).** Rejected for Phase 3: it is a storage + C++ parity change for a value
  the on-demand path already computes within the existing cache/K_LIMIT bound.
  Revisit only if measurement shows on-demand cross-TU union is a hotspot.

## Consequences

Positive:
- Value member/global/by-value-return now prune (the `struct Holder{ B b; }` /
  `B g_b;` / `B make_b()` families) — closes the largest remaining precision gap
  with a sound, exact singleton.
- `call_result` gains a real Γ contribution (was unconditionally ⊤).
- Cross-TU narrowing becomes possible (closed-world opt-in) without re-introducing
  libclang and without new storage — pure model/query change.
- Both defaults stay byte-identical (`prune=False`, `assume_closed_world=False`);
  monotone-subset property preserved (3a/3b only ever shrink the candidate set).

Negative / costs:
- 3a is a **parity change**: `type_is_value` extraction + the two columns + v11
  migration must be mirrored in `ast.cpp` / `storage.{hpp,cpp}` / `records.hpp`;
  `#18 parity_check` reindex of graphlab is the gate.
- v11 makes the standard v10 `~/.cache/cidx/index.db` stale until reindexed.
- Closed-world is a *user-asserted precondition*; asserting it on a partial or
  un-`resolve`d index yields unsound narrowing — must be documented loudly in the
  API docstring and the senior-engineer contract.
- 3a measurement caveat: the self-index has ~0 headroom (all 25 virtual sites are
  in the vendored doctest header → genuinely ⊤). Before/after must be shown on
  `manifests/graphlab` + purpose-built fixtures (value member, value global,
  by-value return, cross-TU param) the Developer adds.

Follow-ups (for the senior-engineer implementation contract — NOT this ADR):
- Exact column DDL + `_migrate()` v10→v11 block + widened `add_call_arg` /
  `add_edge_site` signatures, mirrored Py/C++.
- `_classify_value_source` extension to compute `type_is_value` + populate
  `call_result.type_usr`; receiver value-ness on the edge_site write.
- `GraphQuery.call_sites_into` + `CallContext`; engine on-demand cross-TU union;
  `assume_closed_world` kwarg + the `prune`/closed-world interaction.
- Fixtures + hermetic GP-12.. tests (value member/global/return; closed-world
  cross-TU prune; closed-world-OFF stays ⊤; smart-pointer/ref/ptr stay ⊤).

## References

- Spec/blackboard: `~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md`
  (§"Phase 3 — Architecture Decision").
- ADR-002 (Phase-2 storage): `project/docs/adr-002-devirt-phase2-provenance-storage.md`
  / [[pages/decisions/adr-002-cidx-devirt-phase2-provenance-storage]].
- Code anchors (v10, `main` @ `21c204a`): `model.py:493` (`K_LIMIT`), `:607`
  (`_resolve_source`), `:723` (`gamma_for_site`), `:758` (`decide`); `query.py:705`
  (`edges_in`), `:1230` (`call_args`), `:218` (`Site`), `:254` (`CallArg`);
  `storage.py:resolve_pass` / `cross_repo_edges`; `cli.py:300` (`cmd_resolve`).
- Value-classification probe (2026-06-17, libclang 18.1.1): `/tmp/valprobe.*`,
  `/tmp/sp.*` (canonical-kind + USR-equality results quoted in §3a).
- Cognee: `task:devirt-callgraph-phase3`, `role:architect`.
</content>
</invoke>

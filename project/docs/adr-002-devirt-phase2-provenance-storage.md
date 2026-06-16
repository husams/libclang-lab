# ADR-002: Phase-2 argument/receiver provenance storage

Status: accepted
Date: 2026-06-16
Branch: feat/devirt-callgraph-phase2
Supersedes/relates: ADR-001 (Phase-1 sibling-method walk); spec
`~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md`; contract
`project/docs/design-devirt-phase2.md`.

## Context

Phase 2 prunes the conservative dispatch set Phase 1 attaches at each
`prunable=True` virtual call site. To do that soundly it needs, at index time,
the **value source** of (a) each virtual call's receiver and (b) each call
argument, so a flow-insensitive type-environment Γ can be reconstructed in a pure
Python query/model pass with NO libclang. Today the index records only
`edge_site.args_sig` — a stringified signature, not a typed/decl-keyed source —
so the motivating case `f(){ B b; top_rank(b); }` cannot be narrowed: nothing
ties the argument `b` (a `B`) to `top_rank`'s parameter `a`, and nothing records
that `a.rank()`'s receiver names `a`.

Forces / constraints (all verified against the v9 code on this branch):

- `edge_site` PK is `(edge_id, file_id, line, col)` (`storage.py:158`,
  `records.hpp:77`) — widening it would touch every reader.
- A call has **0..N** arguments → a 1-row-per-position child relation, not a
  fixed set of columns.
- A virtual call has exactly **one** receiver → at most a fixed handful of
  scalar fields per site.
- HARD parity rule: any `storage.py`/`ast.py` change must land in the C++ mirror
  (`storage.cpp`+`records.hpp` / `ast.cpp`+`parse.cpp`) on this same branch.
  `model.py`/`query.py` are Python-only and exempt.
- Migration must be additive: v9 DBs already exist (standard index
  `~/.cache/cidx/index.db`); the existing `_migrate()` pattern is
  CREATE-TABLE-IF-NOT-EXISTS + ALTER-ADD-COLUMN + flip a `changed` flag to bump
  the version (`storage.py:340-368`, `storage.cpp:509-523`).
- Cross-TU correctness matters: cidx has a `resolve` roll-up that joins per-TU
  indexes; provenance must survive in the DB so a caller in TU-X can bind a
  callee param analysed from TU-Y.

## Decision

**Option B — hybrid: `edge_site` gains scalar receiver-provenance columns, and a
new `call_arg` side table holds per-argument provenance.** Schema bumps **9 →
10**, additive only.

1. `edge_site` + three nullable columns (receiver of a virtual call):
   - `recv_src_kind  TEXT NULL` — `local | construct | member | global |
     call_result | this | unknown` (NULL for receiver-less calls).
   - `recv_type_usr  TEXT NULL` — USR of the receiver's **static** record type
     (keys the Phase-1 selection map; the static-type fallback).
   - `recv_decl_usr  TEXT NULL` — USR of the local/param/field the receiver
     *names* (the Γ-location key that carries a call-site binding into the
     dispatch decision).
2. New `call_arg` side table, PK `(edge_id, file_id, line, col, position)`
   (the `edge_site` PK + 0-based `position`), `WITHOUT ROWID`, columns
   `src_kind TEXT NOT NULL`, `type_usr TEXT`, `decl_usr TEXT`, `callee_usr TEXT`,
   plus `idx_call_arg_edge ON (edge_id)`. One row per non-`literal` positional
   argument (literal/builtin args contribute nothing to Γ and are skipped).
3. Mirror in C++ `records.hpp` (`EdgeSite` +3 `std::optional<std::string>`; new
   `struct CallArg`), `storage.cpp`/`storage.hpp` (`kSchemaVersion=10`, schema
   string, v9→v10 migration block, widened `add_edge_site`, new `add_call_arg`),
   and `ast.cpp` extraction. The `#18 parity_check` ctest re-indexes graphlab and
   golden-compares, validating extractor parity end-to-end.

The detailed column/DDL spec, the `classify_value_source` cursor-kind table, the
write-API signatures, and the Γ algorithm live in the implementation contract
`project/docs/design-devirt-phase2.md`; this ADR records only the cross-cutting
storage choice and its justification.

## Alternatives considered

- **Option A — extend `edge_site` with a structured `receiver_source` column +
  an `arg_provenance(edge_id,pos,kind,type_usr)` side table.** Functionally
  equivalent to B for arguments, but it folds the receiver's *kind*, *type*, and
  *named-decl* into one column (encoding/parsing cost, weaker typing) and names
  the arg table `arg_provenance` rather than `call_arg`. B splits the three
  receiver facets into first-class typed columns (cheap, no parsing) and gives
  the arg table the canonical `edge_site`-PK prefix so it joins 1:1 to a site.
  Trade-off: A is marginally fewer columns; B is clearer, directly joinable, and
  needs no in-column encoding. Rejected in favour of B for typing + join clarity.
- **Option C — recompute provenance in a post-index pass (no schema change).**
  Cheapest migration (none), but it must re-parse with libclang to recover
  receiver/arg expressions, and — decisively — it **loses cross-TU
  information**: a post-pass over one TU's AST cannot see a caller in another TU,
  so the `resolve` roll-up could never bind a param from a foreign caller. The
  motivating value (narrowing across the `f → top_rank` call boundary, including
  cross-TU) requires the provenance to be *stored* and joined. Rejected:
  violates the cross-TU correctness force; also re-introduces a libclang
  dependency into what should be a pure graph pass.

A schema-backed option (A or B) is strongly preferred over C precisely because
cidx's `resolve` roll-up depends on durable, joinable per-TU facts.

## Consequences

Positive:
- Receiver provenance is a fixed scalar set → no `edge_site` PK widening, no
  reader churn; existing `Site` SELECTs just add three columns.
- Arguments are modelled as their natural 1:N child; the `edge_site`-PK prefix
  gives a 1:1 join and `ON DELETE CASCADE` keeps it consistent with edge
  deletion.
- Purely additive (CREATE IF NOT EXISTS + ALTER ADD COLUMN): v9 DBs upgrade in
  place with no rebuild; new columns/table are simply empty until the next
  reindex repopulates from the AST (no backfill, matching the v6→v7 / v8→v9
  precedent).
- Provenance is persisted, so the cross-TU `resolve` roll-up can later bind
  params from foreign callers — the C-killing requirement.
- Γ pass stays pure Python over `query.py` reads; no libclang in Phase 2.

Negative / costs:
- Parity cost: schema + extractor changes must be mirrored in four C++ files in
  this branch; the `#18 parity_check` reindex is the gate.
- `call_arg` adds rows roughly proportional to (non-literal call arguments) — a
  new table to size on the self-index; mitigated by skipping `literal` args and
  the `idx_call_arg_edge` index. Measure after landing (Phase-2 design R4).
- Bumping to v10 makes the standard v9 `~/.cache/cidx/index.db` stale for
  structural graph navigation until reindexed (expected; flagged in the brief).

Follow-ups:
- Implement per the contract `project/docs/design-devirt-phase2.md` (storage,
  extractor, query reads, Γ engine, fixture `f(){ B b; top_rank(b); }`).
- Sound-monotone invariant is a test gate: GP-04/05/06/08 (⊤, unprunable, empty
  intersection, k-limit) must all KEEP_ALL; GP-10/11 + the 194-suite must stay
  byte-identical for `prune=False`.

## References

- Spec/blackboard: `~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md`
  (§"What must be captured at index time", §"Phase 2 — Implementation Plan").
- Phase-2 implementation contract: `project/docs/design-devirt-phase2.md`.
- Phase-1 design: `project/docs/design-devirt-phase1.md`.
- Code anchors (v9, this branch): `storage.py:158` (`edge_site` DDL), `:340`
  (`_migrate`), `:1020` (`add_edge_site`); `ast.py:520-554` (CALL_EXPR descent);
  `query.py:207` (`Site`), `:682/:714` (edge_site SELECTs);
  `records.hpp:77` (`EdgeSite`); `storage.cpp:509-523` (migrate/version).
- Cognee: `task:devirt-callgraph-phase2`, `role:architect`.

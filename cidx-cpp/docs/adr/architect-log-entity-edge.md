# Architect log — ADR-008 entity_edge (Layer-1 materialization)

Date: 2026-06-21
Role: architect
Task: cidx-entity-edge
Deliverable: cidx-cpp/docs/adr/ADR-008-entity-edge.md (Status: accepted)

## What was decided (not relitigated — recorded the LOCKED contract + the open architectural choices)
- Roll-up placement: new global phase `materialize_entity_edges()` on
  `Storage.resolve_pass()` (Py storage.py:1777, C++ storage.cpp:1950), after
  `rollup_edge_counts`; DB-only, no reparse; `index` does NOT write entity_edge.
  Global (not per-TU) because Layer-1 edges roll members up to owning records and
  span TUs (needs the resolved whole-index view).
- Re-materialize: DELETE FROM entity_edge + full idempotent rebuild each resolve;
  per-entity invalidation deferred (OQ-10); FK ON DELETE CASCADE auto-cleans.
- Expansion: entity_edge_kind (11) + create_form (1-2 shipped, 3-8 reserved) are
  int enums that extend with NO schema change (new ids only) — the stability
  mechanism for the Layer-2 substrate. New COLUMN would still bump; new VALUES never.
- Type-classification kernel: ONE shared `classify_referent(type)` helper drives
  has-a (composes/aggregates/associates) + factory-create + by-value-return;
  unwraps unique/shared/weak/raw-ptr/ref/container, recovers referent B via
  template_arg.ref_id (ADR-004) — pure-DB roll-up, NEEDS NO REPARSE, must be at
  byte-identical Py↔C++ parity (it writes the on-disk table, NOT model.py-exempt).
- Soundness: partial=1 on any ⊤-incomplete derivation (factory/by-value-return/
  virtual-dispatch/unresolved unwrap); never a confident-wrong edge.
- Template-instance collapse (OQ-3): collapse Foo<int>/Foo<double> onto primary
  Foo at entity altitude (kernel roll-up rule via instantiates/specializes).
- Versioning + build order: PR1 = Layer-0 extraction (NEW/DELETE/CONSTRUCT/
  TEMPORARY_OBJECT handlers + factory template-arg + by-value-return), MINOR
  0.15.0→0.16.0, NO schema. PR2 = v17 entity_edge table + roll-up + readers +
  model.py, MINOR 0.16.0→0.17.0, schema v16→v17. Schema moves ONLY in PR2.

## Verified anchors before writing
- Py SCHEMA_VERSION=16, C++ kSchemaVersion=16 (v16 consumed by symbol-kind-as-int
  v0.15.0) — confirms v17 target.
- resolve_pass exists Py storage.py:1777 / C++ storage.cpp:1950, both call
  rollup_edge_counts first — confirmed the hook point.
- Versions Py cli.py:68 / C++ args.hpp:27 both "0.15.0".
- ADR-004 has no adr/ file (it's a handoff dir + cidx memory) — referenced via
  memory slug + planning page, not a local ADR path.

## Notes / risks flagged in ADR
- Parity gate (parity_check.sh) does NOT yet exercise entity_edge — must grow.
- creates/destroys genuinely blocked on PR1 (foundation §3.7 OQ-1: delete invisible,
  factory route dead today) — that's why two PRs, not one.
- Query engine that reads entity_edge is explicitly OUT of scope (ADR-007 lineage).

## Validation
ADR-008 Status: accepted (no proposed ADRs at return → no ADR_UNRESOLVED).
adr-count: 1.

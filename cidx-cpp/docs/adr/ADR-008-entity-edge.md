# ADR-008: Layer-1 `entity_edge` materialization (the UML/ER entity graph)

Status: accepted
Date: 2026-06-21
Supersedes: none
Relates-to: schema v17 (new), ADR-004 (template-instantiation nodes — own-USR
instances + `template_arg`, schema v13), ADR-007 (C++ graph read-side — the
future query engine that reads `entity_edge`)

## Context

cidx today stores a **Layer-0** symbol graph: USR-keyed `symbol` rows + 9
mechanical `edge` kinds (`calls`/`inherits`/`field_of`/`method_of`/`uses`/…)
with per-site `edge_site` provenance (schema v16). This is precise but
*low-altitude* — it answers "who calls `foo`", not "`Order` owns `LineItem`" or
"`Foo` realizes `Repository`".

The goal (`pages/planning/cidx-code-model-foundation.md` §1, LOCKED by Husam
2026-06-20) is to materialize the **primitive layer** of a layered semantic
model: Layer-0 AST facts (EDB) → **Layer-1 design entities + UML/ER relations**
(this ADR) → Layer-2 user-defined domain concepts (the DSL, later). A separate
high-level query engine is built *on top of* these primitives later — **building
the query engine is explicitly out of scope here.** Layer-1 must satisfy four
properties from §1.1: **altitude** (carry design-level facts), **precision +
traceability** (every derived fact traces to provenance), **soundness under
incompleteness** (surface ⊤/unknown, never a confident-wrong edge), and **scale**
(materialized/pre-computed, not re-derived per query — the materialize-first
decision in `pages/planning/cidx-entity-relationship-graph.md` §6).

This ADR records the **architecture of the materialized Layer-1 primitives**. The
concept/relation contract is LOCKED in the two planning pages — the decisions
below are the genuinely architectural choices left open (roll-up placement,
re-materialize semantics, enum-expansion strategy, the type-classification
kernel, the soundness flag, template-instance collapse, versioning + build
order). **This ADR does not relitigate any locked decision.**

### Hard constraints (inherited project rules)
- **Python↔C++ byte-identical parity** on the schema string AND the roll-up
  behavior (HARD RULE, `cidx-python-cpp-parity`). The schema string moves in
  lockstep: Py `storage.py:35` `SCHEMA_VERSION` + the `_SCHEMA` literal; C++
  `storage.hpp:30` `kSchemaVersion` + `storage.cpp:27` `kSchema` — byte-identical.
- **Product-version bump** in both ports (Py `cli.py:68` `VERSION`, C++
  `args.hpp:27` `kVersion`), currently `0.15.0`, byte-identical
  (`cidx-version-bump-rule`).
- Schema is currently **v16** (consumed by symbol-kind-as-int, v0.15.0); this
  work targets **v17** and is **additive** (new table + new lookup table only).

## Decision

### LOCKED contract (recorded, not re-decided)

- **Entity = a record/enum SYMBOL.** No separate `entity` table. `entity_edge`
  endpoints reference `symbol(id)`, filtered to
  `symbol.kind ∈ {class, struct, union, enum}` (enum included per D-4). R-1
  retired: the two `uses` relations are distinguished by *table + endpoint kind*
  (any symbol → `edge`; record/enum symbol → `entity_edge`), not by name.
- **New table `entity_edge`, ALL-INTEGER / ZERO TEXT.** The only strings in the
  feature are the 11 rows of the `entity_edge_kind` lookup table.
- **11 edge kinds:** 1 generalizes, 2 realizes, 3 specializes, 4 composes,
  5 aggregates, 6 associates, 7 creates, 8 uses, 9 destroys, 10 nests,
  11 befriends.
- **Schema (reproduced faithfully from foundation §3.6):**

```sql
CREATE TABLE entity_edge_kind (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);  -- the ONLY strings, 11 rows
-- 1 generalizes,2 realizes,3 specializes,4 composes,5 aggregates,6 associates,7 creates,8 uses,9 destroys,10 nests,11 befriends
CREATE TABLE entity_edge (
    id INTEGER PRIMARY KEY,
    src_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,   -- record/enum symbol (entity = record symbol; no entity table)
    dst_id INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,   -- record/enum symbol
    kind INTEGER NOT NULL REFERENCES entity_edge_kind(id),
    count INTEGER NOT NULL DEFAULT 1,
    via_member_id INTEGER REFERENCES symbol(id) ON DELETE CASCADE,     -- carrying field (has-a) / method (dep); ALSO the role (look up its name)
    multiplicity INTEGER,    -- enum: 1=one, 2=0..1, 3=0..*, 4=N
    access INTEGER,          -- enum: 0=public, 1=protected, 2=private  (generalizes/realizes)
    is_virtual INTEGER,      -- 0/1: virtual base (diamond inheritance)
    create_form INTEGER,     -- enum: 1=ctor_call,2=return,(later 3=value,4=temp,5=heap,6=factory,7=copy,8=move) — creates/destroys only
    partial INTEGER NOT NULL DEFAULT 0,  -- 0/1: ⊤-partial soundness flag (success-criterion 3)
    UNIQUE (src_id, dst_id, kind, via_member_id)
);
CREATE INDEX idx_entity_edge_src ON entity_edge(src_id, kind);
CREATE INDEX idx_entity_edge_dst ON entity_edge(dst_id, kind);
```

- **No `role` column** (redundant with `via_member_id` — the member's name is one
  join away). **No `entity` table** (entity = the record/enum symbol).
- **`creates`/`destroys` = the RICH version** (full `create_form` taxonomy),
  unblocked by a prerequisite Layer-0 extraction PR (build order below). No
  degraded half-feature.

### What this ADR DECIDES (the open architectural choices)

**1. Roll-up placement — rides `resolve_pass()` as a new GLOBAL phase.**
`entity_edge` is materialized by a new phase `materialize_entity_edges()` invoked
from `Storage.resolve_pass()` (Py `storage.py:1777`; C++ `storage.cpp:1950`)
**after** `rollup_edge_counts()` and before/around `cross_repo_edges()`. It is a
`cidx resolve`-style, **DB-only, NO-reparse** pass.
- **Global, not per-TU.** Layer-1 relations are cross-entity and cross-TU
  roll-ups: `composes(A,B)` joins `field_of` against the field's declared type;
  `uses(A,B)` rolls a method's `calls` edges up to the method's owning record;
  `creates(A,B)` rolls a constructor/`new`/factory site up to the *enclosing
  method's owner*. These need the resolved, whole-index view that only the global
  `resolve` pass has (cross-TU stubs already backfilled, edge counts rolled up).
  A per-TU pass at `index` time would see partial owners and unresolved peers.
- **`index` does NOT write `entity_edge`.** `index` stays the Layer-0 extractor
  (symbols + edges + sites); `resolve` is the Layer-1 materializer. This mirrors
  the existing split (counts/cross-repo roll-up already live only in `resolve`)
  and keeps `index` hot and incremental.

**2. Re-materialize semantics — full idempotent rebuild every resolve.**
`materialize_entity_edges()` begins with `DELETE FROM entity_edge` and re-derives
all 11 kinds from scratch. This makes `resolve` idempotent and avoids stale rows
after a reindex. Per-entity incremental invalidation is **deferred (OQ-10)** —
full rebuild is correct and fast enough at the resolve cadence; optimize only if
measured slow. `entity_edge.src/dst/via_member` carry `ON DELETE CASCADE` on
`symbol(id)`, so dropping a symbol (e.g. on a file delete) auto-cleans its
entity edges between resolves.

**3. Expansion strategy — int enums extend with ZERO schema change.**
`entity_edge_kind` (currently 11) and `create_form` (currently 1–2 shipped, 3–8
reserved) are integer enums. New relation kinds or create forms are added by
*seeding a new id only* — no `ALTER TABLE`, no schema-version bump. This is the
deliberate mechanism that keeps Layer-1 a **stable substrate** for the Layer-2
DSL: Layer-2 rules bind to kind ids, and the kernel can grow create-site
taxonomy without breaking stored data or the reader API. (A *new column* would
still be a schema bump; new *enum values* never are.)

**4. The type-classification kernel — ONE shared helper, DB-only, runs in the
roll-up.** The has-a family (`composes`/`aggregates`/`associates`), factory
`creates`, and by-value-return `creates` all share a single
`classify_referent(type)` helper that unwraps the declared type
(`unique_ptr<B>` → owns B; `shared_ptr<B>` / container-of-shared → shares;
raw `B*` / `B&` / `weak_ptr<B>` → borrows; `vector<B>`/`B[]` → multiplicity `0..*`
of the element entity) and recovers the **referent entity B** via the stored
`template_arg.ref_id` (ADR-004) or the canonical-spelling → `symbol` join — never
by string-parsing USRs.
- **It is pure-DB roll-up logic** over already-stored facts: `symbol.type`
  spellings, `template_arg` rows (ADR-004), and `field_of`/`method_of`/`calls`
  edges. **It needs NO reparse** — confirmed: every input the kernel reads is a
  persisted Layer-0 row, so it runs entirely inside `resolve` against the DB.
- **Parity:** because it is roll-up logic (DB rows in, `entity_edge` rows out),
  it MUST live at byte-identical behavior in both `materialize_entity_edges()`
  implementations (Py `storage.py`, C++ `storage.cpp`) and is covered by the
  parity gate. It is NOT model.py-style Python-only read logic — it writes the
  on-disk table, so the parity rule binds.

**5. Soundness / `partial` — never a confident-wrong edge.** Any derivation that
depends on a libclang-incomplete fact sets `partial = 1`: factory `creates`
where the `make_unique<B>` template arg was only heuristically recovered,
by-value-return `creates` through a dependent/template return, virtual-dispatch
`uses` resolved to a target *set*, or any container/smart-pointer unwrap the
kernel could not fully resolve. A confidently-derived edge (direct value field,
direct base) gets `partial = 0`. This is success-criterion 3: surface ⊤, never a
false positive.

**5b. `realizes` XOR `generalizes` — inheritance emits exactly one.** A base edge
(Layer-0 `inherits(A,B)`) becomes EITHER `realizes(2)` OR `generalizes(1)`, never
both. The discriminator is the **Interface predicate** on the base B (foundation
§2.2): `Interface(B) ⟺ B is a record whose methods are ALL pure-virtual AND which
has NO data members`. If `Interface(B)` → emit `realizes(2)` (A implements an
interface); otherwise (B carries state and/or non-pure-virtual implementation) →
emit `generalizes(1)` (A specializes a concrete/abstract base). The two are
mutually exclusive per base. The kernel computes `Interface(B)` once from B's
`method_of`/`field_of` members + `is_pure` flags (all DB facts — no reparse) and
caches it for reuse across A's bases.

**6. Template-instance collapse (OQ-3) — collapse onto the primary by default.**
At entity altitude, `Foo<int>` and `Foo<double>` collapse onto the primary
template `Foo` for `entity_edge` endpoints (a roll-up rule in the kernel: map an
instance/specialization symbol to its primary via the `instantiates`/
`specializes` edge before emitting). This keeps the UML/ER graph at design
altitude (one `Foo` box, not one per instantiation). The Layer-0 per-instance
nodes (ADR-004) remain intact for callers who want instance granularity.

**7. Versioning + two-PR build order.**
- **PR1 — Layer-0 extraction (additive, MINOR `0.15.0 → 0.16.0`, NO schema
  change).** Add handlers for `CXX_NEW_EXPR` / `CXX_DELETE_EXPR` /
  `CXX_CONSTRUCT_EXPR` / `CXX_TEMPORARY_OBJECT_EXPR` + factory template-arg
  recovery (`make_unique<B>` / `make_shared<B>` → recover B from the template
  arg) + a by-value-return flag, in BOTH `project/indexer/clang/ast.py` and
  `cidx-cpp/src/clangx/ast.cpp`. This closes the OQ-1 gap (foundation §3.7): today
  `delete`/destroy is invisible and the factory route is dead, so creates/destroys
  cannot be derived richly. PR1 emits the richer, *distinguishable* Layer-0
  create/destroy facts that PR2's roll-up reads. Unblocks PR2.

  **PR1 persists the construction/destruction FORM as DISTINCT Layer-0 `edge`
  kinds (not a `call_arg.create_form` column).** Today the form is LOST: a `new B`
  and a ctor-call both collapse to a single `calls → constructor` edge
  (`_classify_value_source` merges them to "construct"), so a `create_form` column
  on `call_arg` cannot recover it. PR1 instead SEEDS new `edge_kind` ids for the
  forms — construct-value, construct-temp, construct-heap, construct-copy,
  construct-move, factory-construct, and destroy — and emits the construction
  site as the matching kind. Rationale (decision recorded here):
  - **(a) Default construction `B b;` has NO arguments → no `call_arg` row**, so a
    `create_form` column on `call_arg` has nowhere to attach. A distinct edge kind
    on the construct/destroy edge itself always has a home.
  - **(b) `edge_kind` is a SEED-ONLY / no-FK table since v0.15.0** — new kinds need
    ZERO schema change. This is exactly this ADR's "int enums extend with no schema
    change" expansion principle (decision 3), applied at Layer-0: PR1 stays
    schema-free, and `entity_edge` stays locked at v17 in PR2.

  **Layer-0 → Layer-1 form mapping (PR2 reads this).** PR2's roll-up maps the new
  Layer-0 construct/destroy `edge.kind` → `entity_edge.create_form` (1–8) when it
  rolls a construction site up to the enclosing method's owner record:
  construct-value→3, construct-temp→4, construct-heap→5, factory-construct→6,
  construct-copy→7, construct-move→8, and the ctor-call edge (when no richer form
  applies)→1; destroy → `kind = destroys(9)`. **By-value-return (form 2) is NOT a
  Layer-0 edge** — there is no ctor cursor under RVO; it stays *derived in PR2* from
  the method's return type via the type-classification kernel (decision 4), as
  noted there. So PR1 needs only `edge_kind` seed rows (NO schema bump); only PR2
  introduces the v17 `entity_edge` schema.
- **PR2 — v17 `entity_edge` (additive, MINOR `0.16.0 → 0.17.0`, schema
  v16 → v17).** Schema (Py `SCHEMA_VERSION` 16→17 + `_SCHEMA`; C++
  `kSchemaVersion` + `kSchema` BYTE-IDENTICAL) + `entity_edge_kind` seed +
  `materialize_entity_edges()` writing all 11 kinds (incl. rich
  `creates`/`destroys` with full `create_form`) + readers + `model.py` typed
  accessors (Python-only, parity-exempt). Validate on `manifests/graphlab`
  (`graphlab-test-project`) before lock.
- **Schema moves only in PR2** (v16 → v17); PR1 is extraction-only. Both are
  MINOR because each is purely additive (new facts / new table); neither breaks
  the CLI surface, reader API, USR semantics, or existing on-disk format.

## Alternatives considered

- **A1 — Roll-up at `index` time (per-TU) instead of in `resolve` (global).**
  Rejected: Layer-1 relations roll a member's edges up to its owning record and
  span TUs (cross-TU stubs, `make_unique<B>` whose B is defined in another TU);
  a per-TU pass sees unresolved peers and partial owners, producing wrong or
  missing edges. The whole-index view exists only after `resolve` rolls up counts
  and backfills cross-repo stubs.
- **A2 — A separate `entity` table (id-mapped) instead of filtering `symbol`.**
  Rejected (R-1 retired): an entity *is* a record/enum symbol; a parallel table
  duplicates identity, adds a join, and risks drift. `symbol.kind ∈
  {class,struct,union,enum}` is the entity predicate; `symbol(id)` is the key.
- **A3 — Incremental per-entity invalidation instead of full DELETE+rebuild.**
  Rejected for now (OQ-10): correctness-first. Full rebuild on each resolve is
  trivially idempotent and avoids a stale-edge invalidation matrix; revisit only
  with a measured perf problem at multi-repo scale.
- **A4 — Degraded `creates`/`destroys` from existing `uses`/ctor-`calls` edges
  (no Layer-0 PR).** Rejected by Husam (foundation §3.7): today `delete` is
  invisible and factory args are unrecovered, so a no-extraction roll-up would
  ship a confidently-wrong/incomplete create graph — violating
  success-criterion 3. Do the extraction first; ship the rich version.
- **A5 — TEXT columns for role / multiplicity / access for readability.**
  Rejected (Husam 2026-06-21): repeated strings are storage-expensive at scale;
  every attribute is an int enum or an FK, the single canonical string per kind
  lives once in `entity_edge_kind`, and roles are recovered by joining
  `via_member_id` back to `symbol`.

## Consequences

Positive: `entity_edge` gives cidx the design-altitude (UML/ER) primitive layer —
the substrate a Layer-2 DSL / query engine reads without re-walking the AST; the
all-integer schema is compact and extends to new kinds/create-forms with no
schema change; the shared type-classification kernel keeps the has-a + create
families consistent in one place; `partial` preserves the project's ⊤-soundness
discipline.

Negative / costs: **a full reindex + `resolve` is required** to populate
`entity_edge` (v17 migration is additive but the table is empty until a resolve
runs). The roll-up adds Python↔C++ parity surface — the type-classification
kernel + `materialize_entity_edges()` must stay byte-identical in both ports
(parity gate must grow `entity_edge` coverage; today's `parity_check.sh` does not
exercise it). The query engine that reads `entity_edge` is **not** delivered here.

Acceptance fixture (the plan owns it): `creates`/`destroys` require a record/enum
SRC (the edge rolls a construction site up to the *enclosing method's owner*), so
a method-scoped `new`/`delete` fixture is needed for acceptance — graphlab's
current `new`/`delete` sites live in FREE functions (no owning record → no
`entity_edge` src). A new/extended graphlab fixture with a class method that
heap-allocates and deletes another entity must exist before PR2 lock.

Stays ⊤-partial (`partial = 1`, by design): factory `creates`
(`make_unique<B>`/`make_shared<B>` template-arg heuristic), by-value-return
`creates` through dependent/template returns, virtual-dispatch `uses` (target
set, not a single edge), and any smart-pointer/container unwrap the kernel cannot
fully resolve. Method-template instances remain a known libclang gap (ADR-004).

Follow-ups (deferred, NOT in scope): the Layer-2 concept/relation DSL evaluator
(`pages/planning/cidx-concept-relation-dsl.md`); `depends_on(Module→Module)`
roll-up (R-4: derive on demand first, materialize only if architecture queries
get slow); per-entity incremental invalidation (OQ-10); promoting `includes`
(file graph, G-6) and `Type`/`Parameter`/`LocalVariable` first-class nodes
(D-1/D-2) needed for the `of_type`/`returns`/`names` join path.

## References

- Foundation (LOCKED): `pages/planning/cidx-code-model-foundation.md` §1
  (goal/scope), §2 (concept catalog), §3 (relation catalog + D-1..D-4 / R-1..R-4),
  §3.6 (v17 schema), §3.7 (OQ-1 resolution + two-PR build order).
- Layer-1 design: `pages/planning/cidx-entity-relationship-graph.md` §3–§5
  (rule sketches + type-classification kernel), §6 (materialize-first decision).
- Layer-2 (downstream): `pages/planning/cidx-concept-relation-dsl.md`.
- ADR-004 (template instances): own-USR instances + `template_arg.ref_id` (the
  kernel's referent-recovery key), schema v13 — cidx memory
  `cidx-template-instantiation-nodes`.
- ADR-007: the C++ graph read-side / future query engine that will read
  `entity_edge` (`cidx-cpp/docs/adr/ADR-007-cpp-graph-port.md`).
- Code anchors — roll-up hook: Py `project/indexer/storage.py:1777`
  (`resolve_pass`), `:1743` (`rollup_edge_counts`), `:1754` (`cross_repo_edges`);
  C++ `cidx-cpp/src/storage/storage.cpp:1950` (`resolve_pass`), `:1911`
  (`rollup_edge_counts`). Schema: Py `storage.py:35`; C++ `storage.hpp:30` +
  `storage.cpp:27`. Layer-0 extraction site: `project/indexer/clang/ast.py`
  (`_body_descent`, `_emit_type_use`), `cidx-cpp/src/clangx/ast.cpp`. Versions:
  Py `cli.py:68`, C++ `args.hpp:27`.
- Cognee: `task:cidx-entity-edge`, `role:architect`.

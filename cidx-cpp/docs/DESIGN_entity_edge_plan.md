# DESIGN — Layer-1 `entity_edge` implementation plan

Status: build-ready
Date: 2026-06-21
Role: senior-developer
Source-of-truth architecture: `cidx-cpp/docs/adr/ADR-008-entity-edge.md` (Status: accepted)
Companion log: `cidx-cpp/docs/adr/architect-log-entity-edge.md`
Foundation (LOCKED): `pages/planning/cidx-code-model-foundation.md` §1–§3.7

This plan turns ADR-008 into two independently-buildable, independently-reviewable
PRs. It is implementation guidance only — **it writes no code, no fixtures, and runs
no git.** Every file:line below was confirmed live against git HEAD on 2026-06-21.

The LOCKED contract (concepts D-1..D-4, relations R-1..R-4, the 11 kinds, the
all-integer `entity_edge` schema, realizes-XOR-generalizes, rich creates/destroys,
resolve-style DB-only roll-up, schema target v17) is NOT relitigated here — see
ADR-008 §"LOCKED contract". The query engine that reads `entity_edge` is OUT OF
SCOPE.

---

## 0. Ground-truth anchors (re-verified live, git HEAD)

| Thing | Python | C++ |
|---|---|---|
| Product version (`0.16.0`) | `project/indexer/cli.py:68` `VERSION` | `cidx-cpp/src/cli/args.hpp:27` `kVersion` |
| Schema version (`16`) | `project/indexer/storage.py:35` `SCHEMA_VERSION` | `cidx-cpp/src/storage/storage.hpp:30` `kSchemaVersion` |
| Schema string literal | `storage.py:68` `_SCHEMA` (f-string) | `cidx-cpp/src/storage/storage.cpp:27` `kSchema` (raw string) |
| `edge_kind` seed | `storage.py:173–176` (inside `_SCHEMA`) | `storage.cpp:132` (inside `kSchema`) |
| in-code edge-name map (QUERY layer) | `query.py:56` `EDGE_KINDS` (→ `EDGE_NAMES` at `:67`) | `query.hpp:58` `edge_names_map()` |
| `symbol_kind` seed (same pattern) | `storage.py:158–163` | mirror in `kSchema` |
| `executescript(_SCHEMA)` on every open | `storage.py:368` | `storage.cpp:553` (`db_.exec(kSchema)`) |
| `resolve_pass()` (roll-up hook) | `storage.py:1777` | `storage.cpp:1950` |
| `rollup_edge_counts()` | `storage.py:1743` | `storage.cpp:1911` |
| `cross_repo_edges()` | `storage.py:1754` | `storage.cpp:1922` |
| `add_edge(...)` | `storage.py:1599` | `storage.cpp` (mirror) |
| `_classify_value_source` / `classify_value_source` | `clang/ast.py:665` | `clangx/ast.cpp:829` |
| ctor `CALL_EXPR` branch | `ast.py:717–725` | `ast.cpp` ~`880` |
| `CXX_NEW_EXPR` branch | `ast.py:731–733` | `ast.cpp:890–894` (literal `(CXCursorKind)134`) |
| `_body_descent` / `body_descent` | `ast.py:1031` | `ast.cpp:1682` (+ `body_descent_visitor` `:1317`) |
| `_emit_type_use` / `emit_type_use` | `ast.py:497` | `ast.cpp:178` |
| `_emit_overloaded_calls` / `emit_overloaded_calls` | `ast.py:842` | `ast.cpp:1132` |
| `template_arg.ref_id` (ADR-004 referent key) | `storage.py:216–223` | mirror in `kSchema` |

**OQ-1 confirmed live:** `_classify_value_source` collapses `new B` (`CXX_NEW_EXPR`,
ast.py:731) AND a ctor `CALL_EXPR` (ast.py:717–725) to the SAME `src_kind`
`"construct"`. There is NO `CXX_DELETE_EXPR` / `CXX_CONSTRUCT_EXPR` /
`CXX_TEMPORARY_OBJECT_EXPR` handler. The C++ mirror returns the same `"construct"`
string for `(CXCursorKind)134` (ast.cpp:891–893). The construction FORM is lost
today, and `delete` is invisible — exactly why PR1 exists.

---

## A. PR BREAKDOWN

### PR1 — Layer-0 extraction (product `0.16.0 → 0.17.0`, MINOR; `SCHEMA_VERSION` stays 16; parity-gated schema-STRING change)

**Goal.** Make the construction/destruction FORM a *distinguishable* Layer-0 fact,
so PR2's roll-up can map it to `entity_edge.create_form` (1–8). Persist the form as
**distinct new `edge_kind` SEED ids** — NOT a `call_arg.create_form` column (a
default-ctor `B b;` has no `call_arg` row, so a column has nowhere to attach;
ADR-008 §7 rationale (a)).

**Why no `SCHEMA_VERSION` bump but still a parity-gated change.** `edge_kind` is a
seed-only, no-FK table since v0.15.0. New kind ids need NO `ALTER TABLE`. BUT the
seed rows live INSIDE the parity-gated `_SCHEMA`/`kSchema` literal, so appending
them changes the schema-STRING *content* — the Py↔C++ byte-identical diff MUST be
re-run for PR1 (ADR-008 §7 PR1 rationale (b)). `executescript(_SCHEMA)` runs on
every `Storage` open (storage.py:368 / storage.cpp:553) with `INSERT OR IGNORE`, so
existing v16 DBs gain the new form rows on next open with no migration.
`SCHEMA_VERSION` literals stay `16` in BOTH ports; only the seed lines move.

#### A1. New `edge_kind` seed ids (proposed)

Append to the `edge_kind` seed list (current max id = 9) in BOTH
`storage.py:173–176` and the `kSchema` mirror at `storage.cpp:132`, byte-identical:

```
(10,'construct-value'),   -- B b;  / B b(x);          -> entity_edge.create_form 3
(11,'construct-temp'),    -- B{}   / B(x) temporary   -> create_form 4
(12,'construct-heap'),    -- new B(...)                -> create_form 5
(13,'construct-copy'),    -- B b2(b1);  copy-ctor      -> create_form 7
(14,'construct-move'),    -- B b2(std::move(b1));      -> create_form 8
(15,'factory-construct'), -- make_unique<B>/make_shared<B> -> create_form 6 (partial=1)
(16,'destroy')            -- delete p; / explicit dtor  -> entity_edge kind=destroys(9)
```

These are Layer-0 `edge` rows: `src = enclosing function/method symbol`,
`dst = the constructed/destroyed record symbol B`, mirroring how `uses`(7) /
`calls`(1) are emitted from a body. PR2 rolls them up to the enclosing method's
OWNER record (decision 4). `create_form 1` (plain ctor_call) and `create_form 2`
(by-value-return) are NOT new Layer-0 kinds: `1` is the existing ctor `calls` edge
when no richer form applies; `2` is derived in PR2 from the return type (no ctor
cursor exists under RVO).

> The in-code edge-name map must gain ids 10–16 alongside the seed, so
> `cidx graph`/stats render the names. **This map lives in the QUERY layer, NOT
> storage** — Python = the hardcoded `EDGE_KINDS` dict at
> `project/indexer/query.py:56` (extend it; `EDGE_NAMES` auto-derives from it at
> `query.py:67`, the single source of truth); C++ = `edge_names_map()` at
> `cidx-cpp/src/graph/query.hpp:58` (extend it). The `storage.py:168` /
> `storage.cpp:127` lines are only a COMMENT pointing at this map — there is no
> map in the storage layer; do NOT edit storage for the names. Update `query.py`
> and `query.hpp` in lockstep (read-side counterpart of the seed — decision 3).
> This is a parity-bound read-side change; the schema-string diff does NOT catch
> it, so it has its own parity-checklist row.

#### A2. Extraction handlers (both ports, byte-behavior parity)

Add to `_classify_value_source` (ast.py:665) / `classify_value_source`
(ast.cpp:829), and wire emission in `_body_descent` (ast.py:1031) /
`body_descent_visitor` (ast.cpp:1317):

1. **`CXX_NEW_EXPR`** (ast.py:731 / ast.cpp:890, literal `134`): today returns
   `"construct"`. Emit Layer-0 edge kind **12 (construct-heap)** to the `new`'d
   record B (recover B via `_record_usr_of_type(peeled.type)` → `lookup_symbol`).
   Keep the existing `"construct"` `src_kind` for the Γ/devirt provenance path
   (do NOT regress it) and ALSO emit the form edge.
2. **`CXX_DELETE_EXPR`** (NEW; libclang `CXXDeleteExpr` = `135`): recover the
   operand's static record type → emit Layer-0 edge kind **16 (destroy)** to B.
   Operand recovery = peel the delete's child expression, classify its pointee
   via `_record_usr_of_type`.
3. **`CXX_CONSTRUCT_EXPR`** (NEW; `CXXConstructExpr`): a direct ctor without
   `new`. Discriminate by the ctor's single-param signature: `const B&` = copy →
   kind **13**; `B&&` = move → kind **14**; else value-init → kind **10
   (construct-value)**.
4. **`CXX_TEMPORARY_OBJECT_EXPR`** (NEW; `CXXTemporaryObjectExpr`): a temporary
   `B{}` / `B(x)` → kind **11 (construct-temp)**.
5. **Factory recovery** — a `CALL_EXPR` whose callee is
   `std::make_unique`/`std::make_shared` (callee in `<memory>`, a system header,
   so the callee USR may be a stub): recover the constructed B from the **template
   argument**, not the (unrecovered) return type. Use the stored
   `template_arg.ref_id` (ADR-004) on the call's type node, or the
   canonical-spelling → `symbol` join. Emit kind **15 (factory-construct)**.
   This route is `partial=1` material in PR2.
6. **By-value-return** — NOT a Layer-0 edge (no ctor cursor under RVO). PR1 emits
   nothing for it; it is derived in PR2 from the method's return type via
   `classify_referent`. Listed only to state that PR1 deliberately defers it.

**Lookup-only discipline (matches `_emit_type_use` ast.py:497):** form edges are
emitted ONLY when B is already an indexed record symbol. No stubs minted (a
`new int` / `new <stdlib-type>` produces no entity edge later, so no Layer-0
noise). This keeps the new edges clean for the roll-up.

#### A3. PR1 version + parity

- Bump `VERSION` `0.16.0 → 0.17.0` (`cli.py:68`) and `kVersion` (`args.hpp:27`),
  byte-identical. MINOR (purely additive new facts).
- `SCHEMA_VERSION` / `kSchemaVersion` STAY 16.
- Re-run the schema-string byte-identical Py↔C++ head-to-head diff (seed lines
  moved) — this gate runs in BOTH PRs.

PR1 ships nothing that reads the new edges; it is independently reviewable as
"new distinguishable construct/destroy Layer-0 facts + their seed".

---

### PR2 — v17 `entity_edge` (product `0.17.0 → 0.18.0`, MINOR; schema `v16 → v17`)

PR2 **depends on PR1** (it reads the form edges 10–16). It adds the v17 schema, the
seed, the global `materialize_entity_edges()` roll-up phase, the shared
`classify_referent` kernel, all 11 entity-edge kinds, readers, and `model.py`
typed accessors.

#### B-schema. The v17 schema (byte-identical Py `_SCHEMA` ↔ C++ `kSchema`)

Append the two tables EXACTLY as reproduced in ADR-008 §"Schema" (`entity_edge_kind`
+ `entity_edge` + the two indexes) into the `_SCHEMA` f-string at `storage.py:68`
and the `kSchema` literal at `storage.cpp:27` — they MUST render byte-identical
(verify with a head-to-head diff). The lookup-table seed:

```sql
INSERT OR IGNORE INTO entity_edge_kind (id, name) VALUES
  (1,'generalizes'),(2,'realizes'),(3,'specializes'),(4,'composes'),
  (5,'aggregates'),(6,'associates'),(7,'creates'),(8,'uses'),
  (9,'destroys'),(10,'nests'),(11,'befriends');
```

Bump `SCHEMA_VERSION` / `kSchemaVersion` `16 → 17` in lockstep. Additive (new table
+ new lookup table only) — no `ALTER`, no migration logic beyond `executescript`
picking up the new `CREATE TABLE IF NOT EXISTS`.

#### B-rollup. `materialize_entity_edges()` — new global phase

Wire into `resolve_pass()` (Py `storage.py:1777`, C++ `storage.cpp:1950`) AFTER
`rollup_edge_counts()` (Py `:1784` / C++ `:1952`) and around `cross_repo_edges()`.
DB-only, NO reparse, GLOBAL. First statement: `DELETE FROM entity_edge` (full
idempotent rebuild each resolve — ADR-008 decision 2). The phase derives all 11
kinds from already-stored Layer-0 rows.

Per-kind derivation (pure SQL/DB joins over `symbol`/`edge`/`field_of`/`method_of`/
`inherits`/`template_arg` + the PR1 form edges):

| Kind | Source roll-up | Notes |
|---|---|---|
| 1 generalizes | `inherits(A,B)` where `NOT Interface(B)` | `access`/`is_virtual` from `edge.base_access`/`is_virtual`; XOR with 2 |
| 2 realizes | `inherits(A,B)` where `Interface(B)` | exactly one of {1,2} per base |
| 3 specializes | `specializes`(4)/`instantiates`(5) edge | collapsed onto primary (decision 6) |
| 4 composes | `field_of`(8), field is a VALUE of record B | `classify_referent` → owned; `multiplicity` per container |
| 5 aggregates | `field_of`, field is `shared_ptr<B>`/container-of-shared | `classify_referent` → shared; `partial=1` if unwrap heuristic |
| 6 associates | `field_of`, field is raw `B*`/`B&`/`weak_ptr<B>` | `classify_referent` → borrowed |
| 7 creates | PR1 form edges 10–15 rolled to enclosing method's OWNER record | `create_form` per map below; by-value-return(2) derived from return type |
| 8 uses | method's `calls`(1)/`uses`(7) edges rolled to owner, dst = record/enum | virtual-dispatch target SET → `partial=1` |
| 9 destroys | PR1 form edge 16 rolled to enclosing method's owner | |
| 10 nests | record B declared inside record A (`parent_usr` of a record == a record) | NOT namespace nesting |
| 11 befriends | `friend` declarations | needs Layer-0 friend capture (see gap) |

**Layer-0 form → `create_form` map (PR2 reads):**
`construct-value(10)→3`, `construct-temp(11)→4`, `construct-heap(12)→5`,
`construct-copy(13)→7`, `construct-move(14)→8`, `factory-construct(15)→6`,
plain ctor `calls`(1) when no richer form applies `→1`; by-value-return `→2`
(derived from return type, `partial=1`). `destroy(16) → entity_edge kind=destroys(9)`.

**`befriends` gap (decision at PR2 kickoff).** No Layer-0 `friend` edge exists
today. Route (a) — recommended: add a minimal `FRIEND_DECL` capture in
`ast.py`/`ast.cpp` (the ONE place PR2 may touch the extractor) → a Layer-0
`befriends` fact. Route (b): defer kind 11 to a follow-up and ship 10 kinds.

#### B-kernel. `classify_referent(type)` — ONE shared helper (NOT parity-exempt)

Drives composes/aggregates/associates + factory-create + by-value-return. Unwraps
`unique_ptr<B>` (owned), `shared_ptr<B>`/container-of-shared (shared), raw
`B*`/`B&`/`weak_ptr<B>` (borrowed), `vector<B>`/`B[]` (multiplicity `0..*`).
Recovers referent B via `template_arg.ref_id` (ADR-004) or canonical-spelling →
`symbol` join — NEVER by string-parsing USRs. **Pure-DB roll-up, NO reparse.**
**It WRITES the on-disk table ⇒ MUST be byte-identical Py↔C++ parity** — it is NOT
`model.py`-exempt. Implement once in `storage.py` `materialize_entity_edges`,
mirror byte-for-byte in `storage.cpp`.

#### B-interface. `Interface(B)` predicate (realizes XOR generalizes)

Computed once per base B from B's `method_of`/`field_of` members + `is_pure` flags
(all DB facts, no reparse): `Interface(B) ⟺ B is a record whose NON-DESTRUCTOR
methods are ALL pure-virtual AND which has NO data members`. **The virtual
destructor is EXEMPT** (ADR-008 §5b — an idiomatic interface declares
`virtual ~B()=default`). Cache per base. `Interface(B)` → `realizes(2)`; else
`generalizes(1)`. Mutually exclusive per base.

#### B-collapse. Template-instance collapse (decision 6)

Before emitting any `entity_edge` endpoint, map an instance/specialization symbol
to its primary template via the `instantiates`(5)/`specializes`(4) edge. `Foo<int>`
and `Foo<double>` collapse to `Foo`. Layer-0 per-instance nodes (ADR-004) stay
intact.

#### B-partial. `partial=1` rules (success-criterion 3)

Set `partial=1` on: factory `creates` (template-arg heuristic), by-value-return
`creates` through dependent/template returns, virtual-dispatch `uses` (resolved to
a target SET), and any smart-pointer/container unwrap the kernel could not fully
resolve. Direct value field / direct base → `partial=0`.

#### B-readers + model. Read-side

- Low-level `entity_edge` readers in `query.py` (Python) + C++ mirror to list rows
  by src/dst/kind (parity-bound — both ports may serve the table).
- `model.py` typed accessors (e.g. `Record.composes()`, `Record.realizes()`,
  `Record.creates()`) are **Python-only / parity-EXEMPT** (`cidx-model-layer`).

#### B-version

Bump `VERSION` `0.17.0 → 0.18.0` (`cli.py:68`) + `kVersion` (`args.hpp:27`),
byte-identical, MINOR (additive new table).

---

## B. PARITY CHECKLIST (paired Python ↔ C++)

`model.py` is **Python-only / parity-EXEMPT**. `classify_referent` and
`materialize_entity_edges` are **NOT exempt** — they write the on-disk table, so
the parity rule binds (ADR-008 decision 4).

| # | Change | Python | C++ | PR |
|---|---|---|---|---|
| 1 | `edge_kind` form seeds 10–16 (inside schema string) | `storage.py:173–176` | `storage.cpp:132` | PR1 |
| 2 | in-code edge-name map gains 10–16 (QUERY layer, NOT storage) | `query.py:56` (`EDGE_KINDS`; `EDGE_NAMES` derives at `:67`) | `query.hpp:58` (`edge_names_map()`) | PR1 |
| 3 | `CXX_NEW_EXPR` → construct-heap(12) | `ast.py:731` | `ast.cpp:890` | PR1 |
| 4 | `CXX_DELETE_EXPR` → destroy(16) | `ast.py` (new branch) | `ast.cpp` (new) | PR1 |
| 5 | `CXX_CONSTRUCT_EXPR` → 10/13/14 | `ast.py` (new) | `ast.cpp` (new) | PR1 |
| 6 | `CXX_TEMPORARY_OBJECT_EXPR` → construct-temp(11) | `ast.py` (new) | `ast.cpp` (new) | PR1 |
| 7 | factory `make_unique`/`make_shared` → factory-construct(15) | `ast.py` near `:842` / body | `ast.cpp:1132` neighborhood | PR1 |
| 8 | emission wiring in body descent | `ast.py:1031` (`_body_descent`) | `ast.cpp:1317` (`body_descent_visitor`) / `:1682` | PR1 |
| 9 | product version `0.16.0→0.17.0` | `cli.py:68` | `args.hpp:27` | PR1 |
| 10 | schema-string byte-identical re-diff (seed moved) | `storage.py:68` `_SCHEMA` | `storage.cpp:27` `kSchema` | PR1 + PR2 |
| 11 | `entity_edge_kind` + `entity_edge` tables + indexes | `storage.py:68` `_SCHEMA` | `storage.cpp:27` `kSchema` | PR2 |
| 12 | `entity_edge_kind` seed (11 rows) | inside `_SCHEMA` | inside `kSchema` | PR2 |
| 13 | `SCHEMA_VERSION` `16→17` | `storage.py:35` | `storage.hpp:30` | PR2 |
| 14 | `materialize_entity_edges()` phase | `storage.py` (new method) | `storage.cpp` (new method) | PR2 |
| 15 | wire into `resolve_pass()` after `rollup_edge_counts` | `storage.py:1777` | `storage.cpp:1950` | PR2 |
| 16 | `classify_referent` kernel (NOT exempt) | `storage.py` (new helper) | `storage.cpp` (new helper) | PR2 |
| 17 | `Interface(B)` predicate (dtor-exempt) | `storage.py` (new helper) | `storage.cpp` (new helper) | PR2 |
| 18 | template-instance collapse rule | `storage.py` (kernel) | `storage.cpp` (kernel) | PR2 |
| 19 | `friend` capture (if befriends route (a)) | `ast.py` (new `FRIEND_DECL`) | `ast.cpp` (new) | PR2 |
| 20 | low-level `entity_edge` readers | `query.py` | C++ mirror | PR2 |
| 21 | `model.py` typed accessors — **Python-only / EXEMPT** | `model.py` | — (none) | PR2 |
| 22 | product version `0.17.0→0.18.0` | `cli.py:68` | `args.hpp:27` | PR2 |

---

## C. TEST MATRIX

`parity_check.sh` does NOT exercise `entity_edge` today — it MUST grow to dump the
table (sorted, all columns) from a Py-built and a C++-built DB on the same libclang
and diff them; PR1 must additionally dump the new `edge`/`edge_kind` form rows. This
is a required deliverable of the respective PR.

### PR1 (pytest + ctest, parity-bound except model)

| Case | Assertion |
|---|---|
| `test_construct_heap` | `new geo::Circle(r)` → Layer-0 edge kind 12 to `Circle` |
| `test_construct_value` | `B b;` / `B b(x)` → kind 10 |
| `test_construct_temp` | `B{}` temporary → kind 11 |
| `test_construct_copy` | copy-ctor site → kind 13 |
| `test_construct_move` | `std::move` ctor site → kind 14 |
| `test_factory_construct` | `make_unique<B>` → kind 15, B recovered via `template_arg.ref_id` |
| `test_destroy` | `delete p;` → kind 16 to the pointee record |
| `test_edge_kind_seed` | `edge_kind` has rows 10–16 with the proposed names |
| `test_no_stub_for_builtin_new` | `new int`/`new <stdlib>` emits no form edge (lookup-only) |
| ctest `parity_*` | C++ form-edge extraction byte-identical to Python on the same TU |
| `parity_check.sh` (extended) | `edge`/`edge_kind` dump identical Py↔C++ |

### PR2 (pytest + ctest)

| Case | Assertion |
|---|---|
| `test_schema_v17` | fresh DB reports `SCHEMA_VERSION == 17`; `entity_edge` + `entity_edge_kind` exist |
| `test_v16_to_v17_open` | opening a v16 DB picks up v17 tables (additive, no data loss) |
| `test_entity_edge_kind_seed` | 11 rows, ids 1–11, the locked names |
| `test_generalizes` | chain `B→A`/`C→B`/`D→C` → `generalizes(1)`, NOT realizes |
| `test_realizes` | `Amphibian→Walker`/`Swimmer` → `realizes(2)`; bases pass `Interface` (dtor exempt) |
| `test_realizes_xor_generalizes` | each base emits exactly one of {1,2}, never both |
| `test_specializes` | template specialization → `specializes(3)`, collapsed onto primary |
| `test_composes` | `devirt3.hpp` `HolderV { chain::B b; }` value field → `composes(4)`, partial=0 |
| `test_aggregates` | `devirt3.hpp` `HolderS { shared_ptr<chain::B> sp; }` → `aggregates(5)` |
| `test_associates` | `devirt3.hpp` `HolderP { chain::B* bp; }` / `HolderR { chain::B& br; }` → `associates(6)` |
| `test_creates_heap` | method-scoped `new B` → `creates(7)` form=5, src = owner record, partial=0 |
| `test_creates_value` | method constructing `B b;`/`B b(x);` by value → `creates(7)` form=3, src = owner record, partial=0 (maps Layer-0 kind 10→form 3) |
| `test_creates_temp` | method building a temporary `B{}`/`B(x)` → `creates(7)` form=4, src = owner record (maps Layer-0 kind 11→form 4) |
| `test_creates_copy` | method invoking a copy-ctor (`B b2(b1);`) → `creates(7)` form=7, src = owner record (maps Layer-0 kind 13→form 7) |
| `test_creates_move` | method invoking a move-ctor (`B b2(std::move(b1));`) → `creates(7)` form=8, src = owner record (maps Layer-0 kind 14→form 8) |
| `test_creates_factory_partial` | `make_unique<B>` in a method → `creates(7)` form=6, partial=1 |
| `test_creates_by_value_return` | method returning `B` by value → `creates(7)` form=2, partial=1 |
| `test_destroys` | method-scoped `delete` → `destroys(9)`, src = owner record |
| `test_uses` | `HolderV::via()` calls `b.rank()` (`b`:`chain::B`) → `uses(8)` HolderV→chain::B, partial=0; `Dashboard::draw()` calls `c.send()` (`Client`) → `uses(8)` Dashboard→Client, partial=0 (both EXISTING fixtures; `Config::value` is NOT usable — it calls the FREE fn `util::helper`, no record dst) |
| `test_uses_virtual_partial` | a method calling a virtual method on an unresolved receiver (Γ target SET, e.g. `Sink::feed(geo::Shape&)` → `s.area()`) → `uses(8)`, partial=1 (NEEDS NEW FIXTURE — D-spec) |
| `test_nests` | record nested in record → `nests(10)` (NOT namespace) |
| `test_befriends` | `friend` decl → `befriends(11)` (route (a)) |
| `test_multiplicity` | `std::vector<Widget> items_;` field on the new `Pool`/`Manager` fixture (D-spec) → `composes(4)` with multiplicity `0..*`(3); element `Widget` recovered via `template_arg.ref_id` |
| `test_idempotent_rebuild` | two `resolve` runs → identical `entity_edge` (DELETE+rebuild) |
| `test_template_instance_collapse` | `Foo<int>`/`Foo<double>` endpoints map to `Foo` |
| ctest `parity_*` | `materialize_entity_edges` output byte-identical Py↔C++ |
| `parity_check.sh` (extended) | full `entity_edge` dump identical Py↔C++ |

---

## D. GRAPHLAB ACCEPTANCE ROWS

`graphlab` = `manifests/graphlab/` (C++17). Existing fixtures cover inheritance,
templates, dispatch, deep calls, namespace nesting, member templates,
record-field has-a (`devirt3.hpp` `HolderV`/`HolderS`/`HolderP`/`HolderR` — value /
`shared_ptr` / raw-ptr / ref fields of `chain::B`, registered via `devirt3.cpp`),
AND record-method→record `uses(8)` at partial=0: `devirt3.hpp` `HolderV::via()`
(`b.rank()`, `b`:`chain::B`) and `pipeline.cpp` `Dashboard::draw()` (`c.send()`,
`Client`). **`nested.hpp` `Config::value()` is NOT a `uses(8)` backing** — it calls
the FREE function `org::project::util::helper` (`nested.cpp:14`), which is not a
record, so it yields no `entity_edge` dst. Existing fixtures do NOT cover
method-scoped new/delete, the value/temp/copy/move/factory/by-value-return create
forms inside a method, virtual-dispatch `uses` at partial=1, record-in-record
nesting, friend, or a `vector<record>` field (multiplicity `0..*`). Rows flagged
**NEEDS NEW FIXTURE** require a fixture that does NOT yet exist; specify only — do
NOT write it now.

| Kind | Fixture | Expected `entity_edge` row (src → dst, via_member, form / mult / partial) | Status |
|---|---|---|---|
| 1 generalizes | `chain.hpp` | `B→A`, `C→B`, `D→C` generalizes(1), access=public, is_virtual=0 | EXISTING |
| 1 generalizes | `shapes.hpp` | `Circle→Shape`, `Rectangle→Shape` generalizes(1) (Shape has non-pure virtual `name()` ⇒ NOT interface) | EXISTING |
| 2 realizes | `creatures.hpp` | `Amphibian→Walker`, `Amphibian→Swimmer` realizes(2) (Walker/Swimmer: `virtual ~T()=default` + one pure-virtual, no fields ⇒ Interface, dtor exempt) | EXISTING |
| 3 specializes | `containers.hpp` | `Wrapper<bool>`/`describe<bool>` → specializes(3), collapsed onto primary `Wrapper`/`describe` | EXISTING |
| 4 composes | `devirt3.hpp` `HolderV { chain::B b; }` | `HolderV→chain::B` composes(4), via_member=`b`, multiplicity=one(1), partial=0 | EXISTING (registered via `devirt3.cpp` in `compile_commands.json`) |
| 5 aggregates | `devirt3.hpp` `HolderS { std::shared_ptr<chain::B> sp; }` | `HolderS→chain::B` aggregates(5), via_member=`sp`, partial per unwrap | EXISTING (`devirt3.cpp`) |
| 6 associates | `devirt3.hpp` `HolderP { chain::B* bp; }` / `HolderR { chain::B& br; }` | `HolderP→chain::B`, `HolderR→chain::B` associates(6), via_member=`bp`/`br` | EXISTING (`devirt3.cpp`) |
| 7 creates (form=5 heap) | — method that `new`s another entity | creates(7), form=5, src=owner record, via_member=method, partial=0 | **NEEDS NEW FIXTURE** |
| 7 creates (form=3 value) | — method with `Widget w(x);` | creates(7), form=3, src=owner record, via_member=method, partial=0 (Layer-0 kind 10→form 3) | **NEEDS NEW FIXTURE** |
| 7 creates (form=4 temp) | — method building `Widget{}`/`Widget(x)` temporary | creates(7), form=4, src=owner record (Layer-0 kind 11→form 4) | **NEEDS NEW FIXTURE** |
| 7 creates (form=7 copy) | — method with copy-ctor `Widget w2(w);` | creates(7), form=7, src=owner record (Layer-0 kind 13→form 7) | **NEEDS NEW FIXTURE** |
| 7 creates (form=8 move) | — method with move-ctor `Widget w3(std::move(w));` | creates(7), form=8, src=owner record (Layer-0 kind 14→form 8) | **NEEDS NEW FIXTURE** |
| 7 creates (form=6 factory) | — method using `make_unique<B>` | creates(7), form=6, partial=1 | **NEEDS NEW FIXTURE** |
| 7 creates (form=2 by-value-return) | — method returning `B` by value | creates(7), form=2, partial=1 | **NEEDS NEW FIXTURE** |
| 8 uses | `devirt3.hpp` `HolderV::via()` / `pipeline.cpp` `Dashboard::draw()` | `HolderV→chain::B` uses(8) (`via()` calls `b.rank()`, `b` is a `chain::B` value member — record dst), `Dashboard→Client` uses(8) (`draw()` calls `c.send()`, `Client` is a record) — both record-method→record-method, both partial=0 | EXISTING |
| 8 uses (virtual-dispatch partial=1) | — method that calls a virtual method on an UNRESOLVED receiver (target SET) | a record method using another record via virtual dispatch whose Γ target set is not a singleton → uses(8), partial=1 (D-spec adds e.g. `Sink::feed(geo::Shape& s)` calling `s.area()` — virtual, ref receiver ⇒ ⊤ target set) | **NEEDS NEW FIXTURE** |
| 9 destroys | — method that `delete`s another entity | destroys(9), src=owner record, via_member=method | **NEEDS NEW FIXTURE** |
| 10 nests | — record declared inside another record | nests(10) src=outer, dst=inner (graphlab `nested.hpp` is NAMESPACE nesting) | **NEEDS NEW FIXTURE** |
| 11 befriends | — `friend` declaration | befriends(11) src=class, dst=friend record | **NEEDS NEW FIXTURE** (only if befriends route (a)) |

### D-spec. The method-scoped new/delete fixture (CRUCIAL — ADR-008 Consequences)

graphlab's `new`/`delete` live in FREE functions (`pipeline.cpp:38` `make_shape`,
`pipeline.cpp:43` `consume`) — no owning record ⇒ no `entity_edge` src. PR2's
`creates`/`destroys` roll a construction/destruction site up to the **enclosing
method's OWNER record**, which does not exist today.

**Required new fixture shape** (specify only; do NOT write now): a class whose
**method** heap-allocates AND deletes another entity — e.g. a `Pool`/`Manager`
record with a method `acquire()` doing `new Widget(...)` and a method
`release(Widget*)` doing `delete w;`, where `Widget` is a record. Yields:
`Pool creates Widget` (form=5, via_member=`acquire`, partial=0) and
`Pool destroys Widget` (via_member=`release`). A factory variant
(`make_unique<Widget>`) covers form=6 (partial=1); a by-value-returning method
covers form=2 (partial=1).

**The fixture MUST ALSO exercise the value/temp/copy/move construction forms
INSIDE a method body** so the four PR2 entity-edge tests `test_creates_value`
(form=3), `test_creates_temp` (form=4), `test_creates_copy` (form=7), and
`test_creates_move` (form=8) each have a backing fixture — without these, a bug in
the Layer-0-form → `entity_edge.create_form` mapping inside
`materialize_entity_edges()` (kind 10→3, 11→4, 13→7, 14→8) would be invisible to
the PR2 suite (only Layer-0 PR1 tests would catch it), violating the no-deferred-
failures discipline. Add to a `Pool`/`Manager` method body:
- a value construction `Widget w(x);` → `Pool creates Widget` form=3 (partial=0),
- a temporary `Widget{}` / `consume(Widget(x));` → `Pool creates Widget` form=4,
- a copy-construction `Widget w2(w);` → `Pool creates Widget` form=7,
- a move-construction `Widget w3(std::move(w));` → `Pool creates Widget` form=8.
(All four roll up to the SAME `Pool→Widget creates` family distinguished by
`create_form`; `via_member` = the enclosing method.) `Widget` therefore needs a
copy-ctor and a move-ctor (declare/`=default` is enough for the AST cursors).

**The fixture MUST ALSO carry a method that uses a record via UNRESOLVED virtual
dispatch** so `test_uses_virtual_partial` (PR2 matrix) and the §D virtual-dispatch
`uses(8)` acceptance row have a backing fixture — e.g. `Sink::feed(geo::Shape& s)`
calling `s.area()` (`area()` is virtual, `s` is a reference receiver ⇒ Γ resolves
to a target SET, not a singleton). Yields `Sink→geo::Shape uses(8)` with
`partial=1`. The non-virtual / value-receiver `uses(8)` cases (partial=0) are
ALREADY covered EXISTING by `devirt3.hpp` `HolderV::via()` and `pipeline.cpp`
`Dashboard::draw()` (see §D) — do NOT re-create them.

**The fixture MUST also carry a `vector<record>` field** so `test_multiplicity`
has a backing fixture: add `std::vector<Widget> items_;` (or `Widget items_[N];`)
as a field on `Pool`/`Manager`. This yields `Pool composes Widget` with
`multiplicity = 0..*(3)` via `classify_referent` (the element entity `Widget`,
recovered through `template_arg.ref_id`), `via_member = items_`. Without this field
`test_multiplicity` (PR2 matrix) asserts a row no fixture produces.

**One fixture can also carry** a nested record (record-in-record → nests) and a
`friend` (befriends), to keep the new fixtures minimal. **Do NOT add value /
`shared_ptr` / raw-ptr / ref has-a fields here** — those single-multiplicity has-a
rows (composes/aggregates/associates) are ALREADY covered by `devirt3.hpp`'s
`HolderV`/`HolderS`/`HolderP`/`HolderR` (acceptance rows 4–6, EXISTING); duplicating
them on `Pool` only adds noise. The only has-a row this new fixture owns is the
`vector<Widget>` multiplicity-`0..*` case above, which devirt3 does not cover.

**`compile_commands.json`:** per project CLAUDE.md "keep this file in sync", the
new fixture's TU(s) MUST be appended to `manifests/compile_commands.json` (one
object per new source: `directory`, `command` preserving `-std=c++17`, `file`;
entries sorted by resolved path). The implementing developer does this — the one
place to register a new TU.

---

## E. Risks / out of scope

- **OUT OF SCOPE:** the query engine that reads `entity_edge` (Layer-2 DSL /
  CXG-QL); `depends_on(Module→Module)`; promoting Type/Parameter/LocalVariable to
  first-class nodes; per-entity incremental invalidation (OQ-10).
- **Risk — `befriends`:** kind 11 needs Layer-0 `friend` capture not present today;
  decide route (a)/(b) at PR2 kickoff. If deferred, ship 10 kinds and document kind
  11 as a follow-up.
- **Risk — `CXX_CONSTRUCT_EXPR` form discrimination:** copy vs move vs value reads
  the ctor declaration's single-param type; verify the C++ libclang path recovers
  the same discriminant as Python (parity-bound).
- **Risk — `parity_check.sh` growth:** today it does not dump `entity_edge` or the
  new form edges; failing to extend it lets a Py↔C++ drift ship silently. Both PRs
  must extend it.
- **Reindex required:** PR2 leaves `entity_edge` empty until a `resolve` runs;
  acceptance needs a full reindex + resolve of `manifests/graphlab` AFTER the new
  fixture lands.

---

## References

- ADR-008 (accepted): `cidx-cpp/docs/adr/ADR-008-entity-edge.md`
- Architect log: `cidx-cpp/docs/adr/architect-log-entity-edge.md`
- Foundation (LOCKED): `pages/planning/cidx-code-model-foundation.md` §1–§3.7
- Layer-1 design: `pages/planning/cidx-entity-relationship-graph.md` §3–§6
- ADR-004 (template instances / `template_arg.ref_id`): cidx memory
  `cidx-template-instantiation-nodes`
- Parity / version rules: `cidx-python-cpp-parity`, `cidx-version-bump-rule`
- graphlab corpus: cidx memory `graphlab-test-project`
- Cognee: `task:cidx-entity-edge`, `role:senior-developer`

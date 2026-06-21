# Implementation Plan — Layer-1 `entity_edge` (code-model foundation)

**Status:** ready to execute. Design is CLOSED — do not redesign. Source of truth:
- `[[pages/planning/cidx-code-model-foundation]]` §3.6 (exact SQL), §3.7 (OQ-1 resolution)
- `[[pages/planning/cidx-entity-relationship-graph]]` §3–§5 (projection rules + type-classification kernel)
- ADR-008 **NOT YET WRITTEN** at `cidx-cpp/docs/adr/ADR-008-entity-edge.md` — **dependency**: gate must confirm ADR-008 `Status: accepted` before PR2 schema lock. PR1 may start now from the locked §3.7 contract.

Two PRs, sequenced: **PR1 (Layer-0 extraction) → merge → PR2 (v17 entity_edge)**. PR2 schema/storage scaffolding is independent of PR1 and parallelizable; only PR2's roll-up depends on PR1 merge.

HARD RULES (apply to every task): Python (`project/indexer`) ↔ C++ (`cidx-cpp`) **byte-identical** schema strings + behavior parity (`model.py` is Python-only/EXEMPT). Bump product version both ports byte-identical. Full `pytest` + `ctest` + `parity_check.sh` green before each PR. **Remind to restart the MCP server** after any server-code change.

---

## PR1 — Layer-0 extraction (version 0.15.0 → **0.16.0**, MINOR)

**Goal:** emit Layer-0 facts that let PR2's roll-up distinguish `create_form` 3–8 and `destroys`. Closes OQ-1. **No schema bump** — the form is persisted via NEW `edge_kind` seed ids on the existing `edge` table (`edge_kind` is seed-only / no-FK since v0.15.0, so adding ids needs zero schema change).

### D-PR1 — RESOLVED by orchestrator (Architect recording in ADR-008)
**How construction/destruction forms surface in the EDB: distinct Layer-0 `edge.kind` ids.** (Supersedes the earlier "derive in PR2 from expr-cursor-kind" proposal, which was **unsound** — ast.py does NOT persist the construct expr-cursor-kind, and `_classify_value_source` collapses `new` (ast.py:731–733) and ctor `CALL_EXPR` (ast.py:725) both to `"construct"`, so the form is LOST once it is a plain `calls`→ctor edge. Also rejected: a `call_arg.create_form` column — a default-ctor `B b;` has no args ⇒ no `call_arg` row.)

**Approach.** PR1 emits, from each construction/destruction site, an `edge` to B's constructor/destructor symbol (mint a named stub when the ctor/dtor is unindexed, gated to non-system headers via the v0.14.2 dependent-call-stub pattern), but tags it with a **form-specific `edge.kind`**:

| Layer-0 edge kind (new seed id) | emitted from | maps to PR2 `create_form` |
|---|---|---|
| `construct-value` | `CXX_CONSTRUCT_EXPR` bound to a VAR_DECL (`B b;` / `B b(x);` / `B{x}`) | 3 (value) |
| `construct-temp` | `CXX_TEMPORARY_OBJECT_EXPR` | 4 (temp) |
| `construct-heap` | `CXX_NEW_EXPR` (pointee = B) | 5 (heap) |
| `construct-copy` | copy `CXX_CONSTRUCT_EXPR` | 7 (copy) |
| `construct-move` | move `CXX_CONSTRUCT_EXPR` | 8 (move) |
| `factory-construct` | `make_unique<B>`/`make_shared<B>` (B via template arg) | 6 (factory) → `partial=1` |
| `destroy` | `CXX_DELETE_EXPR` (pointee = B) or explicit dtor call | (destroys kind, not a create_form) |

New ids are appended to the `edge_kind` seed (current max = 9 `method_of`, so 10–16) in BOTH ports byte-identical. The existing generic `calls`(1)/`uses`(7) edges are unchanged. **by-value return (form 2)** stays signature-derived in PR2 from `symbol.type` via the §5 kernel — there is NO ctor cursor under RVO, so PR1 emits nothing for it (already covered by the existing return-type `uses` at ast.py:1500). PR2's roll-up maps Layer-0 `edge.kind ∈ {10..16}` → `entity_edge.create_form` (1–8) / `destroys`.

> **Open point now CLOSED** — no further gate confirmation needed on D-PR1; the distinct-edge-kind decision is final and goes into ADR-008.

### PR1 task breakdown

| id | title | files | depends-on | role | parallel? |
|----|-------|-------|-----------|------|-----------|
| **P1-T0** | Append new `edge_kind` seed ids 10–16 (`construct-value/-temp/-heap/-copy/-move`, `factory-construct`, `destroy`) — BOTH ports byte-identical; no schema bump (seed-only) | `storage.py` edge_kind seed :173–176 + `EDGE_NAMES` map; `cidx-cpp/src/storage/storage.cpp` :132–141 + in-code map | — | storage dev | first; unblocks T1–T4 |
| **P1-T1** | Add `CXX_CONSTRUCT_EXPR` + `CXX_TEMPORARY_OBJECT_EXPR` body-walk handlers → `construct-value`/`construct-temp` edge to ctor (mint non-system stub); distinguish copy/move ctor → `construct-copy`/`-move` | `project/indexer/clang/ast.py` (`_body_descent` elif-chain ~:1287–1410) | P1-T0 | Python dev | with P1-T2/T3 (different elif arms) |
| **P1-T2** | Add `CXX_NEW_EXPR` handler → `construct-heap` edge to ctor of pointee type (today both `new` and ctor CALL collapse to `"construct"` at ast.py:725/731–733 — this splits them) | `ast.py` `_body_descent` | P1-T0 | Python dev | parallel |
| **P1-T3** | Add `CXX_DELETE_EXPR` handler → `destroy` edge to destructor of pointee type (closes "delete invisible") | `ast.py` `_body_descent` | P1-T0 | Python dev | parallel |
| **P1-T4** | Factory recovery: `make_unique<B>`/`make_shared<B>` → recover `B` via `clang_Type_getTemplateArgumentAsType` → `factory-construct` edge to B::ctor stub | `ast.py` (new helper near template-arg recovery ~:1255/:1363) | P1-T0, P1-T1 | Python dev | sequential after T1 |
| **P1-T5** | By-value-return: verify the existing return-type fact (ast.py:1500) is recoverable in PR2; **no extraction change** (RVO ⇒ no ctor cursor) — documentation/test note only | `ast.py` | — | Python dev | parallel |
| **P1-T6** | **C++ parity**: mirror P1-T1..T5 byte-identical | `cidx-cpp/src/clangx/ast.cpp` (mirror `_body_descent` dispatch + helpers) | P1-T1..T5 | C++ dev | sequential after Py arms land (per-arm parity) |
| **P1-T7** | Version bump 0.15.0→0.16.0 both ports byte-identical | `project/indexer/cli.py:68` (`VERSION`) + `cidx-cpp/src/cli/args.hpp:27` (`kVersion`) | P1-T1..T6 | either | last |
| **P1-T8** | graphlab fixture (P1-FX) + tests (see PR1 test matrix) | `manifests/graphlab/pipeline.{hpp,cpp}` + `manifests/compile_commands.json` + `project/tests/test_create_destroy_extraction.py` (new) + ctest | P1-T1..T6 | Python dev + C++ dev | last |

**P1-FX (graphlab fixture, PR1-owned).** graphlab's `make_shape`(pipeline.cpp:37 `new`) / `consume`(pipeline.cpp:41 `delete`) are **free functions** → no entity src → no `creates`/`destroys` row. Add a **method-scoped** new/delete so PR2 acceptance rows 8/9 have an entity src: extend `app::Dashboard` (pipeline.hpp:10 / pipeline.cpp) with `Dashboard::refresh()` doing `new geo::Circle(r)` and a `delete` through a `geo::Shape*`. **Register the fixture's TU** in `manifests/compile_commands.json` (append a sorted entry, preserve `-std=c++17 -I` flags, per CLAUDE.md). PR1 owns new/delete extraction; PR2 reuses the fixture for acceptance.

**Parallelism:** P1-T0 first (seeds the new kinds). P1-T1/T2/T3/T5 touch independent `elif` arms — one Python dev to avoid merge churn, or split across worktrees and rebase the elif-chain. T4 follows T1. C++ parity (T6) lags each Python arm.

---

## PR2 — v17 `entity_edge` (version 0.16.0 → **0.17.0**, MINOR; schema v16 → **v17**)

**Goal:** materialize all 11 Layer-1 entity relations into `entity_edge` via a resolve-style, DB-only, global roll-up. Depends on PR1 merge (for rich `creates`/`destroys`).

### PR2 task breakdown

| id | title | files | depends-on | role | parallel? |
|----|-------|-------|-----------|------|-----------|
| **P2-T1** | Schema: add `entity_edge` + `entity_edge_kind` to `_SCHEMA` (exact SQL from foundation §3.6) + seed 11 kinds (mirror symbol_kind seed at storage.py:162 / edge_kind seed :173); bump `SCHEMA_VERSION` 16→17 | `project/indexer/storage.py:35,68–263` | ADR-008 accepted | storage dev | **independent of PR1** — start in parallel |
| **P2-T2** | Migration: `if "entity_edge" not in tables: changed = True` (additive, CREATE IF NOT EXISTS pattern — mirror diagnostic flip at storage.py:520–529) | `project/indexer/storage.py` `_migrate` ~:520–529 | P2-T1 | storage dev | with P2-T1 |
| **P2-T3** | Storage accessors: `add_entity_edge(...)`, `clear_entity_edges()`, `entity_edges(src_id=…, kind=…)` | `project/indexer/storage.py` | P2-T1 | storage dev | after T1 |
| **P2-T4** | **Type-classification kernel** (foundation §5 / ER §5): `classify_member_type(type) → (relation∈{compose,aggregate,associate}, B_symbol_id, multiplicity)` — unwrap value / `unique_ptr` / `shared_ptr` / `weak_ptr` / raw-ptr / ref / containers via canonical name + `template_arg.ref_id` | `project/indexer/entity_rollup.py` (new) | P2-T3 | Python dev | **independent of PR1** — pure type logic, parallel |
| **P2-T5** | **Roll-up pass** writing all 11 kinds. Wire into `resolve_pass()` (storage.py:1777, alongside `rollup_edge_counts`:1743 / `cross_repo_edges`:1754). Reads `edge`+`symbol.type`+`template_arg`, applies ER §4 rules + §5 kernel. `DELETE FROM entity_edge` then re-run (re-materialize). **realizes XOR generalizes** (see roll-up rule note below). | `project/indexer/entity_rollup.py` + `storage.py:1777` hook | P2-T3, P2-T4, **PR1 merged** | Python dev | sequential (needs PR1) |
| **P2-T6** | `create_form`/`destroys` derivation: map PR1's Layer-0 `edge.kind ∈ {10..16}` → `entity_edge.create_form` (3–8) / `destroys`; derive form-2 (by-value return) from `symbol.type` via §5 kernel; set `partial=1` for `factory-construct`/by-value/⊤-incomplete | `entity_rollup.py` | P2-T5, **PR1 merged** | Python dev | sequential |
| **P2-T7** | **C++ schema/storage parity**: `kSchemaVersion` 16→17 (storage.hpp:30), `kSchema` string add (BYTE-IDENTICAL to Py `_SCHEMA`), migration flip (storage.cpp:737), accessors | `cidx-cpp/src/storage/storage.hpp:30` + `storage.cpp` (kSchema :27/seed :132, migration flip :737) | P2-T1..T3 | C++ dev | **independent of PR1** — parallel |
| **P2-T8** | **C++ roll-up parity**: mirror kernel + roll-up byte-identical behavior; wire into C++ `resolve_pass()` (storage.cpp:1950, alongside `rollup_edge_counts`:1911) | `cidx-cpp/src/storage/storage.cpp` + clangx | P2-T4..T6, P2-T7 | C++ dev | sequential after Py roll-up |
| **P2-T9** | **Readers (Python-only, EXEMPT)**: `model.py` `Entity.relations()`, `coupling()`, optional `cidx entity relations <Class>` CLI surface | `project/indexer/model.py` + `cli.py`/`query.py` | P2-T5 | Python dev | parallel with C++ |
| **P2-T10** | Version bump 0.16.0→0.17.0 both ports byte-identical | `cli.py:68` + `args.hpp:27` | all | either | last |
| **P2-T11** | Tests (see PR2 test matrix) + graphlab acceptance | new pytest + ctest + parity_check | all | QA/dev | last |

**Parallelism summary:** P2-T1/T2/T3 (Py schema/storage) + P2-T7 (C++ schema) + P2-T4 (kernel) can ALL start in **parallel worktrees before PR1 merges**. P2-T5/T6/T8/T9 (the roll-up that reads PR1's facts) are **gated on PR1 merge**.

**Roll-up rule — `realizes` XOR `generalizes` (P2-T5).** From a single `inherits(A,B)` edge emit **exactly one** of:
- `realizes(2)` if B classifies as an **Interface** (all methods pure-virtual AND no data members);
- `generalizes(1)` otherwise (B carries state/impl).

They are **mutually exclusive** — never both for the same `(A,B)`. Carry `access` (0/1/2) and `is_virtual` on whichever is emitted.

---

## Py/C++ parity checklist (every paired site)

| Concern | Python anchor | C++ anchor | Parity |
|---|---|---|---|
| **PR1** new `edge_kind` seed ids 10–16 | `storage.py` edge_kind seed :173 + `EDGE_NAMES` map | `storage.cpp` edge_kind seed :132 + in-code map | identical ids/names byte-identical |
| **PR1** body-walk ctor/temp/new/delete handlers | `ast.py` `_body_descent` elif-chain ~:1287–1410 (today `new`+ctor both → `"construct"` at :725/:731–733 via `_classify_value_source`) | `cidx-cpp/src/clangx/ast.cpp` (mirror dispatch) | **byte-identical behavior** |
| **PR1** factory template-arg recovery | `ast.py` ~:1255/:1363 helper | `ast.cpp` mirror | byte-identical |
| **PR2** Schema string | `storage.py` `_SCHEMA` :68–263 (add `entity_edge`/`entity_edge_kind`) | `storage.cpp` `kSchema` :27 onward | **BYTE-IDENTICAL** (verify via diff) |
| **PR2** `entity_edge_kind` seed (11 rows) | `storage.py` (mirror symbol_kind seed :162 / edge_kind :173) | `storage.cpp` (mirror :121/:132) | identical row order: `1 generalizes,2 realizes,3 specializes,4 composes,5 aggregates,6 associates,7 creates,8 uses,9 destroys,10 nests,11 befriends` |
| **PR2** `SCHEMA_VERSION` | `storage.py:35` (16→17) | `storage.hpp:30` `kSchemaVersion` (16→17) | identical int |
| **PR2** migration flip | `storage.py` `_migrate` ~:520–529 (mirror diagnostic flip) | `storage.cpp` :737 | identical guard |
| **PR2** storage accessors | `storage.py add_entity_edge/clear/query` | `storage.cpp` + `storage.hpp` | identical signatures/SQL |
| **PR2** roll-up + kernel | `entity_rollup.py` | `storage.cpp`/clangx mirror | byte-identical behavior |
| **PR2** `resolve_pass` hook | `storage.py:1777` (`rollup_edge_counts`:1743, `cross_repo_edges`:1754) | `storage.cpp:1950` (`rollup_edge_counts`:1911) / `storage.hpp:258` | identical |
| Product version | `cli.py:68 VERSION` | `args.hpp:27 kVersion` | byte-identical string |
| **Readers** | `model.py` (Entity.relations/coupling) + CLI | — | **Python-only / EXEMPT** |

---

## Test matrix

### PR1 (`project/tests/test_create_destroy_extraction.py` new + ctest)
Each asserts the form-specific `edge.kind` (10–16), NOT a generic `calls`(1):
| Case | Asserts |
|---|---|
| `B b;` value decl | `construct-value` edge fn→B::ctor exists |
| `f(B(x))` temporary | `construct-temp` edge fn→B::ctor |
| `new B(...)` | `construct-heap` edge fn→B::ctor (split from ctor-CALL, both `"construct"` today) |
| `delete b` (b is `B*`) | `destroy` edge fn→B::~dtor (closes "delete invisible") |
| `make_unique<B>()` | `factory-construct` edge fn→B::ctor via template-arg recovery; system-header callee NOT minted |
| copy `B b2 = b1;` / move | `construct-copy` / `construct-move` edge |
| by-value return `B make()` | return-type fact present (ast.py:1500); NO ctor edge (RVO) — read in PR2 |
| **parity** | `ctest` + `parity_check.sh` Py↔C++ byte-identical on graphlab `pipeline.cpp` (incl. new `Dashboard::refresh()` fixture) |

### PR2 (new pytest + ctest + parity_check)
| Group | Cases |
|---|---|
| **per create_form** | 1=ctor_call, 2=return, 3=value, 4=temp, 5=heap, 6=factory, 7=copy, 8=move — one row each, correct `create_form` int |
| **per entity_edge kind** | generalizes, realizes, specializes, composes, aggregates, associates, creates, uses, destroys, nests, befriends — one fixture each |
| **multiplicity** | `B b`→1; `unique_ptr<B>`→2(0..1); `vector<B>`→3(0..*); `B[N]`→4(N) |
| **partial flag** | factory in system header → `partial=1`; resolved direct ctor → `partial=0` |
| **realizes XOR generalizes** | Interface base → `realizes(2)` only (NOT also generalizes); state-bearing base → `generalizes(1)` only |
| **virtual base** | virtual inheritance → `is_virtual=1` on generalizes |
| **access** | public/protected/private base → `access` 0/1/2 |
| **nested (10)** | pinned scenario `id=nests-1`: conftest `Base::Nested` (or a graphlab nested class) → `nests(10)` row src=Base dst=Nested |
| **befriends (11)** | `id=befriends-1`: `friend class X;` — graphlab has ZERO friend decls, so use a **hermetic synthetic** in pytest (or add `friend class Y;` to a graphlab record) → `befriends(11)` row |
| **partial=1 virtual-dispatch `uses`** | `id=uses-partial-1`: `measure(const Shape&)` body calls `Shape::area()` (target set not singleton) → `uses(8)` partial=1 |
| **roll-up idempotency** | `resolve` twice → identical `entity_edge` rows (no dupes, UNIQUE holds) |
| **re-materialize** | `DELETE FROM entity_edge` + re-run → identical rows |
| **migration** | `entity_edge` EMPTY immediately post-migrate (additive, no backfill); >0 rows post-`resolve` |
| **collapse** (R-3) | `Order{LineItem head; LineItem tail;}` → 2 `composes` rows (keyed on via_member_id); 5 calls to `LineItem::total()` → 1 `uses` row count=5 |
| **parity** | full `ctest` + `parity_check.sh` green; `kSchema` diff = 0 bytes vs `_SCHEMA`. `parity_check.sh` must add any new `cidx entity` CLI invocation; the DB-dump diff already covers `entity_edge` INSERT rows + `kSchema` by construction |

---

## graphlab acceptance cases (`manifests/graphlab`, [[graphlab-test-project]])

Run roll-up on the standard index then eyeball these `entity_edge` rows `(src, dst, kind, create_form, partial)`:

`realizes` and `generalizes` are **mutually exclusive** per `(src,dst)` (Interface base → realizes only). `geo::Shape` is an Interface (all methods pure-virtual, no data fields) → Circle/Rectangle **realize** it; the state-bearing `chain`/`creatures` bases → **generalize**.

| # | src | dst | kind | create_form | partial | source |
|---|-----|-----|------|-------------|---------|--------|
| 1 | `geo::Circle` | `geo::Shape` | **realizes(2)** | — | 0 | shapes.hpp:18 — Shape is Interface (all pure-virtual, no fields) ⇒ realizes, NOT generalizes |
| 2 | `geo::Rectangle` | `geo::Shape` | **realizes(2)** | — | 0 | shapes.hpp:27 — same Interface rule |
| 3 | `B` | `A` | generalizes(1) | — | 0 | chain.hpp:17 `B : A` (A has state ⇒ generalizes) |
| 4 | `C` | `B`; `D` | `C` | generalizes(1) | — | 0 | chain.hpp:21,25 (chain A←B←C←D) |
| 5 | `Amphibian` | `Walker` | generalizes(1) | — | 0 | creatures.hpp:21 multiple inheritance |
| 6 | `Amphibian` | `Swimmer` | generalizes(1) | — | 0 | creatures.hpp:21 (2nd base) |
| 7 | `app::Dashboard` | `geo::Circle` | creates(7) | heap(5) | 0 | **P1-FX** `Dashboard::refresh()` → `new geo::Circle(r)` (method src ⇒ entity row) |
| 8 | `app::Dashboard` | `geo::Shape` | destroys(9) | — | 0 | **P1-FX** `Dashboard::refresh()` → `delete` through `geo::Shape*` (virtual ~Shape) |
| 9 | `app::Dashboard` | `Client` | associates(6) | — | 0/1 | pipeline.hpp:11 `draw(const Client&)` param — association (borrowed) |
| 10 | `Wrapper<bool>` | `Wrapper<T>` | specializes(3) | — | 0 | containers.hpp:50 explicit spec → collapse onto primary (OQ-3) |

> **Note:** rows 7/8 depend on the **P1-FX** fixture (`Dashboard::refresh()`), because graphlab's existing `new`/`delete` live in the FREE functions `make_shape`(pipeline.cpp:37)/`consume`(pipeline.cpp:41) — free functions are not entities, so `creates`/`destroys` (which require a record/enum src, `symbol.kind ∈ {class,struct,union,enum}`) would produce NO row from them. P1-FX adds the method-scoped new/delete so these rows exist.

**Mermaid dump check:** after roll-up, `cidx entity diagram --namespace geo` (or model.py exporter) must render Shape with **realization** arrows from Circle/Rectangle, and the chain A←B←C←D as a **generalization** ladder. Eyeball before schema lock.

---

## Risk / known limits

- **Factory in system headers** → `creates` `partial=1` (ctor call invisible; B recovered only via template-arg). Sound ⊤.
- **Member-template instances** stay empty (no concrete cursor; v0.5.1 limitation) — `specializes` only fires for explicit/instantiated class templates.
- **Explicit fn-template instantiation has no cursor** — no entity row.
- **`creates`/`destroys` need an entity src** — construction in a free function yields no entity_edge (graphlab rows 7/8 rely on the P1-FX `Dashboard::refresh()` fixture); only methods of a record produce these rows.
- **delete→dtor** was previously uncaptured (OQ-1) — **PR1 P1-T3 fixes** via `CXX_DELETE_EXPR` → `destroy` edge kind.
- **Raw `B*` field** → `associates` (OQ-5, non-committal; ownership undecidable from type).

## Migration note

Additive: `entity_edge` + `entity_edge_kind` via `CREATE TABLE IF NOT EXISTS`; migration only flips `changed` to bump v16→v17 (mirror diagnostic/label flip, storage.py:520–529 / storage.cpp:737). PR1's new `edge_kind` ids 10–16 need NO migration (seed-only, `INSERT OR IGNORE`). **No backfill** — `entity_edge` is derived; populate via `cidx index` (PR1 facts) + `cidx resolve` (roll-up). **Reindex + resolve required.** **Verify migration on a COPY of `~/.cache/cidx/index.db`** (`cp` first, open with new binary, assert v17 + `entity_edge` empty + existing edges/symbols preserved, then run resolve and assert rows >0) before touching the standard DB.

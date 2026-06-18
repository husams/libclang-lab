# ADR-004 Implementation Contract — Template instantiations as first-class symbols

Status: settled (plan). Decision of record:
`~/workspace/wiki/pages/decisions/adr-004-cidx-template-instantiation-nodes.md`.
Spec + worked example:
`~/workspace/wiki/pages/planning/cidx-template-instantiation-nodes.md`.
Scope of this document: PLAN ONLY — no indexer/storage code is edited here. This
contract enumerates every edit, resolves the three ADR open items, gives the exact
v12→v13 migration, the reader API surface, and the full test matrix. The grounding
code anchors below were read at v12 on `main`.

This contract is faithful to ADR-004 with ONE flagged design-vs-reality gap (see
§"Design conformance / drift flag"): the C++ `cidx graph callers/callees` opt-in
flag the ADR calls for **cannot be implemented** because no `cidx graph` subcommand
exists in the C++ binary today (graph query CLI is Python-only; C++ port pending,
per memory note `cidx-graph-query-layer`). Everything else conforms exactly.

---

## 0. Verified current state (read at the anchors, v12 / main)

- `ast.py:798–961` `_body_descent` CALL_EXPR handler: mints the instantiation
  **member** stub (`mint_symbol_id`, line 820) + `calls` edge (kind 1, line 832).
  **There is ALREADY a B3 block (lines 941–961)** that emits an `instantiates`
  edge (kind 5) but from `src_id` (the **caller fn**) → primary template — NOT the
  ADR model (member → template-method). This is the pre-existing
  "instantiation_sites" semantics.
- `ast.py:987–1060` VAR_DECL handler: a second pre-existing B3 block that emits
  `instantiates` from `src_id` (caller fn) → primary class template + writes
  `template_arg` rows with **owner_id = src_id** (the using function). This is the
  current home of class-template arg capture, and it is NOT the ADR model either
  (ADR wants args on the `X<int>` TYPE node).
- `ast.py:1294–1366` declaration-level STRUCT/CLASS specialization handler: the
  pattern the ADR says to mirror — `clang_getSpecializedCursorTemplate`, kind-5 vs
  kind-4 decision, `template_arg` rows with `literal = arg_type.spelling` + `ref_id`
  via `arg_type.get_declaration().get_usr()`.
- `storage.py:32` `SCHEMA_VERSION = 12`; `:146–149` edge_kind seed (5 instantiates,
  9 method_of already present); `:189–196` `template_arg` DDL; `:307–449` `_migrate`
  (ALTER-ADD-COLUMN idempotent pattern, `changed` flip, version bump only-if-older);
  `:1092` `mint_symbol_id`; `:1167` `add_edge`; `:1279` `add_template_arg`.
- C++ mirror parity confirmed present: `ast.cpp:1090–1242` (CALL_EXPR + B3
  instantiates), `ast.cpp:1275–1340` (VAR_DECL B3), `ast.cpp:1688–1761`
  (declaration specialization). `storage.hpp:29 kSchemaVersion = 12`;
  `storage.cpp:480–589` `migrate`; `storage.cpp:208–229` `kSymbolInsertCols` (20
  cols) + `kSymbolCols`/`kSymbolColsS` explicit SELECT lists; `records.hpp:40–65`
  `Symbol`. `add_template_arg(const TemplateArg&)` at `storage.hpp:203`.

Key consequence of the current state: the new ADR edges are **additive** to the
existing B3 edges. The existing caller→template `instantiates` edge and its
`owner_id=caller` template_arg rows are NOT removed (removing them would change
`instantiation_sites()` / `model.template_arguments` on the function and break
default behaviour). The ADR adds the member-node and type-node edges + type-node
args alongside them. See §"Row-growth note" for why this does not double-count in
the readers.

---

## 1. Open item resolutions (the three ADR follow-ups)

### 1a. Instantiation marker — DECISION: add `symbol.is_instantiation INTEGER NOT NULL DEFAULT 0` column

Chosen: an explicit additive column, NOT edge-presence-only.

Rationale:
- **Disambiguation.** "Has an outgoing `instantiates` edge" is NOT a sound marker:
  an **explicit** instantiation (`template class Wrapper<int>;`, declaration handler
  at `ast.py:1330`) and a **function that instantiates** (caller→template B3 edge,
  `ast.py:961`) both already own outgoing kind-5 edges but are NOT implicit
  instantiation nodes. Edge-presence would mis-mark the caller function and the
  explicit-instantiation record. A column states the fact directly and locally.
- **Cheap reads.** Readers filter `WHERE is_instantiation = 1` without a join/anti-
  join against the edge table; the rollup readers (`callers(include_instantiations)`)
  need a fast "is this a template node with instantiation children" predicate that a
  column supports cleanly.
- **Precedent + cost.** This is the exact, already-paid pattern of `is_static`
  (v11→v12) and `is_pure`: one additive `ALTER TABLE symbol ADD COLUMN … DEFAULT 0`,
  no backfill, reindex repopulates. The marginal C++ cost (extend the 20→21-entry
  `kSymbolInsertCols`, the two explicit SELECT lists, the decode index, the upsert
  `MAX()` merge) is mechanical and mirrors `is_static` line-for-line.

Set to 1 on BOTH new nodes minted by the ADR extraction: the `X<int>` type node and
each `X<int>::member` node. All other rows stay 0.

### 1b. Method-template targs (`Y::print<int>`) — DECISION: ship class-template args now; log the method-template targs gap

Chosen: extract class-template instantiation args now via
`parent.type.get_template_argument_type(i)`; for a **method template**
(`Y::print<int>`) mint the node + `instantiates` edge + `method_of` (all unaffected),
but DO NOT attempt token-extent parsing of `print < int >` in this change — write a
`cidx.log` note and a test asserting the node exists with empty targs.

Rationale:
- libclang 18.1.1 returns `get_num_template_arguments() == -1` for the method-
  template cursor (verified, ADR §Consequences), so cursor-based extraction yields
  nothing; the only path to `targs=[int]` is token-extent parsing of the call
  expression, the same tokenizing approach used for explicit-instantiation detection
  (`_is_explicit_instantiation`, `[[cidx-template-instances]]`).
- The node + `instantiates` + `method_of` already answer the primary defect
  ("callers of `Y::print`" rolls up); only the `targs` annotation is missing, which
  is a strictly-additive future enhancement that needs NO schema change (the
  `template_arg` table already exists). Token parsing in the same pass widens blast
  radius and risks parity drift (Python `clang.cindex` tokens vs C++ `clang_tokenize`
  whitespace/macro edge cases) for a secondary annotation.
- Deferring keeps Python/C++ extraction byte-identical (both emit node + 2 edges, no
  args), which is what the `#18 parity_check` `.dump` diff requires.

Follow-up logged: "method-template targs via token-extent parse" — separate change,
schema-free, both languages.

### 1c. Row-growth blast radius — DECISION: bound growth by the existing "primary-must-be-indexed" guard; measure on librdkafka before merge; no opt-out flag

Chosen: reuse the existing guard that the **primary template must already be in the
DB** (`db.lookup_symbol(prim_usr) is not None`) before minting any instantiation
node/edge. Do NOT mint nodes for stdlib/std:: instantiations (their primaries live
in unindexed system headers and are never `lookup`-resolvable). Measure DB-size and
`resolve` time delta on the librdkafka corpus (8774 symbols / 40085 edges baseline,
per memory `cidx-graph-layer`) before merge. No `--no-instantiation-nodes` flag —
default behaviour stays byte-identical because the new nodes/edges are purely
additive and the rollup readers are opt-in.

Rationale + bound:
- The current B3 blocks already gate on `lookup_symbol(prim_usr) is not None`
  precisely to avoid inflating stub counts for `std::vector`/`std::move`
  (`ast.py:942–944`, `:1003`). Reusing it caps new rows to instantiations of
  **first-party** templates (graphlab: `Wrapper`, `Stack`; a real C++ repo: its own
  templates), not the libstdc++ explosion the ADR warns about.
- Worst-case per first-party instantiation member call: +2 symbol rows (`X<int>`
  type once per instantiation, `X<int>::member` once per used member), +2
  `instantiates` edges, +1 `method_of` edge, + N `template_arg` rows on the type
  node. The type node and its args are de-duped by USR (`mint_symbol_id` upsert /
  `add_template_arg` INSERT-OR-REPLACE keyed on `(owner_id,position)`), so repeated
  uses of `X<int>` across a TU do not multiply rows.
- Measurement gate (run on the dev box gcc-index-test VM 192.168.1.115 with the
  librdkafka build, per memory `gcc-index-test-box`): record `symbols`, `edges`,
  `template_arg` count, `index.db` byte size, and `cidx resolve` wall-time at v12 vs
  v13 on the same corpus; require the delta to be reported in the PR description.
  This is a **gate, not a code change**.

---

## 2. File edits (Python + C++ parity mirror)

Parity rule: every `ast.py` / `storage.py` extraction+storage change lands in the
C++ mirror on the same branch. `model.py` / `query.py` readers are Python-only and
EXEMPT.

### Extraction — `project/indexer/clang/ast.py`  (mirror: `cidx-cpp/src/clangx/ast.cpp`)

E1. In `_body_descent` CALL_EXPR handler, AFTER the existing member-stub mint +
`calls` edge (after line 832) and alongside the existing B3 caller→primary edge
(lines 941–961), add an **instantiation-member promotion** block, guarded by
`primary = clang_getSpecializedCursorTemplate(ref)` being a real cursor AND the
member's `semantic_parent` being a template instantiation:
  - (a) `db.mint_symbol_id(member_usr, …, kind from _KIND_MAP)` is already done as
    `dst_id`; **set `is_instantiation=1` on it** (new mint_symbol_id kwarg, §storage).
  - (b) add `instantiates` edge `dst_id → prim_member_id` where `prim_member_id =
    mint_symbol_id(primary.get_usr(), …)` (the template method
    `c:@ST>1#T@X@F@print#`), guarded by primary already indexed.
  - (c) mint the `X<int>` TYPE node: `parent = ref.semantic_parent`;
    `type_usr = parent.get_usr()`; `type_id = mint_symbol_id(type_usr,
    parent.spelling, …, kind='struct'/'class' via _KIND_MAP, is_instantiation=1)`.
  - (d) add `method_of` edge (kind 9) `dst_id → type_id`.
  - (e) add `instantiates` edge `type_id → class_primary_id` where
    `class_primary_id = lookup/mint of clang_getSpecializedCursorTemplate(parent)`,
    guarded by class primary already indexed.
  - (f) write `template_arg` rows on the TYPE node (`owner_id = type_id`) from
    `parent.type.get_template_argument_type(i)`, mirroring `ast.py:1338–1365`
    exactly: TYPE arg → `arg_kind=1`, `literal = arg_type.spelling`, `ref_id` via
    `arg_type.get_declaration().get_usr()` when named; INTEGRAL → `arg_kind=2`,
    `literal = value`; else `arg_kind = int(tak)`. De-duped by `(owner_id,position)`.
  - Guard the whole block so it is a no-op when `primary` is null/invalid or the
    primary is not yet indexed (no stubs for std:: — same guard as existing B3).
  - For a **method template** (1b): steps (a),(b),(d),(e) still run; step (f) is
    skipped when `parent.type.get_num_template_arguments()`/cursor returns < 0 — log
    once via the existing `cidx.log` logger ("method-template targs unavailable from
    cursor; node+edges emitted").

E2. Mirror E1 in `ast.cpp` CALL_EXPR handler (after `emit_call_edge`/`emit_call_args`
at lines 1211–1215, alongside the existing B3 at 1217–1242), using
`clang_getCursorSemanticParent`, `clang_getSpecializedCursorTemplate`,
`clang_Type_getNumTemplateArguments` / `clang_Type_getTemplateArgumentAsType`, and
`clang_getCursorType(parent)`. Set `Symbol.is_instantiation = true` on the minted
type and member symbols (the C++ mint path constructs a `Symbol` record — set the
field before upsert). Build `Edge` records with `kind = 5` and `kind = 9` and
`TemplateArg` records with `owner_id = type_id`, identical literal/ref_id logic to
`ast.cpp:1719–1761`.

PARITY NOTE: the existing caller→primary B3 edges (E-pre, `ast.py:961` /
`ast.cpp:1239` and the VAR_DECL block) are **left untouched** in both languages —
removing them is out of scope and would change `instantiation_sites()`.

### Storage — `project/indexer/storage.py`  (mirror: `storage.{hpp,cpp}` + `records.hpp`)

S1. `SCHEMA_VERSION = 12 → 13` (`storage.py:32`); `kSchemaVersion = 12 → 13`
    (`storage.hpp:29`); `INSERT … schema_version VALUES ('12')` → `'13'`
    (`storage.cpp:173`).
S2. Add `is_instantiation INTEGER NOT NULL DEFAULT 0` to the `symbol` DDL in BOTH
    `_SCHEMA` (`storage.py`, after `is_static` at line 123) and `kSchema`
    (`storage.cpp`, after `is_static` at line 87). Comment: "v13: implicit template
    instantiation node (own USR, definition via `instantiates` edge)."
S3. `Symbol` dataclass (`storage.py:264`): add `is_instantiation: bool = False`
    after `is_static`. `_row_to` (`storage.py:279`): add
    `kwargs["is_instantiation"] = bool(kwargs["is_instantiation"])`.
    `records.hpp:57`: add `bool is_instantiation = false;` after `is_static`.
S4. `mint_symbol_id` (`storage.py:1092`): add `is_instantiation: bool = False`
    param; include the column in the INSERT and in the upsert `DO UPDATE SET` as
    `is_instantiation = MAX(excluded.is_instantiation, symbol.is_instantiation)`
    (so a later real-def upsert never clears it, and a stub→instantiation upgrade
    sets it). Mirror in the C++ mint path + the `upsert_symbol` `MAX()` block at
    `storage.cpp:1099–1101` and the `kSymbolInsertCols` array (20→21 entries,
    `storage.cpp:208`), `kSymbolCols` + `kSymbolColsS` SELECT lists
    (`storage.cpp:221–229`), and the decode in `row_to_symbol` (add a
    `col_int64(idx) != 0` read at the new index — append it at the END of the SELECT
    list like `decl_path`, so MIGRATED DBs that ALTER-appended the column decode at a
    stable position; see the file header note at `storage.cpp:1–5`).
S5. Migration block in `_migrate` (`storage.py`, after the `is_static` block at
    line 366–372) and in `migrate()` (`storage.cpp`, after the `is_static` block at
    line 502–508): the exact additive idempotent ALTER — see §3.
S6. No new `add_*` mutator: the new edges go through existing `add_edge(src,dst,5)` /
    `add_edge(src,dst,9)`; the new args through existing `add_template_arg`. No
    `template_arg`/`edge`/`edge_kind` DDL change (kinds 5 and 9 already seeded).

### Readers (Python-only, EXEMPT from parity) — `query.py` / `model.py`

R1. `query.py`:
    - `instantiations(self, sym)` — NEW: incoming `instantiates` edges to `sym`
      filtered to sources with `is_instantiation = 1` (distinguishes ADR instance
      nodes from the existing caller→template `instantiation_sites` sources).
    - `template_of(self, sym)` — NEW: outgoing `instantiates` target of an instance
      node (its definition / primary template).
    - `template_args(self, sym)` — EXISTING (`query.py:1088`), unchanged; already
      reads `template_arg` by `owner_id`, which now resolves on the `X<int>` type
      node.
    - `callers(self, sym, …, include_instantiations: bool = False)` and the matching
      `callees(...)` — extend: when `include_instantiations` and `sym` is a template
      method/class, UNION the callers/callees of its instantiation members (found via
      incoming `instantiates` where source `is_instantiation = 1`), annotating each
      result with the instance's `template_args`. Default `False` → byte-identical to
      today (`query.py:833–839`).
R2. `model.py`:
    - `Record.template_arguments` (`model.py:1297`) — unchanged; now also returns the
      args of an `X<int>` instance Record (owner_id = type node).
    - `Record.instantiations()` (`model.py:1473`) — unchanged semantics, now also
      surfaces ADR implicit-instantiation Records (they carry `is_instantiation`).
      Add an `instance_of` / `template_of` property on the instance Record wrapping
      `query.template_of`.
    - `Callable.callers(include_instantiations=False)` /
      `callees(include_instantiations=False)` (`model.py:931–935`) — thread the new
      opt-in flag through to `query.callers/callees`.
    - Optional surfacing of `is_instantiation` on the `Sym`/`Entity` wrapper for
      `cidx show`.

---

## 3. Exact additive v12 → v13 migration

Additive, idempotent (column-existence guarded), no backfill (reindex repopulates),
version bump only when stored version is older. Mirrors the `is_static` v11→v12 block
exactly.

### Python — `storage.py` `_migrate`, inserted after the `is_static` block (≈line 372)

```python
if "is_instantiation" not in cols:
    # v12 -> v13: implicit template-instantiation node marker. No backfill
    # possible from stored data -- reindex to populate; old rows read as 0.
    self._conn.execute(
        "ALTER TABLE symbol ADD COLUMN is_instantiation INTEGER NOT NULL DEFAULT 0"
    )
    changed = True
```

(`cols` is the existing `{r[1] for r in PRAGMA table_info(symbol)}` set already
computed at the top of `_migrate`. The trailing version-bump logic at
`storage.py:434–448` already writes `'13'` when `changed` is True or the stored
version is `< SCHEMA_VERSION`.)

### C++ — `storage.cpp` `migrate()`, inserted after the `is_static` block (≈line 508)

```cpp
if (!has_col(cols, "is_instantiation")) {
  // v12 -> v13: implicit template-instantiation node marker. No backfill
  // possible from stored data -- reindex to populate; old rows read as 0.
  db_.exec(
      "ALTER TABLE symbol ADD COLUMN is_instantiation INTEGER NOT NULL DEFAULT 0");
  changed = true;
}
```

(`cols` is the existing `table_columns("symbol")`. The trailing bump at
`storage.cpp:583–588` writes `std::to_string(kSchemaVersion)` = `"13"`.)

### Operational step (not code)

After merge: rebuild the standard index — `cidx index` / `cidx resolve` against
`~/.cache/cidx/index.db` (v12 DB auto-migrates the column on open, then a reindex
populates `is_instantiation` and the new nodes/edges). REINDEX REQUIRED.

---

## 4. Reader API surface

Python (model / query — Python-only, EXEMPT from parity):
- `query.instantiations(sym)` → list of instance nodes (incoming `instantiates`,
  source `is_instantiation=1`). Maps to `Record.instantiations()` /
  `template.instantiations()`.
- `query.template_of(instance)` → the primary template (outgoing `instantiates`
  target). Maps to `Record.template_of` / `instance.template_of()`.
- `query.template_args(sym)` (existing) → `[TemplateArg]` on the `X<int>` type node.
  Maps to `Record.template_arguments`.
- `query.callers(sym, include_instantiations=False)` /
  `query.callees(sym, include_instantiations=False)` — opt-in rollup over
  instantiation members, each result annotated with the instance's `template_args`.
  Maps to `Callable.callers(include_instantiations=False)` /
  `Callable.callees(include_instantiations=False)`. Default OFF → byte-identical.
- `Sym.is_instantiation` surfaced for `cidx show`.

C++ `cidx graph` flag — **DRIFT FLAG, cannot implement as specified**:
The ADR asks "the `cidx graph callers/callees` CLI (C++) gains the same opt-in flag."
There is **no `cidx graph` subcommand in the C++ binary** (verified: no `graph`
command in `cli/commands.cpp` / `cli/args.cpp`; the graph query CLI is Python-only,
"C++ port pending" per memory `cidx-graph-query-layer`). Therefore the C++ opt-in
flag is **deferred and bundled with the future C++ graph-query port** — it is NOT in
scope for this change and cannot be, because its host command does not exist. This is
recorded as a gap, not silently dropped. The C++ EXTRACTION + STORAGE parity (which
DOES exist) is fully delivered; only the C++ READER flag is blocked on a missing
subcommand.

---

## 5. Test matrix

### Hermetic pytest (Python)
- `test_instantiation_member_node`: index graphlab; assert `Wrapper<int>::label`
  exists as a node with `is_instantiation=1` and an `instantiates` edge to
  `Wrapper::label` (template method), and a `method_of` edge to `Wrapper<int>`.
- `test_instantiation_type_node`: assert `Wrapper<int>` exists with
  `is_instantiation=1`, an `instantiates` edge to `Wrapper` (primary), and
  `template_args == [TemplateArg(0, type, ref_id?, 'int')]`.
- `test_instance_distinguished_by_targs`: a new fixture with `X<int>` AND `X<double>`
  → both type nodes present, distinguished by `template_arg.literal`
  ('int' vs 'double') and, when the arg is a named type, by `ref_id`.
- `test_callers_rollup_optin`: `callers(Wrapper::label, include_instantiations=True)`
  returns `main`-side caller(s) of `Wrapper<int>::label` annotated `[T=int]`;
  `callers(Wrapper::label)` (default) stays empty/unchanged — proves opt-in.
- `test_callees_rollup_optin`: symmetric for `callees`.
- `test_default_behaviour_unchanged`: `callers/callees` without the flag, and
  `template_arguments` on a non-instance record, return exactly the v12 result
  (snapshot/regression).
- `test_method_template_node_targs_absent`: NEW fixture (member function template,
  `struct Y { template<class T> void print(){} };` + `Y y; y.print<int>();`) →
  `Y::print<int>` node present with `is_instantiation=1` + `instantiates` edge to
  `Y::print` + `method_of` to `Y`, and `template_args == []` (documents the 1b gap).
- `test_migration_v12_to_v13`: open a synthetic v12 DB (no `is_instantiation`
  column), assert `_migrate` adds the column with DEFAULT 0, bumps meta to '13', is
  idempotent on second open, and never downgrades a v14 DB.
- `test_no_stub_for_std_instantiation`: a TU using `std::vector<int>` mints NO
  `vector<int>` instance node (primary unindexed → guard holds) — blast-radius guard.

### ctest (C++) + `#18 parity_check`
- `ast_test` (C++): mirror `test_instantiation_member_node` /
  `test_instantiation_type_node` / `test_method_template_node_targs_absent` against
  graphlab + the new method-template fixture, asserting the same nodes/edges/args
  from the C++ extractor.
- `graph_storage_test` (C++): migration test — v12 DB opened by C++ `migrate()` gains
  the column, bumps to '13', idempotent, no downgrade.
- **`parity_check` (ctest #18, label `parity`)**: the gate. After the extraction +
  storage edits land in BOTH languages, the `.dump` of the Python-built and
  C++-built `index.db` over the graphlab + project fixtures must be byte-identical
  (excluding mtime/indexed_at). The new `is_instantiation` column, the new
  `instantiates`/`method_of` edges, and the new type-node `template_arg` rows must
  appear identically in both dumps. This is the hard parity proof.

### Reindex worked-example check (manual, gated in PR)
- Rebuild graphlab at v13; run the worked-example queries from the spec:
  `callers(Wrapper::label, include_instantiations=True)` →
  `{ Wrapper<int>::label ← main [T=int] }`; callgraph from `main` →
  `main ─calls→ Wrapper<int>::label ─instantiates→ Wrapper::label`. Confirm against
  the spec's expected callgraph (`cidx-template-instantiation-nodes.md` §Worked
  example).
- librdkafka blast-radius measurement (§1c): v12 vs v13 symbol/edge/template_arg
  counts, `index.db` size, `cidx resolve` time — reported in the PR.

---

## Design conformance / drift flag

Conforms to ADR-004 exactly on: instantiation nodes own their USR; definition via
`instantiates` (kind 5, REUSE); `method_of` (kind 9); structured args on the
`X<int>` TYPE node via `template_arg` (literal + ref_id, no USR parsing);
`clang_getSpecializedCursorTemplate` for targets; schema bump 12→13 additive +
idempotent; default behaviour byte-identical with opt-in rollup; Python/C++
extraction+storage parity, Python-only readers.

ONE flagged gap (reality, not a design change): the ADR's C++
`cidx graph callers/callees` opt-in flag **cannot be implemented** — that subcommand
does not exist in the C++ binary (graph CLI is Python-only). The C++ extraction +
storage + migration parity IS delivered in full; the C++ reader flag is deferred to
the future C++ graph-query port and logged here rather than silently dropped. No
scope was added beyond the ADR's enumerated follow-ups.

# 06 — Graph Layer (schema v6→v7) Implementation Step-Plan + Probe Ground Truth

Stage 4 (senior-developer) deliverable for the cidx relationship-graph layer.
Locked design: `~/workspace/wiki/pages/planning/cidx-graph-layer.md` (read every
section). This plan turns that design into an ordered, file-by-file build plan
for **both** the C++ port (`cidx-cpp`) and the Python `cidx`, plus the
probe-confirmed ground truth and the test matrix.

> **Architect-spec note:** `spec/05-graph-design.md` does **not** exist in the
> repo at plan time (only `01..04` + `stories/`). The wiki planning page is the
> authoritative locked design and is treated as canonical here. No schema
> problem was found during probing — every column the design specifies maps to a
> confirmed libclang fact (see Appendix). One naming caveat to flag to the
> architect: the design's `edge_site` PRIMARY KEY is `(edge_id, file_id, line,
> col)`; `file_id` is nullable and SQLite allows NULL in a `WITHOUT ROWID` PK
> only if every other PK column disambiguates — confirmed acceptable because
> `line`/`col` are always set for call sites (see Risk R4).

Goal (one sentence): add a typed relationship-graph layer (`edge_kind`, `edge`,
`edge_site`, `template_param`, `template_arg`) over the existing symbol index,
extracted inside `cidx index` (skippable with `--no-graph`) and finalized by a
new DB-only `cidx resolve` subcommand, bumping the schema `6 → 7` in both tools
with byte/semantic parity.

---

## 0. Canonical toolchain (cpp-conventions + python-conventions)

Learned from `CMakeLists.txt`, `Makefile`, `tests/CMakeLists.txt`,
`parity_check.sh` — **do not guess these**:

**C++ (`cidx-cpp/`)**
- Standard: C++23 (`CMAKE_CXX_STANDARD 23`), AppleClang ≥15 / gcc ≥13 / clang ≥16.
- Configure + build (canonical):
  ```bash
  cd /Users/husam/workspace/qemu-vms/libclang-lab/cidx-cpp
  cmake -S . -B build              # or: make build
  cmake --build build -j4          # or: make
  ```
  (`make` wraps the same; `make CIDX_LIBCLANG=/path` to override libclang.)
- Full unit suite:
  ```bash
  cd build && ctest --output-on-failure          # all labels
  cd build && ctest -L default --output-on-failure   # hermetic suites only
  cd build && ctest -L clang   --output-on-failure   # real-libclang suites
  ```
  Labels (from `tests/CMakeLists.txt`): `default` (hermetic), `clang` (real
  parses, SKIP_RETURN_CODE 77 on fixture gap), `parity` (S08), `e2e` (manual,
  unregistered). New graph cases extend `storage_smoke_test`,
  `storage_migration_test`, `ast_test`, `cli_test` — re-use the existing
  one-exe-two-registrations pattern (hermetic cases `default`, real-parse cases
  `clang`).
- Parity gate:
  ```bash
  cd build && ctest -L parity --output-on-failure
  # or directly: CIDX_CPP_BIN=$PWD/build/cidx bash scripts/parity_check.sh
  ```
  Needs `uv`, the Python launcher `project/cidx`, `sqlite3`. It diffs command
  transcripts AND `sqlite3 .dump` of both DBs (mtime/indexed_at normalized).
  **Graph rows are included in the dump → Python and C++ edge/template output
  must be byte-identical** (see Risk R7).
- Style: `-Wall -Wextra` (already on every target); match the existing
  storage.cpp `migrate()` / character-for-character-SQL convention.

**Python (`project/indexer/`)**
- Launcher `project/cidx` (uv-driven). Schema constant `SCHEMA_VERSION` in
  `indexer/storage.py`. Mirror every SQL string verbatim between the two tools.

---

## 1. Schema + migration (do this first — both tools)

### 1a. C++ `src/storage/storage.cpp` / `storage.hpp`

1. **Bump version**: `storage.hpp` `constexpr int kSchemaVersion = 6;` → `7`.
2. **Extend `kSchema[]`** (append after the symbol indexes, before the
   `INSERT OR IGNORE INTO meta`): the five tables **verbatim from design §2/§6**
   — `edge_kind`, `edge`, `edge_site` (`WITHOUT ROWID`), `template_param`
   (`WITHOUT ROWID`), `template_arg` (`WITHOUT ROWID`), the two edge indexes
   (`idx_edge_src`, `idx_edge_dst`), and the `edge_kind` seed
   `INSERT OR IGNORE ... VALUES (1,'calls')..(9,'method_of')`. Change the final
   `meta` seed value from `'6'` to `'7'`.
   - `CREATE TABLE IF NOT EXISTS` + `INSERT OR IGNORE` make the whole script
     **idempotent** — re-running on a v7 DB is a no-op (matches the existing
     pattern; this is why `migrate()` need add nothing for a *fresh* v7).
3. **`migrate()`**: append a new presence-gated block AFTER the existing
   `file.driver` block, BEFORE the `if (changed)` meta-bump:
   ```cpp
   if (!has_table("edge")) {
     // v6 -> v7: graph layer. The schema script (run AFTER migrate) creates
     // the tables + indexes + seeds edge_kind; nothing to backfill from
     // stored data (edges are derived — re-run `cidx index`/`resolve`).
     changed = true;
   }
   ```
   The actual `CREATE`s live in `kSchema` (G19 ordering: migrate detects, schema
   script creates — same as every prior version). The bump uses the existing
   `UPDATE meta SET value = ?` with `kSchemaVersion` (now 7), so it writes `'7'`.
   - **Idempotency**: on an already-v7 DB `has_table("edge")` is true → block
     skipped → `changed` stays false unless another column was added → meta not
     re-touched. On a v6 DB the block fires once. The existing
     "newer DB opens without refusal" test (migration test, currently asserts
     `'7'` survives an unknown column) must be updated — see Test Matrix T1.
4. **New Storage methods** (declare in `storage.hpp`, define in `storage.cpp`,
   mirror the existing upsert style with `RETURNING`):
   - `int64_t edge_kind_id(std::string_view name);` — `SELECT id FROM edge_kind
     WHERE name=?` (cached in a `std::map` member for the indexing loop).
   - `int64_t mint_stub_symbol(std::string_view usr);` — `INSERT INTO symbol
     (usr, spelling, kind, resolved) VALUES (?, '', 'function', 0) ON
     CONFLICT(usr) DO UPDATE SET usr=usr RETURNING id`. **Flag to architect:**
     `symbol.spelling`/`kind` are `NOT NULL` with a `kind` CHECK; a stub must
     supply a placeholder `kind`. Recommend `kind` derived from the *referencing
     context* when known (callee of a CALL_EXPR → `'function'`), else
     `'function'` default; spelling `''`. The real def later upserts via
     `add_symbol` (MAX(resolved) keeps it resolved). See Risk R3.
   - `int64_t upsert_edge(int64_t src_id, int64_t dst_id, int kind, ...scalars);`
     — `INSERT ... VALUES (...) ON CONFLICT(src_id,dst_id,kind) DO UPDATE SET
     count = edge.count + 1, base_access=COALESCE(excluded.base_access,...),
     ... RETURNING id`. Returns edge id for `edge_site` linkage.
   - `void add_edge_site(int64_t edge_id, std::optional<int64_t> file_id, line,
     col, int conditional, std::optional<std::string> args_sig);` — `INSERT OR
     IGNORE` (PK collision = same site seen twice, harmless).
   - `void add_template_param(...)`, `void add_template_arg(...)` — `INSERT OR
     IGNORE` keyed on `(owner_id, position)`.
   - `void clear_graph();` — `DELETE FROM edge; DELETE FROM template_arg; DELETE
     FROM template_param;` (edge_site cascades from edge). For `resolve`
     rebuild and `--force` reindex.
   - `int resolve_pass();` — see §4.
   - Record structs in `records.hpp`: `Edge`, `EdgeSite`, `TemplateParam`,
     `TemplateArg`, `EdgeKind` (plain data, `int64_t id = -1` convention).

### 1b. Python `indexer/storage.py`
- `SCHEMA_VERSION = 6` → `7`.
- Append the five `CREATE TABLE` + indexes + `edge_kind` seed to `_SCHEMA`
  (the f-string) — **identical SQL text** to the C++ `kSchema`. Bump the meta
  seed to `{SCHEMA_VERSION}` (already interpolated).
- `_migrate()`: add `if "edge" not in tables: changed = True` after the
  `file.driver` block (same detect-only pattern).
- Add the mirror methods: `edge_kind_id`, `mint_stub_symbol`, `upsert_edge`,
  `add_edge_site`, `add_template_param`, `add_template_arg`, `clear_graph`,
  `resolve_pass`. Same SQL strings, same column order.
- Add `@dataclass` rows mirroring `records.hpp`.

---

## 2. AST edge/template extraction (both tools)

### 2a. C++ `src/clangx/libclang.hpp` — add forwarding methods
All declared in vendored `third_party/clang-c/` (verified present). Add inline
forwarders mirroring the existing ones:
`clang_getCursorReferenced`, `clang_isVirtualBase`,
`clang_Cursor_getNumTemplateArguments`, `clang_Cursor_getTemplateArgumentKind`,
`clang_Cursor_getTemplateArgumentType`, `clang_Cursor_getTemplateArgumentValue`,
`clang_getSpecializedCursorTemplate`, `clang_getTypeDeclaration`,
`clang_getCanonicalCursor`. (`clang_getCXXAccessSpecifier` already exists.)

### 2b. C++ `src/clangx/ast.cpp` / `ast.hpp` — new edge walk
The existing `walk_visitor` deliberately returns `CXChildVisit_Continue` on
function-like cursors so bodies are **never** walked (confirmed: 0 CALL_EXPRs
reached — Appendix P1). Do **not** change that — symbol extraction stays as is.
Add a **separate, additive** edge pass:

1. `struct EdgeExtract { ... }` collected counters (mirrors `HeaderStats`).
2. `void AstIndexer::index_edges(const ParsedTu &tu, const std::string
   &filename, int64_t file_id);` — for each top-level cursor in `filename`:
   - **calls**: when the cursor is a function-like **definition**, run a manual
     recursive descent over its full subtree (NOT `for_file_cursors` — that
     prunes bodies). For each `CXCursor_CallExpr`: `callee =
     clang_getCursorReferenced(call)`; `callee_usr =
     clang_getCursorUSR(callee)`. `src_id = symbol id of the enclosing function
     def`; `dst_id = lookup_symbol(callee_usr).id` or `mint_stub_symbol`. Emit
     `upsert_edge(src,dst, kind=calls)` + `add_edge_site(edge_id, file_id, line,
     col, conditional)`. Track a `cond_depth` counter incremented when
     descending into `IF_STMT/FOR_STMT/WHILE_STMT/DO_STMT/SWITCH_STMT/CASE_STMT/
     CONDITIONAL_OPERATOR` (Appendix P6 confirms detection). `conditional =
     cond_depth > 0`.
   - **inherits**: for `CXCursor_CXXBaseSpecifier` children of a class/struct
     def → `dst = clang_getCursorReferenced(base)` (Appendix P3). Edge
     `kind=inherits`, `base_access = clang_getCXXAccessSpecifier(base)`,
     `is_virtual = clang_isVirtualBase(base)`.
   - **field_of / method_of**: for `FIELD_DECL` → `field_of` (src=member,
     dst=owning record); for `CXX_METHOD/CONSTRUCTOR/DESTRUCTOR` → `method_of`.
     Owner = `clang_getCursorSemanticParent`. Member access already on
     `symbol.access` — do NOT duplicate on the edge (design §1). (Appendix P4.)
   - **specializes**: for a specialization decl,
     `clang_getSpecializedCursorTemplate(cursor)` → primary template; edge
     `kind=specializes`.
   - **instantiates** + **template_arg**: when a cursor's type is a class
     specialization, `decl = clang_getTypeDeclaration(type)`;
     `clang_getSpecializedCursorTemplate(decl)` → primary → edge
     `kind=instantiates`. For each arg `i` in
     `clang_Cursor_getNumTemplateArguments(decl)`: `kind =
     clang_Cursor_getTemplateArgumentKind`; TYPE → `ref_id =
     symbol id of clang_getTypeDeclaration(getTemplateArgumentType).usr` (stub
     if unseen); INTEGRAL → `literal = getTemplateArgumentValue`. Store via
     `add_template_arg(owner_id=specialization symbol id, position=i, ...)`.
     (Appendix P5 confirms `Box<Widget>` arg ref_usr = `c:@S@Widget`.)
   - **template_param**: for `CLASS_TEMPLATE`/`FUNCTION_TEMPLATE` decls, iterate
     `TEMPLATE_TYPE_PARAMETER` / `TEMPLATE_NON_TYPE_PARAMETER` /
     `TEMPLATE_TEMPLATE_PARAMETER` children → `add_template_param(owner_id,
     position, param_kind, name, default_txt)`. (Appendix P5.)
   - **contains**: design OQ-2 unresolved — keep relying on `symbol.parent_usr`;
     do NOT emit `contains` edges in this iteration (out of scope; flag for a
     follow-up story).
3. One `db.transaction()` per file wraps symbols **and** edges together (design
   §2 ACID guarantee). **Wiring**: edges must extract in the SAME txn as
   symbols, so the edge pass runs immediately after `index_symbols` for the
   same file id, before commit. Refactor `index_file` to optionally run the
   edge pass, OR have `cmd_index` call `index_symbols` then `index_edges` and
   move the transaction boundary up (see §3 / Risk R1).
4. `ast.hpp`: declare `index_edges`; add a `bool graph` flag plumbed from the
   CLI.

### 2c. Python `indexer/clang/ast.py`
- Mirror: a recursive `_call_edges`, `_inheritance_edges`, `_member_edges`,
  `_template_edges` over `tu`. Use `cursor.referenced`, `cursor.get_children()`,
  `cx.AccessSpecifier`, `cursor.get_definition()`,
  `type.get_declaration().get_num_template_arguments()` etc. (the Python binding
  lacks `is_virtual_base` on Cursor — Appendix P3; call the underlying conf
  function or read it via the type API; if truly unavailable, store NULL and
  document the parity caveat — flag R6). Same edge kinds, same conditional rule,
  same iteration order as C++ so the `.dump` diff is byte-equal.
- `index_source` / `index_symbols` gain a `graph: bool = True` parameter.

---

## 3. `cmd_index` wiring + `--no-graph` (both tools)

### 3a. C++
- `src/cli/args.hpp`: add `bool no_graph = false;` to `ParsedArgs`.
- `src/cli/args.cpp`: add `{"--no-graph", '\0', ValueKind::kNone, "--no-graph",
  nullptr, 0}` to `kIndexSpec.opts`; update `kIndexUsage` / `kIndexHelp` text
  (verbatim-parity: the Python `argparse` help must be regenerated and copied
  byte-for-byte — capture with `COLUMNS=80 python3 -m indexer index -h`). Set
  `pa.no_graph = st.flags.count("--no-graph") != 0;` in the index branch.
- `src/cli/commands.cpp` `cmd_index` / `index_one`: pass `!args.no_graph` into
  the indexer; after `index_symbols` + `index_headers`, call
  `indexer.index_edges(...)` for the main file (and each indexed header) **when
  graph is on**, inside the per-file transaction.
- The per-file summary line is golden-frozen (`index_one`); appending graph
  counts changes the transcript. **Decision:** keep the existing line byte-exact
  and add edges silently (parity-safe), OR extend BOTH tools' line identically.
  Recommend: extend identically (e.g. `; edges: N`) and regenerate the golden.
  Flag to architect/QA. (Risk R7.)

### 3b. Python `indexer/cli.py`
- `cmd_index` argparse: `p.add_argument("--no-graph", action="store_true", ...)`.
- `index_source(..., graph=not args.no_graph)`. Identical summary-line change.

---

## 4. `cidx resolve` subcommand (both tools)

DB-only global pass (design §4/§7 Phase B). No parse, no libclang load.

### 4a. C++
- `args.cpp`: register `resolve` in `kCommands`, the top usage/help blocks, and
  a `kResolveSpec` (no required args; optional `--rebuild` to `clear_graph()`
  first). `args.hpp`: `bool rebuild = false;`.
- `commands.cpp`: `int cmd_resolve(const ParsedArgs&, Context&)`:
  ```
  Storage db(ctx.index_path);
  if (args.rebuild) { db.clear_graph(); }   // then edges must be re-extracted
  int n = db.resolve_pass();                // flips symbol.resolved where a def
                                            // now matches a stub usr; rolls up
                                            // edge.count is already maintained
  *ctx.out << "resolve: " << n << " symbol(s) resolved\n";
  ```
  `resolve_pass()` SQL: the stub-minting design means edges already point at
  stable ids; "resolve" here = `UPDATE symbol SET resolved = 1 WHERE resolved =
  0 AND EXISTS (SELECT 1 FROM symbol d WHERE d.usr = symbol.usr AND
  d.is_definition = 1)` is a no-op given USR is UNIQUE (one row per usr). The
  real resolve work is: a stub minted in TU-A is filled by a later
  `add_symbol` in TU-B (MAX(resolved)), so after a full multi-TU `index` the
  flag is already correct. `resolve` therefore reports the count of rows still
  `resolved = 0` that are pure declarations vs genuinely-unresolved stubs, and
  (if `--rebuild`) re-derives. **Flag to architect:** because stub-minting +
  `add_symbol`'s `MAX(resolved)` already resolve cross-TU edges during indexing
  (Appendix P2: USR equality is automatic), `resolve` is mostly a
  reporting/rebuild command, not a correctness requirement. Confirm intended
  scope. (Risk R5.)
- `run_command`: dispatch `"resolve"`.

### 4b. Python `cli.py`: mirror `cmd_resolve`, `--rebuild`, same output line.

---

## 5. Records / dataclasses (both tools)
- C++ `records.hpp`: `EdgeKind`, `Edge`, `EdgeSite`, `TemplateParam`,
  `TemplateArg` structs.
- Python `storage.py`: matching `@dataclass`es (field order = column order).

---

## 6. Test Matrix

All exit-criteria are runnable commands. Build first:
`cmake --build build -j4`. Run with `cd build && ctest ...`.

### Unit tests (extend existing exes — `tests/`)

| id | file | assertion | exit-criteria command |
|----|------|-----------|-----------------------|
| **T1** migration v6→v7 idempotency | `storage_migration_test.cpp` | Open a fresh v7 twice → tables present, meta=`'7'`, second open no-ops. Add a v6 fixture (regen `fixtures/generate_fixtures.py` for v6) → after open: `edge_kind` seeded 9 rows, `edge`/`edge_site`/`template_*` exist, meta=`'7'`, prior symbol rows intact. Update existing "newer DB opens" case (it currently fabricates `'7'`). | `cd build && ctest -R storage_migration_test --output-on-failure` |
| **T2** edge upsert + count | `storage_smoke_test.cpp` | `upsert_edge(a,b,calls)` twice → one `edge` row, `count=2`; `edge_kind_id('calls')==1`; `(src,dst,kind)` UNIQUE enforced. | `cd build && ctest -R storage_smoke_test --output-on-failure` |
| **T3** stub-mint then resolve | `storage_smoke_test.cpp` | `mint_stub_symbol("c:@F@x")` → row `resolved=0`, stable id; later `add_symbol` def with same usr → same id, `resolved=1`; edge pointing at the stub now joins a resolved symbol. | `cd build && ctest -R storage_smoke_test --output-on-failure` |
| **T4** template_arg ref_id join | `storage_smoke_test.cpp` | Insert `Widget` symbol + a `Box<Widget>` specialization symbol + `template_arg(owner=spec, pos=0, arg_kind=type, ref_id=Widget.id)` → `SELECT s.spelling FROM template_arg ta JOIN symbol s ON s.id=ta.ref_id WHERE ta.owner_id=:spec` returns `Widget`. | `cd build && ctest -R storage_smoke_test --output-on-failure` |
| **T5** edge_kind seed | `storage_smoke_test.cpp` | `SELECT id,name FROM edge_kind ORDER BY id` == the 9 design rows exactly. | (covered by T2 run) |

### Functional graph assertions (real parses — `ast_test.cpp` suite `clang`, label `clang`)

Each parses a manifest READ-ONLY (via `CIDX_MANIFESTS_DIR`), indexes into a
`:memory:` Storage, then runs the SQL below.

| id | manifest | assertion | exact SQL |
|----|----------|-----------|-----------|
| **F1** calls in calls.c | `calls.c` | exactly 7 call edges/sites; `main→compute`, `main→printf`, `compute→mid`, `compute→recurse`, `mid→leaf_a`, `mid→leaf_b`, `recurse→recurse` (self). | `SELECT s.spelling, d.spelling FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=(SELECT id FROM edge_kind WHERE name='calls') ORDER BY s.spelling,d.spelling;` and `SELECT COUNT(*) FROM edge_site;` == 7 |
| **F2** inheritance+virtual+access | `geometry.cpp` (pulls `geometry.hpp`) | `Circle --inherits--> Shape`, `base_access`=public(1), `is_virtual`=0. | `SELECT s.spelling,d.spelling,e.base_access,e.is_virtual FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=(SELECT id FROM edge_kind WHERE name='inherits');` |
| **F3** field_of/method_of | `geometry.cpp` | Shape: 1 `field_of` (`name_`), 4 `method_of` (ctor,dtor,area,name). Member access on `symbol.access`. | `SELECT ek.name,COUNT(*) FROM edge e JOIN edge_kind ek ON ek.id=e.kind JOIN symbol d ON d.id=e.dst_id WHERE d.spelling='Shape' AND ek.name IN ('field_of','method_of') GROUP BY ek.name;` |
| **F4** cross-TU edge | `project/` (index BOTH `mathlib.c` and `app.c`) | an edge `main(app.c) → multiply` resolves to `mathlib.c`'s definition (same USR `c:@F@multiply`); `dst` symbol `resolved=1`; src and dst land in different files. | `SELECT s.usr,d.usr,d.resolved FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE d.usr='c:@F@multiply' AND e.kind=(SELECT id FROM edge_kind WHERE name='calls');` |
| **F5** template_arg ref_id joins back | synthetic `Box<Widget>` buffer (Appendix P5) | the `Widget` type-arg row's `ref_id` joins to the `Widget` symbol. | `SELECT s.spelling FROM template_arg ta JOIN symbol s ON s.id=ta.ref_id WHERE ta.arg_kind=1;` returns `Widget` |

### Parity (label `parity`)
- Extend `parity_check.sh` command script to include an `index` run on the
  `project/` fixture (already does) — the `.dump` now includes `edge`,
  `edge_site`, `template_*` rows; Python and C++ output MUST be byte-identical
  after the existing `compile_options` normalization. Run:
  `cd build && ctest -L parity --output-on-failure`.
- Add a `cidx resolve` invocation to the parity command script and diff its
  transcript + post-resolve dump.

### Full-suite gate
```bash
cd /Users/husam/workspace/qemu-vms/libclang-lab/cidx-cpp
cmake --build build -j4
cd build && ctest --output-on-failure     # default + clang + parity must be green
```

---

## 7. Risk review

- **R1 — transaction boundaries.** Symbols + edges for one file MUST commit in
  one txn (design §2 ACID). `Transaction` is non-nestable (S02);
  `index_symbols`/`index_file` already open their own txn. Adding `index_edges`
  inside the same logical unit requires EITHER moving the txn up into
  `cmd_index`/`index_one` (and passing a no-open-txn flag down) OR adding an
  `index_file_with_edges` that wraps both. **Do not** open a second txn for
  edges — it would create a symbols-committed/edges-rolled-back split on error.
- **R2 — FK / cascade integrity.** `edge.src_id/dst_id REFERENCES symbol(id) ON
  DELETE CASCADE`; `edge_site.edge_id ... ON DELETE CASCADE`. `PRAGMA
  foreign_keys = ON` is set at open. The existing `delete_*` methods delete
  symbols explicitly (because file refs are SET NULL); deleting a symbol now
  also cascades its edges + edge_sites — verify the `cidx delete` tests still
  pass and add a case that a deleted symbol removes its edges. `template_arg.
  ref_id ON DELETE SET NULL` (not cascade) — a deleted referenced type leaves
  the arg row with NULL ref_id, correct.
- **R3 — stub kind/NOT NULL.** `symbol.kind` has a NOT NULL + CHECK constraint;
  a minted stub must pass a valid kind and non-null spelling. `''` spelling is
  allowed (TEXT NOT NULL accepts empty). Choose `kind='function'` for call
  callees; revisit if a non-function reference ever needs a stub (none in this
  iteration). The real def's `add_symbol` upsert overwrites kind/spelling and
  `MAX(resolved)` keeps resolution monotonic.
- **R4 — edge_site WITHOUT ROWID PK with nullable file_id.** PK is
  `(edge_id,file_id,line,col)`; `file_id` is nullable. For call sites
  `file_id`/`line`/`col` are always populated (Appendix P1), so the PK is
  well-defined; a NULL file_id would still be a distinct PK tuple under SQLite's
  NULL-distinct semantics. Acceptable. Use `INSERT OR IGNORE` so a re-seen site
  is a no-op.
- **R5 — USR-resolution edge cases.** (a) builtin/system callees (`printf`,
  `__swbuf`) carry real USRs (`c:@F@printf`) and will be stub-minted unless the
  TU also defines them — harmless, `resolved=0`. (b) Static functions get
  file-scoped USRs (`c:calls.c@F@mid`) — local but stable within the TU; intra-
  file edges resolve fine (Appendix P1). (c) `recurse→recurse` self-edge
  (`src_id==dst_id`) is valid. (d) Cross-TU resolution is automatic via USR
  equality + `MAX(resolved)` (Appendix P2) — `resolve` is mostly reporting;
  confirm scope with architect.
- **R6 — `is_virtual_base` Python binding gap.** The C API
  `clang_isVirtualBase` exists (header verified) and the C++ port can call it,
  but the Python `cindex.Cursor` may not expose it (Appendix P3 fell back). If
  the installed binding lacks it, both tools must agree: either bind it manually
  in Python (`conf.lib.clang_isVirtualBase`) or store NULL in both. Geometry.hpp
  has no virtual bases, so F2 asserts `is_virtual=0` regardless — but a parity
  hazard remains for virtual-inheritance code. Resolve before merge.
- **R7 — Python↔C++ parity hazards.** (a) **JSON/text formatting**: edges carry
  no JSON, but `template_arg.literal` (e.g. `'3'`) and `args_sig` must format
  identically — use the same stringification. (b) **Column order**: keep
  `_SCHEMA`/`kSchema` table column order byte-identical; the parity `.dump`
  compares `CREATE TABLE` text. (c) **Body-walk determinism**: edge insertion
  order must match between tools or the `.dump` row order differs — the dump is
  sorted by rowid for `edge`/`edge_site` is `WITHOUT ROWID` (sorted by PK).
  Ensure both tools iterate cursors in the same (libclang visitation) order, OR
  rely on PK/`ORDER BY` in the dump normalization. Add an `ORDER BY` to the
  parity dump for `edge` if rowid order proves non-deterministic. (d) The
  golden per-file summary line + new `resolve` output must change identically in
  both tools.
- **R8 — migration idempotency.** Covered by `CREATE TABLE IF NOT EXISTS` +
  `INSERT OR IGNORE` + presence-gated migrate block; T1 asserts a double-open is
  a no-op and meta stays `'7'`.

---

## Appendix — Probe ground truth (pasted output, with counts)

Probe script: `libclang-lab/scripts/probe_graph_edges.py` (run from repo root:
`python3 libclang-lab/scripts/probe_graph_edges.py`). libclang 18.1.1 wheel.

### P1 + P6 — bodies not walked; recursive descent; conditional
```
CALL_EXPRs reached by the cidx symbol walk (body skipped): 0  (expect 0)
total CALL_EXPRs found by recursive body descent: 7

caller -> callee  (spelling)  @line:col  conditional
       mid -> leaf_a     @7:12  cond=0
       mid -> leaf_b     @7:24  cond=0
   recurse -> recurse    @12:16  cond=0
   compute -> mid        @16:13  cond=0
   compute -> recurse    @17:10  cond=0
      main -> printf     @22:5  cond=0
      main -> compute    @22:20  cond=0

USR pairs (caller_usr -> callee_usr):
  c:calls.c@F@mid      ->  c:calls.c@F@leaf_a
  c:calls.c@F@mid      ->  c:calls.c@F@leaf_b
  c:calls.c@F@recurse  ->  c:calls.c@F@recurse
  c:@F@compute         ->  c:calls.c@F@mid
  c:@F@compute         ->  c:calls.c@F@recurse
  c:@F@main            ->  c:@F@printf
  c:@F@main            ->  c:@F@compute

conditional-detection positive check (synthetic buffer):
    f -> g        @5  cond=1     (inside `if`)
    f -> h        @6  cond=1     (inside `for`)
    f -> g        @7  cond=0     (unconditional return)
```
Confirms: (1) the current symbol walk reaches **0** CALL_EXPRs (bodies pruned by
`CXChildVisit_Continue`); (2) a recursive body descent finds **7** call edges in
`calls.c` with the exact caller→callee USR pairs above; (3) `conditional=1` is
correctly detected for calls under `if`/`for` ancestors.

### P2 — cross-TU USR identity
```
mathlib.c definition USRs:        add: c:@F@add   multiply: c:@F@multiply   square: c:@F@square
app.c call-site referenced USRs:  multiply: c:@F@multiply  == mathlib.c def
                                  square:   c:@F@square    == mathlib.c def
  cross-TU USR equality for 'multiply': YES
  cross-TU USR equality for 'square':   YES
```
Confirms cross-TU edges fall out of USR equality — a call in `app.c` carries the
SAME USR as the definition in `mathlib.c`. No special machinery needed.

### P3 — inheritance (base USR, access, virtual)
```
  Circle --inherits--> Shape
      derived_usr: c:@N@geo@S@Circle
      base_usr   : c:@N@geo@S@Shape
      access     : PUBLIC   virtual: <C-API only>
```
`access` = PUBLIC via `clang_getCXXAccessSpecifier`. `is_virtual` is **C-API
only** (`clang_isVirtualBase`) — the Python `Cursor` lacks the wrapper (Risk
R6). geometry.hpp has no virtual base, so `is_virtual=0`.

### P4 — field_of / method_of for `geo::Shape`
```
class Shape  usr=c:@N@geo@S@Shape
  method_of: Shape      access=PUBLIC     kind=CONSTRUCTOR
  method_of: ~Shape     access=PUBLIC     kind=DESTRUCTOR
  method_of: area       access=PUBLIC     kind=CXX_METHOD
  method_of: name       access=PUBLIC     kind=CXX_METHOD
  field_of : name_      access=PROTECTED  member_usr=c:@N@geo@S@Shape@FI@name_
  -> 1 field_of, 4 method_of edges for Shape
```
Confirms owning-record USR + per-member access (access stays on `symbol.access`).

### P5 — templates (params + args; ref_id joins back)
```
primary CLASS_TEMPLATE params:
  template Box  usr=c:@N@geo@ST>1#T@Box
    param[0] kind=type  name=T
  template Arr  usr=c:@ST>2#T#NI@Arr
    param[0] kind=type  name=T
    param[1] kind=non-type  name=N  type=int

instantiation template_args (ref_id joins back to a symbol):
  bi: type=geo::Box<int>     specialization_usr=c:@N@geo@S@Box>#I            num_args=1
    arg[0] kind=TYPE  type='int'     ref_usr=''            (builtin: no symbol node)
  bw: type=geo::Box<Widget>  specialization_usr=c:@N@geo@S@Box>#$@S@Widget   num_args=1
    arg[0] kind=TYPE  type='Widget'  ref_usr='c:@S@Widget' (JOINS BACK to Widget symbol)
  ad: type=Arr<double, 3>    specialization_usr=c:@S@Arr>#d#VI3              num_args=2
    arg[0] kind=TYPE      type='double'  ref_usr=''
    arg[1] kind=INTEGRAL  literal=3      (non-type value stored verbatim)
```
Confirms: `clang_Cursor_getNumTemplateArguments` /
`...getTemplateArgumentKind` / `...getTemplateArgumentType` /
`...getTemplateArgumentValue` all work; a **type-arg whose type is a known
symbol** (`Box<Widget>` → `c:@S@Widget`) gives a `ref_id` that joins back to the
graph (T4/F5); builtin args (`int`,`double`) have no symbol node (ref_id NULL);
non-type args (`3`) store as `literal`. Primary-template params enumerate via the
`TEMPLATE_TYPE_PARAMETER` / `TEMPLATE_NON_TYPE_PARAMETER` children.

# S02 — Storage engine + repo/files utils

## Goal
Byte-compatible SQLite schema-v6 storage layer with all upsert/query/migration semantics,
proven by porting `_storage_smoke.py` assertion-for-assertion as the executable spec; plus the
small `repo`/`files` utils that sit directly on Storage.

## Scope — files to create
```
cidx-cpp/src/storage/sqlite.hpp   .cpp    # SqliteDb / SqliteStmt / Transaction RAII (D3)
cidx-cpp/src/storage/records.hpp          # Component/Directory/File/Symbol/Stats structs (design §5.1)
cidx-cpp/src/storage/storage.hpp  .cpp    # Storage class: schema, migrate(), all SQL (design §5.3)
cidx-cpp/src/util/repo.hpp  .cpp          # git_root walk-up, repo_name via tiny INI scanner (D10)
cidx-cpp/src/util/files.hpp .cpp          # resolve_file_arg, index_status (md5-only) (design §5.9)
cidx-cpp/tests/storage_smoke_test.cpp     # PORT of project/indexer/_storage_smoke.py
cidx-cpp/tests/storage_migration_test.cpp
cidx-cpp/tests/fuzzy_match_test.cpp
cidx-cpp/tests/repo_test.cpp
cidx-cpp/tests/fixtures/                  # committed Python-generated v2/v3/v4/v5 fixture DBs
```
Modify: `cidx-cpp/tests/CMakeLists.txt` (register the four exes), root `CMakeLists.txt`
(find_package SQLite3, link `SQLite::SQLite3`).

## Spec references
- 01-analysis: §3 entire (schema, kinds, migrations, write semantics §3.4, read semantics
  §3.5), §4 (md5 incrementality, indexed-flag reset), G13–G20, G30; `repo.py` behavior in
  §1.2 add-source row; `files.py` in §1.2 index row + §4.
- 02-design: §4 (exact DDL text — copy it), §4.1 (column-presence migration order, G19),
  §4.2 (upsert SQL ported character-for-character; **RETURNING ≥ 3.35 probe**), §5.1–5.3
  (records + RAII + full Storage API), D3, D10, D25 (pragma parity: foreign_keys ON only,
  no WAL), D16/D17 (do NOT add fingerprint invalidation or stale-symbol purge).
- Executable spec: `libclang-lab/project/indexer/_storage_smoke.py` (READ-ONLY).

## Pre-implementation task (design §4.2, S2 directive)
Probe the test box's SQLite before writing the upserts:
`ssh husam@192.168.1.115 'sqlite3 --version'` (box per memory note `gcc-index-test-box`).
- ≥ 3.35 → use `RETURNING` + startup assert `sqlite3_libversion_number() >= 3035000`.
- < 3.35 (EL8 ships 3.26) → implement the `INSERT ... ON CONFLICT DO UPDATE` + `SELECT id`
  fallback as the ONLY path. **Do not ship both.** Record the probe result and the chosen
  path in the story log/PR description.

## Acceptance criteria
- Fresh `Storage(":memory:")` and file-backed DBs produce schema v6: table/column names,
  CHECKs (17 kinds incl. `macro`), FKs + ON DELETE actions, 5 indexes, meta row
  `('schema_version','6')` — asserted via `PRAGMA table_info` + `sqlite_master`.
- DB directory `mkdir -p`'d on open (skip for `:memory:`); connect sequence = open →
  `PRAGMA foreign_keys = ON` → `migrate()` → schema script → commit, migration BEFORE
  schema (G19).
- All §3.4 write semantics hold (the smoke test asserts them): component upsert on path,
  directory upsert with `.`/`""`→`""`, file upsert with COALESCE + NULL-safe `IS NOT` md5
  reset (G13), symbol upsert with spelling/kind overwrite + COALESCE non-clobber +
  definition-wins location + MAX-sticky flags (G14), `mark_file_indexed` with
  `datetime('now')`.
- All §3.5 read semantics hold: `component_for_path` longest-prefix app-side (G16),
  `_fuzzy_like` char-in-order with `\ % _` escaping, `search_symbols` `::`-segment match
  ordered `LENGTH(qual_name), qual_name`, `list_symbols` def-OR-decl location scope,
  `_dir_scope_sql` root `''` → `%` (G17, G18), `is_file_indexed` vs md5-only
  `index_status` distinction, `stats()` ported (D18: API yes, CLI no).
- Autocommit-unless-in-txn: every public mutator commits unless inside
  `Storage::transaction()`; RAII Transaction commits on success, rolls back on exception.
- `repo`: `git_root` walks up to the dir containing `.git`; `repo_name` reads
  `[remote "origin"]` → `url` basename with `.git` suffix stripped (D10).
- `files`: `index_status` returns exactly `{kNotIndexed, kNoStoredMd5, kMd5Mismatch, kOk}`
  per files.py:20-28; `resolve_file_arg` resolves relative args against `--source` component
  root else CWD.
- **Unit tests written and passing** — behaviors the tests MUST cover:
  - `storage_smoke_test`: every assertion of `_storage_smoke.py`, same order, including the
    reopen-persistence check and the `update_symbol` unknown-column / bad-kind throws.
  - `storage_migration_test`: open committed fixture DBs at v2/v3/v4/v5 layouts; assert
    column adds, qual_name recursive-CTE backfill (longest parent_usr chain wins),
    decl_* backfill for `is_definition=0` rows, meta bumped to `'6'`; fixtures are
    Python-written → doubles as a cross-tool-open proof. Also: newer-DB opens without
    refusal (no downgrade path).
  - `fuzzy_match_test`: both fuzzy algorithms incl. LIKE-escape of `%`/`_`/`\`, ASCII
    case-insensitivity, length-first ordering; `_dir_scope_sql` root case; longest-prefix
    component resolution with nested components.
  - `repo_test`: synthetic `.git/config` fixture (created in tmp dir by the test); `.git`
    suffix strip; walk-up from a nested dir; no-git-root case.
  - Bad symbol kind rejected by BOTH the SQL CHECK and the application-side throw (§3.2).

## Test plan
- `cmake --build build && ctest --test-dir build --output-on-failure -R "storage|fuzzy|repo"`
  — all default label (hermetic; system libsqlite3 only).
- Fixture generation: one-off Python script run by YOU (not by CI) writes
  `tests/fixtures/v{2,3,4,5}.db`; commit the DBs and the generator script alongside them in
  `tests/fixtures/`. Generator uses only stdlib sqlite3, mirrors the historical layouts
  described in analysis §3.3.
- `libclang-lab/project/indexer/_storage_smoke.py` and `storage.py` are READ-ONLY references
  — copy SQL text out of them, never edit them.

## Dependencies
blockedBy: S01. Parallel-safe with S03.

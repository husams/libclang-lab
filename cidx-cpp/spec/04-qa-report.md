# cidx-cpp — QA Report

Stage: QA (stage 6). Inputs: `spec/01-technical-analysis.md`, `spec/02-design.md`,
`spec/stories/S01–S08`, `spec/03-review.md` (R1–R13 + 7 QA probes), full source tree,
Python reference `libclang-lab/project/` (read-only), test box 192.168.1.115.
Method: independent clean-room build + ctest, code-level verification of all 13 review
fixes, execution of all 7 reviewer probes, a ~50-invocation black-box CLI session driving
the built binary side-by-side with the live Python tool over tmp-dir fixtures, and a
fresh re-run of the librdkafka e2e gate on the test box (the box's previous build
predated the review fixes — `libcidx_util.a` was still on disk — so the old green
result was not trusted).

All scratch DBs/caches lived under `/tmp/qa-cidx/`; no repo file other than this report
was written.

## Verdict: **PASS**

One minor defect found (Q1 — test-suite portability on the e2e box; no story acceptance
criterion violated, product unaffected). All eight stories' acceptance criteria are met
by executed tests; all seven probes pass on substance; all thirteen review fixes are
verified landed.

---

## 1. Clean-room build + test run

```
rm -rf build && cmake -S . -B build && cmake --build build -j8
ctest --test-dir build --output-on-failure
```

Result: build clean (AppleClang, arm64, `CMAKE_BUILD_TYPE` defaulted to Release per R13);
**17/17 tests passed, 0 failed** — 12 `default`, 4 `clang`, 1 `parity` (parity_check
2.60 s, zero diffs).

SKIP-77 honesty check (the clang-labelled tests genuinely RAN, not skipped):
`ctest -L clang -V` shows each suite resolving
`CIDX_LIBCLANG=/opt/homebrew/lib/python3.14/site-packages/clang/native/libclang.dylib`
and reporting real doctest work:

| suite | cases run | assertions |
|---|---|---|
| compiledb_clang_test | 4 | 17 |
| toolchain_clang_test | 1 | 4 |
| ast_clang_test | 13 | **240** |
| cli_clang_test | 7 | 98 |

CTest reports them as "Passed" (not "Skipped"); `SKIP_RETURN_CODE 77` is wired on all
four registrations in `tests/CMakeLists.txt`.

## 2. E2E gate re-run (192.168.1.115, current code)

`bash scripts/e2e_librdkafka.sh` — rsyncs the current tree, rebuilds remotely with the
system g++, then imports + indexes librdkafka under /opt/gcc8 × libclang 21.1.1:

```
imported 93 file(s), skipped 0
index-exit: 0
index: 93 indexed, 0 failed, 0 already indexed
e2e_librdkafka: PASS — 93/93 TUs indexed
```

**93/93 with the post-review code**, exit 0, no warning line, no cidx.log records.
Box ctest (`ctest -E parity`): all 12 default suites pass on Linux/g++ (portability
proof incl. storage/sqlite/pathutil); `toolchain_clang_test` passes against the real
libclang 21 (auto-detected from /opt/llvm-21.1.1 — D1 path exercised). Three clang
suites fail there for fixture reasons only — see defect Q1.

## 3. Story acceptance matrix

| Story | Acceptance criteria | Covered by (executed) | Status |
|---|---|---|---|
| S01 scaffold + util | green build, stub→full CLI, md5/json/pathutil/env/logger/subprocess contracts | `pathutil_test` (6 cases vs posixpath tables), `json_min_test` (7 incl. \uXXXX + reject cases), `hashing_test` (4 incl. G30 nullopt), `env_logger_test` (12: both falsy sets exact, delay=True, record format, warning counter file-sink-only, stderr fallback, subprocess capture/timeout/exit) | **PASS** |
| S02 storage | schema v6, G13/G14 upserts, migration before schema (G19), G16–G18 reads, txn semantics, repo/files utils | `storage_smoke_test` (assertion-for-assertion port + schema-v6 + txn + R2 commit-failure cases), `storage_migration_test` (v2/v3/v4/v5 Python-written fixtures + CTE backfill + no-downgrade), `fuzzy_match_test` (9 cases incl. LIKE-escape injection), `repo_test` (4) | **PASS** |
| S03 shim + compiledb | D1 resolution order, X-macro dlsym nullptr-checked, major() P12, strip/sanitize/driver frozen rules | `compiledb_test` (20 hermetic cases incl. G10/G11/G12, R3 tilde, R4 no-leak, P12 regex, bogus-CIDX_LIBCLANG throw) + 4 `clang` cases (manifests CDB loads). Black-box: bogus path → clear error exit 1; lazy-load parity with Python confirmed | **PASS** |
| S04 toolchain | search-dir parse, builtin-dir substitution G3/G7, gnuc decision table G4, FloatN asymmetry, is_cpp G9, memoization D8/D26 | `toolchain_test` (26 hermetic cases via fake-driver fixture covering the full decision table incl. cap×libclang-major×env-override matrix, memo counting, host-default ordering) + `clang` case (cap follows real major()) — also passed against real libclang 21 on the box | **PASS** |
| S05 parser + diagnostics | final argv G5, options=0 D19, abort level G6, 25-line cap, G27 one-warning-per-file, G28 log-only flag dump | `ast_test` cases 1–7 (argv assembly, clean parse, ClangParseError, tolerated-error single warning + verbatim summary, strict abort first-3-joined, fatal flag-dump-in-log-only, 30-error/25-line-cap). Black-box: strict + fatal paths byte-identical to Python incl. error text | **PASS** |
| S06 AST indexer | 17-kind map, pruning G21, qual names G25, decl/def split G15, header indexing G20/G23/G26 counters | `ast_test` cases 8–14 (16 reachable kinds, project decl/def flow, resolved-skip + decl-patch, pruning, geometry qual/access/pure, anonymous USRs, system-header toggle, spelling-vs-abspath). Black-box header-counter parity on live runs | **PASS** |
| S07 CLI write+query | argparse-compatible grammar/exit-2, D6 no-abbreviation, output formats G31/D14, G27 no log on query, exit-1-zero-match G29 | `cli_test` (42 cases: full grammar table, R5 saturation, format/locale, add-source, import incl. Python-parity failure messages, all query goldens, no-cidx.log case). Black-box: **33/33 CLI invocations byte-matched Python incl. exit codes** (usage errors, search/show/list variants, limits 0/negative/huge, help texts) | **PASS** |
| S08 index + parity/e2e | §6.1 pipeline, md5 skip, continue-on-failure, byte-matched output lines, parity gate, 93/93 e2e | `cli_clang_test` index cases (two-TU flow, re-index on content change, fatal-include continue + exit 1), `parity_check` green in clean-room run (incl. R6 multi-element normalization), e2e re-run **93/93** on current code (§2) | **PASS** |

No vacuous coverage found: every clang-labelled suite executed real parses on this box
(assertion counts above), and the parity gate ran inside the clean-room ctest.

## 4. Reviewer probe results (spec/03-review.md §"What QA should specifically probe")

| # | Probe | Method | Result |
|---|---|---|---|
| 1 | Commit-failure visibility (R2) | SHARED lock held by a background sqlite3 connection on the cache DB during `import` and `index` (COMMIT needs EXCLUSIVE; no busy_timeout configured) | **PASS** — both print `error: exec failed: database is locked`, exit 1, **no** `imported N file(s)` success line; file rows kept their stale md5 / were never falsely marked indexed. Unit pin: `storage_smoke_test` "commit() propagates failure — not silently swallowed (R2)" |
| 2 | Crash-path exit codes (R1) | corrupt index.db (4 KiB urandom); corrupt `compile_options` JSON in DB; `INDEXER_CACHE=/System/...`; `HOME=/nonexistent` without INDEXER_CACHE | **PASS** — all four produce one `error: <msg>` stderr line + exit 1; no SIGABRT anywhere. Catch-chain unit pin: `cli_test` "main: CidxError propagation shape (R9 proxy) + std::exception IS-A chain (R1)" |
| 3 | Stale-header standalone parse (R7) | append to `mathlib.h` only, no-arg `index` on both tools | **PASS** — byte-identical: both re-index the header standalone (empty args, NULL options), identical per-file lines, counters, exit 0, identical resulting md5 row. Design §6.1 text carries the "(corrected per review R7)" annotation |
| 4 | Multi-element options interop (R6) | synthetic CDB with 5-element args incl. `-DGRÉ=héllo wörld`; import with each tool, index/query with the other | **PASS both directions** — C++ decodes Python's `["…", "…"]` + `\uXXXX` form; Python decodes C++'s compact UTF-8 form; both index 1/1 and render `show file` identically |
| 5 | `CIDX_LIBCLANG=~/…` (R3) + env edges | fake `$HOME` with symlinked dylib, `'~/libclang.dylib'`, both tools; `CIDX_STRICT` ∈ {`1`, ``, `" 0 "`, `" yes "`}; `CIDX_GNUC_VERSION` ∈ {``, `" off "`}; `INDEXER_IGNORE_SYSTEM_HEADERS` ∈ {`" off "`, ``, `garbage`} | **PASS** — tilde expanded identically; all 9 env-edge runs byte-matched Python on separated stdout/stderr + exit codes (padded falsy tolerated, padded truthy aborts with identical `1 fatal diagnostic(s)` message, `" off "` flips 39 system → 39 unowned, empty/garbage = default) |
| 6 | Skip accounting on the e2e box | fresh e2e re-run (old box build predated review fixes) + `ctest -E parity` on the box | **PASS with finding Q1** — 93/93 on current code; accounting is honest: nothing SKIP-77'd to inflate green (toolchain_clang genuinely ran against libclang 21; the three manifests-dependent clang suites FAIL loudly rather than skip — see Q1) |
| 7 | Logger fallback | `chmod 000 cidx.log`, index a tolerated-error TU | **PASS** — WARNING record emitted to stderr in full file format, **no** `N warning(s) … logged to` stdout line (counter counts file-sink records only), exit 0, DB writes unaffected |

## 5. Review-fix verification (R1–R13)

| ID | Fix verified at | Evidence |
|---|---|---|
| R1 | `src/main.cpp:74-78` | `catch (const std::exception&)` → `error:` + exit 1, plus `catch (...)` → 1; cli_test case; probe 2 black-box |
| R2 | `storage.cpp:326-348` (dtor ROLLBACK-only; `commit()` throws, `done_` not set on throw so dtor never retries COMMIT), `ast.cpp:371`, `commands.cpp:270` explicit `txn.commit()` | unit pin + probe 1 black-box |
| R3 | `libclang.cpp:186` `load_library(pathutil::expanduser(*env))` | compiledb_test R3 case; probe 5 tilde run both tools |
| R4 | `libclang.cpp:142-170` — dlsym into temp struct, `dlclose(h)` + throw on any missing symbol, members assigned only on full success | compiledb_test R4 case ("function pointers stay nullptr after a failed load") |
| R5 | `args.cpp:578-583` clamp to [INT_MIN, INT_MAX] | cli_test R5 case; black-box `--limit 99999999999999999999999999999999` matched Python ("show all") |
| R6 | `scripts/parity_check.sh` — compile_options column round-tripped through `json.dumps(separators=…)` in the dump copy + synthetic MULTI_ARGS_DB imported in the transcript | parity_check passed in clean-room run; probe 4 |
| R7 | design §6.1 line now reads "all file rows (db.list_files(), header rows included; (corrected per review R7)" | probe 3 confirmed implementation = Python |
| R8 | `storage.cpp:882-895` — sorted, deduped, Python list-repr `['col']` | code read |
| R9 | `main.cpp:30-45` makedirs checks errno (EEXIST tolerated) → CidxError | probe 2: `/System` → `Operation not permitted` exit 1; read-only `/` → `Read-only file system` exit 1 |
| R10 | `format.cpp:51-60` left as floor — review sanctioned "Leave" | all mtime goldens matched Python in the black-box session |
| R11 | `ast.cpp:177-182` `strcmp` on the raw C string before any std::string construction | code read |
| R12 | `toolchain.cpp:441-452` G7 warning hoisted into one `warn_no_resource` lambda used by both sites | code read; toolchain_test "warning re-fires per call" case |
| R13 | root `CMakeLists.txt:9-12` default Release; library renamed `cidx_core` (line 25) | clean-room cmake log: "cidx: CMAKE_BUILD_TYPE defaulted to Release" |

## 6. Defects

| ID | Sev | Description | Repro | Status |
|---|---|---|---|---|
| Q1 | minor (test infra, non-blocking) | The three manifests-dependent clang-labelled suites (`compiledb_clang_test`, `ast_clang_test`, `cli_clang_test`) **fail instead of skipping** on a host that has a loadable libclang but no sibling `libclang-lab/manifests/` checkout (the e2e box: `e2e_librdkafka.sh` rsyncs only `cidx-cpp/`). SKIP-77 covers only the missing-libclang case. | on 192.168.1.115: `cd ~/cidx-cpp-e2e/build && ctest -L clang` → 3/4 fail with `could not load compilation database from '/home/husam/cidx-cpp-e2e/manifests…'`; exit 8 from ctest | open — advisory. No story criterion violated (clang label is a dev-box gate per stories/README; the box gate is the e2e script, which passes). Recommend: SKIP-77 when `CIDX_MANIFESTS_DIR` is absent, or rsync `manifests/` in the e2e script |

No product defects found.

## 7. Footprint sanity

- Binary: `build/cidx` 505,800 bytes (~494 KiB), Mach-O arm64, Release. Build tree 17 MB.
- Cache dir after import+index: exactly `index.db` (+ `cidx.log` only once a record is
  written; query-only invocations create no log — verified). Matches Python's layout.
- Schema: `meta/component/directory/file/symbol`, `schema_version=6` — cross-readable by
  the Python tool (probe 4) and vice versa.
- Repo tree vs design §3 canonical structure: only out-of-design artifact is
  `.cache/clangd/**` (clangd IDE index, ~70 files) — advisory; it is not covered by
  `cidx-cpp/.gitignore` (which lists only `build*/`).
- No stray files written into the repo by any test run (`git status` clean before/after;
  all QA scratch confined to `/tmp/qa-cidx/` and the box's mktemp cache).

## 8. Observations (advisory — never blocking)

- Inherited Python semantics, parity-correct, worth a user-docs line: a TU's stored md5
  is refreshed only at `import` time, so editing a source file after import makes it
  re-index on every subsequent `index` run until re-imported (headers DO get their md5
  refreshed via `index_headers`). Both tools behave identically (verified live).
- The entire `cidx-cpp/` tree is untracked in the enclosing git repo — release/devops
  should commit it before the devops stage.
- Lazy libclang load means a bogus `CIDX_LIBCLANG` goes unnoticed when nothing is
  pending (exit 0) — identical to Python; not a defect.
- `.cache/clangd/` should be added to `.gitignore` alongside `build*/`.

## 9. Residual risks / not-run items

- `clang-tidy` was not run (not installed on this box; review §triage already
  dispositioned the IDE findings).
- Parity byte-strictness for multi-element `compile_options` is enforced via the R6
  normalization + synthetic fixture; real-world richer CDBs are covered indirectly by
  the librdkafka 93/93 gate, whose DB content is not dump-diffed against Python.
- True disk-full COMMIT failure was simulated (SQLITE_BUSY via lock, plus the unit
  test's forced-ROLLBACK SQLITE_ERROR), not reproduced with an actually full filesystem.
- `std::bad_alloc` was not physically induced (macOS lacks an effective `ulimit -v`);
  the R1 catch chain is pinned by unit test and code inspection.

## 10. Counts

- Local clean-room ctest: **17/17 pass** (incl. parity gate).
- Box: e2e gate **93/93 indexed, 0 failed** (current code); box ctest 13/16 pass
  (3 failures = Q1 fixtures, all default suites + toolchain_clang green on Linux/g++).
- Black-box session: 33/33 CLI parity invocations matched + 17 probe/scenario runs, all
  consistent with the Python reference.
- Probes: 7/7 executed, 7 pass (probe 6 with minor finding Q1).
- Review fixes: 13/13 verified landed.
- Defects: 1 (Q1, minor, test infra, advisory).

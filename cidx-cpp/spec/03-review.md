# cidx-cpp — Senior-Developer Code Review

Stage: review (senior-developer). Inputs: `spec/01-technical-analysis.md`,
`spec/02-design.md`, `spec/stories/S01–S08`, full source under `cidx-cpp/`,
Python reference `libclang-lab/project/indexer/` (read-only).
Method: line-by-line comparison of every `src/` file against the cited
Python source (storage.py, cli.py, clang/util.py, clang/ast.py, compiledb.py,
utils/*), plus an independent build + test run and empirical golden checks
against the live Python tool (Python 3.14.3).

Independent verification performed on this box:

- `cmake --build build` clean; `ctest -E parity` → **16/16 passed**, the four
  `clang`-labelled suites really executed (0.57 s ast_clang = real parses, not
  SKIP-77).
- Ran `python3 -m indexer` misuse cases: the C++ pinned usage/error strings
  (incl. the unquoted `(choose from repo, external)` form) match Python 3.14.3
  byte-for-byte.

## Verdict: **request-changes**

Two major findings (R1, R2). Both are small, localized fixes; everything else
is minor/nit. Spec fidelity is otherwise excellent — the schema DDL, the three
upserts, the migration CTE, the drop sets, the diagnostic policy, the gnuc
masquerade decision table, and all reproduce-don't-fix gotchas (D16–D19
including the dead `macro` kind, G20 stale rows, G23 spelling-vs-abspath
split) are faithful to the Python source, not just to the spec text.

---

## Findings table

| ID | Sev | Location | Story | Rubric ref | Description | Recommended fix |
|----|-----|----------|-------|-----------|-------------|-----------------|
| R1 | **major** | `src/main.cpp:42-65` | S08 | lint `bugprone-exception-escape`; D23 | `main()` catches only `UsageError`/`CidxError`. Any other exception (`std::bad_alloc`, `std::regex_error`, iostream/system errors from helpers that don't use `error_code`) escapes → `std::terminate` → SIGABRT, no stderr message, exit ≠ 1. Python parity: an unhandled exception prints a traceback and exits **1**. D23 says main is "the ONLY catch-site mapping to exit codes" — today it doesn't map them all. | Append `catch (const std::exception &e) { std::cerr << "error: " << e.what() << "\n"; return 1; }` (optionally `catch (...)` → exit 1). Keep the existing two handlers first. |
| R2 | **major** | `src/storage/storage.cpp:326-340` (`Transaction::~Transaction`), call sites `src/clangx/ast.cpp:354` (`index_file`), `src/cli/commands.cpp:258-270` (`cmd_import`) | S02 (fix), S06/S07 (call sites) | D3 / analysis §3.4 (txn contract); cpp-conventions "never swallow exceptions silently" | The Transaction destructor `catch (...)`-swallows a **failed COMMIT**. Both write pipelines rely on destructor commit: on commit failure (disk full, I/O error, BUSY) `cmd_import` still prints `imported N file(s)` and exits 0 with nothing persisted; worse, in `index_file` the symbols roll back silently but the subsequent autocommitted `mark_file_indexed` succeeds → file permanently marked indexed with no symbols (md5 skip hides it forever). Python's `_Transaction.__exit__` lets the commit error propagate. | Call `txn.commit()` explicitly at the end of the success path in `AstIndexer::index_file` and `cmd_import` (it throws on failure); keep the destructor as unwind-only ROLLBACK. Also reconsider `done_` handling so a throwing `commit()` doesn't retry COMMIT in the dtor. |
| R3 | minor | `src/clangx/libclang.cpp:167-171`; `src/clangx/toolchain.cpp:369-371` | S03 | analysis §1.3 / §5.1 env contract (AC: "Env-var contract … verbatim") | Python applies `os.path.expanduser` to `CIDX_LIBCLANG` both at `set_library_file` (util.py:48-49) and in `_resource_include` step 2 (util.py:87-90). The C++ `load()` dlopens the raw env value and `resource_include()` derives from the raw `library_path()`. `CIDX_LIBCLANG='~/llvm/lib/libclang.so'` works under Python, fails under cidx-cpp. (`CIDX_RESOURCE_DIR` *is* expanded — asymmetric.) | `pathutil::expanduser` the env value before dlopen (and therefore before `library_path_` is recorded). |
| R4 | minor | `src/clangx/libclang.cpp:139-159` | S03 | D23/§2 RAII rule ("RAII wrappers for every C handle … dlopen handle") | On a missing-symbol failure `load_library` throws without `dlclose(h)` — the handle leaks and the already-assigned member function pointers keep pointing into the leaked library. Today benign (next attempt overwrites them, total failure exits), but the "retry-safe" comment overstates the state: a half-poisoned shim object survives. | `dlclose(h)` before throwing on the dlsym-failure path and reset the resolved pointers (or resolve into locals and assign only on full success). |
| R5 | minor | `src/cli/args.cpp:361-390, 554-562` | S07 | AC S07 (argparse semantics, exit-2 policy); UB risk | `parse_py_int` accumulates into `long` with `val = val*10 + digit` — signed overflow is UB for absurd `--limit` values (e.g. 30 digits). Python ints are unbounded and a huge limit means "show all"; C++ additionally truncates via `static_cast<int>`, so a >2^31 limit can flip negative and silently engage the negative-slice path. | Clamp during accumulation (e.g. stop at a ceiling and saturate to INT_MAX) — preserves "huge limit ⇒ show all" and removes the UB. |
| R6 | minor | `scripts/parity_check.sh` (header note), D5 | S08 | AC S08 (parity gate strength) | The DB-dump diff is byte-strict only because the fixture's `compile_options` arrays are single-element: Python encodes `["a", "b"]` (`", "` separator), cidx-cpp `["a","b"]`. A richer fixture would fail the dump diff for a sanctioned (D5) reason, masking real regressions behind a known-noise diff. Cross-tool read-compat is tested, byte-level dump parity for multi-element arrays is not exercised anywhere. | Normalize the `compile_options` column in the throwaway dump copy (e.g. `json.dumps(json.loads(x), separators=…)` via the python side, or strip whitespace inside the column) so the gate stays strict on richer fixtures; add one multi-arg compile command to the parity transcript. |
| R7 | minor | `spec/02-design.md` §6.1 vs `src/cli/commands.cpp:158-185` | S08 (doc) | AC S08 / design §6.1 | Design §6.1 says no-arg `index` targets "all pending files (indexed=0, **compile_options NOT NULL**)". The Python tool (cli.py `_index_pending` → `db.files()`) iterates **every** file row, header rows included; a stale header row (md5 changed, NULL options) gets a standalone parse with empty args. The C++ correctly follows **Python** (`db.list_files()` no-filter), so the implementation is right and the design text is wrong. Left as-is it will mislead the next maintainer. | Fix the design §6.1 sentence; add a QA probe for the stale-header standalone-parse path (below). |
| R8 | nit | `src/storage/storage.cpp:877-893` | S02 | smoke-parity claim ("assertion-for-assertion") | `update_symbol` error text differs from Python: Python raises `unknown symbol column(s): ['bogus']` (sorted **deduped set**, list repr); C++ emits `unknown symbol column(s): bogus` (sorted, duplicates kept, comma-joined). Never user-visible through the CLI; smoke test only asserts the throw. | Optional: match the Python rendering, or document the delta next to D6's abbreviation delta. |
| R9 | nit | `src/main.cpp:24-36` | S08 | analysis §1.4 (`_setup_logging` `os.makedirs`) | `makedirs` ignores every `mkdir` failure. Python's `os.makedirs(exist_ok=True)` raises on a non-creatable cache dir (read-only FS) → exit 1; C++ continues and fails later with a less direct `cannot open database` (and the logger silently degrades to stderr-only). | Check `mkdir` errno (ignore `EEXIST`), throw `CidxError` otherwise. |
| R10 | nit | `src/cli/format.cpp:51-60` | S07 | G31 (mtime local-time format) | `format_mtime` floors the epoch; `datetime.fromtimestamp` rounds to the nearest microsecond, so an mtime within 0.5 µs of a second boundary can render 1 s apart. Practically unobservable; parity script normalizes mtime anyway. | Leave, or round to nearest µs before flooring. |
| R11 | nit | `src/clangx/ast.cpp:165-187` (`walk_visitor`) | S06 | design §7 small-footprint ("no gratuitous copies on hot paths") | Per visited cursor: one `CxString` + one heap `std::string` just to compare the file name, plus `cursor_location` work, for every cursor including pruned ones. Python pays the same (and more), so parity-perf is fine, but the C string from `clang_getCString` could be compared in place (`strcmp`) without constructing a `std::string`. | Optional micro-opt: compare `clang_getCString` result directly against `ctx->filename->c_str()` before constructing anything. |
| R12 | nit | `src/clangx/toolchain.cpp:443-448 / 464-467` | S04 | maintainability (review skill: duplication) | The G7 "no clang builtin headers found …" warning string is duplicated (memo-hit re-emit vs first computation). If one copy drifts, the byte-frozen log/warning-counter contract breaks asymmetrically. | Hoist into a local helper/lambda or a `static` string builder used by both sites. |
| R13 | nit | `CMakeLists.txt:16-48` | S01 | CMake hygiene (cpp-conventions) | `cidx_util` contains the entire application (storage, clangx, cli), not just util — name is misleading; and no default `CMAKE_BUILD_TYPE` means a bare `cmake -B build` produces a no-optimization, no-debug build. | Rename to `cidx_core` (or split), and set a default build type when none given. |

Counts: **0 blocker, 2 major, 5 minor, 6 nit.**

---

## clang-tidy IDE-finding triage (no clang-tidy on this box; judged from source)

| Finding | Verdict | Reasoning |
|---|---|---|
| `bugprone-exception-escape` in `main.cpp` | **REAL — fix** (= R1) | An uncaught non-`CidxError` aborts with no message and a non-1 exit code; violates D23's "only catch-site mapping to exit codes" and Python parity (traceback + exit 1). |
| `readability-implicit-bool-conversion` in `args.cpp` / `commands.cpp` | style noise — leave | The flagged sites are `std::optional` truthiness checks mirroring Python's `if not name:` semantics; rewriting to `.has_value()` everywhere adds churn with zero behavior change and dilutes the line-to-line Python correspondence the comments rely on. |
| DeMorgan simplifications (`!(args.component && !args.component->empty())`) | style noise — leave | Deliberate transliteration of Python truthiness (`not args.component`); the "simplified" form reads further from the reference. Behavior identical. |
| Nested conditional operators (`cmd_show_symbol` resolved field, `format.cpp` mark) | style noise — leave | Direct ports of Python conditional expressions (`"yes" if … else "n/a (pure virtual)" if … else …`); an if/else chain would be longer and no clearer next to the parity comments. |
| `performance-enum-size` (`LogLevel : int = 20/30/40`) | false positive | The values ARE the Python `logging` level numbers (documented in logger.hpp); shrinking the underlying type saves nothing meaningful and loses the self-documenting constants. |
| `modernize-*` in tests | noise — leave | Tests are doctest-idiomatic; modernize nits there have no production impact and the test tree is already consistent. |

## Security review summary

- All SQL is parameterized; the only string-built SQL fragments are column
  names in `update_symbol` (validated against the frozen `_SYMBOL_COLS`
  allowlist before interpolation — good) and static SELECT lists.
- LIKE patterns escape `\ % _` with `ESCAPE '\'` in both fuzzy paths and
  `dir_scope_sql` (G17/G18) — no LIKE injection.
- `util/subprocess.cpp` uses `posix_spawnp` with a verbatim argv, never a
  shell; stdin is `/dev/null`. Note (inherited from Python, by design): the
  driver probe **executes `argv[0]` from compile_commands.json** — a malicious
  compile DB runs an arbitrary binary at `cidx index` time. Same trust model
  as the Python tool and as any compile-DB consumer; acceptable, worth one
  line in user docs.
- dlopen target is user/env-controlled (`CIDX_LIBCLANG`) — inherent to D1,
  same as the Python launcher.
- No temp-file races in production code (tests use `mkdtemp` correctly); no
  secrets touched; log file lives in the user cache dir with default umask.

## Spec-fidelity spot checks that PASSED (verified against Python source, not the spec)

- Schema v6 DDL and the meta insert: identical text; migration runs before the
  schema script (G19); column-presence detection; qual_name backfill CTE is
  character-identical; meta bump only on change.
- File upsert keeps the NULL-safe `IS NOT` md5 reset (G13); symbol upsert
  COALESCE/CASE/MAX semantics character-faithful (G14); `mark_file_indexed`
  exact; RETURNING floor asserted at open (SQLite ≥ 3.35, design §4.2 — single
  shipped path, per S02 probe).
- `component_for_path` reproduces Python's quirky tie-break (`len(root)` vs
  unstripped `best["path"]`) exactly (G16).
- Both fuzzy algorithms and `LENGTH()`-first ordering (G18); `''`-root
  dir-scope `%` (G17); `list_symbols` def-OR-decl scoping with the same EXISTS
  shape.
- Drop sets, basename source match, glued/spaced `-I/-isystem/-iquote`
  absolutization, `sanitize()` at index time (G10–G12); driver bare-name rule.
- Diagnostic policy: Fatal-default/`CIDX_STRICT` Error (exact falsy set incl.
  empty), summary of first 3 **at abort level**, per-diag INFO lines over all
  ≥ Error diagnostics, 25-line cap + suppressed line, flag dump + `libclang: ?`
  to the log only, one WARNING per file (G5–G8, G27, G28). `str(None)` → "None"
  for locationless diagnostics is reproduced.
- gnuc masquerade: env falsy set, empty-env fallthrough to derivation, 10.9 cap
  gated on env-absence ∧ major ≥ 11 ∧ `__attr_dealloc` ∧ libclang < 21, FloatN
  C ≥ 7 / C++ ≥ 13 ∧ floatn-common probe (G4); builtin-dir first-occurrence
  substitution and G7 verbatim fallback — including the easy-to-miss
  "warning re-fires on memo hits" detail (Python's `driver_flags` is uncached).
- AST: 17-kind map with unreachable `macro` (D19), function-body and subtree
  pruning (G21), USR-keyed identity, semantic-parent qual names skipping
  anonymous levels (G25), decl-site patch on resolved rows (G15), header
  indexing via include *spelling* with abspath dedupe (G23), per-TU
  system-header test (G26), `tu.spelling` main-file match (G24).
- CLI surface: all usage/help/error strings match the live Python 3.14.3 tool
  (empirically checked, including the unquoted choices format and
  `--component/-c` argument naming); limits 25/50 with 0=all and even
  negative-slice semantics; `ls` alias; `--dir`-requires-`--component`;
  mutually-exclusive `--indexed/--pending`; exit codes 0/1/2 (G29); the
  no-warning-line-on-unknown-`--source` subtlety; D6 no-abbreviation delta
  documented.
- Memory/footprint: one TU alive at a time, TU+Index freed before
  `mark_file_indexed`; cursors streamed, never collected; cache dir contains
  exactly `index.db` + `cidx.log`; `PRAGMA foreign_keys` only (D25); no extra
  artifacts.
- Portability: C++17-clean (no designated initializers, no `<charconv>`
  floats, no post-17 library use found); `stdc++fs` linked conditionally for
  GNU < 9; no mac-only assumptions outside `#ifdef __APPLE__` host defaults;
  vendored clang-c/md5 carry licenses (doctest's MIT text is embedded in the
  header itself).
- Tests: storage smoke test is a genuine assertion-for-assertion port (incl.
  reopen-persistence, unknown-column/bad-kind throws, mtime/md5 staleness
  matrix); SKIP-77 is wired so a skipped clang suite reports as CTest
  "Skipped", not "Passed" — and on this box the clang suites actually ran.

## What QA should specifically probe (S08/QA stage)

1. **R2 regression test**: make COMMIT fail (read-only DB file after open, or
   a full tmpfs) during `cidx import` and `cidx index`; assert non-zero exit
   and no `imported N file(s)` success line; assert a file is never marked
   indexed when its symbol transaction did not commit.
2. **Crash-path exit codes (R1)**: force a non-CidxError (e.g. corrupt
   `compile_options` JSON is CidxError — instead try an unreadable `HOME` /
   induced `bad_alloc` via ulimit) and assert exit 1 + `error:` line, not
   SIGABRT.
3. **Stale-header standalone parse (R7)**: index a TU, modify the header's
   content, run no-arg `cidx index`, and diff the C++ vs Python behavior of
   the header row being re-indexed standalone with empty args.
4. **Multi-element compile_options interop (R6)**: import with cidx-cpp, query
   with Python cidx (and vice versa) over a DB whose options arrays have ≥ 2
   elements and `\uXXXX`-escaped characters; both directions must decode.
5. **`CIDX_LIBCLANG=~/...` (R3)** and empty-string env values for
   `CIDX_GNUC_VERSION` / `CIDX_STRICT` / `INDEXER_IGNORE_SYSTEM_HEADERS`
   (falsy-set edge: whitespace-padded values).
6. **Skip accounting on the e2e box**: re-run the 93/93 librdkafka gate and
   capture `ctest` output verifying the four `clang`-labelled tests show
   "Passed" (not "Skipped") and the parity test ran — a SKIP-77 on a
   misconfigured box would have inflated the green count.
7. **Logger fallback**: point `INDEXER_CACHE` at an unwritable dir — confirm
   warnings still reach stderr, the warning-count line is suppressed (counter
   counts file-sink records only), and exit codes are unchanged.

## REVIEW_BLOCK signals

`[R1, R2]` — route to: R1 → S08 owner (main.cpp), R2 → S02 owner
(Transaction) with call-site touch-ups in S06 (`AstIndexer::index_file`) and
S07 (`cmd_import`).

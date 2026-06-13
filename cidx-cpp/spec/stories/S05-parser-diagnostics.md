# S05 — Parser + diagnostic policy

## Goal
Port `parse()` and the tolerant-by-default diagnostic policy: final argv assembly,
fatal-only abort (CIDX_STRICT escalation), and the exact log formats that keep the CLI
warning counter one-per-file.

## Scope — files to create
```
cidx-cpp/src/clangx/parse.hpp .cpp        # ParsedTu RAII + Parser (design §5.7)
cidx-cpp/tests/ast_test.cpp               # CREATE with the parse/diagnostic-policy suite
                                          # (S06 extends this same file with walk/extraction cases)
```
Modify: `cidx-cpp/tests/CMakeLists.txt` (register `ast_test`, label `clang`).

## Spec references
- 01-analysis: §5.2 (final argv = stored_args + toolchain_flags + `-ferror-limit=0`;
  options=0; fresh Index per parse; TranslationUnitLoadError → ClangParseError), §5.3
  (abort level, fatal flag-dump-to-log, 25-per-file INFO cap + suppressed line, tolerated-
  error WARNING summary), §1.4 (log formats), §2.2 (one TU at a time, freed immediately),
  G5, G6, G27, G28; env `CIDX_STRICT` (§1.3).
- 02-design: §5.7 (Parser/ParsedTu shapes; exact log/exception strings), D19 (`options = 0`
  — `macro` kind stays unreachable; do NOT enable DETAILED_PREPROCESSING_RECORD), D23
  (ClangParseError; noexcept C callbacks), D15 (single-threaded).

## Acceptance criteria
- `Parser::parse` builds final argv exactly `stored_args + toolchain_flags(cpp, driver) +
  {"-ferror-limit=0"}` (G5); `options = 0` (D19); fresh `CXIndex` per parse; `ParsedTu`
  RAII disposes TU then Index; `spelling` = the path exactly as passed (G24 prerequisite).
- Parse failure (null TU / error CXErrorCode) → `ClangParseError("cannot parse <file>")`.
- Diagnostic policy:
  - abort level = Fatal by default; `CIDX_STRICT=1` → Error (G6).
  - On abort: full flag dump + libclang major logged at ERROR to `cidx.clang` (log only,
    never stdout/stderr terminal output — G28); up to 25 per-diag INFO lines
    `<TU>: diag <file>:<line>: <msg>` then `... N more diagnostic(s) suppressed`;
    throw `ClangParseError("<file>: N fatal diagnostic(s): <first 3, ';'-joined>")`.
  - On tolerated errors (non-strict): exactly ONE WARNING
    `<file>: N error diagnostic(s) ignored (CIDX_STRICT=1 to abort)` + the same capped INFO
    lines — so the file-sink warning counter increments by exactly 1 per file (G27).
- **Unit tests written and passing** (`ast_test`, label `clang`) — behaviors the tests MUST
  cover, parsing in-tmp-dir generated sources (do NOT modify `manifests/`):
  - clean parse of a valid C file → TU produced, no throw, warning counter unchanged.
  - source with semantic errors, default mode → parse succeeds, ONE warning-counter
    increment, INFO diag lines present in the log file with the exact format, summary text
    matches verbatim.
  - same source with `CIDX_STRICT=1` → `ClangParseError`, message = first-3-';'-joined
    format.
  - fatal diagnostic (e.g. `#include "no-such-header.h"`) → throw; flag dump found in the
    log file, NOT in captured stdout/stderr; ≤ 25 INFO lines + suppressed line when > 25
    diags (generate a source with > 25 errors).
  - `-ferror-limit=0` present in the final argv (expose the assembled argv for testing or
    assert > 20 distinct error diags are reported from a 30-error source).
  - more than-25-diags source → exactly 25 INFO lines + the suppressed line.

## Test plan
- `ctest --test-dir build --output-on-failure -L clang -R ast` — requires a loadable
  libclang (dev box); skipped cleanly where unavailable.
- Fixtures: tests synthesize their own `.c` sources in tmp dirs; the Logger from S01 is
  pointed at a tmp `cidx.log` and its file content is asserted.
- Toolchain flags come from S04's `Toolchain`; on the dev box host defaults apply (macOS
  xcrun path) — tests must not hardcode SDK paths.

## Dependencies
blockedBy: S03, S04. Parallel-safe with S07.

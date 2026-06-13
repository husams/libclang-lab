# S08 — CLI index + parity/e2e gate

## Goal
Wire the full `cidx index` pipeline end-to-end and prove behavioral parity: golden-output
comparison vs the Python tool on the lab fixtures, plus the librdkafka 93/93 e2e gate.

## Scope — files to create / modify
```
cidx-cpp/src/cli/commands.cpp             # MODIFY: replace cmd_index stub with the §6.1 flow
cidx-cpp/src/cli/args.cpp                 # MODIFY only if index-specific arg wiring needs it
cidx-cpp/scripts/parity_check.sh          # NEW: Python cidx vs cidx-cpp on manifests/, diff outputs + DB dumps
cidx-cpp/scripts/e2e_librdkafka.sh        # NEW: 93/93-TU validation driver for 192.168.1.115
cidx-cpp/tests/cli_test.cpp               # EXTEND: index-command cases (label clang)
```
Modify: `cidx-cpp/tests/CMakeLists.txt` if registration changes.

## Spec references
- 02-design: §6.1 verbatim (the full flow: cache resolve, lazy log, LibClang load, Storage
  open, target list = pending files (`indexed=0, compile_options NOT NULL`) or resolved
  args, per-file md5 skip → sanitize → is_cpp → parse → index_symbols → index_headers →
  mark_file_indexed → per-file print → warning-count line → exit codes), §8 rows
  `parity_check.sh` (diff all CLI command outputs + `sqlite3 .dump` excluding
  `indexed_at`/`mtime`; label `parity`) and `e2e_librdkafka.sh` (label `e2e`, manual gate),
  D15 (no `--jobs` — sequential).
- 01-analysis: §1.2 index row (per-file line
  `-> N symbols; headers: A indexed (+B symbols), C already, D system, E unowned`; final
  `N warning(s)/error(s) logged to <path>`; exit 1 if any file failed/unknown), §1.4/G27
  (warning counter), §2.2 (import does no parsing / index does no compile-DB reading —
  consumes stored options re-`sanitize()`d, G11), §4 (md5-only skip via `index_status`),
  §9.1 items 1–11 (the parity contract this story closes), G24 (absolute path passed to
  parse), G29.
- Memory notes (referenced by analysis §9.3): `cidx-toolchain-support`,
  `gcc-index-test-box` (192.168.1.115, /opt/llvm-21.1.1 + /opt/gcc8, librdkafka builds).

## Acceptance criteria
- `cidx index` with no args indexes every pending file; with FILE args resolves each
  (relative → `--source` component root else CWD), errors on unknown files and sets the
  fail flag but continues; already-indexed (md5-current) files skipped; ClangParseError on
  one file → error printed, fail flag, continue with the rest; exit 1 iff any file
  failed/unknown, else 0.
- Per-file and final output lines byte-match the Python tool's format (§1.2); warning-count
  line appears only when the counter > 0.
- Stored options are re-`sanitize()`d at index time (G11); parse receives the reconstructed
  absolute path (G24); TU freed before the next file (one-AST peak memory).
- **Unit tests written and passing** (`cli_test` extensions, label `clang`):
  - end-to-end on a tmp copy of a two-TU project (synthesize in tmp dir — manifests are
    READ-ONLY; mirror `manifests/project/` shape): import → index → per-file lines match,
    second `index` run prints nothing to do / skips via md5, touching a file content
    re-indexes it, header counters appear correctly.
  - failure path: a TU with a fatal include error → exit 1, other TUs still indexed,
    warning/error count line present, flag dump only in `cidx.log`.
  - unknown FILE arg → exit 1.
- **Parity gate (mandatory, label `parity`, dev box)**: `scripts/parity_check.sh` green —
  runs Python cidx (`uv run`) and `cidx-cpp` with separate `INDEXER_CACHE` dirs over
  `libclang-lab/manifests/project/compile_commands.json` (READ-ONLY), executes the same
  command script against both (import, index, search, show symbol, show file, all list
  variants, second-run skip), and diffs: all CLI stdout/stderr + exit codes, and
  `sqlite3 .dump` of both DBs excluding `indexed_at`/`mtime` values. Zero diffs = pass.
- **E2E gate (release gate, label `e2e`)**: `scripts/e2e_librdkafka.sh` runs on
  192.168.1.115 (ssh husam) against librdkafka × /opt/gcc8 cross toolchain ×
  libclang 21.1.1 and asserts **93/93 TUs indexed** (analysis §9.1.11). The script must be
  written and the run executed + its result recorded in the story log before the story is
  done; if the box is unreachable, the story is blocked, not done.

## Test plan
- `ctest --test-dir build --output-on-failure -L clang -R cli` for the unit layer.
- `bash cidx-cpp/scripts/parity_check.sh` (requires Python cidx + uv on the dev box; the
  script must self-check prerequisites and fail loudly, not skip silently).
- `bash cidx-cpp/scripts/e2e_librdkafka.sh` (drives the remote box; see memory note
  `gcc-index-test-box` for paths/credentials conventions; never embed secrets).
- Fixtures: tmp-dir project copies; `manifests/` and `project/` READ-ONLY.

## Dependencies
blockedBy: S06, S07. Final story — nothing depends on it.

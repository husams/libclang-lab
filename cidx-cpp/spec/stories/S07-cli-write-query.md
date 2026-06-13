# S07 — CLI: args grammar, write + query commands

## Goal
The full CLI surface minus `index`: hand-rolled argv grammar with argparse-compatible exit
codes, `add-source` + `import` write commands, and all query commands
(`search`, `show symbol|file`, `list/ls components|dirs|files|symbols`) with exact output
formats.

## Scope — files to create / modify
```
cidx-cpp/src/cli/args.hpp     .cpp        # grammar, ParsedArgs, usage text, exit-2 policy (D6)
cidx-cpp/src/cli/commands.hpp .cpp        # cmd_add_source/import/search/show/list + Context;
                                          #   cmd_index = stub erroring "not implemented" (S08 replaces)
cidx-cpp/src/cli/format.hpp   .cpp        # tables, mtime/indexed_at formatting (G31, D14)
cidx-cpp/src/main.cpp                     # MODIFY: argv → Cli::run, top-level catch → exit code (D23)
cidx-cpp/tests/cli_test.cpp               # NEW test exe (sanctioned §3 refinement — see stories/README.md)
```
Modify: `cidx-cpp/tests/CMakeLists.txt` (register `cli_test`).

## Spec references
- 01-analysis: §1.2 entire (every command row: flags, defaults 25/50, output line formats,
  exit codes, `--dir` requires `--component`, `--indexed`⊕`--pending`, `ls` alias, the two
  pattern algorithms split between `search` and `list`), §1.3 (`INDEXER_CACHE` cache-dir
  policy — DB always `<cache>/index.db`, log `<cache>/cidx.log`, no `--index` flag), §1.4
  (log file attach), G17, G18, G29 (exit codes incl. usage=2), G30 (import: missing-file
  mtime nullopt, unreadable md5 nullopt, row still imported), G31 (mtime local-time,
  indexed_at verbatim + " UTC").
- 02-design: D6 (NO argparse prefix-abbreviation — documented delta; golden tests never use
  abbreviations), D14 (mtime via localtime_r/strftime copied exactly from cli.py:421-424;
  golden-locked), §5.9 (Context shape, cache-dir resolution), §6.2 (`import` flow: component
  root = git root of first source else its dirname; one transaction; outside-component
  sources → stderr line + skip counter; `imported N file(s), skipped M`; exit 1 on load
  failure or empty DB), §6.3 (query commands = Storage reads + format; limits applied by
  CLI slicing, 0 = all; exit-1-on-zero-matches; def/decl second row in search output),
  D23 (`main()` is the only catch-site mapping exceptions to exit codes).
- Python reference for output text (READ-ONLY): `project/indexer/cli.py` —
  `cmd_search` (248-261), `cmd_show_symbol` (348-390, linkage gloss 371-374),
  `cmd_show_file` (393-449), list commands, `cmd_add_source`.

## Acceptance criteria
- Grammar: unknown flag / missing required arg → usage text + **exit 2**; `--dir` without
  `--component` → exit 1 (analysis §1.2 list-files row); `--indexed` + `--pending`
  together → usage error exit 2; `ls` aliases `list`; no flag abbreviation (D6);
  defaults: search `--limit 25`, list symbols `--limit 50`, `--kind repo`.
- `add-source`: repo kind walks to git root, name from `.git/config` remote-origin URL
  basename `.git`-stripped (S02 repo util); external kind path-as-is, name = basename;
  prints `component #N: name (kind) at path`; exit 1 if `--path` not a directory.
- `import`: per design §6.2 verbatim, incl. G30 nullopt handling and G13 indexed-reset via
  the S02 upsert; `--db` accepts json path or directory.
- Query commands reproduce cli.py output line-for-line: search table (id, qual name, kind,
  `def `/`decl`/`pure` mark, `path:line`, second `decl` row for definitions with a stored
  decl site); show symbol key/value dump with None-valued fields omitted and the linkage
  human gloss; show file dump incl.
  `(none -- header indexed via an including TU)` placeholder (G20) and G31 time formats;
  the three list variants with trailing count lines; exit 1 on zero matches (G29).
- Cache dir: `$INDEXER_CACHE` else `~/.cache/cidx`, created on demand; log file attached
  lazily; read-only subcommands never create an empty `cidx.log` (G27/D7).
- `cmd_index` stub exits non-zero with a clear "not implemented (S08)" message.
- **Unit tests written and passing** (`cli_test`, default label — hermetic, drives the
  command functions / args parser in-process with tmp `INDEXER_CACHE` and tmp DBs) —
  behaviors the tests MUST cover:
  - args grammar table: every exit-2 case, the exit-1 `--dir` rule, mutual exclusion,
    `ls` alias, defaults, no-abbreviation (`--lim` rejected).
  - format: mtime localtime rendering (fixed TZ via `setenv("TZ", ...)`), indexed_at
    `" UTC"` suffix, def/decl/pure markers, None-field omission.
  - add-source repo + external against synthetic tmp git trees; non-directory → exit 1.
  - import against READ-ONLY `manifests/project/compile_commands.json` (label `clang` for
    the CXCompilationDatabase load): file rows with stripped options + driver, skip counter
    for an unowned source, `imported N file(s), skipped M` line, exit codes.
  - search/show/list outputs against a DB seeded via Storage API with known symbols:
    pattern algorithms hit the right rows (`::`-segment vs char-in-order), limits + `0 = all`
    slicing, zero-match exit 1.
  - no `cidx.log` created by query-only invocations.

## Test plan
- `ctest --test-dir build --output-on-failure -R cli` (default label; `clang`-labelled
  import case).
- Fixtures: tmp dirs with synthetic `.git/config`; seeded tmp DBs; manifests compile DB
  READ-ONLY. Output assertions compare full captured stdout strings against expected text
  copied from observed Python `cidx` output (cite the command used in a comment).

## Dependencies
blockedBy: S02, S03. Parallel-safe with S04, S05, S06.

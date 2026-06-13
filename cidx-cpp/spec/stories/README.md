# cidx-cpp — Story Index

Stage 5 (scrum-master) deliverable. Refines `spec/02-design.md` §9 (12 rows) into 8
developer-ready stories. All paths are within the canonical directory structure of design §3;
developers may NOT create anything outside it. `libclang-lab/manifests/` and
`libclang-lab/project/` are READ-ONLY fixtures.

## Consolidation vs design §9

| Design rows | Story | Why merged |
|---|---|---|
| S1 | **S01** | as-is (bootstrap) |
| S2 + S5 | **S02** | repo/files utils are ~110 lines, depend only on Storage, and share its test fixtures |
| S3 + S4 | **S03** | CompileDb is a thin client of the dlopen shim; same vendored-header concern |
| S6 | **S04** | as-is (largest single risk area — gnuc masquerade) |
| S7 | **S05** | as-is (diagnostic policy is its own contract) |
| S8 | **S06** | as-is |
| S9 + S11 | **S07** | args grammar, write commands, and query commands share `cli/args` + `cli/format`; splitting forces two agents into the same files |
| S10 + S12 | **S08** | `cmd_index` is the only consumer of the full pipeline; parity/e2e are its acceptance test |

One sanctioned refinement of design §3: `tests/` gains `cli_test.cpp` (S07/S08). The design's
test table has no CLI unit exe (it leaned on parity scripts), but the pipeline rule — every
story ships passing unit tests — requires one. It stays inside the canonical `tests/` directory
and is registered in `tests/CMakeLists.txt` like every other exe.

## Story table

| ID | Title | blockedBy | Parallel with |
|---|---|---|---|
| [S01](S01-scaffold-util-core.md) | Scaffold + util core | — | — |
| [S02](S02-storage.md) | Storage engine + repo/files utils | S01 | S03 |
| [S03](S03-libclang-shim-compiledb.md) | LibClang dlopen shim + CompileDb | S01 | S02 |
| [S04](S04-toolchain.md) | Toolchain resolution + gnuc masquerade | S01, S03 | S07 |
| [S05](S05-parser-diagnostics.md) | Parser + diagnostic policy | S03, S04 | S07 |
| [S06](S06-ast-indexer.md) | AST indexer (symbols + headers) | S02, S05 | S07 |
| [S07](S07-cli-write-query.md) | CLI: args grammar, write + query commands | S02, S03 | S04, S05, S06 |
| [S08](S08-cli-index-parity-e2e.md) | CLI index + parity/e2e gate | S06, S07 | — |

## Execution waves

| Wave | Stories | Notes |
|---|---|---|
| 1 | S01 | bootstrap; everything blocks on the green build |
| 2 | S02 ∥ S03 | independent subtrees (`src/storage`+`src/util` vs `src/clangx/libclang`+`src/compiledb`) |
| 3 | S04 ∥ S07 | S04 needs only S01+S03; S07 needs only S02+S03. Disjoint files |
| 4 | S05 (∥ S07 if still running) | sequential on S04 |
| 5 | S06 | sequential on S05 |
| 6 | S08 | integration + parity gate; last |

Critical path: S01 → S03 → S04 → S05 → S06 → S08 (6 stories deep). S02 and S07 hang off the
side and never block the clangx chain.

## Pipeline rules (apply to every story)

- A story is **never** marked done without its unit tests run and green:
  `cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release && cmake --build build` then the story's
  `ctest` line (design §8 exit-criteria convention).
- CTest labels: default = hermetic; `clang` = needs a loadable libclang; `parity` = needs
  Python cidx + uv (dev box); `e2e` = test box 192.168.1.115.
- File creation only inside `cidx-cpp/` per design §3; build dir `cidx-cpp/build*/` (git-ignored).
- Every G* gotcha and D* ADR cited in a story is a hard requirement, not guidance.

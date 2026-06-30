# FlatBuffer artifact spike (cidx two-stage, Phase-2 follow-up)

Measurement spike (2026-06-25/26) for the per-TU FlatBuffer fact artifact. Built
to measure **disk size / compactness** of the artifact vs the full Clang AST dump
and vs the SQLite per-TU contribution. Not wired into the build — these are the
schema definitions + the harness used to produce the numbers.

Full writeup: `~/workspace/wiki/pages/reviews/cidx-parallel-extraction-experiment-2026-06-25.md`
(§ "FlatBuffer artifact — disk & compactness").

## Files

| file | what |
|---|---|
| `cidx_minimal.fbs` | **THE minimal schema** — only facts that cannot be reconstructed at Stage-2 merge: `usr`/`name`/`parent_usr` (string-pool refs), `kind`, `loc`, packed `flags` (is_def/pure/static/instantiation/named + linkage + access); edges = `src,dst,kind`. `Symbol` = fixed 28 B struct. |
| `cidx_artifact.fbs` | the **lossless** schema (every SQLite column). Bloated — kept only to show why the first measurement was wrong (qual_name/display_name/type_info/decl_path are reconstructable and shouldn't be stored per-TU). |
| `cidx_minimal_generated.h` | `flatc --cpp` output for the minimal schema. Compiler-asserted: `FLATBUFFERS_STRUCT_END(Symbol, 28)`, `Edge, 12`, `Loc, 12`. |
| `fb_minimal.py` | harness: `cidx index <TU>` → read `index.db` facts → emit minimal schema-JSON → `flatc --binary` → real `.bin`; reports size vs SQLite + string-pool breakdown. |
| `fb_compare.py` | fuller harness: also dumps the Clang AST (`-ast-dump` text + json) for the size comparison. |

## Measured result (minimal artifact, LLVM TUs)

| TU | SQLite/TU | minimal `.fb` | gzip | vs SQLite | vs AST.txt | vs AST.json |
|---|---:|---:|---:|---:|---:|---:|
| WithColor.cpp | 2.7 MB | 468 KB | 102 KB | 5.9× | ~145× | ~1100× |
| DebugInfoMetadata.cpp | 16.8 MB | 2.6 MB | 539 KB | 6.5× | ~94× | ~650× |
| ASTWriterStmt.cpp | 40.6 MB | 7.1 MB | 1.4 MB | 5.9× | ~79× | ~510× |

~88–91% of the artifact's string pool is **USR strings** (the cross-TU key) — the
floor. Hashing USRs to 8 B would shave ~another 1.7× (loses string-queryability).

## Reproduce

```sh
flatc=/opt/homebrew/bin/flatc   # brew install flatbuffers
# build a real binary from a TU's facts (after `cidx index <TU>`):
python3 fb_minimal.py <post-import-base-dir> <workdir> /path/to/TU.cpp
# decode any .bin back to JSON (proves it's a valid FlatBuffer):
$flatc --json --raw-binary cidx_minimal.fbs -- <workdir>/TU.cpp.min.bin
# regenerate C++ structs:
$flatc --cpp cidx_minimal.fbs
```

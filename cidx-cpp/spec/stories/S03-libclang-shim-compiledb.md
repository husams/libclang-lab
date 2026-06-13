# S03 — LibClang dlopen shim + CompileDb

## Goal
Runtime-loadable libclang (18–21) through a single dlopen/dlsym shim with launcher-parity
auto-detection, plus compile_commands.json loading and the frozen arg strip/sanitize/driver
logic.

## Scope — files to create
```
cidx-cpp/src/clangx/libclang.hpp  .cpp    # LibClang singleton, auto_detect, major(), CxString (D1, D12)
cidx-cpp/src/compiledb/compiledb.hpp .cpp # CXCompilationDatabase load + strip/sanitize/driver (D20)
cidx-cpp/third_party/clang-c/             # vendored LLVM 18.1 public headers + LICENSE (D24)
cidx-cpp/tests/compiledb_test.cpp
```
Modify: `cidx-cpp/tests/CMakeLists.txt` (register exe; `clang`-labelled cases split out),
root `CMakeLists.txt` (`${CMAKE_DL_LIBS}`, third_party include dir).

## Spec references
- 01-analysis: §1.1 (launcher auto-detect: candidate dirs, newest-wins scoring via
  `<libdir>/clang/` numeric entries, NO llvm-config priority, library name patterns),
  §5.1 (CIDX_LIBCLANG contract; must support libclang 18 AND 21), §6 entire
  (load, strip_for_libclang, sanitize, driver), G10, G11, G12; P12 regex + 0 fallback.
- 02-design: D1 (resolution order: `CIDX_LIBCLANG` → auto_detect → plain dlopen of default
  names; launcher retired), D12 (`major()`: `clang_getClangVersion` → regex
  `version (\d+)` → 0 on no match), D20 (CXCompilationDatabase, NOT own JSON parsing),
  D24 (vendor headers, types only, functions via dlsym), §5.4 (the exact ~36-function
  dlsym list — implement via X-macro, nullptr-checked at load), §5.5 (CompileDb API +
  frozen constants `kDrop`/`kDropWithArg`/`kDropPrefix`), D23 (CxString RAII;
  `clang_disposeString`).

## Acceptance criteria
- `LibClang::instance().load()` resolution order exactly: `CIDX_LIBCLANG` env →
  `auto_detect()` (candidates: `llvm-config --libdir` via S01 subprocess,
  `/opt/llvm*/lib{,64}`, `/usr/lib/llvm-*/lib`, `/usr/local/llvm*/lib`; score = highest
  numeric dir under `<libdir>/clang/`; all candidates compete on version; names
  `libclang.so`, `libclang.so.*`, `libclang.dylib`) → `dlopen("libclang.so"/".dylib")`;
  throws `CidxError` with a clear message when nothing loads. Handle never `dlclose`'d.
- All shim functions dlsym'd via X-macro and nullptr-checked at load (one missing symbol →
  load failure naming the symbol).
- `major()` returns the parsed major, cached; returns **0** when the version string doesn't
  match (P12) — unit-testable by feeding the regex helper raw strings.
- `library_path()` reports the loaded path (needed by Toolchain for CIDX_RESOURCE_DIR
  derivation — S04).
- `CompileDb::load`: `--db` arg accepts the json path (trailing `compile_commands.json`
  stripped) or its directory, abspath'd; throws on load failure; returns commands with
  directory/filename/driver/stripped-args.
- `strip_for_libclang` reproduces §6 exactly: drop argv[0]; bare drops
  `{-c, --, -M, -MM, -MD, -MMD, -MG, -MP, -MV, -Werror, -pedantic-errors}`; pair drops
  `{-o, -MF, -MT, -MQ, -dependency-file, --serialize-diagnostics}`; prefix drops
  `{-Werror=, -Wp,-M, -MF…, -MT…, -MQ… glued}`; source dropped by full path OR basename
  (G10); `-I/-isystem/-iquote` absolutized against `cmd.directory` in spaced AND glued
  forms, normpathed (G12); everything else untouched.
- `sanitize` re-applies ONLY the drop rules (no path fixing) to stored options (G11).
- `driver`: argv[0]; absolutized against directory iff it contains a path separator, else
  kept bare for PATH resolution at parse time.
- **Unit tests written and passing** — behaviors the tests MUST cover:
  - `compiledb_test` (default label, hermetic): table-driven strip/sanitize/driver cases
    feeding arg vectors directly — must include `--`, `-Wp,-MD,foo.d`, glued `-MFdeps.d`,
    `-o out.o` pair, `-Werror=format`, source matched by basename, glued `-Ifoo` and spaced
    `-I foo` absolutization, `-isystem`/`-iquote` both forms, bare `cc` driver kept bare,
    `./gcc` driver absolutized.
  - `compiledb_test` (`clang` label): `CompileDb::load` over
    `libclang-lab/manifests/compile_commands.json` and
    `libclang-lab/manifests/project/compile_commands.json` (READ-ONLY) — command count,
    directories, stripped args match expectations.
  - `major()` regex helper: `"clang version 18.1.8"` → 18, garbage → 0.
  - Load-failure path: bogus `CIDX_LIBCLANG` → `CidxError` (default label; does not require
    a real libclang).

## Test plan
- Hermetic cases: `ctest --test-dir build --output-on-failure -R compiledb` (default label).
- libclang-dependent cases: `ctest --test-dir build -L clang` — skipped automatically (CTest
  DISABLED or runtime SKIP) when no libclang is loadable; document which.
- Fixtures: the two manifests compile DBs, READ-ONLY. No new fixture files needed.

## Dependencies
blockedBy: S01. Parallel-safe with S02.

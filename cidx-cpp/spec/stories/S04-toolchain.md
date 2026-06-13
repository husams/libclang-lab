# S04 — Toolchain resolution + gnuc masquerade

## Goal
Port the highest-risk subsystem: driver introspection, resource-dir search, builtin-dir
substitution, the `-fgnuc-version` masquerade with both glibc probes and the libclang<21 cap,
and host-default include order.

## Scope — files to create
```
cidx-cpp/src/clangx/toolchain.hpp .cpp    # class Toolchain (design §5.6)
cidx-cpp/tests/toolchain_test.cpp
cidx-cpp/tests/fixtures/fake-driver*      # executable shell-script fixture(s) mocking gcc:
                                          #   -E -x <lang> - -v stderr search list,
                                          #   -dumpfullversion / -dumpversion outputs
```
Modify: `cidx-cpp/tests/CMakeLists.txt` (register exe).

## Spec references
- 01-analysis: §5.4 entire (driver probe protocol, search-list parsing, builtin-dir
  substitution at first-occurrence position, include-fixed rationale, no-resource-headers
  fallback, host defaults incl. macOS xcrun, `_resource_include()` 4-step search order),
  §5.5 entire (gcc driver regex `(^|-)(gcc|g\+\+)(-[\d.]+)?$`, version via
  `-dumpfullversion` → `-dumpversion`, malloc-attr cap to "10.9", `_FloatN` alias asymmetry,
  `CIDX_GNUC_VERSION` override + falsy disable), G2, G3, G4, G7, G8, G9.
- 02-design: §5.6 (class shape, `kBuiltinDirRe`, memo maps), D8 (plain std::map memo,
  not thread-safe by design), D26 (in-process memo only, no persistent cache), D9
  (subprocess: 30 s timeout, empty stdin).
- Env contract: `CIDX_RESOURCE_DIR`, `CIDX_GNUC_VERSION` (01 §1.3); `LibClang::major()`
  gate from S03.

## Acceptance criteria
- `driver_search_dirs`: runs `<driver> -E -x c|c++ - -v` with empty stdin, 30 s timeout;
  parses stderr between `#include <...> search starts here` and `End of search list`; skips
  `(framework directory)` lines; keeps only existing dirs, normpathed, in driver order;
  memoized per (driver, lang) (G8).
- `driver_flags` emits `-nostdinc` + gnuc flags + dirs as `-isystem` in driver order; any
  dir matching `[/\\]lib(32|64)?[/\\](gcc|gcc-cross|clang)[/\\]` is dropped and replaced —
  once, at the FIRST occurrence's position — by this libclang's resource include; appended
  last if never matched (G3). Resource include missing entirely → WARNING log + verbatim
  driver list including gcc builtin dirs (G7).
- `resource_include()` search order exactly: `$CIDX_RESOURCE_DIR/include` →
  `<dirname(CIDX_LIBCLANG)>/clang/*/include` highest-version-first → `clang`/`clang++`
  `-print-resource-dir` → the four glob fallbacks, best numeric version; first dir
  containing `stddef.h` wins; memoized.
- Host defaults (no driver / probe mute): macOS `-isysroot $(xcrun --show-sdk-path)`
  [+ `-isystem <sdk>/usr/include/c++/v1` iff C++] then `-isystem <resource include>` —
  order asserted (G2); non-darwin: resource include only.
- gnuc masquerade (G4): driver matched by the basename regex; version
  `-dumpfullversion` → `-dumpversion`, must match `\d+(\.\d+)*`; emit
  `-fgnuc-version=<v>`; cap to `"10.9"` iff claimed major ≥ 11 ∧ `sys/cdefs.h` in the
  driver's search dirs defines `__attr_dealloc` ∧ `LibClang::major() < 21` ∧ no explicit
  env override; `_FloatN` `-D` aliases added for C when major ≥ 7 (always), for C++ only
  when major ≥ 13 AND `bits/floatn-common.h` matches `__GNUC_PREREQ (13`; non-gcc driver
  with no env override → no flag; `CIDX_GNUC_VERSION` overrides (cap bypassed), falsy set
  `{0,off,none,false}` disables entirely.
- `is_cpp`: args checked BEFORE extension — `--driver-mode=g++`, `-xc++`, `-x` followed by
  `c++*`; else extension in `.cpp .cc .cxx .c++ .hpp .hh .hxx` lowercased (G9).
- All probes memoized in plain maps; one subprocess per (driver[,lang]) per run (D8, D26).
- **Unit tests written and passing** — behaviors the tests MUST cover:
  - stderr search-list parsing: normal list, framework-dir lines skipped, nonexistent dirs
    dropped, empty output → fallthrough to host defaults.
  - builtin-dir substitution: gcc `lib/gcc/<triple>/8.5.0/include` + `include-fixed` both
    replaced by ONE resource include at the first position; foreign `lib/clang/17/include`
    also matched; lib32/lib64 variants; never-matched → appended last.
  - gnuc decision table (driver mocked by the fake-driver script fixture): non-gcc name,
    gcc-8.5 (no cap), gcc-11 + `__attr_dealloc` cdefs + libclang major mocked < 21 → "10.9",
    same with major ≥ 21 → no cap, explicit `CIDX_GNUC_VERSION=12` → cap bypassed,
    `CIDX_GNUC_VERSION=off` → no flag; FloatN: C major 7 yes, C++ major 12 no, C++ major 13
    + floatn-common probe yes/no.
  - memoization: fake-driver script counts invocations (touch-file side effect) — exactly
    one probe per (driver, lang).
  - `is_cpp` table incl. `-x c++` two-token form and arg-beats-extension cases.
  - Only `major()`-dependent cases carry the `clang` label; everything else hermetic via
    the script fixture and env injection (allow seam: gnuc cap takes the libclang major as
    an injectable parameter for tests).

## Test plan
- `ctest --test-dir build --output-on-failure -R toolchain` (default label; `clang`-labelled
  subset for real-`major()` smoke).
- Fixtures: `tests/fixtures/fake-driver*` shell scripts (committed, executable) emitting
  canned `-v` stderr with directories created in a tmp tree by the test setup so the
  exists-filter passes; canned glibc header trees (`sys/cdefs.h`, `bits/floatn-common.h`)
  written into the tmp search dirs by the tests.

## Dependencies
blockedBy: S01, S03. Parallel-safe with S07.

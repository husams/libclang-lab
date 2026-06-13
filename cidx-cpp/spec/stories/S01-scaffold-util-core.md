# S01 — Scaffold + util core

## Goal
Stand up the buildable project skeleton (CMake, doctest/CTest wiring, vendored deps, lint
configs) plus the hermetic `util/` layer, so every later story starts from a green
`cmake --build` + `ctest`.

## Scope — files to create
```
cidx-cpp/CMakeLists.txt                  # project, C++17, g++8.5 floor, conditional stdc++fs, cidx target
cidx-cpp/.clang-format                   # LLVM style
cidx-cpp/.clang-tidy
cidx-cpp/src/main.cpp                    # stub: prints usage text, exits 2 (replaced by S07)
cidx-cpp/src/util/logger.hpp  .cpp
cidx-cpp/src/util/env.hpp     .cpp
cidx-cpp/src/util/subprocess.hpp .cpp
cidx-cpp/src/util/hashing.hpp .cpp
cidx-cpp/src/util/json_min.hpp .cpp
cidx-cpp/src/util/pathutil.hpp .cpp
cidx-cpp/third_party/doctest/doctest.h   # vendored, single header
cidx-cpp/third_party/md5/md5.h  md5.c    # RFC 1321 public-domain reference impl + license note
cidx-cpp/tests/CMakeLists.txt            # one doctest exe per module + ctest registration + labels
cidx-cpp/tests/pathutil_test.cpp
cidx-cpp/tests/json_min_test.cpp
cidx-cpp/tests/hashing_test.cpp
cidx-cpp/tests/env_logger_test.cpp       # also hosts subprocess coverage (no separate exe in design §3)
```
Do NOT create `src/storage`, `src/clangx`, `src/compiledb`, `src/cli` files — later stories own them.
Also create the exceptions base (`CidxError`, `ClangParseError`, `StorageError`, `UsageError`)
— smallest viable home is a header in `src/util/` (e.g. inside `env.hpp` is wrong; use a
dedicated section of `logger.hpp` or add `src/util/` header agreed with D23; keep it in `util/`).

## Spec references
- 02-design: §2 (toolchain & deps, g++ 8.5 caveats), §3 (directory rules), §5.9 (Logger,
  env falsy sets, IndexStatus enum signatures only — impl of `files` is S02), D2, D4 (md5),
  D5 (json_min contract), D7 (logger), D9 (subprocess), D11 (pathutil = Python `os.path`
  semantics), D21 (doctest), D22 (co-located headers), D23 (error hierarchy, noexcept rule)
- 01-analysis: §1.3 (env vars + the two falsy-spelling sets), §1.4 (log record format,
  delay=True, warning counter, stderr fallback), G27, G30 (`md5_of` → nullopt on unreadable)

## Acceptance criteria
- `cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release && cmake --build build` succeeds on the
  dev box (AppleClang); CMake sets `CMAKE_CXX_STANDARD 17` REQUIRED, extensions OFF, and links
  `stdc++fs` only for GNU < 9 (D2) — verified by reading the generated link line or a CMake
  message, not by guessing.
- `cidx` binary exists, prints usage, exits 2 (stub).
- **Unit tests written and passing** (`ctest --test-dir build --output-on-failure`, all default-label):
  - `hashing_test`: md5 hex of known strings/files == `hashlib.md5().hexdigest()` pinned
    values (lowercase 32-hex); unreadable/missing file → `std::nullopt` (G30).
  - `json_min_test`: decodes Python `json.dumps(list[str])` output incl. `\uXXXX` escapes →
    UTF-8 and `", "` separators; encode→decode round-trip; non-array / non-string payloads
    rejected with an error (D5).
  - `pathutil_test`: `normpath` / `abspath` / `relpath` / `expanduser` / `dirname` / `basename`
    / `join` against a table of Python-`os.path`-generated expected values, including:
    `..` collapse, `//`, trailing-sep stripping, `"" → "."`, relpath across distinct roots (D11).
  - `env_logger_test`:
    - falsy sets exact: gnuc set `{0,off,none,false}`, headers set `{0,false,no,off}` —
      including a value in one set but not the other (§1.3).
    - logger: file NOT created before the first record (delay=True parity); record format
      `YYYY-MM-DD HH:MM:SS,mmm LEVEL name: message`; warning counter counts only file-sink
      records ≥ WARNING (INFO records don't count) (D7/G27); stderr fallback when no file sink.
    - subprocess: captures stdout+stderr separately; stdin is empty (`/dev/null`);
      timeout kills the child and reports `timed_out`; exit code surfaced (D9).
- `clang-format`/`clang-tidy` configs present; no formatting enforcement on the test box.

## Test plan
- Framework: doctest via vendored header; registered in `tests/CMakeLists.txt` with
  `add_test` + default label.
- Run: `cmake --build build && ctest --test-dir build --output-on-failure`.
- Fixtures: tiny temp files created by the tests themselves (tmp dirs); the Python-expected
  tables are hardcoded constants in the test source (generate them once with `python3` and
  paste; cite the generating one-liner in a comment).
- No libclang, no network, no Python at test runtime — fully hermetic.

## Dependencies
blockedBy: none (first story).

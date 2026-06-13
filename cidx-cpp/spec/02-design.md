# cidx-cpp — Detailed Design

Stage 4 (senior-developer) deliverable. Input: `spec/01-technical-analysis.md` (cited below as
"§N" / "GN" / "PN"). Python reference tree: `libclang-lab/project/` (read-only). Build root:
`libclang-lab/cidx-cpp/`. Conventions: `cpp-conventions` skill (deviations noted in D-ADRs).

Non-negotiable goal: **behavioral parity** with the implemented Python tool (§9.1 items 1–11),
byte-compatible schema v6, same CLI surface/exit codes/output lines, validated by porting
`_storage_smoke.py` and by golden-diff parity runs against the Python tool.

---

## 1. ADR decisions (resolves all §9.2 points P1–P20)

| ADR | Decision | Rationale |
|---|---|---|
| **D1** (P1) | ⚠️ **SUPERSEDED by Amendment A1 (§12, 2026-06-12) — link libclang at build time, no dlopen.** _Original (no longer in force):_ dlopen libclang at runtime through a single `LibClang` shim (~36 `dlsym`'d functions); resolution order `CIDX_LIBCLANG` → auto-detect (newest wins) → default names; bash launcher retired | _Original rationale (kept for the record):_ one binary across libclang 18 *and* 21; preserve `CIDX_LIBCLANG` verbatim; no link-time dependency on the gcc-8.5 build box. **See §12 for why this is reversed and what replaces it.** |
| **D2** (P2) | C++17, g++ 8.5.0 floor; CMake ≥ 3.16; single executable `cidx`; deps = system `libsqlite3` + `libdl` + (g++<9 only) `stdc++fs`; vendored: LLVM-18 `clang-c` headers, doctest, RFC-1321 MD5 | §2 below. Packaging axis from Python (uv/pip split) disappears |
| **D3** (P3) | SQLite3 **C API directly**, thin RAII wrappers (`SqliteDb`, `SqliteStmt`, `Transaction`). Keep autocommit-unless-in-txn via an `in_txn_` flag, exactly the Python `_commit()` pattern. Open with `SQLITE_OPEN_READWRITE\|CREATE`; `BEGIN`/`COMMIT`/`ROLLBACK` issued explicitly (sqlite3 C API has no Python-style implicit txns) | Smallest possible layer over the exact SQL already proven by the smoke test; no ORM/wrapper lib |
| **D4** (P4) | **Vendor the public-domain RFC 1321 MD5** reference implementation (`third_party/md5/`, one `.c`+`.h`). Output: lowercase 32-hex, identical to `hashlib.md5(...).hexdigest()` | Zero external deps (no OpenSSL); algorithm/format must not change or every existing DB's staleness data breaks. Unit test pins known digests against Python output |
| **D5** (P5) | **Hand-rolled minimal JSON codec for arrays-of-strings only** (`util/json_min`). Decode must accept anything Python `json.dumps(list[str])` emits (incl. `\uXXXX` escapes → UTF-8, `", "` separators); encode format is free (we emit compact `["a","b"]`) — read-compatibility is the only contract. Reject non-array/non-string payloads with an error | The `compile_options` column is the *only* JSON in the system once D20 delegates compile-DB parsing to libclang; ~120 lines beats a 25k-line header dep |
| **D6** (P6) | **Hand-rolled argv parser** (`cli/args`). Reproduce: exact flags/defaults of §1.2, `--dir requires --component` (exit 1), mutually-exclusive `--indexed`/`--pending` (usage error, exit 2), `ls` alias, exit 2 + usage text on unknown flag / missing required arg. **Do NOT reproduce argparse prefix-abbreviation** (`--lim` will not match `--limit`) — documented delta, golden tests never rely on abbreviations | No CLI lib gives exact exit-code-2 semantics without fighting it; the grammar is small and frozen |
| **D7** (P7) | `Logger` class: level filter (INFO floor on file sink), **lazy file creation** (open on first record = `delay=True`), record format `YYYY-MM-DD HH:MM:SS,mmm LEVEL name: message` (Python `%(asctime)s %(levelname)s %(name)s: %(message)s`), warning counter counting file-sink records ≥ WARNING (G27), **stderr fallback** when no file sink configured (Python last-resort handler parity, §1.4). Logger names are plain strings: `"cidx"`, `"cidx.clang"` | Direct port of the observable contract; the counter placement (file handler) is load-bearing for the `N warning(s)/error(s) logged to <path>` line |
| **D8** (P8) | Plain `std::map` memo inside `Toolchain`, keyed `(driver)` / `(driver,lang)`. **Not thread-safe by design** — documented, fine under D15 (single-threaded) | `lru_cache` parity without machinery; revisit only if D15 changes |
| **D9** (P9) | `util/subprocess`: `posix_spawnp`-based `run(argv, timeout_sec=30)` capturing stderr (and stdout) via pipes, stdin = `/dev/null` (empty-stdin parity), timeout via `poll`+`waitpid` loop, kill on expiry. Returns `{exit_code, stdout, stderr, timed_out}` | Only consumer is the driver probe (G8); no shell involved, argv passed verbatim |
| **D10** (P10) | **Tiny INI scanner** (`util/repo`): find section header `[remote "origin"]`, then first `url = …` line; name = URL basename with `.git` suffix stripped (repo.py parity). No shelling out to `git` | ~40 lines; matches Python configparser behavior on the one section we read; works when `git` binary is absent |
| **D11** (P11) | `std::filesystem` underneath, but **all path logic goes through `util/pathutil`** which reimplements Python semantics: `normpath` (collapse `.`/`..`/`//`, no symlink resolution, never trailing sep, `""`→`"."`), `abspath` (= `normpath(cwd/p)`), `relpath(p, start)` (`..` synthesis across roots), `expanduser`, `dirname/basename/split/join`. LIKE-escaping uses `'/'` (`os.sep`) literal — POSIX-only target | Python `os.path` and `fs::path::lexically_normal` disagree on edge cases that are stored in the DB (`directory.path` values, `_dir_scope_sql`); centralizing makes them unit-testable against Python-generated fixtures |
| **D12** (P12) | `LibClang::major()`: `clang_getClangVersion()` → `std::regex "version (\\d+)"` → int, **0 on no match**; `clang_disposeString` via the `CxString` RAII wrapper | Direct C-API port; regex + 0-fallback preserved (G4 gate) |
| **D13** (P13) | Explicit map tables (no `.name`-derived strings): `CXLinkage_Invalid→NULL`, `NoLinkage→"no-linkage"`, `Internal→"internal"`, `UniqueExternal→"unique-external"`, `External→"external"`; access `CX_CXXPublic/Protected/Private→"public"/"protected"/"private"`, invalid→NULL; pure via `clang_CXXMethod_isPureVirtual` | Stored spellings are DB content shared with Python-written rows — hardcode, assert in tests |
| **D14** (P14) | `datetime('now')` stays SQLite-side (ports as-is). `show file` mtime: `localtime_r` + `strftime`, format copied **exactly** from `cli.py:421-424` during S10 implementation (golden-tested); `indexed_at` printed verbatim + `" UTC"` suffix (G31) | Display-only; lock with golden tests rather than re-deriving |
| **D15** (P15) | **Single-threaded, sequential — parity.** No `--jobs`. Parallel indexing (per-TU workers, extract-data-not-cursors, single DB writer) is a designed-for but deferred extension: all parse/extract state is already per-call (no globals except `LibClang` + `Logger`, both read-mostly after init) | Do not change semantics silently (analysis directive); headline perf win deferred to post-parity story |
| **D16** (P16) | **Reproduce: no compile-command fingerprint.** Same content + new flags ⇒ `indexed` stays 1 (§4). Documented limitation; an options-hash column is a v7 schema change, out of scope | Parity first; v7 noted in §10 |
| **D17** (P17) | **Reproduce: no stale-symbol purge on reindex.** Removing a symbol from a file leaves the row (§4) | Per-file delete changes cross-file USR-sharing semantics — needs its own design; out of scope |
| **D18** (P18) | **Scope = the implemented Python tool, faithfully: symbol table only.** No xrefs, no call graph, no `query`/`calls` commands, no `stats` CLI (the `Storage::stats()` API *is* ported — the smoke test asserts it), no `--jobs`, no atomic DB replace. All brief-only features are v7+ extensions listed in §10 | Recommended port-first parity (analysis §9.2 P18); a working byte-compatible tool is the milestone |
| **D19** (P19) | **Reproduce: `macro` kind stays unreachable.** `parse()` uses `options = 0` (no `DETAILED_PREPROCESSING_RECORD`); the `macro` kind remains in `_KIND_MAP` and in the SQL CHECK (DB compat, G22) | Enabling the option changes parse cost *and* header skip counters; flagged, not silently changed |
| **D20** (P20) | **Use `CXCompilationDatabase`** (`clang_CompilationDatabase_fromDirectory` etc.) via the same dlopen shim — no own JSON parsing of `compile_commands.json` | Keeps `command`-string shell-unquoting byte-identical to the Python path (both go through libclang) |

Supplementary ADRs (not in §9.2):

| ADR | Decision | Rationale |
|---|---|---|
| **D21** | Test framework: **doctest** (single vendored header, `third_party/doctest/doctest.h`), run via CTest. *Deviation from cpp-conventions' gtest default* | Dispatch's small-footprint rule; no FetchContent network access needed on the air-gapped-ish gcc-8.5 test box (192.168.1.115); compiles fast under g++ 8.5 |
| **D22** | Headers co-located with sources under `src/` (`foo.hpp` + `foo.cpp`); no `include/` tree. *Deviation from cpp-conventions' include/ rule* | Single executable, nothing exported; one fewer directory to police |
| **D23** | Error handling: exceptions **inside C++ frames only** — `CidxError : std::runtime_error` with `ClangParseError`, `StorageError`, `UsageError(exit_code)` subclasses; `main()` is the only catch-site mapping to exit codes. Every libclang **C callback is `noexcept`**: errors are stashed in the visitor context struct and rethrown after the C call returns. Expected-absence = `std::optional` (lookups), never exceptions | C++17 has no `std::expected`; this mirrors Python's `ClangParseError` flow; cpp-conventions "no exceptions across C-API boundaries" honored by construction |
| **D24** | Vendor LLVM 18.1 `clang-c` public headers into `third_party/clang-c/` (Apache-2.0 WITH LLVM-exception; keep license file). Types only — functions come from `dlsym` | `CXCursor` etc. are by-value structs; their layout is ABI-stable 18→21. No clang dev package needed at build time |
| **D25** | SQLite pragmas: **parity** — `PRAGMA foreign_keys = ON` at connect *and* in the schema script, nothing else (no WAL, no synchronous tuning). No VACUUM/strip policy in v1 (Python has none); cache dir contains exactly `index.db` + `cidx.log` | Byte-compatible DB files; journal-mode changes alter on-disk artifacts the Python tool may reopen |
| **D26** | Driver-introspection caching stays **in-process memo only** (no persistent cross-run cache) | One subprocess per (driver,lang) per run (~tens of ms) is noise next to parsing; a persistent cache needs driver-binary invalidation keys (mtime/inode) and a new artifact in the cache dir — complexity for no measured win. Revisit with data |

---

## 2. Toolchain & dependencies

| Item | Choice |
|---|---|
| Language | ⚠️ **SUPERSEDED by Amendment A2 (§13, 2026-06-12) — C++23, not C++17.** _Original (no longer in force):_ C++17 (`CMAKE_CXX_STANDARD 17`), required, extensions off; "must compile with g++ 8.5.0". A2 corrects the misread: g++ 8.5 is an **indexing target**, not a build compiler. See §13 |
| g++ 8.5 caveats | _Obsolete under A2 (build hosts are AppleClang + gcc 13)._ The `<filesystem>`/`-lstdc++fs` workaround, the "no `std::expected`", and "no designated initializers" notes no longer apply to the build. g++ 8.5 remains a fully-supported **indexing target** (toolchain.cpp driver introspection — unchanged) |
| Build | CMake ≥ 3.16 (3.20+ on dev box), Ninja preferred. `CMAKE_EXPORT_COMPILE_COMMANDS=ON` in debug preset for clang-tidy |
| Link deps | `SQLite::SQLite3` (find_package, system devel package), `${CMAKE_DL_LIBS}`, conditional `stdc++fs`. **No libclang link** (D1) |
| Vendored (in-tree, no network) | `third_party/clang-c/` (D24), `third_party/doctest/doctest.h` (D21), `third_party/md5/` (D4) |
| libclang runtime | ≥ 18, ≤ 21 supported; behavior gated at runtime by `LibClang::major()` (G4 cap) |
| Format / lint | `clang-format` (LLVM style, `.clang-format` at root); `clang-tidy` (`.clang-tidy` at root); both dev-box-only, never required on the test box |
| Naming | `PascalCase` types, `snake_case` functions/variables/files, `kConstant` constants (cpp-conventions) |
| Memory rules | No raw `new`/`delete`; RAII wrappers for every C handle (CXString, CXTranslationUnit, CXIndex, CXCompilationDatabase, sqlite3*, sqlite3_stmt*, dlopen handle) |

Canonical commands:

```bash
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release && cmake --build build
ctest --test-dir build --output-on-failure
```

---

## 3. Project directory structure (canonical — nothing outside it)

```
cidx-cpp/
├── CMakeLists.txt              # root: project, options, subdirs, cidx target
├── .clang-format               # LLVM
├── .clang-tidy
├── spec/                       # EXISTS — analysis, this design, stories
│   ├── 01-technical-analysis.md
│   ├── 02-design.md
│   └── stories/                # scrum-master output, one file per story
├── src/
│   ├── main.cpp                # argv → Cli::run, top-level catch → exit code
│   ├── cli/
│   │   ├── args.hpp/.cpp       # argv grammar, ParsedArgs, usage text, exit-2 policy
│   │   ├── commands.hpp/.cpp   # cmd_add_source/import/index/search/show/list
│   │   └── format.hpp/.cpp     # output tables, mtime/indexed_at formatting (G31)
│   ├── storage/
│   │   ├── storage.hpp/.cpp    # Storage class, schema, migration, all SQL
│   │   ├── records.hpp         # Component/Directory/File/Symbol/Stats structs
│   │   └── sqlite.hpp/.cpp     # SqliteDb/SqliteStmt/Transaction RAII wrappers
│   ├── compiledb/
│   │   └── compiledb.hpp/.cpp  # CXCompilationDatabase load, strip/sanitize/driver
│   ├── clangx/                 # "x" avoids clash with vendored clang-c/
│   │   ├── libclang.hpp/.cpp   # dlopen shim + auto-detect + major() (D1, D12)
│   │   ├── toolchain.hpp/.cpp  # driver probe, resource dir, gnuc masquerade (§5.4–5.5)
│   │   ├── parse.hpp/.cpp      # parse() + diagnostic policy (§5.2–5.3)
│   │   └── ast.hpp/.cpp        # cursor walk, Symbol mapping, header indexing (§5.6)
│   └── util/
│       ├── logger.hpp/.cpp     # D7
│       ├── pathutil.hpp/.cpp   # D11
│       ├── json_min.hpp/.cpp   # D5
│       ├── subprocess.hpp/.cpp # D9
│       ├── hashing.hpp/.cpp    # md5_of(path) -> optional<string> (G30: nullopt on unreadable)
│       ├── repo.hpp/.cpp       # git_root walk-up, repo_name (D10)
│       ├── files.hpp/.cpp      # resolve_file_arg, index_status
│       └── env.hpp/.cpp        # env lookup + the two falsy-spelling sets (§1.3)
├── third_party/
│   ├── clang-c/                # vendored LLVM 18.1 headers + LICENSE (D24)
│   ├── doctest/doctest.h
│   └── md5/md5.h md5.c         # RFC 1321 public domain
├── tests/
│   ├── CMakeLists.txt          # one doctest exe per module + ctest registration
│   ├── storage_smoke_test.cpp  # PORT of _storage_smoke.py — executable spec
│   ├── storage_migration_test.cpp
│   ├── pathutil_test.cpp
│   ├── json_min_test.cpp
│   ├── hashing_test.cpp
│   ├── compiledb_test.cpp
│   ├── fuzzy_match_test.cpp
│   ├── repo_test.cpp
│   ├── env_logger_test.cpp
│   ├── toolchain_test.cpp
│   ├── ast_test.cpp            # needs libclang at test time (label: clang)
│   └── fixtures/               # tiny generated fixtures (Python-written DBs, JSON rows)
└── scripts/
    ├── parity_check.sh         # run Python cidx vs cidx-cpp on manifests/, diff outputs + DB dumps
    └── e2e_librdkafka.sh       # 93/93-TU validation driver for 192.168.1.115 (§9.1 item 11)
```

Rules: developers create files **only** inside this tree; `libclang-lab/manifests/` and
`libclang-lab/project/` are referenced **read-only** as test fixtures; the build dir is
`cidx-cpp/build*/` (git-ignored).

---

## 4. Database schema v6 (byte-compatible DDL)

`kSchemaVersion = 6`. Connection sequence (Storage ctor): `mkdir -p dirname(path)` (skip for
`:memory:`) → open → `PRAGMA foreign_keys = ON` → `migrate()` → execute schema script → commit.
Migration runs **before** the schema script (G19: schema indexes reference migrated columns).

Schema script — exact text (matches Python's expanded `_SCHEMA`, kinds in sorted order):

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS component (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    path  TEXT NOT NULL UNIQUE,
    kind  TEXT NOT NULL DEFAULT 'repo'
          CHECK (kind IN ('repo', 'external'))
);

CREATE TABLE IF NOT EXISTS directory (
    id           INTEGER PRIMARY KEY,
    component_id INTEGER NOT NULL REFERENCES component(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    UNIQUE (component_id, path)
);

CREATE TABLE IF NOT EXISTS file (
    id              INTEGER PRIMARY KEY,
    directory_id    INTEGER NOT NULL REFERENCES directory(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    mtime           REAL,
    md5             TEXT,
    compile_options TEXT,
    driver          TEXT,
    indexed         INTEGER NOT NULL DEFAULT 0,
    indexed_at      TEXT,
    UNIQUE (directory_id, name)
);

CREATE TABLE IF NOT EXISTS symbol (
    id           INTEGER PRIMARY KEY,
    usr          TEXT NOT NULL UNIQUE,
    spelling     TEXT NOT NULL,
    qual_name    TEXT,
    display_name TEXT,
    kind         TEXT NOT NULL CHECK (kind IN ('class', 'class-template',
                 'constructor', 'destructor', 'enum', 'enum-constant',
                 'function', 'function-template', 'macro', 'member', 'method',
                 'namespace', 'struct', 'type-alias', 'typedef', 'union',
                 'variable')),
    type_info    TEXT,
    file_id      INTEGER REFERENCES file(id) ON DELETE SET NULL,
    line         INTEGER,
    col          INTEGER,
    decl_file_id INTEGER REFERENCES file(id) ON DELETE SET NULL,
    decl_line    INTEGER,
    decl_col     INTEGER,
    is_definition INTEGER NOT NULL DEFAULT 0,
    is_pure      INTEGER NOT NULL DEFAULT 0,
    linkage      TEXT,
    access       TEXT,
    parent_usr   TEXT,
    resolved     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_symbol_spelling ON symbol(spelling);
CREATE INDEX IF NOT EXISTS idx_symbol_qual     ON symbol(qual_name);
CREATE INDEX IF NOT EXISTS idx_symbol_file     ON symbol(file_id);
CREATE INDEX IF NOT EXISTS idx_symbol_parent   ON symbol(parent_usr);
CREATE INDEX IF NOT EXISTS idx_symbol_kind     ON symbol(kind);

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '6');
```

Note: `CREATE TABLE IF NOT EXISTS` means cosmetic text differences vs Python's stored DDL only
matter for **fresh** DBs; the structural contract (names/constraints/indexes) is what parity
tests assert (`PRAGMA table_info`, `sqlite_master` index list).

### 4.1 Migration (`Storage::migrate()`, column-presence detection — G19)

1. If `symbol` table absent → return (fresh DB; schema script creates everything).
2. `qual_name` missing → `ALTER TABLE symbol ADD COLUMN qual_name TEXT` + recursive-CTE
   backfill (copy SQL verbatim from storage.py:231-244 — longest parent_usr chain wins).
3. `decl_file_id` missing → add `decl_file_id INTEGER REFERENCES file(id) ON DELETE SET NULL`,
   `decl_line INTEGER`, `decl_col INTEGER`; backfill
   `UPDATE symbol SET decl_file_id=file_id, decl_line=line, decl_col=col WHERE is_definition=0`.
4. `is_pure` missing → `ADD COLUMN is_pure INTEGER NOT NULL DEFAULT 0` (no backfill).
5. `file.driver` missing → `ADD COLUMN driver TEXT` (no backfill).
6. Any change → `UPDATE meta SET value='6' WHERE key='schema_version'` + commit.

No downgrade path; newer DBs open without refusal (parity — analysis §3.3).

### 4.2 Upsert SQL (ported verbatim — G13, G14)

Copy the three statements from the Python source character-for-character (semantics frozen by
the smoke test):

- component: `INSERT … ON CONFLICT(path) DO UPDATE SET name=excluded.name, kind=excluded.kind RETURNING id` (storage.py:298-300)
- directory: `ON CONFLICT(component_id, path) DO UPDATE SET path=excluded.path RETURNING id`; path normalized, `.`/`""` → `""` (storage.py:367-374)
- file: COALESCE for mtime/compile_options/driver/md5; `indexed = CASE WHEN excluded.md5 IS NOT NULL AND excluded.md5 IS NOT file.md5 THEN 0 ELSE file.indexed END` — keep the NULL-safe `IS NOT` (storage.py:438-448)
- symbol: spelling/kind always overwritten; qual_name/display_name/type_info/linkage/access/parent_usr/decl_* COALESCE; file_id/line/col `CASE WHEN excluded.is_definition >= symbol.is_definition`; is_definition/is_pure/resolved `MAX(...)` (storage.py:600-623)
- mark_file_indexed: `UPDATE file SET indexed=1, indexed_at=datetime('now'), mtime=COALESCE(?, mtime) WHERE id=?`

`RETURNING` requires SQLite ≥ 3.35 — assert at startup (`sqlite3_libversion_number() >= 3035000`),
fail with a clear message otherwise (the test box ships 3.26 on EL8: if so, the fallback is
`INSERT ... ON CONFLICT DO UPDATE` + `SELECT id` in the same statement scope — **S2 must probe the
box first** and pick one path; do not ship both).

---

## 5. Class design

Conventions for all classes: non-copyable where they own a C handle; `std::optional<T>` for
absent rows; exceptions per D23; all paths through `pathutil`.

### 5.1 `storage/records.hpp` — plain structs (mirror the Python dataclasses)

```cpp
struct Component { int64_t id; std::string name, path, kind; };
struct Directory { int64_t id, component_id; std::string path; };
struct File {
  int64_t id, directory_id;
  std::string name;
  std::optional<double> mtime;
  std::optional<std::string> md5;
  std::optional<std::vector<std::string>> compile_options;  // decoded JSON
  std::optional<std::string> driver;
  bool indexed = false;
  std::optional<std::string> indexed_at;
};
struct Symbol {
  std::string usr, spelling, kind;                  // kind ∈ kSymbolKinds
  std::optional<std::string> qual_name, display_name, type_info,
                             linkage, access, parent_usr;
  std::optional<int64_t> file_id, line, col, decl_file_id, decl_line, decl_col;
  bool is_definition = false, is_pure = false, resolved = false;
  int64_t id = -1;
};
struct Stats { int64_t components, directories, files, files_indexed, symbols,
               symbols_unresolved; std::map<std::string,int64_t> symbols_by_kind; };
```

### 5.2 `storage/sqlite.hpp` — RAII layer

```cpp
class SqliteDb {                       // owns sqlite3*
 public:
  explicit SqliteDb(const std::string& path);   // throws StorageError
  ~SqliteDb();                                  // sqlite3_close
  SqliteStmt prepare(std::string_view sql);
  void exec(std::string_view sql_script);       // sqlite3_exec, multi-stmt
  sqlite3* raw();
};
class SqliteStmt {                     // owns sqlite3_stmt*; movable
 public:
  void bind(int idx, std::nullptr_t / int64_t / double / std::string_view);
  bool step();                                   // true = SQLITE_ROW; throws on error
  // typed column getters: col_int64/col_double/col_text/col_is_null
};
```

### 5.3 `storage/storage.hpp` — `class Storage` (API = Python `Storage`, 1:1)

```cpp
class Storage {
 public:
  explicit Storage(const std::string& path = ":memory:");
  Transaction transaction();                       // RAII: BEGIN; commit on ~ unless failed
  // components
  int64_t add_component(const std::string& name, const std::string& path,
                        const std::string& kind = "repo");
  std::optional<Component> get_component(const std::string& path);          // by abspath
  std::optional<Component> get_component_by_name(const std::string&);
  std::optional<Component> get_component_by_id(int64_t);
  std::optional<Component> component_for_path(const std::string& abs);      // longest-prefix, app-side (G16)
  std::vector<Component> list_components(std::optional<std::string> name = {},
                                         std::optional<std::string> kind = {});
  // directories
  int64_t add_directory(int64_t component_id, const std::string& rel_path);
  std::optional<Directory> get_directory(int64_t component_id, const std::string& path);
  std::optional<Directory> get_directory_by_id(int64_t);
  std::vector<std::pair<Directory, std::string>> list_directories(
      std::optional<int64_t> component_id = {}, std::optional<std::string> name = {});
  // files
  int64_t add_file(int64_t directory_id, const std::string& name,
                   std::optional<double> mtime = {}, std::optional<std::string> md5 = {},
                   std::optional<std::vector<std::string>> compile_options = {},
                   std::optional<std::string> driver = {});
  int64_t add_file_path(const std::string& abs, /* same optionals */);      // throws StorageError if unowned
  std::optional<File> get_file(const std::string& abs);
  std::optional<File> get_file_by_id(int64_t);
  std::optional<std::string> file_abs_path(int64_t file_id);
  std::vector<std::pair<File, std::string>> list_files(                      // (row, abs path)
      std::optional<int64_t> component_id = {}, std::optional<std::string> dir_path = {},
      std::optional<std::string> name = {}, std::optional<bool> indexed = {});
  bool is_file_indexed(const std::string& abs, std::optional<double> mtime = {},
                       std::optional<std::string> md5 = {});
  void mark_file_indexed(int64_t file_id, std::optional<double> mtime = {});
  // symbols
  int64_t add_symbol(const Symbol&);               // throws StorageError on bad kind
  bool update_symbol(const std::string& usr,
                     const std::vector<std::pair<std::string, SqlValue>>& values);
                     // SqlValue = variant<nullptr_t,int64_t,double,string>;
                     // unknown column / bad kind -> throws (smoke-test parity)
  std::optional<Symbol> lookup_symbol(const std::string& usr);
  std::optional<Symbol> lookup_symbol_by_id(int64_t);
  std::vector<Symbol> lookup_symbols_by_name(const std::string& spelling,
                                             std::optional<std::string> kind = {});
  std::vector<Symbol> search_symbols(const std::string& pattern,
                                     std::optional<std::string> kind = {});   // ::-segment fuzzy, length-first
  std::vector<Symbol> list_symbols(std::optional<int64_t> component_id = {},
                                   std::optional<std::string> dir_path = {},
                                   std::optional<int64_t> file_id = {},
                                   std::optional<std::string> name = {},      // char-in-order fuzzy
                                   std::optional<std::string> kind = {});
  std::vector<Symbol> symbols_in_file(int64_t file_id);
  std::vector<Symbol> unresolved_symbols();
  Stats stats();
 private:
  static std::string fuzzy_like(std::string_view);  // %c%c%, escape \ % _ (G18)
  static std::string dir_scope_sql(...);            // '' root -> '%' (G17)
  void commit_unless_in_txn();
  SqliteDb db_; bool in_txn_ = false;
};
```

Behavior notes (all smoke-asserted): location scope in `list_symbols` matches definition **or**
declaration site; `search_symbols` ordering `LENGTH(qual_name), qual_name`; `_fuzzy_like`
strips whitespace chars; limits are applied by the CLI (slicing), not by Storage.

### 5.4 `clangx/libclang.hpp` — `class LibClang` (D1)

```cpp
class LibClang {                       // process singleton; loaded once before first use
 public:
  static LibClang& instance();
  void load();          // CIDX_LIBCLANG -> auto_detect() -> default names; throws CidxError
  int major() const;    // D12; cached
  std::string library_path() const;    // for CIDX_RESOURCE_DIR derivation
  // function pointers, dlsym'd via X-macro; nullptr-checked at load:
  // clang_getClangVersion, clang_getCString, clang_disposeString,
  // clang_createIndex, clang_disposeIndex,
  // clang_parseTranslationUnit2, clang_disposeTranslationUnit,
  // clang_getNumDiagnostics, clang_getDiagnostic, clang_disposeDiagnostic,
  // clang_getDiagnosticSeverity, clang_getDiagnosticSpelling, clang_getDiagnosticLocation,
  // clang_getTranslationUnitCursor, clang_visitChildren,
  // clang_getCursorKind, clang_getCursorUSR, clang_getCursorSpelling,
  // clang_getCursorDisplayName, clang_getCursorLocation, clang_getExpansionLocation,
  // clang_getFileName, clang_getFile, clang_getLocation, clang_Location_isInSystemHeader,
  // clang_isCursorDefinition, clang_CXXMethod_isPureVirtual,
  // clang_getCursorLinkage, clang_getCXXAccessSpecifier, clang_getCursorSemanticParent,
  // clang_getCursorType, clang_getTypeSpelling, clang_getInclusions,
  // clang_CompilationDatabase_fromDirectory, clang_CompilationDatabase_dispose,
  // clang_CompilationDatabase_getAllCompileCommands, clang_CompileCommands_dispose,
  // clang_CompileCommands_getSize, clang_CompileCommands_getCommand,
  // clang_CompileCommand_getDirectory, clang_CompileCommand_getFilename,
  // clang_CompileCommand_getNumArgs, clang_CompileCommand_getArg
 private:
  static std::optional<std::string> auto_detect();  // launcher port (cidx:26-69):
      // candidates: `llvm-config --libdir` (via subprocess), /opt/llvm*/lib{,64},
      // /usr/lib/llvm-*/lib, /usr/local/llvm*/lib; score = highest numeric dir under
      // <libdir>/clang/; no llvm-config priority; names libclang.so|.so.*|.dylib
  void* handle_ = nullptr;             // never dlclose'd (process lifetime)
};
struct CxString {                      // RAII over CXString
  explicit CxString(CXString s); ~CxString();      // clang_disposeString
  std::string str() const;                         // clang_getCString, ""-safe
};
```

### 5.5 `compiledb/compiledb.hpp`

```cpp
struct CompileCommand { std::string directory, filename, driver;
                        std::vector<std::string> args; };   // args = already-stripped
class CompileDb {
 public:
  // --db arg: strip trailing "compile_commands.json", accept dir; abspath
  static std::vector<CompileCommand> load(const std::string& db_arg);  // throws CidxError
  static std::vector<std::string> strip_for_libclang(
      const std::vector<std::string>& argv, const std::string& filename,
      const std::string& directory);                                   // §6 drop sets, G10/G12
  static std::vector<std::string> sanitize(const std::vector<std::string>& stored); // G11
  static std::string driver(const std::vector<std::string>& argv,
                            const std::string& directory);             // abspath iff contains sep
};
```

Frozen constants (G10): `kDrop = {-c, --, -M, -MM, -MD, -MMD, -MG, -MP, -MV, -Werror,
-pedantic-errors}`; `kDropWithArg = {-o, -MF, -MT, -MQ, -dependency-file,
--serialize-diagnostics}`; `kDropPrefix = {-Werror=, -Wp,-M, -MF, -MT, -MQ}`; source dropped by
full path **or basename**; `-I/-isystem/-iquote` absolutized in both spaced and glued forms (G12).

### 5.6 `clangx/toolchain.hpp` — `class Toolchain`

```cpp
class Toolchain {                       // owns all memo maps (D8); one instance per run
 public:
  std::vector<std::string> toolchain_flags(bool cpp, const std::optional<std::string>& driver);
  static bool is_cpp(const std::string& filename, const std::vector<std::string>& args); // G9: args before extension
 private:
  std::optional<std::string> resource_include();   // §5.4 search order 1-4, memoized
  std::vector<std::string> driver_flags(const std::string& driver, bool cpp);  // memo (driver,lang)
  std::vector<std::string> driver_search_dirs(const std::string& driver, bool cpp);
      // subprocess: <driver> -E -x c|c++ - -v, empty stdin, 30 s (D9); parse stderr
      // between "#include <...> search starts here" / "End of search list";
      // skip "(framework directory)"; keep existing dirs, normpath, driver order
  std::optional<std::string> gcc_version(const std::string& driver);  // -dumpfullversion → -dumpversion
  std::vector<std::string> gnuc_flags(const std::string& driver, bool cpp);
      // §5.5: -fgnuc-version=<v>; cap to "10.9" iff major>=11 && cdefs __attr_dealloc
      // && LibClang::major()<21 && !env-override; _FloatN -D aliases:
      // C && major>=7 always; C++ && major>=13 && floatn-common.h "__GNUC_PREREQ (13";
      // CIDX_GNUC_VERSION override (falsy set {0,off,none,false} disables)
  bool glibc_has_attr_dealloc(const std::string& driver);   // scan search dirs for sys/cdefs.h
  bool glibc_floatn_keyword_cpp(const std::string& driver); // bits/floatn-common.h probe
  // builtin-dir substitution: std::regex kBuiltinDirRe{R"([/\\]lib(32|64)?[/\\](gcc|gcc-cross|clang)[/\\])"}
  // replace at first occurrence with resource include; append if never matched (G3);
  // resource include missing entirely -> WARNING + verbatim driver list (G7)
};
```

Host defaults (no driver / probe mute): macOS `-isysroot $(xcrun --show-sdk-path)`
[+ `-isystem <sdk>/usr/include/c++/v1` for C++] then `-isystem <resource include>` — order
load-bearing (G2); non-darwin: resource include only.

### 5.7 `clangx/parse.hpp`

```cpp
struct ParsedTu {                       // RAII: owns CXIndex + CXTranslationUnit
  CXTranslationUnit tu; CXIndex index; std::string spelling;  // = path as passed (G24)
  ~ParsedTu();                          // disposeTranslationUnit then disposeIndex
};
class Parser {
 public:
  // final argv = stored_args + toolchain.toolchain_flags(cpp, driver) + {"-ferror-limit=0"} (G5)
  // options = 0 (D19); fresh CXIndex per parse (§2.2)
  ParsedTu parse(const std::string& abs_path, const std::vector<std::string>& args,
                 const std::optional<std::string>& driver);   // throws ClangParseError
 private:
  void apply_diagnostic_policy(CXTranslationUnit, const std::string& path,
                               const std::vector<std::string>& final_args);
      // abort level: Fatal, or Error when CIDX_STRICT=1 (G6)
      // fatal: log full flag dump + libclang major at ERROR (log only, G28);
      //   <=25 per-diag INFO lines "<TU>: diag <file>:<line>: <msg>" + suppressed line;
      //   throw ClangParseError("<file>: N fatal diagnostic(s): <first 3 ';'-joined>")
      // tolerated errors: one WARNING "<file>: N error diagnostic(s) ignored
      //   (CIDX_STRICT=1 to abort)" + same capped INFO lines (G27 counter stays 1/file)
};
```

### 5.8 `clangx/ast.hpp`

```cpp
struct HeaderStats { int indexed=0, symbols=0, already=0, system=0, unowned=0; };
class AstIndexer {
 public:
  AstIndexer(Storage& db, Logger& log);
  int index_symbols(const ParsedTu&, const std::string& filename, int64_t file_id);
      // wraps one db.transaction(); streams cursors (no vector of cursors retained)
  HeaderStats index_headers(const ParsedTu&);    // clang_getInclusions (transitive);
      // dedupe by abspath; skip system (clang_getLocation(tu,file,1,1) ->
      // clang_Location_isInSystemHeader, default-on via INDEXER_IGNORE_SYSTEM_HEADERS,
      // falsy set {0,false,no,off}) / unowned (component_for_path) / already (md5);
      // else: file row (mtime+md5, NULL options/driver), extract symbols from THIS
      // TU's AST matched against the include *spelling* (G23), mark indexed
 private:
  void for_file_cursors(const ParsedTu&, const std::string& filename,
                        const std::function<void(CXCursor)>& fn);
      // clang_visitChildren; visitor is noexcept (D23):
      //   location file null/!= filename -> CXChildVisit_Continue (prune subtree, G21)
      //   mapped kind -> fn(cursor); function-like kind -> Continue (skip body) else Recurse
  std::optional<Symbol> to_symbol(CXCursor, int64_t file_id);  // §5.6 extraction:
      // skip empty USR; kind map (17 entries, table below); qual_name from semantic
      // parents skipping empty spellings (G25); linkage/access via D13 tables;
      // decl cursors record own site as decl_*, definition cursors leave decl_* null
  void store(const Symbol&);   // resolved-row skip + decl-site patch via update_symbol (G15)
};
```

Kind map (frozen): `ClassDecl→class, StructDecl→struct, UnionDecl→union,
FunctionDecl→function, CXXMethod→method, FieldDecl→member, Constructor→constructor,
Destructor→destructor, EnumDecl→enum, EnumConstantDecl→enum-constant, TypedefDecl→typedef,
TypeAliasDecl→type-alias, ClassTemplate→class-template, FunctionTemplate→function-template,
VarDecl→variable, Namespace→namespace, MacroDefinition→macro` (unreachable, D19).
Function-like (bodies not walked): FunctionDecl, CXXMethod, Constructor, Destructor,
FunctionTemplate.

### 5.9 `util/` + `cli/`

```cpp
// util/logger.hpp (D7)
class Logger {
 public:
  static Logger& root();                       // "cidx"; child("clang") -> "cidx.clang"
  void set_file(const std::string& path);      // lazy-open on first record
  void info/warning/error(const std::string& name, const std::string& msg);
  int warning_count() const;                   // records >= WARNING written to file sink
};
// util/files.hpp
enum class IndexStatus { kNotIndexed, kNoStoredMd5, kMd5Mismatch, kOk };  // files.py:20-28
IndexStatus index_status(Storage&, const std::string& abs_path);          // md5-only (§4)
std::string resolve_file_arg(const std::string& arg, Storage&,
                             const std::optional<std::string>& source_component);
// util/env.hpp — exact falsy sets:
bool env_flag_disabled_gnuc(const char* v);    // {"0","off","none","false"}
bool env_flag_false_headers(const char* v);    // {"0","false","no","off"}
// cli/commands.hpp — int cmd_*(const ParsedArgs&, Context&) for each command;
// Context = {cache_dir, Storage, Logger, Toolchain, Parser}; cache_dir =
// $INDEXER_CACHE else ~/.cache/cidx; DB always <cache>/index.db, log <cache>/cidx.log (§1.3)
```

---

## 6. Main flow

### 6.1 `cidx index [FILE...] [--source C]` (the full pipeline)

```
main(argv)
 ├─ Cli::parse(argv)                 — usage error -> usage text + exit 2 (G29)
 ├─ cache dir resolve; Logger::set_file(<cache>/cidx.log)   — lazy, G27
 ├─ LibClang::instance().load()      — CIDX_LIBCLANG / auto-detect / default (D1)
 ├─ Storage db(<cache>/index.db)     — mkdir -p, migrate, schema (§4)
 ├─ target list: no args -> all file rows (db.list_files(), header rows included;   (corrected per review R7)
 │               skip via md5-only IndexStatus::kOk check); args -> resolve_file_arg
 │               each (rel -> --source root else CWD); unknown -> error, fail flag
 ├─ for each file:
 │   ├─ index_status(db, path) == kOk -> skip (md5-only, §4)
 │   ├─ opts = CompileDb::sanitize(stored compile_options)         (G11)
 │   ├─ cpp  = Toolchain::is_cpp(path, opts)                       (G9)
 │   ├─ tu   = Parser::parse(path, opts, stored driver)
 │   │         argv = opts + toolchain_flags(cpp, driver) + "-ferror-limit=0"
 │   │         diagnostics policy §5.7 — ClangParseError -> log, print error, fail flag, continue
 │   ├─ n    = AstIndexer::index_symbols(tu, path, file_id)        — one txn
 │   ├─ hs   = AstIndexer::index_headers(tu)
 │   ├─ db.mark_file_indexed(file_id, current mtime)
 │   ├─ print "-> N symbols; headers: A indexed (+B symbols), C already, D system, E unowned"
 │   └─ ParsedTu dtor frees TU+Index immediately (one-AST peak memory)
 ├─ if Logger::warning_count() > 0: print "N warning(s)/error(s) logged to <path>"
 └─ exit: 1 if any file failed/unknown, else 0
```

### 6.2 `cidx import --db PATH [--name N]`

`CompileDb::load` (CXCompilationDatabase, D20) → component root = git root of first source
else its dirname → `add_component` → **one transaction**: per command `strip_for_libclang` +
`driver()` + mtime (nullopt if missing, G30) + `md5_of` (nullopt if unreadable) →
`add_file_path` (upsert resets `indexed` on md5 change, G13); sources outside any component →
stderr line + skip counter. Print `imported N file(s), skipped M`. Exit 1 on load failure or
empty DB.

### 6.3 Query commands

`search` / `show symbol|file` / `list components|dirs|files|symbols` are pure
Storage reads + `cli/format` rendering — formats, limits (25/50, 0 = all, applied by slicing),
exit-1-on-zero-matches, `ls` alias, `--dir`-requires-`--component`, def/decl second row in
search output: all per §1.2, locked by golden tests against the Python tool's output.

---

## 7. Technical-requirements compliance

| Requirement | Design answer |
|---|---|
| Small memory footprint | Streaming cursor visits via `clang_visitChildren` callback — cursors are processed and dropped, never collected; one TU alive at a time (`ParsedTu` RAII, freed before the next parse); per-file symbol writes go straight to SQLite inside the txn; no in-memory symbol cache |
| Caching / speed | md5 incrementality preserved exactly (§4 of analysis: `index_status` md5-only skip; upsert `indexed`-reset on md5 change; header skip via own md5); per-file + import transactions (the 100× batching win); driver introspection memoized in-process (D26 — no persistent probe cache in v1); headers harvested from the including TU, never parsed standalone |
| Minimal disk footprint | Cache dir = `index.db` + `cidx.log` only; no new artifacts; pragma parity (D25, no WAL side-files); single static-ish binary; vendored deps compiled in |
| Toolchain reality | g++ 8.5 / C++17 floor enforced in CMake; runtime libclang 18–21 via dlopen (D1); SQLite `RETURNING` floor probed in S2 (§4.2) |

---

## 8. Testing strategy

Framework: doctest (D21); harness: CTest (`ctest --test-dir build --output-on-failure`).
Labels: default (hermetic, no libclang), `clang` (needs a loadable libclang), `parity`
(needs Python cidx + uv; dev box only), `e2e` (test box 192.168.1.115).

| Test exe | Covers | Notes |
|---|---|---|
| `storage_smoke_test` | **Port of `_storage_smoke.py`, assertion-for-assertion** — the executable spec for §3.4/§3.5 (G13–G18) | First C++ test written (analysis §9.1.4); uses tmp dirs; includes reopen-persistence check |
| `storage_migration_test` | §4.1: open fixture DBs at v2/v3/v4/v5 layouts (fixtures generated once by a Python script into `tests/fixtures/`, committed), assert column adds + qual_name CTE backfill + meta bump (G19) | Fixtures are Python-written → also proves cross-tool open |
| `pathutil_test` | normpath/relpath/abspath vs a table of Python-generated expected values | D11 risk burn-down |
| `json_min_test` | decode of Python `json.dumps` outputs (incl. `\uXXXX`, separators), encode round-trip | D5 |
| `hashing_test` | md5 hex vs `hashlib` known digests; unreadable file → nullopt (G30) | D4 |
| `compiledb_test` | strip/sanitize/driver against arg-vector tables (G10–G12: `--`, `-Wp,-M…`, glued `-MF…`, basename source match, glued/spaced `-I` absolutize) | hermetic — feeds vectors directly |
| `fuzzy_match_test` | both algorithms + ordering (G18), `_dir_scope_sql` root case (G17), `component_for_path` longest-prefix (G16) | |
| `repo_test`, `env_logger_test` | git_root/repo_name on a synthetic `.git/config`; falsy sets; lazy log creation; warning counter | |
| `toolchain_test` | search-list stderr parsing, builtin-dir regex substitution position, gnuc cap/FloatN decision table (driver mocked via a fake-driver shell script fixture), `is_cpp` | label `clang` only for `major()` bits |
| `ast_test` | kind map, qual-name building, decl/def split, body/subtree pruning, header counters — parsing `libclang-lab/manifests/` samples (shapes.c, geometry.cpp, project/) **read-only** | label `clang` |
| `scripts/parity_check.sh` | run Python cidx and cidx-cpp with separate `INDEXER_CACHE` dirs over `manifests/project/compile_commands.json`; diff all CLI command outputs + `sqlite3 .dump` (excluding `indexed_at`/`mtime`) | label `parity`; the strongest §9.1 check |
| `scripts/e2e_librdkafka.sh` | librdkafka × gcc-8.5 cross toolchain × libclang 21.1.1 → **93/93 TUs** (analysis §9.1.11; box per memory note `gcc-index-test-box`) | label `e2e`, manual gate before release |

Exit-criteria convention for stories: every story lists the exact `cmake --build` +
`ctest -L <label>` commands that must pass.

---

## 9. Story split (for the scrum master to refine)

Dependency order; ◇ = parallel-safe within its row once the row above is merged.

| # | Story | Delivers | Depends on |
|---|---|---|---|
| S1 | Scaffold + util core | CMakeLists (g++ 8.5 + stdc++fs handling), .clang-format/.clang-tidy, doctest wiring, `logger`, `env`, `subprocess`, vendored md5 + `hashing`, `json_min`, `pathutil` + all their tests | — |
| S2 | Storage | `sqlite` RAII + `storage` + records; **probe test-box SQLite for `RETURNING`** and fix the §4.2 path; `storage_smoke_test` + `storage_migration_test` green | S1 |
| S3 ◇ | LibClang shim | dlopen/X-macro shim, auto-detect (launcher port), `major()`, `CxString`; vendored clang-c headers | S1 |
| S4 ◇ | CompileDb | load via CXCompilationDatabase + strip/sanitize/driver + tests | S3 |
| S5 ◇ | Repo/files utils | `repo` (INI scanner, git_root), `files` (resolve_file_arg, index_status) + tests | S2 |
| S6 | Toolchain | driver probe, resource-dir search, builtin-dir substitution, gnuc masquerade + FloatN, host defaults + `toolchain_test` | S1, S3 |
| S7 | Parser + diagnostics | `parse.cpp`, diagnostic policy, log formats (G27/G28) | S3, S6 |
| S8 | AST indexer | streaming walk, Symbol extraction, header indexing + counters, `ast_test` | S2, S7 |
| S9 | CLI: add-source + import | args grammar core, the two write commands, exit codes | S2, S4, S5 |
| S10 | CLI: index | §6.1 flow end-to-end, warning-count line | S8, S9 |
| S11 ◇ | CLI: search/show/list | query commands + `cli/format` (G31 time formats), golden output tests | S2, S9 |
| S12 | Parity + e2e | `parity_check.sh` green on manifests; `e2e_librdkafka.sh` 93/93 on 192.168.1.115 | S10, S11 |

---

## 10. Out of scope (explicit, per D16–D19)

Deferred to v7+ (each needs its own design/ADR): xref map + call graph + `query`/`calls`
commands; `stats` CLI command; `--jobs` parallel indexing (D15 notes the constraints);
atomic DB replace; compile-command fingerprint invalidation (schema v7); stale-symbol purge;
`DETAILED_PREPROCESSING_RECORD` / reachable `macro` kind; persistent driver-probe cache (D26);
reverse header→TU dependency invalidation (analysis §4 known limitation — carried).

## 11. References

- `spec/01-technical-analysis.md` (primary; §/G/P citations throughout)
- Python source: `libclang-lab/project/indexer/` — `storage.py` (DDL + upsert SQL ported
  verbatim), `_storage_smoke.py` (executable spec), `cli.py` (output formats for golden tests),
  `clang/util.py`, `clang/ast.py`, `compiledb.py`, `utils/*`, `../cidx` (launcher → D1)
- Memory notes: `cidx-toolchain-support` (toolchain matrix), `gcc-index-test-box` (192.168.1.115)
- Wiki prior art: `[[pages/code/cpp-indexer]]`, `[[pages/planning/codexgraph-cpp-libclang-rust]]`
- Conventions: `cpp-conventions` skill (deviations: D21 doctest, D22 co-located headers)

---

## 12. Amendment A1 — D1 revised: link-time libclang (2026-06-12)

**Status:** accepted (stakeholder override). **Supersedes:** D1 (§1, marked superseded).
**Unchanged:** D2–D26, all of §3–§11 except the items A1 explicitly edits below.

**Decision:** cidx **links** libclang at build time like a normal C++ program. The dlopen/dlsym
shim is removed. Rationale: D1's runtime-load was justified as Python parity, but Python loads
via ctypes only because ctypes cannot link; a C++ binary should link. The "one binary spanning
libclang 18 and 21" benefit is dropped deliberately — a build is pinned to the libclang it links
against; the 18-vs-21 axis is now a build-time choice, and `clang_getClangVersion()` still drives
`major()` at runtime so G4's version gate is unaffected.

### A1.1 Linking (CMake — S03 + S01)

- New cache var: `set(CIDX_LIBCLANG "" CACHE FILEPATH "Path to libclang shared library")`.
  `-DCIDX_LIBCLANG=/path/to/libclang.{so,dylib}` at configure time **takes precedence** over all
  search.
- `find_library(CIDX_LIBCLANG_LIB NAMES clang libclang HINTS <hints> PATHS <paths>)` where, in order:
  1. `${CIDX_LIBCLANG}` directory if set (via `get_filename_component(... DIRECTORY)` → `HINTS`);
     also accept it as a full filepath (if it names a file, use it directly, skip `find_library`).
  2. `llvm-config --libdir` (run at configure time via `execute_process`, OPTIONAL/quiet on failure).
  3. Globs: `/opt/llvm*/lib64 /opt/llvm*/lib /usr/lib/llvm-*/lib /usr/local/llvm*/lib`.
  4. macOS pip-wheel dir: `execute_process(python3 -c "import clang.cindex, os; print(os.path.dirname(clang.cindex.__file__)+'/native')")` (OPTIONAL) — the wheel ships `libclang.dylib` there.
  5. System default (no `PATHS` → CMake's standard library search).
- `find_path(CIDX_CLANG_C_INCLUDE clang-c/Index.h ...)` is **not** needed: keep vendored
  `third_party/clang-c/` headers (D24 unchanged) for ABI-stable types; the linker resolves the
  functions. (If a developer prefers the matching system `clang-c` headers they may, but vendored
  stays the default so the build needs no `-dev` package.)
- Link: `target_link_libraries(cidx_core PUBLIC ${CIDX_LIBCLANG_LIB})`.
- **Fail-fast:** if `CIDX_LIBCLANG_LIB` unresolved → `message(FATAL_ERROR "libclang not found; pass
  -DCIDX_LIBCLANG=/path/to/libclang.so")`. The build cannot produce a binary without it — there is
  no runtime "no library" path anymore.
- **RPATH (runs without `LD_LIBRARY_PATH`):**
  `get_filename_component(_clangdir "${CIDX_LIBCLANG_LIB}" DIRECTORY)` then on the `cidx` target set
  `BUILD_RPATH "${_clangdir}"` and `INSTALL_RPATH "${_clangdir}"`, `INSTALL_RPATH_USE_LINK_PATH ON`.
  On macOS this is honored as-is (absolute rpath); no `@rpath` rewrite needed for an absolute dir.

### A1.2 Shim class fate (S03) — RECOMMENDED: thin facade, minimal churn

- **Keep `class LibClang` as a thin facade**; do **not** de-indirect ~43 call sites. Reasons: every
  caller already goes through `lib.clang_*(...)`; rewriting them to free `::clang_*` calls is pure
  churn across `parse.cpp`, `ast.cpp`, `toolchain.cpp`, `compiledb.cpp`, `libclang.cpp` with no
  behavioral gain and a large diff to review.
- Concrete changes to `src/clangx/libclang.{hpp,cpp}`:
  - Delete the `CIDX_LIBCLANG_FUNCTIONS` X-macro, the `decltype(&::name) name = nullptr;` member
    block, `auto_detect()`, `load_library()`, `handle_`, and all `dlopen`/`dlsym` code.
  - Replace each former function-pointer member with a **forwarding method of the same name** that
    calls the linked symbol, e.g.
    `CXString clang_getClangVersion() const { return ::clang_getClangVersion(); }` (variadic-free
    1:1 forwarders; preserves every call site `lib.clang_getCursorUSR(c)` verbatim). A second X-macro
    over `(name, ret, args...)` may generate these to keep the file short — implementer's choice.
  - `load()` becomes a no-op kept for call-site/test compatibility (or is deleted and its one call
    site in §6.1 removed — S03 decides; no-op is lower churn). `loaded()` returns `true`.
  - `major()` / `parse_clang_major()` unchanged (still `clang_getClangVersion()` + regex, P12/D12).
  - `library_path()` semantics change — see A1.3.
- `CxString` (D23) is unchanged; it already calls through a `LibClang&`.

### A1.3 Env-contract delta (analysis §1.3 table — S03 doc, S04 code)

- **`CIDX_LIBCLANG` at runtime: ignored, with a one-shot WARNING.** It is now a *configure-time*
  CMake hint only. If the env var is set at runtime, `main()` (or `LibClang::instance()`) logs once
  at WARNING via the `cidx` logger: `CIDX_LIBCLANG is set but ignored: this build links libclang at
  <path> (set at build time)`. This is a deliberate, documented divergence from Python (where the
  var selects the library). Do not error — a stale env export must not break runs.
- **`library_path()` source = the build-time path baked in.** Add a compile definition in CMake:
  `target_compile_definitions(cidx_core PRIVATE CIDX_LIBCLANG_PATH="${CIDX_LIBCLANG_LIB}")` and have
  `library_path()` return that string. Chosen over `dladdr()` because it is simpler, has no extra
  syscall, is correct even before any libclang call, and yields the exact path CMake linked (dladdr
  would report the resolved/realpath which can differ from the resource-dir-adjacent layout). S04's
  `resource_include()` search order is **unchanged** (CIDX_RESOURCE_DIR → `<dirname(library_path())>/
  clang/*/include` → `clang -print-resource-dir` → globs); only the *source* of `library_path()` moved
  from "what we dlopen'd" to "what we linked".
- **`CIDX_RESOURCE_DIR`: unchanged** (still overrides resource-dir derivation, still must contain
  `include/stddef.h`).
- **`major()` still runtime** via `clang_getClangVersion()` — G4 cap logic (§5.5) untouched.

### A1.4 Test / CI implications (S01, S08)

- **SKIP-77 machinery:** the `clang`-labelled tests now **always build and run** (the binary cannot
  link without libclang, so "no dylib" can't occur). Remove the no-dylib skip path entirely. **Keep
  CTest SKIP (exit 77) ONLY for the fixture-gap case** — e.g. an `ast_test` case that needs a sample
  the environment lacks, or `e2e` when the gcc-8.5 toolchain/box is absent. Default + `clang` labels
  are now unconditional.
- **Configure-time injection:** CI/dev configure passes `-DCIDX_LIBCLANG=...` (or relies on
  auto-find). Add the resolved path to the configure summary (`message(STATUS "libclang: ${...}")`)
  for debuggability.
- **Parity script (`scripts/parity_check.sh`, S08):** the C++ side no longer reads `CIDX_LIBCLANG` —
  drop it from the C++ invocation env (or leave it; it's a harmless ignored-with-warning). The
  **Python side still requires** `CIDX_LIBCLANG` (or relies on its own launcher) — keep pinning it
  there. Both tools must be pointed at the **same libclang** for a fair diff: pin the Python env to
  the path the C++ binary was *built* against (read back from the configure summary or
  `CIDX_LIBCLANG_PATH`).
- **e2e box (`scripts/e2e_librdkafka.sh`, 192.168.1.115, S08):** configure with
  `-DCIDX_LIBCLANG=/opt/llvm-21.1.1/lib/libclang.so` (auto-find via the `/opt/llvm*` glob also works);
  RPATH then lets `cidx` run with no `LD_LIBRARY_PATH`. The 93/93-TU target is unchanged.

### A1.5 Story routing (dispatch deltas — original split §9 otherwise stands)

| Story | Owner-area | A1 change |
|---|---|---|
| **S01** | root CMake | Add `CIDX_LIBCLANG` cache var, `find_library`/RPATH/`CIDX_LIBCLANG_PATH` compile-def to `cidx_core`; configure summary line; **fail-fast** on not-found. Update the `clang`-label CTest gating: only fixture/e2e gaps SKIP-77 |
| **S03** | `src/clangx/libclang.{hpp,cpp}` + tests | Rip out dlopen/X-macro/`auto_detect`/`load_library`; convert members to forwarding methods; `load()`→no-op (or remove + drop its §6.1 call); `library_path()` returns `CIDX_LIBCLANG_PATH`; add the runtime ignored-`CIDX_LIBCLANG` WARNING; `major()`/`parse_clang_major()` untouched |
| **S04** | `src/clangx/toolchain.cpp` (`resource_include`) | No logic change — but the `library_path()` source moved (build-time path, not dlopen path); re-verify the `<dirname(library_path())>/clang/*/include` derivation against a linked layout in `toolchain_test` |
| **S08** | `scripts/parity_check.sh`, `scripts/e2e_librdkafka.sh`, env/contract docs | Stop relying on runtime `CIDX_LIBCLANG` for the C++ side; keep it for Python; pin both to the same lib; e2e configures with `-DCIDX_LIBCLANG=/opt/llvm-21.1.1/lib/libclang.so`. Document the §1.3 env-table delta (CIDX_LIBCLANG now build-time + ignored-with-warning at runtime) |

No other stories change. D24 (vendored clang-c headers) remains the build-time type source.

---

## 13. Amendment A2 — C++23 (2026-06-12)

**Status:** accepted (stakeholder override). **Supersedes:** the §2 "Language" + "g++ 8.5 caveats"
rows (marked superseded). **Unchanged:** D1–D26 (as amended by A1), all behavior, all SQL/DDL.

**Decision:** the cidx binary is built as **C++23**. The original C++17 floor over-read the
toolchain constraint: "support g++ 8.5.0 cross toolchains" means cidx must **index code built by
those toolchains** (driver introspection in `toolchain.cpp`), not that cidx itself is compiled by
g++ 8.5. Actual build hosts — macOS AppleClang (dev) and Ubuntu 24.04 gcc 13 (e2e) — both do C++23.

### A2.1 Standard (S01)

- `set(CMAKE_CXX_STANDARD 23)`, `set(CMAKE_CXX_STANDARD_REQUIRED ON)`,
  `set(CMAKE_CXX_EXTENSIONS OFF)`.
- **Minimum compilers:** gcc ≥ 13, clang ≥ 16, AppleClang ≥ 15. Add a configure guard:
  `if (CMAKE_CXX_COMPILER_ID STREQUAL "GNU" AND CMAKE_CXX_COMPILER_VERSION VERSION_LESS 13) ...
  message(FATAL_ERROR ...)` (and the clang/AppleClang equivalents). Both current build hosts pass.
- **g++ 8.5 toolchains remain fully-supported INDEXING TARGETS.** `toolchain.cpp` (driver probe,
  gnuc masquerade, resource-dir substitution, §5.4–5.5) is **unchanged** — it shells out to the
  target driver; the C++ standard cidx is compiled with is independent of the gcc it introspects.
- **Drop the C++17/g++-8.5 build workarounds** (now dead): no conditional `-lstdc++fs` (gcc 13 +
  AppleClang link `<filesystem>` in libc++/libstdc++ proper); the "no `std::expected`" /
  "avoid `<charconv>` float" / "no designated initializers" caveats are void. `std::expected` is now
  available, but D23's exception-based error model **stays** (no rewrite — see A2.2).
- CMake floor in §2 (≥ 3.16 / 3.20+) is fine for C++23 on these toolchains; no bump needed.

### A2.2 Adoption policy — RECOMMENDED: bump-only + idioms-in-new-code + a short targeted list

- **The codebase is a deliberate line-to-line transliteration of the Python tool with byte-frozen
  behavior, already reviewed and QA'd.** Sweeping C++23 rewrites (ranges, `std::expected`,
  `std::print`, views over the SQL plumbing) would churn reviewed code for **zero behavioral gain**
  and reintroduce review/regression risk against the parity gates.
- **Policy:** (a) bump the standard now; (b) **new** code may freely use C++20/23 idioms; (c) apply
  only the cheap, zero-risk, mechanical upgrades listed below; (d) **everything else stays as-is** —
  no ranges/expected/print refactors of existing reviewed code. D23's error model is unchanged.
- **Targeted upgrades (real instances found in the tree, not hypotheticals):**

  | # | Change | Sites (verified) |
  |---|---|---|
  | A2-U1 | Delete the hand-rolled `starts_with`/`ends_with` helpers; use `std::string::starts_with`/`ends_with` (C++20) at the call sites | helper defs `src/clangx/toolchain.cpp:73-80`; callers `toolchain.cpp:196,200,205`. Note `starts_with` also takes `const char*` — drop the helper, the member overload covers it |
  | A2-U2 | Replace `rfind(prefix, 0) == 0` prefix-checks with `.starts_with(...)` | `toolchain.cpp:74` (inside the helper, folds into A2-U1), `toolchain.cpp:558` (`value->rfind("c++",0)==0` → `value->starts_with("c++")`), `storage.cpp:500` (`abs.rfind(root + "/", 0) == 0` → `abs.starts_with(root + "/")`) |
  | A2-U3 | Replace `s.compare(0, strlen(p), p) == 0` prefix-compares with `.starts_with(p)` | `compiledb.cpp:36` (`has_drop_prefix`), `compiledb.cpp:144` (glued-flag check), `args.cpp:474` (`tok.compare(0,2,"--")==0` → `tok.starts_with("--")`) |
  | A2-U4 | Replace `.find(c) != std::string::npos` membership tests with `std::string::contains` (C++23) | `compiledb.cpp:186` (`argv0.find('/') == npos` → `!argv0.contains('/')`), `format.cpp:37-38` (quote-presence → `.contains('\'')` / `.contains('"')`) |

  **Explicitly NOT in scope (leave as-is):** `rfind('/')`/`rfind('.')` that locate a *position*
  (`repo.cpp:83`, `pathutil.cpp:147`, `toolchain.cpp:564-565`) — these are not prefix tests and
  `starts_with` does not apply; the `.compare(size-4,4,".git")` suffix at `repo.cpp:85` (already
  correct, and an `ends_with` swap there is optional churn — skip unless touching the file); all
  `substr(0, ...)` slices that compute real substrings.

- These four are mechanical, behavior-identical, and each is covered by an existing unit test
  (`toolchain_test`, `compiledb_test`, `fuzzy_match_test`/storage, `args`/`format` golden tests) —
  so they ride their own module's green suite, no new tests required.

### A2.3 Risk notes

- **No meaning-changes under C++23 in this tree.** No `operator<=>`/spaceship rewrites,
  no removed/deprecated facilities in use: nothing relies on `std::auto_ptr`, `std::random_shuffle`,
  `throw()` specs, `std::result_of`, or other C++17/20-removed APIs (confirmed by the module list —
  pure `<string>`/`<vector>/<optional>/<filesystem>/<regex>` + C APIs). `std::string::contains`
  (A2-U4) is the only genuinely-C++23 addition; the rest are C++20 and already compile under the
  C++23 floor.
- **doctest compatibility:** vendored `third_party/doctest/doctest.h` is **2.4.12** (current
  release) — compiles clean under `-std=c++23` on gcc 13 and AppleClang/clang 16. No version bump
  needed. (doctest ≥ 2.4.10 is the safe floor for `-std=c++20/23`; we exceed it.)
- **Warning flags (S01):** keep `-Wall -Wextra`; do **not** add `-Wpedantic`-driven C++23 churn.
  No `-std=gnu++23` (extensions off, A2.1).

### A2.4 Story routing

| Story | A2 change |
|---|---|
| **S01** (root CMake) | `CMAKE_CXX_STANDARD 23` + `STANDARD_REQUIRED ON` + `EXTENSIONS OFF`; add the gcc≥13 / clang≥16 / AppleClang≥15 configure guard (FATAL_ERROR below); **remove** the conditional `-lstdc++fs` block and any `<filesystem>` workaround; update the §2 deps row (drop `stdc++fs`); confirm warning flags unchanged |
| **S06** (owns `src/clangx/toolchain.cpp`) | Apply **A2-U1, A2-U2 (toolchain sites)**: delete the local `starts_with`/`ends_with`, swap callers to the `std::string` members |
| **S04** (owns `src/compiledb/compiledb.cpp`) | Apply **A2-U3 (compiledb sites), A2-U4 (`argv0.contains`)** |
| **S02** (owns `src/storage/storage.cpp`) | Apply **A2-U2** at `storage.cpp:500` (`abs.starts_with(root + "/")`) |
| **S09 / S11** (own `src/cli/args.cpp`, `src/cli/format.cpp`) | Apply **A2-U3** at `args.cpp:474`, **A2-U4** at `format.cpp:37-38` |
| **S08** (parity + e2e) | Re-run all gates under C++23; **e2e box build now uses gcc 13** on Ubuntu 24.04 (no toolchain workaround); parity + 93/93-TU target unchanged |

Each targeted upgrade is applied by the story that already owns the file (above); no new story.
If S01 merges before those module stories, the bump alone is behavior-neutral — the U-edits land
incrementally with their owning suites. D1-as-amended-by-A1 (link-time libclang) is unaffected.

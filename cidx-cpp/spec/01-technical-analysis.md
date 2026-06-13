# cidx — Technical Analysis for the C++ Port

Stage 3 (architect) deliverable. Source of truth: the Python implementation at
`libclang-lab/project/` as of commit `5013afe`. All file/line references are into that tree.
A senior developer should be able to design the C++ version from this document alone.

| Artifact | Path | Lines |
|---|---|---|
| Launcher (bash) | `project/cidx` | 75 |
| CLI | `project/indexer/cli.py` | 561 |
| Storage (SQLite) | `project/indexer/storage.py` | 773 |
| Compile DB | `project/indexer/compiledb.py` | 115 |
| Parse layer | `project/indexer/clang/util.py` | 456 |
| AST extraction | `project/indexer/clang/ast.py` | 245 |
| Utils | `project/indexer/utils/{files,hashing,repo}.py` | 69 |
| Storage ground-truth test | `project/indexer/_storage_smoke.py` | 198 |
| Packaging | `libclang-lab/pyproject.toml` | 24 |

Scope note: the capstone brief (`docs/part_7_capstone_project.md`) describes a larger tool
(xrefs, call graph, `query`/`calls`/`stats` commands, multiprocessing pool, atomic DB
replace). **The implemented tool is narrower**: symbol table only — no xref map, no call
graph, no `stats` CLI command (a `Storage.stats()` API exists but is not exposed), no
parallelism, no atomic-replace. The C++ port must target the implemented behavior; brief-only
features are a separate scope decision for the designer (see §9).

---

## 1. Purpose & user-facing behavior

cidx is a C/C++ **semantic symbol indexer**: it registers code bases ("components"), imports
a `compile_commands.json`, parses each TU with libclang, and stores every file-scope
declaration/definition keyed by clang USR into one SQLite database. Query side: fuzzy search,
detail views, and browse/list commands. Designed to work against **gcc cross toolchains**
(e.g. /opt/1A g++ 8.5.0) using libclang 18–21.

### 1.1 Invocation

- Launcher `project/cidx` (bash) → `uv run --project <repo-root> indexer "$@"` when uv
  exists, else `python3 -m indexer` with `PYTHONPATH` set (cidx:71-75).
- Console script `indexer = indexer.cli:main` (pyproject.toml:17).
- Before exec, the launcher auto-detects the **newest** libclang on the system and exports
  `CIDX_LIBCLANG` unless already set (cidx:41-69). Candidates: `llvm-config --libdir`,
  `/opt/llvm*/lib{,64}`, `/usr/lib/llvm-*/lib`, `/usr/local/llvm*/lib`. Version = highest
  numeric entry under `<libdir>/clang/` (the resource-dir layout, cidx:36-39). All candidates
  compete on version — llvm-config gets **no** priority (cidx:42-43). Looks for
  `libclang.so`, `libclang.so.*`, `libclang.dylib` (cidx:26-34). No detection hit → pip
  wheel's bundled library is used.

### 1.2 Commands and flags (cli.py:452-557)

| Command | Args / flags | Behavior | Exit code |
|---|---|---|---|
| `add-source` | `--path` (required), `--name`, `--kind {repo,external}` (default `repo`) | Registers a component. `repo`: walk up to git root (repo.py:7-17), name from `.git/config` remote-origin URL basename, `.git` suffix stripped (repo.py:19-29); `external`: path as-is, name = basename. Prints `component #N: name (kind) at path` | 1 if `--path` not a directory, else 0 |
| `import` | `--db` (required: `compile_commands.json` or its directory), `--name` | Loads compile DB; component root = git root of first source else its dirname (cli.py:131-135); registers the component; one `file` row per command with mtime, md5, stripped options, driver, `indexed=0`. Sources outside any registered component are skipped with a stderr line and counted (cli.py:144-147). All adds inside one transaction. Prints `imported N file(s), skipped M` | 1 on load failure or empty DB, else 0 |
| `index` | `[FILE...]`, `--source COMPONENT` | No args: index every pending file (cli.py:218-231). With args: resolve each FILE (relative → `--source` component root, else CWD; files.py:9-17), error if not in DB. Already-indexed (md5-current) files are skipped. Per file prints `-> N symbols; headers: A indexed (+B symbols), C already, D system, E unowned`. Final line: count of warnings logged to cidx.log if any (cli.py:243-244) | 1 if any file failed/unknown, else 0 |
| `search` | `PATTERN`, `--kind`, `--limit N` (default 25, 0 = all) | Fuzzy match: each `::`-separated segment of PATTERN must appear in order as a substring of `qual_name` — `conf::set` matches `RdKafka::ConfImpl::set` (storage.py:669-686). Table: id, qual name, kind, `def `/`decl`/`pure` mark, `path:line`; definitions also print a second `decl` row when a decl site is stored (cli.py:248-261) | 0 if ≥1 match, else 1 |
| `show symbol` | `SYMBOL` (numeric id or USR) | Key/value dump: id, usr, name, qualified, display, kind, type, visibility (linkage with human gloss, cli.py:371-374), access, parent (qual name + USR), pure, definition loc, declaration loc, resolved (cli.py:348-390). None-valued fields are omitted | 1 if not found |
| `show file` | `FILE` (numeric id or path), `--component/-c` | Dump: id, path, component, directory, mtime (local-time formatted), md5, driver, options (joined; `(none -- header indexed via an including TU)` when NULL), indexed status + reason, indexed-at, symbol counts (total/defined-here/declared-here), by-kind histogram (cli.py:393-449) | 1 if not found |
| `list`/`ls` `components` | `[PATTERN]`, `--kind` | id, name, kind, path; trailing `N component(s)` | 0 if ≥1 row, else 1 |
| `list dirs` | `[PATTERN]`, `--component/-c` | id, component name, rel path (`.` for root) | same |
| `list files` | `[PATTERN]`, `--component/-c`, `--dir/-d` (requires `--component`), `--indexed`⊕`--pending` | id, `idx `/`pend`, abs path | same; 1 also when `--dir` without `--component` |
| `list symbols` | `[PATTERN]`, `--component/-c`, `--dir/-d` (requires `--component`), `--file/-f`, `--kind`, `--limit N` (default 50, 0 = all) | Same table as `search`; PATTERN is a free-text fuzzy match (chars in order) against the qualified name | same |

- `PATTERN` for `list` is **fzf-style** (every non-space char in order: `shp` matches
  `shapes.c`, storage.py:336-345); `search`'s pattern is **segment-style** (`::`-split).
  These are two distinct match algorithms — keep both.
- argparse misuse (unknown flag, missing required arg) exits **2**; everything else 0/1 as
  above.
- There is **no** `--index`/`--jobs` flag anywhere: DB path is always derived from the cache
  dir (cli.py:554-555).

### 1.3 Environment variables

| Var | Where | Meaning |
|---|---|---|
| `INDEXER_CACHE` | cli.py:48-56 | Cache dir for ALL generated files (default `~/.cache/cidx`): `index.db` + `cidx.log`; never the CWD |
| `CIDX_LIBCLANG` | util.py:44,48-49; launcher | Path to libclang shared library; applied via `Config.set_library_file` at import time, before first parse. Launcher auto-populates it |
| `CIDX_RESOURCE_DIR` | util.py:45,87-90 | Matching clang resource dir; `<dir>/include` must contain `stddef.h`. Auto-derived from `CIDX_LIBCLANG` when unset |
| `CIDX_GNUC_VERSION` | util.py:46,250-254 | Overrides the `-fgnuc-version=` value; `0/off/none/false` disables the flag entirely. An explicit value also disables the libclang<21 cap |
| `CIDX_STRICT` | util.py:361,388-402 | Default off → abort only on FATAL diagnostics; `1` → abort on ERROR too |
| `INDEXER_IGNORE_SYSTEM_HEADERS` | ast.py:22,171-174 | Default true; `0/false/no/off` → index system headers too |
| `CIDX_PYTHON` | launcher only | Interpreter for the no-uv fallback (irrelevant to the port) |

### 1.4 Logging (cli.py:67-99, util.py:36-42)

- Logger hierarchy `cidx` / `cidx.clang`; CLI attaches a `FileHandler` on
  `$INDEXER_CACHE/cidx.log`, format `%(asctime)s %(levelname)s %(name)s: %(message)s`,
  level INFO, **delay=True** (read-only subcommands never create an empty log file).
- A filter counts records at ≥ WARNING (cli.py:67-81); `cmd_index` prints
  `N warning(s)/error(s) logged to <path>` at the end (cli.py:243-244).
- Per-file parse summaries log at WARNING/ERROR (one per file → counter stays
  one-per-file); individual diagnostics log at INFO, capped at **25 per file** with a
  `... N more diagnostic(s) suppressed` line (util.py:364-377).
- Library use without a handler: Python's last-resort handler prints to stderr — a C++
  equivalent should default to stderr when no log sink is configured.

---

## 2. Architecture

### 2.1 Module map

| Module | Responsibility |
|---|---|
| `cidx` (bash) | env bootstrap: libclang auto-detect, interpreter selection |
| `indexer.cli` | argparse tree, command handlers, cache-dir/log policy, output formatting |
| `indexer.storage` | SQLite schema v6, migrations, all queries, upsert semantics, fuzzy LIKE |
| `indexer.compiledb` | compile_commands.json load, arg stripping/sanitizing, driver capture |
| `indexer.clang.util` | libclang config, toolchain/driver introspection, parse + diagnostic policy |
| `indexer.clang.ast` | cursor walk, Symbol mapping, main-file + header indexing |
| `indexer.utils.hashing` | `md5_of` content hash |
| `indexer.utils.repo` | `git_root` walk-up, `repo_name` from `.git/config` |
| `indexer.utils.files` | `resolve_file_arg`, `index_status` |

### 2.2 Data flow

```mermaid
flowchart LR
    A[cidx import --db] --> B[compiledb.load_commands\nCompilationDatabase.fromDirectory]
    B --> C[strip_for_libclang + driver + md5 + mtime]
    C --> D[(file rows, indexed=0)]
    E[cidx index] --> F[for each pending file:\nsanitize stored options]
    F --> G[util.parse\nargs + toolchain_flags(driver) + -ferror-limit=0]
    G --> H[ast.index_symbols\nmain-file cursors -> Symbol upserts]
    G --> I[ast.index_headers\ntu.get_includes -> per-header file rows + symbols]
    H --> J[(symbol table, USR-keyed)]
    I --> J
    K[search / show / list] --> J
```

- `import` does no parsing; `index` does no compile-DB reading (it consumes the stored,
  already-stripped `compile_options` and re-`sanitize()`s them, cli.py:186).
- One TU at a time; the TU is freed immediately after extraction
  (`del tu` in a `finally`, ast.py:243-244) — peak memory is one AST.
- Each parse creates a fresh `Index` (util.py:427).

---

## 3. SQLite schema v6 (storage.py:32-125)

`SCHEMA_VERSION = 6`. Connection: `PRAGMA foreign_keys = ON` both at connect (storage.py:206)
and in the schema script (storage.py:57). `row_factory = sqlite3.Row`. No other pragmas (no
WAL, no synchronous tuning — a deliberate-simplicity point the designer may revisit).
DB directory is created on open (storage.py:202-203).

### 3.1 Tables

**meta** — `key TEXT PRIMARY KEY, value TEXT`. One row written:
`INSERT OR IGNORE ... ('schema_version', '6')` (storage.py:124).

**component** — one code base.

| Column | Type | Meaning |
|---|---|---|
| id | INTEGER PK | |
| name | TEXT NOT NULL | repo/library name (not unique) |
| path | TEXT NOT NULL UNIQUE | abs repo root (where `.git` lives) or external header root |
| kind | TEXT NOT NULL DEFAULT 'repo' CHECK in ('repo','external') | |

**directory** — paths relative to the component root.

| Column | Type | Meaning |
|---|---|---|
| id | INTEGER PK | |
| component_id | INTEGER NOT NULL REFERENCES component ON DELETE CASCADE | |
| path | TEXT NOT NULL | relative; `''` = root; UNIQUE(component_id, path) |

**file**

| Column | Type | Meaning |
|---|---|---|
| id | INTEGER PK | |
| directory_id | INTEGER NOT NULL REFERENCES directory ON DELETE CASCADE | |
| name | TEXT NOT NULL | basename; UNIQUE(directory_id, name) |
| mtime | REAL | source mtime at import time; refreshed by `mark_file_indexed` |
| md5 | TEXT | content hash at import time — the staleness key |
| compile_options | TEXT | **JSON array** of stripped parse args; NULL for headers indexed via an including TU |
| driver | TEXT | compile-command argv[0] (v6 addition); NULL for headers |
| indexed | INTEGER NOT NULL DEFAULT 0 | |
| indexed_at | TEXT | `datetime('now')` — UTC ISO, set by `mark_file_indexed` |

**symbol** — USR-keyed, one row per distinct symbol program-wide.

| Column | Type | Meaning |
|---|---|---|
| id | INTEGER PK | |
| usr | TEXT NOT NULL UNIQUE | clang USR — the cross-TU identity |
| spelling | TEXT NOT NULL | bare name (may be `''` for anonymous entities) |
| qual_name | TEXT | `ns::Class::name` from semantic parents |
| display_name | TEXT | spelling + signature, e.g. `multiply(int, int)` |
| kind | TEXT NOT NULL CHECK (kind IN …17 kinds…) | see §3.2 |
| type_info | TEXT | `cursor.type.spelling` |
| file_id / line / col | INTEGER, FK file ON DELETE SET NULL | definition site once seen, else declaration site |
| decl_file_id / decl_line / decl_col | INTEGER, FK file ON DELETE SET NULL | declaration site (e.g. the .h prototype); NULL if none seen |
| is_definition | INTEGER NOT NULL DEFAULT 0 | |
| is_pure | INTEGER NOT NULL DEFAULT 0 | pure virtual — no definition can ever exist |
| linkage | TEXT | `external` / `internal` / `no-linkage` / … (clang LinkageKind, lowercased, `_`→`-`; INVALID → NULL, ast.py:77-79) |
| access | TEXT | `public` / `protected` / `private` (C++ only) |
| parent_usr | TEXT | semantic parent (class/namespace) USR; NULL at TU scope |
| resolved | INTEGER NOT NULL DEFAULT 0 | definition seen somewhere |

Indexes: `idx_symbol_spelling(spelling)`, `idx_symbol_qual(qual_name)`,
`idx_symbol_file(file_id)`, `idx_symbol_parent(parent_usr)`, `idx_symbol_kind(kind)`
(storage.py:118-122).

Absolute paths are **never stored** for files: recovered as
`component.path / directory.path / file.name` (storage.py:557-568) — moving a repo means
updating one component row.

### 3.2 Symbol kinds (storage.py:36-54)

`class, struct, union, function, method, member, constructor, destructor, enum,
enum-constant, typedef, type-alias, class-template, function-template, variable, namespace,
macro` — enforced both by SQL CHECK and by a Python-side ValueError
(storage.py:596-597, 635-636).

### 3.3 Migration / versioning (storage.py:212-271)

- `_migrate()` runs **before** `executescript(_SCHEMA)` because the schema's indexes
  reference columns the migration adds (storage.py:207).
- Detection is **by column presence**, not by the stored version number; the meta value is
  updated to `6` after any change (storage.py:266-271). Fresh DB (no `symbol` table) → skip,
  let `_SCHEMA` create everything.
- v2→v3: `ADD COLUMN symbol.qual_name`; backfilled by a recursive CTE walking stored
  `parent_usr` chains — longest chain per symbol wins (storage.py:229-244).
- v3→v4: `ADD COLUMN decl_file_id/decl_line/decl_col`; declaration-only rows
  (`is_definition = 0`) copy their location into the decl columns (storage.py:246-255).
- v4→v5: `ADD COLUMN is_pure DEFAULT 0`; no backfill possible — reindex to populate.
- v5→v6: `ADD COLUMN file.driver`; no backfill — re-`import` to populate.
- There is **no downgrade** and no refusal to open newer DBs (a designer decision point).

### 3.4 Write semantics (must be reproduced exactly)

- **component upsert** on path: `ON CONFLICT(path) DO UPDATE SET name, kind ... RETURNING id`
  (storage.py:294-305).
- **directory upsert** on (component_id, path); path normalized, `.`/`''` → `''`
  (storage.py:365-378).
- **file upsert** on (directory_id, name) (storage.py:426-453):
  - `mtime/compile_options/driver`: `COALESCE(excluded.X, file.X)` — NULL never clobbers.
  - `indexed`: reset to 0 only when `excluded.md5 IS NOT NULL AND excluded.md5 IS NOT
    file.md5` — note **`IS NOT`** (NULL-safe inequality); a content change invalidates the
    stored symbols' freshness flag.
  - `md5`: COALESCE.
- **symbol upsert** on usr (storage.py:590-628):
  - `spelling`, `kind`: always overwritten by the new row.
  - `qual_name, display_name, type_info, linkage, access, parent_usr, decl_*`: COALESCE
    (NULL never clobbers a stored value).
  - `file_id/line/col`: taken from excluded only when
    `excluded.is_definition >= symbol.is_definition` — a definition always wins over a
    stored declaration; a later declaration never downgrades a definition's location.
  - `is_definition, is_pure, resolved`: `MAX(excluded, stored)` — sticky once true.
- **mark_file_indexed**: `indexed = 1, indexed_at = datetime('now'), mtime = COALESCE(?, mtime)`
  (storage.py:533-539).
- **Transactions**: every public mutator commits unless inside `with db.transaction():`
  (flag `_in_txn`); the context manager commits on success, rolls back on exception
  (storage.py:284-290, 759-773). `import` and per-file symbol batches use it — row-at-a-time
  autocommit is the documented 100× slowdown.

### 3.5 Read/query semantics

- `component_for_path`: **longest-prefix** match over all component rows, computed in
  application code, comparing `abs_path == root or abs_path.startswith(root + os.sep)`
  (storage.py:325-334). Nested components resolve to the deeper root.
- `_fuzzy_like(text)`: builds `%c%c%c%` from non-space chars, escaping `\ % _`, used with
  `LIKE ... ESCAPE '\'` — ASCII case-insensitive (storage.py:336-345).
- `search_symbols`: `'::'`-split pattern → `%seg%seg%` LIKE on `qual_name` (each segment's
  `%`/`_` escaped); ordered by `LENGTH(qual_name), qual_name` — shortest match first
  (storage.py:669-686).
- `list_symbols`: location scope (component / dir subtree / file) matches if **either** the
  definition site or the declaration site falls inside — listing a header shows prototypes
  whose definitions live in a .c file. Name filter is fuzzy on
  `COALESCE(qual_name, spelling)`; same length-first ordering (storage.py:688-729).
- `_dir_scope_sql`: a directory and its whole subtree — `d.path = ? OR d.path LIKE 'rel/%'`;
  `''` (root) subtree is `%` = everything (storage.py:413-422).
- `is_file_indexed(path, mtime=None, md5=None)`: indexed flag AND (if given) stored mtime
  not older AND md5 equal (storage.py:541-555). The CLI path uses
  `index_status()` instead, which is **md5-only**: not indexed / no stored md5 / md5
  mismatch / ok (files.py:20-28).
- `stats()` exists (counts by table + by kind + unresolved; storage.py:743-756) but has no
  CLI command.

---

## 4. Caching & incrementality

- **Cache directory** = `$INDEXER_CACHE` else `~/.cache/cidx` (cli.py:48-56). Contents
  today: `index.db`, `cidx.log`. Nothing else is written anywhere (the CLI docstring
  reserves the dir for "later PCH/cache artifacts" — none exist yet). No per-project DBs;
  one global index unless the user switches `INDEXER_CACHE`.
- **Staleness key = md5 of file content**, computed by reading the whole file
  (hashing.py:6-12; returns None on unreadable). Captured at `import` for sources and at
  `index_headers` time for headers.
- **Decision to (re)parse** (`index_status`, files.py:20-28): skip iff `file.indexed == 1`
  AND `file.md5` is non-NULL AND equals the file's *current* md5. mtime is stored
  (import + mark-indexed time) but **not consulted** by the CLI skip path — it is metadata /
  available via `is_file_indexed(mtime=...)` for library callers.
- **Invalidation on re-import**: the file upsert clears `indexed` when the imported md5
  differs from the stored one (§3.4). Compile-command changes alone (same content, new
  flags) do **not** invalidate — options/driver are COALESCE-updated but `indexed` stays 1.
  There is no compile-command fingerprint. Flag for the designer.
- **Header incrementality**: headers become their own `file` rows the first time an
  including TU is indexed (md5 + mtime captured then, `compile_options`/`driver` NULL);
  subsequent TUs including the same header skip it via the same md5 check
  (ast.py:213-225). A changed header is re-extracted from whichever TU is indexed next.
- **No reverse dependency tracking**: a changed header does NOT invalidate the `.c` files
  that include it (their own md5 is unchanged). Re-indexing a TU whose header changed only
  happens if the TU itself changed or `indexed` was cleared. Known limitation — carry it or
  fix it consciously in the port.
- Symbols are never deleted: removing a symbol from a file leaves the stale row (no
  per-file purge before reindex). Another conscious decision point.

---

## 5. libclang usage

### 5.1 Bindings & library resolution

- Python deps are platform-split (pyproject.toml:11-14): macOS = `libclang>=18.1.1` wheel
  (bundled dylib, newest is 18.1.1); non-darwin = official `clang>=21.1.7` bindings, **no
  bundled library** → `CIDX_LIBCLANG` mandatory there. The C++ port links the libclang C
  API directly, so this entire axis collapses — but the port must still support **both
  libclang 18 and 21** runtime behavior (the `_libclang_major()`-gated gnuc cap, §5.4).
- `CIDX_LIBCLANG` is applied at module import, only if config not already loaded
  (util.py:48-49).
- `_libclang_major()` (util.py:182-199): calls `clang_getClangVersion()` and regexes
  `version (\d+)`; returns 0 when undeterminable. In C++ this is a direct C-API call — the
  Python `_CXString` lifetime / `c_interop_string` gotchas disappear, but keep the regex
  and the 0 fallback.

### 5.2 Parse pipeline (`parse()`, util.py:405-456)

Final arg vector: `stored_args + toolchain_flags(cpp, driver) + ["-ferror-limit=0"]`.

- `options = 0` — **no** `DETAILED_PREPROCESSING_RECORD`, no skip-bodies, nothing. (See
  gotcha G22: the `macro` kind can therefore never be produced today.)
- Fresh `Index.create()` per parse.
- `TranslationUnitLoadError` → `ClangParseError("cannot parse <file>")`.
- `-ferror-limit=0` lifts clang's default 20-error cap; hitting the cap emits a FATAL
  `too many errors emitted, stopping now` that aborts an otherwise-indexable TU while
  naming none of the real errors (util.py:424-426; commit `5013afe`).

### 5.3 Diagnostic policy (util.py:361-456)

- Abort level (`_abort_level`): default = `Fatal`; `CIDX_STRICT=1` → `Error`.
  Rationale: FATAL = unrecoverable environment problems (header not found) that truncate
  the AST → parse must be rejected. ERROR = semantic disagreements (clang stricter than the
  gcc the code targets) with intact surrounding AST → tolerated by default.
- On fatal: log the **full flag dump + libclang major** at ERROR (kept off the terminal),
  log up to 25 individual diagnostics (INFO, format `<TU>: diag <file>:<line>: <msg>`),
  raise `ClangParseError("<file>: N fatal diagnostic(s): <first 3, ';'-joined>")`.
- On tolerated errors (non-strict): one WARNING summary
  `<file>: N error diagnostic(s) ignored (CIDX_STRICT=1 to abort)` + the same capped INFO
  per-diag lines. Summary at WARNING keeps the CLI counter one-per-file (util.py:368-377).

### 5.4 Toolchain / resource-dir resolution

`toolchain_flags(cpp, driver)` (util.py:334-358) — appended to every parse:

1. **Driver path** (when `file.driver` is set and answers): `driver_flags(driver, cpp)`
   (util.py:280-319) replicates the driver's `#include <...>` system search list:
   - Query: `<driver> -E -x c|c++ - -v` with empty stdin, 30 s timeout; parse stderr
     between `#include <...> search starts here` and `End of search list`; skip macOS
     `(framework directory)` lines; keep only existing dirs, normpathed, in driver order
     (util.py:130-159). Memoized per (driver, lang).
   - Emit `-nostdinc` + gnuc flags (§5.5) + each dir as `-isystem` **in driver order**
     (which is the C++-correct order: libstdc++ → compiler builtins → libc).
   - **Builtin-dir substitution**: any dir matching
     `[/\\]lib(32|64)?[/\\](gcc|gcc-cross|clang)[/\\]` (gcc's
     `lib/gcc/<triple>/<ver>/include` *and* `include-fixed`, or another clang's
     `lib/clang/<ver>/include`) is dropped and replaced — once, at the first occurrence's
     position — by **this libclang's** resource include (util.py:277, 308-319). If never
     substituted, the resource include is appended last.
   - **include-fixed gotcha** (the reason for substitution, util.py:269-277): gcc's
     `include-fixed/limits.h` keys on `_GCC_LIMITS_H_`, which clang's own limits.h defines
     before `#include_next` — feeding include-fixed to libclang severs the chain to glibc's
     limits.h (symptom: librdkafka `IOV_MAX not defined`). gcc's intrinsics headers also use
     gcc-only builtins. Never pass gcc's include/include-fixed to libclang.
   - **No resource headers anywhere** → WARNING logged, search list replicated verbatim
     including the driver's builtin dirs (gcc's stddef.h parses fine; better than fatal,
     util.py:294-307).
   - Driver query fails/empty → fall through to host defaults.
2. **Host defaults** (no driver / driver mute): macOS: `-isysroot $(xcrun --show-sdk-path)`
   (+ `-isystem <sdk>/usr/include/c++/v1` for C++); then `-isystem <resource include>`.
   **Order is load-bearing**: sysroot → libc++ → clang builtins; builtins first breaks
   `<cstddef>`'s include_next chain — a fatal that silently truncates the AST
   (util.py:338-344). Non-darwin host default: just the resource include.

`_resource_include()` search order (util.py:74-126), first dir containing `stddef.h` wins:
1. `$CIDX_RESOURCE_DIR/include`
2. `<dirname(CIDX_LIBCLANG)>/clang/*/include`, highest version first
3. `clang`/`clang++` on PATH: `-print-resource-dir` + `/include`
4. Glob fallbacks, best numeric version across all:
   `/opt/llvm*/lib*/clang/*/include`, `/usr/lib/llvm-*/lib/clang/*/include`,
   `/usr/local/llvm*/lib/clang/*/include`, `/usr/lib*/clang/*/include`

Language detection `is_cpp(filename, args)` (util.py:322-331): `--driver-mode=g++` or
`-xc++` in args, or `-x` followed by a `c++*` value; else extension in
`.cpp .cc .cxx .c++ .hpp .hh .hxx` (lowercased).

### 5.5 GNUC masquerade (`_gnuc_version_flag`, util.py:233-266)

clang claims `__GNUC__ == 4.2` by default; `#if __GNUC__ >= N` guards then hide code the
real g++ compiles (observed: gated class definition vanished → "invalid application of
'sizeof' to an incomplete type" in unique_ptr.h).

- Driver recognized as gcc via basename regex `(^|-)(gcc|g\+\+)(-[\d.]+)?$` (util.py:162);
  version from `-dumpfullversion` then `-dumpversion`, must match `\d+(\.\d+)*`
  (util.py:165-179). Non-gcc driver and no env override → no flag.
- Emit `-fgnuc-version=<ver>`, with two probed glibc landmines (`_glibc_probe`,
  util.py:202-221 — scans the driver's search dirs):
  - **malloc(deallocator) attributes**: glibc ≥ 2.34 (detect: `sys/cdefs.h` defines
    `__attr_dealloc`) emits `__attribute__((malloc(deallocator)))` once the compiler claims
    gcc ≥ 11; only libclang ≥ 21 parses that. If claimed major ≥ 11 ∧ probe positive ∧
    `_libclang_major() < 21` → cap the claimed version to **"10.9"** (skipped when the env
    var set the version explicitly).
  - **_FloatN**: gcc-only keywords clang doesn't implement. Aliased away via
    `-D_Float32=float -D_Float64=double -D_Float128=long double -D_Float32x=double
    -D_Float64x=long double` (util.py:227-231) when: C and claimed major ≥ 7 (always), or
    C++ and claimed major ≥ 13 **and** `bits/floatn-common.h` shows the keyword path
    (`__GNUC_PREREQ (13` regex — glibc ≥ 2.38). On older glibc C++ typedefs them, where the
    `-D` aliases would mangle the typedefs — so NOT added there.
- `CIDX_GNUC_VERSION` overrides the derived version (cap bypassed); `0/off/none/false`
  disables entirely.
- All driver probes are memoized (`lru_cache`) — once per (driver[,lang]) per process.

### 5.6 Cursor walking & extraction (ast.py)

- **USR is the symbol identity** (`cursor.get_usr()`); cursors without a USR are skipped
  (ast.py:99-101).
- `_KIND_MAP` (ast.py:25-43): exactly 17 CursorKinds → storage kinds (CLASS_DECL,
  STRUCT_DECL, UNION_DECL, FUNCTION_DECL, CXX_METHOD, FIELD_DECL, CONSTRUCTOR, DESTRUCTOR,
  ENUM_DECL, ENUM_CONSTANT_DECL, TYPEDEF_DECL, TYPE_ALIAS_DECL, CLASS_TEMPLATE,
  FUNCTION_TEMPLATE, VAR_DECL, NAMESPACE, MACRO_DEFINITION). Anything else is ignored.
- `_file_cursors(tu, filename)` (ast.py:62-74): pre-order walk; a child whose
  `location.file` is None or ≠ filename has its **entire subtree pruned**; function-like
  cursors (FUNCTION_DECL, CXX_METHOD, CONSTRUCTOR, DESTRUCTOR, FUNCTION_TEMPLATE) are
  yielded but their **bodies are not walked** (no locals/statement-scoped types).
- Per-cursor extraction (`_to_symbol`, ast.py:94-130): spelling, qualified name from
  **semantic** parents (anonymous levels with empty spelling skipped; out-of-line methods
  qualify by class, not file scope; ast.py:82-91), displayname, kind, `type.spelling`,
  location (line/col), `is_definition()`, `is_pure_virtual_method()`, linkage (lowercased,
  `_`→`-`, INVALID→NULL), access specifier (map PUBLIC/PROTECTED/PRIVATE), parent USR
  (semantic parent unless TU), `resolved = is_definition`. Declaration cursors record their
  own site as the decl site; definition cursors leave decl_* NULL (the upsert preserves a
  previously stored decl site).
- **Store policy** (`_index_file`, ast.py:133-160), inside one transaction per file:
  - existing row resolved → skip (counted), but if this cursor carries a decl site and the
    stored row has none, patch decl_file_id/line/col via `update_symbol`.
  - else upsert.
- **Main file** = `tu.spelling` (the filename as passed to parse) (ast.py:163-168).
- **Headers** (`index_headers`, ast.py:183-226): iterate `tu.get_includes()` —
  **transitive**, so nested headers are reached in one pass; dedupe by `os.path.abspath`.
  Skip categories (counted separately): `system` (via
  `SourceLocation.from_position(tu, file, 1, 1).is_in_system_header`, default-on),
  `unowned` (no registered component owns the path), `already` (file row indexed with
  matching md5). Otherwise: create the file row (mtime + md5, **no options/driver**),
  extract that header's symbols **out of this TU's AST** (no separate parse), matching
  cursors against `inc.include.name` (libclang's spelling, *not* the abspath), mark
  indexed. Returns `{indexed, symbols, already, system, unowned}`.
- No xref records, no call-graph edges, no token-level data are extracted.

---

## 6. compile_commands.json handling (compiledb.py)

- **Load**: `CompilationDatabase.fromDirectory(abs_dir)`; `--db` accepts the json path
  (suffix `compile_commands.json` stripped) or the directory (compiledb.py:15-20). The C++
  port can use clang's `CXCompilationDatabase` or parse the JSON directly (ADR territory:
  direct JSON parsing avoids quoting differences in `command` vs `arguments` entries that
  libclang already normalizes).
- **strip_for_libclang(cmd)** (compiledb.py:70-98), applied at import; result stored as the
  JSON `compile_options`:
  - drop argv[0] (the driver token);
  - drop bare tokens in `_DROP`: `-c`, `--`, `-M -MM -MD -MMD -MG -MP -MV`, `-Werror`,
    `-pedantic-errors`;
  - drop flag+argument pairs in `_DROP_WITH_ARG`: `-o`, `-MF`, `-MT`, `-MQ`,
    `-dependency-file`, `--serialize-diagnostics`;
  - drop prefixed tokens in `_DROP_PREFIX`: `-Werror=…`, `-Wp,-M…`, glued `-MF…/-MT…/-MQ…`;
  - drop the source file, matched as the command's filename **or its basename**
    (compiledb.py:73, 84-85);
  - absolutize `-I`, `-isystem`, `-iquote` — both the space form (`-I path`) and the glued
    form (`-Ipath`) — against `cmd.directory` (compiledb.py:86-97; `_abs` normpaths).
  - Rationale baked into comments (compiledb.py:27-37): `-M*` writes build artifacts into
    dirs that don't exist outside a real build → fatal `error opening '…'`; `-Werror`
    promotes warnings gcc never emitted into clang error diagnostics.
- **sanitize(args)** (compiledb.py:48-67): re-applies only the drop rules (no path fixing)
  to **already-stored** options at index time — heals databases imported by an older cidx
  whose drop list was shorter, without re-import. Called on every index (cli.py:186).
- **driver(cmd)** (compiledb.py:106-115): argv[0]; absolutized against `cmd.directory` when
  it contains a path separator, else kept bare (`cc`, `g++`) for PATH resolution at parse
  time.
- **Header flags**: there is **no header-flag inference** (no "find a TU that includes this
  header and borrow its flags" — the part_8 doc technique). Headers are only ever indexed
  through an including TU's AST; standalone header parsing does not exist.

---

## 7. Concurrency & performance

- **Strictly sequential, single-process, single-threaded.** No pool, no threads, no jobs
  flag. (The capstone brief's §6.5 multiprocessing design was never implemented.)
- Performance levers that DO exist:
  - one fresh `Index` + TU per file, freed immediately after extraction (ast.py:229-245) —
    bounded memory;
  - per-file symbol writes batched in one transaction; import batched in one transaction
    (cli.py:141, ast.py:142);
  - header symbols harvested from the already-parsed including TU — headers are never
    parsed standalone;
  - md5 skip prevents reparsing unchanged files and re-extracting shared headers across
    TUs;
  - memoized driver introspection (subprocess once per driver/lang);
  - `lookup_symbol` before every insert (ast.py:147) — a read-per-cursor; the upsert could
    do this server-side, the pre-check exists to implement the "resolved rows untouched +
    decl-site patch" policy.
- Known costs: `component_for_path` is a full-table scan per header (ast.py:213); md5 reads
  whole files; `del`-after-use means no TU reuse across reparse. All acceptable at current
  scale (validated: librdkafka, 93/93 TUs); the C++ designer owns the parallelism decision
  (per-TU workers writing to a single SQLite connection requires serialization — see the
  capstone's "extract data, not cursors" guidance).

---

## 8. Edge cases & gotchas (port-loss hazards)

Toolchain / parse:
- **G1** pip-wheel libclang ships no builtin headers → bare parse dies with fatal
  `stddef.h not found` that silently truncates the AST (util.py:3-8). The whole
  resource-dir machinery exists for this.
- **G2** C++ host include order must be sysroot → libc++ → clang builtins; builtins first
  breaks `<cstddef>` include_next (util.py:338-344).
- **G3** Never feed gcc's `include`/`include-fixed` (or a foreign clang resource dir) to
  libclang; substitute with own resource dir at first-occurrence position
  (`_BUILTIN_DIR_RE`, util.py:269-319). include-fixed/limits.h `_GCC_LIMITS_H_` guard
  severs the libc limits.h chain.
- **G4** `-fgnuc-version=` masquerade with the glibc malloc-attr cap (10.9 when
  libclang < 21) and the C/C++-asymmetric `_FloatN` alias rules (§5.5). Capping skipped
  when `CIDX_GNUC_VERSION` set explicitly.
- **G5** `-ferror-limit=0` always appended (20-error cap → information-free FATAL).
- **G6** Tolerant-by-default diagnostics: abort on FATAL only; `CIDX_STRICT=1` for ERROR.
- **G7** Resource-dir-missing fallback: replicate the driver list verbatim (incl. gcc
  builtin dirs) + WARNING, instead of failing every parse (util.py:294-307).
- **G8** Driver probe: 30 s timeout, empty-stdin `-E -x <lang> - -v`, stderr parsing,
  framework-dir filtering, only existing dirs, memoized; bare driver names resolved via
  PATH at parse time, not import time.
- **G9** `is_cpp` checks args before extension (`--driver-mode=g++`, `-xc++`, `-x c++…`).

Compile-DB:
- **G10** Drop sets exactly as in §6 — incl. `--`, `-Wp,-M…`, glued `-MF<file>`,
  `--serialize-diagnostics`, and source matched by basename too.
- **G11** `sanitize()` re-applied at index time to heal stale DBs (no re-import needed).
- **G12** Relative `-I/-isystem/-iquote` absolutized against the command's `directory`
  (both spellings); other relative-path flags are left untouched.

Storage:
- **G13** File upsert's NULL-safe `IS NOT` md5 comparison drives indexed-flag reset.
- **G14** Symbol upsert: definition-wins location, COALESCE non-clobber, MAX sticky flags,
  spelling/kind always overwritten (smoke-asserted, _storage_smoke.py:64-117).
- **G15** Resolved rows are skipped on re-encounter except for decl-site patching
  (ast.py:147-157).
- **G16** `component_for_path` = longest-prefix wins (nested components).
- **G17** Directory `''` = root; root subtree LIKE = `%`; `.` normalized to `''`.
- **G18** Two distinct fuzzy algorithms: char-in-order (`list`) vs `::`-segment
  (`search`); both LIKE-escaped, ASCII case-insensitive; results ordered length-first.
- **G19** Migration ordering: column adds before schema script; detection by column
  presence; qual_name backfill via recursive CTE.
- **G20** Symbols/headers: stale rows are never deleted; header rows have NULL
  options/driver (and `show file` prints the explanatory placeholder).

Extraction:
- **G21** Subtree pruning on file mismatch + function bodies not walked; macro-expansion
  artifacts from other files vanish with their subtree.
- **G22** `macro` kind is mapped but **unreachable**: `parse()` passes `options=0`, so no
  `DETAILED_PREPROCESSING_RECORD`, so MACRO_DEFINITION cursors never appear. Port decision:
  reproduce (dead kind) or fix (enable the option) — flag, don't silently change.
- **G23** Header cursor matching uses `inc.include.name` (libclang spelling) while dedupe/
  storage use `os.path.abspath` — these can differ (symlinks, relative spellings); the
  mismatch is deliberate (cursors' `location.file.name` agrees with the include spelling).
- **G24** Main-file matching uses `tu.spelling` == the path exactly as passed to parse;
  the CLI always passes the reconstructed absolute path.
- **G25** Anonymous entities (empty spelling) are indexed; qual_name skips empty levels.
- **G26** System-header test is per-TU via `SourceLocation.from_position(...,1,1)
  .is_in_system_header`, honoring `-isystem`/sysroot of *this* parse.

CLI / logging:
- **G27** Log file is lazily created (delay=True); warning counter counts file-handler
  records ≥ WARNING; per-diag lines are INFO (capped 25/file) precisely so the counter
  stays one-per-file.
- **G28** Fatal-parse flag dump goes to the log, never the terminal; terminal gets
  `<file>: N fatal diagnostic(s): <first 3>`.
- **G29** Exit codes: search/list = 1 on zero matches; index = 1 if any file failed;
  argparse usage errors = 2.
- **G30** `import` records `mtime=None` for missing files and still imports the row;
  `md5_of` returns NULL on unreadable files → `index_status` then reports "no stored md5"
  and the file is treated as never-indexed.
- **G31** `show file` formats mtime in **local time**, `indexed_at` is stored/displayed as
  UTC with a ` UTC` suffix (cli.py:421-424, 439).

---

## 9. Port checklist

### 9.1 Behaviors the C++ version MUST reproduce

1. CLI surface of §1.2 verbatim: commands, flags, defaults (limits 25/50), output line
   formats, exit codes (incl. usage = 2), the `--dir requires --component` rule, the
   mutually-exclusive `--indexed/--pending` pair, `ls` alias.
2. Env-var contract of §1.3 with the same defaults and the same falsy spellings
   (`0/off/none/false`, `0/false/no/off`).
3. SQLite schema v6 byte-compatible (table/column names, CHECKs, FKs + ON DELETE actions,
   indexes, meta row) — an existing `index.db` written by the Python tool must open and
   migrate identically (column-presence migrations of §3.3 included).
4. All upsert semantics of §3.4 (the smoke test `_storage_smoke.py` is an executable spec —
   port it as the first C++ test suite).
5. md5-based incrementality + indexed-flag reset rules of §4, including header-via-TU
   indexing and skip counters `{indexed, symbols, already, system, unowned}`.
6. Arg stripping/sanitizing/driver capture of §6 (exact drop sets).
7. Toolchain resolution of §5.4–5.5: driver introspection, builtin-dir substitution,
   gnuc masquerade with both glibc probes and the libclang<21 cap, FloatN aliases, host
   fallbacks, resource-dir search order, the C++ include order.
8. Parse policy of §5.2–5.3: `-ferror-limit=0`, fatal-only abort default, CIDX_STRICT,
   log formats (`<TU>: diag file:line: msg` is grep-documented in memory; cap 25),
   warning-count summary line.
9. Extraction policy of §5.6: the 17-kind map, USR identity, semantic-parent
   qualification, body pruning, subtree pruning, decl/def split, resolved-skip +
   decl-patch, pure-virtual handling, linkage/access spellings.
10. Both fuzzy-match algorithms + ordering (length-first).
11. Validation target: librdkafka under a gcc-8.5 cross toolchain × libclang 21.1.1 must
    index 93/93 TUs (see memory note `cidx-toolchain-support`; test box 192.168.1.115).

### 9.2 Python-isms needing a C++ equivalent decision (designer's ADRs)

| # | Python mechanism | Decision needed |
|---|---|---|
| P1 | bash launcher's libclang auto-detect + `CIDX_LIBCLANG` | C++ links libclang at build time — decide: dlopen-style runtime selection (preserves the "newest wins" UX, supports 18 vs 21 on one binary) vs per-toolchain builds. Affects G4's `_libclang_major()` runtime check |
| P2 | `uv run` / pip platform-split deps | disappears; packaging = single static-ish binary; decide build system + min libclang version |
| P3 | stdlib `sqlite3` + `sqlite3.Row` | embed SQLite C API directly or a thin wrapper; keep autocommit-unless-in-txn semantics |
| P4 | `hashlib.md5` | need an MD5 impl (OpenSSL? vendored?) — changing the algorithm breaks every existing DB's staleness data; keep md5 hex format |
| P5 | `json.dumps/loads` for `compile_options` column | pick a JSON lib; array layout must round-trip with Python-written rows (compact vs spaced separators is read-compatible, write format free) |
| P6 | argparse (auto help, prefix matching, exit 2) | pick a CLI lib; decide whether to reproduce argparse's flag prefix-abbreviation (probably not — document the delta) |
| P7 | `logging` module + delay=True + last-resort stderr | design a logger with lazy file creation, level filtering, and a warning counter |
| P8 | `lru_cache` memoization of subprocess probes | plain memo maps; note thread-safety if the port adds parallelism |
| P9 | `subprocess.run` driver probes (timeout 30 s) | posix_spawn/fork-exec wrapper with timeout + stderr capture |
| P10 | `configparser` over `.git/config` | tiny INI reader or shell out to `git`; section name is `remote "origin"` |
| P11 | `os.path` semantics (normpath, relpath, expanduser, abspath) | `std::filesystem` mostly maps; `relpath` across distinct roots and trailing-sep handling need care; LIKE escaping uses `os.sep` |
| P12 | ctypes version probe (`_CXString` lifetime) | direct `clang_getClangVersion()`/`clang_disposeString` — trivial, but keep the regex + 0 fallback |
| P13 | Python bindings' `is_pure_virtual_method`, `linkage.name`, `access_specifier` | map to C API: `clang_CXXMethod_isPureVirtual`, `clang_getCursorLinkage`, `clang_getCXXAccessSpecifier`; reproduce the exact stored spellings (`no-linkage` etc.) |
| P14 | `datetime('now')` (SQLite) + local-time mtime formatting | SQLite side ports as-is; CLI mtime formatting needs localtime |
| P15 | Single-threaded design | parallel indexing is the headline perf win for C++ — but requires DB write serialization and per-thread Index objects; defer to design, do not change semantics silently |
| P16 | No compile-command fingerprint in staleness (G/§4) | decide: reproduce, or add options-hash invalidation (schema change → v7) |
| P17 | No stale-symbol purge on reindex (§4) | decide: reproduce or fix (per-file delete before re-extract changes USR-sharing behavior across files — needs care) |
| P18 | Capstone-brief features absent (xrefs, call graph, `query`/`calls`/`stats`, `--jobs`, atomic DB replace) | scope decision: port-then-extend vs extend-in-port. Recommend port-first parity (this doc), extensions as v7+ |
| P19 | `macro` kind unreachable (G22) | reproduce or enable `DETAILED_PREPROCESSING_RECORD` (parse-time cost; changes header skip counts) |
| P20 | `CompilationDatabase.fromDirectory` | use `CXCompilationDatabase` (keeps `command`-string shell-unquoting behavior identical) vs own JSON parser; recommend CXCompilationDatabase for parity |

### 9.3 Reference materials

- Executable spec for storage: `project/indexer/_storage_smoke.py` (all upsert/query
  semantics asserted).
- Background on the three featured parse gotchas: `libclang-lab/CLAUDE.md` §gotchas;
  `docs/part_8_compile_db_headers.md` (compile-DB + header flags theory).
- Operational memory: `~/.claude/projects/-Users-husam-workspace-qemu-vms-libclang-lab/`
  `memory/cidx-toolchain-support.md` (toolchain matrix, validation runs, commit history of
  the diagnostics/error-cap work).
- Related wiki prior art for a compiled-language reimplementation of a libclang indexer:
  `[[pages/code/cpp-indexer]]` (Rust, Neo4j-targeted — different storage, same
  arg-sanitizing problem family, see its Issue 0001) and
  `[[pages/planning/codexgraph-cpp-libclang-rust]]`.

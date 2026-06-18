# ADR-005: AST cache for the `cidx ast` command group (M1)

Status: accepted
Date: 2026-06-18
Scope: M1 of the cidx AST-analysis feature ([[pages/planning/cidx-ast-analysis]]). Python only; C++ (M5) deferred.

## Context — forces & constraints

The `cidx ast dump|locals|conditions` commands (M0+M2, already implemented and
committed in `project/indexer/astcmd.py`) must (re)parse the translation unit
on every invocation, because they need the **live AST** (cursors, extents,
tokens, statement structure) that `index.db` deliberately does not store. A
libclang parse of a real TU costs hundreds of ms to seconds. M1 adds an
**on-disk AST cache** (`tu.save()` / `TranslationUnit.from_ast_file()`) so
repeated analysis of an unchanged file skips the parse.

Hard constraints (from the dispatch — restated so the implementer honours them):

1. **No graph dependency.** The cache reads only symbol/file tables (already true
   of `astcmd`). `astcache.py` must not import `GraphQuery`, edges, or the
   `graph` command layer.
2. **Python only.** C++ (M5) is a separable follow-up. Do not design C++ here.
   Only the C++ **version string** changes (see §8).
3. **Schema stays v13.** The cache is a *separate file cache* under
   `~/.cache/cidx/files/`, not an `index.db` change. No migration.
4. **No circular import.** `astcache.py` must not import `cli` at module load
   (`cli` imports `astcmd`, which will import `astcache`). See §7.
5. **Version pinning is load-bearing.** AST files are tied to the exact libclang
   build; `from_ast_file()` silently misbehaves across versions (wheel-18 here
   vs clang-21 on the toolchain boxes — [[cidx-toolchain-support]]). The cache
   MUST record and check the libclang version and invalidate on mismatch.

## Decision

Add `project/indexer/astcache.py` — a self-contained serialize/validate module
with a uniform content-hash key, a JSON sidecar, a `load_or_parse()` entry
point, and `build/status/clear` operations. Route `astcmd._parse_target`
through it. Bump product version 0.2.0 → 0.3.0 in both implementations.

---

## 1. Cache directory + key scheme

**Directory:** `~/.cache/cidx/files/` (honour the user-requested name; the spec
defers `asts/` to implementation — keep `files/`). Resolve it inside a function
(§7), never at import:

```python
DEFAULT_CACHE = "~/.cache/cidx"
CACHE_ENV = "INDEXER_CACHE"   # replicate cli.py constants; do NOT import cli

def cache_dir() -> str:
    return os.path.expanduser(os.environ.get(CACHE_ENV) or DEFAULT_CACHE)

def files_dir() -> str:
    return os.path.join(cache_dir(), "files")
```

> The two constants are duplicated from `cli.py` deliberately to keep
> `astcache` import-independent of `cli`. They are one line each and have not
> changed since the cache layout was fixed; if `cli`'s constants ever move, a
> one-line test (§6) asserting `astcache.cache_dir() == cli.cache_dir()` catches
> the drift.

**Key — uniform content hash (DECISION: option (a)).** Key every entry by

```python
def cache_key(target) -> str:
    h = hashlib.sha1()
    h.update(os.path.abspath(target.abspath).encode())
    h.update(b"\0")
    h.update("\0".join(target.flags).encode())
    if target.driver:
        h.update(b"\0drv\0"); h.update(target.driver.encode())
    return h.hexdigest()            # -> "<sha1>.ast" / "<sha1>.json"
```

Files: `files_dir()/<key>.ast` (binary AST) and `files_dir()/<key>.json`
(sidecar).

**Why hash, not `{file_id}.ast`:** `Target` does **not** carry `file_id` today,
and the dispatch explicitly offers (a) uniform `sha1(abspath+flags)` vs (b)
threading `file_id` through `Target`. Choose (a):

- **Index-independent.** Ad-hoc files (`path -- <flags>`) have no `file_id` at
  all, so a hash path is required regardless — (b) would force a *second* key
  scheme and a branch. One scheme is simpler and uniformly correct.
- **Flag-correct by construction.** The key *is* `f(abspath, flags, driver)`, so
  two different flag sets for the same file get distinct entries automatically —
  exactly the "key MUST incorporate the compile flags" invariant. With `file_id`
  keying, re-importing a file with new flags would silently collide on the same
  `.ast` path; the hash scheme cannot.
- **No `Target` change.** `Target` stays as M0 shipped it; `astcache` derives
  everything it needs from `abspath/flags/driver`, which `Target` already
  carries.

The `flags_hash` stored in the sidecar is the same SHA-1 over `flags` alone
(without abspath), used for the cheap validity comparison in §2 — see note there.

---

## 2. Sidecar schema + validation predicate

**Sidecar `<key>.json`** — exactly these fields:

```json
{
  "abspath": "/abs/path/to/foo.c",
  "flags_hash": "<sha1 of \\0-joined flags + driver>",
  "src_mtime": 1718700000.123456,
  "libclang_version": "clang version 18.1.1 (https://github.com/sighingnow/libclang 580da8...)"
}
```

- `abspath` — for human/`status` display and a sanity check (a hash collision is
  astronomically unlikely, but a mismatch here means a corrupt/foreign sidecar →
  treat as invalid).
- `flags_hash` — `sha1("\0".join(flags) + driver)`. Lets validity check the
  flags without re-deriving the full key. (Compute once; reuse for both this and
  `cache_key`.)
- `src_mtime` — `os.stat(abspath).st_mtime` (float) captured at save time.
- `libclang_version` — the **full authoritative** `clang_getClangVersion()`
  string (§ below), not the wheel package version. Exact-string match.

**Validation predicate:**

```python
def is_valid(target, sidecar: dict) -> bool:
    try:
        st = os.stat(target.abspath)
    except OSError:
        return False                      # source gone -> stale
    if sidecar.get("flags_hash") != flags_hash(target):
        return False
    if sidecar.get("src_mtime") != st.st_mtime:
        return False
    if sidecar.get("libclang_version") != libclang_version():
        return False
    if sidecar.get("abspath") != os.path.abspath(target.abspath):
        return False
    return True
```

A sidecar that is missing, unreadable, or not valid JSON → `is_valid` returns
`False` (load the JSON inside a `try/except (OSError, ValueError)` returning
`None`, treated as invalid). Never raise out of validation.

**Header-mtime limitation (DOCUMENT in code + `cache status` legend):** only the
**main file's** mtime is tracked. Editing an `#include`d header does **not**
invalidate the cache — a stale AST can be served. This is the documented M1
limitation (spec §AST-cache "Invalidation"). The escape hatches are
`--no-cache` (one-shot) and `cidx ast cache clear` / `build` (force refresh).
Transitive-include hashing is explicitly out of scope for M1.

**libclang version string** — lives in `astcache.py`, reusing the verified
pattern, but **triggering Config init safely** by importing the indexer clang
layer first (which initialises `clang.cindex.Config`):

```python
from functools import lru_cache

@lru_cache(maxsize=1)
def libclang_version() -> str:
    # Importing indexer.clang initialises clang.cindex.Config (sets the dylib
    # path); without it cx.conf.lib raises. Import inside the function to avoid
    # paying the cost / ordering constraint at module load.
    from .clang import parse as _ensure_config_loaded  # noqa: F401  (side effect)
    import clang.cindex as cx
    from clang.cindex import _CXString
    fn = cx.conf.lib.clang_getClangVersion
    fn.restype = _CXString
    cxstr = fn()                                  # must outlive getCString copy
    s = cx.conf.lib.clang_getCString(cxstr)
    if hasattr(s, "value"):
        s = s.value
    if isinstance(s, bytes):
        s = s.decode(errors="replace")
    del cxstr
    return s or ""
```

> The `from .clang import parse` is purely for its import side effect (Config
> init). `astcmd` already does `from .clang import ... parse`, so by the time any
> cache call runs, Config is up — but importing here too makes `astcache`
> testable in isolation. Keep the `# noqa` + comment so a future cleanup does not
> "remove the unused import".

---

## 3. `load_or_parse(target, use_cache=True) -> TranslationUnit | None`

Lives in `astcache.py`. The single entry point `astcmd._parse_target` routes
through.

```python
_PARSE_COUNT = 0        # testability hook -- see §6

def load_or_parse(target, use_cache: bool = True):
    os.makedirs(files_dir(), exist_ok=True)
    ast_path = os.path.join(files_dir(), cache_key(target) + ".ast")
    side_path = ast_path[:-4] + ".json"

    if use_cache:
        side = _read_sidecar(side_path)            # None on any failure
        if side is not None and is_valid(target, side) and os.path.exists(ast_path):
            tu = _load_ast(ast_path)               # None on any failure
            if tu is not None:
                return tu
            # corrupt / version-skew AST -> fall through and reparse

    tu = _reparse(target)                          # increments _PARSE_COUNT
    if tu is None:
        return None
    if use_cache:
        _try_save(tu, ast_path, side_path, target) # best-effort; never crash
    return tu
```

Helper behaviour (all failure-tolerant — a cache problem must NEVER crash the
command; worst case is a reparse):

- `_reparse(target)` — the **only** place that calls `clang/util.parse`:
  ```python
  def _reparse(target):
      global _PARSE_COUNT
      _PARSE_COUNT += 1
      try:
          return parse(target.abspath, target.flags,
                       driver=target.driver, check=False)
      except ClangParseError as e:
          print(f"error: {e}", file=sys.stderr)
          return None
  ```
  This subsumes the current `astcmd._parse_target` body.
- `_load_ast(path)` — `TranslationUnit.from_ast_file(path, cx.Index.create())`
  wrapped in `try/except Exception` (a foreign/corrupt AST raises
  `TranslationUnitLoadError`); return `None` so the caller reparses. This is the
  cross-libclang-version safety net: even if the sidecar version check somehow
  passes, a load failure degrades to reparse rather than crashing.
- `_try_save(tu, ast_path, side_path, target)` — `tu.save(ast_path)` then write
  the sidecar (write sidecar **after** the AST so a sidecar never points at a
  missing/partial AST). Wrap in `try/except (OSError, cx.TranslationUnitSaveError,
  Exception)`; on failure, best-effort `os.remove` the half-written `.ast` and
  log at debug — the command still returns its result from the live `tu`.
- `_read_sidecar(path)` → `json.load` in `try/except (OSError, ValueError)`
  returning `None`.

**Routing in `astcmd._parse_target`:** replace its body with a delegation that
sources `use_cache` from `args.cache`:

```python
def _parse_target(t: Target, use_cache: bool = True):
    from . import astcache
    return astcache.load_or_parse(t, use_cache=use_cache)
```

and at each call site (`cmd_dump`, `cmd_locals`, `cmd_conditions`) pass
`use_cache=getattr(args, "cache", True)`:

```python
tu = _parse_target(t, use_cache=getattr(args, "cache", True))
```

(`getattr` default `True` keeps `_parse_target` usable from tests/old callers
that don't set the flag.)

---

## 4. `cidx ast cache build|status|clear [target]`

**Argparse shape (cli.py, under the existing `ast` parser):** add a `cache`
parser to the same `asub` that holds `dump|locals|conditions`, with its **own
nested** action subparsers. Each action reuses the target/selector flags via
`_ast_common` **except** `--cache/--no-cache` (those are added separately to the
analysis commands only — §5).

```python
qc = asub.add_parser("cache", help="manage the on-disk AST cache")
csub = qc.add_subparsers(dest="cache_action", required=True)

cb = csub.add_parser("build",  help="parse + cache the target's AST (force)")
_ast_common(cb); cb.set_defaults(fn=astcmd.cmd_cache)

cstat = csub.add_parser("status", help="list cache entries, sizes, validity")
_ast_common(cstat); cstat.set_defaults(fn=astcmd.cmd_cache)

cclr = csub.add_parser("clear", help="remove cached AST(s) for a target, or all")
_ast_common(cclr); cclr.set_defaults(fn=astcmd.cmd_cache)
```

Notes:
- All three set `fn=astcmd.cmd_cache`; the handler dispatches on
  `args.cache_action`.
- `_ast_common` makes the positional `target` `nargs="?"`, so `status` and
  `clear` with no target operate over the **whole cache**; `build` with no
  target is an error (you can't build "everything" — nothing enumerates the
  un-parsed universe).
- `cache` subcommands do **not** get `--cache/--no-cache` (per dispatch).

**Handler signatures (astcmd.py), dispatching to astcache helpers:**

```python
def cmd_cache(args) -> int:
    action = args.cache_action
    if action == "status":
        return astcache.cmd_status(args)      # target optional
    if action == "clear":
        return astcache.cmd_clear(args)       # target optional -> all
    if action == "build":
        return astcache.cmd_build(args)       # target required
    print(f"error: unknown cache action {action!r}", file=sys.stderr)
    return 2
```

**astcache operations:**

- `cmd_build(args)`: `t, rc = resolve_target(args)`; if `t is None: return rc`.
  Then `load_or_parse(t, use_cache=False_for_read_but_force_write)` — concretely
  a small `build_one(t)` that **always** reparses and saves (force), ignoring any
  existing valid entry, then prints the written `.ast` path + size. Return 0, or
  1 if the parse failed.
  - `resolve_target` here requires a target/selector; `build` with neither →
    `resolve_target` already returns `(None, 2)` with its standard message.
- `cmd_status(args)`: enumerate `files_dir()/*.ast` + sidecars. For each, load
  the sidecar and print `key[:12]  <size>  <valid|STALE|orphan>  <abspath>`.
  - If a `target` is given, resolve it and show only its key (present/absent +
    validity). Validity here re-runs `is_valid(target, sidecar)` only when the
    target can be resolved; for the bulk listing (no target) validity is checked
    against the sidecar's own recorded `src_mtime`/`flags_hash`/version vs the
    **current file on disk + current libclang version**, reusing `is_valid` with
    a synthetic `Target` rebuilt from the sidecar (`abspath`, and flags are not
    recoverable from the hash — so bulk validity checks mtime + libclang version
    only, and reports `flags?` for the un-recoverable flag dimension). Document
    this: bulk `status` cannot re-verify flags (one-way hash); a per-target
    `status <target>` can, because it has the live flags.
  - `--json` prints the list as JSON. Always emit total entry count + total
    bytes.
- `cmd_clear(args)`: if a `target` is given, resolve → compute its key → remove
  `<key>.ast` + `<key>.json` (report removed/absent). With **no target**, remove
  every `*.ast`/`*.json` under `files_dir()` (report count + bytes freed).
  Failure-tolerant (`os.remove` in try/except).

> `status`/`clear` operate purely on the filesystem cache; they never need the
> index unless the user passed a `COMPONENT://`/indexed target (then
> `resolve_target` opens the index read-only, as it already does).

---

## 5. `--cache / --no-cache` flag wiring

Add a **mutually-exclusive pair, default cache ON**, to the three analysis
commands only — NOT to `cache` and NOT inside `_ast_common` (since `_ast_common`
is reused by `cache build/status/clear`, which must not carry it).

Add a tiny helper next to `_ast_common` in `cli.py`:

```python
def _cache_toggle(q):
    g = q.add_mutually_exclusive_group()
    g.add_argument("--cache", dest="cache", action="store_true", default=True,
                   help="use the on-disk AST cache (default)")
    g.add_argument("--no-cache", dest="cache", action="store_false",
                   help="ignore the cache: always reparse (and refresh it)")
```

Call `_cache_toggle(q)` on the `dump`, `locals`, and `conditions` parsers
(after `_ast_common(q)`), giving each `args.cache: bool` (default `True`). It
reaches `_parse_target` via `getattr(args, "cache", True)` as shown in §3.

> A single `dest="cache"` with `store_true(default=True)` / `store_false`
> yields the exact "default on, `--no-cache` turns off, `--cache` is an explicit
> no-op-but-allowed" semantics required.

---

## 6. Parse-counter testability hook

M3 must assert "cache hit avoids reparse" by **counting**, not timing. Use a
module-level counter in `astcache` incremented in the single reparse path
(§3, `_reparse`):

```python
_PARSE_COUNT = 0
def _parse_count() -> int:      # tiny accessor so tests don't poke a global
    return _PARSE_COUNT
def _reset_parse_count() -> None:
    global _PARSE_COUNT
    _PARSE_COUNT = 0
```

Because `_reparse` is the **only** caller of `clang/util.parse` for the ast
feature, the counter is an exact reparse tally.

**What M3 tests assert:**

1. **Cold miss parses once:** reset → `load_or_parse(t)` → `_parse_count() == 1`;
   `<key>.ast` + sidecar now exist.
2. **Warm hit avoids reparse:** with the entry present and valid →
   `load_or_parse(t)` → `_parse_count()` unchanged (still 1, i.e. +0 on the
   second call); returned TU still yields the expected cursors.
3. **`--no-cache` always reparses:** `load_or_parse(t, use_cache=False)` →
   counter increments every call; entry is (re)written.
4. **src-mtime invalidation:** `os.utime(abspath, ...)` to bump mtime → next
   `load_or_parse(t)` reparses (+1) and rewrites the sidecar with the new mtime.
5. **flags invalidation:** same file, different `Target.flags` → different key →
   cold miss → +1 (and a *second* entry exists).
6. **libclang-version invalidation:** monkeypatch the sidecar's
   `libclang_version` to a bogus string (or monkeypatch
   `astcache.libclang_version` to return a different value) → `is_valid` False →
   reparse, no crash. (Covers the cross-version gotcha without a second clang.)
7. **corrupt AST file:** truncate/garbage the `.ast` while keeping a valid
   sidecar → `_load_ast` returns None → falls back to reparse (+1), no crash.

Tests import `from indexer import astcache` and use `_reset_parse_count()` /
`_parse_count()`. The counter and accessors are the *entire* hook — no timing,
no monkeypatching of `parse` required (though tests MAY also monkeypatch
`astcache._reparse` if they want to assert "the cache path returned without
touching libclang at all").

Run the cache against a private dir: tests set `INDEXER_CACHE` to a `tmp_path`
so the real `~/.cache/cidx` is never touched (hermetic). Assert
`astcache.files_dir()` is under `tmp_path`.

---

## 7. Circular-import resolution (exact approach)

`cli` imports `astcmd` (module load). `astcmd` will import `astcache`. If
`astcache` imported `cli` at module load, that's a cycle. Resolution:

- `astcache.py` **does not import `cli` at all.** It replicates the two cache
  constants (`DEFAULT_CACHE = "~/.cache/cidx"`, `CACHE_ENV = "INDEXER_CACHE"`)
  and its own `cache_dir()` — three trivial lines that mirror `cli.py:63–65`.
  Justification: identical logic, and a one-line equivalence test (§6) guards
  against drift. This is preferred over a deferred `import cli` inside a function
  because it keeps `astcache` independent of the heavyweight `cli` module
  entirely (faster import, no transitive surprises).
- `astcmd` imports `astcache` **lazily inside functions** (`_parse_target`,
  `cmd_cache`) rather than at module top, so even if import ordering shifts,
  there is no top-level `astcmd → astcache → …` chain evaluated during `cli`'s
  import of `astcmd`. (`astcache`'s own imports — `os, json, hashlib, functools`,
  and `from .clang import parse`, `from .clang import ClangParseError` — are all
  already-loaded leaf modules with no path back to `cli`.)

Net: import graph is `cli → astcmd`, and at call time `astcmd → astcache →
clang`. No cycle.

---

## 8. Version bump

- `cli.py:60` `VERSION = "0.2.0"` → **`"0.3.0"`** (MINOR — additive feature).
- `cidx-cpp/src/cli/args.hpp:27` `kVersion = "0.2.0"` → **`"0.3.0"`**,
  byte-identical. This is the **only** C++ change in this milestone — the C++
  `ast` command itself is M5 (deferred). Keep `pyproject.toml [project].version`
  in sync too (the `cli.py` comment points at it).
- **Schema stays v13.** The AST cache is a separate file cache; no `index.db`
  format change, no migration. ([[cidx-version-bump-rule]] — product version
  moves on logic change; schema version moves only on on-disk index format
  change.)

---

## Alternatives considered

- **Key by `file_id` (option b).** Rejected: `Target` lacks `file_id`; ad-hoc
  files have none, forcing a dual key scheme; and `file_id` keying collides when
  the same file is re-imported with different flags (the AST is flag-specific).
  The content hash is uniform and flag-correct by construction. (No viable
  single-scheme variant of (b) exists because ad-hoc files mandate a hash path.)
- **Store the version/flags as a binary header inside the `.ast` instead of a
  JSON sidecar.** Rejected: libclang's `.ast` is an opaque PCH-format blob with
  no user-extensible header via the Python bindings; a sidecar is the only
  portable place to record metadata, and it lets `cache status` read validity
  without loading the (large) AST.
- **Hash transitive includes for precise invalidation.** Rejected for M1
  (cost + complexity); documented as a known limitation with `--no-cache` /
  `cache clear` escape hatches. Revisit if header-staleness bites in practice.
- **Defer config init by importing `cli.cache_dir()` for the dir.** Rejected:
  reintroduces the `cli` dependency the circular-import constraint forbids;
  duplicating two constants is cheaper and keeps `astcache` standalone.

## Consequences

**Positive:** repeated `ast` analysis of unchanged files skips the parse; the
cache is index-independent (works for ad-hoc files), flag-correct, and
version-safe (sidecar check + load-failure fallback). All cache failures degrade
to a reparse — never a crash. `astcmd`/`Target`/schema unchanged in shape.

**Negative:** main-file-mtime-only invalidation can serve a stale AST after a
header edit (documented; `--no-cache`/`clear` mitigate). Cache files are large
(100 KB–several MB); `cache status`/`clear` exist to manage growth. Bulk
`cache status` (no target) cannot re-verify the *flags* dimension from a one-way
hash — it checks mtime + libclang version and reports flags as un-verifiable;
per-target `status <target>` verifies flags fully.

**Follow-ups:** M3 (tests using the §6 hooks + version bump). M5 (C++ port:
identical key/sidecar scheme via `clang_saveTranslationUnit` /
`clang_createTranslationUnit`, `clang_getClangVersion` — deferred).

## References

- [[pages/planning/cidx-ast-analysis]] — authoritative spec (AST-cache section, M1).
- `project/indexer/astcmd.py` (M0+M2), `project/indexer/clang/util.py:472` `parse`,
  `:183 _libclang_major`; `scripts/p6_pch.py` (save/from_ast_file prototype).
- `project/indexer/storage.py` accessors (`get_file`, `get_file_by_id`,
  `file_abs_path`, `lookup_symbol*`, `search_symbols`, `get_component_by_name`).
- [[cidx-toolchain-support]] (libclang-version gotcha), [[cidx-version-bump-rule]],
  [[cidx-python-cpp-parity]] (read-side exemption).

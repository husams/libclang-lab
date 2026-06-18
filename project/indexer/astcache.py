"""indexer.astcache -- on-disk AST cache for ``cidx ast`` commands (M1).

Caches libclang translation units as ``.ast`` files (PCH format) under
``~/.cache/cidx/files/`` (overridable via ``$INDEXER_CACHE``), with a JSON
sidecar holding validity metadata.

Key scheme: ``sha1(abspath + "\\0" + flags + driver)`` so the same file with
different compile flags gets separate entries.

KNOWN LIMITATION (M1): only the main file's mtime is tracked.  Editing an
``#include``d header does NOT invalidate the cache; the stale AST is served.
Escape hatches: ``--no-cache`` (one-shot bypass) or ``cidx ast cache clear``
(force refresh).  Transitive-include hashing is out of scope for M1.

Circular-import note: this module does NOT import ``cli``.  The two cache
constants (``DEFAULT_CACHE``, ``CACHE_ENV``) are duplicated from ``cli.py``
deliberately so ``astcache`` is import-independent of the heavyweight ``cli``
module.  A one-line equivalence test asserts ``astcache.cache_dir() ==
cli.cache_dir()`` to catch drift.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from functools import lru_cache

# -- constants (duplicated from cli.py to avoid circular import) ---------------

# Keep these byte-identical to cli.py CACHE_ENV / DEFAULT_CACHE.
CACHE_ENV = "INDEXER_CACHE"
DEFAULT_CACHE = "~/.cache/cidx"


# -- directory helpers ---------------------------------------------------------


def cache_dir() -> str:
    """Cache root: ``$INDEXER_CACHE`` or ``~/.cache/cidx``."""
    return os.path.expanduser(os.environ.get(CACHE_ENV) or DEFAULT_CACHE)


def files_dir() -> str:
    """Directory where ``.ast`` / ``.json`` files live."""
    return os.path.join(cache_dir(), "files")


# -- hashing / key -------------------------------------------------------------


def flags_hash(target) -> str:
    """SHA-1 over *flags + driver* only (not the abspath).

    Stored in the sidecar so validity can re-check the flags dimension without
    re-deriving the full cache key.
    """
    h = hashlib.sha1()
    h.update("\0".join(target.flags).encode())
    if target.driver:
        h.update(b"\0drv\0")
        h.update(target.driver.encode())
    return h.hexdigest()


def cache_key(target) -> str:
    """SHA-1 over ``abspath + "\\0" + flags + driver`` -> hex string."""
    h = hashlib.sha1()
    h.update(os.path.abspath(target.abspath).encode())
    h.update(b"\0")
    h.update("\0".join(target.flags).encode())
    if target.driver:
        h.update(b"\0drv\0")
        h.update(target.driver.encode())
    return h.hexdigest()


# -- libclang version ----------------------------------------------------------


@lru_cache(maxsize=1)
def libclang_version() -> str:
    """Full ``clang_getClangVersion()`` string (cached after the first call).

    Importing ``indexer.clang`` initialises ``clang.cindex.Config`` (sets the
    dylib path); without that initialisation ``cx.conf.lib`` raises.  The import
    is done here (inside the function, not at module load) to avoid paying the
    cost / ordering constraint at import time.
    """
    from .clang import parse as _ensure_config_loaded  # noqa: F401  (side effect: Config init)
    import clang.cindex as cx
    from clang.cindex import _CXString

    fn = cx.conf.lib.clang_getClangVersion
    fn.restype = _CXString
    cxstr = fn()  # must outlive the getCString copy below
    s = cx.conf.lib.clang_getCString(cxstr)
    if hasattr(s, "value"):
        s = s.value
    if isinstance(s, bytes):
        s = s.decode(errors="replace")
    del cxstr
    return s or ""


# -- parse counter (testability hook for M3) -----------------------------------

_PARSE_COUNT: int = 0


def _parse_count() -> int:
    """Number of actual libclang parses performed this process (no resets)."""
    return _PARSE_COUNT


def _reset_parse_count() -> None:
    global _PARSE_COUNT
    _PARSE_COUNT = 0


# -- low-level cache helpers ---------------------------------------------------


def _read_sidecar(path: str) -> dict | None:
    """Load the JSON sidecar at *path*; return ``None`` on any failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def is_valid(target, sidecar: dict) -> bool:
    """Return ``True`` iff the cached AST at this sidecar is still fresh.

    Checks (in order):
    1. Source file is still accessible.
    2. ``flags_hash`` matches the current flags + driver.
    3. Source file ``mtime`` is unchanged.
    4. libclang version string matches exactly (AST files are version-pinned).
    5. ``abspath`` sanity check (collision / foreign-sidecar guard).
    """
    try:
        st = os.stat(target.abspath)
    except OSError:
        return False  # source gone -> stale
    if sidecar.get("flags_hash") != flags_hash(target):
        return False
    if sidecar.get("src_mtime") != st.st_mtime:
        return False
    if sidecar.get("libclang_version") != libclang_version():
        return False
    if sidecar.get("abspath") != os.path.abspath(target.abspath):
        return False
    return True


def _reparse(target):
    """The ONLY caller of ``indexer.clang.parse`` for the ast feature.

    Increments ``_PARSE_COUNT`` so M3 tests can assert cache-hit avoidance.
    """
    global _PARSE_COUNT
    _PARSE_COUNT += 1
    # Lazy import keeps the circular-import graph clean (astcache -> clang, never
    # astcache -> cli).
    from .clang import ClangParseError, parse

    try:
        return parse(target.abspath, target.flags, driver=target.driver, check=False)
    except ClangParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return None


def _load_ast(path: str):
    """Load a cached ``.ast`` file; return ``None`` on any failure (version skew,
    corruption, etc.) so the caller can fall through to a reparse."""
    try:
        import clang.cindex as cx

        return cx.TranslationUnit.from_ast_file(path, cx.Index.create())
    except Exception:
        return None


def _try_save(tu, ast_path: str, side_path: str, target) -> None:
    """Best-effort save: write ``tu.save(ast_path)`` then the JSON sidecar.

    Sidecar is written AFTER the AST so there is never a sidecar pointing at a
    missing / half-written ``.ast``.  Any failure is logged at debug level and
    discarded — the caller has a live ``tu`` to return regardless.

    ``tu.save()`` raises ``TranslationUnitSaveError`` on failure (returns None
    on success); we treat any exception as a best-effort failure.
    """
    try:
        tu.save(ast_path)
        # Write the sidecar only after the AST file is safely on disk.
        st = os.stat(target.abspath)
        sidecar = {
            "abspath": os.path.abspath(target.abspath),
            "flags_hash": flags_hash(target),
            "src_mtime": st.st_mtime,
            "libclang_version": libclang_version(),
        }
        with open(side_path, "w", encoding="utf-8") as fh:
            json.dump(sidecar, fh, indent=2)
    except Exception as exc:  # noqa: BLE001  (best-effort; never crash the cmd)
        import logging

        logging.getLogger(__name__).debug(
            "astcache: save failed for %s: %s", ast_path, exc
        )
        # Best-effort cleanup of a partial AST so a future sidecar check doesn't
        # find an AST without a matching sidecar.
        try:
            os.remove(ast_path)
        except OSError:
            pass


# -- public entry point --------------------------------------------------------


def load_or_parse(target, use_cache: bool = True):
    """Return a ``TranslationUnit`` for *target*, using the on-disk cache when
    *use_cache* is True and the entry is still valid.

    On any cache failure, falls back to a live reparse -- never raises.
    """
    os.makedirs(files_dir(), exist_ok=True)
    key = cache_key(target)
    ast_path = os.path.join(files_dir(), key + ".ast")
    side_path = os.path.join(files_dir(), key + ".json")

    if use_cache:
        side = _read_sidecar(side_path)
        if side is not None and is_valid(target, side) and os.path.exists(ast_path):
            tu = _load_ast(ast_path)
            if tu is not None:
                return tu
            # Corrupt / version-skew AST -> fall through and reparse.

    tu = _reparse(target)
    if tu is None:
        return None
    if use_cache:
        _try_save(tu, ast_path, side_path, target)
    return tu


# -- cache subcommand ops (called from astcmd.cmd_cache) ----------------------


def _resolve_target_for_cache(args):
    """Resolve a target for cache build/status/clear; returns (target, rc)."""
    from .astcmd import resolve_target

    return resolve_target(args)


def cmd_build(args) -> int:
    """``cidx ast cache build <target>``: force-reparse and write the cache."""
    t, rc = _resolve_target_for_cache(args)
    if t is None:
        return rc

    os.makedirs(files_dir(), exist_ok=True)
    key = cache_key(t)
    ast_path = os.path.join(files_dir(), key + ".ast")
    side_path = os.path.join(files_dir(), key + ".json")

    tu = _reparse(t)
    if tu is None:
        return 1
    _try_save(tu, ast_path, side_path, t)

    if os.path.exists(ast_path):
        size = os.path.getsize(ast_path)
        print(f"cached: {ast_path}  ({size:,} bytes)")
    else:
        print(f"warning: AST save failed for {t.abspath}", file=sys.stderr)
    return 0


def cmd_status(args) -> int:
    """``cidx ast cache status [target]``: list entries with size + validity."""
    fd = files_dir()
    if not os.path.isdir(fd):
        if getattr(args, "json", False):
            print('{"entries": [], "total_entries": 0, "total_bytes": 0}')
        else:
            print(f"cache dir does not exist: {fd}")
        return 0

    target = getattr(args, "target", None)

    if target is not None:
        # Per-target mode: full validity check (flags re-verifiable).
        t, rc = _resolve_target_for_cache(args)
        if t is None:
            return rc
        key = cache_key(t)
        ast_path = os.path.join(fd, key + ".ast")
        side_path = os.path.join(fd, key + ".json")

        if not os.path.exists(ast_path):
            if getattr(args, "json", False):
                print(
                    json.dumps(
                        {"key": key[:12], "present": False, "abspath": t.abspath}
                    )
                )
            else:
                print(f"{key[:12]}  ABSENT  {t.abspath}")
            return 0

        side = _read_sidecar(side_path)
        valid = side is not None and is_valid(t, side)
        size = os.path.getsize(ast_path)
        status_str = "valid" if valid else "STALE"
        abspath = side.get("abspath", t.abspath) if side else t.abspath

        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "key": key[:12],
                        "present": True,
                        "valid": valid,
                        "size": size,
                        "abspath": abspath,
                    },
                    indent=2,
                )
            )
        else:
            print(f"{key[:12]}  {size:>10,}  {status_str:<8}  {abspath}")
        return 0

    # Bulk mode: enumerate all *.json sidecars.
    # NOTE: bulk validity cannot re-verify flags (one-way hash).  It checks
    # mtime + libclang version only; the flags dimension is reported as "flags?".
    entries = []
    total_bytes = 0
    try:
        names = sorted(os.listdir(fd))
    except OSError:
        names = []

    import dataclasses

    @dataclasses.dataclass
    class _FakeTgt:
        """Minimal target-like object for is_valid when we have no live flags."""

        abspath: str
        flags: list
        driver: str | None

    for name in names:
        if not name.endswith(".json"):
            continue
        key = name[:-5]
        ast_path = os.path.join(fd, key + ".ast")
        side_path = os.path.join(fd, name)
        side = _read_sidecar(side_path)
        if side is None:
            entries.append(
                {"key": key[:12], "status": "orphan-sidecar", "size": 0, "abspath": "?"}
            )
            continue
        if not os.path.exists(ast_path):
            entries.append(
                {
                    "key": key[:12],
                    "status": "orphan-sidecar",
                    "size": 0,
                    "abspath": side.get("abspath", "?"),
                }
            )
            continue

        size = os.path.getsize(ast_path)
        total_bytes += size
        abspath = side.get("abspath", "?")

        # Bulk validity: only what we can check without the original flags.
        try:
            st = os.stat(abspath)
            mtime_ok = side.get("src_mtime") == st.st_mtime
        except OSError:
            mtime_ok = False

        version_ok = side.get("libclang_version") == libclang_version()

        if not mtime_ok:
            st_str = "STALE"
        elif not version_ok:
            st_str = "STALE(ver)"
        else:
            st_str = "valid(flags?)"  # flags not re-verifiable in bulk mode

        entries.append(
            {"key": key[:12], "status": st_str, "size": size, "abspath": abspath}
        )

    total_entries = len(entries)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "entries": entries,
                    "total_entries": total_entries,
                    "total_bytes": total_bytes,
                },
                indent=2,
            )
        )
    else:
        if not entries:
            print("cache is empty")
        else:
            print(f"{'key':<14}  {'size':>10}  {'status':<16}  abspath")
            print("-" * 72)
            for e in entries:
                print(
                    f"{e['key']:<14}  {e['size']:>10,}  {e['status']:<16}  "
                    f"{e['abspath']}"
                )
        print(
            f"\n{total_entries} entr{'y' if total_entries == 1 else 'ies'}, "
            f"{total_bytes:,} bytes total"
        )
        if entries:
            print(
                "note: bulk status cannot re-verify compile flags (one-way "
                "hash); pass a target for full validation"
            )
    return 0


def cmd_clear(args) -> int:
    """``cidx ast cache clear [target]``: remove cache entry/entries."""
    fd = files_dir()

    target = getattr(args, "target", None)
    if target is not None:
        # Per-target clear.
        t, rc = _resolve_target_for_cache(args)
        if t is None:
            return rc
        key = cache_key(t)
        ast_path = os.path.join(fd, key + ".ast")
        side_path = os.path.join(fd, key + ".json")
        removed = 0
        freed = 0
        for p in (ast_path, side_path):
            try:
                size = os.path.getsize(p)
                os.remove(p)
                freed += size
                removed += 1
            except OSError:
                pass
        if removed:
            print(f"removed {removed} file(s), {freed:,} bytes freed")
        else:
            print(f"no cache entry for {t.abspath}")
        return 0

    # Clear all.
    if not os.path.isdir(fd):
        print("cache dir does not exist; nothing to clear")
        return 0
    removed = 0
    freed = 0
    try:
        names = os.listdir(fd)
    except OSError as exc:
        print(f"error listing cache dir: {exc}", file=sys.stderr)
        return 1
    for name in names:
        if not (name.endswith(".ast") or name.endswith(".json")):
            continue
        p = os.path.join(fd, name)
        try:
            freed += os.path.getsize(p)
            os.remove(p)
            removed += 1
        except OSError:
            pass
    print(f"cleared {removed} file(s), {freed:,} bytes freed")
    return 0

"""indexer.pch -- one shared precompiled header (PCH) for system + C++ standard
-library includes, used to accelerate cold indexing.

A PCH is a serialized AST of an umbrella header that ``#include``s the heavy
system/STL headers every C++ TU pulls in.  When present and compatible it is
injected as ``-include-pch <pch>`` into every C++ parse (see
``clang.util.parse``), so libclang deserializes that AST once instead of
re-lexing ``<vector>`` / ``<string>`` / ... for every translation unit.

It is a *pure speed optimization*: the indexed symbols and edges are identical
with or without it (a PCH is semantically transparent), so it never changes
index output -- and therefore does not affect Python<->C++ index parity.

Layout (next to the per-TU ``.ast`` cache, under ``$INDEXER_CACHE/files``):

    files/system.pch          the serialized umbrella-header AST
    files/system.pch.json     sidecar: flags / driver / libclang version / ...
    files/system_umbrella.hpp  the generated umbrella (kept for reproducibility)

Compatibility is conservative.  The baked flag-set is the INTERSECTION of every
indexed C++ TU's PCH-relevant flags, so it can never conflict with a consuming
TU's own macros, and the PCH is only injected into a C++ TU parsed by the SAME
libclang version and the SAME driver it was built with.  On any incompatibility
the consuming parse falls back to a normal reparse, so a stale or mismatched PCH
can only slow indexing to the un-accelerated path -- never break it.

``CIDX_NO_PCH`` (truthy) disables injection entirely.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from collections import Counter
from typing import Sequence

from . import astcache

PCH_NAME = "system.pch"
SIDECAR_NAME = "system.pch.json"
UMBRELLA_NAME = "system_umbrella.hpp"

NO_PCH_ENV = "CIDX_NO_PCH"

#: Heavy C++ standard headers that nearly every TU transitively includes; the
#: default umbrella.  Extra project/third-party headers can be appended with
#: ``cidx pch build --include <header>``.
DEFAULT_HEADERS: tuple[str, ...] = (
    "algorithm",
    "array",
    "atomic",
    "chrono",
    "cstddef",
    "cstdint",
    "cstdio",
    "cstdlib",
    "cstring",
    "deque",
    "exception",
    "functional",
    "iosfwd",
    "iostream",
    "iterator",
    "limits",
    "list",
    "map",
    "memory",
    "mutex",
    "numeric",
    "optional",
    "ostream",
    "set",
    "sstream",
    "stdexcept",
    "string",
    "thread",
    "tuple",
    "type_traits",
    "unordered_map",
    "unordered_set",
    "utility",
    "vector",
)

# -- paths ---------------------------------------------------------------------


def pch_path() -> str:
    return os.path.join(astcache.files_dir(), PCH_NAME)


def sidecar_path() -> str:
    return os.path.join(astcache.files_dir(), SIDECAR_NAME)


def umbrella_path() -> str:
    return os.path.join(astcache.files_dir(), UMBRELLA_NAME)


# -- flag selection ------------------------------------------------------------

# Flags that consume the following token (drop both -- include/linker/dep noise
# plus a stale -include-pch / -x that must never be baked in). The -i* search
# flags appear in both forms (`-isystem /p` and the joined `-isystem/p`); the
# separate form is caught here, the joined form by _DROP_PREFIX below.
_TAKES_VALUE = frozenset(
    {
        "-Xlinker", "-MT", "-MF", "-include", "-include-pch", "-x",
        "-isystem", "-iquote", "-idirafter",
    }
)
# Standalone tokens to drop (linker options).
_DROP_EXACT = frozenset({"-shared", "-static", "-rdynamic", "-pthread"})
# Joined-prefix forms to drop (include paths + linker options).
_DROP_PREFIX = ("-I", "-L", "-l", "-Wl,", "-iquote", "-isystem", "-idirafter")


def pch_relevant(options: Sequence[str]) -> list[str]:
    """The subset of *options* that can affect a system/STL header PCH.

    Drops include paths, linker options, and any ``-x`` / ``-include`` /
    ``-include-pch`` pairs; keeps ``-std`` / ``-D`` / ``-U`` / ``-f*`` / ``-m*``
    / ``-W*`` / ``--driver-mode`` and the like.  Mirrors the filter used by the
    flag-consistency report so the baked flags match what that report calls
    PCH-relevant.
    """
    keep: list[str] = []
    skip_next = False
    for a in options:
        if skip_next:
            skip_next = False
            continue
        if a in _DROP_EXACT:
            continue
        if a in _TAKES_VALUE:
            skip_next = True
            continue
        if a.startswith(_DROP_PREFIX):
            continue
        keep.append(a)
    return keep


def common_cpp_flags(index_db: str) -> tuple[list[str], str | None, int]:
    """Derive (common_flags, driver, n_cpp) from the index's C++ TUs.

    ``common_flags`` is the sorted INTERSECTION of every indexed C++ TU's
    PCH-relevant flags (after the same sanitize/resolve the indexer applies);
    ``driver`` is the dominant C++ driver among them.  Headers are excluded
    (they carry no compile command of their own).  Returns ([], None, 0) when
    the index has no C++ translation units.
    """
    from .storage import Storage
    from . import compiledb
    from .clang import is_cpp

    common: set[str] | None = None
    drivers: Counter[str | None] = Counter()
    n_cpp = 0
    with Storage(index_db) as db:
        for rec, path in db.files():
            opts = rec.compile_options or []
            if not opts:
                continue  # no compile command (e.g. a header): not a TU
            resolved = compiledb.resolve_options(
                compiledb.sanitize(opts), db.get_alias
            )
            if not is_cpp(path, resolved):
                continue
            n_cpp += 1
            flags = set(pch_relevant(resolved))
            common = flags if common is None else (common & flags)
            drivers[rec.driver] += 1
    if not n_cpp:
        return [], None, 0
    driver = drivers.most_common(1)[0][0] if drivers else None
    return sorted(common or set()), driver, n_cpp


# -- consumption gate (called from clang.util.parse for every TU) --------------


def _no_pch() -> bool:
    return os.environ.get(NO_PCH_ENV, "").strip().lower() not in (
        "",
        "0",
        "off",
        "none",
        "false",
    )


def consume_args(cpp: bool, driver: str | None) -> list[str]:
    """``['-include-pch', <path>]`` when a compatible system PCH should be
    injected into this parse, else ``[]``.

    Compatible means: it is a C++ TU, ``CIDX_NO_PCH`` is unset, the PCH and its
    sidecar both exist, and the sidecar's libclang version and driver match the
    current ones.  Any error reading the sidecar yields ``[]`` (never raise:
    this runs on the hot parse path)."""
    if not cpp or _no_pch():
        return []
    pp = pch_path()
    if not os.path.exists(pp):
        return []
    try:
        with open(sidecar_path(), encoding="utf-8") as fh:
            side = json.load(fh)
        if side.get("libclang_version") != astcache.libclang_version():
            return []
        if (side.get("driver") or None) != (driver or None):
            return []
    except (OSError, ValueError):
        return []
    return ["-include-pch", pp]


# -- umbrella ------------------------------------------------------------------


def _umbrella_text(headers: Sequence[str]) -> str:
    lines = [
        "// Generated by `cidx pch build` -- shared system/C++ precompiled header.",
        "// Edit via `cidx pch build --include <header>`; do not hand-edit.",
    ]
    lines += [f"#include <{h}>" for h in headers]
    lines.append("")
    return "\n".join(lines)


# -- build / status / clear (CLI ops) ------------------------------------------


def cmd_build(
    index_db: str,
    add_flags: Sequence[str] = (),
    add_headers: Sequence[str] = (),
    driver: str | None = None,
    std: str | None = None,
    force: bool = False,
) -> int:
    """``cidx pch build``: derive the common C++ flags from the index, compile
    an umbrella of system/STL headers into a single PCH, and cache it."""
    import clang.cindex as cx
    from .clang import ClangParseError, parse

    if os.path.exists(pch_path()) and not force:
        print(
            f"system PCH already exists: {pch_path()} (use --force to rebuild)",
            file=sys.stderr,
        )
        return 1

    derived, dom_driver, n_cpp = common_cpp_flags(index_db)
    if n_cpp == 0:
        print(
            "error: no C++ translation units in the index; nothing to build a "
            "C++ PCH from",
            file=sys.stderr,
        )
        return 1
    chosen_driver = driver if driver is not None else dom_driver

    flags = list(derived)
    if std:
        flags = [f for f in flags if not f.startswith("-std=")]
        flags.append(f"-std={std}")
    flags += list(add_flags)

    headers = list(DEFAULT_HEADERS) + list(add_headers)

    os.makedirs(astcache.files_dir(), exist_ok=True)
    with open(umbrella_path(), "w", encoding="utf-8") as fh:
        fh.write(_umbrella_text(headers))

    # Disable PCH injection while BUILDING the PCH (an old/half-built one must
    # not be prepended onto the umbrella parse), then restore the prior setting.
    prev = os.environ.get(NO_PCH_ENV)
    os.environ[NO_PCH_ENV] = "1"
    try:
        tu = parse(
            umbrella_path(),
            flags + ["-x", "c++-header"],
            driver=chosen_driver,
            options=cx.TranslationUnit.PARSE_INCOMPLETE,
            check=False,
        )
    except ClangParseError as e:
        print(f"error: failed to parse the umbrella header: {e}", file=sys.stderr)
        return 1
    finally:
        if prev is None:
            os.environ.pop(NO_PCH_ENV, None)
        else:
            os.environ[NO_PCH_ENV] = prev

    try:
        tu.save(pch_path())
    except Exception as e:  # cx.TranslationUnitSaveError et al.
        print(f"error: failed to save the PCH: {e}", file=sys.stderr)
        return 1

    sidecar = {
        "libclang_version": astcache.libclang_version(),
        "driver": chosen_driver,
        "flags": flags,
        "headers": headers,
        "n_cpp_tus": n_cpp,
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "cpp": True,
    }
    with open(sidecar_path(), "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2)

    size = os.path.getsize(pch_path())
    print(f"built system PCH: {pch_path()}  ({size:,} bytes)")
    print(f"  C++ TUs in index : {n_cpp}")
    print(f"  driver           : {chosen_driver or '(host default)'}")
    print(f"  flags            : {' '.join(flags) or '(none)'}")
    print(f"  headers          : {len(headers)} (umbrella: {umbrella_path()})")
    print("  injected as `-include-pch` into every matching C++ parse.")
    return 0


def cmd_status() -> int:
    """``cidx pch status``: show the cached PCH's size, flags, and validity."""
    pp = pch_path()
    if not os.path.exists(pp):
        print("no system PCH built (run `cidx pch build`)")
        return 0
    size = os.path.getsize(pp)
    print(f"system PCH : {pp}  ({size:,} bytes)")
    try:
        with open(sidecar_path(), encoding="utf-8") as fh:
            side = json.load(fh)
    except (OSError, ValueError):
        print("sidecar    : MISSING/unreadable -- PCH will NOT be injected")
        return 0
    cur = astcache.libclang_version()
    ver_ok = side.get("libclang_version") == cur
    print(f"built at   : {side.get('built_at', '?')}")
    print(f"driver     : {side.get('driver') or '(host default)'}")
    print(f"flags      : {' '.join(side.get('flags', [])) or '(none)'}")
    print(f"headers    : {len(side.get('headers', []))}")
    print(f"libclang   : {side.get('libclang_version', '?')}")
    print(
        f"validity   : {'OK -- injected into matching C++ parses' if ver_ok else 'STALE (libclang version changed) -- rebuild'}"
    )
    return 0


def cmd_clear() -> int:
    """``cidx pch clear``: remove the cached PCH, its sidecar, and umbrella."""
    removed = 0
    freed = 0
    for p in (pch_path(), sidecar_path(), umbrella_path()):
        try:
            freed += os.path.getsize(p)
            os.remove(p)
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"removed {removed} file(s), {freed:,} bytes freed")
    else:
        print("no system PCH to clear")
    return 0

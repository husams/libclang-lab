"""indexer.clang.util -- parse a source file from its stored clang args.

The pip `libclang` wheel ships the native dylib but NOT clang's builtin
headers, so a bare parse dies with a fatal 'stddef.h file not found' that
silently truncates the AST. parse() therefore appends the missing toolchain
search paths -- in C++-correct order: sysroot -> libc++ -> clang builtins --
and raises ClangParseError on fatal diagnostics instead of handing back a
truncated translation unit.

When the compile command's driver is known (file.driver from the compile
database), its system include search list is replicated instead: the driver
is asked for its paths (`<driver> -E -x c++ - -v`) and they are passed as
-nostdinc + -isystem flags, with the driver's compiler-builtin directory
swapped for THIS libclang's builtin headers. That makes self-contained
cross-toolchains work (e.g. /opt/.../bin/g++ with its own sysroot and
libstdc++), where neither the host headers nor a PATH clang are right.

Environment:
    CIDX_LIBCLANG       path to an alternative libclang shared library
                        (e.g. /opt/llvm-21.1.1/lib/libclang.so) -- must be
                        set before the first parse
    CIDX_RESOURCE_DIR   clang resource dir matching that library; its
                        include/ subdir provides the builtin headers
                        (derived automatically from CIDX_LIBCLANG when unset)
"""

import os
import re
import subprocess
import sys
from collections.abc import Sequence
from functools import lru_cache
from glob import glob

import clang.cindex as cx

LIBCLANG_ENV = "CIDX_LIBCLANG"
RESOURCE_ENV = "CIDX_RESOURCE_DIR"

if os.environ.get(LIBCLANG_ENV) and not cx.Config.loaded:
    cx.Config.set_library_file(os.path.expanduser(os.environ[LIBCLANG_ENV]))


class ClangParseError(Exception):
    """The parse produced error/fatal diagnostics; the AST would be truncated."""


_CPP_SUFFIXES: frozenset[str] = frozenset(
    {".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx"}
)


@lru_cache(maxsize=1)
def _sysroot() -> str | None:
    """macOS SDK path (for <stdio.h>, <string>, ...). None elsewhere."""
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.check_output(
            ["xcrun", "--show-sdk-path"], text=True
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=1)
def _resource_include() -> str | None:
    """Clang's builtin-header dir (stddef.h, stdarg.h, ...).

    Tried in order: $CIDX_RESOURCE_DIR/include; lib/clang/<v>/include next to
    a $CIDX_LIBCLANG library (a full LLVM install ships both side by side);
    and finally `-print-resource-dir` of a PATH clang -- the pip wheel does
    not ship these headers at all.
    """
    def _check(inc: str) -> str | None:
        return inc if os.path.exists(os.path.join(inc, "stddef.h")) else None

    rd = os.environ.get(RESOURCE_ENV)
    if rd:
        inc = _check(os.path.join(os.path.expanduser(rd), "include"))
        if inc:
            return inc
    lib = os.environ.get(LIBCLANG_ENV)
    if lib:
        libdir = os.path.dirname(os.path.expanduser(lib))
        for cand in sorted(glob(os.path.join(libdir, "clang", "*", "include")),
                           reverse=True):
            inc = _check(cand)
            if inc:
                return inc
    for cc in ("clang", "clang++"):
        try:
            rd = subprocess.check_output(
                [cc, "-print-resource-dir"], text=True
            ).strip()
            inc = _check(os.path.join(rd, "include"))
            if inc:
                return inc
        except (OSError, subprocess.SubprocessError):
            continue
    return None


@lru_cache(maxsize=None)
def _driver_search_dirs(driver: str, lang: str) -> tuple[str, ...]:
    """The driver's `#include <...>` system search list, in driver order.

    Asks the actual compiler from the compile command -- gcc and clang both
    print the list on `-E -x <lang> - -v`. Returns () when the driver is
    missing or won't answer (callers fall back to host defaults).
    """
    try:
        proc = subprocess.run(
            [driver, "-E", "-x", lang, "-", "-v"],
            input="", capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    dirs: list[str] = []
    active = False
    for line in proc.stderr.splitlines():
        if line.startswith("#include <...> search starts here"):
            active = True
            continue
        if line.startswith("End of search list"):
            break
        if active and line.startswith(" "):
            d = line.strip()
            if d.endswith("(framework directory)"):    # macOS noise
                continue
            d = os.path.normpath(d)
            if os.path.isdir(d):
                dirs.append(d)
    return tuple(dirs)


#: A compiler's private header dirs inside its search list -- gcc's
#: lib/gcc/<triple>/<ver>/include + include-fixed, or clang's
#: lib/clang/<ver>/include. These must NOT be fed to libclang: gcc's
#: intrinsics headers use gcc-only builtins, and gcc's include-fixed/limits.h
#: keys on the _GCC_LIMITS_H_ guard that clang's own limits.h sets before
#: #include_next, severing the chain to the libc limits.h (clang's driver
#: excludes include-fixed for the same reason). libclang's own resource
#: headers take their place.
_BUILTIN_DIR_RE = re.compile(r"[/\\]lib(32|64)?[/\\](gcc|gcc-cross|clang)[/\\]")


def driver_flags(driver: str, cpp: bool = False) -> list[str]:
    """Replicate `driver`'s system include search for libclang.

    -nostdinc suppresses ALL host defaults, then the driver's reported dirs
    are re-added as -isystem in the driver's own order -- which is the
    C++-correct order (libstdc++ -> compiler builtins -> libc) -- with the
    driver's builtin dir swapped for this libclang's. Empty list when the
    driver can't be queried.
    """
    dirs = _driver_search_dirs(driver, "c++" if cpp else "c")
    if not dirs:
        return []
    res = _resource_include()
    flags = ["-nostdinc"]
    substituted = False
    for d in dirs:
        if _BUILTIN_DIR_RE.search(d):
            if res and not substituted:
                flags += ["-isystem", res]
                substituted = True
            continue
        flags += ["-isystem", d]
    if res and not substituted:
        flags += ["-isystem", res]
    return flags


def is_cpp(filename: str, args: Sequence[str] = ()) -> bool:
    """Treat as C++ if the compile args say so, else go by file extension."""
    if "--driver-mode=g++" in args or "-xc++" in args:
        return True
    try:
        i = list(args).index("-x")
        return args[i + 1].startswith("c++")
    except (ValueError, IndexError):
        pass
    return os.path.splitext(filename)[1].lower() in _CPP_SUFFIXES


def toolchain_flags(cpp: bool = False, driver: str | None = None) -> list[str]:
    """Search-path flags the pip libclang wheel lacks, in C++-correct order.

    With a known compile-command driver, its search list is replicated
    verbatim (see driver_flags) -- required for self-contained cross
    toolchains. Otherwise (or when the driver won't answer) fall back to the
    host defaults: macOS SDK + PATH clang's builtin headers. For C++ the
    order must be sysroot -> libc++ -> clang builtins; putting the builtin
    dir first breaks <cstddef>'s include_next chain (a fatal that silently
    truncates the AST).
    """
    if driver:
        flags = driver_flags(driver, cpp=cpp)
        if flags:
            return flags
    flags = []
    sdk = _sysroot()
    if sdk:
        flags += ["-isysroot", sdk]
        if cpp:
            flags += ["-isystem", os.path.join(sdk, "usr", "include", "c++", "v1")]
    res = _resource_include()
    if res:
        flags += ["-isystem", res]
    return flags


def fatal_diagnostics(tu: cx.TranslationUnit) -> list[cx.Diagnostic]:
    """Error/fatal diagnostics (severity >= ERROR) for a translation unit."""
    return [d for d in tu.diagnostics if d.severity >= cx.Diagnostic.Error]


def parse(
    filename: str,
    args: Sequence[str] = (),
    *,
    driver: str | None = None,
    options: int = 0,
    check: bool = True,
) -> cx.TranslationUnit:
    """Parse `filename` with its (already stripped) compile args.

    Toolchain search paths are appended automatically, so `args` can come
    straight from the database's compile_options; pass the compile command's
    `driver` to replicate that compiler's search paths instead of the host
    defaults. With check=True (default), raise ClangParseError if the parse
    has fatal diagnostics; pass check=False to inspect a broken TU yourself.
    """
    flags = list(args) + toolchain_flags(cpp=is_cpp(filename, args),
                                         driver=driver)
    index = cx.Index.create()
    try:
        tu = index.parse(filename, args=flags, options=options)
    except cx.TranslationUnitLoadError as e:
        raise ClangParseError(f"cannot parse {filename}") from e
    if check:
        fatals = fatal_diagnostics(tu)
        if fatals:
            summary = "; ".join(
                f"{d.location.file}:{d.location.line}: {d.spelling}"
                for d in fatals[:3]
            )
            raise ClangParseError(
                f"{filename}: {len(fatals)} fatal diagnostic(s): {summary}"
            )
    return tu

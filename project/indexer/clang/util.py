"""indexer.clang.util -- parse a source file from its stored clang args.

The pip `libclang` wheel ships the native dylib but NOT clang's builtin
headers, so a bare parse dies with a fatal 'stddef.h file not found' that
silently truncates the AST. parse() therefore appends the missing toolchain
search paths -- in C++-correct order: sysroot -> libc++ -> clang builtins --
and raises ClangParseError on fatal diagnostics instead of handing back a
truncated translation unit.
"""

import os
import subprocess
import sys
from collections.abc import Sequence
from functools import lru_cache

import clang.cindex as cx


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
    """Clang's builtin-header dir (stddef.h, stdarg.h, ...), borrowed from
    whatever clang is on PATH -- the pip wheel does not ship these headers."""
    for cc in ("clang", "clang++"):
        try:
            rd = subprocess.check_output(
                [cc, "-print-resource-dir"], text=True
            ).strip()
            inc = os.path.join(rd, "include")
            if os.path.exists(os.path.join(inc, "stddef.h")):
                return inc
        except (OSError, subprocess.SubprocessError):
            continue
    return None


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


def toolchain_flags(cpp: bool = False) -> list[str]:
    """Search-path flags the pip libclang wheel lacks, in C++-correct order.

    For C++ the order must be sysroot -> libc++ -> clang builtins; putting the
    builtin dir first breaks <cstddef>'s include_next chain (a fatal that
    silently truncates the AST).
    """
    flags: list[str] = []
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
    options: int = 0,
    check: bool = True,
) -> cx.TranslationUnit:
    """Parse `filename` with its (already stripped) compile args.

    Toolchain search paths are appended automatically, so `args` can come
    straight from the database's compile_options. With check=True (default),
    raise ClangParseError if the parse has fatal diagnostics; pass check=False
    to inspect a broken TU yourself.
    """
    flags = list(args) + toolchain_flags(cpp=is_cpp(filename, args))
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

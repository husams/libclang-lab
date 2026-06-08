"""Shared helpers for the libclang lab.

`import clang.cindex` only works if libclang (the native shared library) can be
loaded. The pip `libclang` wheel bundles that dylib, so on this machine nothing
extra is required. If you ever need to point Python at a *specific* libclang
(e.g. a Homebrew LLVM), set it ONCE, before creating any Index:

    import clang.cindex as cx
    cx.Config.set_library_file("/opt/homebrew/opt/llvm/lib/libclang.dylib")

Run any lab script from the repo root, e.g.:

    python3 libclang-lab/scripts/p1_walk.py
"""
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import clang.cindex as cx

# Resolve paths relative to this file so scripts work from any cwd.
LAB_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = LAB_ROOT / "manifests"


@lru_cache(maxsize=1)
def _sysroot():
    """macOS SDK path (for <stdio.h>, <string>, ...). None elsewhere."""
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    except Exception:
        return None


@lru_cache(maxsize=1)
def _resource_include():
    """Clang's builtin-header dir (stddef.h, stdarg.h, ...).

    The pip `libclang` wheel ships the dylib but NOT these headers, so a bare
    parse fails with a fatal 'stddef.h file not found' that silently truncates
    the AST. We borrow them from whatever clang is on PATH.
    """
    for cc in ("clang", "clang++"):
        try:
            rd = subprocess.check_output([cc, "-print-resource-dir"], text=True).strip()
            inc = Path(rd) / "include"
            if (inc / "stddef.h").exists():
                return str(inc)
        except Exception:
            continue
    return None


def clang_args(std="c11", extra_includes=()):
    """Return compiler flags that let libclang resolve system + builtin headers.

    This is the portable fix for the #1 libclang gotcha. On macOS it adds
    `-isysroot <SDK>` and `-I <clang resource dir>/include`; on Linux those are
    usually already on the default search path, so it degrades gracefully.
    """
    args = [f"-std={std}"]
    sysroot = _sysroot()
    if sysroot:
        args += ["-isysroot", sysroot]
    rinc = _resource_include()
    if rinc:
        args += ["-I", rinc]
    args += ["-I", str(MANIFESTS)]
    for inc in extra_includes:
        args += ["-I", str(inc)]
    return args


def parse(path, args=None, options=0, unsaved_files=None):
    """Parse a source file and return its TranslationUnit.

    args:    compiler flags. Pass `clang_args()` for a clean parse, or a raw
             list (even []) to see what happens without header resolution.
    options: bitmask of TranslationUnit.PARSE_* flags.
    """
    index = cx.Index.create()
    return index.parse(
        str(path),
        args=list(args or []),
        unsaved_files=unsaved_files,
        options=options,
    )


def fatal_diagnostics(tu):
    """Return error/fatal diagnostics (severity >= ERROR) for a TU."""
    return [d for d in tu.diagnostics if d.severity >= cx.Diagnostic.Error]


def walk(cursor, depth=0):
    """Yield (cursor, depth) for `cursor` and every descendant, pre-order."""
    yield cursor, depth
    for child in cursor.get_children():
        yield from walk(child, depth + 1)


def loc(cursor):
    """Compact 'file:line:col' for a cursor's start location."""
    location = cursor.location
    if location.file is None:
        return "<builtin>"
    name = Path(location.file.name).name
    return f"{name}:{location.line}:{location.column}"


def in_main_file(cursor):
    """True if the cursor lives in the file we asked to parse (not an #include)."""
    f = cursor.location.file
    return f is not None and f.name == cursor.translation_unit.spelling


def top_level(tu):
    """Yield direct children of the TU that originate in the main file."""
    for child in tu.cursor.get_children():
        if in_main_file(child):
            yield child

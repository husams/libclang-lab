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

import ctypes
import logging
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from functools import lru_cache
from glob import glob

import clang.cindex as cx

#: Parse warnings (tolerated error diagnostics, toolchain fallbacks) go here;
#: the cidx CLI attaches a file handler ($INDEXER_CACHE/cidx.log). Without a
#: handler (library use, lab scripts) logging's last-resort handler keeps
#: printing them to stderr.
_log = logging.getLogger("cidx.clang")

LIBCLANG_ENV = "CIDX_LIBCLANG"
RESOURCE_ENV = "CIDX_RESOURCE_DIR"
GNUC_ENV = "CIDX_GNUC_VERSION"

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
    # last resort: well-known LLVM install prefixes, newest version first
    found = []
    for pattern in ("/opt/llvm*/lib*/clang/*/include",
                    "/usr/lib/llvm-*/lib/clang/*/include",
                    "/usr/local/llvm*/lib/clang/*/include",
                    "/usr/lib*/clang/*/include"):
        for cand in glob(pattern):
            inc = _check(cand)
            if inc:
                ver = os.path.basename(os.path.dirname(cand))
                try:
                    key = tuple(int(p) for p in ver.split("."))
                except ValueError:
                    key = (0,)
                found.append((key, inc))
    if found:
        return max(found)[1]
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


_GCC_DRIVER_RE = re.compile(r"(^|-)(gcc|g\+\+)(-[\d.]+)?$")


@lru_cache(maxsize=None)
def _gcc_version(driver: str) -> str | None:
    """`driver`'s full gcc version (e.g. '13.4.1'), or None for non-gcc."""
    if not _GCC_DRIVER_RE.search(os.path.basename(driver)):
        return None
    for flag in ("-dumpfullversion", "-dumpversion"):
        try:
            v = subprocess.check_output(
                [driver, flag], text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if re.fullmatch(r"\d+(\.\d+)*", v):
            return v
    return None


@lru_cache(maxsize=1)
def _libclang_major() -> int:
    """Major version of the LOADED libclang (0 when undeterminable)."""
    try:
        from clang.cindex import _CXString
        fn = cx.conf.lib.clang_getClangVersion
        fn.restype = _CXString
        cxstr = fn()                # MUST outlive the getCString copy below:
        s = cx.conf.lib.clang_getCString(cxstr)  # points INTO cxstr, and the
        if hasattr(s, "value"):     # _CXString destructor disposes the buffer
            s = s.value             # (official clang>=17 bindings return a
        if isinstance(s, bytes):    # c_interop_string here, not str)
            s = s.decode(errors="replace")
        del cxstr
        m = re.search(r"version (\d+)", s or "")
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


@lru_cache(maxsize=None)
def _glibc_probe(driver: str, cpp: bool) -> tuple[bool, bool]:
    """(cxx13_floatn_keywords, malloc_attr_args) of the driver's libc.

    glibc >= 2.38 treats _FloatN as KEYWORDS in C++ once the compiler claims
    gcc 13 (detectable: floatn-common.h mentions __GNUC_PREREQ (13)); glibc
    >= 2.34 decorates allocators with __attribute__((malloc(deallocator)))
    once it claims gcc 11 (detectable: sys/cdefs.h defines __attr_dealloc).
    """
    floatn13 = malloc_args = False
    for d in _driver_search_dirs(driver, "c++" if cpp else "c"):
        f = os.path.join(d, "bits", "floatn-common.h")
        if not floatn13 and os.path.exists(f):
            with open(f, errors="ignore") as fh:
                floatn13 = bool(re.search(r"__GNUC_PREREQ\s*\(13", fh.read()))
        c = os.path.join(d, "sys", "cdefs.h")
        if not malloc_args and os.path.exists(c):
            with open(c, errors="ignore") as fh:
                malloc_args = "__attr_dealloc" in fh.read()
    return floatn13, malloc_args


#: _Float32 & co are gcc keywords clang doesn't implement; alias them to
#: plain types where glibc's declarations would otherwise be unparseable.
#: Parse-fidelity only -- no codegen ever happens here.
_FLOATN_ALIASES = [
    "-D_Float32=float", "-D_Float64=double", "-D_Float128=long double",
    "-D_Float32x=double", "-D_Float64x=long double",
]


def _gnuc_version_flag(driver: str, cpp: bool = False) -> list[str]:
    """clang's __GNUC__ masquerade (default: gcc 4.2) hides any code behind
    `#if __GNUC__ >= N` guards that the real driver compiles -- symptoms range
    from missing declarations to incomplete types. -fgnuc-version= makes the
    parse report the driver's own version, with two glibc landmines probed
    and defused (see _glibc_probe):

      * malloc(deallocator) attributes: parseable only by libclang >= 21;
        with an older parser the claimed version is capped below 11.
      * _FloatN: in C (claimed >= 7), and in C++ on glibc >= 2.38 (claimed
        >= 13), glibc uses gcc-only keywords -- aliased away via -D. C++ on
        older glibc instead typedefs them, where the aliases would mangle
        the typedefs -- so there they are NOT added.

    $CIDX_GNUC_VERSION overrides the derived version (capping included);
    'off' / '0' disables the flag entirely.
    """
    env = os.environ.get(GNUC_ENV, "").strip()
    if env.lower() in ("0", "off", "none", "false"):
        return []
    ver = env or _gcc_version(driver)
    if not ver:
        return []
    try:
        major = int(ver.split(".")[0])
    except ValueError:
        major = 0
    floatn13, malloc_args = _glibc_probe(driver, cpp)
    if not env and major >= 11 and malloc_args and _libclang_major() < 21:
        ver, major = "10.9", 10
    flags = [f"-fgnuc-version={ver}"]
    if (major >= 7 and not cpp) or (major >= 13 and cpp and floatn13):
        flags += _FLOATN_ALIASES
    return flags


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
    gnuc = _gnuc_version_flag(driver, cpp=cpp)
    res = _resource_include()
    if res is None:
        # No clang builtin headers anywhere (pip-wheel libclang, no clang on
        # PATH, no CIDX_RESOURCE_DIR). Dropping the driver's builtin dirs
        # would make every <stddef.h> include fatal, so replicate the search
        # list verbatim instead: gcc's own stddef.h/stdarg.h parse fine under
        # libclang (only its intrinsics headers may not).
        _log.warning(
            "no clang builtin headers found (set %s or install clang); "
            "falling back to %s's own builtin headers", RESOURCE_ENV, driver
        )
        flags = ["-nostdinc", *gnuc]
        for d in dirs:
            flags += ["-isystem", d]
        return flags
    flags = ["-nostdinc", *gnuc]
    substituted = False
    for d in dirs:
        if _BUILTIN_DIR_RE.search(d):
            if not substituted:
                flags += ["-isystem", res]
                substituted = True
            continue
        flags += ["-isystem", d]
    if not substituted:
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


STRICT_ENV = "CIDX_STRICT"

#: Per-file cap on individual diagnostic lines written to the log, so one
#: rotten TU can't flood cidx.log.
_DIAG_LOG_CAP = 25


def _log_diagnostics(filename: str, diags: Sequence["cx.Diagnostic"]) -> None:
    """Write each diagnostic to the log at INFO -- the per-file summary line
    carries the WARNING/ERROR level, so the CLI's warning counter stays
    one-per-file instead of one-per-diagnostic."""
    for d in diags[:_DIAG_LOG_CAP]:
        _log.info("%s: diag %s:%d: %s",
                  filename, d.location.file, d.location.line, d.spelling)
    if len(diags) > _DIAG_LOG_CAP:
        _log.info("%s: ... %d more diagnostic(s) suppressed",
                  filename, len(diags) - _DIAG_LOG_CAP)


def fatal_diagnostics(tu: cx.TranslationUnit,
                      level: int | None = None) -> list[cx.Diagnostic]:
    """Diagnostics at/above `level` (default: severity >= ERROR)."""
    if level is None:
        level = cx.Diagnostic.Error
    return [d for d in tu.diagnostics if d.severity >= level]


def _abort_level() -> int:
    """Severity that aborts a parse.

    clang grades unrecoverable environment problems (a header not found)
    FATAL -- those truncate the AST and the parse must be rejected. Plain
    ERRORs are semantic disagreements (clang is stricter than the gcc the
    code was written for: eager template instantiation, gcc-only constructs
    behind compiler-detection guards); the AST around them is intact, so by
    default the indexer reports them and keeps the TU. $CIDX_STRICT=1
    restores abort-on-error.
    """
    strict = os.environ.get(STRICT_ENV, "").strip().lower()
    if strict in ("", "0", "off", "none", "false"):
        return cx.Diagnostic.Fatal
    return cx.Diagnostic.Error


#: CIDX_MEM=1 logs one per-TU memory line (clang_getCXTUResourceUsage) after a
#: successful parse. Pure observability: the libclang C API exposes no
#: allocator hook, only this usage breakdown + the dispose lifecycle.
MEM_ENV = "CIDX_MEM"


class _CXTUResourceUsageEntry(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_int), ("amount", ctypes.c_ulong)]


class _CXTUResourceUsage(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p),
                ("numEntries", ctypes.c_uint),
                ("entries", ctypes.POINTER(_CXTUResourceUsageEntry))]


#: Cached (get, dispose, name) ctypes bindings. clang.cindex does NOT register
#: clang_getCXTUResourceUsage, and its shared library handle carries a global
#: errcheck, so we bind these on our OWN handle to the same dylib.
_RU_FNS = None


def _resource_usage_fns():
    global _RU_FNS
    if _RU_FNS is None:
        lib = ctypes.CDLL(cx.conf.get_filename())
        get = lib.clang_getCXTUResourceUsage
        get.restype = _CXTUResourceUsage
        get.argtypes = [ctypes.c_void_p]
        dispose = lib.clang_disposeCXTUResourceUsage
        dispose.argtypes = [_CXTUResourceUsage]
        name = lib.clang_getTUResourceUsageName
        name.argtypes = [ctypes.c_int]
        name.restype = ctypes.c_char_p
        _RU_FNS = (get, dispose, name)
    return _RU_FNS


def _mem_reporting_enabled() -> bool:
    """CIDX_MEM truthy (same falsy spellings as CIDX_STRICT) -> emit the
    per-TU memory report. Default OFF, so a clean parse still writes nothing."""
    mem = os.environ.get(MEM_ENV, "").strip().lower()
    return mem not in ("", "0", "off", "none", "false")


def _log_resource_usage(filename: str, tu: cx.TranslationUnit) -> None:
    """Log one INFO line: total bytes (+ KiB) and every non-zero category from
    clang_getCXTUResourceUsage. All kinds 1..14 are MEMORY_IN_BYTES, so
    `amount` is bytes. The CX-owned buffer is disposed before returning."""
    get, dispose, name = _resource_usage_fns()
    usage = get(tu.obj)
    try:
        total = 0
        parts = []
        for i in range(usage.numEntries):
            entry = usage.entries[i]
            total += entry.amount
            if entry.amount:
                nm = name(entry.kind)
                parts.append(f"{nm.decode() if nm else '?'}={entry.amount}")
    finally:
        dispose(usage)
    _log.info("%s: TU memory total=%d bytes (%d KiB); %s",
              filename, total, total // 1024, ", ".join(parts))


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
    # -ferror-limit=0 lifts clang's default 20-error cap: hitting the cap
    # emits a FATAL 'too many errors emitted, stopping now' that aborts an
    # otherwise indexable TU while naming none of the real errors.
    flags = (list(args)
             + toolchain_flags(cpp=is_cpp(filename, args), driver=driver)
             + ["-ferror-limit=0"])
    index = cx.Index.create()
    try:
        tu = index.parse(filename, args=flags, options=options)
    except cx.TranslationUnitLoadError as e:
        raise ClangParseError(f"cannot parse {filename}") from e
    if check:
        level = _abort_level()
        fatals = fatal_diagnostics(tu, level)
        if fatals:
            summary = "; ".join(
                f"{d.location.file}:{d.location.line}: {d.spelling}"
                for d in fatals[:3]
            )
            # The flag dump is debugging detail -- log it, keep it out of the
            # exception message the CLI shows on screen.
            _log.error("%s: failed parse flags: %s; libclang: %s",
                       filename, " ".join(flags), _libclang_major() or "?")
            _log_diagnostics(filename, fatal_diagnostics(tu,
                                                         cx.Diagnostic.Error))
            raise ClangParseError(
                f"{filename}: {len(fatals)} fatal diagnostic(s): {summary}"
            )
        if level > cx.Diagnostic.Error:
            errors = fatal_diagnostics(tu, cx.Diagnostic.Error)
            if errors:
                _log.warning(
                    "%s: %d error diagnostic(s) ignored (%s=1 to abort)",
                    filename, len(errors), STRICT_ENV)
                _log_diagnostics(filename, errors)
    if _mem_reporting_enabled():
        _log_resource_usage(filename, tu)
    return tu

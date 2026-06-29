"""Live libclang (clang.cindex) exploration API for C/C++ codebases.

Self-contained — no dependency on any lab repo. Requires the libclang bindings
plus a loadable native dylib:

    pip install libclang        # bundles the dylib on most platforms

If you must point Python at a specific libclang (e.g. Homebrew LLVM), do it ONCE
before creating any Index:

    import clang.cindex as cx
    cx.Config.set_library_file("/opt/homebrew/opt/llvm/lib/libclang.dylib")

The point of this module: parse C/C++ *on demand* and answer structural questions
(symbols, types, calls, references, diagnostics) by querying the AST, instead of
reading or grepping source. Keep every query bounded and ground claims in
`file:line` from `loc()`.
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import clang.cindex as cx

# Source extensions we treat as parseable translation units.
SOURCE_EXTS = {".c", ".cc", ".cpp", ".cxx", ".c++", ".m", ".mm"}
HEADER_EXTS = {".h", ".hh", ".hpp", ".hxx", ".h++", ".inl", ".tcc", ".ipp"}


# --------------------------------------------------------------------------- #
# Gotcha #1 — header resolution. The pip `libclang` wheel ships the dylib but
# NOT Clang's builtin headers (stddef.h, stdarg.h, ...), and on macOS the SDK
# path is not on the default search path. A bare `args=[]` parse then emits a
# fatal "'stddef.h' file not found" that SILENTLY TRUNCATES the AST. clang_args()
# is the portable fix.
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _sysroot():
    """macOS SDK path (for <stdio.h>, <string>, ...). None elsewhere."""
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.check_output(
            ["xcrun", "--show-sdk-path"], text=True
        ).strip()
    except Exception:
        return None


@lru_cache(maxsize=1)
def _resource_include():
    """Clang's builtin-header dir (stddef.h, stdarg.h, ...), borrowed from
    whatever clang is on PATH."""
    for cc in ("clang", "clang++"):
        try:
            rd = subprocess.check_output(
                [cc, "-print-resource-dir"], text=True
            ).strip()
            inc = Path(rd) / "include"
            if (inc / "stddef.h").exists():
                return str(inc)
        except Exception:
            continue
    return None


def clang_args(std="c11", project_includes=(), defines=(), extra=()):
    """Compiler flags that let libclang resolve system + builtin headers.

    std:               language standard, e.g. "c11", "c17", "c++17", "c++20".
    project_includes:  dirs added with -I (your repo's include roots).
    defines:           macro defs, e.g. ("DEBUG", "MAX=64") -> -DDEBUG -DMAX=64.
    extra:             any other raw flags appended verbatim.

    Search-path order is C++-correct: -isysroot -> libc++ -> clang builtins, all
    via -isystem. Putting the builtin dir first (a plain `-I resource-dir`) breaks
    libc++'s <cstddef> include_next chain -- a fatal that silently truncates the
    C++ AST. For C, the libc++ leg is skipped.
    """
    cpp = std.startswith("c++") or std.startswith("gnu++")
    args = [f"-std={std}"]
    sysroot = _sysroot()
    if sysroot:
        args += ["-isysroot", sysroot]
        if cpp:
            args += ["-isystem", str(Path(sysroot) / "usr" / "include" / "c++" / "v1")]
    rinc = _resource_include()
    if rinc:
        args += ["-isystem", rinc]
    for inc in project_includes:
        args += ["-I", str(inc)]
    for d in defines:
        args.append(f"-D{d}")
    args += list(extra)
    return args


def parse(path, args=None, options=0, unsaved_files=None):
    """Parse a source file (or in-memory buffer) and return its TranslationUnit.

    args:           compiler flags. Pass clang_args(...) for a clean parse. Pass
                    a raw list (even []) to reproduce the no-header gotcha.
    options:        bitmask of cx.TranslationUnit.PARSE_* flags, e.g.
                    PARSE_DETAILED_PROCESSING_RECORD (macros/includes),
                    PARSE_SKIP_FUNCTION_BODIES (fast decl-only scan).
    unsaved_files:  [(name, source_str)] to parse a buffer without touching disk.
    """
    index = cx.Index.create()
    return index.parse(
        str(path),
        args=list(args or []),
        unsaved_files=unsaved_files,
        options=options,
    )


def fatal_diagnostics(tu):
    """Error/fatal diagnostics (severity >= ERROR). Empty list == clean parse.

    ALWAYS check this after parse() — a non-empty result means the AST may be
    truncated and your query results unreliable (usually a missing -I or -std).
    """
    return [d for d in tu.diagnostics if d.severity >= cx.Diagnostic.Error]


def diagnostics(tu, min_severity=cx.Diagnostic.Warning):
    """All diagnostics at or above `min_severity`, as compact dicts."""
    out = []
    for d in tu.diagnostics:
        if d.severity < min_severity:
            continue
        sev = {0: "ignored", 1: "note", 2: "warning", 3: "error", 4: "fatal"}.get(
            d.severity, str(d.severity)
        )
        out.append({"severity": sev, "spelling": d.spelling, "location": _locstr(d.location)})
    return out


# --------------------------------------------------------------------------- #
# Traversal + location
# --------------------------------------------------------------------------- #
def walk(cursor, depth=0):
    """Yield (cursor, depth) for `cursor` and every descendant, pre-order."""
    yield cursor, depth
    for child in cursor.get_children():
        yield from walk(child, depth + 1)


def _locstr(location):
    if location is None or location.file is None:
        return "<builtin>"
    return f"{location.file.name}:{location.line}:{location.column}"


def loc(cursor):
    """Compact 'path:line:col' for a cursor's start location ('<builtin>' if none).

    Use this to ground every structural claim in a real source site.
    """
    return _locstr(cursor.location)


def in_main_file(cursor):
    """True if the cursor lives in the file we parsed (not a pulled-in #include).

    Gotcha #2: parsing pulls in headers, so an #include'd prototype and the .c
    definition are two cursors with the same spelling. Filter with this, then
    resolve declaration-vs-definition with is_definition()/get_definition().
    """
    f = cursor.location.file
    return f is not None and f.name == cursor.translation_unit.spelling


def top_level(tu):
    """Yield direct children of the TU that originate in the main file."""
    for child in tu.cursor.get_children():
        if in_main_file(child):
            yield child


# --------------------------------------------------------------------------- #
# Higher-level queries over a single TranslationUnit
# --------------------------------------------------------------------------- #
def dump_ast(tu_or_cursor, max_depth=6, main_only=True):
    """Return an indented AST dump string: '<indent>KIND spelling  [type]  loc'."""
    cursor = tu_or_cursor.cursor if isinstance(tu_or_cursor, cx.TranslationUnit) else tu_or_cursor
    lines = []
    for c, depth in walk(cursor):
        if max_depth is not None and depth > max_depth:
            continue
        if main_only and c.location.file is not None and not in_main_file(c):
            continue
        type_part = f"  [{c.type.spelling}]" if c.type and c.type.kind != cx.TypeKind.INVALID else ""
        lines.append(f"{'  ' * depth}{c.kind.name} {c.spelling!r}{type_part}  {loc(c)}")
    return "\n".join(lines)


def find_symbols(tu, pattern="*", kinds=None, main_only=True):
    """Cursors whose spelling matches glob `pattern`, optionally filtered by kind.

    kinds:  iterable of cx.CursorKind (e.g. [cx.CursorKind.FUNCTION_DECL]).
    Returns Cursor objects — read .spelling/.displayname/.type/.get_usr()/loc().
    """
    kinds = set(kinds) if kinds else None
    out = []
    for c, _ in walk(tu.cursor):
        if main_only and c.location.file is not None and not in_main_file(c):
            continue
        if kinds is not None and c.kind not in kinds:
            continue
        if not c.spelling:
            continue
        if fnmatch.fnmatch(c.spelling, pattern):
            out.append(c)
    return out


def callees_of(tu, func_name):
    """Within `tu`: (callee_spelling, callee_usr, site) for each call made by
    function `func_name`. Grounds 'X calls Y' in a file:line site."""
    out = []
    for c, _ in walk(tu.cursor):
        if c.kind != cx.CursorKind.FUNCTION_DECL and c.kind != cx.CursorKind.CXX_METHOD:
            continue
        if c.spelling != func_name or not c.is_definition():
            continue
        for sub, _ in walk(c):
            if sub.kind == cx.CursorKind.CALL_EXPR:
                ref = sub.referenced
                out.append(
                    (sub.spelling, ref.get_usr() if ref else None, loc(sub))
                )
    return out


def callers_of(tu, func_name):
    """Within `tu`: (caller_spelling, site) for each call TO `func_name`.

    NOTE: this only sees callers inside THIS translation unit. For repo-wide
    callers, parse every TU and match by USR (references_to), or use the
    cidx-graph skill's prebuilt index. Pure-libclang repo scans are expensive.
    """
    out = []

    def enclosing(cursor):
        p = cursor.semantic_parent
        while p is not None and p.kind not in (
            cx.CursorKind.FUNCTION_DECL,
            cx.CursorKind.CXX_METHOD,
            cx.CursorKind.FUNCTION_TEMPLATE,
            cx.CursorKind.CONSTRUCTOR,
            cx.CursorKind.DESTRUCTOR,
        ):
            p = p.semantic_parent
        return p

    for c, _ in walk(tu.cursor):
        if c.kind == cx.CursorKind.CALL_EXPR and c.spelling == func_name:
            owner = enclosing(c)
            out.append((owner.spelling if owner else "<file-scope>", loc(c)))
    return out


def references_to(tus, usr):
    """Cross-TU references to a symbol identified by USR.

    tus:  iterable of already-parsed TranslationUnits.
    usr:  a Unified Symbol Resolution string (stable across files) — get it from
          some_cursor.get_usr() or cursor.referenced.get_usr().
    Returns (kind_name, site) for every cursor that references that USR.
    USR is the right cross-file key: it is TU-invariant for the same entity.
    """
    out = []
    for tu in tus:
        for c, _ in walk(tu.cursor):
            ref = c.referenced
            if ref is not None and ref.get_usr() == usr:
                out.append((c.kind.name, loc(c)))
            elif c.get_usr() == usr and c.is_definition():
                out.append(("DEFINITION", loc(c)))
    return out


# --------------------------------------------------------------------------- #
# Repo — flag resolution across a project (compile_commands.json aware)
# --------------------------------------------------------------------------- #
def _strip_compile_command(arguments, filename):
    """Gotcha #3: raw compile_commands.json args carry the driver token (cc),
    the source filename, and -c / -o output pairs that index.parse() does NOT
    want. Strip them; keep -I/-D/-std/-isystem/etc."""
    args = list(arguments)
    if args:
        args = args[1:]  # drop the driver (cc / clang / g++)
    cleaned = []
    skip_next = False
    fname = Path(filename).name
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a == "-c":
            continue
        if a == "-o":
            skip_next = True
            continue
        if a.endswith(fname) and not a.startswith("-"):
            continue
        cleaned.append(a)
    return cleaned


class Repo:
    """A C/C++ project root. Resolves per-file compile flags from a
    compile_commands.json when one is present, else falls back to clang_args().

    Parse is on demand and bounded — this is for targeted exploration, not
    indexing the whole tree. For repo-scale graph queries (all callers across a
    large codebase, hierarchy, reachability) prefer the cidx-graph skill.
    """

    def __init__(self, root, std="c++17", project_includes=(), compdb_dir=None):
        self.root = Path(root).resolve()
        self.std = std
        self.project_includes = [str(p) for p in project_includes]
        self._cdb = None
        self._cmd_by_file = {}
        cdb_dir = Path(compdb_dir) if compdb_dir else self.root
        try:
            self._cdb = cx.CompilationDatabase.fromDirectory(str(cdb_dir))
            for cmd in self._cdb.getAllCompileCommands():
                self._cmd_by_file[str(Path(cmd.directory) / cmd.filename)] = cmd
                self._cmd_by_file[Path(cmd.filename).name] = cmd
        except Exception:
            self._cdb = None

    def compile_args(self, file):
        """Best compile flags for `file`: stripped compdb entry if available,
        else clang_args() with the repo's includes."""
        f = Path(file)
        key = str(f.resolve()) if f.exists() else str(f)
        cmd = self._cmd_by_file.get(key) or self._cmd_by_file.get(f.name)
        if cmd is not None:
            return _strip_compile_command(cmd.arguments, cmd.filename)
        return clang_args(std=self.std, project_includes=self.project_includes)

    def parse(self, file, options=0, unsaved_files=None):
        """Parse one file with its resolved flags. Check fatal_diagnostics()!"""
        return parse(
            file,
            args=self.compile_args(file),
            options=options,
            unsaved_files=unsaved_files,
        )

    def sources(self, pattern="**/*"):
        """Translation-unit source files under root (.c/.cpp/...), as paths."""
        return [
            p for p in self.root.glob(pattern)
            if p.is_file() and p.suffix.lower() in SOURCE_EXTS
        ]

    def find(self, name_pattern, kinds=None, files=None, main_only=True, limit=200):
        """Search symbols across `files` (default: all sources — be careful, this
        parses each one). Returns dicts: {spelling, kind, usr, displayname, loc}.

        ALWAYS pass `files=[...]` to scope the parse when the repo is large."""
        targets = [Path(f) for f in files] if files else self.sources()
        out = []
        for f in targets:
            tu = self.parse(f)
            for c in find_symbols(tu, name_pattern, kinds=kinds, main_only=main_only):
                out.append({
                    "spelling": c.spelling,
                    "kind": c.kind.name,
                    "usr": c.get_usr(),
                    "displayname": c.displayname,
                    "loc": loc(c),
                })
                if len(out) >= limit:
                    return out
        return out


def open_repo(root, **kw):
    """Convenience constructor mirroring open_graph() in the cidx-graph skill."""
    return Repo(root, **kw)

"""indexer.compiledb -- load compile_commands.json and strip args for parse().

Wraps clang's CompilationDatabase (lab section 4.5): the raw command contains
the driver token, the source filename, and -c/-o pairs that libclang.parse()
must NOT see; relative -I paths are resolved against the command's directory.
"""

from __future__ import annotations

import os
import re
import tempfile

from clang.cindex import CompilationDatabase

from indexer import pathx as _pathx
from indexer.pathx import resolve_fs_path  # noqa: F401
from indexer.pathx import split_base_version  # re-exported for CLI use  # noqa: F401


def commands_from_text(text: str):
    """Parse compile_commands.json text into clang compile-command objects.

    Accepts either a single entry object ('{...}') or a full array ('[...]');
    a lone object is wrapped in an array. The text is written to a throwaway
    compile_commands.json and loaded through the same CompilationDatabase path
    `import` uses, so `cidx file -import-args` strips args identically. Each
    entry needs `directory`, `file`, and `arguments` (or `command`)."""
    body = text.lstrip()
    if body.startswith("{"):
        text = "[" + text + "]"
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "compile_commands.json"), "w") as fh:
            fh.write(text)
        return load_commands(d)


def db_directory(db_path: str) -> str:
    """Absolute directory holding compile_commands.json (db_path is the file or its dir)."""
    db_dir = (
        db_path[: -len("compile_commands.json")] or "."
        if db_path.endswith("compile_commands.json")
        else db_path
    )
    return os.path.abspath(db_dir)


def load_commands(db_path: str):
    """All compile commands from a compile_commands.json (file or its directory)."""
    cdb = CompilationDatabase.fromDirectory(db_directory(db_path))
    return list(cdb.getAllCompileCommands())


def _abs(p: str, base: str) -> str:
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))


#: A leading token of the form NAME=value is an environment-variable assignment
#: (e.g. CCACHE_DIR=/x, CCACHE_AOPS-52378DIR=). The name must start with a
#: letter/underscore so a flag like -DFOO=bar (starts with '-') is never matched.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*=", re.ASCII)

#: Compiler-launcher wrappers that sit BEFORE the real compiler in a command
#: (matched by basename). libclang must see neither these nor the env
#: assignments — clang reports them as "'linker' input unused", and the driver
#: must point at the actual compiler.
_LAUNCHERS = frozenset(
    {"ccache", "sccache", "distcc", "icecc", "icerun", "env", "time", "nice"}
)


def command_start(args: list[str]) -> int:
    """Index of the real compiler driver in a raw command vector.

    A compile_commands.json `command` may be prefixed with environment-variable
    assignments (CCACHE_DIR=..., FOO=bar) and compiler-launcher wrappers
    (ccache, sccache, distcc, icecc, env, time, nice) before the actual
    compiler. Returns the index of the first token that is neither an env
    assignment nor a known launcher (0 for a plain `cc ...`). If the whole
    vector is prefix (degenerate), returns 0 so callers stay in bounds.
    """
    i = 0
    n = len(args)
    while i < n:
        tok = args[i]
        if _ENV_ASSIGN_RE.match(tok):
            i += 1
            continue
        if os.path.basename(tok) in _LAUNCHERS:
            i += 1
            continue
        break
    return i if i < n else 0


#: Flags libclang must not see, beyond the driver/source/-c/-o basics.
#: Dependency generation (-M*) writes build artifacts -- the obj/dep dir
#: usually does not exist outside a real build, which surfaces as a fatal
#: "error opening '...'" diagnostic. -Werror (and friends) promote benign
#: warnings to errors, and the indexer treats error diagnostics as a failed
#: parse -- a warning gcc never emitted must not abort indexing under clang.
#:
#: We also drop everything that only affects LINKING, CODEGEN-output, or the
#: MODULE/diagnostic CACHE: a compile_commands.json entry carries the full
#: driver invocation (link libraries, linker scripts, the module cache dir,
#: assembler passthrough ...), none of which influence the AST a libclang
#: *parse* produces -- and several of them (a missing -L dir, a -Wl, group,
#: a stale module cache) only add noise or fatal diagnostics. Header-search
#: flags (-nostdinc / -nostdinc++) and preprocessor-affecting flags
#: (-pthread, -fPIC) are NOT linker flags and are deliberately KEPT.
_DROP = frozenset(
    {
        "-c",
        "--",
        # -- dependency generation --
        "-M",
        "-MM",
        "-MD",
        "-MMD",
        "-MG",
        "-MP",
        "-MV",
        # -- warnings-as-errors --
        "-Werror",
        "-pedantic-errors",
        # -- link-stage modes (no effect on parsing) --
        "-shared",
        "-static",
        "-rdynamic",
        "-pie",
        "-no-pie",
        "-s",  # strip symbols at link
        "-pipe",  # use pipes instead of temp files (codegen plumbing)
        "-nostdlib",
        "-nodefaultlibs",
        "-nostartfiles",
        "-static-libgcc",
        "-shared-libgcc",
        "-static-libstdc++",
    }
)
_DROP_WITH_ARG = frozenset(
    {
        "-o",
        "-MF",
        "-MT",
        "-MQ",
        "-dependency-file",
        "--serialize-diagnostics",
        # -- linker options that take a separate argument --
        "-Xlinker",
        "-T",  # linker script
        "-L",  # space form: -L /usr/lib
        "-l",  # space form: -l foo
    }
)
_DROP_PREFIX = (
    "-Werror=",  # -Werror=return-type: keep it a plain warning
    "-Wp,-M",  # -Wp,-MD,<file> / -Wp,-MMD,<file>
    "-MF",
    "-MT",
    "-MQ",  # glued forms: -MF<file> etc.
    # -- linker / library / cache (glued forms) --
    "-l",  # -lfoo  (no frontend flag starts with -l)
    "-L",  # -L/usr/lib (no frontend flag starts with -L)
    "-Wl,",  # linker passthrough
    "-Wa,",  # assembler passthrough
    "-fuse-ld=",  # linker selection
    "-fmodules-cache-path=",  # module/diagnostic cache dir
)


def sanitize(args: list[str]) -> list[str]:
    """Re-apply the drop rules to already-stored options.

    Options are stripped at import time, so an index written by an older
    version may still carry flags the current rules would drop (-Werror,
    -MF ...); sanitizing again at parse time heals such databases without
    a re-import.

    Also heals a command PREFIX an older import stored when argv[0] was an
    env-var assignment rather than the compiler (e.g. CCACHE_DIR=... was
    dropped, leaving ["CCACHE_COMPRESS=1", ..., "ccache", "g++", "-g", ...]).
    When the first stored token is an env assignment or a launcher, everything
    up to and including the real compiler is dropped. (A re-import is still the
    proper fix — it also corrects the stored driver field.)
    """
    items = list(args)
    if items and (
        _ENV_ASSIGN_RE.match(items[0])
        or os.path.basename(items[0]) in _LAUNCHERS
    ):
        items = items[command_start(items) + 1 :]
    out: list[str] = []
    it = iter(items)
    for tok in it:
        if tok in _DROP:
            continue
        if tok in _DROP_WITH_ARG:
            next(it, None)
            continue
        if tok.startswith(_DROP_PREFIX):
            continue
        out.append(tok)
    return out


def _is_indirected(val: str) -> bool:
    """True if a -I/-isystem/-iquote value already contains a label or env-var.

    Such values must be preserved verbatim (not absolutized) so the stored
    indirected form survives import unchanged and resolves at parse time.
    """
    return "<" in val or "$" in val


def strip_for_libclang(cmd) -> list[str]:
    """Raw driver invocation -> flags parse() wants. Resolves relative includes.

    Preserve rule (v14): if a -I/-isystem/-iquote value already contains '<'
    or '$', emit it verbatim (do NOT absolutize). Otherwise keep the existing
    absolutize-relative behaviour.
    """
    raw, directory = list(cmd.arguments), cmd.directory
    src = {cmd.filename, os.path.basename(cmd.filename)}
    out: list[str] = []
    # Drop the whole command prefix: leading env-var assignments + launcher
    # wrappers (ccache ...) AND the real compiler token at command_start.
    it = iter(raw[command_start(raw) + 1 :])
    for tok in it:
        if tok in _DROP:
            continue
        if tok in _DROP_WITH_ARG:
            next(it, None)  # drop flag + its argument
            continue
        if tok.startswith(_DROP_PREFIX):
            continue
        if tok in src:
            continue
        matched = False
        for flag in ("-I", "-isystem", "-iquote"):
            if tok == flag:  # space form: -I path
                val = next(it, "")
                if _is_indirected(val):
                    out += [flag, val]  # preserve verbatim
                else:
                    out += [flag, _abs(val, directory)]
                matched = True
                break
            if tok.startswith(flag) and len(tok) > len(flag):  # glued: -Ipath
                val = tok[len(flag) :]
                if _is_indirected(val):
                    out.append(tok)  # preserve verbatim
                else:
                    out.append(flag + _abs(val, directory))
                matched = True
                break
        if not matched:
            out.append(tok)
    return out


def source_path(cmd) -> str:
    """Absolute path of the command's source file."""
    return _abs(cmd.filename, cmd.directory)


def driver(cmd) -> str:
    """The command's compiler driver (argv[0]).

    A custom-toolchain driver (e.g. /opt/1A/toolchain/.../bin/g++) carries its
    own header search paths; storing it lets parse() replicate them later.
    Bare names ('cc', 'g++') are kept as-is and resolved via PATH at parse
    time; relative paths are resolved against the command's directory. Leading
    env-var assignments and launcher wrappers (CCACHE_DIR=... ccache g++) are
    skipped so the REAL compiler is returned, not the env prefix.
    """
    args = list(cmd.arguments)
    if not args:
        return ""
    argv0 = args[command_start(args)]
    return _abs(argv0, cmd.directory) if os.sep in argv0 else argv0


# ---------------------------------------------------------------------------
# Include-path aliasing (v0.6.0): encode absolute -I dirs <-> <label> tokens.
#
# Two paired transforms over the -I/-isystem/-iquote VALUES only (both the
# 'space' form `-I path` and the 'glued' form `-Ipath`); every other token
# passes through untouched:
#
#   alias_options   ENCODE  absolute dir  -> <label>   (registry, longest match)
#   resolve_options DECODE  <label>/$VAR  -> absolute  (parse-time, for libclang)
#
# Stored compile_options keep the indirected (aliased) form so the index is
# portable; resolve_options is applied just before handing args to libclang.
# ---------------------------------------------------------------------------

_INCLUDE_FLAGS = ("-I", "-isystem", "-iquote")


def _map_include_values(options, fn) -> list[str]:
    """Return a copy of *options* with *fn* applied to each include-path value.

    Handles `-I path` (two tokens) and `-Ipath` (glued); all other tokens are
    copied verbatim. fn receives the bare path value and returns its rewrite.
    """
    out: list[str] = []
    it = iter(options)
    for tok in it:
        matched = False
        for flag in _INCLUDE_FLAGS:
            if tok == flag:  # space form: -I path
                out += [flag, fn(next(it, ""))]
                matched = True
                break
            if tok.startswith(flag) and len(tok) > len(flag):  # glued: -Ipath
                out.append(flag + fn(tok[len(flag) :]))
                matched = True
                break
        if not matched:
            out.append(tok)
    return out


def include_values(options):
    """Yield each include-path VALUE from *options* (both `-I path` and `-Ipath`
    forms), skipping every other token. Read-only counterpart of
    _map_include_values, used by the import version-bump scan."""
    it = iter(options)
    for tok in it:
        for flag in _INCLUDE_FLAGS:
            if tok == flag:
                yield next(it, "")
                break
            if tok.startswith(flag) and len(tok) > len(flag):
                yield tok[len(flag) :]
                break


def resolve_options(options, lookup=None, autoderive: bool = True) -> list[str]:
    """DECODE: resolve <label>/$VAR/~ in include-path values to absolute paths.

    Only values that look indirected (contain '<' or '$' or start with '~') are
    resolved (via the full resolution chain + abspath); already-absolute plain
    paths are left untouched. Used at parse/index time so libclang sees real
    directories.
    """

    def _fn(val: str) -> str:
        if "<" in val or "$" in val or val.startswith("~"):
            return os.path.abspath(
                resolve_fs_path(val, lookup=lookup, autoderive=autoderive)
            )
        return val

    return _map_include_values(options, _fn)


def build_label_map(labels, lookup=None) -> list[tuple[str, str, bool]]:
    """Build the encode lookup from (name, stored_path[, versioned]) entries.

    Accepts 2-tuples (label, exact match) or 3-tuples (the *versioned* flag, set
    for components — version-agnostic match). Each stored path is resolved to an
    absolute directory (env-vars expanded, NO autoderive). Returned as
    (name, resolved_path, versioned) sorted most-specific first (longest
    resolved path, then name) so the longest prefix wins deterministically in
    both Python and C++.
    """
    out: list[tuple[str, str, bool]] = []
    for entry in labels:
        if len(entry) == 3:
            name, stored, versioned = entry
        else:
            name, stored = entry
            versioned = False
        rp = os.path.abspath(resolve_fs_path(stored, lookup=lookup, autoderive=False))
        out.append((name, rp, versioned))
    out.sort(key=lambda nr: (-len(nr[1]), nr[0]))
    return out


def match_alias(absval, label_map):
    """Longest-match an absolute path against *label_map*.

    Returns (name, version_segment, remainder) for the first (longest) entry
    whose resolved dir equals or contains *absval*, else None. For a *versioned*
    (component) entry the segment immediately after the base, if it looks like a
    version, is captured as *version_segment* and excluded from *remainder* (so
    the stored portable token carries NO version — it is re-injected at decode
    by get_alias -> base + highest version). For label entries version_segment
    is always None.
    """
    for name, rp, versioned in label_map:
        if absval == rp or absval.startswith(rp + os.sep):
            rem = absval[len(rp) :]
            vseg = None
            if versioned and rem.startswith(os.sep):
                head = rem[1:].split(os.sep, 1)
                if head[0] and _pathx.is_version_segment(head[0]):
                    vseg = head[0]
                    rem = (os.sep + head[1]) if len(head) > 1 else ""
            return name, vseg, rem
    return None


def alias_options(options, label_map) -> list[str]:
    """ENCODE: rewrite absolute include-path values to <label> tokens.

    *label_map* is the output of build_label_map (sorted longest-first). A value
    equal to or under an entry's resolved directory becomes `<name>` + remainder
    (longest match wins; for component entries the version segment is stripped).
    Values already indirected ('<' or '$') and relative values are unchanged.
    """

    def _fn(val: str) -> str:
        if "<" in val or "$" in val or not os.path.isabs(val):
            return val
        m = match_alias(os.path.normpath(val), label_map)
        if m is None:
            return val
        name, _vseg, rem = m
        return "<" + name + ">" + rem

    return _map_include_values(options, _fn)

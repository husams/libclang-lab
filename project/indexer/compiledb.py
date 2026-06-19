"""indexer.compiledb -- load compile_commands.json and strip args for parse().

Wraps clang's CompilationDatabase (lab section 4.5): the raw command contains
the driver token, the source filename, and -c/-o pairs that libclang.parse()
must NOT see; relative -I paths are resolved against the command's directory.
"""

from __future__ import annotations

import os
import tempfile

from clang.cindex import CompilationDatabase

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


#: Flags libclang must not see, beyond the driver/source/-c/-o basics.
#: Dependency generation (-M*) writes build artifacts -- the obj/dep dir
#: usually does not exist outside a real build, which surfaces as a fatal
#: "error opening '...'" diagnostic. -Werror (and friends) promote benign
#: warnings to errors, and the indexer treats error diagnostics as a failed
#: parse -- a warning gcc never emitted must not abort indexing under clang.
_DROP = frozenset(
    {
        "-c",
        "--",
        "-M",
        "-MM",
        "-MD",
        "-MMD",
        "-MG",
        "-MP",
        "-MV",
        "-Werror",
        "-pedantic-errors",
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
    }
)
_DROP_PREFIX = (
    "-Werror=",  # -Werror=return-type: keep it a plain warning
    "-Wp,-M",  # -Wp,-MD,<file> / -Wp,-MMD,<file>
    "-MF",
    "-MT",
    "-MQ",  # glued forms: -MF<file> etc.
)


def sanitize(args: list[str]) -> list[str]:
    """Re-apply the drop rules to already-stored options.

    Options are stripped at import time, so an index written by an older
    version may still carry flags the current rules would drop (-Werror,
    -MF ...); sanitizing again at parse time heals such databases without
    a re-import.
    """
    out: list[str] = []
    it = iter(args)
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
    it = iter(raw[1:])  # drop argv[0] (the driver)
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
    time; relative paths are resolved against the command's directory.
    """
    argv0 = list(cmd.arguments)[0]
    return _abs(argv0, cmd.directory) if os.sep in argv0 else argv0

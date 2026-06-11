"""indexer.compiledb -- load compile_commands.json and strip args for parse().

Wraps clang's CompilationDatabase (lab section 4.5): the raw command contains
the driver token, the source filename, and -c/-o pairs that libclang.parse()
must NOT see; relative -I paths are resolved against the command's directory.
"""

from __future__ import annotations

import os

from clang.cindex import CompilationDatabase


def load_commands(db_path: str):
    """All compile commands from a compile_commands.json (file or its directory)."""
    db_dir = db_path[:-len("compile_commands.json")] or "." \
        if db_path.endswith("compile_commands.json") else db_path
    cdb = CompilationDatabase.fromDirectory(os.path.abspath(db_dir))
    return list(cdb.getAllCompileCommands())


def _abs(p: str, base: str) -> str:
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))


def strip_for_libclang(cmd) -> list[str]:
    """Raw driver invocation -> flags parse() wants. Resolves relative includes."""
    raw, directory = list(cmd.arguments), cmd.directory
    src = {cmd.filename, os.path.basename(cmd.filename)}
    out: list[str] = []
    it = iter(raw[1:])                          # drop argv[0] (the driver)
    for tok in it:
        if tok in ("-c", "--"):
            continue
        if tok == "-o":
            next(it, None)                      # drop flag + its argument
            continue
        if tok in src:
            continue
        matched = False
        for flag in ("-I", "-isystem", "-iquote"):
            if tok == flag:                     # space form: -I path
                out += [flag, _abs(next(it, ""), directory)]
                matched = True
                break
            if tok.startswith(flag) and len(tok) > len(flag):   # glued: -Ipath
                out.append(flag + _abs(tok[len(flag):], directory))
                matched = True
                break
        if not matched:
            out.append(tok)
    return out


def source_path(cmd) -> str:
    """Absolute path of the command's source file."""
    return _abs(cmd.filename, cmd.directory)

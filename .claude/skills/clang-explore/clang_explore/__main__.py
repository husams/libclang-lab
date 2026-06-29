"""Quick spot-check CLI for the clang_explore module.

    PYTHONPATH=/Users/husam/workspace/qemu-vms/libclang-lab/.claude/skills/clang-explore python3 -m clang_explore <cmd> ...

Commands:
    ast     <file> [--std S] [--depth N] [--all]      indented AST dump
    symbols <file> [--pattern P] [--kind K] [--std S]  matching symbols
    diag    <file> [--std S]                           parse diagnostics
    callees <file> <func> [--std S]                    calls made BY func
    callers <file> <func> [--std S]                    calls TO func (this TU)
    find    <dir>  <pattern> [--kind K] [--limit N]    search repo sources

--std defaults to c11 for .c, c++17 otherwise. --kind is a CursorKind name
(e.g. FUNCTION_DECL, CLASS_DECL, CXX_METHOD).
"""
import sys

import clang.cindex as cx

from .core import (
    Repo,
    clang_args,
    parse,
    dump_ast,
    find_symbols,
    diagnostics,
    fatal_diagnostics,
    callees_of,
    callers_of,
    loc,
)


def _opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) else default
    return default


def _flag(argv, name):
    return name in argv


def _std_for(path, argv):
    s = _opt(argv, "--std")
    if s:
        return s
    return "c11" if str(path).endswith(".c") else "c++17"


def _kinds(argv):
    k = _opt(argv, "--kind")
    if not k:
        return None
    return [getattr(cx.CursorKind, k.upper())]


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]

    if cmd == "ast":
        path = rest[0]
        tu = parse(path, args=clang_args(std=_std_for(path, rest)))
        fatal = fatal_diagnostics(tu)
        if fatal:
            print("WARNING fatal diagnostics (AST may be truncated):")
            for d in fatal:
                print("  ", d.spelling, "@", loc(d) if hasattr(d, "location") else "?")
        depth = _opt(rest, "--depth")
        print(dump_ast(tu, max_depth=int(depth) if depth else 6,
                       main_only=not _flag(rest, "--all")))
        return 0

    if cmd == "symbols":
        path = rest[0]
        tu = parse(path, args=clang_args(std=_std_for(path, rest)))
        pat = _opt(rest, "--pattern", "*")
        for c in find_symbols(tu, pat, kinds=_kinds(rest)):
            print(f"{c.kind.name:20} {c.displayname or c.spelling:30} {loc(c)}")
        return 0

    if cmd == "diag":
        path = rest[0]
        tu = parse(path, args=clang_args(std=_std_for(path, rest)))
        for d in diagnostics(tu):
            print(f"{d['severity']:8} {d['spelling']}  @ {d['location']}")
        return 0

    if cmd in ("callees", "callers"):
        path, func = rest[0], rest[1]
        tu = parse(path, args=clang_args(std=_std_for(path, rest)))
        fn = callees_of if cmd == "callees" else callers_of
        for row in fn(tu, func):
            print("  ", *row)
        return 0

    if cmd == "find":
        root, pattern = rest[0], rest[1]
        repo = Repo(root)
        limit = int(_opt(rest, "--limit", "200"))
        for r in repo.find(pattern, kinds=_kinds(rest), limit=limit):
            print(f"{r['kind']:20} {r['spelling']:30} {r['loc']}")
        return 0

    print(f"unknown command: {cmd}\n{__doc__}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

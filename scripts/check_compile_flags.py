#!/usr/bin/env python3
"""Check that every TU in the compilation database shares the same PCH-relevant
compile flags, so one shared (system) PCH would be valid for the whole project.

A PCH built from project A is only reusable in project B when their preprocessor
and language flags match. This script compares the flags that actually affect
PCH validity and reports whether they are uniform across every TU.

Compared (PCH-relevant):  -std, -D/-U, -f*, -m*, -W*, -O*, target flags, ...
Excluded (per request):
  * include paths   -I / -iquote / -isystem / -idirafter (and joined -I<path>)
  * linker options  -l / -L / -Wl, / -Xlinker / -shared / -static / -rdynamic
  * build noise     driver token (cc/c++), source file, -c, -o <out>, --

Exit 0 if all TUs agree (one shared PCH is valid), 1 otherwise.

Usage:
    python3 check_compile_flags.py [PATH]

PATH is your project's compile_commands.json, or the directory containing it.
If omitted, it falls back to the lab's sample DB (libclang-lab/manifests).
"""
import os
import sys
from collections import defaultdict

import clang.cindex as cx
from _helpers import MANIFESTS


def db_dir_from_arg(argv):
    """Resolve the directory that holds compile_commands.json from CLI args."""
    if len(argv) < 2:
        return str(MANIFESTS)             # fallback: lab sample DB
    path = os.path.abspath(argv[1])
    if os.path.isfile(path):              # passed the json file itself
        return os.path.dirname(path)
    return path                           # passed the containing directory

# Flags that consume the FOLLOWING token (drop both — all are noise/linker/dep).
_TAKES_VALUE = {"-o", "-include", "-Xlinker", "-MT", "-MF"}
# Standalone tokens to drop (compile-only marker + linker options).
_DROP_EXACT = {"-c", "--", "-shared", "-static", "-rdynamic", "-pthread"}
# Joined-prefix forms to drop (include paths + linker options).
_DROP_PREFIX = ("-I", "-L", "-l", "-Wl,", "-iquote", "-isystem", "-idirafter")


def pch_relevant_flags(cmd):
    """Return the sorted tuple of flags that affect PCH validity for one command."""
    raw = list(cmd.arguments)
    src_base = os.path.basename(cmd.filename)
    keep, skip_next = [], False
    for i, a in enumerate(raw):
        if skip_next:                 # value of the previous flag (e.g. -o app.o)
            skip_next = False
            continue
        if i == 0:                    # driver token: cc / c++ / clang
            continue
        # the source file itself (command may name it relative while `file` is absolute)
        if a == cmd.filename or (not a.startswith("-") and os.path.basename(a) == src_base):
            continue
        if a in _DROP_EXACT:
            continue
        if a in _TAKES_VALUE:
            skip_next = True
            continue
        if a.startswith(_DROP_PREFIX):
            continue
        keep.append(a)
    return tuple(sorted(keep))


def main():
    db_dir = db_dir_from_arg(sys.argv)
    if not os.path.isfile(os.path.join(db_dir, "compile_commands.json")):
        print(f"error: no compile_commands.json in {db_dir}", file=sys.stderr)
        return 2
    cdb = cx.CompilationDatabase.fromDirectory(db_dir)
    cmds = sorted(cdb.getAllCompileCommands(), key=lambda c: c.filename)

    groups = defaultdict(list)
    for c in cmds:
        groups[pch_relevant_flags(c)].append(c.filename)

    print(f"Checked {len(cmds)} TUs in {db_dir}")
    print(f"Distinct PCH-relevant flag sets: {len(groups)}\n")
    for flags, files in sorted(groups.items()):
        print(f"  flags: {list(flags) if flags else '(none)'}")
        for f in sorted(files):
            print(f"      {f}")
        print()

    if len(groups) == 1:
        print("CONSISTENT — one shared PCH is valid for every TU.")
        return 0
    print(f"INCONSISTENT — {len(groups)} flag groups; a single shared PCH is NOT valid.")
    print("Each group above needs its own PCH (or reconcile the flags).")
    return 1


if __name__ == "__main__":
    sys.exit(main())

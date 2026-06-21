#!/usr/bin/env python3
"""Check that every TU in a cidx index shares the same PCH-relevant compile
flags, so one shared (system) PCH would be valid for the whole index.

Reads the already-imported compile flags straight from the cidx index.db
(file.compile_options) — no compile_commands.json or libclang needed.

A PCH is only reusable across TUs whose preprocessor and language flags match.
This compares the flags that affect PCH validity and reports whether they are
uniform across every indexed TU.

Compared (PCH-relevant):  -std, -D/-U, -f*, -m*, -W*, -O*, --driver-mode, ...
Excluded (per request):
  * include paths   -I / -iquote / -isystem / -idirafter (and joined -I<path>)
  * linker options  -l / -L / -Wl, / -Xlinker / -shared / -static / -rdynamic
(file.compile_options is already stripped of the driver token, source file,
 -c, and -o <out> at import time.)

Exit 0 if all TUs agree (one shared PCH is valid), 1 otherwise, 2 if no index.

Usage:
    python3 check_compile_flags.py [INDEX_DB]

INDEX_DB defaults to the standard index at ~/.cache/cidx/index.db.
"""
import json
import os
import sqlite3
import sys
from collections import defaultdict

DEFAULT_DB = os.path.expanduser("~/.cache/cidx/index.db")

# Flags that consume the FOLLOWING token (drop both — all linker/dep noise).
_TAKES_VALUE = {"-Xlinker", "-MT", "-MF"}
# Standalone tokens to drop (linker options).
_DROP_EXACT = {"-shared", "-static", "-rdynamic", "-pthread"}
# Joined-prefix forms to drop (include paths + linker options).
_DROP_PREFIX = ("-I", "-L", "-l", "-Wl,", "-iquote", "-isystem", "-idirafter")


def pch_relevant_flags(options):
    """Return the sorted tuple of PCH-affecting flags from one file's options."""
    keep, skip_next = [], False
    for a in options:
        if skip_next:
            skip_next = False
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
    db = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    if not os.path.isfile(db):
        print(f"error: cidx index not found: {db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db)
    rows = conn.execute(
        """
        SELECT c.path, d.path, f.name, f.compile_options
        FROM file f
        JOIN directory d ON f.directory_id = d.id
        JOIN component c ON d.component_id = c.id
        WHERE f.compile_options IS NOT NULL
        """
    ).fetchall()
    conn.close()

    groups = defaultdict(list)
    for cpath, dpath, name, opts in rows:
        path = os.path.normpath(os.path.join(cpath, dpath, name))
        groups[pch_relevant_flags(json.loads(opts))].append(path)

    print(f"Checked {len(rows)} TUs in {db}")
    print(f"Distinct PCH-relevant flag sets: {len(groups)}\n")
    for flags, files in sorted(groups.items()):
        print(f"  flags: {list(flags) if flags else '(none)'}")
        for f in sorted(files):
            print(f"      {f}")
        print()

    if not groups:
        print("No TUs with compile flags found in this index.")
        return 1
    if len(groups) == 1:
        print("CONSISTENT — one shared PCH is valid for every TU.")
        return 0
    print(f"INCONSISTENT — {len(groups)} flag groups; a single shared PCH is NOT valid.")
    print("Each group above needs its own PCH (or reconcile the flags).")
    return 1


if __name__ == "__main__":
    sys.exit(main())

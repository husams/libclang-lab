#!/usr/bin/env python3
"""Check that every TU in a cidx index shares the same PCH-relevant compile
flags, so one shared (system) PCH would be valid for the whole index.

Reads the already-imported compile flags straight from the cidx index.db
(file.compile_options) — no compile_commands.json or libclang needed.

A PCH is only reusable across TUs whose preprocessor and language flags match.
This compares the flags that affect PCH validity, prints a short summary to the
screen, and writes a full per-group diff to a markdown report.

Compared (PCH-relevant):  -std, -D/-U, -f*, -m*, -W*, -O*, --driver-mode, ...
Excluded (per request):
  * include paths   -I / -iquote / -isystem / -idirafter (and joined -I<path>)
  * linker options  -l / -L / -Wl, / -Xlinker / -shared / -static / -rdynamic
(file.compile_options is already stripped of the driver token, source file,
 -c, and -o <out> at import time.)

Exit 0 if all TUs agree (one shared PCH is valid), 1 otherwise, 2 if no index.

Usage:
    python3 check_compile_flags.py [INDEX_DB] [-o REPORT.md]

INDEX_DB defaults to the standard index at ~/.cache/cidx/index.db.
REPORT.md defaults to ./compile_flags_diff.md.
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict

DEFAULT_DB = os.path.expanduser("~/.cache/cidx/index.db")
DEFAULT_REPORT = "compile_flags_diff.md"

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


def load_groups(db):
    """Map each PCH-relevant flag set -> sorted list of TU paths in the index."""
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
    for files in groups.values():
        files.sort()
    return dict(sorted(groups.items()))


def _fmt(flags):
    return " ".join(flags) if flags else "(none)"


def write_report(path, db, groups, common, differing):
    n_tu = sum(len(f) for f in groups.values())
    verdict = "CONSISTENT" if len(groups) == 1 else "INCONSISTENT"
    lines = [
        "# Compile-flag consistency report",
        "",
        f"- **Index:** `{db}`",
        f"- **TUs checked:** {n_tu}",
        f"- **Distinct PCH-relevant flag sets:** {len(groups)}",
        f"- **Verdict:** {verdict} — "
        + ("one shared PCH is valid for every TU."
           if len(groups) == 1 else
           "a single shared PCH is NOT valid; each group below needs its own."),
        "",
        "Excludes `-I` include paths and linker options. "
        "`compile_options` is already stripped of driver/source/`-c`/`-o`.",
        "",
        "## Differences",
        "",
        f"- **Common to all TUs:** `{_fmt(common)}`",
        f"- **Flags that vary across groups:** `{_fmt(differing)}`",
        "",
        "| Group | # TUs | Flags | Extra vs common |",
        "| ----- | ----- | ----- | --------------- |",
    ]
    for i, (flags, files) in enumerate(groups.items(), 1):
        extra = tuple(f for f in flags if f not in common)
        lines.append(f"| {i} | {len(files)} | `{_fmt(flags)}` | `{_fmt(extra)}` |")
    lines.append("")
    lines.append("## Groups in detail")
    for i, (flags, files) in enumerate(groups.items(), 1):
        lines += ["", f"### Group {i} — `{_fmt(flags)}` ({len(files)} TUs)", ""]
        lines += [f"- `{f}`" for f in files]
    lines.append("")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("index_db", nargs="?", default=DEFAULT_DB,
                    help="cidx index.db (default: ~/.cache/cidx/index.db)")
    ap.add_argument("-o", "--out", default=DEFAULT_REPORT,
                    help="markdown report path (default: ./compile_flags_diff.md)")
    args = ap.parse_args()

    if not os.path.isfile(args.index_db):
        print(f"error: cidx index not found: {args.index_db}", file=sys.stderr)
        return 2

    groups = load_groups(args.index_db)
    if not groups:
        print("No TUs with compile flags found in this index.")
        return 1

    flag_sets = [set(f) for f in groups]
    common = tuple(sorted(set.intersection(*flag_sets)))
    union = tuple(sorted(set.union(*flag_sets)))
    differing = tuple(f for f in union if f not in common)

    n_tu = sum(len(f) for f in groups.values())
    consistent = len(groups) == 1
    write_report(args.out, args.index_db, groups, common, differing)

    # ---- screen summary ----
    print(f"cidx index : {args.index_db}")
    print(f"TUs        : {n_tu}")
    print(f"flag groups: {len(groups)}   "
          f"verdict: {'CONSISTENT' if consistent else 'INCONSISTENT'}")
    print(f"common     : {_fmt(common)}")
    print(f"differing  : {_fmt(differing)}")
    for i, (flags, files) in enumerate(groups.items(), 1):
        print(f"  [{i}] {len(files):3d} TUs  {_fmt(flags)}")
    print(f"report     : {args.out}")
    if not consistent:
        print("=> a single shared PCH is NOT valid (one per group, or reconcile flags).")
    return 0 if consistent else 1


if __name__ == "__main__":
    sys.exit(main())

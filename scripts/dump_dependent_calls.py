#!/usr/bin/env python3
"""Dump the AST for a header, parsed through its real translation unit.

Why this exists
---------------
A call like ``BzRuleValueCache::instance().set<T>(...)`` lives inside a template
body, so it is a *dependent expression*. libclang cannot resolve it until the
template is instantiated, so the ``instance`` / ``set`` cursors come out as
UNEXPOSED_EXPR or OVERLOADED_DECL_REF with an EMPTY ``.spelling`` and
``.referenced == None``. They ARE in the tree -- they just look blank unless you
read the *tokens*.

Two things this script gets right that a naive dump gets wrong:
  1. It parses the *.cpp* (the real TU) and filters to the header, so the
     header sees all the includes the TU pulls in first. Parsing the header
     standalone gives an incomplete AST (more dependent/unexposed nodes, and
     fatal include errors silently TRUNCATE the tree).
  2. It prints tokens, not just spelling, so dependent nodes are visible.

Usage
-----
    python3 libclang-lab/scripts/dump_dependent_calls.py <header> [--tu <cpp>]

If --tu is omitted, the script tries the same path with a .cpp extension.
"""

import argparse
import os
import sys

from indexer.clang.ast import parse
from indexer.storage import Storage

# clang.cindex diagnostic severities: Ignored=0 Note=1 Warning=2 Error=3 Fatal=4
SEV_NAMES = {0: "ignored", 1: "note", 2: "warning", 3: "error", 4: "fatal"}

# Cursor kinds where dependent calls hide. None of these are exhaustive -- the
# script prints every node in the target file; this set is only used to flag the
# interesting ones so they're easy to grep for.
INTERESTING = {
    "CALL_EXPR",
    "MEMBER_REF_EXPR",
    "DECL_REF_EXPR",
    "OVERLOADED_DECL_REF",
    "UNEXPOSED_EXPR",
}


def visit(cursor, depth=0):
    yield cursor, depth
    for child in cursor.get_children():
        yield from visit(child, depth + 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("header", help="header file to inspect (e.g. BzRuleManager.hpp)")
    ap.add_argument("--tu", help="translation unit to parse (defaults to header with .cpp)")
    ap.add_argument("--db", default=os.path.expanduser("~/.cache/cidx/index.db"),
                    help="cidx index.db holding the compile options (default: %(default)s)")
    ap.add_argument("--only-interesting", action="store_true",
                    help="print only CALL/MEMBER/DECL_REF/OVERLOADED/UNEXPOSED nodes")
    args = ap.parse_args(argv)

    header = os.path.abspath(args.header)
    tu_path = os.path.abspath(args.tu) if args.tu \
        else os.path.splitext(header)[0] + ".cpp"

    if not os.path.exists(tu_path):
        sys.exit(f"TU not found: {tu_path}\n"
                 f"Pass --tu pointing at the .cpp that includes {os.path.basename(header)}")

    store = Storage(args.db)
    tu_file = store.get_file(tu_path)
    if tu_file is None:
        sys.exit(f"{tu_path} is not in the index ({args.db}).\n"
                 f"Index the component first, or pass --tu with an indexed file.")

    print(f"# parsing TU : {tu_path}")
    print(f"# filtering  : {header}")
    print(f"# driver     : {tu_file.driver}")
    print()

    tu = parse(tu_path, tu_file.compile_options, driver=tu_file.driver, check=False)

    # Surface anything that may have truncated the AST. A fatal include error
    # here is the usual reason a template body looks empty.
    diags = [d for d in tu.diagnostics if d.severity >= 3]
    if diags:
        print(f"# !! {len(diags)} error/fatal diagnostic(s) -- AST may be truncated:")
        for d in diags[:40]:
            print(f"#    [{SEV_NAMES.get(d.severity, d.severity)}] "
                  f"{d.spelling}  @ {d.location}")
        if len(diags) > 40:
            print(f"#    ... and {len(diags) - 40} more")
        print()

    shown = 0
    for c, depth in visit(tu.cursor):
        f = c.location.file
        if f is None or f.name != header:
            continue
        if args.only_interesting and c.kind.name not in INTERESTING:
            continue

        toks = " ".join(t.spelling for t in c.get_tokens())
        if len(toks) > 80:
            toks = toks[:77] + "..."

        ref = c.referenced
        ref_s = f" -> {ref.spelling}" if (ref is not None and ref.spelling) else ""

        cands = ""
        if c.kind.name == "OVERLOADED_DECL_REF":
            try:
                cands = f"  (overloads={c.get_num_overloaded_decls()})"
            except Exception:
                cands = "  (overloads=?)"

        where = f"{os.path.basename(f.name)}:{c.location.line}:{c.location.column}"
        flag = " *" if c.kind.name in INTERESTING else ""
        print("  " * depth +
              f"{c.kind.name} {c.spelling!r}{ref_s}{cands}  [{toks}]  @ {where}{flag}")
        shown += 1

    print()
    print(f"# {shown} node(s) in {os.path.basename(header)}")
    if shown == 0 and not diags:
        print("# Nothing in the target file. Wrong --tu, or the header isn't "
              "included by it.")


if __name__ == "__main__":
    main()

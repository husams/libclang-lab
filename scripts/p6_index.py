"""6.7 CAPSTONE: a mini semantic indexer over a 2-TU project (USR-keyed)."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, walk, clang_args, loc, in_main_file


def build_index(files, args):
    """Index every TU into a symbol table + cross-reference map, keyed by USR.

    A USR (Unified Symbol Resolution) is stable across translation units, so a
    function defined in mathlib.c and called from app.c share ONE key. This is
    a tiny clang.cindex echo of the cpp-mcp / cpp-indexer pattern: parse each
    TU, harvest definitions and call sites, merge on USR.
    """
    symbols = {}   # usr -> {"name", "kind", "defined_at"}
    xrefs = {}     # usr -> set of "file:line:col" use locations

    for f in files:
        tu = parse(f, args=args)
        for c, _ in walk(tu.cursor):
            if not in_main_file(c):
                continue
            if c.kind == cx.CursorKind.FUNCTION_DECL and c.is_definition():
                symbols[c.get_usr()] = {
                    "name": c.spelling,
                    "kind": str(c.kind).split(".")[-1],
                    "defined_at": loc(c),
                }
            elif c.kind == cx.CursorKind.CALL_EXPR and c.referenced:
                xrefs.setdefault(c.referenced.get_usr(), set()).add(loc(c))
    return symbols, xrefs


def usr_of(symbols, name):
    for usr, info in symbols.items():
        if info["name"] == name:
            return usr
    return None


def main():
    proj = MANIFESTS / "project"
    files = sorted([proj / "mathlib.c", proj / "app.c"])
    args = clang_args(extra_includes=[proj])

    symbols, xrefs = build_index(files, args)

    print("symbol table (sorted by name):")
    for usr in sorted(symbols, key=lambda u: symbols[u]["name"]):
        info = symbols[usr]
        print(f"  {info['name']:10} {info['kind']:14} {info['defined_at']}")

    print()
    print("query 1: where is 'multiply' defined?")
    mult = usr_of(symbols, "multiply")
    print(f"  {symbols[mult]['defined_at']}")

    print()
    print("query 2: who calls 'multiply'?")
    for site in sorted(xrefs.get(mult, [])):
        print(f"  {site}")

    print()
    print("query 2b: who calls 'square'?")
    sq = usr_of(symbols, "square")
    for site in sorted(xrefs.get(sq, [])):
        print(f"  {site}")


if __name__ == "__main__":
    main()

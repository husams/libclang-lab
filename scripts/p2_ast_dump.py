"""2.6 Reusable AST dumper: indented, main-file-filtered tree of calls.c."""
import sys
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, loc, in_main_file, fatal_diagnostics


def dump(cursor, depth=0, kind_filter=None):
    """Recursively print kind | spelling @ loc, indented, main-file only.

    kind_filter: optional set of CursorKind to keep (None = keep all).
    """
    for child in cursor.get_children():
        if not in_main_file(child):
            continue
        if kind_filter is None or child.kind in kind_filter:
            indent = "  " * depth
            name = child.spelling or "<anon>"
            print(f"{indent}{child.kind.name:<20} {name:<14} @ {loc(child)}")
        # Recurse regardless of filter so filtered output still shows nesting
        # of matched nodes wherever they live in the tree.
        dump(child, depth + 1, kind_filter)


def main():
    tu = parse(MANIFESTS / "calls.c", args=clang_args())
    if fatal_diagnostics(tu):
        print("FATAL:", [d.spelling for d in fatal_diagnostics(tu)])
        return

    # Optional CLI arg picks a kind filter; default dumps the whole main file.
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    kind_filter = {getattr(cx.CursorKind, arg)} if arg else None

    label = f"filter={arg}" if arg else "full tree"
    print(f"AST dump of calls.c ({label}):")
    dump(tu.cursor, 0, kind_filter)


if __name__ == "__main__":
    main()

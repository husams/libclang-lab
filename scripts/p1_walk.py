"""§1.5 - Walking the tree: get_children() + the recursive walk() helper."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, loc, walk, in_main_file


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())

    # walk() yields (cursor, depth) pre-order, starting at the root. Each child
    # comes from cursor.get_children(), which preserves SOURCE ORDER -- so we do
    # NOT sort here; the indentation IS the tree.
    #
    # We dump only main-file nodes: descending into a function's body crosses
    # into the same file, so we keep those, but we skip the thousands of nodes
    # that live in #included headers. A full walk of every body runs ~180 lines,
    # so we cap the depth to keep the shape of the tree readable; the deeper
    # statement/expression nodes are Part 2's job.
    MAX_DEPTH = 2
    print(f"{'KIND (indented by depth)':<34} SPELLING @ LOCATION")
    for cursor, depth in walk(tu.cursor):
        if not in_main_file(cursor) or depth > MAX_DEPTH:
            continue
        indent = "  " * (depth - 1)  # depth 1 = top level (root is <builtin>)
        label = f"{indent}{cursor.kind.name}"
        name = cursor.spelling or "-"
        print(f"{label:<34} {name} @ {loc(cursor)}")


if __name__ == "__main__":
    main()

"""3.1 The Type object & TypeKind: a cursor is a node, its .type is what it denotes."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, top_level, loc


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())

    # A cursor names a node in the AST. Its `.type` is the *type that node
    # denotes*: a function decl denotes a function type, a variable a value
    # type, and so on. cursor.kind and type.kind are two different vocabularies.
    print("cursor (node)        type.kind      type.spelling")
    print("-" * 60)
    rows = []
    for c in top_level(tu):
        if c.kind != cx.CursorKind.FUNCTION_DECL:
            continue
        t = c.type
        rows.append((c.spelling, t.kind.name, t.spelling))
    for name, tkind, tspell in sorted(rows):
        print(f"{name:<20} {tkind:<14} {tspell}")


if __name__ == "__main__":
    main()

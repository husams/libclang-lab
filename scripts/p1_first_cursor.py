"""§1.4 - The Cursor: tu.cursor is the TRANSLATION_UNIT root; .kind/.spelling/.location."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, loc, top_level


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())

    # The root cursor is the whole TU. It has no source file of its own,
    # so loc() reports "<builtin>".
    root = tu.cursor
    print("root.kind:", root.kind)
    print("root.location:", loc(root))
    print("=" * 56)

    # A cursor is a typed pointer to an AST node: .kind is what it is,
    # .spelling is its name, .location is where it sits in the source.
    # top_level() = the TU's direct children that originate in the main file
    # (filtering out the hundreds of nodes pulled in by #include).
    print(f"{'KIND':<22} {'SPELLING':<22} LOCATION")
    for cursor in top_level(tu):
        print(f"{cursor.kind.name:<22} {cursor.spelling:<22} {loc(cursor)}")


if __name__ == "__main__":
    main()

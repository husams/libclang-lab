"""3.5 Typedefs & enums: underlying_typedef_type, and enum constants with values."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, top_level


def main():
    tu = parse(MANIFESTS / "shapes.h", args=clang_args())

    # A TYPEDEF_DECL's underlying_typedef_type is the type it aliases. Here it
    # is still "sugar" (ELABORATED: e.g. 'struct Point'); get_canonical() would
    # reduce it further. See 3.2 for the sugar-vs-canonical distinction.
    print("typedef  ->  underlying_typedef_type")
    print("-" * 44)
    rows = []
    for c in top_level(tu):
        if c.kind == cx.CursorKind.TYPEDEF_DECL:
            u = c.underlying_typedef_type
            rows.append((c.spelling, f"{u.spelling}  [{u.kind.name}]"))
    for name, under in sorted(rows):
        print(f"{name:<10} ->  {under}")

    # An ENUM_DECL's children are ENUM_CONSTANT_DECL cursors; each carries an
    # integer .enum_value (assigned by the compiler if not written explicitly).
    enum = next(c for c in top_level(tu)
                if c.kind == cx.CursorKind.ENUM_DECL and c.spelling == "ShapeKind")
    print(f"\nenum {enum.spelling} constants:")
    for k in enum.get_children():
        if k.kind == cx.CursorKind.ENUM_CONSTANT_DECL:
            print(f"  {k.spelling:<16} = {k.enum_value}")


if __name__ == "__main__":
    main()

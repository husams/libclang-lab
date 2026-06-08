"""3.3 Function signatures: result_type, named params vs type-level arg types, variadic."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, top_level


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())

    # get_arguments() is CURSOR-level: it yields PARM_DECL cursors, so you get
    # the *named* parameters. type.argument_types() is TYPE-level: just the
    # types, and (per libclang) it never includes the variadic "..." slot.
    for name in ("shape_area", "shapes_total_area", "average"):
        fn = next(c for c in top_level(tu)
                  if c.kind == cx.CursorKind.FUNCTION_DECL and c.spelling == name)
        named = [(a.spelling, a.type.spelling) for a in fn.get_arguments()]
        types = [t.spelling for t in fn.type.argument_types()]
        print(f"{name}  ->  returns {fn.result_type.spelling}"
              f"  variadic={fn.type.is_function_variadic()}")
        print(f"  get_arguments()   (named) : {named}")
        print(f"  argument_types()  (type)  : {types}")


if __name__ == "__main__":
    main()

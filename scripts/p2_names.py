"""2.2 Names: spelling vs displayname vs get_usr() on shapes.c functions."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, top_level, fatal_diagnostics


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())
    if fatal_diagnostics(tu):
        print("FATAL:", [d.spelling for d in fatal_diagnostics(tu)])
        return

    funcs = sorted(
        (c for c in top_level(tu) if c.kind == cx.CursorKind.FUNCTION_DECL),
        key=lambda c: c.spelling,
    )

    print("Three names libclang attaches to every cursor:")
    print("  spelling     = the identifier as written")
    print("  displayname  = identifier + signature (disambiguates overloads)")
    print("  get_usr()    = Unified Symbol Resolution: stable cross-TU identity")
    print()

    for f in funcs:
        print(f"spelling     : {f.spelling}")
        print(f"displayname  : {f.displayname}")
        print(f"usr          : {f.get_usr()}")
        print()


if __name__ == "__main__":
    main()

"""2.1 CursorKind: enumerate the node taxonomy of shapes.c's top-level cursors."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, loc, top_level, fatal_diagnostics


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())
    if fatal_diagnostics(tu):
        print("FATAL:", [d.spelling for d in fatal_diagnostics(tu)])
        return

    print("Top-level cursors in shapes.c (kind | category | spelling @ loc):")
    rows = []
    for c in top_level(tu):
        k = c.kind
        # Category helpers live on CursorKind, not on the Cursor itself.
        if k.is_declaration():
            category = "declaration"
        elif k.is_reference():
            category = "reference"
        elif k.is_expression():
            category = "expression"
        elif k.is_statement():
            category = "statement"
        else:
            category = "other"
        rows.append((c.kind.name, category, c.spelling or "<anon>", loc(c)))

    for kind_name, category, spelling, where in sorted(rows):
        print(f"  {kind_name:<14} {category:<12} {spelling:<18} {where}")

    # Testing a kind is a plain `==` against a CursorKind constant.
    funcs = [c for c in top_level(tu) if c.kind == cx.CursorKind.FUNCTION_DECL]
    print()
    print(f"FUNCTION_DECL count (== test): {len(funcs)}")
    print("function names:", sorted(f.spelling for f in funcs))


if __name__ == "__main__":
    main()

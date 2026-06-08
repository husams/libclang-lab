"""2.3 Locations & extents: SourceLocation fields and a function's SourceRange."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, loc, top_level, fatal_diagnostics


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())
    if fatal_diagnostics(tu):
        print("FATAL:", [d.spelling for d in fatal_diagnostics(tu)])
        return

    funcs = sorted(
        (c for c in top_level(tu) if c.kind == cx.CursorKind.FUNCTION_DECL),
        key=lambda c: c.spelling,
    )

    print("Each function's start location and full extent (start -> end):")
    print(f"  {'function':<18} {'loc()':<22} extent (line:col -> line:col)")
    for f in funcs:
        ext = f.extent
        span = (f"{ext.start.line}:{ext.start.column} -> "
                f"{ext.end.line}:{ext.end.column}")
        print(f"  {f.spelling:<18} {loc(f):<22} {span}")

    # A SourceLocation carries file/line/column AND a byte offset.
    target = next(f for f in funcs if f.spelling == "shape_area")
    sl = target.location
    print()
    print("SourceLocation fields for shape_area:")
    print(f"  file   : {sl.file.name.split('/')[-1]}")
    print(f"  line   : {sl.line}")
    print(f"  column : {sl.column}")
    print(f"  offset : {sl.offset}  (bytes from start of file)")


if __name__ == "__main__":
    main()

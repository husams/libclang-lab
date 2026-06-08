"""2.4 Filtering to your file: in_main_file + declaration-vs-definition cursors."""
import clang.cindex as cx
from _helpers import (MANIFESTS, parse, clang_args, loc, walk, in_main_file,
                      fatal_diagnostics)


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())
    if fatal_diagnostics(tu):
        print("FATAL:", [d.spelling for d in fatal_diagnostics(tu)])
        return

    # WHY filter: parsing pulls in every #included header. A raw walk of the TU
    # also counts nodes from shapes.h, <stddef.h>, <math.h>, ... (for C++ that
    # is thousands of libc++ nodes), and that header total varies by SDK -- so
    # we never print it. in_main_file() keeps only what YOU wrote: a stable set.
    main_nodes = [c for c, _ in walk(tu.cursor) if in_main_file(c)]
    print("Filtering to the main file:")
    print(f"  tu.spelling           : {tu.spelling.split('/')[-1]}")
    print(f"  nodes in main file     : {len(main_nodes)}  (in_main_file == True)")
    print("  test = (cursor.location.file.name == cursor.translation_unit.spelling)")
    print("  (the rest of the TU comes from headers; that total varies by SDK)")
    print()

    # The gotcha: shapes_total_area is DECLARED in shapes.h and DEFINED in
    # shapes.c. Those are TWO cursors with the same spelling. A naive
    # walk()+next() that grabs the first match can land on the prototype
    # (no body). is_definition() / get_definition() / canonical sort them out.
    matches = [c for c, _ in walk(tu.cursor)
               if c.kind == cx.CursorKind.FUNCTION_DECL
               and c.spelling == "shapes_total_area"]
    matches.sort(key=lambda c: (c.location.line, c.location.column))

    print("Two cursors named 'shapes_total_area' (decl in .h, def in .c):")
    for c in matches:
        body = any(ch.kind == cx.CursorKind.COMPOUND_STMT
                   for ch in c.get_children())
        label = "definition" if c.is_definition() else "declaration (prototype)"
        print(f"  {loc(c):<22} {label:<24} body_present={body}")

    # get_definition() jumps from ANY cursor to the one true definition;
    # canonical points every redeclaration at the first one (stable anchor).
    proto = next(c for c in matches if not c.is_definition())
    defn = proto.get_definition()
    print()
    print("Following the links from the prototype cursor:")
    print(f"  prototype at            : {loc(proto)}")
    print(f"  get_definition() ->      : {loc(defn)}  (the .c body)")
    print(f"  proto.canonical at       : {loc(proto.canonical)}  (first decl seen)")
    print(f"  defn.canonical at        : {loc(defn.canonical)}  (same anchor)")
    print(f"  same canonical?          : {proto.canonical == defn.canonical}")


if __name__ == "__main__":
    main()

"""3.6 Semantic links: referenced, get_definition, canonical, USR, lexical vs semantic parent."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, top_level, walk, loc


def parent_label(cursor):
    """Kind-only label for a parent; never print the TU's absolute path."""
    if cursor is None:
        return "None"
    if cursor.kind == cx.CursorKind.TRANSLATION_UNIT:
        return "TRANSLATION_UNIT"
    return f"{cursor.kind.name} {cursor.spelling!r}"


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())

    # Resolve the call to shape_area inside shapes_total_area's body.
    fn = next(c for c in top_level(tu)
              if c.kind == cx.CursorKind.FUNCTION_DECL and c.spelling == "shapes_total_area")
    call = next(c for c, _ in walk(fn)
                if c.kind == cx.CursorKind.CALL_EXPR and c.spelling == "shape_area")

    print(f"CALL_EXPR 'shape_area' @ {loc(call)}")
    ref = call.referenced                # use -> the decl it resolves to (the definition)
    print(f"  referenced      -> {ref.spelling} @ {loc(ref)}  [{ref.kind.name}]")
    defn = ref.get_definition()          # decl -> its definition
    print(f"  get_definition  -> {defn.spelling} @ {loc(defn)}" if defn else "  get_definition  -> None")
    print(f"  canonical       -> @ {loc(ref.canonical)}  (the first declaration / prototype)")

    # USR = Unified Symbol Resolution: a stable, location-independent symbol id.
    # The prototype and the definition share one USR (forward ref: Part 5 uses
    # USRs to link symbols ACROSS files).
    print(f"  get_usr         -> {ref.get_usr()}")
    print(f"  USR(defn)==USR(ref): {bool(defn) and defn.get_usr() == ref.get_usr()}")

    # lexical_parent = where the cursor is written; semantic_parent = where it
    # logically belongs. In C, file-scope functions: both are the TU. They
    # DIVERGE for out-of-line C++ method definitions (see 3.7: Circle::area).
    sd = next(c for c in top_level(tu)
              if c.kind == cx.CursorKind.FUNCTION_DECL and c.spelling == "shape_area")
    print(f"\nshape_area lexical_parent  : {parent_label(sd.lexical_parent)}")
    print(f"shape_area semantic_parent : {parent_label(sd.semantic_parent)}")


if __name__ == "__main__":
    main()

"""4.2 Parse options: PARSE_* flags change WHAT the parser builds (e.g. skip bodies)."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, walk, clang_args, in_main_file


def funcs(tu):
    """Top-level function signatures defined in the main file, sorted by name."""
    return sorted(c.spelling for c in tu.cursor.get_children()
                  if c.kind == cx.CursorKind.FUNCTION_DECL and in_main_file(c))


def calls(tu):
    """CALL_EXPR cursors anywhere in the main file (these live inside bodies)."""
    return [c for c, _ in walk(tu.cursor)
            if c.kind == cx.CursorKind.CALL_EXPR and in_main_file(c)]


def main():
    src = MANIFESTS / "shapes.c"
    args = clang_args()

    full = parse(src, args=args)
    skip = parse(src, args=args,
                 options=cx.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES)

    print("PARSE_SKIP_FUNCTION_BODIES =", cx.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES)
    print()

    # Signatures survive in BOTH parses — skipping bodies keeps the index intact.
    print("function signatures (full parse):", funcs(full))
    print("function signatures (skip parse):", funcs(skip))
    print()

    # But the bodies — and every CALL_EXPR inside them — vanish when skipped.
    print(f"CALL_EXPR nodes, full parse: {len(calls(full))}")
    print(f"CALL_EXPR nodes, skip parse: {len(calls(skip))}")
    print()
    print("Same signatures, no bodies -> fast declaration/definition indexing.")


if __name__ == "__main__":
    main()

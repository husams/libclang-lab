"""2.5 Tokens: the lexical view. Tokenize shape_translate over its extent."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, walk, in_main_file, fatal_diagnostics


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())
    if fatal_diagnostics(tu):
        print("FATAL:", [d.spelling for d in fatal_diagnostics(tu)])
        return

    fn = next(c for c, _ in walk(tu.cursor)
              if c.kind == cx.CursorKind.FUNCTION_DECL
              and c.spelling == "shape_translate"
              and in_main_file(c) and c.is_definition())

    # get_tokens() lexes the raw source over the cursor's extent. Tokens are the
    # LEXICAL view (every keyword/punct/literal as typed); cursors are the
    # SYNTACTIC view (the tree the parser built). Order here is source order.
    tokens = list(fn.get_tokens())
    print(f"Tokens of shape_translate ({len(tokens)} total), in source order:")
    print(f"  {'#':<3} {'kind':<12} spelling")
    for i, t in enumerate(tokens):
        print(f"  {i:<3} {t.kind.name:<12} {t.spelling}")

    print()
    print("Count by TokenKind:")
    counts = {}
    for t in tokens:
        counts[t.kind.name] = counts.get(t.kind.name, 0) + 1
    for kind_name in sorted(counts):
        print(f"  {kind_name:<12} {counts[kind_name]}")


if __name__ == "__main__":
    main()

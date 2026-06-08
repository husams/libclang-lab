"""6.1 Unsaved files: parse an in-memory buffer without touching disk."""
import clang.cindex as cx
from _helpers import parse, top_level


def main():
    # An editor/IDE never writes the buffer to disk before asking for an AST.
    # libclang accepts virtual files via unsaved_files=[(name, source)]: the
    # `name` is what we parse, and any reference to that name reads `source`.
    name = "virtual.c"
    source = (
        "int answer(void) { return 42; }\n"
        "double half(double x) { return x / 2.0; }\n"
        "static int helper(void) { return answer() - 1; }\n"
    )

    # Note: `virtual.c` does not exist on disk; libclang only sees the buffer.
    tu = parse(name, args=["-std=c11"], unsaved_files=[(name, source)])

    funcs = sorted(
        c.spelling
        for c in top_level(tu)
        if c.kind == cx.CursorKind.FUNCTION_DECL
    )
    print(f"parsed in-memory buffer: {name} ({len(source)} bytes, never on disk)")
    print("functions found:")
    for f in funcs:
        print(f"  {f}")


if __name__ == "__main__":
    main()

"""6.3 Serialized ASTs: tu.save() then from_ast_file() reload without reparsing."""
import os
import tempfile

import clang.cindex as cx
from _helpers import MANIFESTS, parse, top_level, clang_args


def func_names(tu):
    return sorted(
        c.spelling for c in top_level(tu) if c.kind == cx.CursorKind.FUNCTION_DECL
    )


def main():
    # Parse once, the expensive way (front end runs, headers resolved).
    src = MANIFESTS / "shapes.c"
    tu = parse(src, args=clang_args())
    original = func_names(tu)

    # save() writes a binary AST (the same format `-emit-ast` / a PCH uses).
    # A consumer can reload it with from_ast_file() and skip parsing entirely.
    fd, ast_path = tempfile.mkstemp(suffix=".ast")
    os.close(fd)
    try:
        tu.save(ast_path)
        size = os.path.getsize(ast_path)

        # Reload into a FRESH Index — no source file or compiler flags needed.
        index = cx.Index.create()
        reloaded = cx.TranslationUnit.from_ast_file(ast_path, index)
        restored = func_names(reloaded)

        # Print a qualitative size check, not the raw byte count: the exact
        # size drifts across libclang/SDK versions, so it is not deterministic.
        print(f"saved binary AST: True   (> 100 KB: {size > 100_000})")
        print(f"reloaded WITHOUT reparsing: True")
        print(f"main-file functions match after reload: {restored == original}")
        print("functions:")
        for f in restored:
            print(f"  {f}")
    finally:
        os.remove(ast_path)


if __name__ == "__main__":
    main()

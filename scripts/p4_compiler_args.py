"""4.3 Compiler arguments: -I / -D / -std drive the AST — same source, different tree."""
import clang.cindex as cx
from _helpers import parse, walk, in_main_file, fatal_diagnostics, clang_args


def main_funcs(tu):
    return sorted(c.spelling for c in tu.cursor.get_children()
                  if c.kind == cx.CursorKind.FUNCTION_DECL and in_main_file(c))


def main():
    # --- -D toggles a preprocessor branch ---------------------------------
    # The SAME source compiles two different ASTs depending on -DENABLE_LOG.
    # The disabled #if branch is never even tokenized into the tree.
    branch_src = (
        "#ifdef ENABLE_LOG\n"
        "int logging_on(void) { return 1; }\n"
        "#else\n"
        "int logging_off(void) { return 0; }\n"
        "#endif\n"
    )
    off = parse("branch.c", args=["-std=c11"],
                unsaved_files=[("branch.c", branch_src)])
    on = parse("branch.c", args=["-std=c11", "-DENABLE_LOG"],
               unsaved_files=[("branch.c", branch_src)])
    print("-D toggles which branch is compiled:")
    print("  args=[]            -> funcs:", main_funcs(off))
    print("  args=[-DENABLE_LOG]-> funcs:", main_funcs(on))
    print()

    # --- -I controls header resolution ------------------------------------
    # Without -I<manifests>, "shapes.h" is not found: a FATAL diagnostic, and
    # the undefined `Shape` type degrades to `int` under error recovery.
    inc_src = '#include "shapes.h"\nShape g;\n'
    without = parse("u.c", args=["-std=c11"],
                    unsaved_files=[("u.c", inc_src)])
    with_i = parse("u.c", args=clang_args(),
                   unsaved_files=[("u.c", inc_src)])

    def type_of_g(tu):
        g = next((c for c, _ in walk(tu.cursor)
                  if c.kind == cx.CursorKind.VAR_DECL
                  and c.spelling == "g" and in_main_file(c)), None)
        return g.type.spelling if g else "<none>"

    print("-I controls whether the included type resolves:")
    print(f"  without -I -> type of `g`: {type_of_g(without)!r:8} "
          f"fatal={bool(fatal_diagnostics(without))}")
    print(f"  with    -I -> type of `g`: {type_of_g(with_i)!r:8} "
          f"fatal={bool(fatal_diagnostics(with_i))}")
    print()
    print("args ARE the parse: same bytes + different flags = different AST.")


if __name__ == "__main__":
    main()

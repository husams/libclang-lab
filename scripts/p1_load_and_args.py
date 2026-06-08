"""§1.2 - Loading libclang, and the missing-builtin-headers gotcha (broken vs fixed)."""
import os

import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, fatal_diagnostics, walk, in_main_file


def main_file_funcs(tu):
    """Sorted names of the FUNCTION_DECLs that live in the parsed file."""
    return sorted(
        c.spelling
        for c, _ in walk(tu.cursor)
        if c.kind == cx.CursorKind.FUNCTION_DECL and in_main_file(c)
    )


def subtree_size(tu, fname):
    """Number of AST nodes under the named function (its body + descendants)."""
    fn = next(
        c
        for c, _ in walk(tu.cursor)
        if c.kind == cx.CursorKind.FUNCTION_DECL and in_main_file(c) and c.spelling == fname
    )
    return sum(1 for _ in walk(fn))


def main():
    src = MANIFESTS / "shapes.c"

    # 1) Confirm libclang loaded: if Index.create() works, the dylib is bound.
    #    (We print OK, not the dylib path -- that path is machine-specific.)
    cx.Index.create()
    print("libclang loaded:", "OK")
    # clang_args() resolves to machine-specific absolute paths, so we print only
    # the *flags* it contributes, not their values (those vary per SDK/machine).
    flags = clang_args()
    print("clang_args() flags:", sorted({f for f in flags if f.startswith("-")}))
    print("=" * 56)

    # 2) BROKEN: a bare parse (args=[]) cannot find Clang's builtin <stddef.h>.
    bad = parse(src, args=[])
    bad_fatal = fatal_diagnostics(bad)
    print("BROKEN parse (args=[])")
    print("  fatal diagnostics:", len(bad_fatal))
    for d in bad_fatal:
        print("   -", d.spelling)
    print("  main-file functions:", main_file_funcs(bad))
    print("  shapes_total_area subtree nodes:", subtree_size(bad, "shapes_total_area"))

    # 3) FIXED: clang_args() supplies -isysroot <SDK> and -I <resource-dir>/include.
    good = parse(src, args=clang_args())
    good_fatal = fatal_diagnostics(good)
    print("FIXED parse (args=clang_args())")
    print("  fatal diagnostics:", len(good_fatal))
    print("  main-file functions:", main_file_funcs(good))
    print("  shapes_total_area subtree nodes:", subtree_size(good, "shapes_total_area"))


if __name__ == "__main__":
    main()

"""6.3b PCH-as-prefix: precompile a HEADER alone, reuse it via -include-pch.

Unlike 6.3 (which round-trips a whole .c TU through from_ast_file), this is the
real precompiled-header workflow: build a binary AST from shapes.h ONCE, then
feed it to later parses with `-include-pch`. The header is loaded from the PCH,
never reparsed -- even for a file that uses `Shape` without #including it.
"""
import os
import tempfile

import clang.cindex as cx
from _helpers import MANIFESTS, clang_args, fatal_diagnostics, top_level


def func_names(tu):
    return sorted(
        c.spelling for c in top_level(tu) if c.kind == cx.CursorKind.FUNCTION_DECL
    )


def main():
    index = cx.Index.create()
    args = clang_args()

    # 1) Precompile the HEADER ALONE. `-x c-header` is what makes it a reusable
    #    PCH (without it, clang treats the .h as ordinary source). INCOMPLETE
    #    suppresses header-only "no main" style noise.
    hdr = MANIFESTS / "shapes.h"
    hdr_tu = index.parse(
        str(hdr),
        args=args + ["-x", "c-header"],
        options=cx.TranslationUnit.PARSE_INCOMPLETE,
    )
    hdr_fatals = fatal_diagnostics(hdr_tu)

    fd, pch = tempfile.mkstemp(suffix=".pch")
    os.close(fd)
    try:
        hdr_tu.save(pch)
        size = os.path.getsize(pch)

        # 2) Parse a probe buffer that uses `Shape` but does NOT #include the
        #    header. The type resolves purely from the prepended PCH. The
        #    consuming parse MUST use the same clang_args() the PCH was built
        #    with, or libclang rejects the PCH as incompatible.
        probe = "double bbox(const Shape *s) { return s->dimensions[0]; }\n"
        probe_tu = index.parse(
            "probe.c",
            args=args + ["-include-pch", pch],
            unsaved_files=[("probe.c", probe)],
        )
        probe_fatals = fatal_diagnostics(probe_tu)
        arg_types = [
            a.type.spelling
            for c in top_level(probe_tu)
            if c.kind == cx.CursorKind.FUNCTION_DECL
            for a in c.get_arguments()
        ]

        # 3) A real file that DOES #include "shapes.h": the include guard
        #    (SHAPES_H, already defined by the prepended PCH) makes the on-disk
        #    re-include a no-op, so the header comes from the PCH, not a reparse.
        sc_tu = index.parse(str(MANIFESTS / "shapes.c"), args=args + ["-include-pch", pch])
        sc_fatals = fatal_diagnostics(sc_tu)

        print(f"header precompiled alone (-x c-header): {not hdr_fatals}")
        print(f"PCH saved on disk: True   (> 10 KB: {size > 10_000})")
        print(f"probe.c uses Shape with NO #include, 0 fatals: {not probe_fatals}")
        print(f"  Shape resolved from PCH -> arg type: {arg_types[0] if arg_types else '<none>'}")
        print(f"shapes.c (#includes the header) parses with PCH, 0 fatals: {not sc_fatals}")
        print("shapes.c functions:")
        for f in func_names(sc_tu):
            print(f"  {f}")
    finally:
        os.remove(pch)


if __name__ == "__main__":
    main()

"""6.2 reparse: update a TU in place after a buffer edit, faster than re-parsing."""
import clang.cindex as cx
from _helpers import parse, top_level


def func_names(tu):
    return sorted(
        c.spelling for c in top_level(tu) if c.kind == cx.CursorKind.FUNCTION_DECL
    )


def main():
    name = "virtual.c"
    v1 = "int answer(void) { return 42; }\n"

    # Parse with PARSE_PRECOMPILED_PREAMBLE so reparse can reuse the cached
    # preamble (#includes etc.) instead of redoing all the front-end work.
    tu = parse(
        name,
        args=["-std=c11"],
        unsaved_files=[(name, v1)],
        options=cx.TranslationUnit.PARSE_PRECOMPILED_PREAMBLE,
    )
    print("before edit:", func_names(tu))

    # The "editor" appends a function. reparse() mutates the SAME TU object
    # in place — same Index, same cursor handles — rather than building a new
    # one. cheaper than a fresh parse() for incremental edits.
    v2 = v1 + "int doubled(int x) { return x * 2; }\n"
    tu.reparse(unsaved_files=[(name, v2)])
    print("after edit: ", func_names(tu))

    added = sorted(set(func_names(tu)) - {"answer"})
    print("cursor tree changed; new function(s):", added)


if __name__ == "__main__":
    main()

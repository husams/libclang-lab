"""6.4 Code completion: tu.codeComplete() at 's->' yields the Shape members."""
import clang.cindex as cx
from _helpers import parse, clang_args


def main():
    # A virtual buffer with a member-access we want completions for. The cursor
    # sits right after `s->` (line 3, column 8), exactly where an editor would
    # ask "what can follow this dot/arrow?".
    name = "probe.c"
    source = (
        '#include "shapes.h"\n'        # line 1: pulls in the Shape definition
        "void probe(const Shape *s) {\n"  # line 2
        "    s->\n"                      # line 3: complete here -> col 8
        "}\n"                            # line 4
    )
    line, col = 3, 8

    tu = parse(
        name,
        args=clang_args(),
        unsaved_files=[(name, source)],
        options=cx.TranslationUnit.PARSE_PRECOMPILED_PREAMBLE,
    )
    results = tu.codeComplete(name, line, col, unsaved_files=[(name, source)])

    # Each result is a CompletionString of chunks; the TypedText chunk is the
    # token you would actually type (the member name), as opposed to its type
    # or punctuation chunks.
    members = []
    for r in results.results:
        for chunk in r.string:
            if chunk.isKindTypedText():
                members.append(chunk.spelling)
                break

    print(f"completing 's->' at {name}:{line}:{col}")
    print("candidate members:")
    for m in sorted(set(members)):
        print(f"  {m}")


if __name__ == "__main__":
    main()

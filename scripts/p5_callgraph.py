"""5.4 Call-graph extraction: map each defined function to the functions it calls."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, walk, in_main_file, fatal_diagnostics


def callees(func):
    """Sorted unique callee names reachable from a function's body."""
    names = set()
    for c, _ in walk(func):
        if c.kind == cx.CursorKind.CALL_EXPR and c.referenced:
            names.add(c.referenced.spelling)
    return sorted(names)


def main():
    tu = parse(MANIFESTS / "calls.c", args=clang_args())
    if fatal_diagnostics(tu):
        raise SystemExit("parse failed: " + str(fatal_diagnostics(tu)))

    # Only function DEFINITIONS in the main file have a body to scan.
    graph = {}
    for c in tu.cursor.get_children():
        if (c.kind == cx.CursorKind.FUNCTION_DECL
                and c.is_definition() and in_main_file(c)):
            graph[c.spelling] = callees(c)

    for caller in sorted(graph):
        edges = graph[caller]
        print(f"{caller} -> {edges}")


if __name__ == "__main__":
    main()

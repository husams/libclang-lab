"""probe_graph_calls — confirm body-descent is needed for CALL_EXPR (facts 1,2,6).

The production walk (_file_cursors) returns/continues at function-like cursors,
so CALL_EXPR is NEVER visited. Here we descend into bodies ourselves and count
CALL_EXPR sites, resolve caller USR (enclosing fn) + callee USR
(get_cursor_referenced -> USR), and detect conditional enclosure.
"""
import clang.cindex as cx
from _helpers import MANIFESTS, clang_args, parse

CALLS = MANIFESTS / "calls.c"

COND_KINDS = {
    cx.CursorKind.IF_STMT, cx.CursorKind.FOR_STMT, cx.CursorKind.WHILE_STMT,
    cx.CursorKind.DO_STMT, cx.CursorKind.CONDITIONAL_OPERATOR,
    cx.CursorKind.SWITCH_STMT, cx.CursorKind.CASE_STMT,
}
FUNC_KINDS = {
    cx.CursorKind.FUNCTION_DECL, cx.CursorKind.CXX_METHOD,
    cx.CursorKind.CONSTRUCTOR, cx.CursorKind.DESTRUCTOR,
    cx.CursorKind.FUNCTION_TEMPLATE,
}


def count_via_production_walk(tu):
    """Mirror _file_cursors: prove CALL_EXPR is never seen."""
    seen_call = 0

    def walk(cursor):
        nonlocal seen_call
        for child in cursor.get_children():
            f = child.location.file
            if f is None or f.name != tu.spelling:
                continue
            if child.kind == cx.CursorKind.CALL_EXPR:
                seen_call += 1
            if child.kind not in FUNC_KINDS:
                walk(child)
    walk(tu.cursor)
    return seen_call


def body_descent(tu):
    """Descend into each top-level function body, keyed by enclosing fn USR."""
    edges = []  # (caller_usr, callee_usr, callee_spelling, line, col, conditional)

    def descend(node, caller_usr, cond_depth):
        for child in node.get_children():
            k = child.kind
            new_cond = cond_depth + (1 if k in COND_KINDS else 0)
            if k == cx.CursorKind.CALL_EXPR:
                ref = child.referenced
                callee_usr = ref.get_usr() if ref is not None else "<unresolved>"
                callee_sp = ref.spelling if ref is not None else child.spelling
                edges.append((caller_usr, callee_usr, callee_sp,
                              child.location.line, child.location.column,
                              new_cond > 0))
            descend(child, caller_usr, new_cond)

    for c in tu.cursor.get_children():
        f = c.location.file
        if f is None or f.name != tu.spelling:
            continue
        if c.kind in FUNC_KINDS and c.is_definition():
            descend(c, c.get_usr(), 0)
    return edges


def main():
    tu = parse(CALLS, clang_args("c11"))
    print("== fact 1: production walk never visits CALL_EXPR ==")
    print("CALL_EXPR seen by production-style walk:", count_via_production_walk(tu))

    print("\n== facts 1,2,6: body-descent edges (calls.c) ==")
    edges = body_descent(tu)
    print(f"total CALL_EXPR sites via body descent: {len(edges)}")
    for caller, callee, sp, line, col, cond in edges:
        print(f"  L{line}:{col} cond={int(cond)}  {sp}")
        print(f"      caller_usr={caller}")
        print(f"      callee_usr={callee}")


if __name__ == "__main__":
    main()

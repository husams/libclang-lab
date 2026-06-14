"""probe_graph_xtu — facts 2,7: callee USR emitted in an UNINDEXED TU + USR
stability across the two project TUs (mathlib.c def vs app.c call site)."""
import clang.cindex as cx
from _helpers import MANIFESTS, clang_args, parse

PROJ = MANIFESTS / "project"


def call_edges(path):
    tu = parse(path, clang_args("c11", extra_includes=[PROJ]))
    out = []

    def descend(node, caller_usr):
        for child in node.get_children():
            if child.kind == cx.CursorKind.CALL_EXPR:
                ref = child.referenced
                callee_usr = ref.get_usr() if ref is not None else "<none>"
                out.append((caller_usr, child.spelling, callee_usr))
            descend(child, caller_usr)

    for c in tu.cursor.get_children():
        f = c.location.file
        if f is None or f.name != tu.spelling:
            continue
        if c.kind == cx.CursorKind.FUNCTION_DECL and c.is_definition():
            descend(c, c.get_usr())
    return tu, out


def def_usr(path, spelling):
    """USR of the definition of `spelling` in its own TU."""
    tu = parse(path, clang_args("c11", extra_includes=[PROJ]))
    for c in tu.cursor.get_children():
        f = c.location.file
        if f is None or f.name != tu.spelling:
            continue
        if c.kind == cx.CursorKind.FUNCTION_DECL and c.spelling == spelling \
                and c.is_definition():
            return c.get_usr()
    return None


def main():
    print("== fact 2: app.c calls multiply()/add()/square(); callee defs are")
    print("   in mathlib.c (an UNINDEXED TU from app.c's perspective) ==")
    _, edges = call_edges(PROJ / "app.c")
    for caller, sp, callee in edges:
        print(f"  call {sp:10s} -> callee_usr={callee}")

    print("\n== fact 7: USR stability — multiply()'s USR ==")
    mult_def = def_usr(PROJ / "mathlib.c", "multiply")
    print(f"  multiply DEF usr (mathlib.c TU): {mult_def}")
    app_mult = [c for (_, s, c) in edges if s == "multiply"]
    print(f"  multiply CALL usr (app.c TU):    {app_mult[0] if app_mult else None}")
    print(f"  STABLE ACROSS TUs: {bool(app_mult) and app_mult[0] == mult_def}")


if __name__ == "__main__":
    main()

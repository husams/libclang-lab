"""5.5 Code metrics: LOC, max nesting depth, and branch count per function.

These are libclang-AST approximations, NOT a real control-flow graph: nesting is
the deepest stack of control/compound statements, and branches counts decision
nodes. Good enough to spot hot spots; not a substitute for a CFG-based tool.
"""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, walk, in_main_file, fatal_diagnostics

# Control statements that introduce a nesting level. We count the control
# statement itself, NOT its `{...}` COMPOUND_STMT body -- counting both would
# double every braced level (e.g. report depth 8 where the source means 4).
NESTERS = {
    cx.CursorKind.IF_STMT,
    cx.CursorKind.FOR_STMT,
    cx.CursorKind.WHILE_STMT,
}
# Statements that count as a decision/branch point.
BRANCHES = {
    cx.CursorKind.IF_STMT,
    cx.CursorKind.FOR_STMT,
    cx.CursorKind.WHILE_STMT,
    cx.CursorKind.DO_STMT,
    cx.CursorKind.CASE_STMT,
    cx.CursorKind.CONDITIONAL_OPERATOR,
}


def nesting_depth(cursor, level=0):
    """Deepest chain of NESTER statements below `cursor` (its own level counts)."""
    here = level + 1 if cursor.kind in NESTERS else level
    deepest = here
    for child in cursor.get_children():
        deepest = max(deepest, nesting_depth(child, here))
    return deepest


def metrics(func):
    ext = func.extent
    loc_lines = ext.end.line - ext.start.line + 1
    branches = sum(1 for c, _ in walk(func) if c.kind in BRANCHES)
    # Function body is level 0; each enclosing control statement adds a level.
    max_depth = nesting_depth(func)
    return loc_lines, max_depth, branches


def main():
    tu = parse(MANIFESTS / "messy.c", args=clang_args())
    if fatal_diagnostics(tu):
        raise SystemExit("parse failed: " + str(fatal_diagnostics(tu)))

    rows = []
    for c in tu.cursor.get_children():
        if (c.kind == cx.CursorKind.FUNCTION_DECL
                and c.is_definition() and in_main_file(c)):
            rows.append((c.spelling, *metrics(c)))
    rows.sort()

    print(f"{'function':<22}{'loc':>5}{'nest':>6}{'branch':>8}")
    for name, loc_lines, depth, branches in rows:
        print(f"{name:<22}{loc_lines:>5}{depth:>6}{branches:>8}")


if __name__ == "__main__":
    main()

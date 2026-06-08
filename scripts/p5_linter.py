"""5.3 Naming-convention linter: flag non-snake_case names and 1-letter params."""
import re

import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, walk, loc, in_main_file, fatal_diagnostics

# snake_case: lowercase start, only [a-z0-9_], no uppercase, no leading underscore.
SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


def main():
    tu = parse(MANIFESTS / "messy.c", args=clang_args())
    if fatal_diagnostics(tu):
        raise SystemExit("parse failed: " + str(fatal_diagnostics(tu)))

    findings = []  # (loc, rule, message)
    for c, _ in walk(tu.cursor):
        if not in_main_file(c) or not c.spelling:
            continue

        # Rule 1: functions must be snake_case.
        if c.kind == cx.CursorKind.FUNCTION_DECL and not SNAKE.match(c.spelling):
            findings.append((loc(c), "NAME_FUNC",
                             f"function '{c.spelling}' is not snake_case"))

        # Rule 2: file-scope globals must be snake_case (skip locals/params).
        elif (c.kind == cx.CursorKind.VAR_DECL
              and c.semantic_parent.kind == cx.CursorKind.TRANSLATION_UNIT
              and not SNAKE.match(c.spelling)):
            findings.append((loc(c), "NAME_GLOBAL",
                             f"global '{c.spelling}' is not snake_case"))

        # Rule 3: parameters should not be single letters.
        elif c.kind == cx.CursorKind.PARM_DECL and len(c.spelling) == 1:
            findings.append((loc(c), "PARAM_SHORT",
                             f"parameter '{c.spelling}' is a single letter"))

    findings.sort()
    for where, rule, msg in findings:
        print(f"{where}: {rule}: {msg}")
    print(f"--- {len(findings)} issue(s) found")


if __name__ == "__main__":
    main()

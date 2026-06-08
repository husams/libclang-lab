"""4.1 Diagnostics: read tu.diagnostics — severity, spelling, location, category, fix-its."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, loc

SEVERITY = {
    cx.Diagnostic.Ignored: "Ignored",
    cx.Diagnostic.Note: "Note",
    cx.Diagnostic.Warning: "Warning",
    cx.Diagnostic.Error: "Error",
    cx.Diagnostic.Fatal: "Fatal",
}


def diag_loc(d):
    """Compact 'file:line:col' for a diagnostic's location (mirrors helpers.loc)."""
    location = d.location
    if location.file is None:
        return "<no-file>"
    from pathlib import Path
    return f"{Path(location.file.name).name}:{location.line}:{location.column}"


def report(label, diags):
    print(label)
    for d in diags:
        sev = SEVERITY.get(d.severity, str(d.severity))
        print(f"  [{sev}] {diag_loc(d)}: {d.spelling}")
        print(f"        category={d.category_name!r}")
        for fx in d.fixits:
            print(f"        fix-it: insert {fx.value!r}")
    if not diags:
        print("  (none)")


def main():
    # Case A: a real C syntax error. A missing ';' produces an Error plus a
    # fix-it hint — clang knows exactly how to repair it.
    src = "int answer(void) { int x = 41 return x; }\n"
    broken = parse("virtual.c", args=["-std=c11"],
                   unsaved_files=[("virtual.c", src)])
    report("A. missing-semicolon snippet (parsed in-memory):",
           sorted(broken.diagnostics, key=lambda d: (d.location.line, d.location.column)))

    # Case B: the #1 libclang gotcha. Parsing with args=[] strips the builtin
    # header path, so <stddef.h> (pulled in by shapes.h) is not found. That is
    # a FATAL diagnostic, and the AST below it is silently TRUNCATED.
    # Home of this gotcha: Part 1 §1.2 — here we only read the diagnostic.
    truncated = parse(MANIFESTS / "shapes.c", args=[])
    fatals = sorted((d for d in truncated.diagnostics
                     if d.severity >= cx.Diagnostic.Fatal),
                    key=lambda d: d.spelling)
    report("B. shapes.c parsed with args=[] (truncating fatal):", fatals)


if __name__ == "__main__":
    main()

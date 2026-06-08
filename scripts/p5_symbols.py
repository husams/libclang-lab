"""5.1 Symbol extractor: dump shapes.c's top-level decls as sorted JSON."""
import json

import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, loc, top_level, fatal_diagnostics

# Cursor kinds we treat as exported "symbols", with a friendly label.
KINDS = {
    cx.CursorKind.FUNCTION_DECL: "function",
    cx.CursorKind.STRUCT_DECL: "struct",
    cx.CursorKind.ENUM_DECL: "enum",
    cx.CursorKind.TYPEDEF_DECL: "typedef",
}


def main():
    tu = parse(MANIFESTS / "shapes.c", args=clang_args())
    if fatal_diagnostics(tu):
        raise SystemExit("parse failed: " + str(fatal_diagnostics(tu)))

    symbols = []
    for c in top_level(tu):
        kind = KINDS.get(c.kind)
        if kind is None or not c.spelling:
            continue
        symbols.append({
            "name": c.spelling,
            "kind": kind,
            "location": loc(c),
            "signature": c.displayname,
            "usr": c.get_usr(),
        })

    # Deterministic: sort by (kind, name) so output never depends on AST order.
    symbols.sort(key=lambda s: (s["kind"], s["name"]))
    print(json.dumps(symbols, indent=2))


if __name__ == "__main__":
    main()

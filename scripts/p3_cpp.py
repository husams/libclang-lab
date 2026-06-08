"""3.7 C++ semantics: namespaces, access, inheritance, virtual/pure, templates."""
from pathlib import Path

import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, loc, walk

# The TU is geometry.cpp, but the declarations live in geometry.hpp, so
# in_main_file() would drop them all. Filter by basename set instead; this also
# walls off the thousands of libc++ nodes pulled in by <string>/<vector>.
# (Main-file filtering as a gotcha is owned by Part 2 §2.4.)
KEEP = {"geometry.cpp", "geometry.hpp"}


def mine(c):
    f = c.location.file
    return f is not None and Path(f.name).name in KEEP


def main():
    tu = parse(MANIFESTS / "geometry.cpp", args=clang_args(std="c++17"))
    nodes = [c for c, _ in walk(tu.cursor) if mine(c)]

    ns = sorted({c.spelling for c in nodes if c.kind == cx.CursorKind.NAMESPACE})
    print(f"NAMESPACE        : {ns}")

    print("INHERITANCE      :")
    for c in sorted((c for c in nodes if c.kind == cx.CursorKind.CLASS_DECL),
                    key=lambda c: c.spelling):
        bases = [(b.spelling, b.access_specifier.name)
                 for b in c.get_children() if b.kind == cx.CursorKind.CXX_BASE_SPECIFIER]
        print(f"  class {c.spelling:<8} bases={bases or '[]'}")

    print("METHODS (in-class declarations):")
    seen = set()
    for m in sorted((c for c in nodes if c.kind == cx.CursorKind.CXX_METHOD),
                    key=lambda c: (c.spelling, c.location.line)):
        if m.semantic_parent.kind != cx.CursorKind.CLASS_DECL:
            continue
        if Path(m.location.file.name).name != "geometry.hpp":  # the in-class decl, not the .cpp defn
            continue
        key = (m.semantic_parent.spelling, m.spelling)
        if key in seen:
            continue
        seen.add(key)
        print(f"  {m.semantic_parent.spelling}::{m.spelling:<6} "
              f"access={m.access_specifier.name:<9} "
              f"virtual={m.is_virtual_method()} pure={m.is_pure_virtual_method()}")

    print("FIELDS           :")
    for f in sorted((c for c in nodes if c.kind == cx.CursorKind.FIELD_DECL),
                    key=lambda c: c.spelling):
        print(f"  {f.spelling:<8} {f.type.spelling:<12} access={f.access_specifier.name}")

    print("TEMPLATES (libclang shows the PATTERN, not instantiations -> Part 6):")
    for t in sorted((c for c in nodes
                     if c.kind in (cx.CursorKind.FUNCTION_TEMPLATE, cx.CursorKind.CLASS_TEMPLATE)),
                    key=lambda c: c.spelling):
        print(f"  {t.kind.name:<17} {t.spelling}  @ {loc(t)}")

    # Out-of-line definition: lexical parent (where written) != semantic parent
    # (where it belongs). This is the C++ case promised in 3.6.
    outofline = next(c for c in nodes
                     if c.kind == cx.CursorKind.CXX_METHOD and c.spelling == "area"
                     and Path(c.location.file.name).name == "geometry.cpp")
    print(f"OUT-OF-LINE Circle::area @ {loc(outofline)}:")
    print(f"  lexical_parent ={outofline.lexical_parent.kind.name} "
          f"{outofline.lexical_parent.spelling!r}  "
          f"semantic_parent={outofline.semantic_parent.kind.name} "
          f"{outofline.semantic_parent.spelling!r}")


if __name__ == "__main__":
    main()

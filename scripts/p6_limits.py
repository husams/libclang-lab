"""6.6 Limits of libclang: template instantiations are only partially exposed."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, walk, clang_args, in_main_file, loc


def main():
    # geometry.cpp calls the function template max_of<double> inside widest().
    cpp = parse(MANIFESTS / "geometry.cpp", args=clang_args(std="c++17"))

    # Observation 1: the template DEFINITION lives in geometry.hpp, so a
    # main-file-only view of geometry.cpp shows NO template decls at all. The
    # full TU does expose them, but mixed in with thousands of libc++ nodes --
    # see Part 2 §2.4 for why you must filter to the main file.
    mf_templates = sorted(
        c.spelling for c in walk_kinds(
            cpp, cx.CursorKind.FUNCTION_TEMPLATE, cx.CursorKind.CLASS_TEMPLATE
        ) if in_main_file(c)
    )
    print(f"main-file template decls in geometry.cpp: {mf_templates}")

    # Observation 2: at the call site, .referenced does NOT point at the
    # FUNCTION_TEMPLATE. libclang resolves it to a synthesized FUNCTION_DECL --
    # the max_of<double> instantiation -- which carries a body but no template
    # parameters. The instantiation relationship itself is not fully modeled.
    for c, _ in walk(cpp.cursor):
        if c.kind == cx.CursorKind.CALL_EXPR and c.spelling == "max_of" and in_main_file(c):
            ref = c.referenced
            kids = [str(k.kind).split(".")[-1] for k in ref.get_children()]
            print(f"call 'max_of' at {loc(c)}:")
            print(f"  referenced kind : {str(ref.kind).split('.')[-1]}  (NOT FUNCTION_TEMPLATE)")
            print(f"  is_definition   : {ref.is_definition()}")
            print(f"  exposed children: {kids}")
            break

    print("takeaway: implicit/instantiated nodes are a partial view; for full")
    print("template-aware analysis drop to LibTooling / AST Matchers (C++).")


def walk_kinds(tu, *kinds):
    for c, _ in walk(tu.cursor):
        if c.kind in kinds:
            yield c


if __name__ == "__main__":
    main()

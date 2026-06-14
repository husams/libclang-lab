"""probe_graph_cpp — facts 3,4,5: inheritance, membership, templates on
geometry.hpp/.cpp.

  fact 3: CXX_BASE_SPECIFIER -> derived->base USR, access, virtual base
  fact 4: FIELD_DECL/CXX_METHOD -> field_of/method_of (dst=owning record USR),
          member access from access_specifier
  fact 5: CLASS_TEMPLATE params + INSTANTIATES/SPECIALIZES + template args
"""
import clang.cindex as cx
from _helpers import MANIFESTS, clang_args, parse

HPP = MANIFESTS / "geometry.hpp"
CPP = MANIFESTS / "geometry.cpp"

ACCESS = {cx.AccessSpecifier.PUBLIC: "public",
          cx.AccessSpecifier.PROTECTED: "protected",
          cx.AccessSpecifier.PRIVATE: "private"}


def walk_main(tu):
    """Yield (cursor, enclosing_record_cursor) pairs in main file."""
    def rec(c, enclosing):
        for ch in c.get_children():
            f = ch.location.file
            if f is None or f.name != tu.spelling:
                continue
            yield ch, enclosing
            nxt = ch if ch.kind in (cx.CursorKind.CLASS_DECL,
                                    cx.CursorKind.STRUCT_DECL,
                                    cx.CursorKind.CLASS_TEMPLATE) else enclosing
            yield from rec(ch, nxt)
    yield from rec(tu.cursor, None)


def main():
    tu = parse(HPP, clang_args("c++17"))

    print("== fact 3: CXX_BASE_SPECIFIER (inherits) ==")
    for c, enc in walk_main(tu):
        if c.kind == cx.CursorKind.CXX_BASE_SPECIFIER:
            derived = enc                      # lexical enclosing class
            base = c.referenced or c.type.get_declaration()
            access = ACCESS.get(c.access_specifier)
            # Python binding lacks is_virtual_base(); call the C API directly.
            is_virtual = bool(cx.conf.lib.clang_isVirtualBase(c))
            print(f"  derived={derived.spelling} base={base.spelling} "
                  f"access={access} virtual={int(is_virtual)}")
            print(f"     derived_usr={derived.get_usr()}")
            print(f"     base_usr   ={base.get_usr()}")

    print("\n== fact 4: FIELD_DECL/CXX_METHOD membership (dst=owning record) ==")
    for c, _enc in walk_main(tu):
        if c.kind in (cx.CursorKind.FIELD_DECL, cx.CursorKind.CXX_METHOD):
            owner = c.semantic_parent
            edge = "field_of" if c.kind == cx.CursorKind.FIELD_DECL else "method_of"
            oname = owner.spelling if owner else "?"
            print(f"  {edge:9s} {c.spelling:14s} owner={oname:8s} "
                  f"access={ACCESS.get(c.access_specifier)} "
                  f"is_virtual={int(bool(cx.conf.lib.clang_CXXMethod_isVirtual(c)))}")

    print("\n== fact 5a: CLASS_TEMPLATE params (the <typename T> side) ==")
    for c, _enc in walk_main(tu):
        if c.kind in (cx.CursorKind.CLASS_TEMPLATE,
                      cx.CursorKind.FUNCTION_TEMPLATE):
            params = [ch for ch in c.get_children()
                      if ch.kind in (cx.CursorKind.TEMPLATE_TYPE_PARAMETER,
                                     cx.CursorKind.TEMPLATE_NON_TYPE_PARAMETER,
                                     cx.CursorKind.TEMPLATE_TEMPLATE_PARAMETER)]
            print(f"  {c.kind.name} {c.spelling}: "
                  f"params={[(p.spelling, p.kind.name) for p in params]}")

    print("\n== fact 5b: instantiations in geometry.cpp ==")
    tu2 = parse(CPP, clang_args("c++17"))
    # find a type that is a template instantiation: std::vector<double> in widest
    seen = set()
    for c, _enc in walk_main(tu2):
        t = c.type
        decl = t.get_declaration()
        try:
            nargs = t.get_num_template_arguments()
        except Exception:
            nargs = -1
        if nargs > 0 and decl is not None and decl.spelling:
            key = (t.spelling, decl.spelling)
            if key in seen:
                continue
            seen.add(key)
            args = []
            for i in range(nargs):
                at = t.get_template_argument_type(i)
                args.append(at.spelling)
            spec = decl.specialized_template if hasattr(decl, "specialized_template") else None
            print(f"  inst-type={t.spelling!r} -> template-decl={decl.spelling!r}")
            print(f"     decl_kind={decl.kind.name} nargs={nargs} args={args}")


if __name__ == "__main__":
    main()

"""probe_graph_spec — confirm SPECIALIZES + OVERRIDES + conditional-call via
in-memory buffers (no manifest changes). Uses C APIs the Python binding under-
exposes: clang_getSpecializedCursorTemplate, clang_getOverriddenCursors."""
import ctypes
import clang.cindex as cx
from _helpers import clang_args, parse

SPEC_SRC = r"""
template <typename T> struct Holder { T v; };
template <> struct Holder<int> { int v; long extra; };  // explicit spec
template struct Holder<double>;                          // explicit instantiation

struct Base { virtual void f(); virtual ~Base() {} };
struct Derived : Base { void f() override; };

int pick(int n) {
    int r = 0;
    if (n > 0) { r = pick(n - 1); }   // call inside IF -> conditional
    return r;
}
"""


# Register C signatures the Python binding under-exposes.
cx.conf.lib.clang_getSpecializedCursorTemplate.restype = cx.Cursor
cx.conf.lib.clang_getSpecializedCursorTemplate.argtypes = [cx.Cursor]


def walk(tu):
    def rec(c):
        for ch in c.get_children():
            f = ch.location.file
            if f is None or f.name != tu.spelling:
                continue
            yield ch
            yield from rec(ch)
    yield from rec(tu.cursor)


def overridden(cursor):
    """clang_getOverriddenCursors -> list[Cursor] (binding under-exposes it)."""
    arr = ctypes.POINTER(cx.Cursor)()
    n = ctypes.c_uint()
    cx.conf.lib.clang_getOverriddenCursors(cursor, ctypes.byref(arr),
                                           ctypes.byref(n))
    out = []
    for i in range(n.value):
        cur = arr[i]
        cur._tu = cursor._tu
        out.append((cur.spelling, cur.get_usr()))
    cx.conf.lib.clang_disposeOverriddenCursors(arr)
    return out


def main():
    tu = parse("spec.cpp", clang_args("c++17"),
               unsaved_files=[("spec.cpp", SPEC_SRC)])

    print("== SPECIALIZES: specialization decl -> primary template ==")
    for c in walk(tu):
        if c.kind in (cx.CursorKind.CLASS_DECL, cx.CursorKind.STRUCT_DECL,
                      cx.CursorKind.CLASS_TEMPLATE,
                      cx.CursorKind.STRUCT_DECL):
            prim = cx.conf.lib.clang_getSpecializedCursorTemplate(c)
            if prim is None or cx.conf.lib.clang_Cursor_isNull(prim):
                continue
            prim._tu = c._tu
            if True:
                print(f"  {c.kind.name} {c.spelling!r} ({c.type.spelling!r}) "
                      f"specializes -> {prim.spelling!r}")
                print(f"     spec_usr={c.get_usr()}")
                print(f"     prim_usr={prim.get_usr()}")

    print("\n== OVERRIDES: Derived::f overrides Base::f ==")
    for c in walk(tu):
        if c.kind == cx.CursorKind.CXX_METHOD:
            ov = overridden(c)
            if ov:
                print(f"  {c.semantic_parent.spelling}::{c.spelling} overrides {ov}")
                print(f"     src_usr={c.get_usr()}")

    print("\n== conditional CALL_EXPR (call inside IF_STMT) ==")
    COND = {cx.CursorKind.IF_STMT, cx.CursorKind.FOR_STMT,
            cx.CursorKind.WHILE_STMT, cx.CursorKind.DO_STMT,
            cx.CursorKind.CONDITIONAL_OPERATOR, cx.CursorKind.SWITCH_STMT}

    def descend(node, caller, cond):
        for ch in node.get_children():
            nc = cond + (1 if ch.kind in COND else 0)
            if ch.kind == cx.CursorKind.CALL_EXPR:
                print(f"  call {ch.spelling} cond={int(nc>0)} (caller={caller})")
            descend(ch, caller, nc)

    for c in walk(tu):
        if c.kind == cx.CursorKind.FUNCTION_DECL and c.is_definition():
            descend(c, c.spelling, 0)


if __name__ == "__main__":
    main()

"""probe_graph_edges.py — ground-truth probe for the cidx graph layer (v6->v7).

Confirms, against the lab manifests, exactly what the C++/Python edge-extraction
walk will see. Run from the repo root:

    python3 libclang-lab/scripts/probe_graph_edges.py
"""
import clang.cindex as cx
from _helpers import MANIFESTS, clang_args, parse

CK = cx.CursorKind

# Conditional-ancestor kinds: a CALL_EXPR whose nearest enclosing statement of
# one of these kinds means the call site is conditional (design §2 edge_site).
COND_KINDS = {
    CK.IF_STMT, CK.FOR_STMT, CK.WHILE_STMT, CK.DO_STMT,
    CK.CONDITIONAL_OPERATOR, CK.SWITCH_STMT, CK.CASE_STMT,
}

# Function-like cursors whose bodies the cidx symbol walk does NOT descend.
FUNCTION_KINDS = {
    CK.FUNCTION_DECL, CK.CXX_METHOD, CK.CONSTRUCTOR,
    CK.DESTRUCTOR, CK.FUNCTION_TEMPLATE,
}


def usr(c):
    return c.get_usr() if c is not None else None


def enclosing_function(cursor):
    """Walk semantic parents to the nearest function-like cursor (the caller)."""
    c = cursor.semantic_parent
    while c is not None and c.kind != CK.TRANSLATION_UNIT:
        if c.kind in FUNCTION_KINDS:
            return c
        c = c.semantic_parent
    return None


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# ---------------------------------------------------------------------------
# PROBE 1: confirm bodies are NOT walked by the symbol visitor, then design a
# recursive body-descent that yields (caller_usr, callee_usr, loc, conditional).
# ---------------------------------------------------------------------------
def probe_calls():
    hr("PROBE 1+6: calls.c — body walk skipped today; recursive descent + cond")
    path = str(MANIFESTS / "calls.c")
    tu = parse(path, clang_args(std="c11"))

    # 1a. Prove the cidx symbol walk never sees a CALL_EXPR: replicate its
    # CONTINUE-on-function-like rule and count CALL_EXPRs reached.
    seen_calls_symbolwalk = 0

    def symbol_walk(cursor):
        nonlocal seen_calls_symbolwalk
        for child in cursor.get_children():
            f = child.location.file
            if f is None or f.name != path:
                continue
            if child.kind == CK.CALL_EXPR:
                seen_calls_symbolwalk += 1
            if child.kind not in FUNCTION_KINDS:
                symbol_walk(child)
            # function-like: STOP (mirrors CXChildVisit_Continue) — body skipped

    symbol_walk(tu.cursor)
    print(f"CALL_EXPRs reached by the cidx symbol walk (body skipped): "
          f"{seen_calls_symbolwalk}  (expect 0)")

    # 1b. Full recursive body descent (the new edge walk). For each function
    # definition in the main file, descend its WHOLE subtree collecting CALL_EXPRs.
    pairs = []         # (caller_usr, callee_usr, line, col, conditional)
    total_call_exprs = 0

    def has_cond_ancestor(node, stop_at):
        """True if node has a COND_KINDS ancestor up to (not past) the function."""
        p = node.semantic_parent
        # semantic_parent jumps to the function for body statements, so walk the
        # lexical parent chain via a manual DFS instead (below in descend()).
        return False  # replaced by lexical tracking in descend()

    def descend(node, caller, cond_depth):
        nonlocal total_call_exprs
        for child in node.get_children():
            child_cond = cond_depth + (1 if child.kind in COND_KINDS else 0)
            if child.kind == CK.CALL_EXPR:
                total_call_exprs += 1
                ref = child.referenced
                callee = usr(ref)
                loc = child.location
                pairs.append((
                    usr(caller), callee,
                    loc.line, loc.column,
                    1 if cond_depth > 0 else 0,
                    ref.spelling if ref is not None else "<unresolved>",
                ))
            descend(child, caller, child_cond)

    for c in tu.cursor.get_children():
        f = c.location.file
        if f is None or f.name != path:
            continue
        if c.kind in FUNCTION_KINDS and c.is_definition():
            descend(c, c, 0)

    print(f"total CALL_EXPRs found by recursive body descent: {total_call_exprs}")
    print("\ncaller -> callee  (spelling)  @line:col  conditional")
    print("-" * 72)
    for caller, callee, line, col, cond, callee_name in pairs:
        cn = caller.split("@F@")[-1] if caller else "?"
        print(f"  {cn:>8} -> {callee_name:<10} @{line}:{col}  cond={cond}")
    print("\nUSR pairs (caller_usr -> callee_usr):")
    for caller, callee, line, col, cond, _n in pairs:
        print(f"  {caller}  ->  {callee}")

    # Positive conditional case: a call genuinely inside an if/for/?: branch.
    print("\nconditional-detection positive check (synthetic buffer):")
    src = """
int g(int x);
int h(int x);
int f(int x) {
    if (x > 0) return g(x);       /* conditional call: cond=1 */
    for (int i = 0; i < x; i++) h(i);  /* conditional call: cond=1 */
    return g(x);                  /* unconditional: cond=0 */
}
"""
    bufpath = str(MANIFESTS / "_probe_cond.c")
    tu2 = parse(bufpath, clang_args(std="c11"), unsaved_files=[(bufpath, src)])

    def descend2(node, caller, cond_depth):
        for child in node.get_children():
            cd = cond_depth + (1 if child.kind in COND_KINDS else 0)
            if child.kind == CK.CALL_EXPR:
                ref = child.referenced
                print(f"    {caller.spelling} -> "
                      f"{ref.spelling if ref else '?':<8} "
                      f"@{child.location.line}  cond={1 if cond_depth > 0 else 0}")
            descend2(child, caller, cd)

    for c in tu2.cursor.get_children():
        if c.kind in FUNCTION_KINDS and c.is_definition():
            descend2(c, c, 0)
    return pairs


# ---------------------------------------------------------------------------
# PROBE 2: cross-TU USR identity (mathlib multiply/square called from app.c).
# ---------------------------------------------------------------------------
def probe_cross_tu():
    hr("PROBE 2: cross-TU USR identity (project/ app.c calls vs mathlib.c defs)")
    proj = MANIFESTS / "project"
    args = clang_args(std="c11") + ["-I", str(proj)]

    # Definitions live in mathlib.c
    defs = {}
    tu_def = parse(str(proj / "mathlib.c"), args)
    for c in tu_def.cursor.get_children():
        if c.kind == CK.FUNCTION_DECL and c.is_definition():
            defs[c.spelling] = c.get_usr()
    print("mathlib.c definition USRs:")
    for name, u in defs.items():
        print(f"  {name:>10}: {u}")

    # Calls live in app.c
    tu_app = parse(str(proj / "app.c"), args)
    call_usrs = {}

    def descend(node):
        for child in node.get_children():
            if child.kind == CK.CALL_EXPR and child.referenced is not None:
                call_usrs.setdefault(child.referenced.spelling,
                                     child.referenced.get_usr())
            descend(child)

    descend(tu_app.cursor)
    print("\napp.c call-site referenced USRs:")
    for name, u in sorted(call_usrs.items()):
        match = ""
        if name in defs:
            match = "  == mathlib.c def" if u == defs[name] else "  != MISMATCH"
        print(f"  {name:>10}: {u}{match}")

    for name in ("multiply", "square"):
        ok = name in call_usrs and name in defs and call_usrs[name] == defs[name]
        print(f"  cross-TU USR equality for {name!r}: {'YES' if ok else 'NO'}")


# ---------------------------------------------------------------------------
# PROBE 3: CXX_BASE_SPECIFIER — derived->base USR, access, virtual.
# ---------------------------------------------------------------------------
def probe_inheritance():
    hr("PROBE 3: geometry.hpp inheritance (base USR, access, virtual)")
    # Parse geometry.cpp so the .hpp is pulled in with full C++ semantics.
    path = str(MANIFESTS / "geometry.cpp")
    tu = parse(path, clang_args(std="c++17"))
    hpp = str(MANIFESTS / "geometry.hpp")

    def find_classes(node):
        for child in node.get_children():
            if child.kind in (CK.CLASS_DECL, CK.STRUCT_DECL) and child.is_definition():
                f = child.location.file
                if f is not None and f.name == hpp:
                    yield child
            yield from find_classes(child)

    for cls in find_classes(tu.cursor):
        for ch in cls.get_children():
            if ch.kind == CK.CXX_BASE_SPECIFIER:
                base = ch.referenced or ch.type.get_declaration()
                access = ch.access_specifier.name
                # is_virtual_base is exposed only on the C API
                # (clang_isVirtualBase); the Python binding may lack the
                # wrapper, so guard it. The C++ port calls clang_isVirtualBase.
                virt = getattr(ch, "is_virtual_base", lambda: "<C-API only>")()
                print(f"  {cls.spelling} --inherits--> {base.spelling}")
                print(f"      derived_usr: {cls.get_usr()}")
                print(f"      base_usr   : {base.get_usr()}")
                print(f"      access     : {access}   virtual: {virt}")


# ---------------------------------------------------------------------------
# PROBE 4: FIELD_DECL / CXX_METHOD -> field_of / method_of (owner USR + access).
# ---------------------------------------------------------------------------
def probe_members():
    hr("PROBE 4: field_of / method_of for class geo::Shape (owner USR + access)")
    path = str(MANIFESTS / "geometry.cpp")
    tu = parse(path, clang_args(std="c++17"))
    hpp = str(MANIFESTS / "geometry.hpp")

    fields = methods = 0

    def find_shape(node):
        for child in node.get_children():
            if (child.kind == CK.CLASS_DECL and child.spelling == "Shape"
                    and child.is_definition()):
                f = child.location.file
                if f is not None and f.name == hpp:
                    return child
            r = find_shape(child)
            if r is not None:
                return r
        return None

    shape = find_shape(tu.cursor)
    print(f"class {shape.spelling}  usr={shape.get_usr()}")
    for ch in shape.get_children():
        if ch.kind == CK.FIELD_DECL:
            fields += 1
            print(f"  field_of : {ch.spelling:<8} access={ch.access_specifier.name}"
                  f"  member_usr={ch.get_usr()}")
        elif ch.kind in (CK.CXX_METHOD, CK.CONSTRUCTOR, CK.DESTRUCTOR):
            methods += 1
            print(f"  method_of: {ch.spelling:<10} access={ch.access_specifier.name}"
                  f"  kind={ch.kind.name}")
    print(f"  -> {fields} field_of, {methods} method_of edges for Shape")


# ---------------------------------------------------------------------------
# PROBE 5: templates — params on primary template; args on instantiations.
# ---------------------------------------------------------------------------
def probe_templates():
    hr("PROBE 5: templates — template_param (primary) + template_arg (instances)")
    # Build a TU that actually instantiates Box<int> so an arg whose type is a
    # known symbol (and a non-type literal case) both appear.
    src = """
#include "geometry.hpp"
struct Widget { int w; };
geo::Box<int>     bi(1);
geo::Box<Widget>  bw(Widget{});
template <typename T, int N> struct Arr { T data[N]; };
Arr<double, 3> ad;
"""
    unsaved = [(str(MANIFESTS / "_probe_tpl.cpp"), src)]
    tu = parse(str(MANIFESTS / "_probe_tpl.cpp"),
               clang_args(std="c++17"), unsaved_files=unsaved)

    # 5a. primary-template params (the <typename T, int N> side)
    print("primary CLASS_TEMPLATE params:")

    def walk(node):
        for child in node.get_children():
            yield child
            yield from walk(child)

    for c in walk(tu.cursor):
        if c.kind == CK.CLASS_TEMPLATE and c.spelling in ("Box", "Arr"):
            print(f"  template {c.spelling}  usr={c.get_usr()}")
            pos = 0
            for p in c.get_children():
                if p.kind == CK.TEMPLATE_TYPE_PARAMETER:
                    print(f"    param[{pos}] kind=type  name={p.spelling}")
                    pos += 1
                elif p.kind == CK.TEMPLATE_NON_TYPE_PARAMETER:
                    print(f"    param[{pos}] kind=non-type  name={p.spelling}"
                          f"  type={p.type.spelling}")
                    pos += 1

    # 5b. template_arg on instantiated specialization types (VarDecl types).
    print("\ninstantiation template_args (ref_id joins back to a symbol):")
    for c in tu.cursor.get_children():
        if c.kind != CK.VAR_DECL:
            continue
        decl = c.type.get_declaration()
        n = decl.get_num_template_arguments()
        if n <= 0:
            continue
        print(f"  {c.spelling}: type={c.type.spelling}  "
              f"specialization_usr={decl.get_usr()}  num_args={n}")
        for i in range(n):
            akind = decl.get_template_argument_kind(i)
            if akind == cx.TemplateArgumentKind.TYPE:
                t = decl.get_template_argument_type(i)
                tdecl = t.get_declaration()
                ref_usr = tdecl.get_usr() if tdecl is not None else ""
                print(f"    arg[{i}] kind=TYPE  type={t.spelling!r}  "
                      f"ref_usr={ref_usr!r}")
            elif akind == cx.TemplateArgumentKind.INTEGRAL:
                val = decl.get_template_argument_value(i)
                print(f"    arg[{i}] kind=INTEGRAL  literal={val}")
            else:
                print(f"    arg[{i}] kind={akind}")


if __name__ == "__main__":
    probe_calls()
    probe_cross_tu()
    probe_inheritance()
    probe_members()
    probe_templates()
    print("\n[probe_graph_edges] done")

"""Ground-truth API smoke test for the libclang lab.

Exercises every clang.cindex API the lab teaches and asserts the results, so we
KNOW these patterns work on the installed libclang before writing the lessons.
Run: python3 libclang-lab/scripts/_smoke_test.py
"""
import json

import clang.cindex as cx
from _helpers import MANIFESTS, parse, walk, loc, in_main_file, clang_args, fatal_diagnostics

ok = []
def check(label, cond, detail=""):
    ok.append((label, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}  {detail}")

print("clang.cindex loaded from:", cx.__file__)
print("clang_args():", clang_args())
print("=" * 60)

# --- Part 1: Index / TranslationUnit / Cursor ---
tu = parse(MANIFESTS / "shapes.c", args=clang_args())
check("shapes.c parses with no errors", not fatal_diagnostics(tu),
      str([d.spelling for d in fatal_diagnostics(tu)]))
check("parse returns TU", tu is not None)
check("tu.spelling is the file", tu.spelling.endswith("shapes.c"), tu.spelling)
root = tu.cursor
check("root kind TRANSLATION_UNIT", root.kind == cx.CursorKind.TRANSLATION_UNIT, str(root.kind))

# walk + main-file filter
nodes = [c for c, _ in walk(root)]
check("walk yields many nodes", len(nodes) > 20, f"{len(nodes)} nodes")
funcs = [c for c in nodes if c.kind == cx.CursorKind.FUNCTION_DECL and in_main_file(c)]
fnames = sorted(f.spelling for f in funcs)
check("found shape functions", "shape_area" in fnames and "average" in fnames, str(fnames))

# --- Part 2: names, location, extent, tokens ---
area = next(f for f in funcs if f.spelling == "shape_area")
check("displayname has signature", "(" in area.displayname, area.displayname)
check("get_usr non-empty", bool(area.get_usr()), area.get_usr())
ext = area.extent
check("extent has start/end", ext.start.line >= 1 and ext.end.line >= ext.start.line,
      f"{ext.start.line}-{ext.end.line}")
toks = [t.spelling for t in area.get_tokens()]
check("tokens include 'shape_area'", "shape_area" in toks, f"{len(toks)} tokens")
check("token kinds available", all(isinstance(t.kind, cx.TokenKind) for t in area.get_tokens()))

# --- Part 3: types ---
t = area.type
check("function type kind", t.kind == cx.TypeKind.FUNCTIONPROTO, str(t.kind))
check("result_type is double", area.result_type.spelling == "double", area.result_type.spelling)
args = list(area.get_arguments())
check("shape_area has 1 arg", len(args) == 1, str([a.spelling for a in args]))
ptype = args[0].type
check("arg is const Shape *", ptype.kind == cx.TypeKind.POINTER, str(ptype.kind))
pointee = ptype.get_pointee()
check("pointee const-qualified", pointee.is_const_qualified(), pointee.spelling)

avg = next(f for f in funcs if f.spelling == "average")
check("average is variadic", avg.type.is_function_variadic(), str(avg.type.is_function_variadic()))

# struct fields
struct = next((c for c, _ in walk(root)
               if c.kind == cx.CursorKind.STRUCT_DECL and c.spelling == "Shape"), None)
check("found struct Shape", struct is not None)
fields = [f.spelling for f in struct.type.get_fields()] if struct else []
check("Shape fields", fields == ["kind", "origin", "dimensions", "name"], str(fields))

# typedef underlying
typedef = next((c for c, _ in walk(root)
                if c.kind == cx.CursorKind.TYPEDEF_DECL and c.spelling == "ShapeKind"), None)
check("typedef ShapeKind underlying is enum",
      typedef is not None and typedef.underlying_typedef_type.kind == cx.TypeKind.ELABORATED,
      typedef.underlying_typedef_type.spelling if typedef else "?")

# semantic links: referenced / get_definition / canonical
call = next((c for c, _ in walk(root)
             if c.kind == cx.CursorKind.CALL_EXPR and c.spelling == "shape_area"), None)
check("call references a decl", call is not None and call.referenced is not None,
      call.referenced.spelling if call and call.referenced else "?")

# --- C++: access specifiers, namespaces, templates ---
cpp = parse(MANIFESTS / "geometry.cpp", args=clang_args(std="c++17"))
check("C++ parse ok", cpp is not None)
methods = [c for c, _ in walk(cpp.cursor)
           if c.kind == cx.CursorKind.CXX_METHOD and in_main_file(c)]
check("found C++ methods", len(methods) >= 1, str(sorted({m.spelling for m in methods})))
if methods:
    m = methods[0]
    check("access specifier available", m.access_specifier in (
        cx.AccessSpecifier.PUBLIC, cx.AccessSpecifier.PROTECTED, cx.AccessSpecifier.PRIVATE),
        str(m.access_specifier))
ns = [c for c, _ in walk(cpp.cursor) if c.kind == cx.CursorKind.NAMESPACE]
check("found namespace geo", any(n.spelling == "geo" for n in ns), str([n.spelling for n in ns]))
tmpl = [c for c, _ in walk(cpp.cursor)
        if c.kind in (cx.CursorKind.FUNCTION_TEMPLATE, cx.CursorKind.CLASS_TEMPLATE)]
check("found a template", len(tmpl) >= 1, str([t.spelling for t in tmpl]))

# --- Part 4: diagnostics ---
bad = parse(MANIFESTS / "geometry.cpp")  # no -std/-I: should produce diagnostics or still parse
diags = list(bad.diagnostics)
check("diagnostics iterable", isinstance(diags, list), f"{len(diags)} diags")

# detailed preprocessing record -> macro definitions
pp = parse(MANIFESTS / "macros.c",
           args=clang_args(),
           options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
macros = [c.spelling for c, _ in walk(pp.cursor)
          if c.kind == cx.CursorKind.MACRO_DEFINITION and in_main_file(c)]
check("macro defs captured", "VERSION" in macros and "ADD" in macros, str(sorted(macros)))
incs = [c for c, _ in walk(pp.cursor) if c.kind == cx.CursorKind.INCLUSION_DIRECTIVE]
check("inclusion directives captured", len(incs) >= 1, f"{len(incs)} includes")

# --- Part 4/5: CompilationDatabase ---
proj = MANIFESTS / "project"
cdb = cx.CompilationDatabase.fromDirectory(str(proj))
cmds = list(cdb.getAllCompileCommands())
check("compilation db loads", len(cmds) == 2, f"{len(cmds)} commands")
first_args = list(cmds[0].arguments)
check("compile command has args", len(first_args) > 1, str(first_args[:3]))

# --- Part 5: USR cross-file references ---
mathlib = parse(proj / "mathlib.c", args=clang_args(extra_includes=[proj]))
app = parse(proj / "app.c", args=clang_args(extra_includes=[proj]))
def usr_refs(tu, name):
    out = []
    for c, _ in walk(tu.cursor):
        if c.kind == cx.CursorKind.CALL_EXPR and c.spelling == name and c.referenced:
            out.append((tu.spelling.split("/")[-1], c.referenced.get_usr()))
    return out
multiply_refs = usr_refs(mathlib, "multiply") + usr_refs(app, "multiply")
usrs = {u for _, u in multiply_refs}
check("multiply USR stable across files", len(usrs) == 1 and multiply_refs,
      f"{len(multiply_refs)} call sites, {len(usrs)} distinct USR")

# --- Part 6: unsaved files (in-memory editing) ---
src = "int answer(void) { return 42; }\n"
u = parse("virtual.c", args=["-std=c11"], unsaved_files=[("virtual.c", src)])
unsaved_funcs = [c.spelling for c, _ in walk(u.cursor)
                 if c.kind == cx.CursorKind.FUNCTION_DECL]
check("unsaved file parsed", "answer" in unsaved_funcs, str(unsaved_funcs))

print("=" * 60)
passed = sum(1 for _, c, _ in ok if c)
print(f"RESULT: {passed}/{len(ok)} checks passed")
print(json.dumps({"passed": passed, "total": len(ok),
                  "failed": [l for l, c, _ in ok if not c]}))

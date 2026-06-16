#!/usr/bin/env python3
"""Example 06 — The high-level model layer: typed entities, not raw Syms.

Examples 01–05 use the LOW-LEVEL `GraphQuery`: everything comes back as a `Sym`
and you call graph verbs (g.callers, g.members, g.dispatch_targets …). That is
precise and token-cheap, but uniform — a function, a class, and a template all
look the same, and you carry the edge-direction conventions in your head.

The `indexer.model` layer wraps that surface in concept-bearing classes —
Function, Method, Class, Field, Enum, Namespace, FunctionTemplate, … — each with
SEMANTIC properties instead of graph verbs:

    Function/Method : .return_type, .arguments, .callers(), .callees()
    Method          : .owner, .is_pure, .is_virtual, .overrides(),
                      .overridden_by(), .dispatch_targets()
    Class           : .fields, .methods, .parents, .children, .is_abstract
    every entity    : .definition, .declaration (when distinct), .references()

The low-level API is untouched: `entity.sym` is the escape hatch back to the
`Sym`, and `cb.graph` is the underlying `GraphQuery`.

Run:
    cd project && python examples/06_model_layer.py
"""

from __future__ import annotations

from indexer import open_codebase, Function, Method, Record


def main() -> None:
    # open_codebase() mirrors open_query(): same standard read-only index.
    with open_codebase() as cb:

        # --------------------------------------------------------------- #
        # 1. LOOK UP — find() returns TYPED entities, not bare Syms.
        # --------------------------------------------------------------- #
        print("== a function ==")
        for fn in cb.find("multiply"):
            if not isinstance(fn, Function) or isinstance(fn, Method):
                continue
            # .return_type / .arguments are parsed from the signature.
            args = ", ".join(a.spelling for a in fn.arguments)
            print(f"  {fn.name}({args}) -> {fn.return_type}")
            print(f"    defined : {fn.definition.loc if fn.definition else '?'}")
            if fn.declaration:                       # only shown when distinct
                print(f"    declared: {fn.declaration.loc}")
            print(f"    callers : {[c.name for c in fn.callers()][:5]}")
            break

        # --------------------------------------------------------------- #
        # 2. A RECORD — fields, methods, inheritance, abstractness.
        # --------------------------------------------------------------- #
        print("\n== a class/struct ==")
        rec = next((e for e in cb.find("", limit=2000)
                    if isinstance(e, Record) and e.fields), None)
        if rec:
            print(f"  {rec.kind} {rec.name}  (abstract={rec.is_abstract})")
            for f in rec.fields[:5]:
                t = f.type.spelling if f.type else "?"
                print(f"    field  {f.name:<28} : {t}")
            for m in rec.methods[:5]:
                print(f"    method {m.name}  (pure={m.is_pure})")
            if rec.parents:
                print(f"    parents : {[p.name for p in rec.parents]}")
            if rec.children:
                print(f"    children: {[c.name for c in rec.children][:5]}")

        # --------------------------------------------------------------- #
        # 3. DYNAMIC DISPATCH — a virtual method's real run-time targets.
        # --------------------------------------------------------------- #
        print("\n== virtual methods ==")
        vm = next((e for e in cb.find("", limit=4000)
                   if isinstance(e, Method) and e.is_virtual), None)
        if vm:
            print(f"  {vm.name}  owner={vm.owner.name if vm.owner else '?'}")
            print(f"    dispatch targets: "
                  f"{[t.name for t in vm.dispatch_targets()]}")
        else:
            print("  (no virtual methods in this index)")

        # --------------------------------------------------------------- #
        # 4. ESCAPE HATCH — drop back to the low level any time.
        # --------------------------------------------------------------- #
        # entity.sym is the raw Sym; cb.graph is the GraphQuery. Nothing in the
        # low-level API changed — this layer only adds on top of it.


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Example 04 — Class hierarchy & virtual dispatch (C++).

These queries are specific to C++ object models. They use the structural edges
(`inherits`, `method_of`, `field_of`, `overrides`) rather than `calls`.

    bases(cls) ............. base classes (inheritance, up)
    subclasses(cls) ........ derived classes (inheritance, down)
    members(cls) ........... methods + fields declared in the class
    overrides(method) ...... base methods this one overrides (up)
    overridden_by(method) .. derived methods that override this one (down)
    is_virtual_method(m) ... is m virtual / pure-virtual?
    dispatch_targets(m) .... EVERY concrete method a virtual call to m can hit
    virtual_callees(fn) .... resolve fn's virtual call sites to their targets

`dispatch_targets` is the important one for static analysis: a virtual call
`base->draw()` could land on `Base::draw` OR any override in a subclass. This
expands one method into its full run-time dispatch set — the thing a plain
call-graph can't tell you.

NOTE: a pure C index has no classes, so this script prints "no class hierarchy"
on C-only indexes. Point it at a C++ index (e.g. the geometry sample) to see
output.

Run:
    cd project && python examples/04_hierarchy_dispatch.py
"""

from __future__ import annotations

from indexer import open_query, GraphQuery, Sym


def main() -> None:
    with open_query() as g:
        # Find a class that has at least one base or subclass, so there's a
        # hierarchy to show. (Plain `find(kind='class')` also works.)
        cls = _first_class_in_hierarchy(g)
        if cls is None:
            print("No class hierarchy in this index (C-only? point at a C++ "
                  "index).")
            return
        print(f"focus class: {cls.name}  ({cls.loc})\n")

        # ------------------------------------------------------------------- #
        # bases / subclasses — walk the inheritance tree
        # ------------------------------------------------------------------- #
        # direct=True = immediate parents/children only; direct=False = the full
        # transitive ancestor/descendant set.
        print("== bases (direct) ==")
        for b in g.bases(cls, direct=True):
            print(f"   ^ {b.name:<24} {b.loc}")
        print("== subclasses (transitive) ==")
        for d in g.subclasses(cls, direct=False):
            print(f"   v {d.name:<24} {d.loc}")

        # ------------------------------------------------------------------- #
        # members — methods + fields declared in the class
        # ------------------------------------------------------------------- #
        # Each member Sym carries .access (public/protected/private) and .kind
        # (method / member / constructor / destructor / …).
        print("\n== members ==")
        for m in g.members(cls):
            acc = m.access or "?"
            print(f"   {acc:<10} {m.kind:<12} {m.name}")

        # ------------------------------------------------------------------- #
        # virtual dispatch — expand a method into its run-time target set
        # ------------------------------------------------------------------- #
        # Find a virtual method on this class (or its hierarchy) and show the
        # full picture: what it overrides, what overrides it, and the complete
        # set of concrete methods a virtual call could reach.
        method = _first_virtual_method(g, cls)
        if method is None:
            print("\n(no virtual method found on this class hierarchy)")
            return
        print(f"\n== virtual method: {method.name}  "
              f"(pure={method.is_pure}) ==")

        # is_virtual_method: handles the 'pure virtual / override' detection.
        print(f"   is_virtual_method = {g.is_virtual_method(method)}")

        # overrides: base declarations THIS method overrides (look up the tree).
        print("   overrides (up):")
        for s in g.overrides(method):
            print(f"      ^ {s.name:<24} {s.loc}")

        # overridden_by: derived methods that override THIS one (down the tree).
        print("   overridden_by (down):")
        for s in g.overridden_by(method):
            print(f"      v {s.name:<24} {s.loc}")

        # dispatch_targets: THE answer — every concrete method a virtual call to
        # `method` could execute at run time (itself, unless pure, plus every
        # transitive override). This is what you feed into a sound call graph.
        print("   dispatch_targets (run-time reachable):")
        for s in g.dispatch_targets(method):
            print(f"      * {s.name:<24} {s.loc}")

        # ------------------------------------------------------------------- #
        # virtual_callees — resolve a FUNCTION's virtual call sites
        # ------------------------------------------------------------------- #
        # Given a function that makes virtual calls, this returns the union of
        # dispatch targets across all of its virtual call sites — i.e. "every
        # concrete method this function might end up invoking through vtables".
        # (Shown here for completeness; needs a function with virtual calls.)
        # for s in g.virtual_callees(some_function):
        #     print(s.name, s.loc)


def _first_class_in_hierarchy(g: GraphQuery) -> Sym | None:
    for c in g.find("", kind="class", limit=200):
        if g.bases(c, direct=True) or g.subclasses(c, direct=True):
            return c
    return None


def _first_virtual_method(g: GraphQuery, cls: Sym) -> Sym | None:
    # Look across the class and its subclasses for a virtual method.
    candidates = [cls, *g.subclasses(cls, direct=False)]
    for c in candidates:
        for m in g.members(c):
            if m.kind == "method" and g.is_virtual_method(m):
                return m
    return None


if __name__ == "__main__":
    main()

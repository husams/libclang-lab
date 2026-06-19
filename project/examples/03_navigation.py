#!/usr/bin/env python3
"""Example 03 — Navigation: hops, bounded walks, and reachability.

Where example 02 looked one step around a symbol, these traverse the graph:

    neighbors(sym) ......... one hop (the immediate peers)
    walk(start, kinds) ..... bounded breadth-first traversal (a sub-graph)
    reaches(src, dst) ...... shortest path src→dst, or None (reachability)
    path_to(ident) ......... the containment path (namespace/class → symbol)

These answer questions like "what's the blast radius of changing X", "can this
entrypoint ever reach that sink", and "what is X nested inside".

Run:
    cd project && python examples/03_navigation.py
"""

from __future__ import annotations

from indexer import open_query, GraphQuery, Sym


def main() -> None:
    with open_query() as g:
        if g.edge_count() == 0:
            print("This index has no edges — see README to regenerate, then re-run.")
            return

        fn = _first_function_with_calls(g)
        if fn is None:
            print("No function with call edges found.")
            return
        print(f"focus symbol: {fn.name}  ({fn.loc})\n")

        # ------------------------------------------------------------------- #
        # neighbors — one hop in a chosen direction
        # ------------------------------------------------------------------- #
        # direction='out' = edges leaving the symbol; 'in' = edges arriving.
        # kinds=None = any kind; or restrict, e.g. kinds=('calls','uses').
        # Returns the peer symbols (capped at limit).
        print("== neighbors out (calls) ==")
        for s in g.neighbors(fn, kinds=("calls",), direction="out", limit=8):
            print(f"   -> {s.name:<28} {s.loc}")

        # with_kind=True annotates each neighbour with the RELATIONSHIP type it
        # was reached by -- returns (Sym, edge_kind) tuples. Pass kinds=None to
        # see the full mix (calls / uses / contains / field_of / ...).
        print("\n== neighbors out, ALL kinds, with relation type ==")
        for s, kind in g.neighbors(
            fn, kinds=None, direction="out", limit=12, with_kind=True
        ):
            print(f"   --{kind}--> {s.name:<26} {s.loc}")

        # ------------------------------------------------------------------- #
        # walk — bounded BFS, returns a Traversal (a small sub-graph)
        # ------------------------------------------------------------------- #
        # Follow `kinds` edges in one `direction`, up to `depth` hops and
        # `max_nodes` total. The Traversal records, for every reached symbol,
        # the MINIMUM depth at which it was found. This is your "transitive
        # callees within 3 hops" / "dependency cone" query.
        print("\n== walk out over 'calls', depth<=3 ==")
        tr = g.walk(fn, kinds=("calls",), direction="out", depth=3, max_nodes=50)
        # tr.nodes is sorted shallowest-first; tr.depth_by_id gives the depth.
        for s in tr.nodes[:15]:
            depth = tr.depth_by_id.get(s.id, 0)
            print(f"   depth {depth}  {s.name:<26} {s.loc}")
        print(f"   ... {len(tr.nodes)} symbols reached total")

        # ------------------------------------------------------------------- #
        # reaches — shortest path between two symbols (reachability)
        # ------------------------------------------------------------------- #
        # "Can src reach dst by following `kinds` edges?" Returns the shortest
        # path as a list of Syms [src, …, dst], or None if unreachable. Great
        # for "does this public entrypoint ever call that dangerous sink".
        # Here we prove the property by reaching one of fn's own transitive
        # callees, then printing the chain.
        targets = [s for s in tr.nodes if s.id != fn.id]
        if targets:
            dst = targets[-1]  # a deepest reached node
            path = g.reaches(fn, dst, kinds=("calls",), max_depth=8)
            print(f"\n== reaches  {fn.spelling} -> {dst.spelling} ==")
            if path:
                print("   " + " -> ".join(s.spelling for s in path))
            else:
                print("   (no path within max_depth)")

            # --------------------------------------------------------------- #
            # Traversal.path_to — reconstruct the DISCOVERY path from the walk
            # --------------------------------------------------------------- #
            # walk() records, for each reached node, the parent it was first
            # reached from. tr.path_to(node) replays that chain back to the
            # walk's start WITHOUT re-querying the DB. (It's a method on the
            # Traversal returned by walk(), not on GraphQuery, and needs a
            # Traversal that recorded parents — which walk() always does.)
            print(f"\n== Traversal.path_to  {fn.spelling} -> {dst.spelling} ==")
            chain = tr.path_to(dst)
            print(
                "   " + " -> ".join(s.spelling for s in chain)
                if chain
                else "   (not reached in this walk)"
            )


def _first_function_with_calls(g: GraphQuery) -> Sym | None:
    # find('') surfaces empty-spelling stubs first, so skip stubs and prefer
    # main() before a broad scan (see 02_references.py for the why).
    for cand in g.find("main", kind="function", limit=10):
        if not cand.is_stub and g.edges_out(cand, kinds=("calls",), limit=1):
            return cand
    for s in g.find("", kind="function", limit=3000):
        if not s.is_stub and g.edges_out(s, kinds=("calls",), limit=1):
            return s
    return None


if __name__ == "__main__":
    main()

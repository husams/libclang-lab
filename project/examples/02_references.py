#!/usr/bin/env python3
"""Example 02 — References: who calls what, and exactly where.

This is the heart of code inspection: given a symbol, find the edges touching
it. The high-level helpers answer the common questions directly; the low-level
`edges_in/out` give you every typed relationship when you need control.

    callers(fn) ........ functions that CALL fn          (incoming `calls`)
    callees(fn) ........ functions fn CALLS              (outgoing `calls`)
    references(sym) .... everywhere sym is used or called (incoming calls+uses)
    edges_in/out ....... raw typed edges, any of the 9 kinds
    sites(edge) ........ the concrete file:line occurrences of one edge

KEY MENTAL MODEL: edges are *collapsed*. If `main` calls `helper` five times
there is ONE `calls` edge with `count == 5`; the five call sites live in
`sites(edge)`. So `callers`/`callees` answer "who/what" cheaply, and you only
pay for `sites()` when you need the precise locations.

Run:
    cd project && python examples/02_references.py
"""

from __future__ import annotations

from indexer import open_query, GraphQuery, Sym


def main() -> None:
    with open_query() as g:
        if g.edge_count() == 0:
            print("This index has no edges — see README to regenerate, then re-run.")
            return

        # Pick any function that actually participates in the call graph, so
        # the example prints something. In your own scripts you'd target a
        # specific symbol via find()/by_name()/get().
        fn = _first_function_with_calls(g)
        if fn is None:
            print("No function with call edges found in this index.")
            return
        print(f"focus symbol: {fn.name}  ({fn.loc})\n")

        # ------------------------------------------------------------------- #
        # callers — who calls this function?  (incoming `calls` edges)
        # ------------------------------------------------------------------- #
        # Returns the PEER symbols (the callers themselves), nearest first,
        # capped at `limit`. This is "find usages / who depends on me".
        print("== callers (who calls it) ==")
        for s in g.callers(fn, limit=10):
            print(f"   <- {s.name:<28} {s.loc}")

        # ------------------------------------------------------------------- #
        # callees — what does this function call?  (outgoing `calls` edges)
        # ------------------------------------------------------------------- #
        # "What does this function depend on" — including stub targets such as
        # libc functions that were never indexed (marked [stub]).
        print("\n== callees (what it calls) ==")
        for s in g.callees(fn, limit=10):
            tag = "  [stub]" if s.is_stub else ""
            print(f"   -> {s.name:<28} {s.loc}{tag}")

        # ------------------------------------------------------------------- #
        # references — every place the symbol is used OR called
        # ------------------------------------------------------------------- #
        # references() == incoming edges of kind ('calls', 'uses'). Unlike
        # callers() it returns Edge objects (so you keep the edge kind + count),
        # which is what you want for an accurate "find all references".
        print("\n== references (calls + uses, incoming) ==")
        for e in g.references(fn, limit=10):
            # e.peer is the using/calling symbol; e.kind is 'calls' or 'uses';
            # e.count is how many times it occurs.
            print(f"   {e.kind:<6} x{e.count:<3} from {e.peer.name:<24} {e.peer.loc}")

        # ------------------------------------------------------------------- #
        # sites — drill into ONE edge for exact source locations
        # ------------------------------------------------------------------- #
        # Take the first outgoing call edge and list its concrete call sites.
        # Each Site has file/line/col, a `.loc` helper, whether it sits inside a
        # conditional (#if / template that may not compile), and the call's
        # argument signature when known.
        out_edges = g.edges_out(fn, kinds=("calls",), limit=1)
        if out_edges:
            e = out_edges[0]
            print(
                f"\n== sites of  {fn.spelling} -> {e.peer.spelling}  "
                f"(count={e.count}) =="
            )
            for site in g.sites(e, limit=10):
                cond = "  (conditional)" if site.conditional else ""
                print(f"   {site.loc}{cond}")

        # ------------------------------------------------------------------- #
        # edges_in / edges_out — the general primitive
        # ------------------------------------------------------------------- #
        # When the named helpers aren't enough, ask for ANY edge kind(s).
        # Pass kinds=None to get ALL kinds. Here we show every outgoing edge
        # kind on the focus symbol, grouped, to reveal its full footprint:
        #   calls / uses / contains / field_of / method_of / inherits / …
        print("\n== all outgoing edge kinds (footprint) ==")
        by_kind: dict[str, int] = {}
        for e in g.edges_out(fn, kinds=None, limit=1000):
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        for kind, n in sorted(by_kind.items()):
            print(f"   {kind:<12} {n}")


def _first_function_with_calls(g: GraphQuery) -> Sym | None:
    """A real (non-stub) function that has outgoing call edges.

    Note: find('') returns empty-spelling STUBS first (they sort shortest-first
    and are call targets that were never indexed), so we skip stubs and prefer
    main() as a reliable seed before falling back to a broad scan.
    """
    for cand in g.find("main", kind="function", limit=10):
        if not cand.is_stub and g.edges_out(cand, kinds=("calls",), limit=1):
            return cand
    for s in g.find("", kind="function", limit=3000):
        if not s.is_stub and g.edges_out(s, kinds=("calls",), limit=1):
            return s
    return None


if __name__ == "__main__":
    main()

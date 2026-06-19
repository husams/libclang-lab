#!/usr/bin/env python3
"""Example 05 — JSON export: feed the graph into other tools / languages.

Every result type (`Sym`, `Edge`, `Site`) has a `.to_dict()` that produces a
STABLE, documented JSON shape — the exact same shape the `cidx graph … --json`
CLI emits, and the one the C++ port matches byte-for-byte. So you can:

    * build a custom report,
    * emit JSON for a non-Python consumer (jq, a web UI, an LLM tool call),
    * snapshot a sub-graph for diffing across index builds.

This script assembles a small JSON document describing one function and its
neighbourhood, then prints it. Pipe it into `jq` to slice further:

    python examples/05_json_export.py | jq '.callees[].qual_name'

Run:
    cd project && python examples/05_json_export.py
"""

from __future__ import annotations

import json

from indexer import open_query, GraphQuery, Sym


def main() -> None:
    with open_query() as g:
        fn = _pick_symbol(g)
        if fn is None:
            print(json.dumps({"error": "no symbol found"}))
            return

        # Sym.to_dict() -> {id, usr, spelling, qual_name, kind, type_info,
        #                   file, line, col, is_definition, ...}
        doc = {
            "symbol": fn.to_dict(),
            "stats": {"symbols": g.stats().get("symbols"), "edges": g.edge_count()},
        }

        # Only add graph sections when the index actually has edges.
        if g.edge_count() > 0:
            # callers/callees return Syms -> list of dicts.
            doc["callers"] = [s.to_dict() for s in g.callers(fn, limit=25)]
            doc["callees"] = [s.to_dict() for s in g.callees(fn, limit=25)]

            # Edge.to_dict(sites=...) merges the peer symbol's fields with the
            # edge `kind`/`count` and, when you pass them, the concrete `sites`.
            # This is the richest single-object view: who/what + how-many +
            # where. We do it for the first few outgoing call edges.
            refs = []
            for e in g.edges_out(fn, kinds=("calls",), limit=10):
                sites = g.sites(e, limit=20)
                refs.append(e.to_dict(sites=sites))  # includes sites[] in JSON
            doc["outgoing_calls"] = refs

        # indent=2 for human reading; drop it (or use separators) for compact
        # machine output. ensure_ascii=False keeps non-ASCII identifiers intact.
        print(json.dumps(doc, indent=2, ensure_ascii=False))


def _pick_symbol(g: GraphQuery) -> Sym | None:
    """Prefer a real (non-stub) function with call edges; fall back to any
    non-stub function (find('') lists empty-spelling stubs first, so skip them).
    """
    has_edges = g.edge_count() > 0
    for cand in g.find("main", kind="function", limit=10):
        if not cand.is_stub and (
            not has_edges or g.edges_out(cand, kinds=("calls",), limit=1)
        ):
            return cand
    for s in g.find("", kind="function", limit=3000):
        if s.is_stub:
            continue
        if not has_edges or g.edges_out(s, kinds=("calls",), limit=1):
            return s
    return None


if __name__ == "__main__":
    main()

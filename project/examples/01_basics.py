#!/usr/bin/env python3
"""Example 01 — Basics: open the index, read stats, look symbols up.

This is the entry point for every graph-query script. It shows the three things
you do at the start of any inspection:

    1. OPEN the index (read-only) ....................... open_query()
    2. SIZE it up ...................................... g.stats() / g.edge_count()
    3. LOOK a symbol up ............ g.find() / g.by_name() / g.get()

A `Sym` is one declaration/definition. Every Sym carries enough location data
(`file`, `line`, `.loc`) to ground a claim in real source — so a script (or an
LLM) never has to guess where something lives.

Run:
    cd project && python examples/01_basics.py
    # or:  uv run --project project python project/examples/01_basics.py
"""

from __future__ import annotations

# `open_query` and the value types are exported from the package root, so a
# script only ever imports from `indexer` — never from internal modules.
from indexer import open_query, Sym


def main() -> None:
    # ----------------------------------------------------------------------- #
    # 1. OPEN
    # ----------------------------------------------------------------------- #
    # open_query() with no argument opens the STANDARD index
    # ($INDEXER_CACHE/index.db, else ~/.cache/cidx/index.db) — the exact same
    # path the `cidx` CLI uses, so the library and CLI always agree.
    #
    #   * It opens the SQLite file in read-only mode (mode=ro): a query script
    #     can NEVER mutate your index.
    #   * Pass open_query("/path/to/other/index.db") to inspect a non-standard
    #     index.
    #   * Pass require_edges=True to fail fast (NoEdgesError) if the graph has
    #     no edges — useful when your script only makes sense on a real graph.
    #
    # Use it as a context manager so the DB handle is always closed.
    with open_query() as g:
        # ------------------------------------------------------------------- #
        # 2. SIZE IT UP
        # ------------------------------------------------------------------- #
        # stats() returns a dict: total symbols, total edges, a per-kind symbol
        # breakdown, per-edge-kind counts, component/file counts, etc. It is the
        # cheapest way to sanity-check that the index has what you expect.
        st = g.stats()
        print("== index stats ==")
        print(f"  symbols : {st.get('symbols')}")
        print(f"  edges   : {st.get('edges')}")

        # edge_count() is a fast standalone count. If it is ~0 the graph was
        # built with --no-graph (or never resolved): the navigation examples
        # (02–04) will come back empty until you regenerate edges. We warn now
        # so the rest of the output isn't mysteriously blank.
        if g.edge_count() == 0:
            print("\n  !! this index has NO edges — callers/callees/etc. will be")
            print(
                "     empty. Regenerate: cidx set pending=True; cidx index; "
                "cidx resolve\n"
            )

        # ------------------------------------------------------------------- #
        # 3. LOOK A SYMBOL UP — three ways
        # ------------------------------------------------------------------- #
        # (a) find(pattern, kind=, limit=) — FUZZY search by qualified name.
        #     '::'-separated segments must appear IN ORDER, so 'conf::set'
        #     matches 'RdKafka::ConfImpl::set'. Shortest (closest) names first.
        #     This is what you reach for when you only half-remember a name.
        print("== find('main', kind='function') ==")
        hits = g.find("main", kind="function", limit=5)
        for s in hits:
            _print_sym(s)

        # (b) by_name(spelling, kind=) — EXACT unqualified-spelling match.
        #     Returns every symbol whose own name is exactly `spelling`
        #     (a header decl and its .c definition are two rows with the same
        #     spelling, so you often get more than one).
        print("\n== by_name('main') ==")
        for s in g.by_name("main"):
            _print_sym(s)

        # (c) get(ident) — fetch ONE symbol by integer id, by USR string, or
        #     pass a Sym straight through (handy when you accept "either" in a
        #     helper). Returns None if nothing matches.
        if hits:
            same = g.get(hits[0].usr)  # look the first hit back up by USR
            print(f"\n== get({hits[0].usr!r}) ==")
            if same:
                _print_sym(same)

        # ------------------------------------------------------------------- #
        # The Sym fields you'll use most (see indexer/query.py:Sym for all):
        #   .id          integer primary key (stable within one index)
        #   .usr         clang Unified Symbol Resolution — the cross-TU identity
        #   .name        qualified name (else spelling)
        #   .spelling    unqualified name
        #   .kind        function | class | method | variable | … (17 kinds)
        #   .type_info   the type/signature string, e.g. "int (void)"
        #   .file/.line  best-known source location (.loc = "base.c:line")
        #   .is_stub     True = referenced but never indexed (libc, etc.)
        #   .resolved    False for stub/unresolved targets
        # ------------------------------------------------------------------- #


def _print_sym(s: Sym) -> None:
    """Compact one-line dump of a symbol with its grounding location."""
    stub = "  [stub]" if s.is_stub else ""
    print(f"  #{s.id:<6} {s.kind:<10} {s.name:<24} {s.loc}{stub}")


if __name__ == "__main__":
    main()

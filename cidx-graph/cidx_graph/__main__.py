"""Tiny CLI for quick checks: python -m cidx_graph <cmd> ...

    python -m cidx_graph stats
    python -m cidx_graph find <pattern> [kind]
    python -m cidx_graph callers <name>
    python -m cidx_graph callees <name>
    python -m cidx_graph dispatch <method-name>

Set CIDX_GRAPH_DB or $INDEXER_CACHE to point at a non-default index.
This is only for spot checks -- real reasoning is done by importing the module.
"""
import os
import sys

from .graph import open_graph


def _g():
    return open_graph(os.environ.get("CIDX_GRAPH_DB"))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    g = _g()
    if cmd == "stats":
        for k, v in g.stats().items():
            print(f"{k}: {v}")
    elif cmd == "find":
        kind = rest[1] if len(rest) > 1 else None
        for s in g.find(rest[0], kind=kind):
            print(s)
    elif cmd in ("callers", "callees", "dispatch"):
        hits = g.find(rest[0])
        if not hits:
            print(f"no symbol matches {rest[0]!r}")
            return 1
        s = hits[0]
        print(f"# {cmd} of {s!r}")
        fn = {"callers": g.callers, "callees": g.callees,
              "dispatch": g.dispatch_targets}[cmd]
        for t in fn(s):
            print(t)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""cxq CLI entry point — python -m cxq "<query>" [--db path]"""

from __future__ import annotations

import argparse
import json
import sys
import os

# Allow running from the libclang-lab directory directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "project"))


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="python -m cxq",
        description="CXQ V1 — declarative code-graph query language",
    )
    ap.add_argument("query", nargs="?", help="Query string (V1 syntax)")
    ap.add_argument(
        "--db",
        default=None,
        help="Path to cidx index.db (default: ~/.cache/cidx/index.db)",
    )
    ap.add_argument(
        "--limit", type=int, default=500, help="Max entities to scan per kind"
    )
    ap.add_argument(
        "--count", action="store_true", help="Print only the count of results"
    )
    ap.add_argument(
        "--pretty", action="store_true", default=True, help="Pretty-print JSON output"
    )
    ap.add_argument("--compact", action="store_true", help="Compact JSON output")
    args = ap.parse_args()

    if args.query is None:
        ap.print_help()
        sys.exit(0)

    from cxq.parser import parse, ParseError
    from cxq.executor import execute, ExecutorError
    from indexer.model import open_codebase

    try:
        query = parse(args.query)
    except ParseError as e:
        print(f"Parse error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        cb = open_codebase(args.db)
    except Exception as e:
        print(f"Failed to open codebase: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        with cb:
            rows = execute(query, cb, limit=args.limit)
    except ExecutorError as e:
        print(f"Execution error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.count:
        print(len(rows))
        return

    indent = None if args.compact else 2
    print(json.dumps(rows, indent=indent, default=str))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""CXQ V2 shared bake-off demo — queries A through F.

V2 = V1 core (match/where/select + closure) PLUS path and rank operators.

Run from the libclang-lab directory:
    python3 cxq/demo.py
"""

from __future__ import annotations

import json
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "project"))
sys.path.insert(0, os.path.join(_HERE, ".."))

from cxq.parser import parse  # noqa: E402
from cxq.executor import execute  # noqa: E402
from indexer.model import open_codebase  # noqa: E402


def banner(label: str, query_text: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Query: {query_text}")
    print()


def run(label: str, query_text: str, cb, *, dedup_key: str | None = None) -> list[dict]:
    banner(label, query_text)
    q = parse(query_text)
    rows = execute(q, cb)

    if dedup_key:
        seen: set = set()
        deduped = []
        for r in rows:
            k = _nested_get(r, dedup_key)
            if k not in seen:
                seen.add(k)
                deduped.append(r)
        rows = deduped

    print(json.dumps(rows, indent=2, default=str))
    print(f"\n  -> {len(rows)} result(s)")
    return rows


def _nested_get(d: dict, dotpath: str) -> str:
    parts = dotpath.split(".")
    val = d
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return str(val)
    return str(val)


def main() -> None:
    print("CXQ V2 Bake-off Demo")
    print("Index: ~/.cache/cidx/index.db")

    cb = open_codebase()
    with cb:
        stats = cb.stats()
        print(f"Symbols: {stats['symbols']}, Edges: {stats['edges']}")

        # ------------------------------------------------------------------ #
        # A. Attribute match: find functions by name predicate
        # ------------------------------------------------------------------ #
        run(
            "A. Attribute match — functions whose name contains 'rank'",
            'match function f where f.name ~ "rank" select f',
            cb,
        )

        # ------------------------------------------------------------------ #
        # B. Relation/join: classes inheriting a base
        # ------------------------------------------------------------------ #
        run(
            "B. Relation — classes inheriting geo::Shape (transitive)",
            'match class c where c inherits+ "geo::Shape" select c',
            cb,
        )

        # ------------------------------------------------------------------ #
        # C. Closure: everything reachable from main via calls+
        # ------------------------------------------------------------------ #
        run(
            "C. Closure — all functions reachable from main via calls+",
            'match function f where "main" calls+ f select f',
            cb,
        )

        # ------------------------------------------------------------------ #
        # D. Hierarchy closure: all descendants of geo::Shape
        # ------------------------------------------------------------------ #
        run(
            "D. Hierarchy closure — all subclasses of geo::Shape",
            'match class c where c inherits+ "geo::Shape" select c',
            cb,
        )

        # ------------------------------------------------------------------ #
        # E. PATH query (V2) — ordered call route from A to B
        # ------------------------------------------------------------------ #
        run(
            "E. Path (V2) — call ROUTE from main to app::normalize",
            'show path from "main" to "app::normalize" via calls',
            cb,
        )

        # Second path demo: shorter route
        run(
            "E2. Path (V2) — call ROUTE from main to org::project::net::connect",
            'show path from "main" to "org::project::net::connect" via calls',
            cb,
        )

        # ------------------------------------------------------------------ #
        # F. RANK query (V2) — top-N by transitive-caller count
        # ------------------------------------------------------------------ #
        run(
            "F. Rank (V2) — top 10 functions by blast radius (callers+)",
            "rank f in match function f select f by count(callers+ f) desc limit 10",
            cb,
        )

    print("\nDemo complete.")


if __name__ == "__main__":
    main()

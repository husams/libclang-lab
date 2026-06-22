"""indexer.relink -- rewrite PUBLISHED-library include paths to the CLONED repo.

A compile_commands.json captured from a feature build often mixes two kinds of
`-I` paths for the same library:

  * the *published* (released, versioned) copy in a shared replication tree, e.g.
        -I/remote/tmp/weekly/shared-bms-replication-dir/dcs/cml/coredata/flight/18-91-0-15/src/bom/generate/cat
  * the *cloned* working copy registered as a cidx component, e.g.
        component  dcs::cml::coredata::flight   /workspace/DCS/.../AOPS-52378/ngfcr

When a component is cloned locally we want its includes to resolve to the CLONE,
not the published release. The link between the two is the component NAME: a
name `a::b::c` is the published path fragment `a/b/c`. So a published include is
recognised by finding the component's name-fragment as path segments, optionally
followed by a version segment, anywhere inside the `-I` value:

    .../dcs/cml/coredata/flight / 18-91-0-15 / src/bom/generate/cat
        └────────── fragment ──────┘ └ version ┘ └──── remainder ────┘

and the matched `<prefix>/fragment/<version>` is replaced with the cloned repo,
so the value becomes the full cloned path by DEFAULT, e.g.
`/workspace/DCS/.../ngfcr/src/bom/generate/cat` -- or, with --alias, the portable
`<dcs::cml::coredata::flight>/src/bom/generate/cat` token instead.

This is a Python-only maintenance utility (not part of the cidx CLI surface);
run it with:

    python3 -m indexer.relink                 # DRY-RUN against the default index
    python3 -m indexer.relink --apply         # write the changes
    python3 -m indexer.relink --index PATH --component dcs::cml::coredata::flight --apply
"""

from __future__ import annotations

import argparse
import os
import sys

from indexer import pathx
from indexer.storage import Storage

_INCLUDE_FLAGS = ("-I", "-isystem", "-iquote")


def default_index_path() -> str:
    """Same default index location cidx uses (INDEXER_CACHE or ~/.cache/cidx)."""
    base = os.environ.get("INDEXER_CACHE") or "~/.cache/cidx"
    return os.path.join(os.path.expanduser(base), "index.db")


def build_fragment_map(components) -> list[tuple[list[str], str, str]]:
    """Return (fragment_segments, component_name, cloned_path) for each component
    whose name maps to a published path fragment (name `a::b::c` -> `a/b/c`),
    sorted MOST-SPECIFIC first (longest fragment) so nested components win the
    match deterministically."""
    out: list[tuple[list[str], str, str]] = []
    for c in components:
        frag_segs = [s for s in c.name.replace("::", "/").split("/") if s]
        if not frag_segs:
            continue
        out.append((frag_segs, c.name, c.path))
    out.sort(key=lambda t: (-len(t[0]), -len("/".join(t[0])), t[1]))
    return out


def relink_value(
    value: str,
    frag_map: list[tuple[list[str], str, str]],
    *,
    alias: bool = False,
    require_version: bool = True,
) -> str:
    """Rewrite one include VALUE from its published form to the cloned component.

    Returns the value unchanged when it is already indirected (`<...>`/`$...`),
    relative, or matches no component fragment. When `require_version` is set,
    the fragment must be immediately followed by a version segment (the hallmark
    of a published *release*) -- this avoids rewriting a coincidental match.
    """
    if "<" in value or "$" in value or not os.path.isabs(value):
        return value
    segs = [s for s in os.path.normpath(value).split("/") if s]
    for frag_segs, name, cloned in frag_map:  # longest fragment first
        n = len(frag_segs)
        for i in range(len(segs) - n + 1):
            if segs[i : i + n] != frag_segs:
                continue
            rest = segs[i + n :]
            has_version = bool(rest) and pathx.is_version_segment(rest[0])
            if require_version and not has_version:
                continue  # not a published-versioned path at this position
            if has_version:
                rest = rest[1:]
            remainder = ("/" + "/".join(rest)) if rest else ""
            if alias:
                return "<" + name + ">" + remainder
            return cloned.rstrip("/") + remainder
    return value


def relink_options(
    options: list[str],
    frag_map: list[tuple[list[str], str, str]],
    *,
    alias: bool = False,
    require_version: bool = True,
) -> list[str]:
    """Apply relink_value to every -I/-isystem/-iquote value (space + glued
    forms); all other tokens pass through verbatim."""

    def _fn(val: str) -> str:
        return relink_value(
            val, frag_map, alias=alias, require_version=require_version
        )

    out: list[str] = []
    it = iter(options)
    for tok in it:
        matched = False
        for flag in _INCLUDE_FLAGS:
            if tok == flag:  # space form: -I path
                out += [flag, _fn(next(it, ""))]
                matched = True
                break
            if tok.startswith(flag) and len(tok) > len(flag):  # glued: -Ipath
                out.append(flag + _fn(tok[len(flag) :]))
                matched = True
                break
        if not matched:
            out.append(tok)
    return out


def run(args) -> int:
    with Storage(args.index) as db:
        cid = None
        if args.component:
            comp = db.get_component_by_name(args.component)
            if comp is None:
                print(f"error: no component named {args.component!r}", file=sys.stderr)
                return 1
            cid = comp.id
        frag_map = build_fragment_map(db.list_components())
        if not frag_map:
            print("error: no components registered in the index", file=sys.stderr)
            return 1

        changed_files = 0
        changed_paths = 0
        for rec, path in db.list_files(component_id=cid):
            if not rec.compile_options or rec.id is None:
                continue
            cur = list(rec.compile_options)
            new = relink_options(
                cur,
                frag_map,
                alias=args.alias,
                require_version=not args.no_require_version,
            )
            if new == cur:
                continue
            diffs = [(a, b) for a, b in zip(cur, new) if a != b]
            changed_files += 1
            changed_paths += len(diffs)
            if args.apply:
                db.update_file_compile_options(rec.id, new)
            if not args.apply or args.verbose:
                print(path)
                for a, b in diffs:
                    print(f"   - {a}")
                    print(f"   + {b}")

        mode = (
            "applied"
            if args.apply
            else "DRY-RUN (nothing written; re-run with --apply)"
        )
        print(
            f"\n{mode}: {changed_paths} include path(s) relinked "
            f"across {changed_files} file(s)"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m indexer.relink",
        description="Relink published-library include paths to their cloned cidx "
        "component (matched via the component name -> path fragment).",
    )
    ap.add_argument(
        "--index",
        default=default_index_path(),
        help="path to index.db (default: $INDEXER_CACHE/index.db or "
        "~/.cache/cidx/index.db)",
    )
    ap.add_argument(
        "--component",
        help="only relink files of this component (default: all files)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write the changes (default: dry-run, only print the diff)",
    )
    ap.add_argument(
        "--alias",
        action="store_true",
        help="rewrite to the portable <component> token instead of the full "
        "cloned absolute path (default: full cloned path)",
    )
    ap.add_argument(
        "--no-require-version",
        action="store_true",
        help="also relink a fragment that is NOT followed by a version segment "
        "(more aggressive; off by default)",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true", help="print the diff even with --apply"
    )
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

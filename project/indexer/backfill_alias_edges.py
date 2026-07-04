"""indexer.backfill_alias_edges -- add the missing alias -> template-instance
``uses`` edge WITHOUT re-indexing the whole codebase.

Background
----------
A template-instance type alias (``using IntBox = Box<int>;``) links to its
``Box<int>`` instance node through a ``uses`` edge. Indexes built before the
extraction-ordering fix (v0.44.0) emitted that edge BEFORE the instance was
minted, so it was never written -- the alias ended up with no outgoing ``uses``
edge at all. The one fact needed to rebuild the link -- WHICH instance the alias
names (``Box<int>``) -- lives only in the AST, never in a DB column, so it cannot
be reconstructed from existing rows. But it also does NOT need a full reindex:
only the files that DECLARE such an alias have to be re-parsed.

What it does
------------
1. Find every typedef / type-alias symbol that has NO outgoing ``uses`` edge --
   the template-instance aliases that need the backfill (plus builtin aliases
   like ``typedef int Integer;``, which are harmless no-ops).
2. Group them by their defining file so each file is parsed exactly ONCE.
3. Parse each file (reusing the on-disk AST cache when it is warm) and, for the
   alias cursors only, mint the instance + emit the ``uses`` edge -- the same
   operation the extractor now performs, applied selectively.

Record / struct / enum / typedef-of-typedef aliases already carry their ``uses``
edge, so they are never candidates and no existing edge is touched (``add_edge``
would otherwise bump the edge's ``count``).

This is a Python-only maintenance utility (like :mod:`indexer.relink`); it is
not part of the cidx CLI surface. Run it with::

    python3 -m indexer.backfill_alias_edges                 # DRY-RUN, default index
    python3 -m indexer.backfill_alias_edges --apply         # write the edges
    python3 -m indexer.backfill_alias_edges --index PATH --component NAME --apply
"""

from __future__ import annotations

import argparse
import os
import sys

import clang.cindex as cx

from indexer import astcache, compiledb
from indexer.astcmd import Target
from indexer.clang import ast as A
from indexer.query import EDGE_KINDS
from indexer.storage import SYMBOL_KIND_IDS, Storage

_TYPEDEF = SYMBOL_KIND_IDS["typedef"]
_TYPEALIAS = SYMBOL_KIND_IDS["type-alias"]
_USES = EDGE_KINDS["uses"]
_ALIAS_CURSOR_KINDS = (cx.CursorKind.TYPEDEF_DECL, cx.CursorKind.TYPE_ALIAS_DECL)


def default_index_path() -> str:
    """Same default index location cidx uses (INDEXER_CACHE or ~/.cache/cidx)."""
    base = os.environ.get("INDEXER_CACHE") or "~/.cache/cidx"
    return os.path.join(os.path.expanduser(base), "index.db")


def find_candidate_aliases(
    db: Storage, component: str | None = None
) -> dict[int, set[str]]:
    """Return ``{file_id: {alias_usr, ...}}`` for every type-alias / typedef with
    NO outgoing ``uses`` edge -- the aliases that may be missing their
    instance link. Optionally scoped to one ``component`` by name."""
    sql = (
        "SELECT s.file_id AS file_id, s.usr AS usr "
        "FROM symbol s "
        "WHERE s.kind IN (?, ?) AND s.file_id IS NOT NULL AND s.usr <> '' "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM edge e WHERE e.src_id = s.id AND e.kind = ?)"
    )
    params: list = [_TYPEDEF, _TYPEALIAS, _USES]
    if component is not None:
        # Restrict to files under the named component (file -> directory ->
        # component). Component ids are small; resolve the name to an id first.
        row = db._conn.execute(
            "SELECT id FROM component WHERE name = ?", (component,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"error: no component named {component!r}")
        sql += (
            " AND s.file_id IN ("
            "   SELECT f.id FROM file f JOIN directory d ON d.id = f.directory_id "
            "   WHERE d.component_id = ?)"
        )
        params.append(row["id"])

    out: dict[int, set[str]] = {}
    for r in db._conn.execute(sql, params):
        out.setdefault(r["file_id"], set()).add(r["usr"])
    return out


def _parse_file(db: Storage, file_id: int, use_cache: bool = True):
    """Parse the file behind ``file_id`` with its stored compile flags, reusing
    the AST cache when warm. Returns ``(tu, abspath)`` or ``(None, abspath)``."""
    rec = db.get_file_by_id(file_id)
    if rec is None:
        return None, None
    abspath = rec.abspath
    flags = compiledb.resolve_options(
        compiledb.sanitize(rec.compile_options or []), db.get_alias
    )
    target = Target(abspath=abspath, flags=list(flags), driver=rec.driver)
    tu = astcache.load_or_parse(target, use_cache=use_cache)
    return tu, abspath


def _backfill_one_file(
    db: Storage, file_id: int, want_usrs: set[str], apply: bool, use_cache: bool
) -> list[tuple[str, str]]:
    """Parse one file and, for its candidate alias cursors, emit the missing
    ``uses`` edge to the instance. Returns ``[(alias_usr, instance_display), ...]``
    for the edges that were (or would be) added."""
    tu, abspath = _parse_file(db, file_id, use_cache=use_cache)
    if tu is None:
        print(f"  warning: could not parse file id {file_id}", file=sys.stderr)
        return []

    added: list[tuple[str, str]] = []
    for cursor in tu.cursor.walk_preorder():
        if cursor.kind not in _ALIAS_CURSOR_KINDS:
            continue
        usr = cursor.get_usr()
        if usr not in want_usrs:
            continue  # not a candidate (already linked, or a different alias)
        sym = db.lookup_symbol(usr)
        if sym is None:
            continue
        # Only act when the underlying type is a template specialization -- that
        # is the case the old extraction dropped. Mint the instance (idempotent;
        # keyed on the spec USR) then look for the uses target it produces.
        before = _uses_target(db, sym.id)
        if before is not None:
            continue  # already has an edge (raced with another pass); skip
        if apply:
            with db.transaction():
                A._mint_named_instance(db, cursor)
                A._emit_type_use(
                    db,
                    sym.id,
                    cursor.underlying_typedef_type,
                    file_id,
                    cursor.location,
                )
            tgt = _uses_target(db, sym.id)
        else:
            # Dry run: resolve what the edge WOULD point at without writing.
            tgt = _dry_run_target(db, cursor)
        if tgt is not None:
            added.append((usr, tgt))
    return added


def _uses_target(db: Storage, alias_id: int) -> str | None:
    """The display name of the single ``uses`` out-neighbour of ``alias_id``,
    or ``None`` when there is none."""
    row = db._conn.execute(
        "SELECT s.display_name AS dn, s.qual_name AS qn "
        "FROM edge e JOIN symbol s ON s.id = e.dst_id "
        "WHERE e.src_id = ? AND e.kind = ? LIMIT 1",
        (alias_id, _USES),
    ).fetchone()
    if row is None:
        return None
    return row["dn"] or row["qn"]


def _dry_run_target(db: Storage, cursor) -> str | None:
    """Resolve, WITHOUT writing, the instance a template-instance alias would
    link to -- returns its display name, or ``None`` when the underlying type is
    not an indexed template specialization (e.g. ``typedef int``)."""
    decl = A._named_type_decl(cursor.underlying_typedef_type)
    if decl is None:
        return None
    usr = decl.get_usr()
    if not usr:
        return None
    sym = db.lookup_symbol(usr)
    if sym is None:
        return None
    return sym.display_name or sym.qual_name


def run(args) -> int:
    index = args.index or default_index_path()
    if not os.path.exists(index):
        print(f"error: no index at {index}", file=sys.stderr)
        return 1

    with Storage(index) as db:
        candidates = find_candidate_aliases(db, component=args.component)
        if not candidates:
            print("no type-alias symbols missing a uses edge -- nothing to do.")
            return 0

        n_files = len(candidates)
        n_aliases = sum(len(v) for v in candidates.values())
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(
            f"[{mode}] {n_aliases} candidate alias(es) across {n_files} file(s) "
            f"(parsing each file once)"
        )

        total = 0
        for file_id, want in sorted(candidates.items()):
            added = _backfill_one_file(
                db, file_id, want, apply=args.apply, use_cache=not args.no_cache
            )
            for alias_usr, tgt in added:
                arrow = "->" if args.apply else "would ->"
                print(f"  {alias_usr}  {arrow}  {tgt}")
            total += len(added)

        if args.apply:
            db._conn.commit()
            print(f"\nbackfilled {total} alias -> instance uses edge(s).")
            print("run `cidx resolve` to roll the new edges into the graph.")
        else:
            print(f"\n{total} edge(s) would be added. Re-run with --apply to write.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m indexer.backfill_alias_edges",
        description=(
            "Backfill the missing type-alias -> template-instance `uses` edge by "
            "re-parsing ONLY the files that declare such an alias (no full reindex)."
        ),
    )
    ap.add_argument("--index", help="index.db path (default: cidx's default index)")
    ap.add_argument(
        "--component", help="restrict to files under this component (by name)"
    )
    ap.add_argument(
        "--apply", action="store_true", help="write the edges (default: dry-run)"
    )
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="always reparse from source (ignore the on-disk AST cache)",
    )
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

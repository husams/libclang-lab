"""cidx_graph.live -- libclang escape hatch.

USE THE GRAPH FIRST. This module is the LAST resort, for the few questions the
static graph cannot answer:

  * template instantiations the indexer doesn't materialize (a known cidx limit)
  * exact resolved types / canonical types at a specific cursor
  * macro-expanded code, or a file edited since the last `cidx index`
  * anything needing the live AST rather than the stored summary

It parses ONE file on demand with libclang and hands back cursors. It is slower
than the graph by orders of magnitude and loads only that translation unit, so
scope every call tightly.

It reuses the cidx indexer's own parser when that package is importable (so the
builtin-header / custom-toolchain handling is identical to indexing). Set the
repo root via CIDX_REPO or pass `indexer_path=`. Honors CIDX_LIBCLANG for an
external libclang (e.g. /opt/llvm-21.1.1/lib/libclang.so).

    from cidx_graph import live
    tu = live.parse_file("/abs/path/foo.cpp")          # uses stored compile args if a DB is given
    for cur in live.walk(tu.cursor):
        if cur.kind == cur.kind.CALL_EXPR: ...
"""

from __future__ import annotations

import os
import sys
from typing import Iterator, Optional, Sequence


def _ensure_indexer_on_path(indexer_path: Optional[str]) -> None:
    if indexer_path:
        sys.path.insert(0, indexer_path)
        return
    repo = os.environ.get("CIDX_REPO")
    candidates = []
    if repo:
        candidates.append(os.path.join(repo, "libclang-lab", "project"))
        candidates.append(os.path.join(repo, "project"))
    candidates.append(os.path.expanduser(
        "~/workspace/qemu-vms/libclang-lab/project"))
    for c in candidates:
        if os.path.isdir(os.path.join(c, "indexer")):
            sys.path.insert(0, c)
            return


def parse_file(path: str, args: Optional[Sequence[str]] = None,
               db_path: Optional[str] = None, indexer_path: Optional[str] = None):
    """Parse one file and return a clang.cindex.TranslationUnit.

    Compile-arg resolution order:
      1. explicit `args`
      2. the file's stored compile_options from the cidx DB (if `db_path`/default
         DB has them) -- the most faithful
      3. the cidx default toolchain args (sysroot + builtin headers)
    Raises if libclang/the indexer cannot be loaded or the parse is fatal.
    """
    _ensure_indexer_on_path(indexer_path)
    try:
        from indexer.clang import util as cutil  # type: ignore
    except Exception as e:                                    # pragma: no cover
        raise RuntimeError(
            "cidx indexer not importable for live parsing. Set CIDX_REPO or pass "
            f"indexer_path=. Underlying error: {e}")

    if args is None:
        args = _stored_args(path, db_path)
    if args is None:
        args = cutil.clang_args() if hasattr(cutil, "clang_args") else []
    return cutil.parse(path, list(args))


def _stored_args(path: str, db_path: Optional[str]) -> Optional[list[str]]:
    """Pull the file's stored compile_options from the index, if present."""
    try:
        from .graph import default_db_path
        import sqlite3
        dbp = db_path or default_db_path()
        if not os.path.exists(dbp):
            return None
        conn = sqlite3.connect(f"file:{os.path.abspath(dbp)}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        ap = os.path.abspath(path)
        for r in conn.execute(
            "SELECT f.compile_options AS opts, c.path AS root, d.path AS rel, "
            "f.name AS name FROM file f JOIN directory d ON d.id=f.directory_id "
            "JOIN component c ON c.id=d.component_id WHERE f.compile_options IS NOT NULL"
        ):
            full = (os.path.join(r["root"], r["rel"], r["name"])
                    if r["rel"] else os.path.join(r["root"], r["name"]))
            if os.path.abspath(full) == ap:
                import json
                conn.close()
                return json.loads(r["opts"])
        conn.close()
    except Exception:
        return None
    return None


def walk(cursor) -> Iterator:
    """Depth-first iterator over a cursor's whole subtree."""
    yield cursor
    for child in cursor.get_children():
        yield from walk(child)


def overridden(cursor) -> list:
    """Base-class cursors `cursor` overrides (live equivalent of
    Graph.overrides). Useful to confirm/extend graph dispatch results."""
    try:
        return list(cursor.get_overriden_cursors())  # clang spells it this way
    except AttributeError:
        return list(getattr(cursor, "get_overridden_cursors", lambda: [])())

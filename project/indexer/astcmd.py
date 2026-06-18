"""indexer.astcmd -- on-demand AST analysis commands (``cidx ast ...``).

This is a *read-side* layer, like ``query``/``model``: it resolves a target to a
parsed translation unit, walks the AST, and emits. It reads ONLY the symbol and
file tables of the index (which file a symbol lives in, and that file's compile
flags) -- it never touches the edge/call graph.

Three target forms (see :func:`resolve_target`):

* **symbol**   ``--usr/--id/--name`` -> resolved against the ``symbol`` table.
* **file**     ``COMPONENT://path`` or a bare indexed path -> flags from the index.
* **ad-hoc**   ``path -- <flags>`` -> parsed with the given flags, no index needed.

Commands: ``dump`` (AST subtree), ``locals`` (a function's local variables),
``conditions`` (conditionals guarding a call + their condition).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

import clang.cindex as cx

from .clang.ast import _COND_KINDS, _FUNCTION_KINDS, _file_cursors
from .storage import Storage, Symbol


# -- target resolution --------------------------------------------------------


@dataclass
class Target:
    """A resolved AST target: a file to parse + how to find the focus cursor."""

    abspath: str
    flags: list[str]
    driver: str | None = None
    focus_usr: str | None = None  # resolved symbol's USR (indexed targets)
    focus_name: str | None = None  # spelling to match post-parse (ad-hoc only)
    symbol: Symbol | None = None

    @property
    def whole_file(self) -> bool:
        return self.focus_usr is None and self.focus_name is None


def _has_selector(args) -> bool:
    return any(getattr(args, a, None) is not None for a in ("usr", "id", "name"))


def _resolve_symbol(db: Storage, args) -> tuple[Symbol | None, int]:
    """Resolve --usr/--id/--name against the symbol table (no graph)."""
    if args.usr is not None:
        s = db.lookup_symbol(args.usr)
        if s is None:
            print(f"error: no symbol with USR {args.usr!r}", file=sys.stderr)
            return None, 1
        return s, 0
    if args.id is not None:
        s = db.lookup_symbol_by_id(int(args.id))
        if s is None:
            print(f"error: no symbol with id {args.id}", file=sys.stderr)
            return None, 1
        return s, 0
    hits = db.search_symbols(args.name, kind=args.kind)
    if not hits:
        print(
            f"error: no symbol matches --name {args.name!r}"
            + (f" (kind {args.kind})" if args.kind else ""),
            file=sys.stderr,
        )
        return None, 1
    if len(hits) > 1 and not args.first:
        print(
            f"error: --name {args.name!r} matches {len(hits)} symbols; "
            f"disambiguate with --usr/--id (or pass --first):",
            file=sys.stderr,
        )
        for s in hits[:25]:
            loc = f"{s.qual_name or s.spelling}"
            print(f"  #{s.id}  {s.kind:<14} {loc}  [{s.usr}]", file=sys.stderr)
        if len(hits) > 25:
            print(f"  ... and {len(hits) - 25} more", file=sys.stderr)
        return None, 2
    return hits[0], 0


def resolve_target(args) -> tuple[Target | None, int]:
    """Resolve CLI args to a :class:`Target`, or ``(None, exit_code)`` on error.

    A **file target wins when present**: with a ``FILE``/``COMPONENT://PATH``
    target, ``--name`` means "find this spelling *in that file*" and ``--usr``
    "find this USR in that file" -- NOT an index-wide search. Only when no target
    is given does the selector run against the symbol table. The index is opened
    only when needed; a pure ad-hoc ``path -- <flags>`` never requires an index.
    """
    adhoc_flags = list(getattr(args, "rest", None) or [])
    if adhoc_flags and adhoc_flags[0] == "--":
        adhoc_flags = adhoc_flags[1:]
    target = getattr(args, "target", None)
    focus_usr = getattr(args, "usr", None)
    focus_name = getattr(args, "name", None)

    # ---- file target present: file wins; --name/--usr scope the focus -------
    if target:
        if "://" in target:  # COMPONENT://PATH
            with Storage(args.index) as db:
                comp_name, _, rel = target.partition("://")
                comp = db.get_component_by_name(comp_name)
                if comp is None:
                    print(f"error: no component named {comp_name!r}", file=sys.stderr)
                    return None, 1
                abs_path = os.path.normpath(os.path.join(comp.path, rel.lstrip("/")))
                rec = db.get_file(abs_path)
                if rec is None:
                    print(f"error: not in index database: {abs_path}", file=sys.stderr)
                    return None, 1
                return Target(
                    abspath=abs_path,
                    flags=list(rec.compile_options or []),
                    driver=rec.driver,
                    focus_usr=focus_usr,
                    focus_name=focus_name,
                ), 0

        abs_path = os.path.abspath(os.path.expanduser(target))
        if adhoc_flags:  # explicit ad-hoc flags
            return Target(
                abspath=abs_path,
                flags=adhoc_flags,
                focus_usr=focus_usr,
                focus_name=focus_name,
            ), 0
        with Storage(args.index) as db:  # indexed file -> its flags
            rec = db.get_file(abs_path)
            if rec is not None:
                return Target(
                    abspath=abs_path,
                    flags=list(rec.compile_options or []),
                    driver=rec.driver,
                    focus_usr=focus_usr,
                    focus_name=focus_name,
                ), 0
        if os.path.exists(abs_path):
            print(
                f"warning: {abs_path} is not in the index and no flags were "
                f"given (pass '-- <flags>'); parsing with defaults",
                file=sys.stderr,
            )
            return Target(
                abspath=abs_path, flags=[], focus_usr=focus_usr, focus_name=focus_name
            ), 0
        print(f"error: no such file and not in index: {target}", file=sys.stderr)
        return None, 1

    # ---- no target: selector runs against the symbol table -----------------
    if not _has_selector(args):
        print(
            "error: need a symbol selector (--usr/--id/--name) or a "
            "FILE/COMPONENT://PATH target",
            file=sys.stderr,
        )
        return None, 2
    with Storage(args.index) as db:
        sym, rc = _resolve_symbol(db, args)
        if sym is None:
            return None, rc
        file_id = sym.file_id if sym.file_id is not None else sym.decl_file_id
        if file_id is None:
            print(
                f"error: symbol {sym.spelling!r} has no indexed file "
                f"(declaration-only/external)",
                file=sys.stderr,
            )
            return None, 1
        rec = db.get_file_by_id(file_id)
        path = db.file_abs_path(file_id)
        if rec is None or path is None:
            print(
                "error: cannot resolve the symbol's file in the index", file=sys.stderr
            )
            return None, 1
        return Target(
            abspath=path,
            flags=list(rec.compile_options or []),
            driver=rec.driver,
            focus_usr=sym.usr,
            symbol=sym,
        ), 0


def _parse_target(t: Target, use_cache: bool = True) -> cx.TranslationUnit | None:
    """Parse *t*, using the on-disk AST cache unless *use_cache* is False."""
    from . import (
        astcache,
    )  # lazy import to keep import graph acyclic (cli->astcmd->astcache->clang)

    return astcache.load_or_parse(t, use_cache=use_cache)


# -- AST walking --------------------------------------------------------------


def _loc(c: cx.Cursor) -> str:
    f = c.location.file
    if f is None:
        return "<no-location>"
    return f"{os.path.basename(f.name)}:{c.location.line}:{c.location.column}"


def _find_focus(tu: cx.TranslationUnit, t: Target) -> cx.Cursor | None:
    """Locate the focus cursor (function/class/...) in the parsed TU."""
    for c in _file_cursors(tu, t.abspath):
        if t.focus_usr is not None and c.get_usr() == t.focus_usr:
            return c
        if t.focus_name is not None and c.spelling == t.focus_name:
            return c
    return None


def _subtree(cursor: cx.Cursor):
    """Pre-order walk descending fully into the cursor's subtree (incl. bodies).

    Yields ``(cursor, depth, parent)`` with depth relative to ``cursor``.
    """

    def rec(c: cx.Cursor, depth: int):
        for ch in c.get_children():
            yield ch, depth, c
            yield from rec(ch, depth + 1)

    yield from rec(cursor, 0)


# -- JSON / text emission -----------------------------------------------------


def _extent_dict(c: cx.Cursor) -> dict:
    ext = c.extent
    sf = ext.start.file
    return {
        "file": os.path.basename(sf.name) if sf else None,
        "start": [ext.start.line, ext.start.column],
        "end": [ext.end.line, ext.end.column],
    }


def _cursor_json(
    c: cx.Cursor, depth: int, max_depth: int | None, want_tokens: bool, want_types: bool
) -> dict:
    d: dict = {
        "kind": c.kind.name,
        "spelling": c.spelling or None,
        "usr": c.get_usr() or None,
        "extent": _extent_dict(c),
    }
    if want_types:
        d["type"] = c.type.spelling or None if c.type is not None else None
    if want_tokens:
        d["tokens"] = [tok.spelling for tok in c.get_tokens()]
    if max_depth is None or depth < max_depth:
        kids = [
            _cursor_json(ch, depth + 1, max_depth, want_tokens, want_types)
            for ch in c.get_children()
        ]
        if kids:
            d["children"] = kids
    return d


def _dump_text(
    c: cx.Cursor, depth: int, max_depth: int | None, want_tokens: bool, want_types: bool
) -> None:
    indent = "  " * depth
    name = c.spelling or "<anon>"
    typ = ""
    if want_types and c.type is not None and c.type.spelling:
        typ = f" : {c.type.spelling}"
    print(f"{indent}{c.kind.name:<26} {name}{typ}  @ {_loc(c)}")
    if want_tokens:
        toks = " ".join(tok.spelling for tok in c.get_tokens())
        if toks:
            print(f"{indent}  ` {toks}")
    if max_depth is None or depth < max_depth:
        for ch in c.get_children():
            _dump_text(ch, depth + 1, max_depth, want_tokens, want_types)


# -- commands -----------------------------------------------------------------


def cmd_dump(args) -> int:
    t, rc = resolve_target(args)
    if t is None:
        return rc
    tu = _parse_target(t, use_cache=getattr(args, "cache", True))
    if tu is None:
        return 1
    max_depth = args.depth if args.depth and args.depth > 0 else None

    if t.whole_file:
        # top-level cursors of the main file (not the nested decls _file_cursors
        # would also yield), each dumped as a subtree.
        roots = [
            c
            for c in tu.cursor.get_children()
            if c.location.file is not None and c.location.file.name == t.abspath
        ]
    else:
        focus = _find_focus(tu, t)
        if focus is None:
            sel = t.focus_usr or t.focus_name
            print(
                f"error: could not locate {sel!r} in {os.path.basename(t.abspath)}",
                file=sys.stderr,
            )
            return 1
        roots = [focus]

    if args.json:
        out = [_cursor_json(c, 0, max_depth, args.tokens, args.types) for c in roots]
        print(json.dumps(out, indent=2))
    else:
        for c in roots:
            _dump_text(c, 0, max_depth, args.tokens, args.types)
    return 0


def _focus_function(tu: cx.TranslationUnit, t: Target) -> cx.Cursor | None:
    if t.whole_file:
        print(
            "error: this command needs a function "
            "(use --name/--usr/--id, or 'COMPONENT://path --name fn')",
            file=sys.stderr,
        )
        return None
    focus = _find_focus(tu, t)
    if focus is None:
        sel = t.focus_usr or t.focus_name
        print(
            f"error: could not locate {sel!r} in {os.path.basename(t.abspath)}",
            file=sys.stderr,
        )
        return None
    if focus.kind not in _FUNCTION_KINDS:
        print(
            f"error: {focus.spelling!r} is a {focus.kind.name}, not a function",
            file=sys.stderr,
        )
        return None
    return focus


def cmd_locals(args) -> int:
    t, rc = resolve_target(args)
    if t is None:
        return rc
    tu = _parse_target(t, use_cache=getattr(args, "cache", True))
    if tu is None:
        return 1
    focus = _focus_function(tu, t)
    if focus is None:
        return 1

    wanted = {cx.CursorKind.VAR_DECL}
    if args.params:
        wanted.add(cx.CursorKind.PARM_DECL)
    rows = []
    for c, _depth, _parent in _subtree(focus):
        if c.kind in wanted:
            rows.append(
                {
                    "name": c.spelling,
                    "type": c.type.spelling if c.type is not None else None,
                    "kind": "param" if c.kind == cx.CursorKind.PARM_DECL else "local",
                    "loc": _loc(c),
                }
            )
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{focus.spelling}: {len(rows)} variable(s)")
        for r in rows:
            tag = "param" if r["kind"] == "param" else "local"
            print(f"  {tag:<6} {r['type'] or '?':<24} {r['name']}  @ {r['loc']}")
    return 0


def _condition_child(stmt: cx.Cursor) -> cx.Cursor | None:
    """Best-effort controlling expression of a conditional statement.

    libclang lists statement children positionally but omits null slots (e.g. a
    ``for(;;)`` with no condition), so picking by index is fragile. We take the
    first expression-kind child, which is the controlling condition for
    if/while/switch/do/?: and the condition expression of a for-loop in the
    common case. Documented limitation: an unusual for-header can mis-pick.
    """
    for ch in stmt.get_children():
        if ch.kind.is_expression():
            return ch
    return None


def cmd_conditions(args) -> int:
    t, rc = resolve_target(args)
    if t is None:
        return rc
    tu = _parse_target(t, use_cache=getattr(args, "cache", True))
    if tu is None:
        return 1
    focus = _focus_function(tu, t)
    if focus is None:
        return 1

    # parent map over the focus subtree, then climb each call to its guard.
    parent_of: dict[int, cx.Cursor] = {}
    calls: list[cx.Cursor] = []
    for c, _depth, parent in _subtree(focus):
        parent_of[c.hash] = parent
        if c.kind == cx.CursorKind.CALL_EXPR and c.spelling:
            calls.append(c)

    seen: set[int] = set()
    rows = []
    for call in calls:
        node = parent_of.get(call.hash)
        guard = None
        while node is not None and node.hash != focus.hash:
            if node.kind in _COND_KINDS:
                guard = node
                break
            node = parent_of.get(node.hash)
        if guard is None or guard.hash in seen:
            continue
        seen.add(guard.hash)
        cond = _condition_child(guard)
        cond_toks = " ".join(tok.spelling for tok in cond.get_tokens()) if cond else ""
        guarded = sorted(
            {c.spelling for c in calls if _guarded_by(c, guard, parent_of, focus)}
        )
        row = {
            "control": guard.kind.name,
            "loc": _loc(guard),
            "condition": cond_toks,
            "calls": guarded,
        }
        if args.ast and cond is not None:
            row["condition_ast"] = _cursor_json(cond, 0, None, False, True)
        rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{focus.spelling}: {len(rows)} conditional(s) guarding calls")
        for r in rows:
            print(f"  {r['control']:<20} @ {r['loc']}")
            print(f"    cond: {r['condition']}")
            print(f"    -> calls: {', '.join(r['calls'])}")
    return 0


def _guarded_by(
    call: cx.Cursor, guard: cx.Cursor, parent_of: dict[int, cx.Cursor], focus: cx.Cursor
) -> bool:
    node = parent_of.get(call.hash)
    while node is not None and node.hash != focus.hash:
        if node.hash == guard.hash:
            return True
        node = parent_of.get(node.hash)
    return False


def cmd_cache(args) -> int:
    """Dispatch ``cidx ast cache build|status|clear`` to the astcache helpers."""
    from . import astcache  # lazy import (same pattern as _parse_target)

    action = args.cache_action
    if action == "status":
        return astcache.cmd_status(args)
    if action == "clear":
        return astcache.cmd_clear(args)
    if action == "build":
        return astcache.cmd_build(args)
    print(f"error: unknown cache action {action!r}", file=sys.stderr)
    return 2

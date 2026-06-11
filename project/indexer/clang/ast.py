"""indexer.clang.ast -- walk a parsed translation unit and store its symbols.

index_symbols() stores only cursors from the TU's MAIN FILE; cursors pulled
in through #include are skipped. index_headers() then covers the headers:
every file the TU includes (tu.get_includes() is transitive, so nested
includes are reached too) is indexed as its own file row, skipping headers
that are already indexed and -- by default -- system headers
($INDEXER_IGNORE_SYSTEM_HEADERS=false to index those as well).
"""

import os
from collections.abc import Iterator, Sequence
from typing import Any

import clang.cindex as cx

from ..storage import Storage, Symbol
from ..utils import md5_of
from .util import parse

#: Set to "false" / "0" / "no" / "off" to index system headers too.
IGNORE_SYSTEM_HEADERS_ENV: str = "INDEXER_IGNORE_SYSTEM_HEADERS"

#: CursorKind -> storage symbol kind. Cursors of any other kind are ignored.
_KIND_MAP: dict[cx.CursorKind, str] = {
    cx.CursorKind.CLASS_DECL: "class",
    cx.CursorKind.STRUCT_DECL: "struct",
    cx.CursorKind.UNION_DECL: "union",
    cx.CursorKind.FUNCTION_DECL: "function",
    cx.CursorKind.CXX_METHOD: "method",
    cx.CursorKind.FIELD_DECL: "member",
    cx.CursorKind.CONSTRUCTOR: "constructor",
    cx.CursorKind.DESTRUCTOR: "destructor",
    cx.CursorKind.ENUM_DECL: "enum",
    cx.CursorKind.ENUM_CONSTANT_DECL: "enum-constant",
    cx.CursorKind.TYPEDEF_DECL: "typedef",
    cx.CursorKind.TYPE_ALIAS_DECL: "type-alias",
    cx.CursorKind.CLASS_TEMPLATE: "class-template",
    cx.CursorKind.FUNCTION_TEMPLATE: "function-template",
    cx.CursorKind.VAR_DECL: "variable",
    cx.CursorKind.NAMESPACE: "namespace",
    cx.CursorKind.MACRO_DEFINITION: "macro",
}

_ACCESS: dict[cx.AccessSpecifier, str] = {
    cx.AccessSpecifier.PUBLIC: "public",
    cx.AccessSpecifier.PROTECTED: "protected",
    cx.AccessSpecifier.PRIVATE: "private",
}

#: Function-like cursors: indexed themselves, but their bodies are NOT walked
#: (locals, body-scoped types, and statements are not file-scope symbols).
_FUNCTION_KINDS: frozenset[cx.CursorKind] = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.CXX_METHOD,
    cx.CursorKind.CONSTRUCTOR,
    cx.CursorKind.DESTRUCTOR,
    cx.CursorKind.FUNCTION_TEMPLATE,
})


def _file_cursors(tu: cx.TranslationUnit, filename: str) -> Iterator[cx.Cursor]:
    """Pre-order walk yielding only cursors located in `filename`."""

    def walk(cursor: cx.Cursor) -> Iterator[cx.Cursor]:
        for child in cursor.get_children():
            f = child.location.file
            if f is None or f.name != filename:
                continue            # cursor from another file: skip subtree
            yield child
            if child.kind not in _FUNCTION_KINDS:
                yield from walk(child)

    yield from walk(tu.cursor)


def _linkage(cursor: cx.Cursor) -> str | None:
    name: str = cursor.linkage.name
    return None if name == "INVALID" else name.lower().replace("_", "-")


def _qualified_name(cursor: cx.Cursor) -> str:
    """'ns::Class::name' built from SEMANTIC parents, so an out-of-line method
    definition is qualified by its class, not the file scope it sits in."""
    parts: list[str] = []
    c: cx.Cursor | None = cursor
    while c is not None and c.kind != cx.CursorKind.TRANSLATION_UNIT:
        if c.spelling:              # anonymous namespace/struct: skip the level
            parts.append(c.spelling)
        c = c.semantic_parent
    return "::".join(reversed(parts))


def _to_symbol(cursor: cx.Cursor, file_id: int) -> Symbol | None:
    """Storage Symbol for a cursor, or None if the cursor is not indexable."""
    kind = _KIND_MAP.get(cursor.kind)
    if kind is None:
        return None
    usr: str = cursor.get_usr()
    if not usr:
        return None
    parent: cx.Cursor | None = cursor.semantic_parent
    parent_usr: str | None = None
    if parent is not None and parent.kind != cx.CursorKind.TRANSLATION_UNIT:
        parent_usr = parent.get_usr() or None
    is_def: bool = cursor.is_definition()
    return Symbol(
        usr=usr,
        spelling=cursor.spelling,
        qual_name=_qualified_name(cursor) or None,
        display_name=cursor.displayname or None,
        kind=kind,
        type_info=cursor.type.spelling or None,
        file_id=file_id,
        line=cursor.location.line,
        col=cursor.location.column,
        # A declaration cursor records itself as the decl site too; the
        # upsert keeps it when the .cpp definition later takes file/line/col.
        decl_file_id=None if is_def else file_id,
        decl_line=None if is_def else cursor.location.line,
        decl_col=None if is_def else cursor.location.column,
        is_definition=is_def,
        is_pure=cursor.is_pure_virtual_method(),
        linkage=_linkage(cursor),
        access=_ACCESS.get(cursor.access_specifier),
        parent_usr=parent_usr,
        # A definition resolves the symbol; a bare declaration leaves it
        # unresolved until some TU provides the definition.
        resolved=is_def,
    )


def _index_file(db: Storage, tu: cx.TranslationUnit, filename: str,
                file_id: int) -> tuple[int, int]:
    """Store the symbols of one file from this TU; returns (stored, skipped).

    A symbol is stored only when it is NOT already in the database, or the
    stored row is unresolved (no definition seen yet). A row that is already
    resolved is left untouched and counted as skipped.
    """
    stored = skipped = 0
    with db.transaction():
        for cursor in _file_cursors(tu, filename):
            sym = _to_symbol(cursor, file_id)
            if sym is None:
                continue
            existing = db.lookup_symbol(sym.usr)
            if existing is not None and existing.resolved:
                # The definition is already stored, but this cursor may be the
                # header declaration of it -- record the decl site if missing.
                if sym.decl_file_id is not None and existing.decl_file_id is None:
                    db.update_symbol(sym.usr,
                                     decl_file_id=sym.decl_file_id,
                                     decl_line=sym.decl_line,
                                     decl_col=sym.decl_col)
                skipped += 1
                continue
            db.add_symbol(sym)
            stored += 1
    return stored, skipped


def index_symbols(db: Storage, tu: cx.TranslationUnit, file_id: int) -> tuple[int, int]:
    """Store the TU's MAIN-FILE symbols; returns (stored, skipped).

    Headers are not touched here -- run index_headers() for those.
    """
    return _index_file(db, tu, tu.spelling, file_id)


def _ignore_system_headers() -> bool:
    """$INDEXER_IGNORE_SYSTEM_HEADERS, default true (system headers ignored)."""
    val = os.environ.get(IGNORE_SYSTEM_HEADERS_ENV, "true").strip().lower()
    return val not in ("0", "false", "no", "off")


def _is_system_header(tu: cx.TranslationUnit, fobj: cx.File) -> bool:
    """True if the file is a system header (sysroot / -isystem) in this TU."""
    loc = cx.SourceLocation.from_position(tu, fobj, 1, 1)
    return bool(loc.is_in_system_header)


def index_headers(db: Storage, tu: cx.TranslationUnit,
                  ignore_system: bool | None = None) -> dict[str, int]:
    """Index every header this TU includes, skipping ones already indexed.

    tu.get_includes() lists the inclusions TRANSITIVELY (a header included by
    another header is in the list too), so one pass covers the whole tree.
    For each header not yet indexed (file row missing, never indexed, or md5
    changed) its symbols are read out of THIS TU's AST -- no separate parse --
    then the file row is marked indexed.

    Skipped and counted separately:
      system   system headers (default; $INDEXER_IGNORE_SYSTEM_HEADERS=false
               or ignore_system=False to index them)
      unowned  headers no registered component owns (nowhere to store them)
      already  headers whose row is indexed with a matching md5

    Returns {"indexed", "symbols", "already", "system", "unowned"} counts.
    """
    if ignore_system is None:
        ignore_system = _ignore_system_headers()
    counts = {"indexed": 0, "symbols": 0, "already": 0, "system": 0, "unowned": 0}
    seen: set[str] = set()
    for inc in tu.get_includes():
        path = os.path.abspath(inc.include.name)
        if path in seen:
            continue
        seen.add(path)
        if ignore_system and _is_system_header(tu, inc.include):
            counts["system"] += 1
            continue
        if db.component_for_path(path) is None:
            counts["unowned"] += 1
            continue
        md5 = md5_of(path)
        if db.is_file_indexed(path, md5=md5):
            counts["already"] += 1
            continue
        mtime = os.path.getmtime(path) if os.path.exists(path) else None
        file_id = db.add_file_path(path, mtime=mtime, md5=md5)
        stored, _ = _index_file(db, tu, inc.include.name, file_id)
        db.mark_file_indexed(file_id, mtime=mtime)
        counts["indexed"] += 1
        counts["symbols"] += stored
    return counts


def index_source(db: Storage, filename: str, args: Sequence[str], file_id: int,
                 ignore_system: bool | None = None,
                 driver: str | None = None) -> dict[str, Any]:
    """Parse one source file, index its symbols and its headers, free the TU.

    The translation unit exists only inside this call: nothing libclang-owned
    (cursors, tokens, the TU itself) escapes -- only plain-data Symbol rows are
    written to the database -- so dropping the last reference in the `finally`
    immediately runs clang_disposeTranslationUnit and returns the AST's memory.
    """
    tu = parse(filename, args, driver=driver)
    try:
        stored, skipped = index_symbols(db, tu, file_id)
        headers = index_headers(db, tu, ignore_system=ignore_system)
    finally:
        del tu                      # last reference -> native AST freed NOW
    return {"symbols": stored, "skipped": skipped, "headers": headers}

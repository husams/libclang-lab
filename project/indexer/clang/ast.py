"""indexer.clang.ast -- walk a parsed translation unit and store its symbols.

index_symbols() stores only cursors from the TU's MAIN FILE; cursors pulled
in through #include are skipped. index_headers() then covers the headers:
every file the TU includes (tu.get_includes() is transitive, so nested
includes are reached too) is indexed as its own file row, skipping headers
that are already indexed and -- by default -- system headers
($INDEXER_IGNORE_SYSTEM_HEADERS=false to index those as well).

index_edges() extracts typed relationships (calls, inherits, field_of,
method_of, overrides, specializes, template_param, template_arg) from the
SAME TU; it must be called AFTER index_symbols for the same file.
"""

import ctypes
import os
from collections.abc import Iterator, Sequence
from typing import Any, Optional

import clang.cindex as cx

from ..storage import Storage, Symbol

# ---------------------------------------------------------------------------
# Raw libclang function bindings not exposed by the Python bindings layer.
# ---------------------------------------------------------------------------
_lib = cx.conf.lib

# clang_isVirtualBase(CXCursor) -> unsigned int
_lib.clang_isVirtualBase.restype = ctypes.c_uint
_lib.clang_isVirtualBase.argtypes = [cx.Cursor]

# clang_getSpecializedCursorTemplate(CXCursor) -> CXCursor
_lib.clang_getSpecializedCursorTemplate.restype = cx.Cursor
_lib.clang_getSpecializedCursorTemplate.argtypes = [cx.Cursor]

# clang_getNumOverloadedDecls(CXCursor) -> unsigned ;
# clang_getOverloadedDecl(CXCursor, unsigned) -> CXCursor
# Used to recover the callee of a CALL_EXPR whose `.referenced` is None because
# the callee names a DEPENDENT/overloaded symbol inside a template body
# (e.g. `combine(a, b)` in Stack<T>::summary). libclang exposes the candidate
# set via the callee's OVERLOADED_DECL_REF cursor.
_lib.clang_getNumOverloadedDecls.restype = ctypes.c_uint
_lib.clang_getNumOverloadedDecls.argtypes = [cx.Cursor]
_lib.clang_getOverloadedDecl.restype = cx.Cursor
_lib.clang_getOverloadedDecl.argtypes = [cx.Cursor, ctypes.c_uint]

# clang_getOverriddenCursors + clang_disposeOverriddenCursors
_CursorArrayPtr = ctypes.POINTER(cx.Cursor)
_lib.clang_getOverriddenCursors.restype = None
_lib.clang_getOverriddenCursors.argtypes = [
    cx.Cursor,
    ctypes.POINTER(_CursorArrayPtr),
    ctypes.POINTER(ctypes.c_uint),
]
_lib.clang_disposeOverriddenCursors.restype = None
_lib.clang_disposeOverriddenCursors.argtypes = [_CursorArrayPtr]

# clang_Type_getNumTemplateArguments(CXType) -> int
_lib.clang_Type_getNumTemplateArguments.restype = ctypes.c_int
_lib.clang_Type_getNumTemplateArguments.argtypes = [cx.Type]

# clang_Type_getTemplateArgumentAsType(CXType, unsigned) -> CXType
_lib.clang_Type_getTemplateArgumentAsType.restype = cx.Type
_lib.clang_Type_getTemplateArgumentAsType.argtypes = [cx.Type, ctypes.c_uint]


def _get_overridden_cursors(cursor: cx.Cursor) -> list[cx.Cursor]:
    """Return the list of cursors that `cursor` overrides (may be empty)."""
    out_ptr = _CursorArrayPtr()
    out_num = ctypes.c_uint(0)
    _lib.clang_getOverriddenCursors(
        cursor, ctypes.byref(out_ptr), ctypes.byref(out_num)
    )
    result: list[cx.Cursor] = []
    if out_num.value > 0 and out_ptr:
        for i in range(out_num.value):
            c = out_ptr[i]
            # Cursors from this C-array API lack the binding's `_tu` backref;
            # without it `.semantic_parent` (hence _qualified_name) raises.
            c._tu = cursor._tu
            result.append(c)
        _lib.clang_disposeOverriddenCursors(out_ptr)
    return result


from ..utils import md5_of  # noqa: E402
from .util import collect_diagnostics, parse  # noqa: E402

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
_FUNCTION_KINDS: frozenset[cx.CursorKind] = frozenset(
    {
        cx.CursorKind.FUNCTION_DECL,
        cx.CursorKind.CXX_METHOD,
        cx.CursorKind.CONSTRUCTOR,
        cx.CursorKind.DESTRUCTOR,
        cx.CursorKind.FUNCTION_TEMPLATE,
    }
)


def _file_cursors(tu: cx.TranslationUnit, filename: str) -> Iterator[cx.Cursor]:
    """Pre-order walk yielding only cursors located in `filename`."""

    def walk(cursor: cx.Cursor) -> Iterator[cx.Cursor]:
        for child in cursor.get_children():
            f = child.location.file
            if f is None or f.name != filename:
                continue  # cursor from another file: skip subtree
            yield child
            if child.kind not in _FUNCTION_KINDS:
                yield from walk(child)

    yield from walk(tu.cursor)


def _file_cursors_p(
    tu: cx.TranslationUnit, filename: str
) -> Iterator[tuple[cx.Cursor, cx.Cursor]]:
    """Parent-aware variant: yields (cursor, walk_parent) for cursors in `filename`.

    `walk_parent` is the immediate parent in the walk tree — the enclosing
    CLASS_DECL for a CXX_BASE_SPECIFIER cursor (spec §1.4: semantic_parent and
    lexical_parent are both NULL on that cursor kind).
    """

    def walk(
        cursor: cx.Cursor, parent: cx.Cursor
    ) -> Iterator[tuple[cx.Cursor, cx.Cursor]]:
        for child in cursor.get_children():
            f = child.location.file
            if f is None or f.name != filename:
                continue
            yield child, cursor  # cursor is the walk-parent of child
            if child.kind not in _FUNCTION_KINDS:
                yield from walk(child, child)

    yield from walk(tu.cursor, tu.cursor)


def _linkage(cursor: cx.Cursor) -> str | None:
    name: str = cursor.linkage.name
    return None if name == "INVALID" else name.lower().replace("_", "-")


def _qualified_name(cursor: cx.Cursor) -> str:
    """'ns::Class::name' built from SEMANTIC parents, so an out-of-line method
    definition is qualified by its class, not the file scope it sits in.

    Defensive against cursors minted by C-array APIs (e.g.
    clang_getOverriddenCursors) that lack the binding's `_tu` backref, on which
    `.semantic_parent` depends -- those raise AttributeError mid-walk. We fall
    back to whatever prefix we gathered (at worst the bare spelling) rather than
    crash the whole index."""
    parts: list[str] = []
    c: cx.Cursor | None = cursor
    try:
        while c is not None and c.kind != cx.CursorKind.TRANSLATION_UNIT:
            if c.spelling:  # anonymous namespace/struct: skip the level
                parts.append(c.spelling)
            c = c.semantic_parent
    except (AttributeError, ValueError):
        pass
    return "::".join(reversed(parts))


def _is_explicit_instantiation(cursor: cx.Cursor) -> bool:
    """True when a STRUCT/CLASS_DECL cursor that has a specialized template is an
    *explicit instantiation* (``template class Foo<int>;``) rather than an
    *explicit specialization* (``template <> class Foo<bool> { ... };``).

    Both surface as a CLASS_DECL whose ``clang_getSpecializedCursorTemplate``
    returns the primary, and both report ``is_definition() == True``, so the
    stable libclang C API cannot tell them apart directly. The written syntax
    can: an explicit specialization begins ``template <>`` (empty angle
    brackets), an explicit instantiation begins ``template class`` /
    ``template struct`` (optionally after ``extern``). We inspect the leading
    tokens of the cursor's extent: the token immediately after the ``template``
    keyword is ``class``/``struct`` for an instantiation and ``<`` for a
    specialization."""
    try:
        toks = [t.spelling for t in cursor.get_tokens()]
    except Exception:  # pragma: no cover - tokenization failure is non-fatal
        return False
    for i, t in enumerate(toks):
        if t == "template":
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            return nxt in ("class", "struct")
        if t in ("class", "struct"):
            break
    return False


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
        # C++ static member function. False for free functions and non-methods;
        # a file-scope `static` free function is captured by linkage='internal'.
        is_static=cursor.is_static_method(),
        linkage=_linkage(cursor),
        access=_ACCESS.get(cursor.access_specifier),
        parent_usr=parent_usr,
        # A definition resolves the symbol; a bare declaration leaves it
        # unresolved until some TU provides the definition.
        resolved=is_def,
    )


def _index_file_notxn(
    db: Storage, tu: cx.TranslationUnit, filename: str, file_id: int
) -> tuple[int, int]:
    """M4: txn-free inner work of _index_file; caller MUST own an open transaction.

    Store the symbols of one file from this TU; returns (stored, skipped).
    A symbol is stored only when it is NOT already in the database, or the
    stored row is unresolved (no definition seen yet). A row that is already
    resolved is left untouched and counted as skipped.
    """
    stored = skipped = 0
    for cursor in _file_cursors(tu, filename):
        sym = _to_symbol(cursor, file_id)
        if sym is None:
            continue
        existing = db.lookup_symbol(sym.usr)
        if existing is not None and existing.resolved:
            # The definition is already stored, but this cursor may be the
            # header declaration of it -- record the decl site if missing.
            if sym.decl_file_id is not None and existing.decl_file_id is None:
                db.update_symbol(
                    sym.usr,
                    decl_file_id=sym.decl_file_id,
                    decl_line=sym.decl_line,
                    decl_col=sym.decl_col,
                )
            skipped += 1
            continue
        db.add_symbol(sym)
        stored += 1
    return stored, skipped


def _index_file(
    db: Storage, tu: cx.TranslationUnit, filename: str, file_id: int
) -> tuple[int, int]:
    """Store the symbols of one file from this TU; returns (stored, skipped)."""
    with db.transaction():
        return _index_file_notxn(db, tu, filename, file_id)


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


def index_headers(
    db: Storage,
    tu: cx.TranslationUnit,
    ignore_system: bool | None = None,
    header_options: Sequence[str] | None = None,
    header_driver: str | None = None,
) -> dict[str, int]:
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
    # Two passes over this TU's headers. A header may reference a symbol declared
    # in a header it includes (which appears LATER in include order) -- e.g. a
    # function template whose body calls a member function template in a deeper
    # header. That call is dependent/recovered and only LINKS to an
    # already-indexed target (no stub is minted), so the target symbol must
    # already exist. Pass 1 mints symbols for every not-yet-indexed header;
    # pass 2 then extracts edges with all header symbols present.
    pending: list[tuple[str, int, Optional[float], int]] = []
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
        # Stamp the header with the SAME (encoded) compile options + driver as
        # its including TU, so the header is standalone-reparseable (e.g.
        # `cidx ast dump <header>`) with the TU's full -I/-std/-D context
        # instead of bare defaults. The options stay in their portable
        # <label>/$VAR form (decoded at parse time), mirroring TU rows.
        file_id = db.add_file_path(
            path,
            mtime=mtime,
            md5=md5,
            compile_options=list(header_options)
            if header_options is not None
            else None,
            driver=header_driver,
        )
        # Pass 1: symbols only (each in its own transaction).
        with db.transaction():
            stored, _ = _index_file_notxn(db, tu, inc.include.name, file_id)
        pending.append((inc.include.name, file_id, mtime, stored))
    # Pass 2: edges for those headers, now that every header symbol is in the DB.
    for inc_name, file_id, mtime, stored in pending:
        with db.transaction():
            db.delete_edges_for_file(file_id)
            _index_edges_notxn(db, tu, inc_name, file_id)
        db.mark_file_indexed(file_id, mtime=mtime)
        counts["indexed"] += 1
        counts["symbols"] += stored
    return counts


#: Cursor kinds that open a conditional scope for cond_depth tracking.
_COND_KINDS: frozenset[cx.CursorKind] = frozenset(
    {
        cx.CursorKind.IF_STMT,
        cx.CursorKind.FOR_STMT,
        cx.CursorKind.WHILE_STMT,
        cx.CursorKind.DO_STMT,
        cx.CursorKind.SWITCH_STMT,
        cx.CursorKind.CASE_STMT,
        cx.CursorKind.CONDITIONAL_OPERATOR,
    }
)

#: Function-like cursor kinds that body_descent recurses INTO (definitions only).
_FUNCTION_DEF_KINDS: frozenset[cx.CursorKind] = frozenset(
    {
        cx.CursorKind.FUNCTION_DECL,
        cx.CursorKind.CXX_METHOD,
        cx.CursorKind.CONSTRUCTOR,
        cx.CursorKind.DESTRUCTOR,
        cx.CursorKind.FUNCTION_TEMPLATE,
    }
)

#: Parent kinds for which a TYPE_REF/TEMPLATE_REF child is a *declaration*
#: type-reference (signature / field / var / typedef) already captured by the
#: declaration paths -- so the body-descent TYPE_REF branch must NOT re-emit it.
_TYPEREF_PARENT_SKIP_KINDS: frozenset[cx.CursorKind] = frozenset(
    {
        cx.CursorKind.VAR_DECL,
        cx.CursorKind.PARM_DECL,
        cx.CursorKind.FIELD_DECL,
        cx.CursorKind.FUNCTION_DECL,
        cx.CursorKind.CXX_METHOD,
        cx.CursorKind.CONSTRUCTOR,
        cx.CursorKind.DESTRUCTOR,
        cx.CursorKind.FUNCTION_TEMPLATE,
        cx.CursorKind.TYPEDEF_DECL,
        cx.CursorKind.TYPE_ALIAS_DECL,
    }
)

#: Type layers stripped to reach the named (record/enum/typedef) type that a
#: signature/field/variable mentions: `const Conf *[]` -> `Conf`.
_TYPE_WRAPPERS: frozenset[cx.TypeKind] = frozenset(
    {
        cx.TypeKind.POINTER,
        cx.TypeKind.LVALUEREFERENCE,
        cx.TypeKind.RVALUEREFERENCE,
        cx.TypeKind.CONSTANTARRAY,
        cx.TypeKind.INCOMPLETEARRAY,
        cx.TypeKind.VARIABLEARRAY,
        cx.TypeKind.DEPENDENTSIZEDARRAY,
    }
)


def _named_type_decl(ctype: cx.Type) -> Optional[cx.Cursor]:
    """Strip pointer/reference/array layers off `ctype` and return the
    declaration cursor of the named type it spells, or None when the type has
    no user declaration (builtins like int, function pointers, …).

    Single-level by design: it resolves the type as WRITTEN (a typedef alias
    stays the alias), mirroring how `uses` body references name a symbol
    directly rather than chasing canonical forms.
    """
    t = ctype
    for _ in range(32):  # guard against pathological nesting
        if t.kind in _TYPE_WRAPPERS:
            t = (
                t.get_array_element_type()
                if t.kind
                not in (
                    cx.TypeKind.POINTER,
                    cx.TypeKind.LVALUEREFERENCE,
                    cx.TypeKind.RVALUEREFERENCE,
                )
                else t.get_pointee()
            )
        else:
            break
    decl = t.get_declaration()
    if decl is None:
        return None
    if decl.kind == cx.CursorKind.NO_DECL_FOUND or decl.kind.value <= 0:
        return None
    return decl


def _emit_type_use(
    db: Storage,
    src_id: int,
    ctype: cx.Type,
    file_id: int,
    loc: Any,
    conditional: int = 0,
) -> None:
    """Emit a `uses` edge (kind=7) src -> the record/enum/typedef named by
    `ctype` (parameter, return, field, variable, or typedef-underlying type).

    Lookup-only, like body descent: the type's symbol must already be indexed,
    so builtins and unindexed stdlib types create neither edges nor stubs. No
    self-edge (a factory returning its own class would otherwise loop).
    """
    decl = _named_type_decl(ctype)
    if decl is None:
        return
    usr: str = decl.get_usr()
    if not usr:
        return
    dst = db.lookup_symbol(usr)
    if dst is None or dst.id == src_id:
        return
    edge_id = db.add_edge(src_id, dst.id, 7)  # uses
    if loc is not None and loc.line:
        db.add_edge_site(
            edge_id, file_id, loc.line, loc.column, conditional=conditional
        )


def _ref_decl_loc(
    db: Storage, ref: cx.Cursor
) -> tuple[Optional[int], Optional[int], Optional[int], Optional[str]]:
    """Resolve a reference cursor's decl site to (file_id, line, col, raw_path).

    The target of a mint (callee/base/override/primary) carries a real source
    location even when its definition body is never separately indexed -- e.g.
    an implicit/defaulted ctor is anchored to its `struct` line. Recording it
    here is what lets `chain::D::D` resolve to `chain.hpp:25` instead of
    `@<no-location>`.

    Lookup-only for the registered file id (db.get_file, never add_file_path).
    A target in a file no registered component owns -- system/stdlib headers --
    has no `file` row, so `file_id` is None; but the AST still knows where it is,
    so we return the raw path as the 4th element (with line/col). The stub then
    keeps that location instead of going `@<no-location>` (e.g. a libstdc++
    `__normal_iterator::operator*` shows `stl_iterator.h:NNNN`). Only a cursor
    with no source location at all (implicit/builtin) returns all-None.
    """
    loc = ref.location
    f = loc.file
    if f is None:
        return None, None, None, None
    row = db.get_file(f.name)
    if row is None:  # unregistered (system/stdlib) header
        return None, loc.line, loc.column, f.name
    return row.id, loc.line, loc.column, None


def _peel_expr(expr: cx.Cursor) -> cx.Cursor:
    """Peel implicit casts, parentheses, and address-of/dereference from
    ``expr`` to the underlying named subexpression.  At most 16 layers."""
    # Also peel implicit cast (not a named cursor kind — check via spelling hack)
    cur = expr
    for _ in range(16):
        k = cur.kind
        if k == cx.CursorKind.PAREN_EXPR:
            children = list(cur.get_children())
            if children:
                cur = children[0]
                continue
        if k == cx.CursorKind.UNARY_OPERATOR:
            children = list(cur.get_children())
            if children:
                cur = children[0]
                continue
        if k == cx.CursorKind.CSTYLE_CAST_EXPR:
            children = list(cur.get_children())
            if children:
                cur = children[0]
                continue
        # UNEXPOSED_EXPR (value=100 in Python bindings) covers implicit casts
        # and other nodes that libclang exposes without a specific kind name.
        # Also peel UNEXPOSED_DECL (value=1) for completeness, mirroring
        # the C++ peel_expr which checks CXCursor_UnexposedExpr and
        # (CXCursorKind)1 (CXCursor_UnexposedDecl).
        if k == cx.CursorKind.UNEXPOSED_EXPR or k == cx.CursorKind.UNEXPOSED_DECL:
            children = list(cur.get_children())
            if children:
                cur = children[0]
                continue
        break
    return cur


def _type_is_value(loc_type: cx.Type, dispatch_record_usr: Optional[str]) -> bool:
    """True iff loc_type holds `dispatch_record_usr` BY VALUE (exact, non-erased).

    Sound: pointer / lvalue-ref / rvalue-ref / builtin fail the RECORD kind gate;
    a smart-pointer/handle (shared_ptr<B>, IntrusiveRefCntPtr<B>, …) is canonical
    RECORD but its decl USR is the *wrapper*, never the dispatch type, so the
    USR-equality clause rejects it with no denylist.  typedef/using is stripped
    by get_canonical()."""
    if not dispatch_record_usr:
        return False
    c = loc_type.get_canonical()  # strips typedef/using; KEEPS ref/ptr/cv
    if c.kind != cx.TypeKind.RECORD:  # POINTER / L/RVALUEREF / builtin -> not value
        return False
    decl = c.get_declaration()
    if decl is None or decl.kind.value <= 0:
        return False
    return (decl.get_usr() or None) == dispatch_record_usr


def _decl_type_for_expr(peeled: cx.Cursor) -> Optional[cx.Type]:
    """Return the DECLARED type of the value source, not the use-site expression type.

    At the call-site, libclang auto-derefs lvalue-references: a field ``B& br``
    presents as expression-type ``B``, so feeding ``peeled.type`` to
    ``_type_is_value`` would misclassify a reference as a value.

    Instead we read the declared type from the underlying decl:
      DECL_REF_EXPR / MEMBER_REF_EXPR → referenced decl's ``.type``  (B& preserved)
      CALL_EXPR (call_result)          → referenced callee's ``.result_type``
    Falls back to ``peeled.type`` for anything else (safe: only harms
    call_result without a referenced decl, which can't be value-narrowed anyway)."""
    k = peeled.kind
    if k in (cx.CursorKind.DECL_REF_EXPR, cx.CursorKind.MEMBER_REF_EXPR):
        ref = peeled.referenced
        if ref is None:
            return None
        return ref.type
    if k in (cx.CursorKind.CALL_EXPR, cx.CursorKind.CXX_FUNCTIONAL_CAST_EXPR):
        ref = peeled.referenced
        if ref is None:
            # Fallback to expression type: for a value return this IS the return
            # type; for a ref return the expression type is LVALUEREFERENCE, which
            # _type_is_value's RECORD-kind gate then correctly rejects.
            return peeled.type
        return ref.result_type
    return peeled.type


def _record_usr_of_type(t: cx.Type) -> Optional[str]:
    """Return the USR of the record declaration for type ``t``, stripping
    reference/pointer/cv-qualifiers.  Returns None for builtins."""
    canonical = t.get_canonical()
    # Strip pointer / reference
    for _ in range(8):
        tk = canonical.kind
        if tk in (
            cx.TypeKind.POINTER,
            cx.TypeKind.LVALUEREFERENCE,
            cx.TypeKind.RVALUEREFERENCE,
        ):
            canonical = canonical.get_pointee().get_canonical()
        else:
            break
    decl = canonical.get_declaration()
    if decl is None or decl.kind == cx.CursorKind.NO_DECL_FOUND:
        return None
    if decl.kind.value <= 0:
        return None
    usr = decl.get_usr()
    return usr if usr else None


def _classify_value_source(
    expr: cx.Cursor,
) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Classify the provenance of a value expression.

    Returns ``(src_kind, type_usr, decl_usr, callee_usr)`` where:
    - ``src_kind`` is one of: local, construct, member, global, call_result,
      literal, this, unknown
    - ``type_usr`` is the USR of the expression's static record type (or None)
    - ``decl_usr`` is the USR of the named local/param/field (or None)
    - ``callee_usr`` is the USR of the callee for call_result (or None)
    """
    peeled = _peel_expr(expr)
    k = peeled.kind

    # CXXThisExpr
    if k == cx.CursorKind.CXX_THIS_EXPR:
        type_usr = _record_usr_of_type(peeled.type)
        return "this", type_usr, type_usr, None

    # DECL_REF_EXPR
    if k == cx.CursorKind.DECL_REF_EXPR:
        ref = peeled.referenced
        if ref is None:
            return "unknown", None, None, None
        ref_kind = ref.kind
        decl_usr = ref.get_usr() or None
        type_usr = _record_usr_of_type(peeled.type)
        if ref_kind == cx.CursorKind.PARM_DECL:
            return "local", type_usr, decl_usr, None
        if ref_kind == cx.CursorKind.VAR_DECL:
            # Distinguish local vs global by parent kind
            parent = ref.semantic_parent
            if parent is not None and parent.kind in (
                cx.CursorKind.FUNCTION_DECL,
                cx.CursorKind.CXX_METHOD,
                cx.CursorKind.CONSTRUCTOR,
                cx.CursorKind.DESTRUCTOR,
                cx.CursorKind.LAMBDA_EXPR,
            ):
                return "local", type_usr, decl_usr, None
            return "global", type_usr, decl_usr, None
        return "unknown", type_usr, decl_usr, None

    # MEMBER_REF_EXPR -> FIELD_DECL
    if k == cx.CursorKind.MEMBER_REF_EXPR:
        ref = peeled.referenced
        decl_usr = ref.get_usr() if ref is not None else None
        type_usr = _record_usr_of_type(peeled.type)
        return "member", type_usr, decl_usr or None, None

    # Constructor call / CXXTemporaryObjectExpr / CXXNewExpr
    if k in (cx.CursorKind.CALL_EXPR, cx.CursorKind.CXX_FUNCTIONAL_CAST_EXPR):
        ref = peeled.referenced
        # Check if it is a constructor
        if ref is not None and ref.kind in (
            cx.CursorKind.CONSTRUCTOR,
            cx.CursorKind.CONVERSION_FUNCTION,
        ):
            type_usr = _record_usr_of_type(peeled.type)
            return "construct", type_usr, None, None
        # Non-ctor call => call_result
        callee_usr = ref.get_usr() if ref is not None else None
        type_usr = _record_usr_of_type(peeled.type)
        return "call_result", type_usr, None, callee_usr or None

    if k == cx.CursorKind.CXX_NEW_EXPR:
        type_usr = _record_usr_of_type(peeled.type)
        return "construct", type_usr, None, None

    # Literals and builtins
    if k in (
        cx.CursorKind.INTEGER_LITERAL,
        cx.CursorKind.FLOATING_LITERAL,
        cx.CursorKind.STRING_LITERAL,
        cx.CursorKind.CHARACTER_LITERAL,
        cx.CursorKind.CXX_BOOL_LITERAL_EXPR,
        cx.CursorKind.CXX_NULL_PTR_LITERAL_EXPR,
        cx.CursorKind.GNU_NULL_EXPR,
    ):
        return "literal", None, None, None

    return "unknown", None, None, None


def _receiver_subexpr(call: cx.Cursor) -> Optional[cx.Cursor]:
    """Return the receiver sub-expression of a C++ member call, or None.

    For a member call ``obj.method(args)`` the libclang AST has a
    MEMBER_REF_EXPR as the first child of the CALL_EXPR; the MEMBER_REF_EXPR's
    first child is the base object (``obj``).

    For free-function calls there is no MEMBER_REF_EXPR child, so returns None.
    For implicit ``this->`` calls returns a CXXThisExpr child of the
    MEMBER_REF_EXPR.
    """
    children = list(call.get_children())
    if not children:
        return None
    first = children[0]
    # Peel implicit casts wrapping the callee
    peeled_first = _peel_expr(first)
    if peeled_first.kind == cx.CursorKind.MEMBER_REF_EXPR:
        # The receiver is the MEMBER_REF_EXPR's first child
        mref_children = list(peeled_first.get_children())
        if mref_children:
            return mref_children[0]
        # Implicit this (no explicit children under MEMBER_REF_EXPR)
        return None
    return None


def _parm_position(recv_expr: cx.Cursor) -> Optional[int]:
    """Return the 0-based parameter position if ``recv_expr`` names a PARM_DECL.

    Peels implicit casts on ``recv_expr`` to reach a DECL_REF_EXPR, then
    resolves its referenced cursor.  When that cursor is a PARM_DECL, walks
    the semantic parent function's parameters to find the matching position.

    Returns None when the receiver is not a named parameter (e.g. a field, a
    global, or the receiver was a construct expression)."""
    peeled = _peel_expr(recv_expr)
    if peeled.kind != cx.CursorKind.DECL_REF_EXPR:
        return None
    ref = peeled.referenced
    if ref is None or ref.kind != cx.CursorKind.PARM_DECL:
        return None
    decl_usr = ref.get_usr()
    parent = ref.semantic_parent
    if parent is None:
        return None
    for i, param in enumerate(parent.get_arguments()):
        if param.get_usr() == decl_usr:
            return i
    return None


def _overload_set_candidates(call_cursor: cx.Cursor) -> list[cx.Cursor]:
    """The candidate declarations of a CALL_EXPR's dependent/overloaded callee.

    Inside a template body, a call to a dependent/overloaded name (e.g.
    ``combine(a, b)`` in ``Stack<T>::summary``, or an overloaded member function
    template ``cache.set(...)``) has ``CALL_EXPR.referenced is None``, even
    though the call IS present in the AST. The callee sub-expression still
    carries an ``OVERLOADED_DECL_REF`` listing the candidate declarations; this
    returns all of them (empty when there is no such node).

    Only the FIRST child (the callee position) is searched -- an argument that is
    itself an overloaded name must not be mistaken for the callee.
    """
    children = list(call_cursor.get_children())
    if not children:
        return []
    stack = [children[0]]
    while stack:
        cur = stack.pop()
        if cur.kind == cx.CursorKind.OVERLOADED_DECL_REF:
            n = _lib.clang_getNumOverloadedDecls(cur)
            return [_lib.clang_getOverloadedDecl(cur, i) for i in range(n)]
        stack.extend(cur.get_children())
    return []


def _recover_overloaded_callee(call_cursor: cx.Cursor) -> Optional[cx.Cursor]:
    """Recover the callee of a CALL_EXPR whose ``referenced`` is None, when the
    dependent overload set names exactly ONE declaration; otherwise None.

    A single-candidate set is resolved precisely (one target). Multi-candidate
    sets are handled separately by :func:`_emit_overloaded_calls`, which links
    the call site to every indexed overload of that name rather than guessing
    one (or dropping it). Unrelated ADL sets such as stdlib ``to_string`` resolve
    to no indexed symbol there, so they create neither edges nor stubs.
    """
    cands = _overload_set_candidates(call_cursor)
    return cands[0] if len(cands) == 1 else None


def _emit_overloaded_calls(
    db: Storage,
    call_cursor: cx.Cursor,
    src_id: int,
    file_id: int,
    cond_depth: int,
) -> None:
    """Emit ``calls`` edges for a dependent call whose overload set has MORE THAN
    one candidate (e.g. an overloaded member function template ``cache.set(...)``
    invoked inside another template body).

    libclang cannot say which overload a dependent call selects, so rather than
    drop the call entirely (leaving the symbol with no references) we link the
    site to every overload of that name -- a faithful, sound over-approximation
    for find-references / call-graph navigation: the site does call one of them.

    Each candidate USR is TU-invariant by contract, so a candidate not yet in
    the DB is given a USR-keyed stub (backfilled when its defining TU is indexed
    later) -- making the call order-independent. True system/stdlib candidates
    (ADL overload sets that are never separately indexed) are skipped so they do
    not become permanent unresolved externals.

    No receiver/argument provenance is recorded: that feeds virtual-dispatch
    devirtualization, and a function-template call is never a virtual dispatch.
    """
    cands = _overload_set_candidates(call_cursor)
    if len(cands) < 2:
        return
    dst_ids: set[int] = set()
    for cand in cands:
        usr = cand.get_usr()
        if not usr:
            continue
        s = db.lookup_symbol(usr)
        if s is not None:
            dst_ids.add(s.id)
            continue
        # Not yet indexed: mint a USR-keyed stub so a later index backfills it,
        # but skip true system/stdlib overloads.
        if cand.location.is_in_system_header:
            continue
        _dfid, _dln, _dcol, _dpath = _ref_decl_loc(db, cand)
        dst_ids.add(
            db.mint_symbol_id(
                usr,
                cand.spelling,
                _qualified_name(cand),
                cand.displayname,
                _KIND_MAP.get(cand.kind, "function"),
                decl_file_id=_dfid,
                decl_line=_dln,
                decl_col=_dcol,
                decl_path=_dpath,
            )
        )
    if not dst_ids:
        # Nothing resolved or minted (e.g. all candidates system/USR-less):
        # fall back to the shared qualified name + kind over indexed symbols.
        first = cands[0]
        qn = _qualified_name(first)
        if qn:
            for s in db.lookup_symbols_by_qual_name(
                qn, _KIND_MAP.get(first.kind, "function")
            ):
                dst_ids.add(s.id)
    if not dst_ids:
        return
    loc = call_cursor.location
    for dst_id in dst_ids:
        edge_id = db.add_edge(src_id, dst_id, 1)  # calls
        db.add_edge_site(
            edge_id,
            file_id,
            loc.line,
            loc.column,
            conditional=1 if cond_depth > 0 else 0,
        )


def _mint_instantiation_nodes(
    db: Storage,
    ref: cx.Cursor,
    member_id: int,
    prim_member_id: int,
) -> None:
    """ADR-004: mint the X<int> type node, write its template_arg rows, and add
    instantiates + method_of edges for an implicit template instantiation.

    Called from _body_descent when the callee `ref` is an instantiation member
    (clang_getSpecializedCursorTemplate returned a valid primary method).

    `member_id`      -- already-minted symbol id for X<int>::method (dst_id).
    `prim_member_id` -- symbol id of the primary template method X::method.

    Steps:
      (b) add instantiates(5) edge:  member_id -> prim_member_id
      (c) mint the X<int> TYPE node from ref.semantic_parent
      (d) add method_of(9) edge:     member_id -> type_id
      (e) add instantiates(5) edge:  type_id   -> class_primary_id
          (guarded: class primary must already be indexed)
      (f) write template_arg rows on the TYPE node from
          parent.type.get_template_argument_type(i)
          (TYPE args only via the type API; INTEGRAL/other are skipped here
           since clang.Type has no get_template_argument_kind/value; see
           ADR-004 §1b known limitation for method-template targs)
    """
    # (b) member instantiates -> primary method
    db.add_edge(member_id, prim_member_id, 5)

    # (c) mint X<int> TYPE node
    parent = ref.semantic_parent
    if parent is None:
        return
    type_usr = parent.get_usr()
    if not type_usr:
        return
    parent_kind = _KIND_MAP.get(parent.kind, "struct")
    _tdfid, _tdln, _tdcol, _tdpath = _ref_decl_loc(db, parent)
    type_id = db.mint_symbol_id(
        type_usr,
        parent.spelling,
        _qualified_name(parent),
        parent.displayname,
        parent_kind,
        decl_file_id=_tdfid,
        decl_line=_tdln,
        decl_col=_tdcol,
        decl_path=_tdpath,
        is_instantiation=True,
    )

    # (d) method_of(9): member_id -> type_id
    db.add_edge(member_id, type_id, 9)

    # (e) instantiates(5): type_id -> class primary, guarded by primary indexed
    class_primary = _lib.clang_getSpecializedCursorTemplate(parent)
    if (
        class_primary is not None
        and class_primary.kind.value > 0
        and class_primary.kind
        not in (
            cx.CursorKind.NO_DECL_FOUND,
            cx.CursorKind.INVALID_FILE,
        )
    ):
        class_prim_usr = class_primary.get_usr()
        if class_prim_usr and class_prim_usr != type_usr:
            class_prim_sym = db.lookup_symbol(class_prim_usr)
            if class_prim_sym is not None:
                db.add_edge(type_id, class_prim_sym.id, 5)

    # (f) template_arg rows on the TYPE node (TYPE args only via type API)
    # clang.Type has get_template_argument_type(i) but no
    # get_template_argument_kind — we can only extract TYPE args here.
    # For a method template, get_num_template_arguments on the type returns < 0
    # (the type is not itself specialized); log once and skip.
    parent_type = parent.type
    nargs = parent_type.get_num_template_arguments() if parent_type is not None else -1
    if nargs < 0:
        # Method-template or type API returned nothing; node+edges already
        # emitted above. Log the gap per ADR-004 §1b.
        import logging as _logging

        _logging.getLogger("cidx").debug(
            "template-instantiation-nodes: no type-level targs for %s "
            "(method-template or unavailable from clang.Type API)",
            type_usr,
        )
        return
    for ai in range(nargs):
        arg_type = parent_type.get_template_argument_type(ai)
        if arg_type is None:
            continue
        arg_spelling = arg_type.spelling or None
        ref_id: Optional[int] = None
        arg_decl = arg_type.get_declaration()
        if arg_decl is not None and arg_decl.kind not in (
            cx.CursorKind.NO_DECL_FOUND,
            cx.CursorKind.INVALID_FILE,
        ):
            arg_usr = arg_decl.get_usr()
            if arg_usr:
                rsym = db.lookup_symbol(arg_usr)
                if rsym is not None:
                    ref_id = rsym.id
        # All args from the type API are TYPE args (arg_kind=1)
        db.add_template_arg(type_id, ai, 1, ref_id=ref_id, literal=arg_spelling)


def _body_descent(
    db: Storage,
    fn_cursor: cx.Cursor,
    src_id: int,
    file_id: int,
    cond_depth: int = 0,
    enclosing_owner_usr: Optional[str] = None,
) -> None:
    """Recurse through the body of a function-like cursor emitting calls + uses edges.

    For each CALL_EXPR: emit upsert_edge(src_id, dst_id, kind=1) +
    add_edge_site(edge_id, file_id, line, col, conditional).
    For each DECL_REF_EXPR/MEMBER_REF_EXPR referencing a non-function indexed
    symbol: emit upsert_edge(src_id, dst_id, kind=7) + add_edge_site.
    Tracks cond_depth for IF/FOR/WHILE/DO/SWITCH/CASE/CONDITIONAL_OPERATOR.
    """
    for child in fn_cursor.get_children():
        kind = child.kind
        if kind == cx.CursorKind.CALL_EXPR:
            ref = child.referenced
            recovered = False
            if ref is None:
                # Dependent/overloaded callee inside a template body (e.g.
                # `combine(a, b)` in Stack<T>::summary): recover it from the
                # callee's single-overload OVERLOADED_DECL_REF. See
                # _recover_overloaded_callee.
                ref = _recover_overloaded_callee(child)
                recovered = ref is not None
                if ref is None:
                    # Multi-candidate dependent overload set (e.g. an overloaded
                    # member function template `cache.set(...)` called from
                    # another template body): the single-overload recovery above
                    # declines it. Link the site to every indexed overload so the
                    # references are complete. See _emit_overloaded_calls.
                    _emit_overloaded_calls(db, child, src_id, file_id, cond_depth)
            if ref is not None:
                callee_usr: str = ref.get_usr()
                if callee_usr:
                    # Resolved calls mint a stub for an unindexed target. A
                    # RECOVERED (dependent) call does the same -- a USR is
                    # TU-invariant by contract, so a stub keyed on the callee's
                    # USR is backfilled when its defining TU is indexed later,
                    # making the call order-independent. (An earlier belief that
                    # libclang emits an inconsistent USR for dependent member
                    # templates was a parse artifact: a fatal builtin-header
                    # miss truncated `std::string` to a fallback type. With a
                    # complete parse the call-site USR matches the declaration.)
                    if recovered:
                        _dst = db.lookup_symbol(callee_usr)
                        if _dst is None:
                            # USR not yet indexed: try the stable fully-qualified
                            # name + kind first (links to an already-present
                            # symbol when unambiguous), else mint a USR-keyed
                            # stub for a later index to backfill.
                            _qn = _qualified_name(ref)
                            if _qn:
                                _cands = db.lookup_symbols_by_qual_name(
                                    _qn, _KIND_MAP.get(ref.kind, "function")
                                )
                                if len(_cands) == 1:
                                    _dst = _cands[0]
                        dst_id = _dst.id if _dst is not None else None
                        if dst_id is None and not ref.location.is_in_system_header:
                            # Skip true system/stdlib targets (e.g. one-arg
                            # std::move) -- they are never separately indexed, so
                            # a stub would be a permanent unresolved external.
                            _dfid, _dln, _dcol, _dpath = _ref_decl_loc(db, ref)
                            dst_id = db.mint_symbol_id(
                                callee_usr,
                                ref.spelling,
                                _qualified_name(ref),
                                ref.displayname,
                                _KIND_MAP.get(ref.kind, "function"),
                                decl_file_id=_dfid,
                                decl_line=_dln,
                                decl_col=_dcol,
                                decl_path=_dpath,
                            )
                    else:
                        _dfid, _dln, _dcol, _dpath = _ref_decl_loc(db, ref)
                        # Pre-check: is this an instantiation member? We set
                        # is_instantiation=1 on the mint if the callee has a
                        # specialized parent (clang_getSpecializedCursorTemplate
                        # returns a valid non-null cursor with a non-trivial kind).
                        _pre_primary = _lib.clang_getSpecializedCursorTemplate(ref)
                        _is_inst_member = (
                            _pre_primary is not None
                            and _pre_primary.kind.value > 0
                            and _pre_primary.kind
                            not in (
                                cx.CursorKind.NO_DECL_FOUND,
                                cx.CursorKind.INVALID_FILE,
                            )
                        )
                        _pre_prim_usr = (
                            _pre_primary.get_usr() if _is_inst_member else ""
                        )
                        _is_inst_member = (
                            _is_inst_member
                            and bool(_pre_prim_usr)
                            and _pre_prim_usr != callee_usr
                        )
                        dst_id = db.mint_symbol_id(
                            callee_usr,
                            ref.spelling,
                            _qualified_name(ref),
                            ref.displayname,
                            _KIND_MAP.get(ref.kind, "function"),
                            decl_file_id=_dfid,
                            decl_line=_dln,
                            decl_col=_dcol,
                            decl_path=_dpath,
                            is_instantiation=_is_inst_member,
                        )
                    if dst_id is not None:
                        edge_id = db.add_edge(src_id, dst_id, 1)  # calls
                        loc = child.location
                        # Phase 2: compute receiver provenance for member calls
                        recv_src_kind: Optional[str] = None
                        recv_type_usr: Optional[str] = None
                        recv_decl_usr: Optional[str] = None
                        recv_param_pos: Optional[int] = None
                        recv_expr = _receiver_subexpr(child)
                        if recv_expr is not None:
                            (recv_src_kind, recv_type_usr, recv_decl_usr, _) = (
                                _classify_value_source(recv_expr)
                            )
                            # If the receiver is a local PARM_DECL, record its
                            # 0-based parameter position for position-indexed
                            # Gamma binding in the Phase-2 engine.
                            if recv_src_kind == "local" and recv_decl_usr:
                                recv_param_pos = _parm_position(recv_expr)
                        elif ref is not None and ref.kind in (
                            cx.CursorKind.CXX_METHOD,
                            cx.CursorKind.CONSTRUCTOR,
                            cx.CursorKind.DESTRUCTOR,
                        ):
                            # Implicit this (MEMBER_REF_EXPR had no child)
                            owner = ref.semantic_parent
                            if owner is not None:
                                owner_usr = owner.get_usr()
                                recv_src_kind = "this"
                                recv_type_usr = owner_usr or None
                                recv_decl_usr = owner_usr or None
                        # Phase 3a: compute recv_type_is_value for value-eligible
                        # src_kinds (member/global/call_result).
                        recv_type_is_value: Optional[int] = None
                        if (
                            recv_src_kind in ("member", "global", "call_result")
                            and recv_expr is not None
                        ):
                            dispatch_usr = None
                            if ref is not None and ref.kind in (
                                cx.CursorKind.CXX_METHOD,
                                cx.CursorKind.CONSTRUCTOR,
                                cx.CursorKind.DESTRUCTOR,
                                cx.CursorKind.CONVERSION_FUNCTION,
                            ):
                                owner = ref.semantic_parent
                                dispatch_usr = (
                                    owner.get_usr() if owner is not None else None
                                )
                            # Use the DECLARED type of the underlying decl, not the
                            # use-site expression type (which auto-derefs references
                            # in libclang: B& br presents as B at the call-site).
                            peeled_recv = _peel_expr(recv_expr)
                            decl_type = _decl_type_for_expr(peeled_recv)
                            recv_type_is_value = (
                                1
                                if (
                                    decl_type is not None
                                    and _type_is_value(decl_type, dispatch_usr)
                                )
                                else 0
                            )
                        db.add_edge_site(
                            edge_id,
                            file_id,
                            loc.line,
                            loc.column,
                            conditional=1 if cond_depth > 0 else 0,
                            recv_src_kind=recv_src_kind,
                            recv_type_usr=recv_type_usr,
                            recv_decl_usr=recv_decl_usr,
                            recv_param_pos=recv_param_pos,
                            recv_type_is_value=recv_type_is_value,
                        )
                        # Phase 2: emit call_arg rows for non-literal positional args
                        for pos, arg_cursor in enumerate(child.get_arguments()):
                            (a_kind, a_type_usr, a_decl_usr, a_callee_usr) = (
                                _classify_value_source(arg_cursor)
                            )
                            if a_kind != "literal":
                                # Phase 3a: compute type_is_value for eligible arg kinds.
                                # Use the declared type of the underlying decl (not the
                                # use-site expression type which auto-derefs references).
                                a_is_value: Optional[int] = None
                                if (
                                    a_kind
                                    in ("member", "global", "call_result", "local")
                                    and a_type_usr
                                ):
                                    peeled_arg = _peel_expr(arg_cursor)
                                    decl_type_a = _decl_type_for_expr(peeled_arg)
                                    a_is_value = (
                                        1
                                        if (
                                            decl_type_a is not None
                                            and _type_is_value(decl_type_a, a_type_usr)
                                        )
                                        else 0
                                    )
                                db.add_call_arg(
                                    edge_id,
                                    file_id,
                                    loc.line,
                                    loc.column,
                                    pos,
                                    a_kind,
                                    type_usr=a_type_usr,
                                    decl_usr=a_decl_usr,
                                    callee_usr=a_callee_usr,
                                    type_is_value=a_is_value,
                                )
                        # B3 instantiates (kind=5): when the callee is a template
                        # specialization, emit an edge to the primary template.
                        # Only emit when primary is already indexed (no stubs for
                        # stdlib templates — prevents inflating stub count for
                        # std::vector, std::move, etc.). For a recovered primary
                        # template this is a no-op (it has no specialized parent).
                        primary = _lib.clang_getSpecializedCursorTemplate(ref)
                        if (
                            primary is not None
                            and primary.kind.value > 0
                            and primary.kind
                            not in (
                                cx.CursorKind.NO_DECL_FOUND,
                                cx.CursorKind.INVALID_FILE,
                            )
                        ):
                            prim_usr = primary.get_usr()
                            if prim_usr and prim_usr != callee_usr:
                                prim_sym = db.lookup_symbol(prim_usr)
                                if prim_sym is not None:
                                    db.add_edge(src_id, prim_sym.id, 5)  # instantiates
                                    # ADR-004 instantiation-member promotion block.
                                    # Runs alongside the existing caller→primary
                                    # edge above; does NOT replace it.
                                    # (a) member node already minted as dst_id with
                                    #     is_instantiation=1 (handled at call-site
                                    #     mint below via _mint_instantiation_nodes).
                                    # Delegate to helper to keep this loop concise.
                                    if dst_id is not None:
                                        _mint_instantiation_nodes(
                                            db, ref, dst_id, prim_sym.id
                                        )
        elif kind in (cx.CursorKind.DECL_REF_EXPR, cx.CursorKind.MEMBER_REF_EXPR):
            # B2 uses: non-function indexed symbols referenced in function body.
            # Only emit for symbols already in the DB (lookup, no stub) —
            # prevents creating stubs for every stdlib constant touched in body.
            ref = child.referenced
            if ref is not None:
                ref_kind = ref.kind
                if ref_kind not in _FUNCTION_KINDS and ref_kind not in (
                    cx.CursorKind.CXX_METHOD,
                    cx.CursorKind.CONSTRUCTOR,
                    cx.CursorKind.DESTRUCTOR,
                ):
                    ref_usr: str = ref.get_usr()
                    if ref_usr:
                        dst_sym = db.lookup_symbol(ref_usr)
                        if dst_sym is not None:
                            edge_id = db.add_edge(src_id, dst_sym.id, 7)  # uses
                            loc = child.location
                            db.add_edge_site(
                                edge_id,
                                file_id,
                                loc.line,
                                loc.column,
                                conditional=1 if cond_depth > 0 else 0,
                            )
        elif kind in (cx.CursorKind.TYPE_REF, cx.CursorKind.TEMPLATE_REF):
            # B2 uses: a bare type NAME in an expression/statement position
            # (e.g. `Color::Red`, `MyClass::instance()`, `sizeof(T)`,
            # `static_cast<T>(...)`, `new T`). libclang exposes these as
            # TYPE_REF / TEMPLATE_REF cursors that no other branch handles, so
            # the named type would otherwise be invisible in the graph.
            #
            # PARENT-KIND GUARD: in _body_descent the immediate parent of
            # `child` is `fn_cursor`. Signature / field / var-decl / typedef
            # type-refs are already emitted by the declaration paths
            # (_emit_type_use, template_arg rows). Those type-refs are direct
            # children of a *declaration* cursor, so skip when fn_cursor is a
            # declaration kind. Only type-names under expression/statement
            # nodes (parent is a CALL_EXPR, DECL_REF_EXPR, UNEXPOSED_EXPR, ...)
            # survive this guard.
            if fn_cursor.kind not in _TYPEREF_PARENT_SKIP_KINDS:
                ref = child.referenced
                if ref is not None:
                    usr: str = ref.get_usr()
                    # Lookup-only, NO stubs: stdlib / unindexed types create no
                    # edges. Skip self-edge and the enclosing method's own
                    # owning record (redundant with method_of). Use the
                    # immutable `enclosing_owner_usr` parameter, NOT the local
                    # `owner_usr` temporary reassigned by the CALL_EXPR
                    # implicit-`this` branch (which would otherwise leak the
                    # callee's class USR into descendants and wrongly suppress
                    # legitimate type uses).
                    if usr and usr != enclosing_owner_usr:
                        dst = db.lookup_symbol(usr)
                        if dst is not None and dst.id != src_id:
                            edge_id = db.add_edge(src_id, dst.id, 7)  # uses
                            loc = child.location
                            db.add_edge_site(
                                edge_id,
                                file_id,
                                loc.line,
                                loc.column,
                                conditional=1 if cond_depth > 0 else 0,
                            )
        elif kind == cx.CursorKind.VAR_DECL:
            # B2 uses: a LOCAL variable's declared type names a record/enum/
            # typedef -> uses edge (src=enclosing fn). `Conf local;` counts as
            # the function using Conf even when no method is called on it.
            _emit_type_use(
                db,
                src_id,
                child.type,
                file_id,
                child.location,
                conditional=1 if cond_depth > 0 else 0,
            )
            # B3 class-template instantiates (kind=5): when a variable's type is
            # a class-template instantiation, emit instantiates (src=enclosing fn,
            # dst=primary template) + template_arg rows.
            # Only emit when the primary is already indexed (no stubs for stdlib
            # types — prevents inflating stub count for std::string, std::vector).
            var_type = child.type
            nargs = _lib.clang_Type_getNumTemplateArguments(var_type)
            if nargs > 0:
                type_decl = var_type.get_declaration()
                if (
                    type_decl is not None
                    and type_decl.kind != cx.CursorKind.NO_DECL_FOUND
                    and type_decl.kind.value > 0
                ):
                    primary = _lib.clang_getSpecializedCursorTemplate(type_decl)
                    if (
                        primary is not None
                        and primary.kind.value > 0
                        and primary.kind
                        not in (
                            cx.CursorKind.NO_DECL_FOUND,
                            cx.CursorKind.INVALID_FILE,
                        )
                    ):
                        prim_usr = primary.get_usr()
                        if prim_usr:
                            prim_sym = db.lookup_symbol(prim_usr)
                            if prim_sym is not None:
                                # instantiates edge: fn -> primary template
                                db.add_edge(src_id, prim_sym.id, 5)  # instantiates
                                # template_arg rows: owner_id = src_id (the using
                                # function), recording which types are used.
                                for ai in range(nargs):
                                    arg_type = (
                                        _lib.clang_Type_getTemplateArgumentAsType(
                                            var_type, ai
                                        )
                                    )
                                    ref_id: Optional[int] = None
                                    # Always store the type spelling as literal so
                                    # builtin args (e.g. int, bool) are
                                    # distinguishable — mirrors C++ peel_expr which
                                    # always writes ta.literal = spelling.
                                    arg_literal = arg_type.spelling or None
                                    arg_decl = arg_type.get_declaration()
                                    if (
                                        arg_decl is not None
                                        and arg_decl.kind != cx.CursorKind.NO_DECL_FOUND
                                        and arg_decl.kind.value > 0
                                    ):
                                        ref_usr = arg_decl.get_usr()
                                        if ref_usr:
                                            rsym = db.lookup_symbol(ref_usr)
                                            if rsym is not None:
                                                ref_id = rsym.id
                                    db.add_template_arg(
                                        src_id,
                                        ai,
                                        1,
                                        ref_id=ref_id,
                                        literal=arg_literal,
                                    )
        is_cond = kind in _COND_KINDS
        _body_descent(
            db,
            child,
            src_id,
            file_id,
            cond_depth + (1 if is_cond else 0),
            enclosing_owner_usr,
        )


def _index_edges_notxn(
    db: Storage, tu: cx.TranslationUnit, filename: str, file_id: int
) -> None:
    """M4: txn-free inner work of index_edges; caller MUST own an open transaction.

    Extract typed edges from this TU's AST for `filename`.
    Must be called AFTER index_symbols for the same file (B1 declaration-level
    + B2 body descent). Edge deletion is NOT done here; caller handles it.
    """
    # B1: declaration-level edges from the same file-cursor stream.
    # Use the parent-aware walk so that CXX_BASE_SPECIFIER handlers can get
    # the enclosing record from the walk parent (spec §1.4: semantic_parent
    # and lexical_parent are both NULL on that cursor kind — probed in
    # geometry.hpp Circle:Shape).
    for cursor, walk_parent in _file_cursors_p(tu, filename):
        ck = cursor.kind

        # -- contains (kind=3): namespace/record → child symbol ---------------
        # Emitted FIRST so it fires regardless of which handler runs below
        # (each handler may `continue` before reaching the end of the loop).
        # src = the enclosing namespace or record; dst = this cursor.
        # Covers: NAMESPACE_DECL → any indexed child,
        #         record/class_template → nested type/enum/typedef/union.
        # Does NOT duplicate field_of (members) or method_of (methods) —
        # those emit child→parent, while contains emits parent→child.
        _pk = walk_parent.kind
        _parent_is_ns = _pk == cx.CursorKind.NAMESPACE
        _parent_is_record = _pk in (
            cx.CursorKind.CLASS_DECL,
            cx.CursorKind.STRUCT_DECL,
            cx.CursorKind.CLASS_TEMPLATE,
            cx.CursorKind.UNION_DECL,
        )
        _child_is_nested_type = ck in (
            cx.CursorKind.CLASS_DECL,
            cx.CursorKind.STRUCT_DECL,
            cx.CursorKind.UNION_DECL,
            cx.CursorKind.ENUM_DECL,
            cx.CursorKind.TYPEDEF_DECL,
            cx.CursorKind.TYPE_ALIAS_DECL,
            cx.CursorKind.CLASS_TEMPLATE,
            cx.CursorKind.FUNCTION_TEMPLATE,
        )
        if _parent_is_ns or (_parent_is_record and _child_is_nested_type):
            _child_usr = cursor.get_usr()
            _parent_usr = walk_parent.get_usr()
            if _child_usr and _parent_usr:
                _child_sym = db.lookup_symbol(_child_usr)
                _parent_sym = db.lookup_symbol(_parent_usr)
                if _child_sym is not None and _parent_sym is not None:
                    db.add_edge(_parent_sym.id, _child_sym.id, 3)  # contains

        # -- uses (kind=7): TYPE references in signatures / fields / vars ------
        # A class named only as a parameter, return, field, variable, or
        # typedef-underlying type never appears as a body DECL_REF_EXPR, so the
        # body-descent `uses` pass misses it. Emit those signature-level uses
        # here. Emitted FIRST (alongside contains) so it fires regardless of a
        # later handler's `continue`. (Local-variable types inside bodies are
        # handled in _body_descent; the walk does not descend into bodies here.)
        if ck in _FUNCTION_KINDS:
            _fn_usr = cursor.get_usr()
            if _fn_usr:
                _fn_sym = db.lookup_symbol(_fn_usr)
                if _fn_sym is not None:
                    # return type (constructors/destructors have none worth recording)
                    if ck not in (cx.CursorKind.CONSTRUCTOR, cx.CursorKind.DESTRUCTOR):
                        _emit_type_use(
                            db, _fn_sym.id, cursor.result_type, file_id, cursor.location
                        )
                    for _arg in cursor.get_arguments():
                        _emit_type_use(
                            db, _fn_sym.id, _arg.type, file_id, _arg.location
                        )
        elif ck == cx.CursorKind.FIELD_DECL:
            _m_usr = cursor.get_usr()
            if _m_usr:
                _m_sym = db.lookup_symbol(_m_usr)
                if _m_sym is not None:
                    _emit_type_use(db, _m_sym.id, cursor.type, file_id, cursor.location)
        elif ck == cx.CursorKind.VAR_DECL:
            # File-scope variable (locals are reached via _body_descent).
            _v_usr = cursor.get_usr()
            if _v_usr:
                _v_sym = db.lookup_symbol(_v_usr)
                if _v_sym is not None:
                    _emit_type_use(db, _v_sym.id, cursor.type, file_id, cursor.location)
        elif ck in (cx.CursorKind.TYPEDEF_DECL, cx.CursorKind.TYPE_ALIAS_DECL):
            _t_usr = cursor.get_usr()
            if _t_usr:
                _t_sym = db.lookup_symbol(_t_usr)
                if _t_sym is not None:
                    _emit_type_use(
                        db,
                        _t_sym.id,
                        cursor.underlying_typedef_type,
                        file_id,
                        cursor.location,
                    )

        # -- CXX_BASE_SPECIFIER: inherits ---------------------------------
        # Derived class is the enclosing record from the walk parent, NOT
        # from semantic_parent (which is NULL — spec §1.4 gotcha).
        if ck == cx.CursorKind.CXX_BASE_SPECIFIER:
            pk = walk_parent.kind
            if pk not in (cx.CursorKind.CLASS_DECL, cx.CursorKind.STRUCT_DECL):
                continue  # unexpected parent; skip
            derived_usr = walk_parent.get_usr()
            if not derived_usr:
                continue
            ref = cursor.referenced
            if ref is None:
                continue
            base_usr = ref.get_usr()
            if not base_usr:
                continue
            src_sym = db.lookup_symbol(derived_usr)
            if src_sym is None:
                continue
            _dfid, _dln, _dcol, _dpath = _ref_decl_loc(db, ref)
            dst_id = db.mint_symbol_id(
                base_usr,
                ref.spelling,
                _qualified_name(ref),
                ref.displayname,
                _KIND_MAP.get(ref.kind, "function"),
                decl_file_id=_dfid,
                decl_line=_dln,
                decl_col=_dcol,
                decl_path=_dpath,
            )
            acc_map = {
                cx.AccessSpecifier.PUBLIC: 1,
                cx.AccessSpecifier.PROTECTED: 2,
                cx.AccessSpecifier.PRIVATE: 3,
            }
            base_access: Optional[int] = acc_map.get(cursor.access_specifier)
            is_virtual: Optional[int] = 1 if _lib.clang_isVirtualBase(cursor) else 0
            db.add_edge(
                src_sym.id,
                dst_id,
                2,  # inherits
                base_access=base_access,
                is_virtual=is_virtual,
            )
            continue

        # -- FIELD_DECL: field_of -----------------------------------------
        if ck == cx.CursorKind.FIELD_DECL:
            member_usr = cursor.get_usr()
            if not member_usr:
                continue
            owner = cursor.semantic_parent
            if owner is None or owner.kind == cx.CursorKind.TRANSLATION_UNIT:
                continue
            owner_usr = owner.get_usr()
            if not owner_usr:
                continue
            src_sym = db.lookup_symbol(member_usr)
            dst_sym = db.lookup_symbol(owner_usr)
            if src_sym is None or dst_sym is None:
                continue
            db.add_edge(src_sym.id, dst_sym.id, 8)  # field_of
            continue

        # -- CXX_METHOD/CONSTRUCTOR/DESTRUCTOR: method_of -----------------
        if ck in (
            cx.CursorKind.CXX_METHOD,
            cx.CursorKind.CONSTRUCTOR,
            cx.CursorKind.DESTRUCTOR,
        ):
            method_usr = cursor.get_usr()
            if not method_usr:
                continue
            owner = cursor.semantic_parent
            if owner is None or owner.kind == cx.CursorKind.TRANSLATION_UNIT:
                continue
            owner_usr = owner.get_usr()
            if not owner_usr:
                continue
            src_sym = db.lookup_symbol(method_usr)
            dst_sym = db.lookup_symbol(owner_usr)
            if src_sym is None or dst_sym is None:
                continue
            db.add_edge(src_sym.id, dst_sym.id, 9)  # method_of

            # overrides (CXX_METHOD only)
            if ck == cx.CursorKind.CXX_METHOD:
                for ov in _get_overridden_cursors(cursor):
                    ov_usr = ov.get_usr()
                    if not ov_usr:
                        continue
                    _dfid, _dln, _dcol, _dpath = _ref_decl_loc(db, ov)
                    dst_ov = db.mint_symbol_id(
                        ov_usr,
                        ov.spelling,
                        _qualified_name(ov),
                        ov.displayname,
                        _KIND_MAP.get(ov.kind, "function"),
                        decl_file_id=_dfid,
                        decl_line=_dln,
                        decl_col=_dcol,
                        decl_path=_dpath,
                    )
                    db.add_edge(src_sym.id, dst_ov, 6)  # overrides
            continue

        # -- CLASS_TEMPLATE/FUNCTION_TEMPLATE: template_param -------------
        if ck in (cx.CursorKind.CLASS_TEMPLATE, cx.CursorKind.FUNCTION_TEMPLATE):
            tmpl_usr = cursor.get_usr()
            if not tmpl_usr:
                continue
            tmpl_sym = db.lookup_symbol(tmpl_usr)
            if tmpl_sym is None:
                continue
            # A member function template (FUNCTION_TEMPLATE whose semantic parent
            # is a record/class-template) is a method too, but the CXX_METHOD
            # method_of block above never sees it (its cursor kind is
            # FUNCTION_TEMPLATE), so it would lack a method_of edge and be
            # invisible to method-oriented queries. Emit method_of here.
            if ck == cx.CursorKind.FUNCTION_TEMPLATE:
                owner = cursor.semantic_parent
                if owner is not None and owner.kind in (
                    cx.CursorKind.CLASS_DECL,
                    cx.CursorKind.STRUCT_DECL,
                    cx.CursorKind.CLASS_TEMPLATE,
                    cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
                ):
                    owner_usr = owner.get_usr()
                    if owner_usr:
                        owner_sym = db.lookup_symbol(owner_usr)
                        if owner_sym is not None:
                            db.add_edge(tmpl_sym.id, owner_sym.id, 9)  # method_of
            _PARAM_KIND_MAP = {
                cx.CursorKind.TEMPLATE_TYPE_PARAMETER: 1,
                cx.CursorKind.TEMPLATE_NON_TYPE_PARAMETER: 2,
                cx.CursorKind.TEMPLATE_TEMPLATE_PARAMETER: 3,
            }
            pos = 0
            for child in cursor.get_children():
                pk = _PARAM_KIND_MAP.get(child.kind)
                if pk is None:
                    continue
                nm = child.spelling or None
                db.add_template_param(tmpl_sym.id, pos, pk, name=nm)
                pos += 1
            continue

        # -- STRUCT_DECL/CLASS_DECL: specializes --------------------------
        if ck in (cx.CursorKind.STRUCT_DECL, cx.CursorKind.CLASS_DECL):
            if not cursor.is_definition():
                continue
            primary = _lib.clang_getSpecializedCursorTemplate(cursor)
            if (
                primary is None
                or primary.kind == cx.CursorKind.NO_DECL_FOUND
                or primary.kind == cx.CursorKind.INVALID_FILE
                or primary.kind.value <= 0
            ):
                continue
            spec_usr = cursor.get_usr()
            prim_usr = primary.get_usr()
            if not spec_usr or not prim_usr or spec_usr == prim_usr:
                continue
            spec_sym = db.lookup_symbol(spec_usr)
            if spec_sym is None:
                continue
            _dfid, _dln, _dcol, _dpath = _ref_decl_loc(db, primary)
            prim_id = db.mint_symbol_id(
                prim_usr,
                primary.spelling,
                _qualified_name(primary),
                primary.displayname,
                _KIND_MAP.get(primary.kind, "function"),
                decl_file_id=_dfid,
                decl_line=_dln,
                decl_col=_dcol,
                decl_path=_dpath,
            )
            # An explicit instantiation (`template class Foo<int>;`) is a concrete
            # INSTANCE of the template, not a specialization of it: record it as
            # `instantiates` (kind=5, instance -> primary) so it surfaces under
            # ClassTemplate.instantiations(). A true explicit specialization
            # (`template <> class Foo<bool> {...}`) stays `specializes` (kind=4).
            edge_kind = 5 if _is_explicit_instantiation(cursor) else 4
            db.add_edge(spec_sym.id, prim_id, edge_kind)

            # template_arg rows. For TYPE args we always store the type spelling
            # in `literal` (e.g. 'bool', 'int') so the binding is distinguishable
            # even when the arg is a builtin with no declaration to resolve a
            # ref_id from -- the previous code left such rows with neither ref_id
            # nor literal, making Foo<bool> and Foo<int> indistinguishable.
            nargs = cursor.get_num_template_arguments()
            for ai in range(nargs if nargs >= 0 else 0):
                tak = cursor.get_template_argument_kind(ai)
                if tak == cx.TemplateArgumentKind.TYPE:
                    arg_type = cursor.get_template_argument_type(ai)
                    arg_decl = arg_type.get_declaration()
                    ref_id: Optional[int] = None
                    if (
                        arg_decl is not None
                        and arg_decl.kind != cx.CursorKind.NO_DECL_FOUND
                    ):
                        ref_usr = arg_decl.get_usr()
                        if ref_usr:
                            rsym = db.lookup_symbol(ref_usr)
                            if rsym is not None:
                                ref_id = rsym.id
                    db.add_template_arg(
                        spec_sym.id,
                        ai,
                        1,
                        ref_id=ref_id,
                        literal=arg_type.spelling or None,
                    )
                elif tak == cx.TemplateArgumentKind.INTEGRAL:
                    literal = str(cursor.get_template_argument_value(ai))
                    db.add_template_arg(spec_sym.id, ai, 2, literal=literal)
                else:
                    db.add_template_arg(spec_sym.id, ai, int(tak))
            continue

    # B2: body descent for calls -- recurse into each function-like definition.
    for cursor in _file_cursors(tu, filename):
        if cursor.kind not in _FUNCTION_DEF_KINDS:
            continue
        if not cursor.is_definition():
            continue
        fn_usr = cursor.get_usr()
        if not fn_usr:
            continue
        fn_sym = db.lookup_symbol(fn_usr)
        if fn_sym is None:
            continue
        # Self-owner skip for the TYPE_REF branch: when `cursor` is a method,
        # its semantic parent is the owning record; record that USR so a method
        # naming its own class does not emit a redundant uses edge.
        _owner = cursor.semantic_parent
        _owner_usr = (
            _owner.get_usr()
            if _owner is not None
            and _owner.kind
            in (
                cx.CursorKind.CLASS_DECL,
                cx.CursorKind.STRUCT_DECL,
                cx.CursorKind.UNION_DECL,
                cx.CursorKind.CLASS_TEMPLATE,
                cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
            )
            else None
        )
        _body_descent(
            db, cursor, fn_sym.id, file_id, enclosing_owner_usr=_owner_usr
        )


def index_edges(
    db: Storage, tu: cx.TranslationUnit, filename: str, file_id: int
) -> None:
    """Extract typed edges from this TU's AST for `filename`.

    Must be called AFTER index_symbols for the same file. Runs inside one
    transaction (delete stale edges first, then B1 declaration-level + B2
    body descent).
    """
    # Delete stale edges from a previous index of this file.
    db.delete_edges_for_file(file_id)
    with db.transaction():
        _index_edges_notxn(db, tu, filename, file_id)


def index_source(
    db: Storage,
    filename: str,
    args: Sequence[str],
    file_id: int,
    ignore_system: bool | None = None,
    driver: str | None = None,
    no_graph: bool = False,
    header_options: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Parse one source file, index its symbols and its headers, free the TU.

    The translation unit exists only inside this call: nothing libclang-owned
    (cursors, tokens, the TU itself) escapes -- only plain-data Symbol rows are
    written to the database -- so dropping the last reference in the `finally`
    immediately runs clang_disposeTranslationUnit and returns the AST's memory.
    """
    tu = parse(filename, args, driver=driver)
    try:
        # Parse diagnostics (warnings + tolerated errors) captured while the TU
        # is live, before the AST is freed; persisted by the caller against
        # this file's row.
        diagnostics = collect_diagnostics(tu)
        stored, skipped = index_symbols(db, tu, file_id)
        headers = index_headers(
            db,
            tu,
            ignore_system=ignore_system,
            header_options=header_options,
            header_driver=driver,
        )
        if not no_graph:
            index_edges(db, tu, filename, file_id)
    finally:
        del tu  # last reference -> native AST freed NOW
    return {
        "symbols": stored,
        "skipped": skipped,
        "headers": headers,
        "diagnostics": diagnostics,
    }

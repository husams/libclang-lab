"""indexer.entity_rollup -- Layer-1 entity-edge materialisation (PR2).

Reads the Layer-0 symbol/edge graph written by the indexer and produces
entity_edge rows (v17 schema).  The caller is Storage.resolve_pass(); this
module is NEVER called during indexing -- it is a pure post-processing step
that only reads + writes the SQLite database.

Entity definition: a symbol whose kind is in {class(4), struct(2), union(3),
enum(5)}.  The entity_edge endpoints reference symbol(id) directly; no
separate entity table exists.

Relation ids (entity_edge_kind):
  1  generalizes   inheritance where base carries state/impl
  2  realizes      inheritance where base is a pure Interface
  3  specializes   explicit template specialization
  4  composes      field: value member (owns lifetime)
  5  aggregates    field: unique_ptr / shared_ptr (optional ownership)
  6  associates    field: raw ptr / ref / weak_ptr (borrowed / weak)
  7  creates       method allocates an entity (new / ctor / factory)
  8  uses          method uses an entity (calls virtual method on it)
  9  destroys      method deallocates an entity (delete)
  10 nests         record contains a nested record type
  11 befriends     friend class declaration

Layer-0 edge.kind ids for construction/destruction (PR1 seeds):
  10  construct-value    (maps to create_form 3)
  11  construct-temp     (maps to create_form 4)
  12  construct-heap     (maps to create_form 5)
  13  construct-copy     (maps to create_form 7)
  14  construct-move     (maps to create_form 8)
  15  factory-construct  (maps to create_form 6, partial=1)
  16  destroy            (destroys kind)

By-value return (create_form 2) is derived here from a method's return type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from indexer.storage import Storage

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Entity symbol.kind ints (v16+ storage: CXCursorKind values)
_ENTITY_KINDS = frozenset({2, 3, 4, 5})  # struct=2, union=3, class=4, enum=5

# entity_edge_kind ids
_EK_GENERALIZES = 1
_EK_REALIZES = 2
_EK_SPECIALIZES = 3
_EK_COMPOSES = 4
_EK_AGGREGATES = 5
_EK_ASSOCIATES = 6
_EK_CREATES = 7
_EK_USES = 8
_EK_DESTROYS = 9
_EK_NESTS = 10
_EK_BEFRIENDS = 11

# Layer-0 edge.kind ids
_L0_CALLS = 1
_L0_INHERITS = 2
_L0_CONTAINS = 3
_L0_SPECIALIZES = 4
_L0_INSTANTIATES = 5
_L0_OVERRIDES = 6
_L0_USES = 7
_L0_FIELD_OF = 8
_L0_METHOD_OF = 9
_L0_CONSTRUCT_VALUE = 10
_L0_CONSTRUCT_TEMP = 11
_L0_CONSTRUCT_HEAP = 12
_L0_CONSTRUCT_COPY = 13
_L0_CONSTRUCT_MOVE = 14
_L0_FACTORY_CONSTRUCT = 15
_L0_DESTROY = 16

# Layer-0 construction kind -> create_form int
_CONSTRUCT_FORM: dict[int, int] = {
    _L0_CONSTRUCT_VALUE: 3,    # value
    _L0_CONSTRUCT_TEMP: 4,     # temp
    _L0_CONSTRUCT_HEAP: 5,     # heap
    _L0_CONSTRUCT_COPY: 7,     # copy
    _L0_CONSTRUCT_MOVE: 8,     # move
    _L0_FACTORY_CONSTRUCT: 6,  # factory
}

# Access string -> int
_ACCESS_INT = {"public": 0, "protected": 1, "private": 2}


# ---------------------------------------------------------------------------
# Interface test
# ---------------------------------------------------------------------------

def _is_interface(db: "Storage", sym_id: int) -> bool:
    """Return True iff sym_id is a pure Interface.

    Interface = record whose methods are ALL pure-virtual AND has NO data
    fields (member symbols with kind=6/member).
    Destructor (kind=25) is EXCLUDED from the pure-virtual check so a class
    with only `virtual ~T() = default` can still qualify.
    """
    conn = db._conn
    # Any non-pure method (excluding destructors)?
    row = conn.execute(
        "SELECT COUNT(*) FROM symbol "
        "WHERE parent_usr = (SELECT usr FROM symbol WHERE id = ?) "
        "  AND kind = 21 "       # method (CXCursor_CXXMethod=21)
        "  AND is_pure = 0",
        (sym_id,),
    ).fetchone()
    if row and row[0] > 0:
        return False
    # Any data members?
    row = conn.execute(
        "SELECT COUNT(*) FROM symbol "
        "WHERE parent_usr = (SELECT usr FROM symbol WHERE id = ?) "
        "  AND kind = 6",       # member (CXCursor_FieldDecl=6)
        (sym_id,),
    ).fetchone()
    if row and row[0] > 0:
        return False
    # Must have at least one pure method to be a real interface
    row = conn.execute(
        "SELECT COUNT(*) FROM symbol "
        "WHERE parent_usr = (SELECT usr FROM symbol WHERE id = ?) "
        "  AND kind = 21 AND is_pure = 1",
        (sym_id,),
    ).fetchone()
    return bool(row and row[0] > 0)


# ---------------------------------------------------------------------------
# Type-classification kernel (for has-a / creates / by-value-return)
# ---------------------------------------------------------------------------

# Canonical prefixes of smart-ptr / container wrappers.
# We match on the type_info spelling (e.g. "std::unique_ptr<Foo>").
_UNIQUE_PTR_PREFIX = ("std::unique_ptr<", "unique_ptr<")
_SHARED_PTR_PREFIX = ("std::shared_ptr<", "shared_ptr<")
_WEAK_PTR_PREFIX   = ("std::weak_ptr<",   "weak_ptr<")
_PTR_SUFFIX = ("*",)
_REF_SUFFIX = ("&",)
_CONTAINER_PREFIXES = (
    "std::vector<", "vector<",
    "std::list<", "list<",
    "std::deque<", "deque<",
    "std::set<", "set<",
    "std::unordered_set<", "unordered_set<",
    "std::map<", "std::unordered_map<",
)


def _strip_qualifiers(spelling: str) -> str:
    """Remove const / volatile qualifiers and leading/trailing whitespace."""
    for q in ("const ", "volatile "):
        spelling = spelling.replace(q, "")
    return spelling.strip()


def _classify_field_type(
    type_spelling: str,
) -> tuple[int, int]:
    """Classify a field's type spelling into an entity_edge kind + multiplicity.

    Returns (kind_int, multiplicity_int):
      kind: _EK_COMPOSES / _EK_AGGREGATES / _EK_ASSOCIATES
      multiplicity: 1=one, 2=0..1, 3=0..*

    This function decides ownership semantics from the type:
    - value type (neither ptr/ref/smart-ptr): composes, multiplicity=1
    - unique_ptr: aggregates, multiplicity=2 (0..1 -- owns one)
    - shared_ptr: aggregates, multiplicity=2 (shared ownership)
    - raw ptr / ref / weak_ptr: associates, multiplicity=2
    - container of the above: lift the inner kind + multiplicity=3 (0..*)
    - arrays (T[N]): composes, multiplicity=4 (we simplify; N not recoverable here)
    """
    s = _strip_qualifiers(type_spelling)

    # Array types: T[N] or T[] -- composes, multiplicity=4 (N)
    if s.endswith("]"):
        return _EK_COMPOSES, 4

    # Container: std::vector<X>/std::set<X>/... -> inner classification + 0..*
    for prefix in _CONTAINER_PREFIXES:
        if s.startswith(prefix):
            inner = s[len(prefix):].rstrip("> ").strip()
            inner_kind, _ = _classify_field_type(inner)
            return inner_kind, 3

    # unique_ptr / shared_ptr → aggregates (optional ownership), multiplicity=2
    for prefix in _UNIQUE_PTR_PREFIX + _SHARED_PTR_PREFIX:
        if s.startswith(prefix):
            return _EK_AGGREGATES, 2

    # weak_ptr / raw ptr / ref → associates (borrowed / weak), multiplicity=2
    for prefix in _WEAK_PTR_PREFIX:
        if s.startswith(prefix):
            return _EK_ASSOCIATES, 2

    if s.endswith("*"):
        return _EK_ASSOCIATES, 2
    if s.endswith("&"):
        return _EK_ASSOCIATES, 2

    # Value type: composes, multiplicity=1
    return _EK_COMPOSES, 1


def _spelling_to_symbol_id(conn, canonical_spelling: str) -> Optional[int]:
    """Look up a symbol id by its qual_name or type_info spelling.

    Used by the kernel to find the entity referenced by a field type.
    Returns None when not found (partial=1 case).
    """
    # Strip std:: prefix wrappers for qual_name lookup
    s = canonical_spelling.strip()
    # Try direct qual_name match first
    row = conn.execute(
        "SELECT id FROM symbol WHERE qual_name = ? AND kind IN (2,3,4,5) LIMIT 1",
        (s,),
    ).fetchone()
    if row:
        return row["id"]
    # Try spelling match
    row = conn.execute(
        "SELECT id FROM symbol WHERE spelling = ? AND kind IN (2,3,4,5) LIMIT 1",
        (s,),
    ).fetchone()
    if row:
        return row["id"]
    return None


def _resolve_entity_from_type(conn, type_spelling: str) -> Optional[int]:
    """Unwrap type_spelling and return the entity symbol id, or None."""
    s = _strip_qualifiers(type_spelling)

    # Strip trailing &, *, []
    while s.endswith(("&", "*", "]")):
        if s.endswith("]"):
            s = s[:s.rfind("[")].strip()
        else:
            s = s[:-1].strip()
    s = _strip_qualifiers(s)

    # Strip smart-ptr / container wrappers to get the inner type
    for prefix in (
        _UNIQUE_PTR_PREFIX + _SHARED_PTR_PREFIX + _WEAK_PTR_PREFIX
        + _CONTAINER_PREFIXES
    ):
        if s.startswith(prefix):
            inner = s[len(prefix):].rstrip(">").strip()
            return _resolve_entity_from_type(conn, inner)

    return _spelling_to_symbol_id(conn, s)


# ---------------------------------------------------------------------------
# Main materialisation pass
# ---------------------------------------------------------------------------

def materialize_entity_edges(db: "Storage") -> None:
    """DELETE all entity_edge rows then re-materialise from the Layer-0 graph.

    Called by Storage.resolve_pass() after rollup_edge_counts().
    Pure DB-only pass: no libclang / AST re-parse.
    """
    conn = db._conn

    # Idempotent: full re-materialise each resolve.
    conn.execute("DELETE FROM entity_edge")
    conn.commit()

    with db.transaction():
        _materialise_inheritance(db)
        _materialise_specializes(db)
        _materialise_field_relations(db)
        _materialise_creates_destroys(db)
        _materialise_uses(db)
        _materialise_nests(db)
        _materialise_befriends(db)


# ---------------------------------------------------------------------------
# Phase 1: generalizes / realizes  (inherits edges between entity symbols)
# ---------------------------------------------------------------------------

def _materialise_inheritance(db: "Storage") -> None:
    conn = db._conn
    # All inherits(2) edges where BOTH endpoints are entity symbols.
    rows = conn.execute(
        "SELECT e.src_id, e.dst_id, e.base_access, e.is_virtual "
        "FROM edge e "
        "JOIN symbol src ON src.id = e.src_id "
        "JOIN symbol dst ON dst.id = e.dst_id "
        "WHERE e.kind = 2 "
        "  AND src.kind IN (2,3,4,5) "
        "  AND dst.kind IN (2,3,4,5)"
    ).fetchall()

    for r in rows:
        src_id, dst_id = r[0], r[1]
        raw_access = r[2]  # 0/1/2 or NULL
        is_virtual = r[3] or 0
        access = int(raw_access) if raw_access is not None else 0

        if _is_interface(db, dst_id):
            ek = _EK_REALIZES
        else:
            ek = _EK_GENERALIZES

        conn.execute(
            "INSERT INTO entity_edge "
            "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
            " access, is_virtual, create_form, partial) "
            "VALUES (?, ?, ?, 1, NULL, 1, ?, ?, NULL, 0) "
            "ON CONFLICT(src_id, dst_id, kind, via_member_id) DO UPDATE SET "
            "  access     = excluded.access, "
            "  is_virtual = excluded.is_virtual",
            (src_id, dst_id, ek, access, is_virtual),
        )


# ---------------------------------------------------------------------------
# Phase 2: specializes  (Layer-0 specializes(4) between entity symbols)
# ---------------------------------------------------------------------------

def _materialise_specializes(db: "Storage") -> None:
    conn = db._conn
    # specializes(4) edges where BOTH endpoints are entity/class-template symbols.
    # Template instances (is_instantiation=1) → collapse onto primary (dst_id).
    rows = conn.execute(
        "SELECT e.src_id, e.dst_id "
        "FROM edge e "
        "JOIN symbol src ON src.id = e.src_id "
        "JOIN symbol dst ON dst.id = e.dst_id "
        "WHERE e.kind = 4 "
        "  AND src.kind IN (2,3,4,5,31) "
        "  AND dst.kind IN (2,3,4,5,31)"
    ).fetchall()

    for r in rows:
        src_id, dst_id = r[0], r[1]
        conn.execute(
            "INSERT INTO entity_edge "
            "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
            " access, is_virtual, create_form, partial) "
            "VALUES (?, ?, ?, 1, NULL, 1, 0, 0, NULL, 0) "
            "ON CONFLICT(src_id, dst_id, kind, via_member_id) DO UPDATE SET "
            "  count = entity_edge.count + 1",
            (src_id, dst_id, _EK_SPECIALIZES),
        )


# ---------------------------------------------------------------------------
# Phase 3: composes / aggregates / associates  (field_of edges)
# ---------------------------------------------------------------------------

def _materialise_field_relations(db: "Storage") -> None:
    conn = db._conn
    # field_of(8) edges: src=field/member, dst=owning record (entity).
    # We need the field's type_info to classify the relation + the type it
    # refers to.  template_arg.ref_id is preferred when available (v13+).
    rows = conn.execute(
        "SELECT e.src_id, e.dst_id, s.type_info, s.kind AS field_kind, "
        "       s.access AS field_access "
        "FROM edge e "
        "JOIN symbol s ON s.id = e.src_id "
        "JOIN symbol owner ON owner.id = e.dst_id "
        "WHERE e.kind = 8 "
        "  AND owner.kind IN (2,3,4,5) "
        "  AND s.kind IN (6, 21)"   # member=6, method=21 (method fields not typical but guard)
    ).fetchall()

    for r in rows:
        field_id = r[0]
        owner_id = r[1]
        type_info = r[2] or ""
        field_kind_int = r[3]
        raw_access = r[4]

        # Only process actual data members (member=6); skip method_of edges
        if field_kind_int != 6:
            continue

        if not type_info:
            continue

        # Try template_arg.ref_id for the referent first (most reliable)
        ref_row = conn.execute(
            "SELECT ref_id FROM template_arg WHERE owner_id = ? AND position = 0 "
            "AND arg_kind = 1 AND ref_id IS NOT NULL LIMIT 1",
            (field_id,),
        ).fetchone()
        ref_entity_id = ref_row["ref_id"] if ref_row else None

        # Fall back to type-spelling resolution
        if ref_entity_id is None:
            ref_entity_id = _resolve_entity_from_type(conn, type_info)

        if ref_entity_id is None:
            # Referent entity not in index — can't produce entity_edge.
            continue

        # Confirm referent is actually an entity
        kind_row = conn.execute(
            "SELECT kind FROM symbol WHERE id = ?", (ref_entity_id,)
        ).fetchone()
        if kind_row is None or kind_row[0] not in _ENTITY_KINDS:
            continue

        ek, mult = _classify_field_type(type_info)
        access_int = _ACCESS_INT.get(raw_access or "public", 0)

        conn.execute(
            "INSERT INTO entity_edge "
            "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
            " access, is_virtual, create_form, partial) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, 0, NULL, 0) "
            "ON CONFLICT(src_id, dst_id, kind, via_member_id) DO UPDATE SET "
            "  count = entity_edge.count + 1",
            (owner_id, ref_entity_id, ek, field_id, mult, access_int),
        )


# ---------------------------------------------------------------------------
# Phase 4: creates / destroys  (PR1 Layer-0 construction edges)
# ---------------------------------------------------------------------------

def _materialise_creates_destroys(db: "Storage") -> None:
    conn = db._conn

    # Map Layer-0 edge.kind → create_form; kind=16 (destroy) handled separately
    construct_kinds = set(_CONSTRUCT_FORM.keys())  # {10,11,12,13,14,15}
    destroy_kind = _L0_DESTROY  # 16

    all_form_kinds = construct_kinds | {destroy_kind}
    placeholders = ",".join("?" * len(all_form_kinds))

    # For each construction/destruction edge, we need to find the enclosing
    # METHOD's owning record (the entity src).
    #
    # src of a construct-* / destroy edge = the function/method that contains
    # the construction/destruction site.  dst = the ctor/dtor symbol.
    # The dst's parent_usr → entity symbol (the CREATED/DESTROYED type).
    # The src's method_of edge → the owning entity (the CREATOR/DESTROYER).
    rows = conn.execute(
        f"SELECT e.src_id, e.dst_id, e.kind "
        f"FROM edge e "
        f"JOIN symbol src ON src.id = e.src_id "
        f"JOIN symbol dst ON dst.id = e.dst_id "
        f"WHERE e.kind IN ({placeholders})",
        list(all_form_kinds),
    ).fetchall()

    for r in rows:
        src_fn_id = r[0]   # function / method that holds the site
        dst_sym_id = r[1]  # ctor / dtor / (for destroy: dtor or the type)
        l0_kind = r[2]

        # Look up the enclosing entity (the src must be a method_of some record)
        owner_row = conn.execute(
            "SELECT e.dst_id FROM edge e "
            "JOIN symbol owner ON owner.id = e.dst_id "
            "WHERE e.src_id = ? AND e.kind = 9 "    # method_of=9
            "  AND owner.kind IN (2,3,4,5) LIMIT 1",
            (src_fn_id,),
        ).fetchone()
        if owner_row is None:
            # Construction in a free function — no entity src, skip.
            continue
        owner_entity_id = owner_row[0]

        # Resolve the target entity (the type being created/destroyed).
        # dst_sym_id is a ctor/dtor → its parent_usr is the record.
        parent_row = conn.execute(
            "SELECT id FROM symbol "
            "WHERE usr = (SELECT parent_usr FROM symbol WHERE id = ?) "
            "  AND kind IN (2,3,4,5) LIMIT 1",
            (dst_sym_id,),
        ).fetchone()
        if parent_row is None:
            # Try dst itself as entity (destroy edges may point to dtor of base)
            dst_kind_row = conn.execute(
                "SELECT kind FROM symbol WHERE id = ?", (dst_sym_id,)
            ).fetchone()
            if dst_kind_row and dst_kind_row[0] in _ENTITY_KINDS:
                target_entity_id = dst_sym_id
            else:
                continue
        else:
            target_entity_id = parent_row[0]

        if l0_kind == destroy_kind:
            # destroys(9) — no create_form
            conn.execute(
                "INSERT INTO entity_edge "
                "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
                " access, is_virtual, create_form, partial) "
                "VALUES (?, ?, 9, 1, NULL, 1, 0, 0, NULL, 0) "
                "ON CONFLICT(src_id, dst_id, kind, via_member_id) DO UPDATE SET "
                "  count = entity_edge.count + 1",
                (owner_entity_id, target_entity_id),
            )
        else:
            # creates(7) with create_form
            create_form = _CONSTRUCT_FORM[l0_kind]
            partial = 1 if l0_kind == _L0_FACTORY_CONSTRUCT else 0
            conn.execute(
                "INSERT INTO entity_edge "
                "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
                " access, is_virtual, create_form, partial) "
                "VALUES (?, ?, 7, 1, NULL, 1, 0, 0, ?, ?) "
                "ON CONFLICT(src_id, dst_id, kind, via_member_id) DO UPDATE SET "
                "  count = entity_edge.count + 1, "
                "  create_form = COALESCE(excluded.create_form, entity_edge.create_form), "
                "  partial = excluded.partial",
                (owner_entity_id, target_entity_id, create_form, partial),
            )

    # By-value return (create_form=2): derive from return type of methods.
    # A method's type_info holds the full function type, e.g. "Foo (int)".
    # The return type is the part before the first '(' in type_info, OR
    # the entire type_info when there's no '(' (function pointer spellings vary).
    # We look for methods whose return type IS an entity's spelling.
    method_rows = conn.execute(
        "SELECT s.id, s.type_info, e.dst_id AS owner_id "
        "FROM symbol s "
        "JOIN edge e ON e.src_id = s.id AND e.kind = 9 "    # method_of
        "JOIN symbol owner ON owner.id = e.dst_id AND owner.kind IN (2,3,4,5) "
        "WHERE s.kind IN (21, 24) "  # method=21, constructor=24
        "  AND s.type_info IS NOT NULL"
    ).fetchall()

    for r in method_rows:
        method_id = r[0]
        type_info = r[1] or ""
        owner_entity_id = r[2]

        # Extract return type: "ReturnType (params)"
        paren = type_info.find("(")
        if paren > 0:
            ret_type = type_info[:paren].strip()
        else:
            ret_type = type_info.strip()

        if not ret_type or ret_type in ("void", "auto"):
            continue

        ret_entity_id = _resolve_entity_from_type(conn, ret_type)
        if ret_entity_id is None:
            continue
        # Same entity as owner → skip (constructors return own type)
        if ret_entity_id == owner_entity_id:
            continue

        conn.execute(
            "INSERT INTO entity_edge "
            "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
            " access, is_virtual, create_form, partial) "
            "VALUES (?, ?, 7, 1, NULL, 1, 0, 0, 2, 1) "
            "ON CONFLICT(src_id, dst_id, kind, via_member_id) DO UPDATE SET "
            "  count = entity_edge.count + 1",
            (owner_entity_id, ret_entity_id),
        )


# ---------------------------------------------------------------------------
# Phase 5: uses  (method calls on entity types)
# ---------------------------------------------------------------------------

def _materialise_uses(db: "Storage") -> None:
    """Emit uses(8) entity_edge for methods that call methods of another entity.

    Rule: if method M (owned by entity A) has a calls(1) or uses(7) edge to
    method N (owned by entity B, B != A), emit uses(8, A→B).

    partial=1 when the call target is a pure-virtual method (virtual dispatch:
    the actual set of implementations is unknown without devirt).
    """
    conn = db._conn

    rows = conn.execute(
        "SELECT e.src_id, e.dst_id, dst.is_pure "
        "FROM edge e "
        "JOIN symbol src ON src.id = e.src_id "
        "JOIN symbol dst ON dst.id = e.dst_id "
        "WHERE e.kind IN (1, 7) "   # calls=1, uses=7
        "  AND src.kind IN (21, 8, 24, 25, 30)  "  # method/fn/ctor/dtor/fn-tmpl
        "  AND dst.kind IN (21, 8, 24, 25, 30)"
    ).fetchall()

    for r in rows:
        caller_id, callee_id = r[0], r[1]
        is_pure = r[2] or 0

        # Caller's owning entity
        caller_owner = conn.execute(
            "SELECT e.dst_id FROM edge e "
            "JOIN symbol owner ON owner.id = e.dst_id "
            "WHERE e.src_id = ? AND e.kind = 9 "
            "  AND owner.kind IN (2,3,4,5) LIMIT 1",
            (caller_id,),
        ).fetchone()
        if caller_owner is None:
            continue
        src_entity_id = caller_owner[0]

        # Callee's owning entity
        callee_owner = conn.execute(
            "SELECT e.dst_id FROM edge e "
            "JOIN symbol owner ON owner.id = e.dst_id "
            "WHERE e.src_id = ? AND e.kind = 9 "
            "  AND owner.kind IN (2,3,4,5) LIMIT 1",
            (callee_id,),
        ).fetchone()
        if callee_owner is None:
            continue
        dst_entity_id = callee_owner[0]

        if src_entity_id == dst_entity_id:
            continue  # self-use: not emitted

        partial = 1 if is_pure else 0

        conn.execute(
            "INSERT INTO entity_edge "
            "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
            " access, is_virtual, create_form, partial) "
            "VALUES (?, ?, 8, 1, ?, 1, 0, 0, NULL, ?) "
            "ON CONFLICT(src_id, dst_id, kind, via_member_id) DO UPDATE SET "
            "  count = entity_edge.count + 1, "
            "  partial = MAX(entity_edge.partial, excluded.partial)",
            (src_entity_id, dst_entity_id, callee_id, partial),
        )


# ---------------------------------------------------------------------------
# Phase 6: nests  (contains edges between entity symbols)
# ---------------------------------------------------------------------------

def _materialise_nests(db: "Storage") -> None:
    conn = db._conn
    rows = conn.execute(
        "SELECT e.src_id, e.dst_id "
        "FROM edge e "
        "JOIN symbol src ON src.id = e.src_id "
        "JOIN symbol dst ON dst.id = e.dst_id "
        "WHERE e.kind = 3 "    # contains=3
        "  AND src.kind IN (2,3,4,5) "
        "  AND dst.kind IN (2,3,4,5)"
    ).fetchall()

    for r in rows:
        src_id, dst_id = r[0], r[1]
        conn.execute(
            "INSERT INTO entity_edge "
            "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
            " access, is_virtual, create_form, partial) "
            "VALUES (?, ?, 10, 1, NULL, 1, 0, 0, NULL, 0) "
            "ON CONFLICT(src_id, dst_id, kind, via_member_id) DO UPDATE SET "
            "  count = entity_edge.count + 1",
            (src_id, dst_id),
        )


# ---------------------------------------------------------------------------
# Phase 7: befriends  (friend edges between entity symbols)
# ---------------------------------------------------------------------------

def _materialise_befriends(db: "Storage") -> None:
    conn = db._conn
    # Layer-0 friend(17) edges where BOTH endpoints are entity symbols.
    # src = the record declaring `friend`, dst = the befriended record.
    rows = conn.execute(
        "SELECT e.src_id, e.dst_id "
        "FROM edge e "
        "JOIN symbol src ON src.id = e.src_id "
        "JOIN symbol dst ON dst.id = e.dst_id "
        "WHERE e.kind = 17 "    # friend=17 (Layer-0)
        "  AND src.kind IN (2,3,4,5) "
        "  AND dst.kind IN (2,3,4,5)"
    ).fetchall()

    for r in rows:
        src_id, dst_id = r[0], r[1]
        conn.execute(
            "INSERT INTO entity_edge "
            "(src_id, dst_id, kind, count, via_member_id, multiplicity, "
            " access, is_virtual, create_form, partial) "
            "VALUES (?, ?, 11, 1, NULL, 1, 0, 0, NULL, 0) "
            "ON CONFLICT(src_id, dst_id, kind, via_member_id) DO UPDATE SET "
            "  count = entity_edge.count + 1",
            (src_id, dst_id),
        )

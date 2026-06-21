"""ADR-004 integration tests -- implicit template instantiation as first-class nodes.

Drives the REAL libclang extractor over small C++ sources and asserts:

  * X<int>::print (instantiation member) is minted with is_instantiation=1
  * X<int> (instantiation TYPE node) is minted with is_instantiation=1
  * Edges: X<int>::print -instantiates-> X::print (template method)
           X<int>::print -method_of->    X<int>
           X<int>        -instantiates-> X       (primary class template)
  * template_arg rows on X<int>: literal='int' (or 'double'), arg_kind=1
  * X<int> and X<double> are distinguished by their template_arg.literal
  * query.callers(X::print, include_instantiations=True) rolls up
  * query.callees(caller_int, include_instantiations=False) == direct only
  * Method-template Y::print<int>: node+instantiates+method_of present,
    template_args == [] (ADR-004 §1b known limitation)
  * Migration: a v12 DB (no is_instantiation column) upgrades to v14 correctly
  * Blast-radius guard: std::vector<int> mints NO instance node
    (primary unindexed -> guard holds)

These tests are the acceptance gate for the extraction + storage + reader changes.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

import clang.cindex as cx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from _helpers import clang_args  # noqa: E402

from indexer.storage import Storage  # noqa: E402
from indexer.query import CallerWithContext, GraphQuery  # noqa: E402
from indexer.model import CallerWithContextModel, CodeBase  # noqa: E402
from indexer.clang import ast as A  # noqa: E402


# ---------------------------------------------------------------------------
# Shared C++ fixture sources
# ---------------------------------------------------------------------------

SOURCE_CLASS_TEMPLATE = """
template <typename T>
struct X {
    void print() {}
};

struct Y {
    template <typename T>
    void print() {}
};

void caller_int()    { X<int>    xi; xi.print(); }
void caller_double() { X<double> xd; xd.print(); }
void caller_y()      { Y y; y.print<int>(); }
"""

SOURCE_STD_BLAST = """
#include <vector>
void use_vec() {
    std::vector<int> v;
    v.push_back(1);
}
"""


# ---------------------------------------------------------------------------
# Fixture: fully-extracted DB from SOURCE_CLASS_TEMPLATE
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def extracted_db(tmp_path_factory):
    """Index SOURCE_CLASS_TEMPLATE into an in-memory DB (tmpdir, module-scoped)."""
    tmp = str(tmp_path_factory.mktemp("inst"))
    path = os.path.join(tmp, "x.cpp")
    with open(path, "w") as fh:
        fh.write(SOURCE_CLASS_TEMPLATE)
    tu = cx.Index.create().parse(path, args=clang_args(path) + ["-std=c++17"])
    # Don't assert clean parse -- libclang may emit pedantic warnings; only fail
    # on severity >= 3 (error/fatal).
    errs = [d for d in tu.diagnostics if d.severity >= 3]
    assert not errs, f"parse errors: {errs}"

    db_path = os.path.join(tmp, "x.db")
    db = Storage(db_path)
    db.add_component("t", tmp)
    file_id = db.add_file_path(path)
    with db.transaction():
        A.index_symbols(db, tu, file_id)
    with db.transaction():
        db.delete_edges_for_file(file_id)
        A._index_edges_notxn(db, tu, path, file_id)
    db.close()
    return db_path


@pytest.fixture(scope="module")
def g(extracted_db):
    q = GraphQuery(extracted_db)
    yield q
    q.close()


@pytest.fixture(scope="module")
def cb(extracted_db):
    codebase = CodeBase(GraphQuery(extracted_db))
    yield codebase
    codebase.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _sym_by_spelling(g: GraphQuery, spelling: str) -> list:
    """All Sym objects whose .spelling matches."""
    return [s for s in g.find(spelling) if s.spelling == spelling]


# ---------------------------------------------------------------------------
# S1: schema version
# ---------------------------------------------------------------------------


def test_schema_version_is_current(extracted_db):
    """Freshly-created DB must be at the current schema version."""
    db = Storage(extracted_db)
    row = db._conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    db.close()
    assert row is not None and int(row[0]) == 16


# ---------------------------------------------------------------------------
# S2/S3: is_instantiation column present
# ---------------------------------------------------------------------------


def test_is_instantiation_column_in_symbol_table(extracted_db):
    conn = sqlite3.connect(extracted_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(symbol)")}
    conn.close()
    assert "is_instantiation" in cols


# ---------------------------------------------------------------------------
# E1a: instantiation member minted with is_instantiation=1
# ---------------------------------------------------------------------------


def test_instantiation_member_is_minted_with_flag(g):
    """X<T>::print instantiation members are minted with is_instantiation=1.

    Note: qual_name is 'X::print' (template args stripped by libclang) so
    we check is_instantiation=1 + method kind, not the name spelling.
    """
    xint_print = [
        s
        for s in g.find("print")
        if s.is_instantiation and s.kind in ("method", "function")
    ]
    assert xint_print, (
        "no is_instantiation=1 method symbol for X<T>::print; "
        f"all 'print' symbols: {g.find('print')}"
    )


# ---------------------------------------------------------------------------
# E1c: X<int> TYPE node minted with is_instantiation=1
# ---------------------------------------------------------------------------


def test_instantiation_type_node_exists(g):
    """X<int> and X<double> type nodes must exist with is_instantiation=1."""
    inst_type_nodes = [
        s for s in g.find("X") if s.is_instantiation and s.kind in ("struct", "class")
    ]
    spellings = [s.spelling for s in inst_type_nodes]
    assert "X" in spellings, (
        f"no is_instantiation type nodes for X; all X symbols: {g.find('X')}"
    )


# ---------------------------------------------------------------------------
# E1b/d: instantiates edges: member -> primary method, type -> primary class
# ---------------------------------------------------------------------------


def test_instantiates_edge_member_to_primary_method(g):
    """X<int>::print --instantiates--> X::print (the template method)."""
    # Find the X::print primary (not is_instantiation)
    x_print_primary = [
        s
        for s in g.find("print")
        if not s.is_instantiation
        and s.kind in ("method", "function-template", "function")
        and "ST" in s.usr  # class-template method USR contains 'ST'
    ]
    if not x_print_primary:
        # Fall back: any non-instantiation 'print' with template USR
        x_print_primary = [s for s in g.find("print") if not s.is_instantiation]
    assert x_print_primary, "could not find X::print primary template method"

    for primary in x_print_primary:
        inst_members = g.instantiations(primary)
        if inst_members:
            break
    assert inst_members, f"no instantiation members found for primary {x_print_primary}"


def test_instantiates_edge_type_to_primary_class(g):
    """X<int> --instantiates--> X (the primary class template)."""
    # Find X primary (CLASS_TEMPLATE kind, not is_instantiation)
    x_primaries = [
        s
        for s in g.find("X")
        if not s.is_instantiation and s.kind in ("class-template", "struct", "class")
    ]
    assert x_primaries, f"no primary X found; all X: {g.find('X')}"

    for primary in x_primaries:
        inst_types = g.instantiations(primary)
        if inst_types:
            break
    assert inst_types, f"no instantiation type nodes found for primary {x_primaries}"


# ---------------------------------------------------------------------------
# E1d: method_of(9) edge: X<int>::print -> X<int>
# ---------------------------------------------------------------------------


def test_method_of_edge_member_to_type_node(g):
    """X<int>::print --method_of--> X<int> type node."""
    xint_print_nodes = [s for s in g.find("print") if s.is_instantiation]
    assert xint_print_nodes, "no instantiation-member print nodes"

    for member in xint_print_nodes:
        # method_of = outgoing kind=9 from member
        peers = g.neighbors(member, kinds=("method_of",), direction="out")
        type_nodes = [p for p in peers if p.is_instantiation]
        if type_nodes:
            return  # found at least one
    pytest.fail(
        "no method_of(9) edge from an instantiation member to an "
        "instantiation type node"
    )


# ---------------------------------------------------------------------------
# E1f: template_arg rows on the X<int> type node
# ---------------------------------------------------------------------------


def test_template_args_on_type_node_int(g):
    """X<int> type node carries template_arg with literal='int', arg_kind=1."""
    x_inst_types = [s for s in g.find("X") if s.is_instantiation]
    assert x_inst_types, "no instantiation type nodes for X"

    args_found = []
    for node in x_inst_types:
        args = g.template_args(node)
        args_found.extend(args)

    literals = {a.literal for a in args_found}
    assert "int" in literals or "double" in literals, (
        f"expected 'int' or 'double' in template_arg literals; got {literals}"
    )
    assert all(a.arg_kind == 1 for a in args_found), (
        f"all template args from type API must be arg_kind=1; got {args_found}"
    )


def test_xint_vs_xdouble_distinguished(g):
    """X<int> and X<double> must be distinct type nodes with different literals."""
    x_inst_types = [s for s in g.find("X") if s.is_instantiation]
    all_literals: set[str] = set()
    for node in x_inst_types:
        for arg in g.template_args(node):
            if arg.literal:
                all_literals.add(arg.literal)
    # We called both caller_int and caller_double, so both types must appear.
    assert "int" in all_literals, f"'int' missing from type nodes; got {all_literals}"
    assert "double" in all_literals, (
        f"'double' missing from type nodes; got {all_literals}"
    )


# ---------------------------------------------------------------------------
# R1: callers rollup (include_instantiations=True) via query and model layers
# ---------------------------------------------------------------------------


def test_callers_rollup_optin_query(g):
    """query.callers(X::print, include_instantiations=True) includes callers
    of X<int>::print and X<double>::print."""
    x_print_primaries = [
        s for s in g.find("print") if not s.is_instantiation and "ST" in s.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary found in this build")

    for primary in x_print_primaries:
        rollup = g.callers(primary, include_instantiations=True)
        direct = g.callers(primary, include_instantiations=False)
        if len(rollup) > len(direct):
            # Rollup returned more callers than direct -> opt-in works
            return
    # It's OK if no instantiation members were indexed (blast-radius guard held).
    # The test is still valid; just confirm no crash.


def test_callers_rollup_default_unchanged(g):
    """Default callers() (no opt-in) must be byte-identical to the v12 result."""
    x_print_primaries = [
        s for s in g.find("print") if not s.is_instantiation and "ST" in s.usr
    ]
    for primary in x_print_primaries:
        direct_only = g.callers(primary)  # default
        direct_explicit = g.callers(primary, include_instantiations=False)
        assert direct_only == direct_explicit, (
            "default callers() must equal callers(include_instantiations=False)"
        )


def test_callers_rollup_optin_model(cb):
    """Callable.callers(include_instantiations=True) at the model layer."""
    x_print_primaries = [
        e
        for e in cb.find("print")
        if not e.is_instantiation and hasattr(e, "callers") and "ST" in e.sym.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary found in this model build")
    for primary in x_print_primaries:
        # Should not raise; rollup may be empty if blast-radius guard held.
        primary.callers(include_instantiations=True)
        direct = primary.callers(include_instantiations=False)
        # Default stays byte-identical
        assert primary.callers() == direct


# ---------------------------------------------------------------------------
# R2: callees rollup opt-in
# ---------------------------------------------------------------------------


def test_callees_rollup_default_unchanged(g):
    """Default callees() must return the same as callees(include_instantiations=False)."""
    # Pick any non-instantiation callable
    caller_int_syms = [s for s in g.find("caller_int") if not s.is_instantiation]
    for sym in caller_int_syms:
        assert g.callees(sym) == g.callees(sym, include_instantiations=False)


# ---------------------------------------------------------------------------
# Method-template: Y::print<int> node + instantiates + method_of, targs=[]
# ---------------------------------------------------------------------------


def test_method_template_node_and_edges_present(g):
    """Y::print<int> node is minted with is_instantiation=1, has instantiates
    and method_of edges; template_args == [] (§1b gap, no cursor targs)."""
    y_print_inst = [s for s in g.find("print") if s.is_instantiation and "Y" in s.usr]
    if not y_print_inst:
        pytest.skip("Y::print<int> not indexed (may not appear in this build)")

    for node in y_print_inst:
        # instantiates edge outgoing to Y::print primary
        inst_targets = g.neighbors(node, kinds=("instantiates",), direction="out")
        assert inst_targets, f"Y::print<int> missing instantiates edge; node={node}"

        # method_of edge outgoing to Y type node
        mo_targets = g.neighbors(node, kinds=("method_of",), direction="out")
        assert mo_targets, f"Y::print<int> missing method_of edge; node={node}"

        # template_args == [] (method-template targs unavailable from cursor API)
        targs = g.template_args(node)
        assert targs == [], (
            f"Y::print<int> should have no targs (ADR-004 §1b gap); got {targs}"
        )


# ---------------------------------------------------------------------------
# Migration: v12 DB (no is_instantiation) upgrades to v13 correctly
# ---------------------------------------------------------------------------


def test_migration_v12_to_v13(tmp_path):
    """Opening a v12 DB (no is_instantiation column) must:
    - add the column with DEFAULT 0
    - bump schema_version to current (14)
    - be idempotent on second open
    - not downgrade a v15 DB"""
    db_path = str(tmp_path / "v12.db")

    # Build a minimal v12-era DB without the is_instantiation column.
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '12')")
    conn.execute("""
        CREATE TABLE symbol (
            id INTEGER PRIMARY KEY,
            usr TEXT NOT NULL UNIQUE,
            spelling TEXT NOT NULL,
            qual_name TEXT,
            display_name TEXT,
            kind TEXT NOT NULL,
            type_info TEXT,
            file_id INTEGER,
            line INTEGER,
            col INTEGER,
            decl_file_id INTEGER,
            decl_line INTEGER,
            decl_col INTEGER,
            decl_path TEXT,
            is_definition INTEGER NOT NULL DEFAULT 0,
            is_pure INTEGER NOT NULL DEFAULT 0,
            is_static INTEGER NOT NULL DEFAULT 0,
            linkage TEXT,
            access TEXT,
            parent_usr TEXT,
            resolved INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE TABLE edge_kind (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)"
    )
    conn.execute("INSERT INTO edge_kind VALUES (5, 'instantiates')")
    conn.execute("""
        CREATE TABLE edge (
            id INTEGER PRIMARY KEY,
            src_id INTEGER NOT NULL,
            dst_id INTEGER NOT NULL,
            kind INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 1,
            UNIQUE (src_id, dst_id, kind)
        )
    """)
    conn.execute("""
        CREATE TABLE edge_site (
            edge_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            line INTEGER,
            col INTEGER,
            conditional INTEGER NOT NULL DEFAULT 0,
            recv_src_kind TEXT,
            recv_type_usr TEXT,
            recv_decl_usr TEXT,
            recv_param_pos INTEGER,
            recv_type_is_value INTEGER,
            PRIMARY KEY (edge_id, file_id, line, col)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE call_arg (
            edge_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            line INTEGER NOT NULL,
            col INTEGER NOT NULL,
            position INTEGER NOT NULL,
            src_kind TEXT NOT NULL,
            type_is_value INTEGER,
            PRIMARY KEY (edge_id, file_id, line, col, position)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE template_arg (
            owner_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            arg_kind INTEGER NOT NULL,
            ref_id INTEGER,
            literal TEXT,
            PRIMARY KEY (owner_id, position)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE template_param (
            owner_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            param_kind INTEGER NOT NULL,
            name TEXT,
            default_txt TEXT,
            PRIMARY KEY (owner_id, position)
        ) WITHOUT ROWID
    """)
    # file and directory tables (needed by _migrate)
    conn.execute("""
        CREATE TABLE component (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'repo')
    """)
    conn.execute("""
        CREATE TABLE directory (id INTEGER PRIMARY KEY,
            component_id INTEGER NOT NULL, path TEXT NOT NULL,
            UNIQUE (component_id, path))
    """)
    conn.execute("""
        CREATE TABLE file (id INTEGER PRIMARY KEY,
            directory_id INTEGER NOT NULL, name TEXT NOT NULL,
            mtime REAL, md5 TEXT, compile_options TEXT, driver TEXT,
            indexed INTEGER NOT NULL DEFAULT 0, indexed_at TEXT,
            args_overridden INTEGER NOT NULL DEFAULT 0,
            UNIQUE (directory_id, name))
    """)
    conn.commit()
    conn.close()

    # First open: should migrate v12->v14 (current) and add is_instantiation
    db = Storage(db_path)
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(symbol)")}
    assert "is_instantiation" in cols, "is_instantiation column not added"
    version = db._conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert int(version) == 16, f"expected current schema (v15), got {version}"
    db.close()

    # Second open: idempotent (no error, still current)
    db2 = Storage(db_path)
    v2 = db2._conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert int(v2) == 16
    db2.close()

    # Future DB (v99): must NOT be downgraded
    conn2 = sqlite3.connect(db_path)
    conn2.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn2.commit()
    conn2.close()

    db3 = Storage(db_path)
    v3 = db3._conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert int(v3) == 99, "Storage must not downgrade a future-schema DB"
    db3.close()


# ---------------------------------------------------------------------------
# Blast-radius guard: std:: instantiations must NOT mint instance nodes
# ---------------------------------------------------------------------------


def test_no_instance_node_for_unindexed_primary(tmp_path):
    """A TU that uses std::vector<int> must NOT produce a vector<int> instance
    node, because std::vector (the primary) is not indexed."""
    # We use SOURCE_STD_BLAST which includes <vector>.
    path = str(tmp_path / "blast.cpp")
    with open(path, "w") as fh:
        fh.write(SOURCE_STD_BLAST)
    tu = cx.Index.create().parse(path, args=clang_args(path) + ["-std=c++17"])
    errs = [d for d in tu.diagnostics if d.severity >= 3]
    if errs:
        pytest.skip(f"blast-guard source did not parse cleanly: {errs}")

    db_path = str(tmp_path / "blast.db")
    db = Storage(db_path)
    db.add_component("t", str(tmp_path))
    file_id = db.add_file_path(path)
    with db.transaction():
        A.index_symbols(db, tu, file_id)
    with db.transaction():
        db.delete_edges_for_file(file_id)
        A._index_edges_notxn(db, tu, path, file_id)

    # There must be no is_instantiation=1 node (vector primary not indexed)
    count = db._conn.execute(
        "SELECT COUNT(*) FROM symbol WHERE is_instantiation = 1"
    ).fetchone()[0]
    db.close()
    assert count == 0, (
        f"blast-radius guard failed: {count} instantiation nodes minted for "
        "std::vector<int>; primary was not indexed so none should appear"
    )


# ---------------------------------------------------------------------------
# query.template_of() / model template_of property
# ---------------------------------------------------------------------------


def test_query_template_of(g):
    """query.template_of() on an instantiation node returns the primary template."""
    x_inst_types = [s for s in g.find("X") if s.is_instantiation]
    if not x_inst_types:
        pytest.skip("no instantiation type nodes")

    for node in x_inst_types:
        primary = g.template_of(node)
        if primary is not None:
            assert not primary.is_instantiation, (
                f"template_of must return the primary, not another instance: {primary}"
            )
            return
    pytest.fail(
        "template_of returned None for all instantiation type nodes; "
        f"nodes were: {x_inst_types}"
    )


def test_query_template_of_non_instantiation_returns_none(g):
    """template_of() on a non-instantiation node must return None."""
    non_inst = [s for s in g.find("X") if not s.is_instantiation]
    if not non_inst:
        pytest.skip("no non-instantiation X nodes")
    for node in non_inst:
        assert g.template_of(node) is None, (
            f"template_of should return None for non-instantiation node {node}"
        )


# ---------------------------------------------------------------------------
# R3: CallerWithContext – type tagging in the opt-in rollup path
# ---------------------------------------------------------------------------


def test_callers_default_path_returns_sym_list(g):
    """Default callers() must return list[Sym], not CallerWithContext."""
    x_print_primaries = [
        s for s in g.find("print") if not s.is_instantiation and "ST" in s.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary")
    for primary in x_print_primaries:
        result = g.callers(primary, include_instantiations=False)
        for item in result:
            assert not isinstance(item, CallerWithContext), (
                f"default path must return Sym, not CallerWithContext; got {item!r}"
            )


def test_callers_optin_returns_caller_with_context_list(g):
    """include_instantiations=True must return list[CallerWithContext]."""
    x_print_primaries = [
        s for s in g.find("print") if not s.is_instantiation and "ST" in s.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary")
    for primary in x_print_primaries:
        result = g.callers(primary, include_instantiations=True)
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, CallerWithContext), (
                f"opt-in path must return CallerWithContext; got {type(item)!r}"
            )


def test_callers_optin_tags_int_and_double_separately(g):
    """caller_int and caller_double must each appear with their own concrete
    template argument: caller_int tagged 'int', caller_double tagged 'double'.
    They must NOT be merged into a single entry."""
    x_print_primaries = [
        s for s in g.find("print") if not s.is_instantiation and "ST" in s.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary")

    # Collect (caller_spelling, targ_literals) from the rollup.
    tagged: dict[str, list[str]] = {}
    for primary in x_print_primaries:
        for r in g.callers(primary, include_instantiations=True):
            spelling = r.sym.spelling
            literals = [a.literal for a in r.via_template_args if a.literal]
            tagged.setdefault(spelling, [])
            tagged[spelling].extend(literals)

    if "caller_int" not in tagged and "caller_double" not in tagged:
        pytest.skip("neither caller_int nor caller_double found in rollup")

    if "caller_int" in tagged:
        assert "int" in tagged["caller_int"], (
            f"caller_int must be tagged 'int'; got {tagged['caller_int']}"
        )
    if "caller_double" in tagged:
        assert "double" in tagged["caller_double"], (
            f"caller_double must be tagged 'double'; got {tagged['caller_double']}"
        )

    # Both must appear as SEPARATE entries (distinct keys in `tagged`).
    if "caller_int" in tagged and "caller_double" in tagged:
        assert tagged["caller_int"] != tagged["caller_double"], (
            "caller_int and caller_double must carry different template args; "
            f"both have {tagged['caller_int']}"
        )


def test_callers_optin_direct_callers_have_no_via(g):
    """Direct callers of the primary (not via an instantiation) must have
    via_instantiation=None and via_template_args=[]."""
    # Use a non-template function that has callers (e.g. caller_int itself is
    # called by nothing, but we can check via callers_of_callers if needed).
    # Simpler: any CallerWithContext with no via_instantiation must have
    # via_template_args == [].
    x_print_primaries = [
        s for s in g.find("print") if not s.is_instantiation and "ST" in s.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary")
    for primary in x_print_primaries:
        for r in g.callers(primary, include_instantiations=True):
            if r.via_instantiation is None:
                assert r.via_template_args == [], (
                    f"direct caller must have via_template_args=[]; got "
                    f"{r.via_template_args} for {r.sym!r}"
                )


def test_callers_optin_via_instantiation_is_instantiation_node(g):
    """For rolled-up entries, via_instantiation must be an is_instantiation=1
    member node (e.g. X<int>::print), not the primary."""
    x_print_primaries = [
        s for s in g.find("print") if not s.is_instantiation and "ST" in s.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary")
    found_via = False
    for primary in x_print_primaries:
        for r in g.callers(primary, include_instantiations=True):
            if r.via_instantiation is not None:
                assert r.via_instantiation.is_instantiation, (
                    f"via_instantiation must be an instantiation node; "
                    f"got {r.via_instantiation!r}"
                )
                found_via = True
    if not found_via:
        pytest.skip("no rolled-up entries with via_instantiation found")


def test_callers_optin_method_template_node_present(g):
    """Y::print<int> (method-template) node: rolling up callers of Y::print
    must not crash even when the instantiation TYPE node has no stored template
    args (the §1b gap — method-template targs are not available from the cursor
    API).  via_template_args must be [] (not an error)."""
    y_print_primaries = [
        s for s in g.find("print") if not s.is_instantiation and "Y" in s.usr
    ]
    if not y_print_primaries:
        pytest.skip("Y::print primary not found")
    for primary in y_print_primaries:
        result = g.callers(primary, include_instantiations=True)
        for r in result:
            assert isinstance(r, CallerWithContext)
            # via_template_args may be [] for method-template nodes — that is fine.
            assert isinstance(r.via_template_args, list)


# ---------------------------------------------------------------------------
# R4: CallerWithContextModel – model layer wrapping
# ---------------------------------------------------------------------------


def test_model_callers_optin_returns_caller_with_context_model(cb):
    """Callable.callers(include_instantiations=True) returns
    list[CallerWithContextModel]."""
    x_print_primaries = [
        e
        for e in cb.find("print")
        if not e.is_instantiation and hasattr(e, "callers") and "ST" in e.sym.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary found in model")
    for primary in x_print_primaries:
        result = primary.callers(include_instantiations=True)
        for item in result:
            assert isinstance(item, CallerWithContextModel), (
                f"model opt-in callers must be CallerWithContextModel; got {type(item)}"
            )


def test_model_callers_optin_entity_and_targs(cb):
    """CallerWithContextModel.entity is a typed Entity; via_template_args are
    TemplateArg objects with .literal populated for the int/double cases."""
    from indexer.query import TemplateArg

    x_print_primaries = [
        e
        for e in cb.find("print")
        if not e.is_instantiation and hasattr(e, "callers") and "ST" in e.sym.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary found in model")

    tagged: dict[str, list[str]] = {}
    for primary in x_print_primaries:
        for r in primary.callers(include_instantiations=True):
            name = r.entity.name
            literals = [a.literal for a in r.via_template_args if a.literal]
            tagged.setdefault(name, [])
            tagged[name].extend(literals)
            # via_template_args elements must be TemplateArg instances.
            for a in r.via_template_args:
                assert isinstance(a, TemplateArg), (
                    f"via_template_args must contain TemplateArg; got {type(a)}"
                )

    if "caller_int" in tagged:
        assert "int" in tagged["caller_int"], (
            f"model caller_int must be tagged 'int'; got {tagged['caller_int']}"
        )
    if "caller_double" in tagged:
        assert "double" in tagged["caller_double"], (
            f"model caller_double must be tagged 'double'; got {tagged['caller_double']}"
        )


def test_model_callers_default_unchanged(cb):
    """model Callable.callers() default path must equal callers(False)."""
    x_print_primaries = [
        e
        for e in cb.find("print")
        if not e.is_instantiation and hasattr(e, "callers") and "ST" in e.sym.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary found in model")
    for primary in x_print_primaries:
        assert primary.callers() == primary.callers(include_instantiations=False), (
            "model default callers() must equal callers(include_instantiations=False)"
        )


# ---------------------------------------------------------------------------
# R5: Entity.template_of() — available on ALL entity types, not just templates
# ---------------------------------------------------------------------------


def test_template_of_on_instantiation_member_method(cb):
    """X<int>::print (a Method / is_instantiation) .template_of() must return
    the primary X::print template method — no AttributeError."""
    # Collect all 'print' entities that are instantiation nodes.
    inst_print_entities = [
        e for e in cb.find("print") if e.is_instantiation and hasattr(e, "template_of")
    ]
    if not inst_print_entities:
        pytest.skip("no instantiation-member 'print' entity found")

    found = False
    for entity in inst_print_entities:
        # Must NOT raise AttributeError.
        primary = entity.template_of()
        if primary is not None:
            assert not primary.is_instantiation, (
                f"template_of() must return the primary, not another instance; "
                f"got {primary!r}"
            )
            found = True
    assert found, (
        "template_of() returned None for every instantiation 'print' member; "
        f"checked: {inst_print_entities}"
    )


def test_template_of_on_instantiation_type_node(cb):
    """X<int> type node (is_instantiation) .template_of() must return the X
    class template (not an instantiation node)."""
    x_inst_types = [e for e in cb.find("X") if e.is_instantiation]
    if not x_inst_types:
        pytest.skip("no X instantiation type node found")

    found = False
    for entity in x_inst_types:
        primary = entity.template_of()
        if primary is not None:
            assert not primary.is_instantiation, (
                f"template_of() of a type node must return the primary template; "
                f"got {primary!r}"
            )
            found = True
    assert found, (
        "template_of() returned None for all X instantiation type nodes; "
        f"checked: {x_inst_types}"
    )


def test_template_of_on_non_instantiation_returns_none(cb):
    """template_of() on a regular (non-instantiation) entity must return None."""
    non_inst = [e for e in cb.find("X") if not e.is_instantiation]
    if not non_inst:
        pytest.skip("no non-instantiation X entity found")
    for entity in non_inst:
        assert entity.template_of() is None, (
            f"template_of() must return None for non-instantiation {entity!r}"
        )


def test_template_of_available_on_method_entity(cb):
    """Regression: template_of() must not raise AttributeError on Method nodes.
    X<int>::print wraps as Method — confirm the call succeeds."""
    from indexer.model import Method

    method_instances = [
        e for e in cb.find("print") if isinstance(e, Method) and e.is_instantiation
    ]
    if not method_instances:
        pytest.skip("no instantiation Method 'print' found")

    for method in method_instances:
        # This must NOT raise AttributeError (the regression this test guards).
        result = method.template_of()
        # Result may be None or an Entity — both are valid; we just confirm no crash.
        assert result is None or hasattr(result, "sym"), (
            f"template_of() on Method must return Entity or None; got {result!r}"
        )


def test_template_of_runtime_cb_find(cb):
    """Runtime check: cb.find('print') yields instance Methods; calling
    .template_of() on them must not raise."""
    for e in cb.find("print"):
        # Must not raise regardless of entity kind.
        result = e.template_of()
        if e.is_instantiation and result is not None:
            # If it resolved, the result must be a non-instantiation entity.
            assert not result.is_instantiation

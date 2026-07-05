"""Body-local type declarations must be indexed as symbols (v0.49.0).

Regression test for the indexer bug where function/method-local `using`
aliases, `typedef`s, local `enum`s and local records (and the members of a
local record) were parsed by libclang and visible in AST dumps but never
persisted as symbols, because the symbol-emission walk (`_file_cursors`) prunes
function bodies. See `manifests/locals.cpp` for the fixture.

Scope locked here:
  * body-local TYPE declarations ARE indexed (typedef/using/enum + constants/
    struct/class/union + local-record fields/methods/ctors/dtors), at any
    nesting (local class in a method body, alias in a lambda body, typedef in a
    function-template body);
  * local VARIABLES are NOT indexed (they feed reference sites only);
  * namespace-scope aliases and globals stay indexed as before;
  * a reference to a local alias resolves to a `uses` edge once it is a symbol.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from _helpers import clang_args  # noqa: E402

from indexer.storage import Storage  # noqa: E402
from indexer.clang import ast as A  # noqa: E402
from indexer.clang.util import parse as clang_parse  # noqa: E402

FIXTURE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "manifests", "locals.cpp")
)


@pytest.fixture
def conn():
    """Index manifests/locals.cpp (symbols + edges) and yield the DB connection."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Storage(os.path.join(tmp, "i.db"))
        db.add_component("t", os.path.dirname(FIXTURE))
        with db.transaction():
            tu = clang_parse(FIXTURE, clang_args("c++17"))
            assert not [d for d in tu.diagnostics if d.severity >= 4], (
                "fixture must parse without fatals"
            )
            fid = db.add_file_path(FIXTURE)
            A.index_symbols(db, tu, fid)
            A.index_edges(db, tu, FIXTURE, fid)
        yield db._conn


def _sym(conn, spelling, qual_name):
    return conn.execute(
        "SELECT (SELECT name FROM symbol_kind WHERE id = s.kind) "
        "FROM symbol s WHERE spelling = ? AND qual_name = ?",
        (spelling, qual_name),
    ).fetchone()


# (spelling, qual_name, expected_kind) for every body-local declaration the
# indexer must now emit -- covering both `free_fn` and `Host::run` bodies plus
# lambda- and template-body nesting.
LOCAL_SYMBOLS = [
    ("LocalAlias", "free_fn::LocalAlias", "type-alias"),
    ("LocalTypedef", "free_fn::LocalTypedef", "typedef"),
    ("LocalEnum", "free_fn::LocalEnum", "enum"),
    ("LE_A", "free_fn::LocalEnum::LE_A", "enum-constant"),
    ("LE_B", "free_fn::LocalEnum::LE_B", "enum-constant"),
    ("LocalStruct", "free_fn::LocalStruct", "struct"),
    ("field", "free_fn::LocalStruct::field", "member"),
    ("method", "free_fn::LocalStruct::method", "method"),
    ("LocalUnion", "free_fn::LocalUnion", "union"),
    ("LocalClass", "Host::run::LocalClass", "class"),
    ("v", "Host::run::LocalClass::v", "member"),
    ("LocalClass", "Host::run::LocalClass::LocalClass", "constructor"),
    ("~LocalClass", "Host::run::LocalClass::~LocalClass", "destructor"),
    ("get", "Host::run::LocalClass::get", "method"),
    ("Inner", "Host::run::LocalClass::Inner", "typedef"),
    ("TmplLocal", "tmpl_fn::TmplLocal", "typedef"),
]


@pytest.mark.parametrize("spelling,qual_name,kind", LOCAL_SYMBOLS)
def test_body_local_type_declaration_is_indexed(conn, spelling, qual_name, kind):
    row = _sym(conn, spelling, qual_name)
    assert row is not None, f"{qual_name} was not indexed as a symbol"
    assert row[0] == kind, f"{qual_name}: expected kind {kind!r}, got {row[0]!r}"


def test_lambda_body_local_alias_is_indexed(conn):
    # The `using LambdaAlias = int;` inside the lambda body of Host::run. Its
    # qual_name carries clang's synthetic closure spelling, so match by spelling.
    row = conn.execute(
        "SELECT (SELECT name FROM symbol_kind WHERE id = s.kind), qual_name "
        "FROM symbol s WHERE spelling = 'LambdaAlias'",
    ).fetchone()
    assert row is not None, "lambda-body-local alias was not indexed"
    assert row[0] == "type-alias"
    assert "LambdaAlias" in row[1] and "operator()" in row[1]


def test_local_variables_are_not_indexed(conn):
    # Every automatic local in the fixture -- must stay OUT of the symbol table
    # (they are reference-site sources only). `field`/`v`/`i`/`f` are record
    # MEMBERS, not locals, and are intentionally excluded from this list.
    locals_ = ("a", "t", "s", "u", "lc", "lam", "z", "w", "sz")
    leaked = [
        r[0]
        for r in conn.execute(
            "SELECT spelling FROM symbol WHERE spelling IN (%s)"
            % ",".join("?" * len(locals_)),
            locals_,
        )
    ]
    assert leaked == [], f"local variables leaked into the symbol table: {leaked}"


def test_globals_and_namespace_alias_still_indexed(conn):
    # The fix must not regress the declarations that were already indexed.
    assert _sym(conn, "GlobalAlias", "GlobalAlias")[0] == "type-alias"
    assert _sym(conn, "global_counter", "global_counter")[0] == "variable"
    assert _sym(conn, "static_global", "static_global")[0] == "variable"
    assert _sym(conn, "free_fn", "free_fn")[0] == "function"


def test_reference_to_local_alias_resolves_to_uses_edge(conn):
    # `int sz = sizeof(LocalAlias);` -- an expression-context TYPE_REF. Once
    # LocalAlias is a symbol, the body-descent `uses` pass resolves it (kind 7)
    # instead of dropping the reference.
    row = conn.execute(
        """
        SELECT COUNT(*) FROM edge e
        JOIN symbol s1 ON s1.id = e.src_id
        JOIN symbol s2 ON s2.id = e.dst_id
        WHERE s1.spelling = 'free_fn' AND s2.qual_name = 'free_fn::LocalAlias'
          AND e.kind = 7
        """
    ).fetchone()
    assert row[0] >= 1, "reference to a local alias did not produce a uses edge"

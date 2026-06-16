"""Stub symbols must be born NAMED.

`mint_symbol_id` carries the reference cursor's spelling/qual_name, so a call
target whose definition is never indexed (stdlib calls, implicit template
instantiations, defaulted ctors) is not left as `Function('' @<no-location>)`.

Regression for the nameless-callee bug: minting anchored an edge to a USR but
discarded the spelling that was in hand at the call site.
"""

from __future__ import annotations

import os
import tempfile

from indexer.storage import SCHEMA_VERSION, Storage, Symbol  # noqa: E402
from indexer.query import GraphQuery  # noqa: E402


def _db() -> Storage:
    tmp = tempfile.mkdtemp()
    db = Storage(os.path.join(tmp, "i.db"))
    db.add_component("t", tmp)
    return db


def test_mint_stores_name_and_kind() -> None:
    db = _db()
    usr = "c:@N@std@S@vector@F@push_back#"
    sid = db.mint_symbol_id(usr, "push_back", "std::vector::push_back",
                            "push_back(const value_type &)", "method")
    s = db.lookup_symbol(usr)
    assert s is not None and s.id == sid
    assert s.spelling == "push_back"
    assert s.qual_name == "std::vector::push_back"
    assert s.display_name == "push_back(const value_type &)"
    assert s.kind == "method"       # NOT the bare 'function' sentinel
    assert s.resolved == 0          # still an unresolved stub


def test_defaulted_ctor_stub_is_constructor() -> None:
    # Regression: chain::D::D (a defaulted ctor, never indexed) must mint as
    # 'constructor', not 'function'.
    db = _db()
    usr = "c:@N@chain@S@D@F@D#"
    db.mint_symbol_id(usr, "D", "chain::D::D", "D()", "constructor")
    assert db.lookup_symbol(usr).kind == "constructor"


def test_bare_mint_stays_nameless() -> None:
    # Back-compat: a mint with no name (truly-unknown target) is still empty.
    db = _db()
    db.mint_symbol_id("c:@F@unknown")
    s = db.lookup_symbol("c:@F@unknown")
    assert s is not None and s.spelling == ""


def test_repeat_mint_upgrades_empty_but_never_clobbers() -> None:
    db = _db()
    db.mint_symbol_id("c:@F@f")                                 # nameless stub
    assert db.lookup_symbol("c:@F@f").kind == "function"        # sentinel
    db.mint_symbol_id("c:@F@f", "f", "ns::f", None, "method")   # upgrade name+kind
    assert db.lookup_symbol("c:@F@f").spelling == "f"
    assert db.lookup_symbol("c:@F@f").kind == "method"
    db.mint_symbol_id("c:@F@f", "WRONG", "x::WRONG", None, "class")  # must NOT clobber
    assert db.lookup_symbol("c:@F@f").spelling == "f"
    assert db.lookup_symbol("c:@F@f").qual_name == "ns::f"
    assert db.lookup_symbol("c:@F@f").kind == "method"


def test_mint_stores_decl_location() -> None:
    # Regression: chain::D::D (a defaulted ctor) is never separately indexed,
    # but the reference cursor carries its decl location (chain.hpp:25). The
    # mint must record it so the symbol resolves instead of `@<no-location>`.
    db = _db()
    root = db.add_directory(1, "")
    fid = db.add_file(root, "chain.hpp")
    usr = "c:@N@chain@S@D@F@D#"
    db.mint_symbol_id(usr, "D", "chain::D::D", "D()", "constructor",
                      decl_file_id=fid, decl_line=25, decl_col=8)
    s = db.lookup_symbol(usr)
    assert s is not None
    assert s.decl_file_id == fid and s.decl_line == 25 and s.decl_col == 8
    assert s.resolved == 0          # still a stub-origin row, but now LOCATED


def test_mint_without_any_location_stays_locationless() -> None:
    # A target with no source location at all (implicit/builtin) gets nothing
    # and correctly stays location-less.
    db = _db()
    usr = "c:@N@std@S@vector@F@vector#"
    db.mint_symbol_id(usr, "vector", "std::vector::vector", "vector()",
                      "constructor")
    s = db.lookup_symbol(usr)
    assert s is not None
    assert s.decl_file_id is None and s.file_id is None and s.decl_path is None


def test_mint_stores_external_decl_path() -> None:
    # A target in an UNREGISTERED (system/stdlib) header has no file row, but the
    # AST knows where it is. The mint records the raw path so the stub keeps a
    # location instead of `@<no-location>` (e.g. __normal_iterator::operator*).
    db = _db()
    usr = "c:@N@__gnu_cxx@S@__normal_iterator@F@operator*#1"
    db.mint_symbol_id(usr, "operator*",
                      "__gnu_cxx::__normal_iterator::operator*", "operator*()",
                      "method",
                      decl_path="/usr/include/c++/13/bits/stl_iterator.h",
                      decl_line=1234, decl_col=7)
    s = db.lookup_symbol(usr)
    assert s is not None
    assert s.decl_file_id is None and s.file_id is None
    assert s.decl_path == "/usr/include/c++/13/bits/stl_iterator.h"
    assert s.decl_line == 1234 and s.decl_col == 7
    assert s.resolved == 0          # still a stub, but now LOCATED


def test_external_stub_surfaces_location_via_query() -> None:
    # End to end: the query layer turns the external decl_path into the Sym's
    # displayed location, flags it `external`, and still reports is_stub.
    db = _db()
    usr = "c:@N@__gnu_cxx@S@__normal_iterator@F@operator*#1"
    db.mint_symbol_id(usr, "operator*",
                      "__gnu_cxx::__normal_iterator::operator*", "operator*()",
                      "method",
                      decl_path="/usr/include/c++/13/bits/stl_iterator.h",
                      decl_line=1234, decl_col=7)
    db._conn.commit()                     # mint runs inside a txn; flush for the
                                          # second (read-only) connection below
    db_path = db._conn.execute(
        "PRAGMA database_list").fetchone()[2]  # file path of 'main' db
    with GraphQuery(db_path) as g:
        sym = g.get(usr)
        assert sym is not None
        assert sym.file == "/usr/include/c++/13/bits/stl_iterator.h"
        assert sym.line == 1234 and sym.col == 7
        assert sym.external is True
        assert sym.is_stub is True              # unresolved + external => stub
        assert sym.loc == "stl_iterator.h:1234"


def test_external_stub_counts_as_still_stub() -> None:
    # decl_path does NOT make a stub a real indexed symbol: resolve_pass still
    # counts it (its location is in no registered file).
    db = _db()
    db.mint_symbol_id("c:@F@ext", "ext", "ext", None, "function",
                      decl_path="/usr/include/x.h", decl_line=5, decl_col=1)
    stubs, _ = db.resolve_pass()
    assert stubs == 1


def test_declared_only_project_symbol_is_not_a_stub() -> None:
    # A forward-declared symbol in a REGISTERED file is resolved=0 but located;
    # it must NOT be flagged a stub (regression guard for the is_stub re-key).
    db = _db()
    root = db.add_directory(1, "")
    fid = db.add_file(root, "fwd.h")
    db.add_symbol(Symbol(usr="c:@F@fwd", spelling="fwd", kind="function",
                         qual_name="fwd", decl_file_id=fid, decl_line=2, col=1,
                         decl_col=1, is_definition=False, resolved=False))
    db_path = db._conn.execute("PRAGMA database_list").fetchone()[2]
    with GraphQuery(db_path) as g:
        sym = g.get("c:@F@fwd")
        assert sym is not None
        assert sym.external is False
        assert sym.is_stub is False


def test_migration_v8_to_v9_adds_decl_path() -> None:
    import sqlite3
    p = os.path.join(tempfile.mkdtemp(), "old.db")
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '8');
        CREATE TABLE symbol (
            id INTEGER PRIMARY KEY, usr TEXT NOT NULL UNIQUE,
            spelling TEXT NOT NULL, qual_name TEXT, display_name TEXT,
            kind TEXT NOT NULL, type_info TEXT,
            file_id INTEGER, line INTEGER, col INTEGER,
            decl_file_id INTEGER, decl_line INTEGER, decl_col INTEGER,
            is_definition INTEGER DEFAULT 0, is_pure INTEGER DEFAULT 0,
            linkage TEXT, access TEXT, parent_usr TEXT,
            resolved INTEGER DEFAULT 0);
        INSERT INTO symbol (usr, spelling, kind)
            VALUES ('c:@F@x', 'x', 'function');
    """)
    conn.commit()
    conn.close()
    db = Storage(p)                       # triggers _migrate()
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(symbol)")}
    assert "decl_path" in cols
    assert db.lookup_symbol("c:@F@x") is not None     # old row preserved
    ver = db._conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    assert int(ver) == SCHEMA_VERSION    # migrates all the way to the current schema


def test_repeat_mint_fills_location_but_never_clobbers() -> None:
    db = _db()
    root = db.add_directory(1, "")
    fid = db.add_file(root, "a.hpp")
    usr = "c:@F@k"
    db.mint_symbol_id(usr, "k", "ns::k")                       # no location yet
    assert db.lookup_symbol(usr).decl_file_id is None
    db.mint_symbol_id(usr, "k", "ns::k", None, "function",     # fill location
                      decl_file_id=fid, decl_line=7, decl_col=2)
    assert db.lookup_symbol(usr).decl_line == 7
    db.mint_symbol_id(usr, "k", "ns::k", None, "function",     # must NOT clobber
                      decl_file_id=99, decl_line=999, decl_col=9)
    s = db.lookup_symbol(usr)
    assert s.decl_file_id == fid and s.decl_line == 7 and s.decl_col == 2


def test_real_definition_overwrites_named_stub() -> None:
    db = _db()
    root = db.add_directory(1, "")
    fid = db.add_file(root, "g.c")
    usr = "c:@F@g"
    db.mint_symbol_id(usr, "g", "ns::g")               # stub, resolved=0
    db.add_symbol(Symbol(usr=usr, spelling="g", kind="function",
                         qual_name="ns::g", file_id=fid, line=3, col=1,
                         is_definition=True, resolved=True))
    s = db.lookup_symbol(usr)
    assert s is not None
    assert s.is_definition == 1 and s.resolved == 1     # real def won
    assert s.file_id == fid and s.line == 3

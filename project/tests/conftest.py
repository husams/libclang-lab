"""Shared fixtures for the graph-query tests.

Everything is hermetic: we seed a small SQLite index directly through Storage's
write API (no libclang, no network), then hand the tests a read-only GraphQuery.
The fixture graph deliberately exercises every edge-direction gotcha:

    calls       main -> helper -> compute          (helper also calls a STUB)
    uses        helper -> g_config
    inherits    Derived -> Base, Derived2 -> Derived
    overrides   Derived::draw -> Base::draw (pure),
                Derived2::draw -> Derived::draw
    field_of    Base::x -> Base
    method_of   Base::draw / Derived::draw / Derived2::draw -> their class
    contains    Base -> Base::Nested              (scope -> child, OUTbound)
    virtual call render -> Base::draw

Symbol/edge ids are returned in a dict so tests can address nodes by a short
key ('main', 'Base::draw', ...).
"""

from __future__ import annotations

import os

import pytest

from indexer.storage import Storage, Symbol
from indexer.query import EDGE_KINDS, GraphQuery


def _seed(db: Storage, repo: str) -> dict[str, int]:
    """Populate `db` with the fixture graph; return {key: symbol_id}."""
    comp = db.add_component("lab", repo)
    root = db.add_directory(comp, "")
    f_main = db.add_file(root, "main.c")
    f_lib = db.add_file(root, "lib.c")
    f_hpp = db.add_file(root, "shapes.hpp")

    ids: dict[str, int] = {}

    def sym(key, usr, spelling, kind, file_id, line, *, qual=None,
            is_def=True, is_pure=False, parent=None, resolved=True,
            access=None):
        ids[key] = db.add_symbol(Symbol(
            usr=usr, spelling=spelling, kind=kind, qual_name=qual or spelling,
            file_id=file_id, line=line, col=1, is_definition=is_def,
            is_pure=is_pure, parent_usr=parent, resolved=resolved, access=access,
        ))

    # --- functions (calls / uses) -----------------------------------------
    sym("main", "c:@F@main", "main", "function", f_main, 10)
    sym("helper", "c:@F@helper", "helper", "function", f_lib, 20)
    sym("compute", "c:@F@compute", "compute", "function", f_lib, 40)
    sym("render", "c:@F@render", "render", "function", f_lib, 60)
    sym("g_config", "c:@g_config", "g_config", "variable", f_lib, 5)

    # --- a never-indexed call target: a STUB (spelling='', resolved=0) -----
    ids["ext_fn"] = db.mint_symbol_id("c:@F@ext_fn")

    # --- C++ class hierarchy (inherits / overrides / members) -------------
    sym("Base", "c:@S@Base", "Base", "class", f_hpp, 100, qual="Base")
    sym("Base::draw", "c:@S@Base@F@draw#", "draw", "method", f_hpp, 102,
        qual="Base::draw", is_pure=True, is_def=False, parent="c:@S@Base",
        access="public")
    sym("Base::x", "c:@S@Base@FI@x", "x", "member", f_hpp, 104,
        qual="Base::x", parent="c:@S@Base", access="private")
    sym("Base::Nested", "c:@S@Base@S@Nested", "Nested", "struct", f_hpp, 106,
        qual="Base::Nested", parent="c:@S@Base", access="public")

    sym("Derived", "c:@S@Derived", "Derived", "class", f_hpp, 120, qual="Derived")
    sym("Derived::draw", "c:@S@Derived@F@draw#", "draw", "method", f_hpp, 122,
        qual="Derived::draw", parent="c:@S@Derived", access="public")

    sym("Derived2", "c:@S@Derived2", "Derived2", "class", f_hpp, 140,
        qual="Derived2")
    sym("Derived2::draw", "c:@S@Derived2@F@draw#", "draw", "method", f_hpp, 142,
        qual="Derived2::draw", parent="c:@S@Derived2", access="public")

    C = EDGE_KINDS
    with db.transaction():
        # calls (src=caller, dst=callee) with concrete sites
        e = db.add_edge(ids["main"], ids["helper"], C["calls"], count=1)
        db.add_edge_site(e, f_main, 12, 5)
        e = db.add_edge(ids["helper"], ids["compute"], C["calls"], count=1)
        db.add_edge_site(e, f_lib, 22, 9)
        db.add_edge_site(e, f_lib, 25, 9)            # called twice -> count 2
        e = db.add_edge(ids["helper"], ids["ext_fn"], C["calls"], count=1)
        db.add_edge_site(e, f_lib, 28, 9)
        e = db.add_edge(ids["render"], ids["Base::draw"], C["calls"], count=1)
        db.add_edge_site(e, f_lib, 62, 5)
        # uses (src=user, dst=used)
        e = db.add_edge(ids["helper"], ids["g_config"], C["uses"], count=1)
        db.add_edge_site(e, f_lib, 23, 3)
        # inherits (src=derived, dst=base)
        db.add_edge(ids["Derived"], ids["Base"], C["inherits"], base_access=1)
        db.add_edge(ids["Derived2"], ids["Derived"], C["inherits"], base_access=1)
        # overrides (src=overriding/derived, dst=overridden/base)
        db.add_edge(ids["Derived::draw"], ids["Base::draw"], C["overrides"])
        db.add_edge(ids["Derived2::draw"], ids["Derived::draw"], C["overrides"])
        # field_of / method_of (src=member, dst=record)
        db.add_edge(ids["Base::x"], ids["Base"], C["field_of"])
        db.add_edge(ids["Base::draw"], ids["Base"], C["method_of"])
        db.add_edge(ids["Derived::draw"], ids["Derived"], C["method_of"])
        db.add_edge(ids["Derived2::draw"], ids["Derived2"], C["method_of"])
        # contains (src=scope, dst=child) -- the OUTbound member edge
        db.add_edge(ids["Base"], ids["Base::Nested"], C["contains"])
    return ids


@pytest.fixture
def index_db(tmp_path) -> str:
    """Path to a freshly-seeded (unresolved) index database file."""
    repo = str(tmp_path / "repo")
    os.makedirs(os.path.join(repo))
    db_path = str(tmp_path / "index.db")
    with Storage(db_path) as db:
        _seed(db, repo)
    return db_path


@pytest.fixture
def resolved_db(tmp_path) -> str:
    """Like index_db, but after `resolve` (counts rolled up, meta flag set)."""
    repo = str(tmp_path / "repo")
    os.makedirs(os.path.join(repo))
    db_path = str(tmp_path / "index.db")
    with Storage(db_path) as db:
        _seed(db, repo)
        db.resolve_pass()
    return db_path


@pytest.fixture
def ids(tmp_path) -> dict[str, int]:
    """The {key: symbol_id} map for the *resolved_db* fixture's graph.

    Rebuilt in its own DB so a test can ask for stable ids without depending on
    insertion order in another fixture's connection."""
    repo = str(tmp_path / "repo_ids")
    os.makedirs(repo)
    db_path = str(tmp_path / "ids.db")
    with Storage(db_path) as db:
        return _seed(db, repo)


@pytest.fixture
def g(resolved_db):
    """A read-only GraphQuery over the resolved fixture DB."""
    q = GraphQuery(resolved_db)
    yield q
    q.close()


@pytest.fixture
def empty_db(tmp_path) -> str:
    """A valid index with symbols but NO edges (built with --no-graph)."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    db_path = str(tmp_path / "empty.db")
    with Storage(db_path) as db:
        comp = db.add_component("lab", repo)
        root = db.add_directory(comp, "")
        fid = db.add_file(root, "main.c")
        db.add_symbol(Symbol(usr="c:@F@main", spelling="main", kind="function",
                             qual_name="main", file_id=fid, line=1, col=1,
                             is_definition=True, resolved=True))
    return db_path

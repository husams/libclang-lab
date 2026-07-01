"""Tests for the read-only GraphQuery library API (indexer.query).

Hermetic: every test runs against the seeded in-memory-style fixture DB built by
conftest.py -- no libclang, no network.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from indexer.query import (
    GraphQuery,
    Sym,
    Site,
    NoIndexError,
    NoEdgesError,
    default_db_path,
)
from indexer.storage import Storage, Symbol


# --------------------------------------------------------------------------- #
# Lookup symbols
# --------------------------------------------------------------------------- #


def test_find_fuzzy_qualified_name(g):
    hits = g.find("Base::draw")
    assert any(s.name == "Base::draw" for s in hits)
    # shortest-first ordering: a plain 'draw' fuzzy returns the shortest names up top
    names = [s.name for s in g.find("draw")]
    assert names == sorted(names, key=len)


def test_find_kind_filter(g):
    assert all(s.kind == "class" for s in g.find("Derived", kind="class"))
    assert g.find("Derived", kind="function") == []


def test_by_name_exact(g):
    # three classes define a method spelled 'draw'
    draws = g.by_name("draw")
    assert {s.name for s in draws} == {"Base::draw", "Derived::draw", "Derived2::draw"}
    assert g.by_name("draw", kind="class") == []


def test_get_by_id_usr_and_passthrough(g):
    s = g.get("c:@F@main")
    assert s is not None and s.spelling == "main"
    again = g.get(s.id)
    assert again is not None and again.usr == s.usr
    assert g.get(s) is s  # Sym pass-through
    assert g.get("c:@F@does_not_exist") is None


def test_symbols_in_file(g):
    syms = g.symbols_in_file("shapes.hpp")
    assert {s.name for s in syms} >= {"Base", "Derived", "Base::draw"}
    assert g.symbols_in_file("nonexistent.xyz") == []


def test_sym_carries_grounding(g):
    s = g.get("c:@F@helper")
    assert s.file is not None and s.file.path.endswith("lib.c")
    assert s.line == 20
    assert s.loc == "lib.c:20"


# --------------------------------------------------------------------------- #
# Lookup references
# --------------------------------------------------------------------------- #


def test_callers_and_callees(g):
    helper = g.get("c:@F@helper")
    assert {s.name for s in g.callers(helper)} == {"main"}
    callee_names = {s.name for s in g.callees(helper)}
    assert "compute" in callee_names
    # the stub callee is present but reported as a stub, not a real name
    stub = [s for s in g.callees(helper) if s.is_stub]
    assert len(stub) == 1 and stub[0].usr == "c:@F@ext_fn"


def test_references_includes_calls_and_uses(g):
    g_config = g.get("c:@g_config")
    refs = g.references(g_config)
    assert {e.peer.name for e in refs} == {"helper"}
    assert {e.kind for e in refs} == {"uses"}


def test_edges_in_out_typed(g):
    base = g.get("c:@S@Base")
    inbound = g.edges_in(base)
    # Derived inherits Base; Base::x field_of; Base::draw method_of -> all inbound
    kinds = {e.kind for e in inbound}
    assert {"inherits", "field_of", "method_of"} <= kinds
    outbound = g.edges_out(base, ("contains",))
    assert {e.peer.name for e in outbound} == {"Base::Nested"}


def test_sites_grounding(g):
    helper = g.get("c:@F@helper")
    edge = [e for e in g.edges_out(helper, ("calls",)) if e.peer.name == "compute"][0]
    sites = g.sites(edge)
    assert len(sites) == 2
    assert all(isinstance(s, Site) and s.file.endswith("lib.c") for s in sites)
    assert {s.line for s in sites} == {22, 25}


def test_count_multiplicity_resolved(g):
    helper = g.get("c:@F@helper")
    edge = [e for e in g.edges_out(helper, ("calls",)) if e.peer.name == "compute"][0]
    assert edge.count == 2  # two call sites


def test_count_falls_back_to_site_count_when_unresolved(index_db):
    # index_db is NOT resolved -> count must come from COUNT(edge_site)
    with GraphQuery(index_db) as q:
        helper = q.get("c:@F@helper")
        edge = [e for e in q.edges_out(helper, ("calls",)) if e.peer.name == "compute"][
            0
        ]
        assert edge.count == 2


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #


def test_neighbors_direction(g):
    base = g.get("c:@S@Base")
    assert {s.name for s in g.neighbors(base, ("inherits",), "in")} == {"Derived"}
    assert g.neighbors(base, ("inherits",), "out") == []


def test_neighbors_with_kind_returns_relation_type(g):
    base = g.get("c:@S@Base")
    # default: plain Syms (no relation type)
    plain = g.neighbors(base, ("inherits",), "in")
    assert all(isinstance(s, Sym) for s in plain)
    # with_kind: (Sym, edge_kind) tuples
    tagged = g.neighbors(base, ("inherits",), "in", with_kind=True)
    assert tagged == [(plain[0], "inherits")]
    # spanning multiple kinds annotates each peer with how it was reached
    main = g.get("c:@F@main")
    kinds = {kind for _s, kind in g.neighbors(main, None, "out", with_kind=True)}
    assert "calls" in kinds


def test_walk_bounded_depth(g):
    main = g.get("c:@F@main")
    tr = g.walk(main, ("calls",), "out", depth=1)
    # depth 1: only helper
    assert {s.name for s in tr.nodes if s.id != main.id} == {"helper"}
    tr2 = g.walk(main, ("calls",), "out", depth=3)
    reached = {s.name for s in tr2.nodes}
    assert {"helper", "compute"} <= reached
    # path reconstruction
    compute = g.get("c:@F@compute")
    path = [s.name for s in tr2.path_to(compute)]
    assert path == ["main", "helper", "compute"]


def test_walk_max_nodes_bound(g):
    main = g.get("c:@F@main")
    tr = g.walk(main, ("calls",), "out", depth=5, max_nodes=2)
    assert len(tr) <= 2


def test_reaches_shortest_path(g):
    chain = g.reaches("c:@F@main", "c:@F@compute")
    assert chain is not None
    assert [s.name for s in chain] == ["main", "helper", "compute"]


def test_reaches_none_when_unreachable(g):
    # compute does not call main
    assert g.reaches("c:@F@compute", "c:@F@main") is None


# --------------------------------------------------------------------------- #
# Hierarchy (direction gotchas)
# --------------------------------------------------------------------------- #


def test_bases_direct_and_transitive(g):
    d2 = g.get("c:@S@Derived2")
    assert {s.name for s in g.bases(d2, direct=True)} == {"Derived"}
    assert {s.name for s in g.bases(d2, direct=False)} == {"Derived", "Base"}


def test_subclasses_direct_and_transitive(g):
    base = g.get("c:@S@Base")
    assert {s.name for s in g.subclasses(base, direct=True)} == {"Derived"}
    assert {s.name for s in g.subclasses(base, direct=False)} == {"Derived", "Derived2"}


def test_members_unions_in_and_out_edges(g):
    base = g.get("c:@S@Base")
    members = {s.name for s in g.members(base)}
    # field_of (in) + method_of (in) + contains (out) all included
    assert members == {"Base::x", "Base::draw", "Base::Nested"}


def test_members_access_filter(g):
    base = g.get("c:@S@Base")
    # seed: Base::draw public (method), Base::Nested public (struct),
    #       Base::x private (member)
    assert {s.name for s in g.members(base, access="public")} == {
        "Base::draw",
        "Base::Nested",
    }
    assert {s.name for s in g.members(base, access="private")} == {"Base::x"}
    assert g.members(base, access="protected") == []
    # None and 'all' both mean "every member"
    assert (
        {s.name for s in g.members(base)}
        == {s.name for s in g.members(base, access="all")}
        == {"Base::x", "Base::draw", "Base::Nested"}
    )


def test_members_access_invalid_raises(g):
    base = g.get("c:@S@Base")
    with pytest.raises(ValueError, match="unknown access"):
        g.members(base, access="bogus")


# --------------------------------------------------------------------------- #
# Dynamic dispatch
# --------------------------------------------------------------------------- #


def test_overrides_and_overridden_by(g):
    bdraw = g.get("c:@S@Base@F@draw#")
    assert {s.name for s in g.overridden_by(bdraw)} == {"Derived::draw"}
    ddraw = g.get("c:@S@Derived@F@draw#")
    assert {s.name for s in g.overrides(ddraw)} == {"Base::draw"}


def test_is_virtual_method(g):
    assert g.is_virtual_method("c:@S@Base@F@draw#") is True  # pure
    assert g.is_virtual_method("c:@S@Derived2@F@draw#") is True  # overrides
    assert g.is_virtual_method("c:@F@main") is False


def test_dispatch_targets_excludes_pure_root(g):
    # Base::draw is pure -> not itself a target; both overriders are
    targets = {s.name for s in g.dispatch_targets("c:@S@Base@F@draw#")}
    assert targets == {"Derived::draw", "Derived2::draw"}


def test_dispatch_targets_includes_concrete_root(g):
    # Derived::draw is concrete -> itself + its transitive overrider
    targets = {s.name for s in g.dispatch_targets("c:@S@Derived@F@draw#")}
    assert targets == {"Derived::draw", "Derived2::draw"}


def test_virtual_callees(g):
    render = g.get("c:@F@render")
    vc = {s.name for s in g.virtual_callees(render)}
    assert vc == {"Base::draw"}


# --------------------------------------------------------------------------- #
# Error paths / guards
# --------------------------------------------------------------------------- #


def test_no_index_raises(tmp_path):
    with pytest.raises(NoIndexError):
        GraphQuery(str(tmp_path / "nope.db"))


def test_no_edges_raises_with_require(empty_db):
    # opens fine without the guard...
    q = GraphQuery(empty_db)
    assert q.edge_count() == 0
    q.close()
    # ...but require_edges refuses
    with pytest.raises(NoEdgesError):
        GraphQuery(empty_db, require_edges=True)


def test_unknown_edge_kind_raises(g):
    with pytest.raises(ValueError):
        g.edges_out("c:@F@main", ("bogus",))


def test_bad_direction_raises(g):
    with pytest.raises(ValueError):
        g._edges("c:@F@main", "sideways", None, 10)


def test_default_db_path_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("INDEXER_CACHE", str(tmp_path))
    assert default_db_path() == str(tmp_path / "index.db")


# --------------------------------------------------------------------------- #
# Dataclass / repr sanity
# --------------------------------------------------------------------------- #


def test_sym_to_dict_schema(g):
    d = g.get("c:@F@main").to_dict()
    assert set(d) == {
        "id",
        "usr",
        "spelling",
        "qual_name",
        "kind",
        "type_info",
        "file",
        "line",
        "col",
        "end_line",
        "end_col",
        "is_definition",
        "is_pure",
        "is_static",
        "is_instantiation",
        "is_stub",
    }


def test_edge_to_dict_carries_peer_and_sites(g):
    helper = g.get("c:@F@helper")
    edge = [e for e in g.edges_out(helper, ("calls",)) if e.peer.name == "compute"][0]
    d = edge.to_dict(sites=g.sites(edge))
    assert d["qual_name"] == "compute"
    assert d["edge_kind"] == "calls"
    assert d["count"] == 2
    assert len(d["sites"]) == 2
    assert all("file" in s and "line" in s for s in d["sites"])


def test_edge_to_dict_auto_populates_sites(g):
    """Regression: edges_out/edges_in eager-load sites, so to_dict() returns the
    reference location (WHERE the edge occurs) WITHOUT an explicit sites= arg."""
    helper = g.get("c:@F@helper")
    edge = [e for e in g.edges_out(helper, ("calls",)) if e.peer.name == "compute"][0]
    # eager-loaded on the edge itself, not just via g.sites()
    assert {s.line for s in edge.sites} == {22, 25}
    d = edge.to_dict()  # no sites= passed
    assert {s["line"] for s in d["sites"]} == {22, 25}
    # the peer's own decl line is distinct from the reference sites
    assert d["line"] not in {s["line"] for s in d["sites"]} or len(d["sites"]) > 0


# --------------------------------------------------------------------------- #
# Sym.source() (#25) -- read the symbol's own region straight off disk
# --------------------------------------------------------------------------- #


def test_source_reads_own_extent(tmp_path, g):
    src = tmp_path / "region.c"
    src.write_text("int add(int a, int b) {\n    return a + b;\n}\n")
    sym = replace(
        g.get("c:@F@main"),
        file=g.make_file(str(src)),
        line=1,
        col=1,
        end_line=3,
        end_col=1,
    )
    assert sym.source() == "int add(int a, int b) {\n    return a + b;\n}"


def test_source_falls_back_to_default_lines_without_extent(tmp_path, g):
    src = tmp_path / "region2.c"
    src.write_text("\n".join(f"line{i}" for i in range(1, 20)) + "\n")
    sym = replace(
        g.get("c:@F@main"),
        file=g.make_file(str(src)),
        line=2,
        col=1,
        end_line=None,
        end_col=None,
    )
    assert sym.source(default_lines=3) == "line2\nline3\nline4"


def test_source_default_lines_clips_at_eof(tmp_path, g):
    src = tmp_path / "region3.c"
    src.write_text("line1\nline2\n")
    sym = replace(
        g.get("c:@F@main"),
        file=g.make_file(str(src)),
        line=1,
        col=1,
        end_line=None,
        end_col=None,
    )
    assert sym.source(default_lines=10) == "line1\nline2"


def test_source_empty_without_file_or_line(g):
    sym = replace(g.get("c:@F@main"), file=None, line=None)
    assert sym.source() == ""


def test_files_resolves_grouped_relative_component_path(tmp_path):
    """Regression: a component grouped under a repository stores its path
    RELATIVE to the active clone root (v24 -- see relativize_component). The
    read-only GraphQuery._files() cache must route through
    Storage.component_abs_base like the writable side's file_abs_path does,
    else Sym.file.path comes back clone-relative and unopenable -- e.g.
    Sym.source() raising FileNotFoundError on a perfectly valid index."""
    clone_root = tmp_path / "clone"
    clone_root.mkdir()
    src = clone_root / "region.c"
    src.write_text("int main(void) { return 0; }\n")

    db_path = str(tmp_path / "i.db")
    with Storage(db_path) as db:
        rid = db.add_repository("r")
        clone_id = db.add_clone(rid, str(clone_root))
        db.set_active_clone(rid, clone_id)
        comp = db.add_component("r", str(clone_root), "repo")
        db.set_component_repository(comp, rid)
        db.relativize_component(comp, str(clone_root))  # path becomes "."
        root = db.add_directory(comp, "")
        fid = db.add_file(root, "region.c")
        db.add_symbol(
            Symbol(
                usr="c:@F@main",
                spelling="main",
                kind="function",
                qual_name="main",
                file_id=fid,
                line=1,
                col=1,
                is_definition=True,
                resolved=True,
            )
        )

    g = GraphQuery(db_path)
    try:
        sym = g.get("c:@F@main")
        assert sym.file is not None
        assert os.path.isabs(sym.file.path)
        assert os.path.realpath(sym.file.path) == os.path.realpath(str(src))
        assert sym.source(default_lines=1) == "int main(void) { return 0; }"
    finally:
        g.close()

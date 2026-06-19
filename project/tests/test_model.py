"""Tests for the high-level OO model layer (indexer.model).

Reuses the hermetic fixture graph from conftest.py (resolved_db / ids) and adds
a couple of bespoke tiny DBs where the fixture lacks the needed shape (distinct
decl-vs-def locations, a function with a real type_info signature).
"""

from __future__ import annotations

import os

import pytest

from indexer.storage import Storage, Symbol
from indexer.query import GraphQuery, EDGE_KINDS
from indexer.model import (
    CodeBase,
    Function,
    FunctionTemplate,
    Method,
    Class,
    Namespace,
    Field,
    Variable,
    Type,
    Location,
    Reference,
    _parse_signature,
    _base_type_name,
    _split_top_level,
)


def _build_callgraph_db(db_path: str, repo: str, edges):
    """Tiny single-file DB of `function` symbols wired with `calls` edges.

    `edges` is a list of (caller, callee, [(line, col), ...]) -- each call site
    is recorded so callees() / callgraph() can order by source position."""
    os.makedirs(repo, exist_ok=True)
    names = sorted({n for c, e, _ in edges for n in (c, e)})
    with Storage(db_path) as db:
        comp = db.add_component("lab", repo)
        root = db.add_directory(comp, "")
        f = db.add_file(root, "m.c")
        ids = {
            n: db.add_symbol(
                Symbol(
                    usr=f"c:@F@{n}",
                    spelling=n,
                    kind="function",
                    qual_name=n,
                    file_id=f,
                    line=1,
                    col=1,
                    is_definition=True,
                    resolved=True,
                )
            )
            for n in names
        }
        with db.transaction():
            for caller, callee, sites in edges:
                e = db.add_edge(
                    ids[caller], ids[callee], EDGE_KINDS["calls"], count=len(sites)
                )
                for line, col in sites:
                    db.add_edge_site(e, f, line, col)
    return ids


@pytest.fixture
def cb(resolved_db):
    """A CodeBase over the resolved fixture DB."""
    c = CodeBase(GraphQuery(resolved_db))
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# factory: kind -> entity class
# --------------------------------------------------------------------------- #


def test_wrap_maps_kinds_to_classes(cb, ids):
    assert isinstance(cb.get(ids["main"]), Function)
    assert not isinstance(cb.get(ids["main"]), Method)
    assert isinstance(cb.get(ids["Base"]), Class)
    assert isinstance(cb.get(ids["Base::draw"]), Method)
    assert isinstance(cb.get(ids["Base::x"]), Field)
    assert isinstance(cb.get(ids["g_config"]), Variable)
    # struct is also a Class
    assert isinstance(cb.get(ids["Base::Nested"]), Class)
    assert cb.get(ids["Base::Nested"]).kind == "struct"


def test_find_returns_typed_entities(cb):
    hits = cb.find("Base")
    assert any(isinstance(e, Class) and e.name == "Base" for e in hits)


def test_escape_hatch_to_low_level(cb, ids):
    fn = cb.get(ids["main"])
    assert fn.sym.id == ids["main"]
    assert isinstance(cb.graph, GraphQuery)  # underlying handle reachable


# --------------------------------------------------------------------------- #
# callables: call graph
# --------------------------------------------------------------------------- #


def test_function_callers_callees(cb, ids):
    main = cb.get(ids["main"])
    helper = cb.get(ids["helper"])
    assert helper in main.callees()
    assert main in helper.callers()
    # helper calls compute and the never-indexed stub ext_fn
    callee_ids = {e.id for e in helper.callees()}
    assert ids["compute"] in callee_ids
    assert ids["ext_fn"] in callee_ids


def test_callgraph_unbounded(cb, ids):
    # main -> helper -> {compute, ext_fn(stub leaf)}
    main = cb.get(ids["main"])
    reached = {e.id: d for e, d in main.callgraph()}
    assert reached == {
        ids["helper"]: 1,
        ids["compute"]: 2,
        ids["ext_fn"]: 2,  # external stub: reached, then a leaf
    }


def test_callgraph_is_lazy_generator(cb, ids):
    import types

    gen = cb.get(ids["main"]).callgraph()
    assert isinstance(gen, types.GeneratorType)
    first, depth = next(gen)  # only the first level is computed so far
    assert first.id == ids["helper"] and depth == 1


def test_callgraph_depth_bound(cb, ids):
    main = cb.get(ids["main"])
    # depth=1 -> direct callees only
    assert {e.id for e, _ in main.callgraph(depth=1)} == {ids["helper"]}
    # depth=2 reaches the leaves
    assert {e.id for e, _ in main.callgraph(depth=2)} == {
        ids["helper"],
        ids["compute"],
        ids["ext_fn"],
    }


def test_callgraph_leaf_has_empty_walk(cb, ids):
    # compute calls nothing -> the generator yields nothing and terminates
    compute = cb.get(ids["compute"])
    assert list(compute.callgraph()) == []


def test_callgraph_reaches_method_callee(cb, ids):
    # render -> Base::draw: the walk surfaces a Method callee
    render = cb.get(ids["render"])
    assert {e.id for e, _ in render.callgraph()} == {ids["Base::draw"]}


def test_callgraph_available_on_all_callable_kinds(cb, ids):
    # Method / Constructor / Destructor / FunctionTemplate inherit it from
    # Callable; here Base::draw is a Method whose walk is empty (no callees).
    draw = cb.get(ids["Base::draw"])
    assert isinstance(draw, Method)
    assert hasattr(draw, "callgraph")
    assert list(draw.callgraph()) == []


def test_callees_ordered_by_source_not_count(tmp_path):
    # A is called ONCE at line 10; B is called TWICE at line 20. Count order
    # would put B first; source order must put A (earlier line) first.
    db_path = str(tmp_path / "i.db")
    ids = _build_callgraph_db(
        db_path,
        str(tmp_path / "r"),
        [
            ("f", "A", [(10, 5)]),
            ("f", "B", [(20, 5), (25, 5)]),
        ],
    )
    with CodeBase(GraphQuery(db_path)) as cb:
        names = [e.name for e in cb.get(ids["f"]).callees()]
        assert names == ["A", "B"]


def test_callgraph_is_dfs_preorder_call_sequence(tmp_path):
    # f calls B (line 30) then A (line 10); A calls C (line 5).
    #   source order of f's calls: A (10) before B (30)
    #   execution/DFS pre-order: A, C (A's callee), B
    db_path = str(tmp_path / "i.db")
    ids = _build_callgraph_db(
        db_path,
        str(tmp_path / "r"),
        [
            ("f", "B", [(30, 5)]),
            ("f", "A", [(10, 5)]),
            ("A", "C", [(5, 5)]),
        ],
    )
    with CodeBase(GraphQuery(db_path)) as cb:
        seq = [(e.name, d) for e, d in cb.get(ids["f"]).callgraph()]
        assert seq == [("A", 1), ("C", 2), ("B", 1)]


def test_callgraph_dfs_depth_bound(tmp_path):
    # same shape; depth=1 stops before descending into A's callee C
    db_path = str(tmp_path / "i.db")
    ids = _build_callgraph_db(
        db_path,
        str(tmp_path / "r"),
        [
            ("f", "A", [(10, 5)]),
            ("f", "B", [(30, 5)]),
            ("A", "C", [(5, 5)]),
        ],
    )
    with CodeBase(GraphQuery(db_path)) as cb:
        seq = [(e.name, d) for e, d in cb.get(ids["f"]).callgraph(depth=1)]
        assert seq == [("A", 1), ("B", 1)]


# --------------------------------------------------------------------------- #
# methods: owner / virtual / dispatch
# --------------------------------------------------------------------------- #


def test_method_owner_and_pure(cb, ids):
    draw = cb.get(ids["Base::draw"])
    assert isinstance(draw, Method)
    assert draw.owner == cb.get(ids["Base"])
    assert draw.is_pure is True
    assert draw.is_virtual is True
    assert draw.access == "public"


def test_method_override_relations(cb, ids):
    base_draw = cb.get(ids["Base::draw"])
    derived_draw = cb.get(ids["Derived::draw"])
    assert base_draw in derived_draw.overrides()
    assert derived_draw in base_draw.overridden_by()


def test_dispatch_targets_excludes_pure_root(cb, ids):
    base_draw = cb.get(ids["Base::draw"])
    targets = {m.id for m in base_draw.dispatch_targets()}
    # pure root excluded; both concrete overriders included
    assert ids["Base::draw"] not in targets
    assert ids["Derived::draw"] in targets
    assert ids["Derived2::draw"] in targets


# --------------------------------------------------------------------------- #
# records: members / inheritance / abstractness
# --------------------------------------------------------------------------- #


def test_record_fields_and_methods(cb, ids):
    base = cb.get(ids["Base"])
    assert cb.get(ids["Base::x"]) in base.fields
    assert cb.get(ids["Base::draw"]) in base.methods


def test_record_member_access_filter(cb, ids):
    base = cb.get(ids["Base"])
    private = base.members(access="private")
    assert cb.get(ids["Base::x"]) in private
    assert cb.get(ids["Base::draw"]) not in private  # draw is public


def test_inheritance_parents_children(cb, ids):
    base = cb.get(ids["Base"])
    derived = cb.get(ids["Derived"])
    derived2 = cb.get(ids["Derived2"])
    # direct
    assert derived.parents == [base]
    assert derived2.parents == [derived]
    # transitive
    assert base in derived2.ancestors and derived in derived2.ancestors
    child_ids = {c.id for c in base.children}
    assert ids["Derived"] in child_ids and ids["Derived2"] in child_ids


def test_is_abstract(cb, ids):
    # Base declares a pure-virtual draw -> abstract
    assert cb.get(ids["Base"]).is_abstract is True
    # Derived overrides draw with a concrete method -> not abstract
    assert cb.get(ids["Derived"]).is_abstract is False


# --------------------------------------------------------------------------- #
# references
# --------------------------------------------------------------------------- #


def test_references_calls_and_uses(cb, ids):
    compute = cb.get(ids["compute"])
    refs = compute.references()
    assert len(refs) == 1
    r = refs[0]
    assert isinstance(r, Reference)
    assert r.by == cb.get(ids["helper"])
    assert r.kind == "calls"
    assert r.sites and all(s.line for s in r.sites)

    g_config = cb.get(ids["g_config"])
    uses = g_config.references()
    assert uses[0].kind == "uses"
    assert uses[0].by == cb.get(ids["helper"])


# --------------------------------------------------------------------------- #
# locations: definition vs declaration
# --------------------------------------------------------------------------- #


def test_definition_only_has_no_separate_declaration(cb, ids):
    main = cb.get(ids["main"])
    assert main.definition is not None
    assert main.definition.line == 10
    # fixture symbols carry only a def location -> no distinct declaration
    assert main.declaration is None


def test_distinct_decl_and_def(tmp_path):
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    db_path = str(tmp_path / "i.db")
    with Storage(db_path) as db:
        comp = db.add_component("lab", repo)
        root = db.add_directory(comp, "")
        f_h = db.add_file(root, "m.h")
        f_c = db.add_file(root, "m.c")
        # prototype in header (decl), body in .c (def): both sites recorded
        db.add_symbol(
            Symbol(
                usr="c:@F@multiply",
                spelling="multiply",
                kind="function",
                qual_name="multiply",
                file_id=f_c,
                line=40,
                col=1,
                decl_file_id=f_h,
                decl_line=3,
                decl_col=1,
                is_definition=True,
                resolved=True,
                type_info="int (int, int)",
            )
        )
    with CodeBase(GraphQuery(db_path)) as cb:
        fn = cb.by_name("multiply")[0]
        assert isinstance(fn, Function)
        assert fn.definition == Location(os.path.join(repo, "m.c"), 40, 1)
        decl = fn.declaration
        assert decl is not None and decl.line == 3 and decl.file.endswith("m.h")
        # signature parsed from type_info
        assert fn.return_type.spelling == "int"
        assert [a.spelling for a in fn.arguments] == ["int", "int"]


# --------------------------------------------------------------------------- #
# signature / type parsing units
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sig, ret, args",
    [
        ("int (void)", "int", []),
        ("int (int, int)", "int", ["int", "int"]),
        ("std::list<std::string> *()", "std::list<std::string> *", []),
        ("RdKafka::Conf *(ConfType)", "RdKafka::Conf *", ["ConfType"]),
        (
            "Error *(ErrorCode, const std::string *)",
            "Error *",
            ["ErrorCode", "const std::string *"],
        ),
        (
            "void (const std::pair<int, int> &)",
            "void",
            ["const std::pair<int, int> &"],
        ),  # comma inside <> not a separator
    ],
)
def test_parse_signature(sig, ret, args):
    assert _parse_signature(sig) == (ret, args)


def test_parse_signature_non_function():
    assert _parse_signature("int") == ("int", None)
    assert _parse_signature(None) == (None, None)


@pytest.mark.parametrize(
    "spelling, base",
    [
        ("const std::string &", "std::string"),
        ("Foo<int> *", "Foo"),
        ("unsigned int", "int"),
        ("struct error", "error"),
        ("uint32_t[8][256]", "uint32_t"),
    ],
)
def test_base_type_name(spelling, base):
    assert _base_type_name(spelling) == base


def test_split_top_level_respects_brackets():
    assert _split_top_level("a, b<c, d>, e") == ["a", "b<c, d>", "e"]


# --------------------------------------------------------------------------- #
# type resolution
# --------------------------------------------------------------------------- #


def test_type_resolves_to_declaration(cb, ids):
    # a Type naming Base resolves to the Base class entity
    t = Type("const Base &", cb)
    assert t.name == "Base"
    decl = t.declaration()
    assert decl == cb.get(ids["Base"])


# --------------------------------------------------------------------------- #
# function templates surface as members / free functions (regression)
#
# FunctionTemplate is a sibling of Method/Function, not a subclass. A member
# function template (e.g. Cache::set<T>) must still appear in Record.methods,
# and a free function template must be findable via CodeBase.function() and
# Namespace.functions. (cf. utl::BzRuleValueCache::set/get vanishing report.)
# --------------------------------------------------------------------------- #


def _build_template_db(db_path: str, repo: str) -> dict[str, int]:
    os.makedirs(repo, exist_ok=True)
    ids: dict[str, int] = {}
    with Storage(db_path) as db:
        comp = db.add_component("lab", repo)
        root = db.add_directory(comp, "")
        f = db.add_file(root, "t.hpp")

        def sym(key, usr, spelling, kind, line, *, qual=None, parent=None,
                access=None):
            ids[key] = db.add_symbol(
                Symbol(usr=usr, spelling=spelling, kind=kind,
                       qual_name=qual or spelling, file_id=f, line=line, col=1,
                       is_definition=True, parent_usr=parent, resolved=True,
                       access=access)
            )

        # class Cache with a plain method + two member function templates
        sym("Cache", "c:@S@Cache", "Cache", "class", 1, qual="Cache")
        sym("Cache::clear", "c:@S@Cache@F@clear#", "clear", "method", 2,
            qual="Cache::clear", parent="c:@S@Cache", access="public")
        sym("Cache::set", "c:@S@Cache@FT@>1#Tset#", "set", "function-template",
            3, qual="Cache::set", parent="c:@S@Cache", access="public")
        sym("Cache::get", "c:@S@Cache@FT@>1#Tget#", "get", "function-template",
            4, qual="Cache::get", parent="c:@S@Cache", access="public")
        # namespace ns with a free function template
        sym("ns", "c:@N@ns", "ns", "namespace", 10, qual="ns")
        sym("ns::wrap", "c:@N@ns@FT@>1#Twrap#", "wrap", "function-template", 11,
            qual="ns::wrap", parent="c:@N@ns")

        # edges (add_edge does not auto-commit -> wrap in a transaction):
        #   method_of (kind=9): member -> record
        #   contains  (kind=3): namespace -> member
        with db.transaction():
            for m in ("Cache::clear", "Cache::set", "Cache::get"):
                db.add_edge(ids[m], ids["Cache"], EDGE_KINDS["method_of"])
            db.add_edge(ids["ns"], ids["ns::wrap"], EDGE_KINDS["contains"])
    return ids


def test_member_function_templates_in_methods(tmp_path):
    db_path = str(tmp_path / "t.db")
    ids = _build_template_db(db_path, str(tmp_path / "r"))
    with CodeBase(GraphQuery(db_path)) as cb:
        cache = cb.get(ids["Cache"])
        names = sorted(m.spelling for m in cache.methods)
        # plain method AND both member templates present
        assert names == ["clear", "get", "set"]
        # the templates are typed as FunctionTemplate, the plain one as Method
        by_name = {m.spelling: m for m in cache.methods}
        assert isinstance(by_name["clear"], Method)
        assert isinstance(by_name["set"], FunctionTemplate)
        assert isinstance(by_name["get"], FunctionTemplate)


def test_free_function_template_found_by_function_lookup(tmp_path):
    db_path = str(tmp_path / "t.db")
    _build_template_db(db_path, str(tmp_path / "r"))
    with CodeBase(GraphQuery(db_path)) as cb:
        f = cb.function("wrap")
        assert isinstance(f, FunctionTemplate)
        assert f.spelling == "wrap"


def test_namespace_functions_include_templates(tmp_path):
    db_path = str(tmp_path / "t.db")
    ids = _build_template_db(db_path, str(tmp_path / "r"))
    with CodeBase(GraphQuery(db_path)) as cb:
        ns = cb.get(ids["ns"])
        assert isinstance(ns, Namespace)
        names = sorted(e.spelling for e in ns.functions)
        assert names == ["wrap"]
        assert all(isinstance(e, FunctionTemplate) for e in ns.functions)

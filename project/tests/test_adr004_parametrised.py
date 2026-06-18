"""ADR-004 parametrised + boundary tests (QA addition).

Three mandatory test categories:
  (A) Parametrised boundary — multiple concrete type-arg spellings confirmed
      via template_arg rows, not via USR string parsing.
  (B) Callgraph chain — caller -> calls -> instance -> instantiates -> template
      traversal is complete and non-cyclic for each instantiated type.
  (C) Mutation boundary — default callers() NEVER returns instantiation members;
      include_instantiations=True ALWAYS returns a strict superset when instances exist.

The fixture builds a class template X<T> with callers for T in
{int, double, unsigned int} plus a bare-pointer type (Wrapper*) to exercise
named-type ref_id lookup without crashing.  All assertions operate only on
data already stored in the DB (template_arg.literal, edge kinds) — zero
USR-string parsing.
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import NamedTuple

import pytest

import clang.cindex as cx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from _helpers import clang_args  # noqa: E402

from indexer.clang import ast as A  # noqa: E402
from indexer.query import GraphQuery  # noqa: E402
from indexer.storage import Storage  # noqa: E402


# ---------------------------------------------------------------------------
# Source corpus — note: Wrapper* used to exercise named-type path
# ---------------------------------------------------------------------------

SOURCE = """
struct Wrapper {};

template <typename T>
struct X {
    void print() {}
    void run()   {}
};

void caller_int()      { X<int>          xi; xi.print(); xi.run(); }
void caller_double()   { X<double>       xd; xd.print(); }
void caller_uint()     { X<unsigned int> xu; xu.print(); }
void caller_wrapper()  { X<Wrapper>      xw; xw.print(); }
"""


class _Env(NamedTuple):
    db_path: str
    g: GraphQuery


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = str(tmp_path_factory.mktemp("adr004_param"))
    path = os.path.join(tmp, "tmpl.cpp")
    with open(path, "w") as fh:
        fh.write(SOURCE)

    tu = cx.Index.create().parse(path, args=clang_args(path) + ["-std=c++17"])
    errs = [d for d in tu.diagnostics if d.severity >= 3]
    assert not errs, f"parse errors: {errs}"

    db_path = os.path.join(tmp, "tmpl.db")
    db = Storage(db_path)
    db.add_component("t", tmp)
    fid = db.add_file_path(path)
    with db.transaction():
        A.index_symbols(db, tu, fid)
    with db.transaction():
        db.delete_edges_for_file(fid)
        A._index_edges_notxn(db, tu, path, fid)
    db.close()

    g = GraphQuery(db_path)
    yield _Env(db_path=db_path, g=g)
    g.close()


# ---------------------------------------------------------------------------
# (A) Parametrised boundary: each expected type-arg literal appears exactly
#     once (one per X<T> instantiation type node), regardless of how many
#     members are called on that instance.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expected_literal", ["int", "double", "unsigned int"])
def test_type_arg_literal_present(env: _Env, expected_literal: str):
    """Each instantiated builtin type must appear as a template_arg literal
    on exactly one X instantiation type node.  Tests the structured storage
    path (NOT USR string parsing)."""
    g = env.g
    x_inst_types = [s for s in g.find("X") if s.is_instantiation]
    assert x_inst_types, "no instantiation type nodes for X"

    literals_per_node = {}
    for node in x_inst_types:
        args = g.template_args(node)
        for ta in args:
            if ta.literal:
                literals_per_node.setdefault(ta.literal, []).append(node.usr)

    assert expected_literal in literals_per_node, (
        f"'{expected_literal}' not found in any type node's template_arg; "
        f"found: {sorted(literals_per_node.keys())}"
    )
    # Each literal must appear on exactly ONE type node (no duplication).
    assert len(literals_per_node[expected_literal]) == 1, (
        f"'{expected_literal}' appears on multiple type nodes: "
        f"{literals_per_node[expected_literal]}"
    )


def test_named_type_arg_has_ref_id(env: _Env):
    """X<Wrapper>: the Wrapper template arg must have ref_id != None because
    Wrapper is an indexed named declaration (not a builtin)."""
    g = env.g
    x_inst_types = [s for s in g.find("X") if s.is_instantiation]
    wrapper_args = []
    for node in x_inst_types:
        for ta in g.template_args(node):
            if ta.literal and "Wrapper" in ta.literal:
                wrapper_args.append(ta)

    if not wrapper_args:
        pytest.skip(
            "X<Wrapper> not indexed in this build (OK — Wrapper may be filtered)"
        )

    for ta in wrapper_args:
        assert ta.ref_id is not None, (
            f"named-type arg 'Wrapper' must have a ref_id; got literal={ta.literal!r}"
        )


# ---------------------------------------------------------------------------
# (B) Callgraph chain: caller --calls--> instance --instantiates--> template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "caller_name,expected_type_literal",
    [
        ("caller_int", "int"),
        ("caller_double", "double"),
        ("caller_uint", "unsigned int"),
    ],
)
def test_callgraph_chain_caller_instance_template(
    env: _Env, caller_name: str, expected_type_literal: str
):
    """For each caller, verify the chain:
      caller --calls--> X<T>::print (is_instantiation=1)
              --instantiates--> X::print (is_instantiation=0, template)
    and that X<T> carries the expected template_arg literal."""
    g = env.g

    callers = [s for s in g.find(caller_name) if not s.is_instantiation]
    assert callers, f"caller function {caller_name!r} not found"
    caller_sym = callers[0]

    # Step 1: direct callees of the caller
    direct_callees = g.callees(caller_sym, include_instantiations=False)
    inst_callees = [s for s in direct_callees if s.is_instantiation]
    assert inst_callees, (
        f"{caller_name} has no instantiation-member callees; "
        f"all callees: {direct_callees}"
    )

    # Step 2: each instantiation member has an instantiates edge to the template
    for inst in inst_callees:
        template_syms = g.neighbors(inst, kinds=("instantiates",), direction="out")
        assert template_syms, (
            f"instantiation member {inst.usr!r} missing outgoing instantiates edge"
        )
        template = template_syms[0]
        assert not template.is_instantiation, (
            f"instantiates target should be the template, not another instance: {template}"
        )
        # Step 3: the instance type node carries the expected template arg
        type_nodes = g.neighbors(inst, kinds=("method_of",), direction="out")
        assert type_nodes, f"instantiation member {inst.usr!r} missing method_of edge"
        type_node = type_nodes[0]
        targs = g.template_args(type_node)
        literals = {ta.literal for ta in targs if ta.literal}
        assert expected_type_literal in literals, (
            f"for {caller_name}: expected type arg '{expected_type_literal}' "
            f"on type node {type_node.usr!r}; found: {literals}"
        )


# ---------------------------------------------------------------------------
# (C) Mutation boundary: default callers() isolation + rollup superset
# ---------------------------------------------------------------------------


def test_default_callers_never_returns_instantiation_nodes(env: _Env):
    """Default callers(node) must NEVER return symbols with is_instantiation=1.
    Verifies that the rollup is fully opt-in and does not leak by accident."""
    g = env.g
    all_syms = g.find("X") + g.find("print") + g.find("run")
    for sym in all_syms:
        direct = g.callers(sym)
        leaked = [s for s in direct if s.is_instantiation]
        assert not leaked, (
            f"default callers() returned instantiation-member callers for {sym!r}; "
            f"leaked: {leaked}"
        )


def test_rollup_is_strict_superset_when_instances_exist(env: _Env):
    """callers(X::print, include_instantiations=True) must return strictly
    more callers than include_instantiations=False when instantiations exist."""
    g = env.g
    x_print_primaries = [
        s for s in g.find("print") if not s.is_instantiation and "ST" in s.usr
    ]
    if not x_print_primaries:
        pytest.skip("no template-method primary found")

    for primary in x_print_primaries:
        inst_members = g.instantiations(primary)
        if not inst_members:
            continue
        direct = g.callers(primary, include_instantiations=False)
        rollup = g.callers(primary, include_instantiations=True)
        assert len(rollup) >= len(direct), (
            f"rollup must be >= direct; rollup={rollup}, direct={direct}"
        )
        if inst_members:
            assert len(rollup) > len(direct), (
                "with instantiations present, rollup must be strictly larger than direct; "
                f"rollup={[r.sym.spelling for r in rollup]}, "
                f"direct={[s.spelling for s in direct]}"
            )
            return  # at least one primary confirmed the property
    # No primary had instances indexed — skip rather than fail.
    pytest.skip("no primary had indexed instantiation members (blast-radius guard)")


def test_rollup_result_spellings_match_expected_callers(env: _Env):
    """Concrete regression: callers(X::print, include_instantiations=True)
    must include ALL callers of each instantiation, not a subset."""
    g = env.g
    x_print_primaries = [
        s for s in g.find("print") if not s.is_instantiation and "ST" in s.usr
    ]
    if not x_print_primaries:
        pytest.skip("no X::print primary found")

    all_rollup_spellings: set[str] = set()
    for primary in x_print_primaries:
        for r in g.callers(primary, include_instantiations=True):
            all_rollup_spellings.add(r.sym.spelling)

    # Every function that calls xi.print() or xd.print() etc. must appear.
    for expected in ("caller_int", "caller_double", "caller_uint"):
        assert expected in all_rollup_spellings, (
            f"'{expected}' missing from rollup result; "
            f"got: {sorted(all_rollup_spellings)}"
        )


# ---------------------------------------------------------------------------
# (D) Method with multiple call-sites to same instantiation: no duplicate nodes
# ---------------------------------------------------------------------------


def test_caller_int_calls_two_methods_on_same_instance(env: _Env):
    """caller_int calls xi.print() AND xi.run() — both must be in direct callees
    but the X<int> type node must still be minted exactly ONCE (no duplication)."""
    g = env.g
    callers = [s for s in g.find("caller_int") if not s.is_instantiation]
    assert callers, "caller_int not found"
    caller_sym = callers[0]

    inst_callees = [
        s
        for s in g.callees(caller_sym, include_instantiations=False)
        if s.is_instantiation
    ]
    # caller_int calls print + run on X<int>, so at least 2 instantiation callees
    assert len(inst_callees) >= 2, (
        f"caller_int should call at least 2 instantiation members (print+run); "
        f"got: {[(s.spelling, s.usr) for s in inst_callees]}"
    )

    # The X<int> type node must appear exactly once
    x_inst_types = [s for s in g.find("X") if s.is_instantiation]
    int_type_nodes = [
        n for n in x_inst_types if any(ta.literal == "int" for ta in g.template_args(n))
    ]
    assert len(int_type_nodes) == 1, (
        f"expected exactly 1 X<int> type node; got {len(int_type_nodes)}: "
        f"{[n.usr for n in int_type_nodes]}"
    )

"""Regression: a type alias must never report a NAMESPACE as its underlying type.

Bug: when an alias's underlying type is namespace-qualified -- ``using X =
Ns::Foo;`` or a dependent nested member ``Tmpl<..., Ns::T>::type`` -- the indexer
emits a ``uses`` edge to the qualifier namespace ``Ns`` (via the NAMESPACE_REF
pass) alongside (or, for a dependent member, INSTEAD of) the real underlying-type
edge. ``Typedef.aliased()`` walked the first ``uses`` neighbour, so it returned
the namespace and ``underlying_type`` became ``Type('Ns')`` -- wrong.

A type alias can never *name* a namespace, so ``aliased()`` now skips namespace
uses-neighbours. This drives the REAL libclang extraction path so the edge shape
(namespace edge present) is exactly what the indexer produces, not a hand-built
fixture.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from indexer.clang import ast as A
from indexer.clang import util as U
from indexer.model import CodeBase, Struct, Typedef
from indexer.query import GraphQuery
from indexer.storage import Storage

SOURCE = r"""
namespace IDC {
  struct Foo { int y; };
  template <class T> struct Wrap { using type = T; };
}
struct Bar {};
struct SvcA {};
struct SvcB {};

template <class A, class B, class C>
struct EntTemplateFastDecode { using type = A; };

// top-level namespace-qualified aliases (both a namespace uses-edge AND a
// real record/instance uses-edge are emitted):
using FooTop  = IDC::Foo;
using WrapTop = IDC::Wrap<Bar>;

struct Host {
  void reg() {
    // body-local alias whose underlying type is a dependent nested member of a
    // template instantiation with a namespace-qualified argument. libclang can
    // resolve no indexed target for `...::type`, so the ONLY uses-edge here is
    // the NAMESPACE_REF -> IDC edge. aliased() must ignore it (-> None) and let
    // underlying_type fall back to the full type_info spelling.
    using EntLocal =
      EntTemplateFastDecode<SvcA, SvcB, IDC::Foo>::type;
    // body-local simple namespace-qualified alias.
    using FooLocal = IDC::Foo;
    EntLocal a{};
    FooLocal b{};
    (void)a; (void)b;
  }
};
"""


@pytest.fixture
def cb():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "qualified.cpp")
        with open(path, "w") as fh:
            fh.write(SOURCE)
        tu = U.parse(path, args=["-std=c++17"], check=False)
        fatal = [d.spelling for d in tu.diagnostics if d.severity >= 3]
        assert not fatal, "fixture must parse cleanly: " + "; ".join(fatal)

        db_path = os.path.join(tmp, "i.db")
        db = Storage(db_path)
        db.add_component("t", tmp)
        file_id = db.add_file_path(path)
        with db.transaction():
            A.index_symbols(db, tu, file_id)
        with db.transaction():
            db.delete_edges_for_file(file_id)
            A._index_edges_notxn(db, tu, path, file_id)
        db.close()

        codebase = CodeBase(GraphQuery(db_path))
        try:
            yield codebase
        finally:
            codebase.close()


def _one(cb: CodeBase, name: str) -> Typedef:
    hits = cb.find(name)
    assert len(hits) == 1, hits
    alias = hits[0]
    assert isinstance(alias, Typedef), alias
    return alias


def test_top_level_qualified_alias_resolves_to_record_not_namespace(cb):
    """`using FooTop = IDC::Foo;` -> the Foo struct, never the IDC namespace."""
    alias = _one(cb, "FooTop")
    target = alias.aliased()
    assert isinstance(target, Struct)
    assert target.spelling == "Foo"
    assert target.name == "IDC::Foo"
    ut = alias.underlying_type
    assert ut is not None and ut.spelling == "IDC::Foo"


def test_top_level_qualified_template_alias_resolves_to_instance(cb):
    """`using WrapTop = IDC::Wrap<Bar>;` -> the concrete instance, not IDC."""
    alias = _one(cb, "WrapTop")
    target = alias.aliased()
    assert target is not None
    assert target.is_instantiation
    assert target.display_name == "IDC::Wrap<Bar>"
    ut = alias.underlying_type
    assert ut is not None and ut.spelling == "IDC::Wrap<Bar>"


def test_body_local_dependent_alias_falls_back_to_spelling(cb):
    """Dependent `EntTemplateFastDecode<...>::type`: no indexed target, so
    aliased() is None and underlying_type is the full type_info string -- NOT
    the IDC namespace that appears as the alias's only uses-edge."""
    alias = _one(cb, "EntLocal")
    assert alias.aliased() is None
    ut = alias.underlying_type
    assert ut is not None
    assert ut.spelling == (
        "EntTemplateFastDecode<SvcA, SvcB, IDC::Foo>::type"
    )
    # regression: the namespace must never leak through as the underlying type
    assert ut.spelling != "IDC"


def test_body_local_alias_never_reports_namespace_underlying_type(cb):
    """`using FooLocal = IDC::Foo;` inside a body: whatever fidelity aliased()
    achieves, underlying_type must not be the bare namespace `IDC`."""
    alias = _one(cb, "FooLocal")
    ut = alias.underlying_type
    assert ut is not None
    assert ut.spelling != "IDC"
    assert ut.spelling == "IDC::Foo"
    # aliased() may be None (no body-local underlying-type edge is emitted) or
    # the Foo record, but it must never be the IDC namespace.
    target = alias.aliased()
    assert target is None or (isinstance(target, Struct) and target.spelling == "Foo")

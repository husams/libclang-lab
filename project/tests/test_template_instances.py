"""Integration test for template parameters, arguments, and the
instantiation-vs-specialization distinction.

Drives the REAL libclang extractor (like test_template_calls.py) over a source
that exercises every shape:

  * a class template with a formal parameter            -> template_param rows
  * an explicit specialization (`template <> class ...`) -> `specializes` edge
  * an explicit instantiation  (`template class X<int>;`) -> `instantiates` edge
  * a function that instantiates by use (`X<char> v;`)    -> `instantiates` edge
                                                              from the function

and asserts the regression fixes:

  * an explicit instantiation is `instantiates` (kind 5), NOT `specializes`
  * TYPE args store the spelling in `literal` so Box<bool> != Box<int>
  * the model layer separates concrete instances (instantiations) from the
    functions that instantiate (instantiation_sites), exposes `parameters`,
    and surfaces `template_arguments` on each instance/specialization.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

import clang.cindex as cx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from _helpers import clang_args  # noqa: E402

from indexer.storage import Storage  # noqa: E402
from indexer.query import GraphQuery  # noqa: E402
from indexer.model import CodeBase, Record, Callable  # noqa: E402
from indexer.clang import ast as A  # noqa: E402


SOURCE = """
namespace nn {

template <class T>
class Box {
    T value_;
public:
    explicit Box(T v) : value_(v) {}
    T get() const { return value_; }
};

// explicit specialization for bool: its own body.
template <>
class Box<bool> {
    bool b_;
public:
    explicit Box(bool b) : b_(b) {}
    bool get() const { return b_; }
};

// explicit instantiation for int: NOT a specialization.
template class Box<int>;

// a function that instantiates Box<char> by using it.
char use() {
    Box<char> bc('x');
    return bc.get();
}

}  // namespace nn
"""


@pytest.fixture
def cb():
    """A CodeBase over a freshly-extracted index of SOURCE."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.cpp")
        with open(path, "w") as fh:
            fh.write(SOURCE)
        tu = cx.Index.create().parse(path, args=clang_args(path) + ["-std=c++17"])
        assert not [d for d in tu.diagnostics if d.severity >= 3], (
            "fixture source must parse cleanly"
        )

        db = Storage(os.path.join(tmp, "i.db"))
        db.add_component("t", tmp)
        file_id = db.add_file_path(path)
        with db.transaction():
            A.index_symbols(db, tu, file_id)
        with db.transaction():
            db.delete_edges_for_file(file_id)
            A._index_edges_notxn(db, tu, path, file_id)

        c = CodeBase(GraphQuery.from_connection(db._conn))
        yield c


def _box_template(cb):
    hits = [e for e in cb.find("Box", kind="class-template")]
    assert hits, "expected a Box class-template"
    return hits[0]


# --------------------------------------------------------------------------- #
# template parameters
# --------------------------------------------------------------------------- #


def test_class_template_parameters(cb):
    box = _box_template(cb)
    params = box.parameters
    assert [p.name for p in params] == ["T"]
    assert params[0].kind_name == "type"
    assert params[0].position == 0


# --------------------------------------------------------------------------- #
# instantiation vs specialization
# --------------------------------------------------------------------------- #


def test_explicit_instantiation_is_instance_not_specialization(cb):
    box = _box_template(cb)
    # The explicit instantiation `template class Box<int>;` is a concrete
    # instance: it must show up under instantiations(), NOT specializations().
    inst_args = {
        tuple(a.literal for a in i.template_arguments) for i in box.instantiations()
    }
    assert ("int",) in inst_args
    spec_args = {
        tuple(a.literal for a in s.template_arguments) for s in box.specializations()
    }
    assert ("int",) not in spec_args


def test_explicit_specialization_is_specialization(cb):
    box = _box_template(cb)
    # `template <> class Box<bool>` is a true specialization.
    spec_args = {
        tuple(a.literal for a in s.template_arguments) for s in box.specializations()
    }
    assert ("bool",) in spec_args


def test_instantiations_returns_records_not_functions(cb):
    box = _box_template(cb)
    assert box.instantiations(), "expected at least one concrete instance"
    assert all(isinstance(i, Record) for i in box.instantiations())


def test_instantiation_sites_are_the_using_functions(cb):
    box = _box_template(cb)
    sites = box.instantiation_sites()
    assert all(isinstance(s, Callable) for s in sites)
    assert "nn::use" in {s.name for s in sites}


# --------------------------------------------------------------------------- #
# template arguments carry the type spelling (Box<bool> != Box<int>)
# --------------------------------------------------------------------------- #


def test_type_args_record_spelling_for_builtins(cb):
    box = _box_template(cb)
    everything = box.instantiations() + box.specializations()
    seen = {tuple(a.literal for a in e.template_arguments) for e in everything}
    # builtins used to land as (None,) -- now distinguishable.
    assert ("bool",) in seen
    assert ("int",) in seen
    assert (None,) not in seen


def test_query_layer_template_readers(cb):
    box = _box_template(cb)
    g = cb.graph
    assert [p.name for p in g.template_params(box.sym)] == ["T"]
    # an instance carries args via the query layer too
    inst = box.instantiations()[0]
    args = g.template_args(inst.sym)
    assert args and args[0].arg_kind == 1 and args[0].literal in ("int", "bool")

"""Integration test for signature/field/variable `uses` edge extraction.

Unlike the hermetic query tests (which seed Storage directly), this drives the
REAL libclang extractor: it parses a small C++ source and asserts that a class
named only as a TYPE -- a parameter, return, field, local-variable, or
typedef-underlying type -- earns an inbound `uses` edge. That case is invisible
to the body-descent pass (the class never appears as a DECL_REF_EXPR), so it is
the regression guard for indexer.clang.ast._emit_type_use.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

import clang.cindex as cx

# scripts/_helpers.clang_args supplies -isysroot + the clang resource dir so the
# bundled-wheel parse is not truncated (the lab's home-section gotcha §1.2).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from _helpers import clang_args  # noqa: E402

from indexer.storage import Storage  # noqa: E402
from indexer.clang import ast as A  # noqa: E402


SOURCE = """
namespace RdKafka {
  class Conf { public: int x; Conf *self(); };
  class Producer {
   public:
    static Producer *create(const Conf *conf, int n);   // param + return
    Conf *m_conf;                                        // field
  };
  Producer *Producer::create(const Conf *conf, int n) {
    Conf local;                                          // local variable
    (void)local; (void)conf; (void)n;
    return 0;
  }
  typedef Conf ConfAlias;                                // typedef underlying
}
"""


@pytest.fixture
def conf_uses() -> set[str]:
    """qual_names of every symbol with a `uses` edge -> RdKafka::Conf."""
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

        conf = db.lookup_symbol("c:@N@RdKafka@S@Conf")
        assert conf is not None
        rows = db._conn.execute(
            "SELECT s.qual_name FROM edge e "
            "JOIN symbol s ON e.src_id = s.id "
            "WHERE e.dst_id = ? AND e.kind = 7",
            (conf.id,),
        ).fetchall()
        return {r[0] for r in rows}


def test_parameter_and_return_types_use_the_class(conf_uses):
    # `create` mentions Conf twice (param `const Conf*`, body local) -> one edge.
    assert "RdKafka::Producer::create" in conf_uses


def test_field_type_uses_the_class(conf_uses):
    assert "RdKafka::Producer::m_conf" in conf_uses


def test_return_type_uses_the_class(conf_uses):
    # Conf::self() returns Conf* -> the method uses Conf (no self-edge: the
    # method symbol is distinct from the class symbol).
    assert "RdKafka::Conf::self" in conf_uses


def test_typedef_underlying_type_uses_the_class(conf_uses):
    assert "RdKafka::ConfAlias" in conf_uses


def test_no_self_edge_from_class_to_itself(conf_uses):
    assert "RdKafka::Conf" not in conf_uses

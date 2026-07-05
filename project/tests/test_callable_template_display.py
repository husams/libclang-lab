from __future__ import annotations

import os
import sys

import clang.cindex as cx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from _helpers import clang_args  # noqa: E402

from indexer.clang import ast as A  # noqa: E402
from indexer.model import CodeBase, SpecializedMethod  # noqa: E402
from indexer.query import GraphQuery, Sym  # noqa: E402
from indexer.storage import Storage  # noqa: E402


SOURCE = """
namespace disp {
struct MyType {};
struct Other {};

struct Context {
    template <class T> void reg(int) {}
    template <class A, class B> void pair(double) {}
    void ordinary(int) {}
};

void use(Context& c) {
    c.reg<MyType>(1);
    c.pair<MyType, Other>(2.0);
    c.ordinary(3);
}
}  // namespace disp
"""


def _indexed(tmp_path):
    path = tmp_path / "display.cpp"
    path.write_text(SOURCE)
    tu = cx.Index.create().parse(
        str(path), args=clang_args(str(path)) + ["-std=c++17"]
    )
    assert not [d for d in tu.diagnostics if d.severity >= 3]

    db = Storage(str(tmp_path / "i.db"))
    db.add_component("t", str(tmp_path))
    file_id = db.add_file_path(str(path))
    with db.transaction():
        A.index_symbols(db, tu, file_id)
    with db.transaction():
        db.delete_edges_for_file(file_id)
        A._index_edges_notxn(db, tu, str(path), file_id)
    return db, CodeBase(GraphQuery.from_connection(db._conn))


def _specialized(g: GraphQuery, spelling: str, args: list[str]) -> Sym:
    for sym in g.find(spelling):
        if (
            sym.is_instantiation
            and [a.literal for a in g.template_args(sym)] == args
        ):
            return sym
    raise AssertionError(f"no specialized {spelling} with args {args!r}")


def test_specialized_method_display_includes_single_template_arg(tmp_path):
    db, cb = _indexed(tmp_path)
    try:
        sym = _specialized(cb.graph, "reg", ["MyType"])
        assert sym.display_name == "reg<MyType>(int)"

        entity = cb.get(sym)
        assert isinstance(entity, SpecializedMethod)
        assert entity.display_name == "disp::Context::reg<MyType>(int)"
    finally:
        cb.close()
        db.close()


def test_specialized_method_display_keeps_template_arg_order(tmp_path):
    db, cb = _indexed(tmp_path)
    try:
        sym = _specialized(cb.graph, "pair", ["MyType", "Other"])
        assert sym.display_name == "pair<MyType, Other>(double)"

        entity = cb.get(sym)
        assert isinstance(entity, SpecializedMethod)
        assert entity.display_name == "disp::Context::pair<MyType, Other>(double)"
    finally:
        cb.close()
        db.close()


def test_non_specialized_method_display_is_unchanged(tmp_path):
    db, cb = _indexed(tmp_path)
    try:
        ordinary = [
            sym
            for sym in cb.graph.find("ordinary")
            if sym.kind == "method" and not sym.is_instantiation
        ]
        assert ordinary
        assert ordinary[0].display_name == "ordinary(int)"
    finally:
        cb.close()
        db.close()

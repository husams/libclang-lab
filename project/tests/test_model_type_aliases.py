"""Model API behavior for typedef/type-alias navigation.

These tests drive the real libclang extractor because alias and template
instance locations depend on clang cursor/extents, not just row shape.
"""

from __future__ import annotations

import os
import sys

import clang.cindex as cx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from _helpers import clang_args  # noqa: E402

from indexer.clang import ast as A  # noqa: E402
from indexer.model import CodeBase, Struct, Typedef  # noqa: E402
from indexer.query import GraphQuery  # noqa: E402
from indexer.storage import Storage  # noqa: E402


SOURCE = """\
struct Widget {};
typedef Widget WidgetAlias;
struct Gadget {};
using GadgetAlias = Gadget;
template <class T> struct MyTemplate {};
struct MyType {};
using XYZ = MyTemplate<MyType>;
"""


@pytest.fixture
def cb(tmp_path):
    path = tmp_path / "aliases.cpp"
    path.write_text(SOURCE)
    tu = cx.Index.create().parse(str(path), args=clang_args(str(path)) + ["-std=c++17"])
    errs = [d for d in tu.diagnostics if d.severity >= 3]
    assert not errs, f"parse errors: {errs}"

    db_path = tmp_path / "i.db"
    db = Storage(str(db_path))
    db.add_component("t", str(tmp_path))
    file_id = db.add_file_path(str(path))
    with db.transaction():
        A.index_symbols(db, tu, file_id)
    with db.transaction():
        db.delete_edges_for_file(file_id)
        A._index_edges_notxn(db, tu, str(path), file_id)
    db.close()

    codebase = CodeBase(GraphQuery(str(db_path)))
    yield codebase
    codebase.close()


def _one(cb: CodeBase, name: str):
    hits = cb.find(name)
    assert len(hits) == 1, hits
    return hits[0]


def _template_instance(cb: CodeBase):
    hits = [e for e in cb.find("MyTemplate") if e.display_name == "MyTemplate<MyType>"]
    assert len(hits) == 1, hits
    return hits[0]


def _primary_template(cb: CodeBase):
    hits = [e for e in cb.find("MyTemplate") if e.display_name == "MyTemplate<T>"]
    assert len(hits) == 1, hits
    return hits[0]


def test_typedef_entity_points_to_typedef_declaration(cb):
    alias = _one(cb, "WidgetAlias")
    target = _one(cb, "Widget")

    assert isinstance(alias, Typedef)
    assert alias.kind == "typedef"
    assert alias.location.line == 2 and alias.location.col == 1
    assert alias.definition is not None
    assert alias.definition.line == 2 and alias.definition.col == 1
    assert alias.source() == "typedef Widget WidgetAlias;"

    via_alias = alias.aliased()
    assert via_alias == target
    assert isinstance(via_alias, Struct)
    assert via_alias.location.line == 2 and via_alias.location.col == 1
    assert via_alias.definition is not None
    assert via_alias.definition.line == 2 and via_alias.definition.col == 1
    assert via_alias.source() == "typedef Widget WidgetAlias;"
    assert target.source() == "struct Widget {};"
    assert alias in target.aliased_by()


def test_using_type_alias_entity_points_to_alias_declaration(cb):
    alias = _one(cb, "GadgetAlias")
    target = _one(cb, "Gadget")

    assert isinstance(alias, Typedef)
    assert alias.kind == "type-alias"
    assert alias.location.line == 4 and alias.location.col == 1
    assert alias.definition is not None
    assert alias.definition.line == 4 and alias.definition.col == 1
    assert alias.source() == "using GadgetAlias = Gadget;"

    via_alias = alias.aliased()
    assert via_alias == target
    assert isinstance(via_alias, Struct)
    assert via_alias.location.line == 4 and via_alias.location.col == 1
    assert via_alias.definition is not None
    assert via_alias.definition.line == 4 and via_alias.definition.col == 1
    assert via_alias.source() == "using GadgetAlias = Gadget;"
    assert target.source() == "struct Gadget {};"
    assert alias in target.aliased_by()


def test_template_alias_entity_points_to_alias_declaration(cb):
    alias = _one(cb, "XYZ")
    target = alias.aliased()
    direct = _template_instance(cb)

    assert isinstance(alias, Typedef)
    assert alias.location.line == 7 and alias.location.col == 1
    assert alias.definition is not None
    assert alias.definition.line == 7 and alias.definition.col == 1
    assert alias.source() == "using XYZ = MyTemplate<MyType>;"

    assert target == direct
    assert target is not None
    assert target.kind == "struct"
    assert target.is_instantiation
    assert target.location.line == 7 and target.location.col == 1
    assert target.definition is not None
    assert target.definition.line == 7 and target.definition.col == 1
    assert target.source() == "using XYZ = MyTemplate<MyType>;"
    assert target.file is not None and alias.file is not None
    assert target.file.path == alias.file.path
    assert target.to_dict()["file"] == alias.file.path
    assert target.to_dict()["line"] == 7
    assert alias in target.aliased_by()


def test_direct_template_instance_keeps_real_template_source_not_alias_source(cb):
    alias = _one(cb, "XYZ")
    via_alias = alias.aliased()
    direct = _template_instance(cb)
    primary = _primary_template(cb)

    assert via_alias == direct
    assert via_alias != alias
    assert via_alias.source() == "using XYZ = MyTemplate<MyType>;"
    assert direct.location.line == 5 and direct.location.col == 1
    assert direct.definition == primary.definition
    assert direct.source() == "template <class T> struct MyTemplate {};"
    assert direct.to_dict()["line"] == 5

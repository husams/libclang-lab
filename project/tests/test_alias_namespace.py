"""End-to-end tests for Typedef.aliased() and the typed Namespace accessors.

Drives the REAL libclang extraction path (index_symbols + _index_edges_notxn)
on an inline C++ TU, so it locks two behaviours together:

  1. Typedef.aliased() resolves an alias to the *original* type it names --
     class / struct / enum / another alias -- AND, for an alias of a template
     specialization (``using IntBox = Box<int>;``), to the concrete ``Box<int>``
     INSTANCE node. The instance resolution is the regression guard for the
     extraction ordering fix: the TYPEDEF/TYPE_ALIAS handler must mint the
     instance BEFORE emitting the underlying-type ``uses`` edge (mirroring the
     FIELD_DECL/VAR_DECL handlers), otherwise the alias links to nothing.

  2. Namespace's typed member accessors (classes / class_templates / functions /
     function_templates / type_aliases / enums / variables / constants /
     namespaces) partition the namespace's members by kind.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from indexer.storage import Storage
from indexer.clang import ast as A
from indexer.clang import util as U
from indexer.query import GraphQuery
from indexer.model import (
    CodeBase,
    Namespace,
    Typedef,
    Class,
    Struct,
    Enum,
    _is_top_level_const,
)

SOURCE = r"""
namespace app {

class Widget { int v; };
struct Gadget { int g; };
enum Color { RED, GREEN };

template <class T> class Box { T item; };
template <class T> T identity(T x) { return x; }

int free_fn(int a) { return a; }

int global_var = 3;
const int kMax = 64;
constexpr double kPi = 3.14;
const char *name = "x";           // pointer-to-const: variable is mutable

typedef Widget WidgetAlias;
using GadgetAlias = Gadget;
using ColorAlias = Color;
using IntBox = Box<int>;           // alias of a template instance
typedef int Integer;              // alias of a builtin (no indexed target)
using AliasOfAlias = WidgetAlias; // alias of an alias (single-level)

namespace inner { int nested_fn() { return 0; } }

}  // namespace app
"""


@pytest.fixture
def ns():
    """Index SOURCE for real and return the `app` Namespace entity."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fixture.cpp")
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

        cb = CodeBase(GraphQuery(db_path))
        try:
            hits = [n for n in cb.find("app", kind="namespace") if n.name == "app"]
            assert hits, "namespace app not indexed"
            yield hits[0]
        finally:
            cb.close()


def _spellings(entities):
    return sorted(e.spelling for e in entities)


# --------------------------------------------------------------------------- #
# Namespace accessors
# --------------------------------------------------------------------------- #


def test_namespace_is_namespace(ns):
    assert isinstance(ns, Namespace)


def test_namespace_classes_exclude_templates_and_instances(ns):
    # plain records only -- NOT the Box template, NOT the Box<int> instance
    assert _spellings(ns.classes()) == ["Gadget", "Widget"]


def test_namespace_class_templates(ns):
    assert _spellings(ns.class_templates()) == ["Box"]


def test_namespace_functions_include_templates(ns):
    # documented contract: functions includes free function templates
    assert _spellings(ns.functions()) == ["free_fn", "identity"]


def test_namespace_function_templates_only(ns):
    assert _spellings(ns.function_templates()) == ["identity"]


def test_namespace_type_aliases(ns):
    assert _spellings(ns.type_aliases()) == [
        "AliasOfAlias",
        "ColorAlias",
        "GadgetAlias",
        "IntBox",
        "Integer",
        "WidgetAlias",
    ]


def test_namespace_enums(ns):
    assert _spellings(ns.enums()) == ["Color"]


def test_namespace_variables_are_all_variables(ns):
    assert _spellings(ns.variables()) == ["global_var", "kMax", "kPi", "name"]


def test_namespace_constants_are_top_level_const_subset(ns):
    # kMax (const int) and kPi (constexpr double) are constants; global_var is
    # mutable; `name` is a mutable pointer-to-const and must NOT count.
    assert _spellings(ns.constants()) == ["kMax", "kPi"]


def test_namespace_nested_namespaces(ns):
    assert _spellings(ns.namespaces()) == ["inner"]


# --------------------------------------------------------------------------- #
# name-contains filter on the accessors
# --------------------------------------------------------------------------- #


def test_members_filter_substring(ns):
    # substring match across the whole member set (case-insensitive)
    assert _spellings(ns.members("box")) == ["Box", "IntBox"]


def test_classes_filter(ns):
    assert _spellings(ns.classes("Widget")) == ["Widget"]
    assert ns.classes("Box") == []  # Box is a template, excluded from classes()


def test_class_templates_filter(ns):
    assert _spellings(ns.class_templates("Box")) == ["Box"]
    assert ns.class_templates("nope") == []


def test_type_aliases_filter(ns):
    # substring 'Alias' matches every *Alias name (not IntBox / Integer)
    assert _spellings(ns.type_aliases("Alias")) == [
        "AliasOfAlias",
        "ColorAlias",
        "GadgetAlias",
        "WidgetAlias",
    ]


def test_functions_filter(ns):
    assert _spellings(ns.functions("free")) == ["free_fn"]


def test_variables_filter_qualified_and_display(ns):
    # matches on qualified name too (app::kMax contains 'k')
    assert _spellings(ns.variables("kp")) == ["kPi"]


def test_filter_none_returns_all(ns):
    assert ns.classes(None) == ns.classes()


# --------------------------------------------------------------------------- #
# Typedef.aliased()
# --------------------------------------------------------------------------- #


def _alias(ns, spelling) -> Typedef:
    (a,) = [e for e in ns.type_aliases() if e.spelling == spelling]
    return a


def test_alias_to_class(ns):
    assert isinstance(_alias(ns, "WidgetAlias").aliased(), Class)


def test_alias_to_struct(ns):
    assert isinstance(_alias(ns, "GadgetAlias").aliased(), Struct)


def test_alias_to_enum(ns):
    assert isinstance(_alias(ns, "ColorAlias").aliased(), Enum)


def test_alias_to_builtin_is_none(ns):
    # typedef int Integer; -- underlying type is a builtin, nothing indexed
    assert _alias(ns, "Integer").aliased() is None


def test_alias_to_template_instance(ns):
    """The headline case: `using IntBox = Box<int>;` resolves to the concrete
    Box<int> instance node, not the primary template and not None."""
    target = _alias(ns, "IntBox").aliased()
    assert target is not None
    assert target.is_instantiation
    assert target.display_name == "app::Box<int>"
    assert [t.spelling for t in target.template_argument_types] == ["int"]


def test_alias_of_alias_is_single_level(ns):
    """An alias of an alias resolves to the next alias (as written), and
    chaining .aliased() walks to the final record."""
    aoa = _alias(ns, "AliasOfAlias").aliased()
    assert isinstance(aoa, Typedef)
    assert aoa.spelling == "WidgetAlias"
    assert isinstance(aoa.aliased(), Class)


# --------------------------------------------------------------------------- #
# _is_top_level_const helper (unit)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spelling,expected",
    [
        ("int", False),
        ("const int", True),
        ("const double", True),
        ("char *const", True),
        ("const char *", False),
        ("const char *const", True),
        ("int &", False),
    ],
)
def test_is_top_level_const(spelling, expected):
    assert _is_top_level_const(spelling) is expected

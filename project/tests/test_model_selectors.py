"""Tests for the typed, signature-aware selectors on the model layer
(``indexer.model.CodeBase``).

Builds a REAL index from an inline C++ TU (an inheritance chain with an
interface / abstract / concrete partitioning, overloaded free functions and
methods, a class template + function template, and explicit template
instantiations), then drives every selector calling convention:

  * ``cb.function`` in all three forms (parts / full-signature / ``ret(params)``)
  * ``cb.method`` with owner scoping and overload disambiguation
  * ``cb.klass`` / ``cb.struct`` / ``cb.record`` (concrete records only)
  * ``cb.interface`` / ``cb.abstract_class`` (class-kind partitioning)
  * ``cb.class_template`` / ``cb.function_template`` (primary templates)
  * ``cb.instance`` matched by the types used for the instantiation
  * the ``Signature`` parser + type-normalization edge cases
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from indexer.storage import Storage  # noqa: E402
from indexer.query import GraphQuery  # noqa: E402
from indexer.clang import ast as A  # noqa: E402
from indexer.clang import util as U  # noqa: E402
from indexer.model import (  # noqa: E402
    CodeBase,
    ClassKind,
    Signature,
    Function,
    Method,
    ClassTemplate,
    FunctionTemplate,
)


# An interface (Drawable, all-pure + virtual dtor), an abstract class (Shape:
# pure area() + state + a concrete method), and a concrete class (Circle).  A
# concrete struct (Point) and union (Value).  Overloaded free functions and
# overloaded + static methods.  A class template (Box<T>) with two explicit
# instantiations, and a free function template (identity<T>).
SOURCE = """
namespace app {

struct Drawable {                       // pure interface
    virtual void draw() const = 0;
    virtual ~Drawable() = default;
};

class Shape : public Drawable {         // abstract (pure + state + concrete fn)
    int id_;
public:
    virtual double area() const = 0;
    int id() const { return id_; }
};

class Circle : public Shape {           // concrete
    double r;
public:
    void draw() const override {}
    double area() const override { return r; }
};

struct Point { int x, y; int sum() const { return x + y; } };   // concrete struct
union Value { int i; float f; };                                // concrete union

int combine(int a, int b) { return a + b; }      // overload set
double combine(double a, double b) { return a + b; }
int combine(int a) { return a; }
const char *label(int code) { return ""; }
int px(app::Point p) { return p.x; }             // by-value namespaced param

struct Calc {
    int run(int n) const { return n; }           // overloaded method
    int run(int n, int m) const { return n + m; }
    void clear() {}
    static Calc make() { return {}; }            // static member
};

template <class T>
struct Box {
    T value;
    T get() const { return value; }
};

template <class T> T identity(T v) { return v; }

template struct Box<int>;
template struct Box<double>;

void driver() {
    Box<int> bi; bi.get();
    Box<double> bd; bd.get();
    identity<int>(1);
}

}  // namespace app
"""


@pytest.fixture
def cb():
    """A real index over SOURCE, wrapped as a CodeBase (read-only)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fixture.cpp")
        with open(path, "w") as fh:
            fh.write(SOURCE)
        tu = U.parse(path, args=["-std=c++17"], check=False)
        fatal = [d for d in tu.diagnostics if d.severity >= 3]
        assert not fatal, "; ".join(d.spelling for d in fatal)

        db = Storage(os.path.join(tmp, "i.db"))
        db.add_component("t", tmp)
        file_id = db.add_file_path(path)
        with db.transaction():
            A.index_symbols(db, tu, file_id)
        with db.transaction():
            db.delete_edges_for_file(file_id)
            A._index_edges_notxn(db, tu, path, file_id)

        code = CodeBase(GraphQuery.from_connection(db._conn))
        yield code
        db.close()


def _names(ents) -> list[str]:
    return sorted(e.name for e in ents)


# --------------------------------------------------------------------------- #
# Signature parser (unit-level, no index)
# --------------------------------------------------------------------------- #


def test_signature_from_parts():
    s = Signature.from_parts(ret="int", params=["int", "std::string"])
    assert s.ret == "int"
    assert s.params == ["int", "std::string"]
    # omitted dimensions stay unconstrained / explicit-empty
    assert Signature.from_parts().ret is None
    assert Signature.from_parts().params is None
    assert Signature.from_parts(params=[]).params == []


def test_signature_parse_ret_params():
    s = Signature.parse_ret_params("int(std::string)")
    assert s.ret == "int"
    assert s.params == ["std::string"]
    # void / empty argument lists collapse to zero params
    assert Signature.parse_ret_params("void()").params == []
    assert Signature.parse_ret_params("int(void)").params == []
    # an empty return leaves the return unconstrained
    assert Signature.parse_ret_params("(int)").ret is None


def test_signature_parse_full():
    s, name = Signature.parse_full("int app::func(int, std::string)")
    assert name == "app::func"
    assert s.ret == "int"
    assert s.params == ["int", "std::string"]
    # a return type carrying spaces inside <> is kept whole
    s2, name2 = Signature.parse_full("std::map<int, char> ns::f()")
    assert name2 == "ns::f"
    assert s2.ret == "std::map<int, char>"
    assert s2.params == []


def test_signature_parse_errors():
    with pytest.raises(ValueError):
        Signature.parse_ret_params("garbage")  # no (...)
    with pytest.raises(ValueError):
        Signature.parse_full("int app::f(")  # unbalanced
    with pytest.raises(ValueError):
        Signature.parse_full("(int)")  # no function name


# --------------------------------------------------------------------------- #
# Class-kind partitioning (klass / abstract_class / interface mutually excl.)
# --------------------------------------------------------------------------- #


def test_class_kind_properties(cb):
    circle = cb.klass("Circle")[0]
    shape = cb.abstract_class("Shape")[0]
    drawable = cb.interface("Drawable")[0]
    assert circle.class_kind is ClassKind.CONCRETE
    assert shape.class_kind is ClassKind.ABSTRACT
    assert drawable.class_kind is ClassKind.INTERFACE
    assert drawable.is_interface and not shape.is_interface
    assert not circle.is_interface


def test_klass_is_concrete_only(cb):
    assert _names(cb.klass("Circle")) == ["app::Circle"]
    # abstract / interface / not-a-class all excluded
    assert cb.klass("Shape") == []
    assert cb.klass("Drawable") == []
    assert cb.klass("Point") == []  # a struct, not a class
    assert cb.klass("Box") == []  # a class template, not a plain class


def test_interface_and_abstract_partition(cb):
    assert _names(cb.interface("Drawable")) == ["app::Drawable"]
    assert _names(cb.abstract_class("Shape")) == ["app::Shape"]
    # the three kinds are mutually exclusive
    assert cb.interface("Shape") == []  # abstract, not pure interface
    assert cb.interface("Circle") == []
    assert cb.abstract_class("Drawable") == []  # pure interface, not "abstract"
    assert cb.abstract_class("Circle") == []


def test_struct_record_union(cb):
    assert _names(cb.struct("Point")) == ["app::Point"]
    assert _names(cb.record("Point")) == ["app::Point"]
    assert _names(cb.record("Value")) == ["app::Value"]
    assert _names(cb.record("Circle")) == ["app::Circle"]
    # kind mismatch
    assert cb.struct("Circle") == []  # a class, not a struct
    assert cb.klass("Point") == []  # a struct, not a class


# --------------------------------------------------------------------------- #
# Functions — all three calling conventions
# --------------------------------------------------------------------------- #


def test_function_returns_all_overloads(cb):
    hits = cb.function("combine")
    assert len(hits) == 3
    assert all(isinstance(h, Function) for h in hits)


def test_function_form1_parts(cb):
    # narrow by params
    two_int = cb.function("combine", params=["int", "int"])
    assert len(two_int) == 1
    assert two_int[0].return_type.spelling == "int"
    one_int = cb.function("combine", params=["int"])
    assert len(one_int) == 1
    # narrow by return type
    dbl = cb.function("combine", ret="double")
    assert len(dbl) == 1
    assert dbl[0].arguments[0].spelling == "double"
    # ret + params together
    assert len(cb.function("combine", ret="int", params=["int", "int"])) == 1
    assert cb.function("combine", ret="double", params=["int", "int"]) == []


def test_function_form2_full_signature(cb):
    hits = cb.function("int app::combine(int, int)")
    assert len(hits) == 1
    assert hits[0].return_type.spelling == "int"
    assert [a.spelling for a in hits[0].arguments] == ["int", "int"]
    assert cb.function("double app::combine(double, double)")[0].return_type.spelling == "double"


def test_function_form3_ret_params_string(cb):
    hits = cb.function("combine", "double(double, double)")
    assert len(hits) == 1
    assert hits[0].return_type.spelling == "double"
    # a bare name + ret(params) on the single-arg overload
    assert len(cb.function("combine", "int(int)")) == 1


def test_function_qualified_vs_spelling(cb):
    assert len(cb.function("app::combine")) == 3  # qualified
    assert len(cb.function("combine")) == 3  # spelling
    assert cb.function("nope") == []


def test_function_excludes_methods(cb):
    # `run` is a member function — not a free function
    assert cb.function("run") == []


def test_empty_params_requires_zero_arity(cb):
    # label(int) has one parameter -> excluded by params=[]
    assert cb.function("label", params=[]) == []
    assert len(cb.function("label", params=["int"])) == 1


# --------------------------------------------------------------------------- #
# Methods — owner scoping + overloads + static
# --------------------------------------------------------------------------- #


def test_method_overloads_and_params(cb):
    assert len(cb.method("run")) == 2
    assert len(cb.method("run", params=["int"])) == 1
    assert len(cb.method("run", params=["int", "int"])) == 1
    assert all(isinstance(m, Method) for m in cb.method("run"))


def test_method_owner_scoping(cb):
    assert len(cb.method("run", owner="Calc")) == 2
    assert cb.method("run", owner="Box") == []  # Box has no run
    # owner as an entity object
    calc = cb.struct("Calc")[0]
    assert len(cb.method("run", owner=calc)) == 2
    # qualified method name
    assert len(cb.method("Calc::run")) == 2


def test_method_full_signature_form(cb):
    hits = cb.method("int app::Calc::run(int, int)")
    assert len(hits) == 1
    assert hits[0].owner.name == "app::Calc"


def test_static_method(cb):
    hits = cb.method("make", owner="Calc")
    assert len(hits) == 1
    assert hits[0].is_static is True


# --------------------------------------------------------------------------- #
# Templates — primary templates + instances
# --------------------------------------------------------------------------- #


def test_class_template(cb):
    hits = cb.class_template("Box")
    assert len(hits) == 1
    assert isinstance(hits[0], ClassTemplate)
    assert hits[0].kind == "class-template"
    assert cb.class_template("Circle") == []  # not a template


def test_function_template(cb):
    hits = cb.function_template("identity")
    assert len(hits) == 1
    assert isinstance(hits[0], FunctionTemplate)
    assert cb.function_template("combine") == []  # plain functions, not templates


def test_instance_by_display_string(cb):
    bi = cb.instance("Box<int>")
    assert len(bi) == 1
    assert [t.literal for t in bi[0].template_arguments] == ["int"]
    # qualified base accepted, same instance
    qi = cb.instance("app::Box<int>")
    assert len(qi) == 1 and qi[0].id == bi[0].id


def test_instance_by_args_kwarg(cb):
    bi = cb.instance("Box", args=["int"])
    bd = cb.instance("Box", args=["double"])
    assert len(bi) == 1 and len(bd) == 1
    assert bi[0].id != bd[0].id
    assert [t.literal for t in bd[0].template_arguments] == ["double"]


def test_instance_no_args_matches_all(cb):
    both = cb.instance("Box")
    assert len(both) == 2  # Box<int> and Box<double>
    # the primary template is NOT an instance
    assert all(not isinstance(e, ClassTemplate) for e in both)


def test_instance_no_match(cb):
    assert cb.instance("Box<long>") == []
    assert cb.instance("Box", args=["int", "int"]) == []  # wrong arity


# --------------------------------------------------------------------------- #
# Type-normalization edge cases
# --------------------------------------------------------------------------- #


def test_type_normalization_spacing(cb):
    # const char* return — tolerate spacing around '*'
    assert len(cb.function("label", ret="const char*")) == 1
    assert len(cb.function("label", ret="const char *")) == 1


def test_type_normalization_namespace_strip(cb):
    # px takes app::Point by value — match with the unqualified base name
    assert len(cb.function("px", params=["Point"])) == 1
    assert len(cb.function("px", params=["app::Point"])) == 1

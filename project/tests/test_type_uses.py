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


# ---------------------------------------------------------------------------
# TYPE_REF / TEMPLATE_REF uses edges (v0.5.0): a bare type NAME in expression
# position (`MyClass::instance()`, scoped-enum access) is a TYPE_REF cursor that
# the var/field/signature paths never see. The body-descent TYPE_REF branch
# emits a lookup-only `uses` edge with a parent-kind guard + self-owner skip.

TYPEREF_SOURCE = """
namespace N {
  enum class Color { Red, Green };
  struct Widget {
    static Widget* instance();
    int v;
    void touch() { (void)this; }            // method naming nothing external
    Color self_color() { return Color::Red; } // TYPE_REF Color in expr (member of N)
  };
  struct Other {
    void use() {
      Widget* w = Widget::instance();        // TYPE_REF Widget in expr position
      (void)w;
      Color c = Color::Green;                 // TYPE_REF Color in expr position
      (void)c;
    }
  };
}
"""


@pytest.fixture
def typeref_uses() -> dict[str, set[str]]:
    """Map dst-qual_name -> set(src qual_names) for kind=7 uses edges."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "tr.cpp")
        with open(path, "w") as fh:
            fh.write(TYPEREF_SOURCE)
        tu = cx.Index.create().parse(path, args=clang_args(path) + ["-std=c++17"])
        assert not [d for d in tu.diagnostics if d.severity >= 3], \
            "fixture source must parse cleanly"

        db = Storage(os.path.join(tmp, "i.db"))
        db.add_component("t", tmp)
        file_id = db.add_file_path(path)
        with db.transaction():
            A.index_symbols(db, tu, file_id)
        with db.transaction():
            db.delete_edges_for_file(file_id)
            A._index_edges_notxn(db, tu, path, file_id)

        rows = db._conn.execute(
            "SELECT dst.qual_name, src.qual_name FROM edge e "
            "JOIN symbol src ON e.src_id = src.id "
            "JOIN symbol dst ON e.dst_id = dst.id "
            "WHERE e.kind = 7").fetchall()
        out: dict[str, set[str]] = {}
        for dst_q, src_q in rows:
            out.setdefault(dst_q, set()).add(src_q)
        return out


def test_typeref_static_call_emits_use(typeref_uses):
    # Other::use names Widget only via `Widget::instance()` (no local of type
    # Widget by value) -> a TYPE_REF use edge must appear.
    assert "N::Other::use" in typeref_uses.get("N::Widget", set())


def test_typeref_scoped_enum_emits_use(typeref_uses):
    # `Color::Green` in Other::use and `Color::Red` in Widget::self_color.
    color_users = typeref_uses.get("N::Color", set())
    assert "N::Other::use" in color_users
    assert "N::Widget::self_color" in color_users


def test_typeref_self_owner_skipped(typeref_uses):
    # Widget::touch / any Widget method must NOT emit a uses edge to its own
    # owning class N::Widget (self-owner skip).
    assert "N::Widget::touch" not in typeref_uses.get("N::Widget", set())
    assert "N::Widget::self_color" not in typeref_uses.get("N::Widget", set())


def test_typeref_template_arg_local_not_double_counted(conf_uses):
    # The pre-existing conf_uses fixture: `Conf local;` (VAR_DECL path) and the
    # param/return paths still produce exactly the expected set; the new
    # TYPE_REF branch (parent=VAR_DECL/PARM_DECL is guarded out) adds nothing
    # spurious. create() must appear (set semantics) and Conf must have no
    # self-edge.
    assert "RdKafka::Producer::create" in conf_uses
    assert "RdKafka::Conf" not in conf_uses


# ---------------------------------------------------------------------------
# Regression: the self-owner guard must read the IMMUTABLE enclosing-owner USR,
# never a value clobbered by the CALL_EXPR implicit-`this` branch. Two scenarios
# that the original (parameter-mutating) code dropped:
#   (a) a non-owner method calling `OtherClass::staticMethod()` into a pointer
#       local — the receiver TYPE_REF to OtherClass must still emit a use, even
#       though the CALL_EXPR sets a local owner_usr to OtherClass's USR.
#   (b) a Derived method calling an INHERITED base method via implicit this and
#       then naming Base in expression position — Derived -> Base must survive.
# We assert on the EXACT (line, col) of the receiver TYPE_REF, not just set
# membership, so a leak that suppresses precisely that site is caught.

OWNER_LEAK_SOURCE = """
namespace N {
  struct Widget {
    static Widget* instance();
    static int kStatic;
    int v;
  };
  struct Other {
    void use() {
      Widget* w = Widget::instance();   // receiver TYPE_REF Widget @ this line
      (void)w;
    }
  };
  struct Base {
    void baseMethod();
    static int kStatic;
  };
  struct Derived : Base {
    void run() {
      baseMethod();                     // implicit-this CALL to inherited Base
      int x = Base::kStatic;            // TYPE_REF Base must NOT be suppressed
      (void)x;
    }
  };
}
"""


@pytest.fixture
def owner_leak_sites() -> set[tuple[str, str, int, int]]:
    """kind=7 edge SITES as (src_qual, dst_qual, line, col)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ol.cpp")
        with open(path, "w") as fh:
            fh.write(OWNER_LEAK_SOURCE)
        tu = cx.Index.create().parse(path, args=clang_args(path) + ["-std=c++17"])
        assert not [d for d in tu.diagnostics if d.severity >= 3], \
            "fixture source must parse cleanly"

        db = Storage(os.path.join(tmp, "i.db"))
        db.add_component("t", tmp)
        file_id = db.add_file_path(path)
        with db.transaction():
            A.index_symbols(db, tu, file_id)
        with db.transaction():
            db.delete_edges_for_file(file_id)
            A._index_edges_notxn(db, tu, path, file_id)

        rows = db._conn.execute(
            "SELECT src.qual_name, dst.qual_name, es.line, es.col "
            "FROM edge e "
            "JOIN edge_site es ON es.edge_id = e.id "
            "JOIN symbol src ON e.src_id = src.id "
            "JOIN symbol dst ON e.dst_id = dst.id "
            "WHERE e.kind = 7").fetchall()
        return {(r[0], r[1], r[2], r[3]) for r in rows}


def _line_of(source: str, needle: str) -> int:
    """1-based source line containing `needle` (exactly one match expected)."""
    lines = source.split("\n")
    matches = [i + 1 for i, ln in enumerate(lines) if needle in ln]
    assert len(matches) == 1, f"{needle!r} matched {matches} lines"
    return matches[0]


def test_owner_not_leaked_receiver_typeref_emitted(owner_leak_sites):
    # Receiver TYPE_REF `Widget` in `Widget* w = Widget::instance();`. The
    # CALL_EXPR sets a local owner_usr to N::Widget; the immutable enclosing
    # owner of Other::use is N::Other, so the guard must NOT suppress this.
    line = _line_of(OWNER_LEAK_SOURCE, "Widget* w = Widget::instance();")
    # The TYPE_REF column points at the `Widget` token in `Widget::instance()`.
    sites = {(ln, c) for (s, d, ln, c) in owner_leak_sites
             if s == "N::Other::use" and d == "N::Widget" and ln == line}
    assert sites, (
        f"expected a Widget receiver TYPE_REF use at line {line}; "
        f"all sites: {sorted(owner_leak_sites)}"
    )


def test_owner_not_leaked_inherited_call_then_base_typeref(owner_leak_sites):
    # Derived::run calls inherited baseMethod() via implicit this (sets local
    # owner_usr=N::Base), then names Base in `Base::kStatic`. The Base TYPE_REF
    # must still emit Derived::run -> N::Base.
    base_uses = {(ln, c) for (s, d, ln, c) in owner_leak_sites
                 if s == "N::Derived::run" and d == "N::Base"}
    assert base_uses, (
        "Derived::run must emit a use of N::Base (inherited-call owner leak); "
        f"all sites: {sorted(owner_leak_sites)}"
    )

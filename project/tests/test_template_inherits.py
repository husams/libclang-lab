"""Regression: a class template that inherits from a *concrete* base must emit
an ``inherits`` (edge kind 2) edge -- exactly like a plain class.

Bug: the CXX_BASE_SPECIFIER handler in ``clang/ast.py`` only accepted a
CLASS_DECL / STRUCT_DECL walk-parent, so a base specifier nested under a
CLASS_TEMPLATE (or its partial specialization) was silently dropped.  Every
inheritance query starting from such a template (`GraphQuery.bases`,
`CodeBase.class_template(...).parents`, the entity graph) returned an empty
parent list even though the source declares an unambiguous, non-dependent base.

This drives the REAL ast.py extraction path on ``manifests/template_inherit.cpp``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from indexer.storage import Storage  # noqa: E402
from indexer.clang import ast as A  # noqa: E402
from indexer.clang import util as U  # noqa: E402
from indexer import model as M  # noqa: E402

_MANIFEST = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "manifests", "template_inherit.cpp"
    )
)


def _index(tmp_path):
    """Index the fixture for real and return an on-disk Storage path."""
    tu = U.parse(_MANIFEST, args=["-std=c++17"], check=False)
    fatal = [d for d in tu.diagnostics if d.severity >= 3]
    assert not fatal, "fixture must parse cleanly: " + "; ".join(
        d.spelling for d in fatal
    )
    db_path = os.path.join(str(tmp_path), "i.db")
    db = Storage(db_path)
    db.add_component("t", os.path.dirname(_MANIFEST))
    file_id = db.add_file_path(_MANIFEST)
    with db.transaction():
        A.index_symbols(db, tu, file_id)
    with db.transaction():
        db.delete_edges_for_file(file_id)
        A._index_edges_notxn(db, tu, _MANIFEST, file_id)
    db.close()
    return db_path


def _inherits_pairs(db_path):
    db = Storage(db_path)
    rows = db._conn.execute(
        "SELECT s1.spelling AS src, s2.spelling AS dst "
        "FROM edge e JOIN symbol s1 ON s1.id=e.src_id "
        "JOIN symbol s2 ON s2.id=e.dst_id WHERE e.kind=2"
    ).fetchall()
    db.close()
    return {(r["src"], r["dst"]) for r in rows}


def _inherits_src_usrs(db_path, base_spelling="Rule"):
    """USRs of every derived symbol that inherits `base_spelling` (dedup by USR
    so the primary template and the partial specialization are distinguishable --
    they share the spelling 'RuleTemplate' but have distinct USRs)."""
    db = Storage(db_path)
    rows = db._conn.execute(
        "SELECT s1.usr AS src_usr "
        "FROM edge e JOIN symbol s1 ON s1.id=e.src_id "
        "JOIN symbol s2 ON s2.id=e.dst_id "
        "WHERE e.kind=2 AND s2.spelling=?",
        (base_spelling,),
    ).fetchall()
    db.close()
    return {r["src_usr"] for r in rows}


def test_class_template_emits_inherits_edge(tmp_path):
    """RuleTemplate<Adapter, NameType> : public Rule -> inherits(2) edge."""
    pairs = _inherits_pairs(_index(tmp_path))
    assert ("RuleTemplate", "Rule") in pairs, (
        "class-template base specifier dropped; got " + repr(sorted(pairs))
    )


def test_partial_specialization_emits_inherits_edge(tmp_path):
    """RuleTemplate<int, NameType> : public Rule -> inherits(2) edge.

    Asserted BY USR, not spelling: the partial specialization shares the
    spelling 'RuleTemplate' with the primary template, so a spelling-only check
    is satisfied by the primary alone and would miss the partial spec.  The
    primary's USR contains 'ST>' (a template); the partial spec's USR contains
    'SP>' (a partial specialization).  BOTH must inherit Rule.
    """
    src_usrs = _inherits_src_usrs(_index(tmp_path))
    assert any("ST>" in u for u in src_usrs), (
        "primary class-template missing inherits edge; got " + repr(sorted(src_usrs))
    )
    assert any("SP>" in u for u in src_usrs), (
        "partial specialization missing inherits edge; got "
        + repr(sorted(src_usrs))
    )


def test_plain_class_still_inherits(tmp_path):
    """Control: the plain derived class keeps its inherits edge."""
    pairs = _inherits_pairs(_index(tmp_path))
    assert ("PlainRule", "Rule") in pairs


def test_model_class_template_parents_nonempty(tmp_path):
    """End-to-end: CodeBase.class_template(...).parents surfaces the base."""
    cb = M.open_codebase(_index(tmp_path))
    templates = cb.class_template("RuleTemplate")
    assert templates, "RuleTemplate class template not found"
    parent_names = {p.name for t in templates for p in t.parents}
    assert any(n.split("::")[-1] == "Rule" for n in parent_names), (
        "class-template parents empty; got " + repr(sorted(parent_names))
    )

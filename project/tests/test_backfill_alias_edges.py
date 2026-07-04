"""Tests for the indexer.backfill_alias_edges maintenance utility.

Simulates a pre-v0.44.0 index (where a template-instance alias lost its
uses->instance edge) and asserts the targeted backfill restores exactly that
edge -- re-parsing only the alias-declaring file, leaving record/enum aliases
and their edge counts untouched.
"""

from __future__ import annotations

import argparse
import os
import tempfile

import pytest

from indexer.storage import Storage
from indexer.clang import ast as A
from indexer.clang import util as U
from indexer.query import GraphQuery, EDGE_KINDS
from indexer.model import CodeBase
from indexer import backfill_alias_edges as BF

SRC = r"""
namespace app {
template <class T> class Box { T item; };
struct Gadget {};
using IntBox = Box<int>;      // template-instance alias (loses its edge pre-0.44)
using GA = Gadget;            // record alias (keeps its edge)
typedef int Integer;          // builtin alias (never has an edge)
}
"""


def _build_index(tmp: str) -> str:
    """Index SRC with real flags/driver recorded, then DELETE the IntBox->Box<int>
    uses edge to simulate a stale pre-v0.44.0 index. Returns the db path."""
    path = os.path.join(tmp, "f.cpp")
    with open(path, "w") as fh:
        fh.write(SRC)
    tu = U.parse(path, args=["-std=c++17"], check=False)
    assert not [d for d in tu.diagnostics if d.severity >= 3]
    db_path = os.path.join(tmp, "i.db")
    db = Storage(db_path)
    db.add_component("app", tmp)
    fid = db.add_file_path(path, compile_options=["-std=c++17"], driver="c++")
    with db.transaction():
        A.index_symbols(db, tu, fid)
    with db.transaction():
        db.delete_edges_for_file(fid)
        A._index_edges_notxn(db, tu, path, fid)
    conn = db._conn
    ib = conn.execute(
        "SELECT id FROM symbol WHERE qual_name='app::IntBox'"
    ).fetchone()["id"]
    conn.execute(
        "DELETE FROM edge WHERE src_id=? AND kind=?", (ib, EDGE_KINDS["uses"])
    )
    conn.commit()
    db.close()
    return db_path


def _alias_targets(db_path: str) -> dict[str, str | None]:
    cb = CodeBase(GraphQuery(db_path))
    try:
        ns = [n for n in cb.find("app", kind="namespace") if n.name == "app"][0]
        return {
            a.spelling: (a.aliased().display_name if a.aliased() else None)
            for a in ns.type_aliases()
        }
    finally:
        cb.close()


def _uses_count(db_path: str, qual_name: str) -> int:
    db = Storage(db_path)
    try:
        row = db._conn.execute(
            "SELECT e.count AS c FROM edge e JOIN symbol s ON s.id = e.src_id "
            "WHERE s.qual_name = ? AND e.kind = ?",
            (qual_name, EDGE_KINDS["uses"]),
        ).fetchone()
        return row["c"] if row else 0
    finally:
        db.close()


def _args(db_path, apply):
    return argparse.Namespace(
        index=db_path, component=None, apply=apply, no_cache=True
    )


@pytest.fixture
def stale_db():
    with tempfile.TemporaryDirectory() as tmp:
        yield _build_index(tmp)


def test_candidates_are_only_unlinked_aliases(stale_db):
    db = Storage(stale_db)
    try:
        cands = BF.find_candidate_aliases(db)
        usrs = {u for s in cands.values() for u in s}
    finally:
        db.close()
    # IntBox (edge deleted) + Integer (builtin, never linked); GA is linked -> excluded
    assert "c:@N@app@IntBox" in usrs
    assert any("Integer" in u for u in usrs)
    assert not any(u.endswith("GA") or "@GA" in u for u in usrs)
    assert len(usrs) == 2  # exactly IntBox + Integer
    assert len(cands) == 1  # all candidates live in the single file


def test_stale_index_has_no_intbox_target(stale_db):
    before = _alias_targets(stale_db)
    assert before["IntBox"] is None
    assert before["GA"] == "app::Gadget"
    assert before["Integer"] is None


def test_dry_run_writes_nothing(stale_db):
    rc = BF.run(_args(stale_db, apply=False))
    assert rc == 0
    assert _alias_targets(stale_db)["IntBox"] is None  # unchanged


def test_apply_restores_template_instance_edge(stale_db):
    rc = BF.run(_args(stale_db, apply=True))
    assert rc == 0
    after = _alias_targets(stale_db)
    assert after["IntBox"] == "app::Box<int>"  # instance edge restored
    assert after["GA"] == "app::Gadget"        # untouched
    assert after["Integer"] is None            # builtin: no-op


def test_apply_is_idempotent_and_leaves_ga_count(stale_db):
    ga_before = _uses_count(stale_db, "app::GA")
    BF.run(_args(stale_db, apply=True))
    BF.run(_args(stale_db, apply=True))  # second run: nothing left to do
    # GA was never a candidate, so its edge count is not bumped by the backfill.
    assert _uses_count(stale_db, "app::GA") == ga_before
    # IntBox now linked, so a further run finds no candidate for it.
    db = Storage(stale_db)
    try:
        cands = BF.find_candidate_aliases(db)
        usrs = {u for s in cands.values() for u in s}
    finally:
        db.close()
    assert "c:@N@app@IntBox" not in usrs


def test_nothing_to_do_returns_zero():
    # a fresh (post-0.44) index already has the edge -> no candidates for IntBox,
    # but Integer (builtin) is always a candidate; assert the run still succeeds.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "f.cpp")
        with open(path, "w") as fh:
            fh.write("namespace app { struct S {}; }\n")  # no aliases at all
        tu = U.parse(path, args=["-std=c++17"], check=False)
        db_path = os.path.join(tmp, "i.db")
        db = Storage(db_path)
        db.add_component("app", tmp)
        fid = db.add_file_path(path, compile_options=["-std=c++17"], driver="c++")
        with db.transaction():
            A.index_symbols(db, tu, fid)
        with db.transaction():
            db.delete_edges_for_file(fid)
            A._index_edges_notxn(db, tu, path, fid)
        db.close()
        assert BF.run(_args(db_path, apply=True)) == 0

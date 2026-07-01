"""Virtual-dispatch caller edges (kind 18, ``dispatch_calls``).

A static ``calls`` edge into a virtual method B records the *declared* target
(e.g. ``execute() -> base::doSomething`` for a pure-virtual base). Asking for
callers of the concrete override ``child::doSomething`` therefore comes back
empty even though ``execute`` reaches it by dynamic dispatch. ``resolve``
materialises a ``dispatch_calls`` edge from every caller of B to every
transitive override of B; ``callers(M, include_overrides=True)`` reads it in one
hop. See the memory ``cidx-devirt-*`` and the base/child bug report.
"""

import os

from indexer.storage import Storage, Symbol
from indexer.query import GraphQuery, EDGE_KINDS


def _seed(db: Storage, repo: str, *, grandchild: bool = False) -> dict[str, int]:
    """base (pure doSomething + execute) <- child (override) [<- gchild (override)].

    ``execute()`` statically calls ``base::doSomething`` (the pure decl)."""
    comp = db.add_component("v", repo)
    root = db.add_directory(comp, "")
    f = db.add_file(root, "vdisp.cpp")
    ids: dict[str, int] = {}
    C = EDGE_KINDS

    def sym(key, usr, spelling, kind, *, pure=False, parent=None):
        ids[key] = db.add_symbol(
            Symbol(
                usr=usr,
                spelling=spelling,
                kind=kind,
                qual_name=spelling,
                file_id=f,
                line=1,
                col=1,
                is_definition=True,
                is_pure=pure,
                parent_usr=parent,
                resolved=True,
            )
        )

    sym("base", "c:@S@base", "base", "struct")
    sym("child", "c:@S@child", "child", "struct")
    sym("base::do", "c:@S@base@F@doSomething#", "doSomething", "method",
        pure=True, parent="c:@S@base")
    sym("child::do", "c:@S@child@F@doSomething#", "doSomething", "method",
        parent="c:@S@child")
    sym("execute", "c:@S@base@F@execute#", "execute", "method", parent="c:@S@base")
    if grandchild:
        sym("gchild", "c:@S@gchild", "gchild", "struct")
        sym("gchild::do", "c:@S@gchild@F@doSomething#", "doSomething", "method",
            parent="c:@S@gchild")

    with db.transaction():
        db.add_edge(ids["child"], ids["base"], C["inherits"], base_access=1)
        db.add_edge(ids["child::do"], ids["base::do"], C["overrides"])
        # execute() -> base::doSomething : the static edge libclang records
        db.add_edge(ids["execute"], ids["base::do"], C["calls"], count=1)
        if grandchild:
            db.add_edge(ids["gchild"], ids["child"], C["inherits"], base_access=1)
            db.add_edge(ids["gchild::do"], ids["child::do"], C["overrides"])
    return ids


def _resolved(tmp_path, **kw):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    db_path = str(tmp_path / "v.db")
    with Storage(db_path) as db:
        ids = _seed(db, repo, **kw)
        db.resolve_pass()
    return db_path, ids


def test_override_has_no_direct_callers(tmp_path):
    """The bug: the concrete override has no incoming ``calls`` edge; the
    direct-only view (opt out) is empty."""
    db_path, ids = _resolved(tmp_path)
    with GraphQuery(db_path) as g:
        assert g.callers(ids["child::do"], include_overrides=False) == []


def test_default_recovers_virtual_caller(tmp_path):
    """The fix: ``callers`` includes ``execute`` BY DEFAULT (no flag) after
    resolve materialises the dispatch_calls edge."""
    db_path, ids = _resolved(tmp_path)
    with GraphQuery(db_path) as g:
        assert [s.spelling for s in g.callers(ids["child::do"])] == ["execute"]


def test_base_direct_callers_unchanged(tmp_path):
    """The base method keeps its ordinary direct caller; no double-count (no
    dispatch_calls edge points AT the base, so default == direct)."""
    db_path, ids = _resolved(tmp_path)
    with GraphQuery(db_path) as g:
        assert [s.spelling for s in g.callers(ids["base::do"])] == ["execute"]
        assert [
            s.spelling
            for s in g.callers(ids["base::do"], include_overrides=False)
        ] == ["execute"]


def test_transitive_override_two_levels(tmp_path):
    """A call to base dispatches to a grandchild override two hops down --
    surfaced by default."""
    db_path, ids = _resolved(tmp_path, grandchild=True)
    with GraphQuery(db_path) as g:
        assert g.callers(ids["gchild::do"], include_overrides=False) == []
        assert [s.spelling for s in g.callers(ids["gchild::do"])] == ["execute"]


def test_dispatch_calls_edge_materialised(tmp_path):
    """resolve writes exactly the kind-18 edges: caller -> each override."""
    db_path, ids = _resolved(tmp_path, grandchild=True)
    with Storage(db_path) as db:
        rows = db._conn.execute(
            "SELECT src_id, dst_id FROM edge WHERE kind = 18"
        ).fetchall()
    pairs = {(r[0], r[1]) for r in rows}
    assert pairs == {
        (ids["execute"], ids["child::do"]),
        (ids["execute"], ids["gchild::do"]),
    }


def test_resolve_is_idempotent(tmp_path):
    """Re-running resolve rebuilds kind-18 without accumulating duplicates."""
    db_path, _ = _resolved(tmp_path, grandchild=True)
    with Storage(db_path) as db:
        n1 = db._conn.execute("SELECT COUNT(*) FROM edge WHERE kind = 18").fetchone()[0]
        db.resolve_pass()
        db.resolve_pass()
        n3 = db._conn.execute("SELECT COUNT(*) FROM edge WHERE kind = 18").fetchone()[0]
    assert n1 == n3 == 2


def test_direct_only_opt_out(tmp_path):
    """include_overrides=False restores the pre-dispatch direct-only view."""
    db_path, ids = _resolved(tmp_path, grandchild=True)
    with GraphQuery(db_path) as g:
        assert g.callers(ids["child::do"], include_overrides=False) == []
        assert g.callers(ids["gchild::do"], include_overrides=False) == []

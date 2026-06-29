"""v23 repository + clone layer: grouping components, switchable clones.

Hermetic -- everything goes through Storage's write API (no libclang), plus a
couple of end-to-end CLI checks driven through subprocess with an isolated
INDEXER_CACHE so the import path (repository auto-creation) and the
`repo switch` rebase are exercised against the real argument parser.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile

import pytest

from indexer.storage import SCHEMA_VERSION, Storage


# -- storage layer -----------------------------------------------------------


def test_fresh_schema_has_repository_clone_and_column():
    db = Storage(":memory:")
    try:
        ver = db._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert ver == str(SCHEMA_VERSION) == "23"
        tables = {
            r[0]
            for r in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"repository", "clone"} <= tables
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(component)")}
        assert "repository_id" in cols
    finally:
        db.close()


def test_add_repository_idempotent_and_remote_coalesce():
    db = Storage(":memory:")
    try:
        rid = db.add_repository("rdk", "repo", "git@h:edenhill/librdkafka.git")
        # Re-add with no remote must NOT wipe the stored one (COALESCE).
        assert db.add_repository("rdk") == rid
        repo = db.get_repository_by_name("rdk")
        assert repo is not None
        assert repo.remote_url == "git@h:edenhill/librdkafka.git"
        assert repo.kind == "repo"
        # Remote lookup finds it (clone-identity path).
        assert db.get_repository_by_remote(
            "git@h:edenhill/librdkafka.git"
        ).id == rid
    finally:
        db.close()


def test_add_clone_idempotent_on_path_and_active_pointer():
    db = Storage(":memory:")
    try:
        rid = db.add_repository("r")
        a = db.add_clone(rid, "/w/a", "main")
        assert db.add_clone(rid, "/w/a") == a  # idempotent on path
        b = db.add_clone(rid, "/w/b", "feat")
        db.set_active_clone(rid, a)
        assert db.get_repository_by_name("r").active_clone_id == a
        # Deleting a non-active clone leaves the pointer intact ...
        db.delete_clone(b)
        assert db.get_repository_by_name("r").active_clone_id == a
        # ... deleting the active clone clears it.
        db.delete_clone(a)
        assert db.get_repository_by_name("r").active_clone_id is None
    finally:
        db.close()


def test_rebase_components_prefix_rewrite():
    db = Storage(":memory:")
    try:
        rid = db.add_repository("r")
        a = db.add_clone(rid, "/w/a")
        db.set_active_clone(rid, a)
        c_root = db.add_component("r", "/w/a", "repo")
        c_sub = db.add_component("sub", "/w/a/src", "repo")
        c_ext = db.add_component("ext", "/opt/lib", "external")
        for c in (c_root, c_sub, c_ext):
            db.set_component_repository(c, rid)
        n = db.rebase_components(rid, "/w/a", "/w/b")
        assert n == 2  # the /opt/lib component is outside the old root -> skipped
        paths = sorted(c.path for c in db.components_for_repository(rid))
        assert paths == ["/opt/lib", "/w/b", "/w/b/src"]
    finally:
        db.close()


def test_rebase_skips_portable_paths():
    db = Storage(":memory:")
    try:
        rid = db.add_repository("r")
        c = db.add_component("p", "<LABEL>/inc", "external")
        db.set_component_repository(c, rid)
        assert db.rebase_components(rid, "/w/a", "/w/b") == 0
        assert db.get_component_by_id(c).path == "<LABEL>/inc"
    finally:
        db.close()


def test_delete_repository_detaches_components():
    db = Storage(":memory:")
    try:
        rid = db.add_repository("r")
        a = db.add_clone(rid, "/w/a")
        db.set_active_clone(rid, a)
        c = db.add_component("r", "/w/a", "repo")
        db.set_component_repository(c, rid)
        db.delete_repository(rid)
        # Component survives but is detached; clone cascaded away.
        assert db.get_component_by_id(c).repository_id is None
        assert db.list_clones() == []
        assert db.get_repository_by_name("r") is None
    finally:
        db.close()


def test_migration_recreates_dropped_repository_tables():
    """A v23 DB with the repository/clone tables dropped and the version rolled
    back is brought back to v23 with the tables recreated on reopen."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "i.db")
        Storage(path).close()  # fresh v23
        c = sqlite3.connect(path)
        c.execute("DROP TABLE clone")
        c.execute("DROP TABLE repository")
        c.execute("UPDATE meta SET value = '22' WHERE key = 'schema_version'")
        c.commit()
        c.close()
        db = Storage(path)
        try:
            ver = db._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            assert ver == "23"
            tables = {
                r[0]
                for r in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert {"repository", "clone"} <= tables
        finally:
            db.close()


# -- git worktree identity (no .git/config in the worktree) ------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def test_worktree_resolves_to_main_repo_name_and_remote(tmp_path):
    from indexer.utils import git_remote_url, git_root, repo_name

    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    _git(main, "remote", "add", "origin", "https://github.com/acme/coolproj.git")
    (main / "f.txt").write_text("hi")
    _git(main, "add", ".")
    _git(main, "commit", "-qm", "init")
    _git(main, "worktree", "add", "-q", str(tmp_path / "wt-feature"))

    wt = tmp_path / "wt-feature"
    # A linked worktree's .git is a FILE and carries no config of its own ...
    assert (wt / ".git").is_file()
    assert not (wt / ".git" / "config").exists()
    # ... yet both checkouts resolve to the same repo name + remote.
    assert git_root(str(wt)) == str(wt)
    assert repo_name(str(wt)) == "coolproj"
    assert git_remote_url(str(wt)) == "https://github.com/acme/coolproj.git"
    assert repo_name(str(main)) == repo_name(str(wt))


# -- backfill migration script -----------------------------------------------

_LAB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_backfill_groups_by_git_name_and_skips_non_git():
    from scripts.backfill_repositories import backfill_repositories

    db = Storage(":memory:")
    try:
        # Two components under the same git repo collapse to ONE repository by
        # git NAME; a component NOT inside any git repo is left UNGROUPED (no
        # bogus per-component repository).
        db.add_component("libclang-lab", _LAB_ROOT, "repo")
        db.add_component("proj", os.path.join(_LAB_ROOT, "project"), "repo")
        db.add_component("libfoo", "/opt/libfoo-not-on-disk", "external")
        stats = backfill_repositories(db)
        assert stats["assigned"] == 2
        assert stats["ungrouped"] == 1
        assert stats["repositories"] == 1  # only the git repo

        git_repo = db.get_repository_by_name("libclang-lab")
        assert git_repo is not None
        members = {c.name for c in db.components_for_repository(git_repo.id)}
        assert members == {"libclang-lab", "proj"}
        assert git_repo.remote_url and git_repo.remote_url.endswith(".git")

        # The non-git component is ungrouped, not its own repository.
        assert db.get_repository_by_name("libfoo") is None
        libfoo = next(c for c in db.list_components() if c.name == "libfoo")
        assert libfoo.repository_id is None

        # Deterministic rebuild: re-running yields the SAME end state.
        again = backfill_repositories(db)
        assert again["assigned"] == 2 and again["repositories"] == 1
    finally:
        db.close()


def test_backfill_cli_reports_and_exit_codes(tmp_path):
    # A real git repo so the component is git-rooted; plus a non-git component
    # that must be left ungrouped.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.c").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "i")

    db_path = str(tmp_path / "index.db")
    db = Storage(db_path)
    db.add_component("myrepo", str(repo), "repo")
    db.add_component("ext", "/opt/not-on-disk", "external")
    db.close()

    out = _cidx_script(db_path)
    assert out.returncode == 0, out.stderr
    assert "1 component(s) in 1 repository" in out.stdout
    assert "1 ungrouped" in out.stdout
    # Deterministic rebuild: second run reports the same.
    out2 = _cidx_script(db_path)
    assert out2.returncode == 0 and "1 component(s) in 1 repository" in out2.stdout
    # Missing DB is an error.
    miss = _cidx_script(str(tmp_path / "nope.db"))
    assert miss.returncode == 1 and "no index database" in miss.stderr


def _cidx_script(*args):
    return subprocess.run(
        [sys.executable, "scripts/backfill_repositories.py", *args],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )


# -- CLI end-to-end (subprocess, isolated cache) -----------------------------

_MANIFESTS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "manifests")
)
_CDB = os.path.join(_MANIFESTS, "compile_commands.json")


def _cidx(cache, *args):
    env = dict(os.environ, INDEXER_CACHE=cache)
    return subprocess.run(
        [sys.executable, "-m", "indexer", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )


@pytest.mark.skipif(not os.path.exists(_CDB), reason="manifests CDB absent")
def test_cli_import_groups_and_switch_rebases(tmp_path):
    cache = str(tmp_path / "cache")

    out = _cidx(cache, "import", "--db", _CDB)
    assert out.returncode == 0, out.stderr
    assert "repository '" in out.stdout

    # list components carries a repository column (id name kind ver repo path).
    lc = _cidx(cache, "list", "components")
    assert lc.returncode == 0, lc.stderr
    assert "libclang-lab" in lc.stdout

    # repo list / show.
    rl = _cidx(cache, "repo", "list")
    assert rl.returncode == 0 and "repositories" in rl.stdout
    rs = _cidx(cache, "repo", "show", "libclang-lab")
    assert rs.returncode == 0 and "active clone" in rs.stdout

    # add a second clone dir and switch to it: component paths rebase under it.
    wt = tmp_path / "wt"
    wt.mkdir()
    ac = _cidx(cache, "repo", "add-clone", "libclang-lab", str(wt), "--label", "feat")
    assert ac.returncode == 0, ac.stderr
    sw = _cidx(cache, "repo", "switch", "libclang-lab", "feat")
    assert sw.returncode == 0, sw.stderr
    assert "rebased" in sw.stdout

    lc2 = _cidx(cache, "list", "components")
    assert str(wt) in lc2.stdout

    # switch back by exact path.
    orig = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    sw2 = _cidx(cache, "repo", "switch", "libclang-lab", orig)
    assert sw2.returncode == 0, sw2.stderr

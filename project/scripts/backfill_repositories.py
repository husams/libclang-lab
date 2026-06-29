#!/usr/bin/env python3
"""Backfill the v23 repository/clone layer for an EXISTING cidx index.

Schema v23 adds `repository` + `clone` tables and `component.repository_id`, but
the in-place schema migration deliberately leaves existing components *ungrouped*
(`repository_id = NULL`) -- so a plain `cidx migrate` brings the schema forward
without populating the new layer. This script fills that layer in **without a
re-index / re-import**: it derives repositories purely from the component rows
already in the database (plus a best-effort read of each component's `.git`).

Rule -- per component, grouped by git NAME (mirrors the `import` identity logic):

  * For EACH component, take its path and find the enclosing git repo's **name**
    (worktree-aware: `git_root` follows a `.git` file, and the name/remote come
    from the shared config via gitdir->commondir). That name IS the repository;
    `add_repository` is idempotent on name, so every component of the same repo
    -- even across worktrees or different checkout paths -- lands in ONE
    repository, and each distinct checkout root is added as a clone of it.
  * No `.git` reachable (system / external include dirs that are not git repos):
    the component is **left ungrouped** -- we do NOT invent a repository from the
    component name (that produced hundreds of bogus one-off repositories).
  * Grouping is by NAME only -- never by path -- so the same repo never splits
    into many repositories.

The repository/clone tables are a pure function of the components, so the script
**rebuilds them from scratch** every run (clears the existing grouping first):
deterministic and safe to re-run, and it FIXES a previously mis-grouped index.
(Clones added manually via `cidx repo add-clone` are regenerated from component
roots -- re-add extra worktrees afterward.)

Usage:
    python3 -m scripts.backfill_repositories [INDEX_DB]
    python3 libclang-lab/project/scripts/backfill_repositories.py [INDEX_DB]

INDEX_DB defaults to the standard cidx cache index (the same path the CLI uses).
"""

from __future__ import annotations

import os
import sys

# Allow running as a loose script (python3 scripts/backfill_repositories.py)
# as well as a module (python3 -m scripts.backfill_repositories).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexer import pathx
from indexer.cli import index_path
from indexer.storage import Storage
from indexer.utils import git_remote_url, git_root, repo_name


def _git_identity(comp) -> tuple[str, str, str | None] | None:
    """(git_root, repository_name, remote_url) when the component's path is inside
    a git checkout (worktree-aware: `git_root` follows a `.git` file,
    `repo_name`/`git_remote_url` follow gitdir->commondir->shared config), else
    None -- the component is not part of any git repository."""
    eff = os.path.abspath(pathx.resolve_fs_path(Storage.effective_root(comp)))
    groot = git_root(eff)
    if not groot:
        return None
    return os.path.abspath(groot), repo_name(groot), git_remote_url(groot)


def backfill_repositories(db: Storage) -> dict:
    """(Re)build the repository/clone layer from the components, deterministically.

    For EACH component, find the **git repository** enclosing its path (worktree-
    aware) and assign the component to the repository of that git NAME. Grouping
    is by NAME only -- `add_repository` is idempotent on name -- so components in
    the same repo (even across worktrees / different checkout paths) land in ONE
    repository, with each distinct checkout root added as a clone. No path-based
    bucketing, so a repo never splits into many.

    A component NOT inside any git checkout (system / external include dirs that
    are not git repos) is **left ungrouped** -- we never invent a repository from
    a component's name, which is what produced hundreds of bogus repositories.

    The repository/clone tables are a pure function of the components, so this
    REBUILDS them from scratch each run (clears existing grouping first):
    deterministic, re-runnable, and it repairs a previously mis-grouped index.
    (Clones added manually via `cidx repo add-clone` are regenerated from the
    component roots; re-add extra worktrees afterward.)

    Returns a stats dict: components assigned, components left ungrouped, and the
    resulting repository/clone counts."""
    # Pure rebuild: wipe the derived layer, then re-derive from the components.
    db._conn.execute("UPDATE component SET repository_id = NULL")
    db._conn.execute("DELETE FROM clone")
    db._conn.execute("DELETE FROM repository")
    db._commit()

    assigned = 0
    ungrouped = 0
    for comp in db.list_components():
        ident = _git_identity(comp)
        if ident is None:
            ungrouped += 1  # not inside any git repo -> stays ungrouped
            continue
        clone_root, name, remote = ident
        # Group by NAME: idempotent on name, so same-named repos reuse one row.
        rid = db.add_repository(name, comp.kind, remote)
        clone_id = db.add_clone(rid, clone_root)
        repo = db.get_repository_by_id(rid)
        if repo is not None and repo.active_clone_id is None:
            db.set_active_clone(rid, clone_id)
        db.set_component_repository(comp.id, rid)
        assigned += 1

    return {
        "assigned": assigned,
        "ungrouped": ungrouped,
        "repositories": len(db.list_repositories()),
        "clones": len(db.list_clones()),
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Expand ~ / $VARS ourselves so a literal "~/.cache/..." that the shell did
    # not expand (quoting, some `uv run` paths) still resolves to the real file.
    path = argv[0] if argv else index_path()
    path = os.path.expanduser(os.path.expandvars(path))
    if not os.path.exists(path):
        print(f"error: no index database at {path}", file=sys.stderr)
        return 1
    # Opening through Storage applies the v23 schema migration first (adds the
    # repository/clone tables + component.repository_id), then we rebuild them.
    with Storage(path) as db:
        stats = backfill_repositories(db)
    print(
        f"backfilled {path}: {stats['assigned']} component(s) in "
        f"{stats['repositories']} repositor"
        f"{'y' if stats['repositories'] == 1 else 'ies'}, "
        f"{stats['clones']} clone(s); "
        f"{stats['ungrouped']} ungrouped (not in a git repo)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

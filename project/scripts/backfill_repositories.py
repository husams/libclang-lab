#!/usr/bin/env python3
"""Backfill the v23 repository/clone layer for an EXISTING cidx index.

Schema v23 adds `repository` + `clone` tables and `component.repository_id`, but
the in-place schema migration deliberately leaves existing components *ungrouped*
(`repository_id = NULL`) -- so a plain `cidx migrate` brings the schema forward
without populating the new layer. This script fills that layer in **without a
re-index / re-import**: it derives repositories purely from the component rows
already in the database (plus a best-effort read of each component's `.git`).

Grouping rule (mirrors the `import`/`add-source` identity logic):

  * Resolve each ungrouped component's effective root to an absolute path.
  * If a git checkout encloses it (a `.git` dir/file is found by walking up) AND
    the path exists on disk, the **git root** is the clone root and the
    repository name/remote come from `.git` (origin-URL basename, else dir name).
    Components sharing one git root collapse into ONE repository with ONE clone.
  * Otherwise the component's own root is the clone root and the repository name
    is the component name (1:1 -- typical for external libraries or for an index
    whose source tree is no longer present locally).
  * `add_repository` is idempotent on name, so two checkouts of a same-named repo
    naturally become one repository with two clones.

The first clone registered for a repository becomes its active clone. The script
is **idempotent**: components already attached to a repository are skipped, and
re-running it attaches only what is still ungrouped.

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


def _clone_root_and_identity(comp) -> tuple[str, str, str | None]:
    """(clone_root, repository_name, remote_url) for one component.

    Prefers the enclosing git checkout (so sub-components of one repo group
    together); falls back to the component's own root when there is no reachable
    `.git` -- e.g. external libraries or a relocated/portable index."""
    eff = os.path.abspath(pathx.resolve_fs_path(Storage.effective_root(comp)))
    groot = git_root(eff) if os.path.exists(eff) else None
    if groot:
        return os.path.abspath(groot), repo_name(groot), git_remote_url(groot)
    return eff, comp.name, None


def backfill_repositories(db: Storage) -> dict:
    """Populate repositories/clones from the existing components. Idempotent.

    Returns a stats dict: attached components, and the resulting repository and
    clone counts."""
    # Map an absolute clone root -> repository id, seeded with any clones that
    # already exist so a re-run reuses them instead of duplicating.
    root_to_repo: dict[str, int] = {}
    for cl in db.list_clones():
        root_to_repo.setdefault(os.path.abspath(cl.path), cl.repository_id)

    attached = 0
    for comp in db.list_components():
        if comp.repository_id is not None:
            continue  # already grouped
        clone_root, name, remote = _clone_root_and_identity(comp)
        rid = root_to_repo.get(clone_root)
        if rid is None:
            rid = db.add_repository(name, comp.kind, remote)
            clone_id = db.add_clone(rid, clone_root)
            repo = db.get_repository_by_id(rid)
            if repo is not None and repo.active_clone_id is None:
                db.set_active_clone(rid, clone_id)
            root_to_repo[clone_root] = rid
        db.set_component_repository(comp.id, rid)
        attached += 1

    return {
        "attached": attached,
        "repositories": len(db.list_repositories()),
        "clones": len(db.list_clones()),
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else index_path()
    if not os.path.exists(path):
        print(f"error: no index database at {path}", file=sys.stderr)
        return 1
    # Opening through Storage applies the v23 schema migration first (adds the
    # repository/clone tables + component.repository_id), then we populate them.
    with Storage(path) as db:
        before = sum(1 for c in db.list_components() if c.repository_id is None)
        stats = backfill_repositories(db)
    print(
        f"backfilled {path}: attached {stats['attached']} component(s) "
        f"({before} were ungrouped) -> {stats['repositories']} repositor"
        f"{'y' if stats['repositories'] == 1 else 'ies'}, "
        f"{stats['clones']} clone(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

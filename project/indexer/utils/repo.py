"""Git repository discovery: locate the repo root and derive a component name.

Worktree-aware: a *linked* git worktree has a `.git` **file** (not a directory)
containing `gitdir: <path>` that points at `…/.git/worktrees/<name>`; that gitdir
holds a `commondir` file pointing back to the main repository's `.git`, where the
shared `config` (and its `[remote "origin"]`) lives. The helpers below follow that
chain so a worktree resolves to the SAME repo name / remote as its main checkout.
"""

import configparser
import os


def git_root(path: str) -> str | None:
    """Walk up from path looking for a `.git` directory OR file (worktree)."""
    cur = os.path.abspath(path)
    while True:
        dot_git = os.path.join(cur, ".git")
        if os.path.isdir(dot_git) or os.path.isfile(dot_git):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _git_dir(root: str) -> str | None:
    """Resolve <root>/.git to the repo's git directory.

    A normal checkout has `.git` as a directory (returned as-is). A linked
    worktree has `.git` as a file `gitdir: <path>` -- the pointed-at directory is
    this worktree's private gitdir (`…/.git/worktrees/<name>`)."""
    dot_git = os.path.join(root, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    if os.path.isfile(dot_git):
        try:
            with open(dot_git, encoding="utf-8") as f:
                line = f.read().strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            gd = line[len("gitdir:"):].strip()
            if not os.path.isabs(gd):
                gd = os.path.normpath(os.path.join(root, gd))
            return gd
    return None


def _git_common_dir(root: str) -> str | None:
    """The shared git directory holding `config` -- for a normal repo that IS the
    `.git` dir; for a worktree it is the main repo's `.git`, reached via the
    `commondir` file in the worktree's private gitdir."""
    gd = _git_dir(root)
    if gd is None:
        return None
    commondir = os.path.join(gd, "commondir")
    if os.path.isfile(commondir):
        try:
            with open(commondir, encoding="utf-8") as f:
                cd = f.read().strip()
        except OSError:
            return gd
        if not os.path.isabs(cd):
            cd = os.path.normpath(os.path.join(gd, cd))
        return cd
    return gd


def git_remote_url(root: str) -> str | None:
    """The `origin` remote URL from the repo's shared `config` (worktree-aware),
    or None if unavailable."""
    common = _git_common_dir(root)
    if common is None:
        return None
    cfg = configparser.ConfigParser()
    try:
        cfg.read(os.path.join(common, "config"))
        return cfg.get('remote "origin"', "url")
    except (configparser.Error, OSError):
        return None


def repo_name(root: str) -> str:
    """Repository/component name: origin-url basename from the shared config; else
    the MAIN working-tree directory name (so a worktree shares its main repo's
    name); else `root`'s own basename."""
    url = git_remote_url(root)
    if url:
        name = url.rstrip("/").rsplit("/", 1)[-1]
        return name[: -len(".git")] if name.endswith(".git") else name
    common = _git_common_dir(root)
    if common is not None:
        # The main checkout is the parent of the shared `.git` directory.
        return os.path.basename(os.path.dirname(os.path.abspath(common)))
    return os.path.basename(root)

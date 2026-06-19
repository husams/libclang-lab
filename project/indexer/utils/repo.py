"""Git repository discovery: locate the repo root and derive a component name."""

import configparser
import os


def git_root(path: str) -> str | None:
    """Walk up from path looking for a .git directory."""
    cur = os.path.abspath(path)
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def repo_name(root: str) -> str:
    """Component name: remote origin url basename from .git/config, else dir name."""
    cfg_path = os.path.join(root, ".git", "config")
    cfg = configparser.ConfigParser()
    try:
        cfg.read(cfg_path)
        url = cfg.get('remote "origin"', "url")
        name = url.rstrip("/").rsplit("/", 1)[-1]
        return name[: -len(".git")] if name.endswith(".git") else name
    except (configparser.Error, OSError):
        return os.path.basename(root)

"""indexer.utils -- shared helpers for the cidx CLI.

hashing   md5_of: content hash for staleness detection
repo      git_root / repo_name: component discovery from .git
files     resolve_file_arg / index_status: file-argument + index-state logic
"""

from .files import index_status, resolve_file_arg
from .hashing import md5_of
from .repo import git_root, repo_name

__all__ = [
    "git_root",
    "index_status",
    "md5_of",
    "repo_name",
    "resolve_file_arg",
]

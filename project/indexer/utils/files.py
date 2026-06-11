"""File-argument resolution and index-state logic shared by CLI subcommands."""

import os

from ..storage import File
from .hashing import md5_of


def resolve_file_arg(arg: str, root: str | None = None) -> str:
    """Absolute path for a CLI file argument.

    Relative paths resolve against `root` (a component path, from --source)
    when given, else against the current directory.
    """
    if os.path.isabs(arg):
        return os.path.abspath(arg)
    return os.path.abspath(os.path.join(root or os.getcwd(), arg))


def index_status(rec: File, path: str) -> tuple[bool, str]:
    """(already indexed?, human reason). Indexed = flag set AND md5 current."""
    if not rec.indexed:
        return False, "no (never indexed)"
    if rec.md5 is None:
        return False, "no (no stored md5)"
    if rec.md5 != md5_of(path):
        return False, "no (content changed since import)"
    return True, "yes (indexed, md5 match)"

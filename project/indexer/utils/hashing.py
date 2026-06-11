"""Content hashing for staleness detection."""

import hashlib


def md5_of(path: str) -> str | None:
    """md5 hex digest of a file's current content, or None if unreadable."""
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return None

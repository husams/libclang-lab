"""indexer.pathx -- portable-path resolution utilities (v14).

Resolution chain (applied wherever a stored path becomes a real filesystem path):
    1. label_expand(stored, lookup, autoderive)  -- replace <name> placeholders
    2. expandvars(s)                             -- $VAR / ${VAR} (Python-semantics)
    3. expanduser(s)                             -- ~ / ~user
    4. normpath(s)                               -- collapses /./ / /../ etc.

Stored forms are NEVER expanded at import or display time; expansion is deferred
to parse/index/existence-check/longest-prefix time only.

This module MUST NOT import storage, cli, or compiledb (no import cycles).
"""

from __future__ import annotations

import os
import re
from typing import Callable

# ---------------------------------------------------------------------------
# Version-detection regex (§2 of the contract)
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^v?[0-9]+([._-][0-9]+)*$", re.ASCII)


def is_version_segment(segment: str) -> bool:
    """True iff *segment* looks like a version string (matches the detection regex)."""
    return bool(_VERSION_RE.match(segment))


def version_key(version: str) -> tuple[int, ...]:
    """Numeric per-segment sort key for a version string.

    Strips an optional leading 'v', splits on '.', '_' and '-', and maps each
    numeric field to an int so 18-0-0-275 > 18-0-0-100 > 18-0-0-11 (NOT a
    string compare). Non-numeric fields are dropped. Used to pick the highest
    version among same-named components / incoming compile-command paths.
    """
    v = version[1:] if version.startswith("v") else version
    return tuple(int(p) for p in re.split(r"[._-]", v) if p.isdigit())


def split_base_version(root: str) -> tuple[str, str | None]:
    """Split a component root into (base, version) by trailing-segment detection.

    Algorithm (§2):
      1. normpath(root)
      2. split into (head, tail)
      3. If tail matches the version regex AND head is non-empty AND head != '/'
         → return (head, tail)
      4. Else return (root, None)
    """
    root = os.path.normpath(root)
    base, seg = os.path.split(root)
    if seg and base and base != "/" and is_version_segment(seg):
        return base, seg
    return root, None


# ---------------------------------------------------------------------------
# expandvars -- exact port of CPython posixpath.expandvars (string branch)
# ---------------------------------------------------------------------------

# The regex captures $WORD or ${...}; semantics match Python os.path.expandvars
# with re.ASCII so only [A-Za-z0-9_] is a word character (never locale-tainted).
_EXPANDVARS_RE = re.compile(r"\$(\w+|\{[^}]*\}?)", re.ASCII)


def expandvars(path: str) -> str:
    """Expand $VAR and ${VAR} in *path* using Python os.path.expandvars semantics.

    - Undefined variable → left literal (the whole $VAR / ${VAR} text preserved).
    - Unterminated brace (${FOO without closing }) → left literal.
    - No $$ escaping: $$FOO with FOO=/x → $/x (first $ is a literal prefix).
    - Fast-path: if no '$' in path, return immediately without touching the regex.
    """
    if "$" not in path:
        return path

    def _repl(m: re.Match) -> str:
        name: str = m.group(1)
        if name.startswith("{"):
            if not name.endswith("}"):
                # Unterminated brace → leave literal
                return m.group(0)
            name = name[1:-1]
        val = os.environ.get(name)
        if val is None:
            # Undefined → leave literal
            return m.group(0)
        return val

    return _EXPANDVARS_RE.sub(_repl, path)


# ---------------------------------------------------------------------------
# label_expand -- replace <name> placeholders (§1.2)
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(r"<([^<>]*)>")

# Type alias for the registry lookup callable
LookupFn = Callable[[str], str | None]


def label_expand(
    token: str,
    lookup: LookupFn | None = None,
    autoderive: bool = True,
) -> str:
    """Replace every <name> placeholder in *token*.

    For each captured name:
      1. If lookup(name) returns a value → substitute it.
      2. Else if autoderive is True and name is non-empty → "/" + name.replace("-", "/").
      3. Else leave the <name> text literal.

    Surrounding text (e.g. '-I', trailing '/sub') is preserved unchanged.
    """

    def _repl(m: re.Match) -> str:
        name = m.group(1)
        # Registry lookup first
        if lookup is not None:
            val = lookup(name)
            if val is not None:
                return val
        # Autoderive fallback
        if autoderive and name:
            return "/" + name.replace("-", "/")
        # Leave literal
        return m.group(0)

    return _LABEL_RE.sub(_repl, token)


# ---------------------------------------------------------------------------
# resolve_fs_path -- the full resolution chain (§1.3)
# ---------------------------------------------------------------------------


def resolve_fs_path(
    stored: str,
    lookup: LookupFn | None = None,
    autoderive: bool = True,
) -> str:
    """Apply the full portable-path resolution chain to *stored*.

    Order (load-bearing):
      1. label_expand(stored, lookup, autoderive)
      2. expandvars(s)
      3. os.path.expanduser(s)
      4. os.path.normpath(s)

    abspath is applied by the CALLER when an absolute path is required;
    this function does NOT call getcwd().

    resolve_fs_path("") → normpath("") → "." (existing normpath behaviour;
    callers that store "" must guard as they already do).
    """
    s = label_expand(stored, lookup=lookup, autoderive=autoderive)
    s = expandvars(s)
    s = os.path.expanduser(s)
    s = os.path.normpath(s)
    return s

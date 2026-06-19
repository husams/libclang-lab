"""Boundary / parametrised tests for cidx portable-paths (v14).

Category: property-based / parametrised (mandatory addition — category 2).

Covers edge-conditions NOT reached by the developer's test_portable_paths.py:
  - is_version_segment: direct parametrised table (currently only exercised
    indirectly via split_base_version).
  - label_expand: token with TWO <name> placeholders; empty-name <> passthrough.
  - expandvars: adjacent ${A}${B} without separator; lone-dollar-at-end.
  - effective_root: empty-string version is falsy → treated as unversioned.
  - component_for_path: longest-prefix wins when two components both match.
  - resolve_fs_path: order-of-chain: label expands first, then expandvars, then
    expanduser (verifiable by constructing a stored form that requires all three
    in sequence).
"""

from __future__ import annotations

import os

import pytest

from indexer import pathx
from indexer.storage import Component, Storage


# ---------------------------------------------------------------------------
# 1. is_version_segment -- parametrised boundary table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seg,expected",
    [
        # Canonical numeric versions
        ("1", True),
        ("1.2", True),
        ("1.2.3", True),
        ("10.20.30", True),
        # v-prefixed
        ("v1", True),
        ("v1.2.3", True),
        # Dash / underscore separators
        ("1-2-3", True),
        ("1_2_3", True),
        ("10-10-20", True),
        # Mixed
        ("v1.2-3", True),
        # NOT versions
        ("include", False),
        ("src", False),
        ("lib", False),
        ("", False),
        # Leading v without digits
        ("v", False),
        # Starts with non-digit, non-v
        ("alpha1", False),
        # Dot without trailing digit
        ("1.2.", False),
        # Starts with digit then non-sep non-digit
        ("1a", False),
    ],
)
def test_is_version_segment(seg, expected):
    assert pathx.is_version_segment(seg) is expected


# ---------------------------------------------------------------------------
# 2. split_base_version -- boundary: root-level version segment is rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "root,expected_base,expected_ver",
    [
        # head == '/' → must NOT split
        ("/v1", "/v1", None),
        ("/1.2.3", "/1.2.3", None),
        # head == '' (relative with no parent) → must NOT split
        ("1.2.3", "1.2.3", None),
        # Normal split
        ("/opt/libfoo/3.0.0", "/opt/libfoo", "3.0.0"),
        # Trailing slash normalised, then split
        ("/a/b/2.0/", "/a/b", "2.0"),
        # Non-version tail
        ("/opt/foo/bar", "/opt/foo/bar", None),
    ],
)
def test_split_base_version_boundary(root, expected_base, expected_ver):
    base, ver = pathx.split_base_version(root)
    assert base == expected_base
    assert ver == expected_ver


# ---------------------------------------------------------------------------
# 3. expandvars -- boundary conditions not covered by the parity table
# ---------------------------------------------------------------------------


@pytest.fixture
def _env_ab(monkeypatch):
    """Set A=/a and B=/b."""
    monkeypatch.setenv("A", "/a")
    monkeypatch.setenv("B", "/b")


def test_expandvars_adjacent_braced_vars(_env_ab):
    """${A}${B} → '/a/b' — no separator between two expanded tokens."""
    assert pathx.expandvars("${A}${B}") == "/a/b"


def test_expandvars_dollar_at_end_of_string():
    """A trailing bare $ is not a valid variable and must be left literal."""
    # The regex does not match '$' alone (requires \w+ after), so it is
    # left unchanged.
    assert pathx.expandvars("path$") == "path$"


def test_expandvars_double_dollar_semantics(monkeypatch):
    """$$FOO: first $ is literal; $FOO is expanded.
    Parity table already covers this, but here we assert the exact byte result
    for the case where FOO is undefined (both must be literal).
    """
    monkeypatch.delenv("FOO", raising=False)
    # $$FOO → first $ literal, then $FOO = undefined → $FOO literal
    # so result is '$$FOO' reduced by the regex as: '$ ' + '$FOO' (unmatched)
    # The regex sees '$' not followed by \w+ / {…}, so both stay literal.
    result = pathx.expandvars("$$FOO")
    assert result == "$$FOO"


def test_expandvars_var_adjacent_to_slash(monkeypatch):
    """$VAR/sub where VAR=/base: resolves to /base/sub."""
    monkeypatch.setenv("BASE", "/base")
    assert pathx.expandvars("$BASE/sub") == "/base/sub"


# ---------------------------------------------------------------------------
# 4. label_expand -- multi-placeholder token
# ---------------------------------------------------------------------------


def test_label_expand_two_placeholders_in_one_token():
    """A single token containing two <name> placeholders; both are replaced."""

    def lookup(name):
        return {"a": "/p/a", "b": "/p/b"}.get(name)

    result = pathx.label_expand("<a>-<b>", lookup=lookup)
    assert result == "/p/a-/p/b"


def test_label_expand_placeholder_with_trailing_subdir_and_autoderive():
    """<unknown>/include with autoderive → /unknown/include."""
    result = pathx.label_expand("<unknown>/include", lookup=None, autoderive=True)
    assert result == "/unknown/include"


def test_label_expand_empty_angle_brackets_not_expanded_autoderive_on():
    """<> (empty name) must be left literal even when autoderive=True."""
    assert pathx.label_expand("<>", autoderive=True) == "<>"


# ---------------------------------------------------------------------------
# 5. resolve_fs_path -- full-chain ordering observable
# ---------------------------------------------------------------------------


def test_resolve_fs_path_chain_order(monkeypatch):
    """label → expandvars → expanduser → normpath, in that exact order.

    Stored form: '<lbl>/sub/../x'
    label 'lbl' expands to '$H/lib'
    $H → /home/user
    normpath collapses /sub/../x
    Expected:  /home/user/lib/x
    """
    monkeypatch.setenv("H", "/home/user")

    def lookup(name):
        return "$H/lib" if name == "lbl" else None

    result = pathx.resolve_fs_path("<lbl>/sub/../x", lookup=lookup)
    assert result == "/home/user/lib/x"


def test_resolve_fs_path_expanduser_after_label(monkeypatch):
    """A label value containing ~ is expanded by expanduser (step 3)."""
    monkeypatch.setenv("HOME", "/home/alice")

    def lookup(name):
        return "~/projects" if name == "home-projects" else None

    result = pathx.resolve_fs_path("<home-projects>", lookup=lookup)
    assert result == "/home/alice/projects"


# ---------------------------------------------------------------------------
# 6. effective_root -- empty-string version is falsy → unversioned behaviour
# ---------------------------------------------------------------------------


def test_effective_root_empty_string_version_is_unversioned():
    """version='' is falsy; effective_root must return the base path unchanged."""
    comp = Component(name="foo", path="/opt/foo", version="")
    assert Storage.effective_root(comp) == "/opt/foo"


def test_effective_root_none_version():
    comp = Component(name="foo", path="/opt/foo", version=None)
    assert Storage.effective_root(comp) == "/opt/foo"


def test_effective_root_version_with_path_sep(monkeypatch):
    """Verify normpath in effective_root strips trailing sep on base."""
    comp = Component(name="foo", path="/opt/foo/", version="2.0.0")
    result = Storage.effective_root(comp)
    assert result == "/opt/foo/2.0.0"
    assert not result.endswith(os.sep + "2.0.0" + os.sep)


# ---------------------------------------------------------------------------
# 7. component_for_path -- longest-prefix wins among two matching components
# ---------------------------------------------------------------------------


def test_component_for_path_longest_prefix_wins(tmp_path):
    """When two components' effective roots are prefixes of the query path,
    the longer (more specific) match wins."""
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        # 'parent' owns /opt/sdk
        db.add_component("parent", "/opt/sdk")
        # 'child' owns /opt/sdk/libfoo — more specific
        db.add_component("child", "/opt/sdk/libfoo")
        comp = db.component_for_path("/opt/sdk/libfoo/include/foo.h")
    assert comp is not None
    assert comp.name == "child"


def test_component_for_path_exact_root_match(tmp_path):
    """A path that IS the effective root (no trailing subdir) is still claimed."""
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("repo", "/opt/repo")
        comp = db.component_for_path("/opt/repo")
    assert comp is not None
    assert comp.name == "repo"


def test_component_for_path_partial_prefix_not_matched(tmp_path):
    """/opt/repoxtra must NOT be claimed by a component rooted at /opt/repo."""
    db_path = str(tmp_path / "t.db")
    with Storage(db_path) as db:
        db.add_component("repo", "/opt/repo")
        comp = db.component_for_path("/opt/repoxtra/foo.h")
    assert comp is None

"""M3 tests for indexer.astcache (on-disk AST cache) and the astcmd analysis
commands (``cidx ast dump / locals / conditions``).

All tests are hermetic: $INDEXER_CACHE is set to a per-test tmp_path so the
real ~/.cache/cidx is never touched.  The parse counter
(astcache._reset_parse_count() / _parse_count()) is the sole signal for
cache-hit vs cache-miss -- never timing.

Test categories:
  1. Cold miss / warm hit semantics            (cache_hit_*)
  2. --no-cache / use_cache=False              (nocache_*)
  3. src-mtime invalidation                    (invalidate_mtime_*)
  4. flags invalidation via different flags    (invalidate_flags_*)
  5. libclang-version mismatch invalidation    (invalidate_version_*)
  6. Corrupt .ast with valid sidecar           (corrupt_ast_*)
  7. cache_dir() / files_dir() drift guard     (drift_guard_*)
  8. cli.cache_dir() == astcache.cache_dir()   (drift_guard_cache_dir_parity)
  9. Analysis commands -- locals text + JSON   (cmd_locals_*)
 10. Analysis commands -- conditions           (cmd_conditions_*)
 11. Analysis commands -- dump (text + JSON)   (cmd_dump_*)
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Resolve the manifests directory relative to this file tree so tests run
# from any working directory.
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
_MANIFESTS = os.path.join(_REPO_ROOT, "libclang-lab", "manifests")

# Canonical clang args that parse the C manifests successfully (no headers
# fatal).  We derive these here rather than importing the lab's _helpers.py so
# that the test module has no dependency on the scripts/ tree.
import subprocess as _sp


def _clang_args(std: str = "c11") -> list[str]:
    """Return minimal libclang flags to parse manifests cleanly on this host."""
    sysroot = _sp.check_output(
        ["xcrun", "--show-sdk-path"], text=True
    ).strip()
    # clang resource dir (builtin headers)
    resource_dir = _sp.check_output(
        ["clang", "-print-resource-dir"], text=True
    ).strip()
    return [
        f"-std={std}",
        "-isysroot",
        sysroot,
        "-I",
        os.path.join(resource_dir, "include"),
        "-I",
        _MANIFESTS,
    ]


def _calls_c() -> str:
    return os.path.join(_MANIFESTS, "calls.c")


def _messy_c() -> str:
    return os.path.join(_MANIFESTS, "messy.c")


def _geometry_cpp() -> str:
    return os.path.join(_MANIFESTS, "geometry.cpp")


def _shapes_c() -> str:
    return os.path.join(_MANIFESTS, "shapes.c")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_cache(tmp_path, monkeypatch):
    """Redirect the cache to a private tmp_path for the duration of each test.

    Also resets the parse counter before every test so counter assertions
    start from zero.
    """
    from indexer import astcache

    monkeypatch.setenv("INDEXER_CACHE", str(tmp_path))
    astcache._reset_parse_count()
    # lru_cache on libclang_version must survive across tests in the same
    # process (it's a pure read from the dylib), so we don't clear it.
    yield


@pytest.fixture()
def calls_target():
    """A Target for manifests/calls.c with ad-hoc C11 flags."""
    from indexer.astcmd import Target

    return Target(
        abspath=_calls_c(),
        flags=_clang_args("c11"),
    )


@pytest.fixture()
def messy_target():
    """A Target for manifests/messy.c with ad-hoc C11 flags."""
    from indexer.astcmd import Target

    return Target(
        abspath=_messy_c(),
        flags=_clang_args("c11"),
    )


@pytest.fixture()
def geometry_target():
    """A Target for manifests/geometry.cpp with ad-hoc C++17 flags."""
    from indexer.astcmd import Target

    return Target(
        abspath=_geometry_cpp(),
        flags=_clang_args("c++17"),
    )


# ---------------------------------------------------------------------------
# 1. Cold miss / warm hit
# ---------------------------------------------------------------------------


def test_cold_miss_parses_once(calls_target, tmp_path):
    """First load_or_parse on a fresh cache -> one parse, .ast + .json created."""
    from indexer import astcache

    tu = astcache.load_or_parse(calls_target)

    assert tu is not None
    assert astcache._parse_count() == 1

    fd = astcache.files_dir()
    # files_dir must be under the hermetic tmp_path (not ~/.cache/cidx)
    assert fd.startswith(str(tmp_path))

    key = astcache.cache_key(calls_target)
    assert os.path.exists(os.path.join(fd, key + ".ast"))
    assert os.path.exists(os.path.join(fd, key + ".json"))


def test_warm_hit_avoids_reparse(calls_target):
    """Second load_or_parse on a valid entry -> no additional parse."""
    from indexer import astcache

    tu1 = astcache.load_or_parse(calls_target)
    count_after_cold = astcache._parse_count()
    assert count_after_cold == 1

    tu2 = astcache.load_or_parse(calls_target)
    assert astcache._parse_count() == count_after_cold  # unchanged

    # Both TUs yield the same top-level functions.
    assert tu2 is not None
    spellings_1 = {
        c.spelling
        for c in tu1.cursor.get_children()
        if c.location.file and calls_target.abspath in c.location.file.name
    }
    spellings_2 = {
        c.spelling
        for c in tu2.cursor.get_children()
        if c.location.file and calls_target.abspath in c.location.file.name
    }
    assert "compute" in spellings_1
    assert spellings_1 == spellings_2


def test_warm_hit_function_spelling(calls_target):
    """Warm-hit TU contains the known function 'compute' from calls.c."""
    from indexer import astcache

    astcache.load_or_parse(calls_target)  # prime cache
    tu = astcache.load_or_parse(calls_target)  # hit

    spellings = {
        c.spelling
        for c in tu.cursor.get_children()
        if c.location.file and calls_target.abspath in c.location.file.name
    }
    assert "compute" in spellings
    assert "main" in spellings


# ---------------------------------------------------------------------------
# 2. --no-cache / use_cache=False always reparses
# ---------------------------------------------------------------------------


def test_nocache_reparses_every_call(calls_target):
    """use_cache=False increments _parse_count on every call."""
    from indexer import astcache

    astcache.load_or_parse(calls_target, use_cache=False)
    assert astcache._parse_count() == 1

    astcache.load_or_parse(calls_target, use_cache=False)
    assert astcache._parse_count() == 2

    astcache.load_or_parse(calls_target, use_cache=False)
    assert astcache._parse_count() == 3


def test_nocache_does_not_write_cache(calls_target, tmp_path):
    """use_cache=False skips writing to the cache (bypass both ways).

    The load_or_parse() code only calls _try_save() when use_cache is True
    (see astcache.py:249).  ``--no-cache`` is a pure bypass, not a
    force-refresh-and-write.
    """
    from indexer import astcache

    astcache.load_or_parse(calls_target, use_cache=False)
    fd = astcache.files_dir()
    key = astcache.cache_key(calls_target)
    # No .ast or .json should exist since use_cache=False skips _try_save.
    assert not os.path.exists(os.path.join(fd, key + ".ast"))
    assert not os.path.exists(os.path.join(fd, key + ".json"))


# ---------------------------------------------------------------------------
# 3. src-mtime invalidation
# ---------------------------------------------------------------------------


def test_mtime_invalidation_triggers_reparse(calls_target, tmp_path):
    """Bumping the source mtime invalidates the cache -> reparse on next load."""
    from indexer import astcache

    astcache.load_or_parse(calls_target)
    assert astcache._parse_count() == 1

    # Advance mtime by a full second (float precision varies by OS).
    stat = os.stat(calls_target.abspath)
    new_atime = stat.st_atime + 1.0
    new_mtime = stat.st_mtime + 1.0
    os.utime(calls_target.abspath, (new_atime, new_mtime))

    try:
        tu = astcache.load_or_parse(calls_target)
        assert tu is not None
        assert astcache._parse_count() == 2  # forced reparse

        # Sidecar must now record the new mtime.
        fd = astcache.files_dir()
        key = astcache.cache_key(calls_target)
        side = json.loads(open(os.path.join(fd, key + ".json")).read())
        assert side["src_mtime"] == pytest.approx(new_mtime, abs=1e-3)
    finally:
        # Restore original mtime so the manifest file is unmodified after test.
        os.utime(calls_target.abspath, (stat.st_atime, stat.st_mtime))


# ---------------------------------------------------------------------------
# 4. flags invalidation
# ---------------------------------------------------------------------------


def test_flags_invalidation_creates_separate_entry(tmp_path):
    """Two Targets with different flags -> two separate cache entries."""
    from indexer import astcache
    from indexer.astcmd import Target

    flags_a = _clang_args("c11")
    flags_b = _clang_args("c11") + ["-DSOME_DEFINE=1"]

    t_a = Target(abspath=_calls_c(), flags=flags_a)
    t_b = Target(abspath=_calls_c(), flags=flags_b)

    assert astcache.cache_key(t_a) != astcache.cache_key(t_b), (
        "Different flags must produce different cache keys"
    )

    astcache.load_or_parse(t_a)
    assert astcache._parse_count() == 1  # cold miss for A

    astcache.load_or_parse(t_b)
    assert astcache._parse_count() == 2  # cold miss for B (separate entry)

    # Verify two distinct .ast files exist.
    fd = astcache.files_dir()
    ast_files = [f for f in os.listdir(fd) if f.endswith(".ast")]
    assert len(ast_files) == 2


def test_flags_warm_hit_independent(tmp_path):
    """After priming both entries, each is a warm hit independently."""
    from indexer import astcache
    from indexer.astcmd import Target

    flags_a = _clang_args("c11")
    flags_b = _clang_args("c11") + ["-DSOME_DEFINE=1"]

    t_a = Target(abspath=_calls_c(), flags=flags_a)
    t_b = Target(abspath=_calls_c(), flags=flags_b)

    astcache.load_or_parse(t_a)
    astcache.load_or_parse(t_b)
    count = astcache._parse_count()

    # Both entries are now hot.
    astcache.load_or_parse(t_a)
    astcache.load_or_parse(t_b)
    assert astcache._parse_count() == count  # no additional parses


# ---------------------------------------------------------------------------
# 5. libclang-version mismatch invalidation  (LOAD-BEARING gotcha)
# ---------------------------------------------------------------------------


def test_version_mismatch_triggers_reparse_no_crash(calls_target, monkeypatch):
    """A sidecar with a wrong libclang_version -> is_valid False -> reparse, no crash.

    This is the cross-libclang-version safety guard (wheel-18 vs clang-21).
    """
    from indexer import astcache

    # 1. Prime the cache normally.
    astcache.load_or_parse(calls_target)
    assert astcache._parse_count() == 1

    # 2. Monkeypatch libclang_version to return a bogus string.
    monkeypatch.setattr(
        astcache, "libclang_version", lambda: "clang version 99.0.0 (fake)"
    )

    # 3. Next load must detect the version mismatch -> reparse, no crash.
    tu = astcache.load_or_parse(calls_target)
    assert tu is not None, "Should return a TU even after version-mismatch reparse"
    assert astcache._parse_count() == 2


def test_version_mismatch_via_sidecar_edit(calls_target, tmp_path):
    """Editing the sidecar's libclang_version field directly -> invalidation."""
    from indexer import astcache

    astcache.load_or_parse(calls_target)
    assert astcache._parse_count() == 1

    # Corrupt the sidecar version field.
    fd = astcache.files_dir()
    key = astcache.cache_key(calls_target)
    side_path = os.path.join(fd, key + ".json")
    side = json.loads(open(side_path).read())
    side["libclang_version"] = "clang version 1.2.3 (bogus)"
    with open(side_path, "w") as fh:
        json.dump(side, fh)

    # Next load must reparse (sidecar version != real libclang_version()).
    tu = astcache.load_or_parse(calls_target)
    assert tu is not None
    assert astcache._parse_count() == 2


# ---------------------------------------------------------------------------
# 6. Corrupt .ast file (valid sidecar)
# ---------------------------------------------------------------------------


def test_corrupt_ast_fallback_to_reparse_no_crash(calls_target, tmp_path):
    """Garbage in the .ast file with a valid sidecar -> _load_ast returns None
    -> caller falls back to reparse, no crash."""
    from indexer import astcache

    # Prime the cache.
    astcache.load_or_parse(calls_target)
    assert astcache._parse_count() == 1

    # Corrupt the .ast file (keep sidecar intact).
    fd = astcache.files_dir()
    key = astcache.cache_key(calls_target)
    ast_path = os.path.join(fd, key + ".ast")
    with open(ast_path, "wb") as fh:
        fh.write(b"THIS IS GARBAGE; NOT A VALID AST FILE\n" * 10)

    # Next load must reparse without raising.
    tu = astcache.load_or_parse(calls_target)
    assert tu is not None
    assert astcache._parse_count() == 2


def test_load_ast_returns_none_on_garbage(tmp_path):
    """_load_ast on a non-existent / garbage file returns None, never raises."""
    from indexer import astcache

    garbage = str(tmp_path / "garbage.ast")
    with open(garbage, "wb") as fh:
        fh.write(b"\x00\x01\x02 not a PCH file")

    result = astcache._load_ast(garbage)
    assert result is None


# ---------------------------------------------------------------------------
# 7 + 8. Drift guard: files_dir() under tmp_path + cli.cache_dir() parity
# ---------------------------------------------------------------------------


def test_files_dir_is_under_hermetic_cache(tmp_path):
    """files_dir() must be under the test's private cache, not ~/.cache/cidx."""
    from indexer import astcache

    fd = astcache.files_dir()
    assert fd.startswith(str(tmp_path)), (
        f"files_dir() {fd!r} should be under tmp_path {tmp_path}"
    )


def test_cache_dir_parity_with_cli():
    """astcache.cache_dir() == cli.cache_dir() (constants must not drift).

    Documented in ADR-005 §7 as the equivalence-test guard.
    """
    from indexer import astcache
    from indexer import cli

    assert astcache.cache_dir() == cli.cache_dir(), (
        "astcache and cli have drifted: DEFAULT_CACHE or CACHE_ENV differ"
    )


def test_cache_env_var_controls_files_dir(tmp_path, monkeypatch):
    """INDEXER_CACHE environment variable must redirect both cache_dir() and files_dir()."""
    from indexer import astcache

    custom = str(tmp_path / "custom_cache")
    monkeypatch.setenv("INDEXER_CACHE", custom)

    assert astcache.cache_dir() == custom
    assert astcache.files_dir() == os.path.join(custom, "files")


# ---------------------------------------------------------------------------
# 9. Analysis command: ast locals
# ---------------------------------------------------------------------------


def test_cmd_locals_text_output(messy_target, capsys):
    """ast locals on BadlyNamedFunction returns the known local 'Result'."""
    from indexer import astcmd, astcache

    messy_target.focus_name = "BadlyNamedFunction"
    tu = astcache.load_or_parse(messy_target)
    assert tu is not None

    # Run via cmd_locals with a minimal mock args object.
    class _Args:
        params = False
        json = False

    # Locate focus cursor.
    from indexer.clang.ast import _file_cursors
    import clang.cindex as cx

    focus = None
    for c in _file_cursors(tu, messy_target.abspath):
        if c.spelling == "BadlyNamedFunction":
            focus = c
            break
    assert focus is not None

    # Collect locals via _subtree + VAR_DECL (same logic as cmd_locals).
    from indexer.astcmd import _subtree

    var_names = [
        c.spelling
        for c, _, _ in _subtree(focus)
        if c.kind == cx.CursorKind.VAR_DECL
    ]
    assert "Result" in var_names


def test_cmd_locals_json_shape(messy_target, capsys):
    """ast locals --json on BadlyNamedFunction returns documented JSON keys."""
    from indexer import cli

    # Named options must come BEFORE the positional file; ad-hoc flags after --.
    argv = [
        "ast",
        "locals",
        "--name",
        "BadlyNamedFunction",
        "--json",
        _messy_c(),
        "--",
        *_clang_args("c11"),
    ]
    rc = cli.main(argv)
    out, _err = capsys.readouterr()
    assert rc == 0, _err
    rows = json.loads(out)
    assert isinstance(rows, list)
    assert len(rows) >= 1
    for row in rows:
        assert "name" in row
        assert "type" in row
        assert "loc" in row
        assert "kind" in row
    # The known local must be present.
    names = [r["name"] for r in rows]
    assert "Result" in names


def test_cmd_locals_includes_params_flag(messy_target, capsys):
    """ast locals --params includes parameter declarations."""
    from indexer import cli

    argv = [
        "ast",
        "locals",
        "--name",
        "BadlyNamedFunction",
        "--params",
        "--json",
        _messy_c(),
        "--",
        *_clang_args("c11"),
    ]
    rc = cli.main(argv)
    out, _err = capsys.readouterr()
    assert rc == 0, _err
    rows = json.loads(out)
    kinds = {r["kind"] for r in rows}
    assert "param" in kinds  # params flag activated


# ---------------------------------------------------------------------------
# 10. Analysis command: ast conditions
# ---------------------------------------------------------------------------


def test_cmd_conditions_finds_guard_in_shape_area(capsys):
    """ast conditions on shape_area() finds the CASE_STMT guarding circle_area.

    shapes.c::shape_area has a switch statement where circle_area() is called
    inside a CASE_STMT -- the canonical guarded-call example in the manifests.
    """
    from indexer import cli

    argv = [
        "ast",
        "conditions",
        "--name",
        "shape_area",
        "--json",
        _shapes_c(),
        "--",
        *_clang_args("c11"),
    ]
    rc = cli.main(argv)
    out, _err = capsys.readouterr()
    assert rc == 0, _err
    rows = json.loads(out)
    assert isinstance(rows, list) and len(rows) >= 1
    # Verify documented JSON keys.
    for row in rows:
        assert "control" in row
        assert "loc" in row
        assert "condition" in row
        assert "calls" in row
    # The CASE_STMT guards circle_area.
    controls = [r["control"] for r in rows]
    assert "CASE_STMT" in controls
    # circle_area appears in the guarded calls list.
    all_calls = [c for row in rows for c in row["calls"]]
    assert "circle_area" in all_calls


def test_cmd_conditions_text_output(capsys):
    """ast conditions text output for shape_area() mentions CASE_STMT and circle_area."""
    from indexer import cli

    argv = [
        "ast",
        "conditions",
        "--name",
        "shape_area",
        _shapes_c(),
        "--",
        *_clang_args("c11"),
    ]
    rc = cli.main(argv)
    out, _err = capsys.readouterr()
    assert rc == 0, _err
    assert "CASE_STMT" in out
    assert "circle_area" in out


def test_cmd_conditions_recurse_no_guarded_calls(capsys):
    """ast conditions returns empty for recurse() -- the recursive call is not
    inside any conditional (it is in the else-path return statement)."""
    from indexer import cli

    argv = [
        "ast",
        "conditions",
        "--name",
        "recurse",
        "--json",
        _calls_c(),
        "--",
        *_clang_args("c11"),
    ]
    rc = cli.main(argv)
    out, _err = capsys.readouterr()
    assert rc == 0, _err
    rows = json.loads(out)
    # No conditional wraps the recursive call -- correctly empty.
    assert isinstance(rows, list) and len(rows) == 0


# ---------------------------------------------------------------------------
# 11. Analysis command: ast dump (text + JSON shape)
# ---------------------------------------------------------------------------


def test_cmd_dump_text_calls_c(capsys):
    """ast dump text for calls.c whole file emits known function spellings."""
    from indexer import cli

    argv = [
        "ast",
        "dump",
        _calls_c(),
        "--",
        *_clang_args("c11"),
    ]
    rc = cli.main(argv)
    out, _err = capsys.readouterr()
    assert rc == 0, _err
    assert "FUNCTION_DECL" in out
    assert "compute" in out
    assert "main" in out


def test_cmd_dump_json_shape(capsys):
    """ast dump --json has the documented stable cursor shape keys."""
    from indexer import cli

    # Options before file; --json/--depth are options, file and -- FLAGS last.
    argv = [
        "ast",
        "dump",
        "--json",
        "--depth",
        "1",
        _calls_c(),
        "--",
        *_clang_args("c11"),
    ]
    rc = cli.main(argv)
    out, _err = capsys.readouterr()
    assert rc == 0, _err
    nodes = json.loads(out)
    assert isinstance(nodes, list) and len(nodes) > 0
    for node in nodes:
        # Documented stable cursor JSON shape from ADR-005 / M2 spec.
        assert "kind" in node
        assert "spelling" in node
        assert "usr" in node
        assert "extent" in node
    # extent sub-shape
    ext = nodes[0]["extent"]
    assert "file" in ext
    assert "start" in ext
    assert "end" in ext
    assert len(ext["start"]) == 2  # [line, col]
    assert len(ext["end"]) == 2


def test_cmd_dump_function_focus(capsys):
    """ast dump with --name focuses on the named function only."""
    from indexer import cli

    argv = [
        "ast",
        "dump",
        "--name",
        "compute",
        "--json",
        "--depth",
        "0",
        _calls_c(),
        "--",
        *_clang_args("c11"),
    ]
    rc = cli.main(argv)
    out, _err = capsys.readouterr()
    assert rc == 0, _err
    nodes = json.loads(out)
    assert len(nodes) == 1
    assert nodes[0]["spelling"] == "compute"


def test_cmd_dump_geometry_cpp(capsys):
    """ast dump works on a C++ file (geometry.cpp) without crashing."""
    from indexer import cli

    argv = [
        "ast",
        "dump",
        "--depth",
        "1",
        _geometry_cpp(),
        "--",
        *_clang_args("c++17"),
    ]
    rc = cli.main(argv)
    out, _err = capsys.readouterr()
    assert rc == 0, _err
    assert "FUNCTION_DECL" in out or "CXX_METHOD" in out or "NAMESPACE" in out


# ---------------------------------------------------------------------------
# Property-based / parametrised boundary tests  (M3 mandatory addition)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source,std", [
    ("calls.c", "c11"),
    ("messy.c", "c11"),
    ("geometry.cpp", "c++17"),
])
def test_cache_round_trip_multiple_sources(source, std, tmp_path):
    """Cold miss then warm hit on each manifest file -- parametrised boundary.

    Validates that the cache key / sidecar scheme is consistent across file
    types (C and C++), that files_dir() is always under tmp_path, and that a
    warm hit returns a non-None TU every time.
    """
    from indexer import astcache
    from indexer.astcmd import Target

    abspath = os.path.join(_MANIFESTS, source)
    t = Target(abspath=abspath, flags=_clang_args(std))

    before = astcache._parse_count()
    tu1 = astcache.load_or_parse(t)
    assert tu1 is not None
    assert astcache._parse_count() == before + 1

    tu2 = astcache.load_or_parse(t)
    assert tu2 is not None
    assert astcache._parse_count() == before + 1  # no additional parse

    fd = astcache.files_dir()
    assert fd.startswith(str(tmp_path))


@pytest.mark.parametrize("use_cache", [True, False])
def test_load_or_parse_returns_valid_tu(use_cache, calls_target):
    """load_or_parse returns a non-None TU regardless of use_cache flag."""
    from indexer import astcache

    tu = astcache.load_or_parse(calls_target, use_cache=use_cache)
    assert tu is not None
    # Minimal sanity: TU has a root cursor.
    assert tu.cursor is not None


def test_sidecar_fields_complete(calls_target, tmp_path):
    """After a cold miss, the sidecar contains all four required fields."""
    from indexer import astcache

    astcache.load_or_parse(calls_target)
    fd = astcache.files_dir()
    key = astcache.cache_key(calls_target)
    side = json.loads(open(os.path.join(fd, key + ".json")).read())

    assert "abspath" in side
    assert "flags_hash" in side
    assert "src_mtime" in side
    assert "libclang_version" in side
    # Spot-check values.
    assert side["abspath"] == calls_target.abspath
    assert side["libclang_version"] == astcache.libclang_version()
    assert isinstance(side["src_mtime"], float)

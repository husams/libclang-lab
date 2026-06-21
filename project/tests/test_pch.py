"""Tests for indexer.pch -- the shared system/C++ precompiled header.

Split in two: pure gate/derivation logic (no libclang) and a handful of
real-libclang cases (build a small PCH, prove it is injected and transparent,
and prove an incompatible PCH falls back to a normal reparse rather than
breaking the parse).
"""

from __future__ import annotations

import json
import os

from indexer import astcache, pch
from indexer.storage import Storage


# -- pure logic ----------------------------------------------------------------


def test_pch_relevant_keeps_lang_drops_include_and_linker():
    opts = [
        "--driver-mode=g++",
        "-std=c++17",
        "-DFOO=1",
        "-I/usr/include",
        "-isystem",  # value-taking? no: -isystem joined OR separate; here separate
        "/opt/inc",
        "-iquote",
        "/q",
        "-L/usr/lib",
        "-lpthread",
        "-Wl,-z,now",
        "-include",
        "prefix.h",
        "-include-pch",
        "stale.pch",
        "-x",
        "c++",
        "-fexceptions",
        "-m64",
    ]
    kept = pch.pch_relevant(opts)
    assert "--driver-mode=g++" in kept
    assert "-std=c++17" in kept
    assert "-DFOO=1" in kept
    assert "-fexceptions" in kept
    assert "-m64" in kept
    # include paths + linker + stale pch/include/x pairs all gone
    for gone in ("-I/usr/include", "/opt/inc", "/q", "-L/usr/lib", "-lpthread",
                 "-Wl,-z,now", "prefix.h", "stale.pch", "c++"):
        assert gone not in kept
    assert "-isystem" not in kept and "-iquote" not in kept
    assert "-include" not in kept and "-include-pch" not in kept and "-x" not in kept


def test_common_cpp_flags_is_intersection_over_cpp_tus(tmp_path):
    db_path = str(tmp_path / "index.db")
    with Storage(db_path) as db:
        comp = db.add_component("lab", str(tmp_path / "src"))
        d = db.add_directory(comp, "")
        # two C++ TUs share -std=c++17 + -DCOMMON; differ on -DONLYA
        db.add_file(d, "a.cpp", compile_options=["--driver-mode=g++", "-std=c++17",
                    "-DCOMMON", "-DONLYA", "-I/x"], driver="c++")
        db.add_file(d, "b.cpp", compile_options=["--driver-mode=g++", "-std=c++17",
                    "-DCOMMON", "-I/y"], driver="c++")
        # a C TU must be ignored entirely
        db.add_file(d, "c.c", compile_options=["-std=c11", "-DCNOPE"], driver="cc")
    flags, driver, n = pch.common_cpp_flags(db_path)
    assert n == 2
    assert driver == "c++"
    assert "-std=c++17" in flags and "-DCOMMON" in flags and "--driver-mode=g++" in flags
    assert "-DONLYA" not in flags          # only in a.cpp -> not common
    assert all(not f.startswith("-I") for f in flags)   # include paths dropped
    assert "-DCNOPE" not in flags          # from the C TU, excluded


def test_common_cpp_flags_empty_when_no_cpp(tmp_path):
    db_path = str(tmp_path / "index.db")
    with Storage(db_path) as db:
        comp = db.add_component("lab", str(tmp_path / "src"))
        d = db.add_directory(comp, "")
        db.add_file(d, "x.c", compile_options=["-std=c11"], driver="cc")
    flags, driver, n = pch.common_cpp_flags(db_path)
    assert (flags, driver, n) == ([], None, 0)


# -- consumption gate ----------------------------------------------------------


def _fake_pch(tmp_path, monkeypatch, *, driver="c++", version="clang 18"):
    """Drop a fake system.pch + sidecar into an isolated cache; return nothing.
    consume_args only stats the .pch and reads the sidecar, so the bytes need
    not be a real PCH for the gate tests."""
    monkeypatch.setenv(astcache.CACHE_ENV, str(tmp_path))
    monkeypatch.setattr(astcache, "libclang_version", lambda: version)
    os.makedirs(pch.astcache.files_dir(), exist_ok=True)
    with open(pch.pch_path(), "wb") as fh:
        fh.write(b"PCHFAKE")
    with open(pch.sidecar_path(), "w") as fh:
        json.dump({"libclang_version": version, "driver": driver, "cpp": True}, fh)


def test_consume_args_injects_when_compatible(tmp_path, monkeypatch):
    _fake_pch(tmp_path, monkeypatch, driver="c++", version="clang 18")
    got = pch.consume_args(True, "c++")
    assert got == ["-include-pch", pch.pch_path()]


def test_consume_args_skips_for_c(tmp_path, monkeypatch):
    _fake_pch(tmp_path, monkeypatch, driver="c++", version="clang 18")
    assert pch.consume_args(False, "c++") == []


def test_consume_args_skips_on_env_off(tmp_path, monkeypatch):
    _fake_pch(tmp_path, monkeypatch, driver="c++", version="clang 18")
    monkeypatch.setenv(pch.NO_PCH_ENV, "1")
    assert pch.consume_args(True, "c++") == []


def test_consume_args_skips_on_driver_mismatch(tmp_path, monkeypatch):
    _fake_pch(tmp_path, monkeypatch, driver="clang++", version="clang 18")
    assert pch.consume_args(True, "g++") == []


def test_consume_args_skips_on_version_mismatch(tmp_path, monkeypatch):
    _fake_pch(tmp_path, monkeypatch, driver="c++", version="clang 18")
    monkeypatch.setattr(astcache, "libclang_version", lambda: "clang 21")
    assert pch.consume_args(True, "c++") == []


def test_consume_args_no_pch_present(tmp_path, monkeypatch):
    monkeypatch.setenv(astcache.CACHE_ENV, str(tmp_path))
    assert pch.consume_args(True, "c++") == []


# -- real-libclang: build / inject / transparency ------------------------------


def _seed_cpp_index(tmp_path, driver="c++"):
    db_path = str(tmp_path / "index.db")
    with Storage(db_path) as db:
        comp = db.add_component("lab", str(tmp_path / "src"))
        d = db.add_directory(comp, "")
        db.add_file(d, "probe.cpp",
                    compile_options=["--driver-mode=g++", "-std=c++17"], driver=driver)
    return db_path


def test_build_inject_and_transparency(tmp_path, monkeypatch):
    monkeypatch.setenv(astcache.CACHE_ENV, str(tmp_path))
    # keep the umbrella tiny so the build is fast
    monkeypatch.setattr(pch, "DEFAULT_HEADERS", ("cstddef", "string", "vector"))
    db_path = _seed_cpp_index(tmp_path)

    rc = pch.cmd_build(db_path, force=True)
    assert rc == 0
    assert os.path.exists(pch.pch_path())
    side = json.load(open(pch.sidecar_path()))
    assert side["cpp"] is True and side["driver"] == "c++"
    assert "-std=c++17" in side["flags"]

    # the gate now injects for a matching C++ parse
    assert pch.consume_args(True, "c++") == ["-include-pch", pch.pch_path()]

    # a probe that uses std::vector WITHOUT including <vector> resolves from the
    # PCH, and the indexed top-level symbols are identical to a no-PCH parse.
    from indexer.clang import parse

    probe = tmp_path / "use.cpp"
    probe.write_text("std::vector<int> g_v;\nstd::size_t n = g_v.size();\n")

    def topsyms(tu):
        return sorted(
            c.spelling for c in tu.cursor.get_children()
            if c.location.file and c.location.file.name == str(probe)
        )

    tu_with = parse(str(probe), ["--driver-mode=g++", "-std=c++17"], driver="c++")
    syms_with = topsyms(tu_with)
    fatals_with = [d.spelling for d in tu_with.diagnostics if d.severity >= 3]

    monkeypatch.setenv(pch.NO_PCH_ENV, "1")
    tu_no = parse(str(probe), ["--driver-mode=g++", "-std=c++17"], driver="c++")
    syms_no = topsyms(tu_no)

    assert fatals_with == []                 # PCH-injected parse is clean
    assert "g_v" in syms_with and "n" in syms_with
    assert syms_with == syms_no              # transparent: same main-file symbols


def test_incompatible_pch_falls_back_to_reparse(tmp_path, monkeypatch):
    """A corrupt/incompatible system.pch must not break the parse: the injected
    parse fails fatally, then `parse` retries once WITHOUT the PCH and succeeds."""
    monkeypatch.setenv(astcache.CACHE_ENV, str(tmp_path))
    os.makedirs(pch.astcache.files_dir(), exist_ok=True)
    # a sidecar that matches the current libclang+driver so the gate INJECTS it,
    # but a .pch that is not a real PCH -> libclang fatal -> fallback path.
    with open(pch.pch_path(), "wb") as fh:
        fh.write(b"this is not a valid precompiled header")
    with open(pch.sidecar_path(), "w") as fh:
        json.dump({"libclang_version": astcache.libclang_version(),
                   "driver": "c++", "cpp": True}, fh)
    assert pch.consume_args(True, "c++") == ["-include-pch", pch.pch_path()]

    from indexer.clang import parse

    probe = tmp_path / "ok.cpp"
    probe.write_text("int answer() { return 42; }\n")
    # Must NOT raise: the bad PCH triggers the retry-without-PCH fallback.
    tu = parse(str(probe), ["--driver-mode=g++", "-std=c++17"], driver="c++")
    syms = sorted(
        c.spelling for c in tu.cursor.get_children()
        if c.location.file and c.location.file.name == str(probe)
    )
    assert "answer" in syms
    assert [d.spelling for d in tu.diagnostics if d.severity >= 3] == []

"""Tests for include-path aliasing (v0.6.0): encode/decode of -I labels.

Hermetic — exercises the pure compiledb transforms plus the storage
round-trip; no libclang required.
"""

from __future__ import annotations

import os

from indexer import compiledb


def test_build_label_map_resolves_and_sorts_longest_first(monkeypatch):
    monkeypatch.setenv("REP", "/opt/rep")
    lm = compiledb.build_label_map(
        [("a", "/opt/rep/a"), ("ab", "$REP/a/b"), ("c", "/c")]
    )
    # resolved + sorted by (-len, name): /opt/rep/a/b, /opt/rep/a, /c
    assert lm == [("ab", "/opt/rep/a/b", False), ("a", "/opt/rep/a", False), ("c", "/c", False)]


def test_alias_options_longest_match_and_remainder():
    lm = [("inc", "/p/inc", False), ("p", "/p", False)]  # already longest-first
    # exact match, sub-path remainder, and most-specific wins
    assert compiledb.alias_options(["-I/p/inc"], lm) == ["-I<inc>"]
    assert compiledb.alias_options(["-I/p/inc/sub"], lm) == ["-I<inc>/sub"]
    assert compiledb.alias_options(["-I/p/other"], lm) == ["-I<p>/other"]


def test_alias_options_space_form_and_isystem():
    lm = [("inc", "/p/inc", False)]
    assert compiledb.alias_options(["-I", "/p/inc"], lm) == ["-I", "<inc>"]
    assert compiledb.alias_options(["-isystem", "/p/inc"], lm) == ["-isystem", "<inc>"]
    assert compiledb.alias_options(["-iquote/p/inc"], lm) == ["-iquote<inc>"]


def test_alias_options_leaves_unmatched_and_nonpath_tokens():
    lm = [("inc", "/p/inc", False)]
    opts = ["-DFOO=1", "-std=c++17", "-I/other/place", "-I<inc>"]
    # no registry match -> unchanged; non-include tokens -> unchanged;
    # already-indirected -> unchanged.
    assert compiledb.alias_options(opts, lm) == opts


def test_alias_options_ignores_relative_values():
    lm = [("inc", "/p/inc", False)]
    assert compiledb.alias_options(["-Iinclude"], lm) == ["-Iinclude"]


def test_resolve_options_decodes_label_and_envvar(monkeypatch):
    monkeypatch.setenv("REP", "/opt/rep")
    lookup = {"inc": "/p/inc"}.get
    assert compiledb.resolve_options(["-I<inc>"], lookup) == ["-I/p/inc"]
    assert compiledb.resolve_options(["-I<inc>/sub"], lookup) == ["-I/p/inc/sub"]
    assert compiledb.resolve_options(["-I$REP/x"], lookup) == ["-I/opt/rep/x"]
    # plain absolute path is left untouched
    assert compiledb.resolve_options(["-I/abs/dir"], lookup) == ["-I/abs/dir"]


def test_encode_then_decode_round_trip():
    lm = compiledb.build_label_map([("inc", "/p/inc")])
    encoded = compiledb.alias_options(["-I/p/inc/sub", "-DK=1"], lm)
    assert encoded == ["-I<inc>/sub", "-DK=1"]
    decoded = compiledb.resolve_options(encoded, {"inc": "/p/inc"}.get)
    assert decoded == ["-I/p/inc/sub", "-DK=1"]


def test_storage_realias_helper_does_not_set_args_overridden(tmp_path):
    from indexer.storage import Storage

    db_path = os.path.join(tmp_path, "i.db")
    with Storage(db_path) as db:
        cid = db.add_component("c", str(tmp_path))
        did = db.add_directory(cid, "")
        fid = db.add_file(did, "a.c", compile_options=["-I/abs/inc"])
        db.update_file_compile_options(fid, ["-I<inc>"])
        rec = db.get_file_by_id(fid)
        assert rec.compile_options == ["-I<inc>"]
        assert rec.args_overridden == 0  # realias is not a manual override


# -- v0.9.0: version-agnostic component alias registry --------------------------


def _triples(db):
    """list_alias_pairs -> {name: (match_path, versioned)}."""
    return {name: (path, ver) for name, path, ver in db.list_alias_pairs()}


def test_alias_registry_matches_component_base_strips_version(tmp_path):
    """Components match on the version-STRIPPED base; encode drops the version
    segment, decode (get_alias) re-injects the highest version."""
    from indexer.storage import Storage

    with Storage(os.path.join(tmp_path, "i.db")) as db:
        db.add_component("Numactl", "/opt/osp/Numactl")  # unversioned
        db.add_component("memhog", "/opt/osp/Numactl/memhog", version="1.2.0")
        t = _triples(db)
        assert t["Numactl"] == ("/opt/osp/Numactl", True)
        assert t["memhog"] == ("/opt/osp/Numactl/memhog", True)  # version stripped
        assert db.get_alias("Numactl") == "/opt/osp/Numactl"
        assert db.get_alias("memhog") == "/opt/osp/Numactl/memhog/1.2.0"  # +max ver
        lm = compiledb.build_label_map(db.list_alias_pairs(), lookup=db.get_alias)
        # version segment in the -I is stripped from the stored token
        assert compiledb.alias_options(["-I/opt/osp/Numactl/memhog/1.2.0/inc"], lm) == [
            "-I<memhog>/inc"
        ]
        # a DIFFERENT version still matches the same base (version-agnostic)
        assert compiledb.alias_options(["-I/opt/osp/Numactl/memhog/9.9.9/inc"], lm) == [
            "-I<memhog>/inc"
        ]
        assert compiledb.alias_options(["-I/opt/osp/Numactl/src"], lm) == [
            "-I<Numactl>/src"
        ]
        # round-trip: decode injects the registered max version
        assert compiledb.resolve_options(["-I<memhog>/inc"], db.get_alias) == [
            "-I/opt/osp/Numactl/memhog/1.2.0/inc"
        ]


def test_alias_registry_collapses_same_base_multiversion(tmp_path):
    """Two rows with the same name + same base but different version segments
    collapse to one entry resolving to the numeric-max version."""
    from indexer.storage import Storage

    with Storage(os.path.join(tmp_path, "i.db")) as db:
        # version-in-path registration (no version property): same base /m/OTF
        db.add_component("mdw::OTF", "/m/OTF/18-0-0-100")
        db.add_component("mdw::OTF", "/m/OTF/18-0-0-275")
        assert _triples(db)["mdw::OTF"] == ("/m/OTF", True)
        # numeric-max wins (275 > 100), not lexicographic
        assert db.get_alias("mdw::OTF") == "/m/OTF/18-0-0-275"
        lm = compiledb.build_label_map(db.list_alias_pairs(), lookup=db.get_alias)
        assert compiledb.alias_options(["-I/m/OTF/18-0-0-100/generated/include"], lm) == [
            "-I<mdw::OTF>/generated/include"
        ]


def test_alias_registry_skips_conflicting_bases(tmp_path):
    """Same name at two DIFFERENT bases is ambiguous: excluded and get_alias None."""
    from indexer.storage import Storage

    with Storage(os.path.join(tmp_path, "i.db")) as db:
        db.add_component("dup", "/a/dup")
        db.add_component("dup", "/b/dup")
        assert "dup" not in _triples(db)
        assert db.get_alias("dup") is None


def test_alias_registry_label_wins_over_component(tmp_path):
    """An explicit label shadows a same-named component in both encode + decode."""
    from indexer.storage import Storage

    with Storage(os.path.join(tmp_path, "i.db")) as db:
        db.add_component("foo", "/component/foo")
        db.add_label("foo", "/label/foo")
        assert _triples(db)["foo"] == ("/label/foo", False)  # exact (not versioned)
        assert db.get_alias("foo") == "/label/foo"


def test_import_bumps_component_version_when_higher(tmp_path, capsys, monkeypatch):
    """Porting a compile_commands.json whose -I carries a higher version than the
    registered (version-as-property) component advances the stored version, and
    the source then resolves under the bumped effective root."""
    import json

    from indexer import cli
    from indexer.storage import Storage

    root = tmp_path / "proj" / "OTF" / "18-0-0-275"
    (root / "include").mkdir(parents=True)
    src = root / "a.c"
    src.write_text("int x;")
    base = str(tmp_path / "proj" / "OTF")

    db_path = str(tmp_path / "idx.db")
    monkeypatch.setattr(cli, "index_path", lambda: db_path)
    with Storage(db_path) as db:
        db.add_component("OTF", base, kind="external", version="18-0-0-100")

    cc_path = root / "compile_commands.json"
    cc_path.write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "file": str(src),
                    "arguments": [
                        "cc",
                        f"-I{root}/include",
                        "-c",
                        str(src),
                    ],
                }
            ]
        )
    )
    rc = cli.main(["import", "--db", str(cc_path)])
    assert rc == 0
    with Storage(db_path) as db:
        comp = db.get_component_by_name("OTF")
        assert comp is not None and comp.version == "18-0-0-275"  # bumped 100 -> 275
        # stored include is the portable, version-stripped token
        opts = [o for f, _d in db.list_files() for o in (f.compile_options or [])]
        assert "-I<OTF>/include" in opts


# -- v0.27.0: drop unrelated (linker / cache) flags ----------------------------


def test_strip_drops_linker_library_and_cache_flags():
    """sanitize() removes every flag that only affects linking, the module
    cache, or assembler passthrough, while keeping parse-affecting flags."""
    args = [
        "-I/inc",
        "-std=c++17",
        "-DFOO=1",
        "-nostdinc",  # header search -> KEEP
        "-pthread",  # preprocessor define -> KEEP
        # -- everything below must be dropped --
        "-lfoo",
        "-l",
        "bar",  # space form lib
        "-L/usr/lib",
        "-L",
        "/extra/lib",  # space form -L
        "-Wl,-rpath,/x",
        "-Wa,--noexecstack",
        "-shared",
        "-static",
        "-rdynamic",
        "-pie",
        "-no-pie",
        "-s",
        "-pipe",
        "-static-libstdc++",
        "-fuse-ld=lld",
        "-fmodules-cache-path=/tmp/cc",
        "-Xlinker",
        "-znow",
        "-T",
        "link.ld",
    ]
    assert compiledb.sanitize(args) == [
        "-I/inc",
        "-std=c++17",
        "-DFOO=1",
        "-nostdinc",
        "-pthread",
    ]


def test_import_no_bump_when_higher_version_dir_missing(tmp_path, monkeypatch):
    """A -I carrying a HIGHER version whose directory is absent on disk must NOT
    bump the component — the include decodes to the registered version."""
    import json

    from indexer import cli
    from indexer.storage import Storage

    # Only the registered version dir exists; the higher one does not.
    root = tmp_path / "proj" / "OTF" / "18-0-0-100"
    (root / "include").mkdir(parents=True)
    src = root / "a.c"
    src.write_text("int x;")
    base = str(tmp_path / "proj" / "OTF")

    db_path = str(tmp_path / "idx.db")
    monkeypatch.setattr(cli, "index_path", lambda: db_path)
    with Storage(db_path) as db:
        db.add_component("OTF", base, kind="external", version="18-0-0-100")

    # Compile command references a NEWER version dir that is NOT on disk.
    cc_path = root / "compile_commands.json"
    cc_path.write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "file": str(src),
                    "arguments": [
                        "cc",
                        f"-I{base}/18-0-0-275/include",  # missing on disk
                        "-c",
                        str(src),
                    ],
                }
            ]
        )
    )
    assert cli.main(["import", "--db", str(cc_path)]) == 0
    with Storage(db_path) as db:
        comp = db.get_component_by_name("OTF")
        assert comp is not None and comp.version == "18-0-0-100"  # NOT bumped
        # encoded version-agnostically; decodes back to the registered version
        opts = [o for f, _d in db.list_files() for o in (f.compile_options or [])]
        assert "-I<OTF>/include" in opts


def test_import_bumps_embedded_version_path(tmp_path, monkeypatch):
    """A component whose version is embedded in its PATH (no version property)
    is bumped by rewriting the trailing path segment when a higher include
    version exists on disk."""
    import json

    from indexer import cli
    from indexer.storage import Storage

    newer = tmp_path / "m" / "OTF" / "18-0-0-275"
    (newer / "include").mkdir(parents=True)
    src = newer / "a.c"
    src.write_text("int x;")

    db_path = str(tmp_path / "idx.db")
    monkeypatch.setattr(cli, "index_path", lambda: db_path)
    with Storage(db_path) as db:
        # version-in-path registration: path carries the version, column is NULL
        db.add_component("OTF", str(tmp_path / "m" / "OTF" / "18-0-0-100"))

    cc_path = newer / "compile_commands.json"
    cc_path.write_text(
        json.dumps(
            [
                {
                    "directory": str(newer),
                    "file": str(src),
                    "arguments": ["cc", f"-I{newer}/include", "-c", str(src)],
                }
            ]
        )
    )
    assert cli.main(["import", "--db", str(cc_path)]) == 0
    with Storage(db_path) as db:
        comp = db.get_component_by_name("OTF")
        assert comp is not None
        # path's trailing version segment rewritten to the bumped version
        assert comp.path.endswith("18-0-0-275")
        assert comp.version is None
        assert db.get_alias("OTF") == str(newer)


def test_set_component_effective_version_property_and_embedded(tmp_path):
    """Direct unit test of the property-vs-embedded version write + the
    ambiguous multi-row no-op (parity with the C++ unit tests)."""
    from indexer.storage import Storage

    with Storage(os.path.join(tmp_path, "i.db")) as db:
        # version-as-property: column updated, path unchanged
        db.add_component("A", "/m/A", version="1-0-0")
        assert db.set_component_effective_version("A", "1-0-1") is True
        a = db.get_component_by_name("A")
        assert a is not None and a.path == "/m/A" and a.version == "1-0-1"
        assert db.get_alias("A") == "/m/A/1-0-1"

        # version-in-path: trailing segment rewritten, column stays NULL
        db.add_component("B", "/m/B/1-0-0")
        assert db.set_component_effective_version("B", "1-0-1") is True
        b = db.get_component_by_name("B")
        assert b is not None and b.path == "/m/B/1-0-1" and b.version is None

        # ambiguous multi-row: no-op
        db.add_component("C", "/m/C/1-0-0")
        db.add_component("C", "/m/C/1-0-1")
        assert db.set_component_effective_version("C", "2-0-0") is False


def test_import_aliases_lazily_created_component_includes(tmp_path, monkeypatch):
    """A component created lazily DURING import has its own -I paths aliased to
    <name> (the registry is rebuilt after the lazy creation)."""
    import json

    from indexer import cli
    from indexer.storage import Storage

    root = tmp_path / "fresh"
    (root / "include").mkdir(parents=True)
    src = root / "a.c"
    src.write_text("int x;")

    db_path = str(tmp_path / "idx.db")
    monkeypatch.setattr(cli, "index_path", lambda: db_path)

    cc_path = root / "compile_commands.json"
    cc_path.write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "file": str(src),
                    "arguments": ["cc", f"-I{root}/include", "-c", str(src)],
                }
            ]
        )
    )
    assert cli.main(["import", "--name", "fresh", "--db", str(cc_path)]) == 0
    with Storage(db_path) as db:
        opts = [o for f, _d in db.list_files() for o in (f.compile_options or [])]
        # encoded against the just-created component, not stored absolute
        assert "-I<fresh>/include" in opts
        assert not any(o.startswith("-I" + str(root)) for o in opts)

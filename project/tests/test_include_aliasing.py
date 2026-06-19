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
    assert lm == [("ab", "/opt/rep/a/b"), ("a", "/opt/rep/a"), ("c", "/c")]


def test_alias_options_longest_match_and_remainder():
    lm = [("inc", "/p/inc"), ("p", "/p")]  # already longest-first
    # exact match, sub-path remainder, and most-specific wins
    assert compiledb.alias_options(["-I/p/inc"], lm) == ["-I<inc>"]
    assert compiledb.alias_options(["-I/p/inc/sub"], lm) == ["-I<inc>/sub"]
    assert compiledb.alias_options(["-I/p/other"], lm) == ["-I<p>/other"]


def test_alias_options_space_form_and_isystem():
    lm = [("inc", "/p/inc")]
    assert compiledb.alias_options(["-I", "/p/inc"], lm) == ["-I", "<inc>"]
    assert compiledb.alias_options(["-isystem", "/p/inc"], lm) == ["-isystem", "<inc>"]
    assert compiledb.alias_options(["-iquote/p/inc"], lm) == ["-iquote<inc>"]


def test_alias_options_leaves_unmatched_and_nonpath_tokens():
    lm = [("inc", "/p/inc")]
    opts = ["-DFOO=1", "-std=c++17", "-I/other/place", "-I<inc>"]
    # no registry match -> unchanged; non-include tokens -> unchanged;
    # already-indirected -> unchanged.
    assert compiledb.alias_options(opts, lm) == opts


def test_alias_options_ignores_relative_values():
    lm = [("inc", "/p/inc")]
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

"""Tests for indexer.relink -- published-library -> cloned-repo include rewrite.

Hermetic: exercises the pure transforms plus a storage round-trip; no libclang.
"""

from __future__ import annotations

from indexer import relink
from indexer.storage import Component, Storage

# The user's real scenario:
#   component  dcs::cml::coredata::flight  -> /workspace/DCS/.../AOPS-52378/ngfcr
#   include    /remote/tmp/weekly/shared-bms-replication-dir/dcs/cml/coredata/flight/18-91-0-15/src/bom/generate/cat
_NAME = "dcs::cml::coredata::flight"
_CLONE = "/workspace/DCS/dcs_cml_compo_staging/AOPS-52378/ngfcr"
_PUB = (
    "/remote/tmp/weekly/shared-bms-replication-dir/dcs/cml/coredata/flight/"
    "18-91-0-15/src/bom/generate/cat"
)


def _frag_map(*comps):
    return relink.build_fragment_map(comps)


def test_relink_value_published_to_alias():
    fm = _frag_map(Component(_NAME, _CLONE))
    assert (
        relink.relink_value(_PUB, fm)
        == "<dcs::cml::coredata::flight>/src/bom/generate/cat"
    )


def test_relink_value_published_to_absolute_clone():
    fm = _frag_map(Component(_NAME, _CLONE))
    assert (
        relink.relink_value(_PUB, fm, alias=False)
        == _CLONE + "/src/bom/generate/cat"
    )


def test_relink_value_requires_version_by_default():
    """A fragment NOT followed by a version segment is left alone by default
    (avoids rewriting a coincidental match), but relinked with require_version
    off."""
    fm = _frag_map(Component(_NAME, _CLONE))
    no_ver = "/some/tree/dcs/cml/coredata/flight/src/bom"
    assert relink.relink_value(no_ver, fm) == no_ver  # unchanged
    assert (
        relink.relink_value(no_ver, fm, require_version=False)
        == "<dcs::cml::coredata::flight>/src/bom"
    )


def test_relink_value_skips_aliased_relative_and_unmatched():
    fm = _frag_map(Component(_NAME, _CLONE))
    for v in (
        "<dcs::cml::coredata::flight>/src",  # already aliased
        "$REP/include",  # env-var indirection
        "relative/inc",  # not absolute
        "/remote/other/lib/9-0-0/include",  # no fragment match
    ):
        assert relink.relink_value(v, fm) == v


def test_relink_longest_fragment_wins():
    """A nested component (longer fragment) wins over its parent."""
    fm = _frag_map(
        Component("dcs::cml::coredata", "/clone/parent"),
        Component(_NAME, _CLONE),
    )
    assert (
        relink.relink_value(_PUB, fm)
        == "<dcs::cml::coredata::flight>/src/bom/generate/cat"
    )


def test_relink_options_space_and_glued_forms():
    fm = _frag_map(Component(_NAME, _CLONE))
    opts = ["-I", _PUB, "-DKEEP", "-I" + _PUB, "-isystem", "/sys/inc"]
    assert relink.relink_options(opts, fm) == [
        "-I",
        "<dcs::cml::coredata::flight>/src/bom/generate/cat",
        "-DKEEP",
        "-I<dcs::cml::coredata::flight>/src/bom/generate/cat",
        "-isystem",
        "/sys/inc",
    ]


def test_run_end_to_end_rewrites_stored_options(tmp_path):
    db_path = str(tmp_path / "idx.db")
    with Storage(db_path) as db:
        db.add_component(_NAME, _CLONE, kind="repo")
        # register the cloned repo dir so the file attaches to the component
        src = _CLONE + "/src/serviceinterface/x.cpp"
        db.add_file_path(
            src,
            mtime=None,
            md5=None,
            compile_options=["-I" + _PUB, "-DFOO", "-I<other>/inc"],
            driver="g++",
        )

    class A:
        index = db_path
        component = None
        apply = True
        absolute = False
        no_require_version = False
        verbose = False

    assert relink.run(A()) == 0
    with Storage(db_path) as db:
        opts = [o for f, _ in db.list_files() for o in (f.compile_options or [])]
        assert "-I<dcs::cml::coredata::flight>/src/bom/generate/cat" in opts
        assert "-DFOO" in opts  # untouched
        assert "-I<other>/inc" in opts  # already aliased, untouched
        assert not any(_PUB in o for o in opts)  # no published path survives

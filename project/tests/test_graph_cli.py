"""Tests for the `cidx graph ...` CLI subcommands (indexer.cli).

We drive cli.main(argv) directly with --db pointing at the seeded fixture DB,
capture stdout/stderr with capsys, and assert exit codes + (for --json) the
machine schema. Hermetic -- no libclang, no network.
"""

from __future__ import annotations

import json

from indexer import cli


def run(argv, capsys):
    """Invoke cli.main(argv); return (rc, stdout, stderr)."""
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


# --------------------------------------------------------------------------- #
# callers / callees / refs
# --------------------------------------------------------------------------- #


def test_callers_json(resolved_db, capsys):
    rc, out, _ = run(
        ["graph", "callers", "--db", resolved_db, "--name", "helper", "--json"], capsys
    )
    assert rc == 0
    data = json.loads(out)
    assert [d["qual_name"] for d in data] == ["main"]
    d = data[0]
    # documented stable schema
    for key in (
        "id",
        "usr",
        "qual_name",
        "kind",
        "file",
        "line",
        "count",
        "edge_kind",
        "sites",
    ):
        assert key in d
    assert d["edge_kind"] == "calls"


def test_callees_human(resolved_db, capsys):
    rc, out, _ = run(
        ["graph", "callees", "--db", resolved_db, "--name", "helper"], capsys
    )
    assert rc == 0
    assert "compute" in out
    assert "x2" in out  # multiplicity shown


def test_callees_includes_stub_marker(resolved_db, capsys):
    rc, out, _ = run(
        ["graph", "callees", "--db", resolved_db, "--name", "helper"], capsys
    )
    assert rc == 0
    assert "[stub]" in out  # the ext_fn stub callee


def test_refs_json(resolved_db, capsys):
    rc, out, _ = run(
        ["graph", "refs", "--db", resolved_db, "--name", "g_config", "--json"], capsys
    )
    assert rc == 0
    data = json.loads(out)
    assert {d["edge_kind"] for d in data} == {"uses"}
    assert {d["qual_name"] for d in data} == {"helper"}


# --------------------------------------------------------------------------- #
# selector behavior
# --------------------------------------------------------------------------- #


def test_selector_by_usr(resolved_db, capsys):
    rc, out, _ = run(
        ["graph", "callers", "--db", resolved_db, "--usr", "c:@F@helper", "--json"],
        capsys,
    )
    assert rc == 0
    assert [d["qual_name"] for d in json.loads(out)] == ["main"]


def test_selector_by_id(resolved_db, capsys):
    with cli.GraphQuery(resolved_db) as q:
        helper_id = q.get("c:@F@helper").id
    rc, out, _ = run(
        ["graph", "callers", "--db", resolved_db, "--id", str(helper_id), "--json"],
        capsys,
    )
    assert rc == 0
    assert [d["qual_name"] for d in json.loads(out)] == ["main"]


def test_ambiguous_name_exits_2(resolved_db, capsys):
    # 'draw' matches three methods -> require disambiguation
    rc, out, err = run(
        ["graph", "dispatch", "--db", resolved_db, "--name", "draw"], capsys
    )
    assert rc == 2
    assert "matches 3 symbols" in err


def test_ambiguous_name_first_wins(resolved_db, capsys):
    rc, out, err = run(
        [
            "graph",
            "dispatch",
            "--db",
            resolved_db,
            "--name",
            "draw",
            "--first",
            "--json",
        ],
        capsys,
    )
    assert rc == 0
    json.loads(out)  # valid JSON, no error


def test_unknown_name_exits_1(resolved_db, capsys):
    rc, out, err = run(
        ["graph", "callers", "--db", resolved_db, "--name", "nope"], capsys
    )
    assert rc == 1
    assert "no symbol matches" in err


# --------------------------------------------------------------------------- #
# neighbors / walk / path
# --------------------------------------------------------------------------- #


def test_neighbors_edge_filter(resolved_db, capsys):
    rc, out, _ = run(
        [
            "graph",
            "neighbors",
            "--db",
            resolved_db,
            "--usr",
            "c:@S@Base",
            "--edge",
            "inherits",
            "--direction",
            "in",
            "--json",
        ],
        capsys,
    )
    assert rc == 0
    assert [d["qual_name"] for d in json.loads(out)] == ["Derived"]


def test_walk_depth_bound(resolved_db, capsys):
    rc, out, _ = run(
        [
            "graph",
            "walk",
            "--db",
            resolved_db,
            "--name",
            "main",
            "--edge",
            "calls",
            "--depth",
            "1",
            "--json",
        ],
        capsys,
    )
    assert rc == 0
    data = json.loads(out)
    assert {d["qual_name"] for d in data} == {"helper"}
    assert all("depth" in d for d in data)


def test_path_found(resolved_db, capsys):
    rc, out, _ = run(
        [
            "graph",
            "path",
            "--db",
            resolved_db,
            "--usr",
            "c:@F@main",
            "--to-usr",
            "c:@F@compute",
            "--json",
        ],
        capsys,
    )
    assert rc == 0
    assert [d["qual_name"] for d in json.loads(out)] == ["main", "helper", "compute"]


def test_path_not_found(resolved_db, capsys):
    rc, out, _ = run(
        [
            "graph",
            "path",
            "--db",
            resolved_db,
            "--usr",
            "c:@F@compute",
            "--to-usr",
            "c:@F@main",
            "--json",
        ],
        capsys,
    )
    assert rc == 1
    assert out.strip() == "null"


# --------------------------------------------------------------------------- #
# hierarchy / dispatch
# --------------------------------------------------------------------------- #


def test_hierarchy_json(resolved_db, capsys):
    rc, out, _ = run(
        [
            "graph",
            "hierarchy",
            "--db",
            resolved_db,
            "--usr",
            "c:@S@Derived2",
            "--transitive",
            "--json",
        ],
        capsys,
    )
    assert rc == 0
    data = json.loads(out)
    assert {b["qual_name"] for b in data["bases"]} == {"Derived", "Base"}
    assert data["symbol"]["qual_name"] == "Derived2"


def test_hierarchy_members_union(resolved_db, capsys):
    rc, out, _ = run(
        ["graph", "hierarchy", "--db", resolved_db, "--usr", "c:@S@Base", "--json"],
        capsys,
    )
    assert rc == 0
    data = json.loads(out)
    assert {m["qual_name"] for m in data["members"]} == {
        "Base::x",
        "Base::draw",
        "Base::Nested",
    }


def test_dispatch_pure_root(resolved_db, capsys):
    rc, out, _ = run(
        [
            "graph",
            "dispatch",
            "--db",
            resolved_db,
            "--usr",
            "c:@S@Base@F@draw#",
            "--json",
        ],
        capsys,
    )
    assert rc == 0
    data = json.loads(out)
    assert data["is_virtual"] is True
    assert {t["qual_name"] for t in data["targets"]} == {
        "Derived::draw",
        "Derived2::draw",
    }


# --------------------------------------------------------------------------- #
# standard-DB discipline: no DB / no edges
# --------------------------------------------------------------------------- #


def test_missing_db_errors(tmp_path, capsys):
    missing = str(tmp_path / "nope.db")
    rc, out, err = run(["graph", "callers", "--db", missing, "--name", "x"], capsys)
    assert rc == 1
    assert "no cidx index" in err
    assert out == ""  # nothing on stdout


def test_edgeless_db_errors_no_substitution(empty_db, capsys):
    rc, out, err = run(["graph", "callers", "--db", empty_db, "--name", "main"], capsys)
    assert rc == 1
    assert "no graph edges" in err
    assert out == ""


def test_default_db_used_when_no_db_flag(resolved_db, monkeypatch, tmp_path, capsys):
    # Point the standard cache at the fixture's directory and name it index.db.
    import shutil

    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(resolved_db, cache / "index.db")
    monkeypatch.setenv("INDEXER_CACHE", str(cache))
    rc, out, _ = run(["graph", "callers", "--name", "helper", "--json"], capsys)
    assert rc == 0
    assert [d["qual_name"] for d in json.loads(out)] == ["main"]

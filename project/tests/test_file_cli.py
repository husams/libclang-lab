"""Tests for the `cidx file ...` and `cidx dump-compile-commands ...` CLI
subcommands (indexer.cli).

Drives cli.main(argv) with --db pointing at the seeded fixture DB. The `file`
subcommand addresses a file as COMPONENT://RELPATH and inspects or edits its
stored compile flags; edits set args_overridden so a re-import keeps them.
Because the operation tail is an argparse REMAINDER (so flag-shaped values like
'-I/x' pass through verbatim), `--db` must precede the target. Hermetic.
"""

from __future__ import annotations

import json

from indexer import cli
from indexer.storage import Storage


def run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def file_cmd(db_path, target, *op):
    """`cidx file --db DB TARGET OP...` — --db before the REMAINDER target."""
    return ["file", "--db", db_path, target, *op]


def _file(db_path, basename):
    """(abs_path, File) for a file by basename in the seeded component."""
    with Storage(db_path) as db:
        comp = db.get_component_by_name("lab")
        ap = f"{comp.path}/{basename}"
        return ap, db.get_file(ap)


# -- inspection ---------------------------------------------------------------


def test_dump_args_empty(index_db, capsys):
    rc, out, _ = run(file_cmd(index_db, "lab://main.c", "-dump-args"), capsys)
    assert rc == 0
    assert out.strip() == "[]"


def test_dump_args_is_default_op(index_db, capsys):
    # No operation -> -dump-args.
    rc, out, _ = run(file_cmd(index_db, "lab://main.c"), capsys)
    assert rc == 0
    assert out.strip() == "[]"


# -- set / unset --------------------------------------------------------------


def test_set_flag_appends_and_marks_override(index_db, capsys):
    rc, out, _ = run(
        file_cmd(index_db, "lab://main.c", "-set-flag", "-I/extra"), capsys
    )
    assert rc == 0
    assert "added flag" in out
    _, rec = _file(index_db, "main.c")
    assert rec.compile_options == ["-I/extra"]
    assert rec.args_overridden is True


def test_set_flag_idempotent(index_db, capsys):
    run(file_cmd(index_db, "lab://main.c", "-set-flag", "-I/x"), capsys)
    rc, out, _ = run(file_cmd(index_db, "lab://main.c", "-set-flag", "-I/x"), capsys)
    assert rc == 0
    assert "already present" in out
    _, rec = _file(index_db, "main.c")
    assert rec.compile_options == ["-I/x"]  # not duplicated


def test_unset_flag_removes(index_db, capsys):
    run(file_cmd(index_db, "lab://main.c", "-set-flag", "-DA"), capsys)
    rc, out, _ = run(file_cmd(index_db, "lab://main.c", "-unset-flag", "-DA"), capsys)
    assert rc == 0
    assert "removed flag" in out
    _, rec = _file(index_db, "main.c")
    assert rec.compile_options == []


def test_unset_flag_absent(index_db, capsys):
    rc, out, _ = run(file_cmd(index_db, "lab://main.c", "-unset-flag", "-DA"), capsys)
    assert rc == 0
    assert "not present" in out


def test_override_survives_reseed_add_file(index_db, capsys):
    # add_file (the path import uses) must NOT clobber overridden options.
    run(file_cmd(index_db, "lab://main.c", "-set-flag", "-DKEEP"), capsys)
    ap, _ = _file(index_db, "main.c")
    with Storage(index_db) as db:
        db.add_file_path(ap, compile_options=["-DFRESH"], driver="cc")
    _, rec = _file(index_db, "main.c")
    assert rec.compile_options == ["-DKEEP"]  # override won


# -- import-args --------------------------------------------------------------


def test_import_args_inline(index_db, capsys):
    entry = json.dumps(
        {
            "directory": "/proj",
            "file": "main.c",
            "arguments": ["cc", "-I/proj/inc", "-DX=1", "-c", "main.c", "-o", "main.o"],
        }
    )
    rc, out, _ = run(file_cmd(index_db, "lab://main.c", "-import-args", entry), capsys)
    assert rc == 0
    assert "imported" in out
    _, rec = _file(index_db, "main.c")
    # driver/source/-c/-o stripped; -I left absolute.
    assert rec.compile_options == ["-I/proj/inc", "-DX=1"]
    assert rec.driver == "cc"
    assert rec.args_overridden is True


# -- error paths --------------------------------------------------------------


def test_malformed_target(index_db, capsys):
    rc, _, err = run(file_cmd(index_db, "lab:/main.c", "-dump-args"), capsys)
    assert rc == 1
    assert "COMPONENT://PATH" in err


def test_unknown_component(index_db, capsys):
    rc, _, err = run(file_cmd(index_db, "nope://main.c", "-dump-args"), capsys)
    assert rc == 1
    assert "no component named" in err


def test_not_in_index(index_db, capsys):
    rc, _, err = run(file_cmd(index_db, "lab://ghost.c", "-dump-args"), capsys)
    assert rc == 1
    assert "not in index database" in err


def test_unknown_op(index_db, capsys):
    rc, _, err = run(file_cmd(index_db, "lab://main.c", "-frobnicate"), capsys)
    assert rc == 2
    assert "unknown operation" in err


# -- dump-compile-commands ----------------------------------------------------


def test_dump_compile_commands_empty(index_db, capsys):
    # Seeded files have no stored flags -> empty array.
    rc, out, _ = run(["dump-compile-commands", "lab", "--db", index_db], capsys)
    assert rc == 0
    assert json.loads(out) == []


def test_dump_compile_commands_after_edits(index_db, capsys):
    run(file_cmd(index_db, "lab://main.c", "-set-flag", "-I/inc"), capsys)
    rc, out, _ = run(["dump-compile-commands", "lab", "--db", index_db], capsys)
    assert rc == 0
    entries = json.loads(out)
    assert len(entries) == 1
    e = entries[0]
    assert e["file"].endswith("/main.c")
    assert e["arguments"][0] == "cc"  # default driver
    assert "-I/inc" in e["arguments"]
    assert e["arguments"][-1] == e["file"]  # source appended last


def test_dump_compile_commands_unknown_component(index_db, capsys):
    rc, _, err = run(["dump-compile-commands", "nope", "--db", index_db], capsys)
    assert rc == 1
    assert "no component named" in err

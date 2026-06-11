"""Ground-truth check for indexer.storage -- every public API, asserted.

Run from the lab root:
    python3 project/indexer/_storage_smoke.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from indexer.storage import Storage, Symbol  # noqa: E402


def main():
    tmp = tempfile.mkdtemp()
    repo = os.path.join(tmp, "myrepo")
    os.makedirs(os.path.join(repo, "src"))
    db_path = os.path.join(tmp, "index.db")

    with Storage(db_path) as db:
        # -- components --------------------------------------------------
        comp = db.add_component("myrepo", repo)
        assert db.add_component("myrepo", repo) == comp, "idempotent on path"
        ext = db.add_component("libc", "/usr/include", kind="external")
        assert ext != comp
        assert db.get_component(repo).name == "myrepo"
        assert db.component_for_path(os.path.join(repo, "src", "a.c")).id == comp

        # -- directories -------------------------------------------------
        d_src = db.add_directory(comp, "src")
        assert db.add_directory(comp, "src") == d_src, "idempotent"
        d_root = db.add_directory(comp, "")
        assert db.get_directory(comp, "src").id == d_src

        # -- files ---------------------------------------------------------
        f1 = db.add_file(d_src, "a.c", mtime=100.0, md5="aaa",
                         compile_options=["-I.", "-DDEBUG"])
        assert db.add_file(d_src, "a.c") == f1, "idempotent"
        a_c = os.path.join(repo, "src", "a.c")
        assert db.add_file_path(a_c) == f1, "path convenience resolves to same row"
        assert db.file_abs_path(f1) == a_c

        rec = db.get_file(a_c)
        assert rec.compile_options == ["-I.", "-DDEBUG"], "options round-trip"
        assert rec.md5 == "aaa" and rec.indexed is False

        assert not db.is_file_indexed(a_c), "not indexed yet"
        db.mark_file_indexed(f1, mtime=100.0)
        assert db.is_file_indexed(a_c)
        assert db.is_file_indexed(a_c, mtime=100.0), "fresh"
        assert not db.is_file_indexed(a_c, mtime=200.0), "stale mtime -> reindex"
        assert db.is_file_indexed(a_c, md5="aaa"), "same content"
        assert not db.is_file_indexed(a_c, md5="bbb"), "changed content -> reindex"
        assert not db.is_file_indexed("/nowhere/else.c"), "unknown component"

        # re-import with a new md5 resets the indexed flag
        db.add_file(d_src, "a.c", md5="ccc")
        assert not db.is_file_indexed(a_c), "content change clears indexed"
        db.mark_file_indexed(f1)
        assert db.is_file_indexed(a_c)

        # -- symbols -------------------------------------------------------
        decl = Symbol(usr="c:@F@multiply", spelling="multiply", kind="function",
                      type_info="int (int, int)", file_id=f1, line=3, col=5,
                      decl_file_id=f1, decl_line=3, decl_col=5,
                      is_definition=False)
        sid = db.add_symbol(decl)
        assert db.lookup_symbol("c:@F@multiply").is_definition is False

        # definition upserts over the declaration (same USR, same row);
        # the declaration site recorded earlier survives alongside it
        defn = Symbol(usr="c:@F@multiply", spelling="multiply", kind="function",
                      type_info="int (int, int)", file_id=f1, line=10, col=1,
                      is_definition=True, resolved=True)
        assert db.add_symbol(defn) == sid, "USR upsert, not a new row"
        got = db.lookup_symbol("c:@F@multiply")
        assert got.is_definition and got.resolved and got.line == 10
        assert got.decl_line == 3, "decl site survives the definition upsert"

        # a later declaration must NOT downgrade the stored definition's location
        db.add_symbol(decl)
        got = db.lookup_symbol("c:@F@multiply")
        assert got.line == 10, "definition wins"
        assert got.decl_line == 3, "decl site stays"

        # qual_name: stored, upsert-preserved, and fuzzy-searchable
        db.add_symbol(Symbol(usr="c:@N@rk@S@Conf@F@set", spelling="set",
                             kind="method", qual_name="rk::Conf::set",
                             parent_usr="c:@N@rk@S@Conf", is_pure=True,
                             resolved=True))
        got = db.lookup_symbol("c:@N@rk@S@Conf@F@set")
        assert got.qual_name == "rk::Conf::set"
        assert got.is_pure is True, "is_pure round-trips"
        db.add_symbol(Symbol(usr="c:@N@rk@S@Conf@F@set", spelling="set",
                             kind="method", resolved=True))
        got = db.lookup_symbol("c:@N@rk@S@Conf@F@set")
        assert got.qual_name == "rk::Conf::set", "NULL must not clobber qual_name"
        assert [s.usr for s in db.search_symbols("conf::set")] == \
            ["c:@N@rk@S@Conf@F@set"], "segment fuzzy match"
        assert db.search_symbols("conf::set", kind="function") == []
        assert db.search_symbols("nosuchthing") == []

        # update_symbol
        assert db.update_symbol("c:@F@multiply", display_name="multiply(int, int)")
        assert db.lookup_symbol("c:@F@multiply").display_name == "multiply(int, int)"
        assert not db.update_symbol("c:@F@missing", resolved=True)
        try:
            db.update_symbol("c:@F@multiply", bogus=1)
            raise AssertionError("unknown column must raise")
        except ValueError:
            pass
        try:
            db.add_symbol(Symbol(usr="x", spelling="x", kind="not-a-kind"))
            raise AssertionError("unknown kind must raise")
        except ValueError:
            pass

        # name lookup returns every row with that spelling
        db.add_symbol(Symbol(usr="c:a.c@F@multiply", spelling="multiply",
                             kind="function", is_definition=True))
        hits = db.lookup_symbols_by_name("multiply")
        assert len(hits) == 2 and all(h.spelling == "multiply" for h in hits)
        assert len(db.lookup_symbols_by_name("multiply", kind="struct")) == 0

        # bulk insert inside one transaction
        with db.transaction():
            for i in range(50):
                db.add_symbol(Symbol(usr=f"c:@S@T{i}", spelling=f"T{i}",
                                     kind="struct", resolved=True))

        # unresolved + per-file views
        assert {s.usr for s in db.unresolved_symbols()} == {"c:a.c@F@multiply"}
        assert [s.usr for s in db.symbols_in_file(f1)] == ["c:@F@multiply"]

        # -- stats -----------------------------------------------------------
        st = db.stats()
        assert st["components"] == 2 and st["files"] == 1
        assert st["files_indexed"] == 1
        assert st["symbols"] == 53
        assert st["symbols_by_kind"] == {"function": 2, "method": 1, "struct": 50}
        assert st["symbols_unresolved"] == 1

    # data survives reopen
    with Storage(db_path) as db:
        assert db.lookup_symbol("c:@F@multiply").display_name == "multiply(int, int)"

    print("storage smoke: ALL OK")
    for k, v in st.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

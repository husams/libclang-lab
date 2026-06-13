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

        # -- by-id getters -----------------------------------------------------
        assert db.get_component_by_id(comp).name == "myrepo"
        assert db.get_component_by_id(99999) is None
        assert db.get_directory_by_id(d_src).path == "src"
        assert db.get_directory_by_id(d_src).component_id == comp
        assert db.get_directory_by_id(99999) is None
        assert db.get_file_by_id(f1).name == "a.c"
        assert db.get_file_by_id(f1).directory_id == d_src
        assert db.get_file_by_id(99999) is None

        # -- list views --------------------------------------------------------
        assert [c.name for c in db.list_components()] == ["libc", "myrepo"]
        assert [c.name for c in db.list_components(name="myrp")] == \
            ["myrepo"], "fuzzy: chars in order"
        assert [c.name for c in db.list_components(kind="external")] == ["libc"]
        assert db.list_components(name="zzz") == []

        dirs = db.list_directories(component_id=comp)
        assert [(d.path, n) for d, n in dirs] == \
            [("", "myrepo"), ("src", "myrepo")]
        assert [d.path for d, _ in db.list_directories(name="sr")] == ["src"]

        assert [p for _, p in db.list_files(component_id=comp)] == [a_c]
        assert [p for _, p in db.list_files(component_id=comp,
                                            dir_path="src")] == [a_c]
        assert [p for _, p in db.list_files(component_id=comp,
                                            dir_path="")] == [a_c], \
            "root subtree covers everything"
        assert db.list_files(component_id=comp, dir_path="other") == []
        assert [p for _, p in db.list_files(name="ac")] == [a_c], "fuzzy name"
        assert db.list_files(indexed=False) == []
        assert [p for _, p in db.list_files(indexed=True)] == [a_c]

        assert [s.usr for s in db.list_symbols(component_id=comp)] == \
            ["c:@F@multiply"], "scoped by definition/declaration site"
        assert [s.usr for s in db.list_symbols(component_id=comp,
                                               dir_path="src")] == \
            ["c:@F@multiply"]
        assert [s.usr for s in db.list_symbols(file_id=f1)] == ["c:@F@multiply"]
        assert [s.usr for s in db.list_symbols(name="cfset")] == \
            ["c:@N@rk@S@Conf@F@set"], "fuzzy hits the qualified name"
        assert len(db.list_symbols(kind="struct")) == 50
        assert db.list_symbols(component_id=comp, kind="struct") == []

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

    # -- delete_component (import --force): cascade files + explicit symbols ----
    with Storage(":memory:") as db:
        a = db.add_component("a", "/repo/a")
        da = db.add_directory(a, "")
        fa = db.add_file(da, "a.c")
        db.add_symbol(Symbol(usr="c:@F@a_fn", spelling="a_fn", kind="function",
                             file_id=fa, decl_file_id=fa))
        b = db.add_component("b", "/repo/b")
        dbdir = db.add_directory(b, "")
        fb = db.add_file(dbdir, "b.c")
        db.add_symbol(Symbol(usr="c:@F@b_fn", spelling="b_fn", kind="function",
                             file_id=fb, decl_file_id=fb))
        # Defined in B but declared in A's file -> related to A, removed with A.
        db.add_symbol(Symbol(usr="c:@F@cross", spelling="cross", kind="function",
                             file_id=fb, decl_file_id=fa))

        db.delete_component(a)

        assert db.get_component("/repo/a") is None
        assert db.get_file("/repo/a/a.c") is None
        assert db.lookup_symbol("c:@F@a_fn") is None, "A's symbol deleted"
        assert db.lookup_symbol("c:@F@cross") is None, "decl-site-in-A deleted"
        assert db.get_component("/repo/b") is not None, "B untouched"
        assert db.get_file("/repo/b/b.c") is not None
        assert db.lookup_symbol("c:@F@b_fn") is not None

    print("storage smoke: ALL OK")
    for k, v in st.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

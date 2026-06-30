"""Tests for the storage-layer Repository / Component / Directory *smart paths*.

Each is handed back bound to its Storage (mirroring the File smart path) and
exposes lazy generators that walk the tree downward:

    Repository.components()  -> Component   (also .files())
    Component.directories()  -> Directory   (also .files())
    Directory.files()        -> File

Every listing generator takes an optional fuzzy ``name`` filter
(``comp.files("sample.hpp")`` yields every file whose name matches).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from indexer.storage import Component, Directory, Repository, Storage
from indexer.utils.hashing import md5_of

SRC = "int add(int a, int b) { return a + b; }\n"


@pytest.fixture
def tree(tmp_path):
    """A Storage with one repository -> one component -> two directories,
    holding sample.hpp (root) and util.c + sample.hpp (src/)."""
    root_dir = tmp_path / "proj"
    (root_dir / "src").mkdir(parents=True)
    (root_dir / "sample.hpp").write_text(SRC)
    (root_dir / "src" / "util.c").write_text(SRC)
    (root_dir / "src" / "sample.hpp").write_text(SRC)

    db = Storage(str(tmp_path / "index.db"))
    rid = db.add_repository("proj", remote_url="git@example.com:proj.git")
    cid = db.add_clone(rid, str(root_dir), label="main")
    db.set_active_clone(rid, cid)
    comp_id = db.add_component("proj-core", str(root_dir))
    db.set_component_repository(comp_id, rid)

    for rel, fname in [("", "sample.hpp"), ("src", "util.c"), ("src", "sample.hpp")]:
        p = root_dir / rel / fname if rel else root_dir / fname
        d = db.add_directory(comp_id, rel)
        db.add_file(d, fname, md5=md5_of(str(p)), compile_options=["-std=c11"])

    yield db, rid, comp_id, str(root_dir)
    db.close()


# -- binding / unbound errors ----------------------------------------------- #


def test_unbound_raise_clearly():
    with pytest.raises(RuntimeError, match="not bound to a Storage"):
        list(Component(name="x", path="/x").directories())
    with pytest.raises(RuntimeError, match="not bound to a Storage"):
        _ = Component(name="x", path="/x").abspath
    with pytest.raises(RuntimeError, match="not bound to a Storage"):
        list(Repository(name="x").components())
    with pytest.raises(RuntimeError, match="not bound to a Storage"):
        list(Directory(component_id=1, path="src").files())


def test_accessors_bind(tree):
    db, rid, comp_id, _root = tree
    assert db.get_repository_by_id(rid)._storage is db
    assert db.get_component_by_id(comp_id)._storage is db
    d, _name = db.list_directories(component_id=comp_id)[0]
    assert d._storage is db


# -- properties: name / path ------------------------------------------------ #


def test_repository_path_is_active_clone(tree):
    db, rid, _comp_id, root = tree
    repo = db.get_repository_by_id(rid)
    assert os.path.realpath(repo.path) == os.path.realpath(root)
    assert repo.name == "proj"


def test_component_abspath_and_repo(tree):
    db, _rid, comp_id, root = tree
    comp = db.get_component_by_id(comp_id)
    assert os.path.realpath(comp.abspath) == os.path.realpath(root)
    assert comp.repo is not None and comp.repo.name == "proj"


def test_directory_name_and_abspath(tree):
    db, _rid, comp_id, root = tree
    src = db.get_directory(comp_id, "src")
    assert src.name == "src"
    assert os.path.realpath(src.abspath) == os.path.realpath(os.path.join(root, "src"))
    root_dir = db.get_directory(comp_id, "")
    assert root_dir.name == ""  # component root has empty relative path


# -- generators (lazy, not lists) ------------------------------------------- #


def test_methods_are_generators(tree):
    db, rid, comp_id, _root = tree
    repo, comp = db.get_repository_by_id(rid), db.get_component_by_id(comp_id)
    assert isinstance(repo.components(), Iterator)
    assert isinstance(comp.directories(), Iterator)
    assert isinstance(comp.files(), Iterator)
    assert isinstance(db.get_directory(comp_id, "src").files(), Iterator)


def test_repository_components(tree):
    db, rid, _comp_id, _root = tree
    comps = list(db.get_repository_by_id(rid).components())
    assert [c.name for c in comps] == ["proj-core"]
    assert all(c._storage is db for c in comps)


def test_component_directories(tree):
    db, _rid, comp_id, _root = tree
    dirs = list(db.get_component_by_id(comp_id).directories())
    assert sorted(d.path for d in dirs) == ["", "src"]
    assert all(d._storage is db for d in dirs)


def test_directory_files_direct_only(tree):
    db, _rid, comp_id, _root = tree
    # root directory holds ONLY sample.hpp (src/* live under the src dir row)
    root_dir = db.get_directory(comp_id, "")
    assert [f.name for f in root_dir.files()] == ["sample.hpp"]
    src = db.get_directory(comp_id, "src")
    assert sorted(f.name for f in src.files()) == ["sample.hpp", "util.c"]
    assert all(f._storage is db for f in src.files())


def test_component_files_whole_subtree(tree):
    db, _rid, comp_id, _root = tree
    names = sorted(f.name for f in db.get_component_by_id(comp_id).files())
    assert names == ["sample.hpp", "sample.hpp", "util.c"]


def test_repository_files(tree):
    db, rid, _comp_id, _root = tree
    names = sorted(f.name for f in db.get_repository_by_id(rid).files())
    assert names == ["sample.hpp", "sample.hpp", "util.c"]


# -- name filter ------------------------------------------------------------ #


def test_files_name_filter(tree):
    db, rid, comp_id, _root = tree
    comp = db.get_component_by_id(comp_id)
    assert [f.name for f in comp.files("sample.hpp")] == ["sample.hpp", "sample.hpp"]
    assert [f.name for f in comp.files("util")] == ["util.c"]
    # repository-level and directory-level filters too
    assert [f.name for f in db.get_repository_by_id(rid).files("util")] == ["util.c"]
    src = db.get_directory(comp_id, "src")
    assert [f.name for f in src.files("sample")] == ["sample.hpp"]


def test_directories_name_filter(tree):
    db, _rid, comp_id, _root = tree
    dirs = list(db.get_component_by_id(comp_id).directories("src"))
    assert [d.path for d in dirs] == ["src"]


def test_components_name_filter(tree):
    db, rid, _comp_id, _root = tree
    repo = db.get_repository_by_id(rid)
    assert [c.name for c in repo.components("core")] == ["proj-core"]
    assert list(repo.components("nope")) == []


# -- end-to-end: walk a yielded File down to its symbols -------------------- #


def test_yielded_file_is_indexable(tree):
    db, _rid, comp_id, _root = tree
    src = db.get_directory(comp_id, "src")
    util = next(f for f in src.files() if f.name == "util.c")
    res = util.index()
    assert res["symbols"] == 1
    assert [s.spelling for s in util.symbols()] == ["add"]

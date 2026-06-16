"""Stub symbols must be born NAMED.

`mint_symbol_id` carries the reference cursor's spelling/qual_name, so a call
target whose definition is never indexed (stdlib calls, implicit template
instantiations, defaulted ctors) is not left as `Function('' @<no-location>)`.

Regression for the nameless-callee bug: minting anchored an edge to a USR but
discarded the spelling that was in hand at the call site.
"""

from __future__ import annotations

import os
import tempfile

from indexer.storage import Storage, Symbol  # noqa: E402


def _db() -> Storage:
    tmp = tempfile.mkdtemp()
    db = Storage(os.path.join(tmp, "i.db"))
    db.add_component("t", tmp)
    return db


def test_mint_stores_name() -> None:
    db = _db()
    usr = "c:@N@std@S@vector@F@push_back#"
    sid = db.mint_symbol_id(usr, "push_back", "std::vector::push_back",
                            "push_back(const value_type &)")
    s = db.lookup_symbol(usr)
    assert s is not None and s.id == sid
    assert s.spelling == "push_back"
    assert s.qual_name == "std::vector::push_back"
    assert s.display_name == "push_back(const value_type &)"
    assert s.resolved == 0          # still an unresolved stub


def test_bare_mint_stays_nameless() -> None:
    # Back-compat: a mint with no name (truly-unknown target) is still empty.
    db = _db()
    db.mint_symbol_id("c:@F@unknown")
    s = db.lookup_symbol("c:@F@unknown")
    assert s is not None and s.spelling == ""


def test_repeat_mint_upgrades_empty_but_never_clobbers() -> None:
    db = _db()
    db.mint_symbol_id("c:@F@f")                         # nameless first
    db.mint_symbol_id("c:@F@f", "f", "ns::f")           # upgrade the empty name
    assert db.lookup_symbol("c:@F@f").spelling == "f"
    db.mint_symbol_id("c:@F@f", "WRONG", "x::WRONG")    # must NOT clobber
    assert db.lookup_symbol("c:@F@f").spelling == "f"
    assert db.lookup_symbol("c:@F@f").qual_name == "ns::f"


def test_real_definition_overwrites_named_stub() -> None:
    db = _db()
    root = db.add_directory(1, "")
    fid = db.add_file(root, "g.c")
    usr = "c:@F@g"
    db.mint_symbol_id(usr, "g", "ns::g")               # stub, resolved=0
    db.add_symbol(Symbol(usr=usr, spelling="g", kind="function",
                         qual_name="ns::g", file_id=fid, line=3, col=1,
                         is_definition=True, resolved=True))
    s = db.lookup_symbol(usr)
    assert s is not None
    assert s.is_definition == 1 and s.resolved == 1     # real def won
    assert s.file_id == fid and s.line == 3

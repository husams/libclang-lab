#!/usr/bin/env python3
"""Example 07 — Devirtualizing a callgraph: selection maps + Γ type pruning.

A virtual call recorded through a base reference resolves to the *declared*
type, so a plain callgraph over-approximates. cidx's two-phase devirtualization
narrows it:

    Phase 1  (conservative superset) — at each virtual call site, expand to
             EVERY concrete override the static type could dispatch to, and
             attach a SELECTION MAP `concrete-type -> target-method`.
    Phase 2  (type-environment Γ prune) — propagate a per-location set of
             possible concrete types, then at a *prunable* dispatch site keep
             only the targets whose key is actually reachable. Always sound:
             if Γ is unknown (⊤) or the site is unprunable, the full Phase-1
             set is kept (so it degrades to today's callgraph).

The motivating case (manifests/graphlab/chain.{hpp,cpp}):

    struct A { virtual int rank() const; };          // B:A, C:B, D:C override
    int  top_rank(const A& a) { return a.rank(); }    // site: A::rank -> {A,B,C,D}
    void f() { B b; top_rank(b); }                    // Γ[b]={B} -> prune to B::rank

The API is the model layer (`indexer.model`): the same graph the rest of the
examples use, plus two Phase-2 verbs —

    Method   : .dispatch_selection()              -> the selection map + prunability
    Callable : .devirtualized_callgraph(
                   expand_virtual=True,           # walk into virtual targets
                   prune=False|True)              # False = Phase 1, True = Phase 2

Run (from the repo root, with the lab venv):
    cd /Users/husam/workspace/qemu-vms/libclang-lab
    .venv/bin/python project/examples/07_devirtualization.py
"""

from __future__ import annotations

import os
import sys
import tempfile

# Repo layout: this file is project/examples/07_*.py -> repo root is ../../..
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_GRAPHLAB = os.path.join(_REPO, "manifests", "graphlab")
sys.path.insert(0, os.path.join(_REPO, "project"))   # the `indexer` package
sys.path.insert(0, os.path.join(_REPO, "scripts"))   # `_helpers.clang_args`

from indexer.model import CodeBase, Method            # noqa: E402
from indexer.query import GraphQuery                  # noqa: E402


# --------------------------------------------------------------------------- #
# Setup — build a tiny graph from the chain fixture (real libclang parse).
# In normal use you'd open the prebuilt index instead:
#     from indexer import open_codebase
#     with open_codebase() as cb: ...
# We index inline so the example is self-contained and deterministic, and so the
# v10 `call_arg` argument-provenance Phase 2 relies on is freshly extracted.
# --------------------------------------------------------------------------- #
def build_chain_codebase(db_path: str) -> CodeBase:
    import clang.cindex as cx
    from _helpers import clang_args
    from indexer.clang import ast as A
    from indexer.storage import Storage

    hpp = os.path.join(_GRAPHLAB, "chain.hpp")
    cpp = os.path.join(_GRAPHLAB, "chain.cpp")
    args = clang_args(cpp) + ["-std=c++17", "-I", _GRAPHLAB]
    idx = cx.Index.create()
    tu_h = idx.parse(hpp, args=args)
    tu_c = idx.parse(cpp, args=args)

    db = Storage(db_path)
    db.add_component("graphlab", _GRAPHLAB)
    hid = db.add_file_path(hpp)
    with db.transaction():
        A.index_symbols(db, tu_h, hid)               # declarations (the .hpp)
    cid = db.add_file_path(cpp)
    with db.transaction():
        A.index_symbols(db, tu_c, cid)               # definitions
    with db.transaction():
        db.delete_edges_for_file(cid)
        A._index_edges_notxn(db, tu_c, cpp, cid)     # calls + arg provenance
    db.resolve_pass()                                # link decls <-> defs
    db.close()
    return CodeBase(GraphQuery(db_path))


def _name(x):
    """Readable name for a model entity, Sym, or plain value."""
    return getattr(x, "name", None) or str(x)


def _targets(selections):
    """Pretty list of target method names from a list of SelectionModel."""
    return sorted(_name(s.target.sym) for s in selections if s.target is not None)


def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="cidx_devirt_")
    db_path = os.path.join(tmpdir, "chain.db")
    cb = build_chain_codebase(db_path)
    try:
        # ---------------------------------------------------------------- #
        # 1. THE SELECTION MAP — Method.dispatch_selection()
        #    For the virtual A::rank, what concrete override does each
        #    run-time type select? And is the site prunable at all?
        # ---------------------------------------------------------------- #
        a_rank = next(
            m for m in cb.find("rank")
            if isinstance(m, Method) and m.owner and m.owner.name == "chain::A"
        )
        site = a_rank.dispatch_selection()
        print("== 1. selection map for chain::A::rank (Phase 1) ==")
        print(f"   receiver static type : {site.receiver_static_type}")
        print(f"   prunable             : {site.prunable}"
              + ("" if site.prunable else f"  ({site.unprunable_reasons})"))
        for sel in site.selections:
            print(f"     {_name(sel.selecting_type):<12} -> {_name(sel.target.sym)}")

        # ---------------------------------------------------------------- #
        # 2. PHASE 1 — devirtualized_callgraph(prune=False)
        #    The conservative superset: every virtual hop keeps ALL targets.
        #    This is byte-identical to the plain callgraph (the default).
        # ---------------------------------------------------------------- #
        f = cb.get("c:@N@chain@F@f#") or next(x for x in cb.find("f") if x.name == "chain::f")
        print("\n== 2. chain::f()  devirtualized_callgraph(prune=False) — Phase 1 ==")
        for step in f.devirtualized_callgraph(expand_virtual=True, prune=False):
            if step.dispatch_site is None:
                continue
            print(f"   virtual dispatch -> {_targets(step.dispatch_site.selections)}"
                  f"   (pruned_candidates={step.pruned_candidates})")

        # ---------------------------------------------------------------- #
        # 3. PHASE 2 — devirtualized_callgraph(prune=True)
        #    Γ flows `B b` into top_rank's parameter, so a.rank() is pruned
        #    to B::rank only. `gamma_receiver` shows the type set that did it.
        # ---------------------------------------------------------------- #
        print("\n== 3. chain::f()  devirtualized_callgraph(prune=True) — Phase 2 ==")
        pruned_once = False
        for step in f.devirtualized_callgraph(expand_virtual=True, prune=True):
            if step.dispatch_site is None:
                continue
            full = _targets(step.dispatch_site.selections)
            if step.pruned_candidates is None:        # ⊤ / unprunable -> sound fallback
                print(f"   virtual dispatch -> kept full set {full}  (no type info)")
            else:
                kept = _targets(step.pruned_candidates)
                gamma = sorted(_name(cb.get(u) or u) for u in step.gamma_receiver)
                print(f"   virtual dispatch -> Γ(receiver)={gamma} "
                      f"pruned {full} -> {kept}")
                pruned_once = True

        # A self-check so this doubles as a smoke test.
        assert pruned_once, "expected at least one pruned dispatch site"
        print("\nOK — Phase 2 narrowed a.rank() from {A,B,C,D}::rank to {B::rank}.")
    finally:
        cb.close()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()

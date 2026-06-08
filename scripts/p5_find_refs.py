"""5.2 Find all references to a symbol by USR, across two translation units."""
import clang.cindex as cx
from _helpers import MANIFESTS, parse, clang_args, walk, loc, fatal_diagnostics

PROJECT = MANIFESTS / "project"
TARGET = "multiply"  # the symbol we want every call/reference site for

# A reference is any of these cursor kinds that resolves back to a decl.
REF_KINDS = (cx.CursorKind.CALL_EXPR, cx.CursorKind.DECL_REF_EXPR)


def find_target_usr(tu):
    """USR of the FUNCTION_DECL named TARGET (USR is identical in every TU)."""
    for c, _ in walk(tu.cursor):
        if c.kind == cx.CursorKind.FUNCTION_DECL and c.spelling == TARGET:
            return c.get_usr()
    return None


def refs_in(tu, usr):
    """Yield the source location of every cursor that references `usr`.

    We scan both CALL_EXPR and DECL_REF_EXPR for generality, but a call expr
    already contains a decl-ref to its callee at the SAME location -- so we key
    the final set on location alone to report one site per physical reference.
    """
    for c, _ in walk(tu.cursor):
        if c.kind in REF_KINDS and c.referenced and c.referenced.get_usr() == usr:
            yield loc(c)


def main():
    # extra_includes=[PROJECT] lets each .c find its "mathlib.h".
    args = clang_args(extra_includes=[PROJECT])
    tus = [parse(PROJECT / name, args=args) for name in ("mathlib.c", "app.c")]
    for tu in tus:
        if fatal_diagnostics(tu):
            raise SystemExit("parse failed: " + str(fatal_diagnostics(tu)))

    # The USR is content-addressed, so the declaration's USR matches everywhere.
    usr = next((u for u in (find_target_usr(t) for t in tus) if u), None)
    print(f"target: {TARGET}  usr: {usr}")

    sites = sorted({site for tu in tus for site in refs_in(tu, usr)})
    print(f"call sites: {len(sites)}")
    for where in sites:
        print(f"  {where}")


if __name__ == "__main__":
    main()

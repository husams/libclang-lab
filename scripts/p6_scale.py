"""6.5 Parsing at scale: multiprocessing.Pool over files, returning DATA not cursors."""
import multiprocessing as mp

import clang.cindex as cx
from _helpers import MANIFESTS, clang_args, loc, in_main_file, walk


def extract_functions(job):
    """Worker: parse one file, return plain (name, location) DATA.

    Runs in a separate process. macOS/Py3.14 uses the 'spawn' start method, so
    this must be a top-level (importable) function and its return value must be
    picklable. A Cursor is ONLY valid while its TranslationUnit is alive, so we
    pull strings out HERE, inside the TU's scope, and never return cursors/TUs.
    Each worker creates its OWN Index (one Index per process).
    """
    path, args = job
    index = cx.Index.create()            # this worker's own Index
    tu = index.parse(str(path), args=list(args))
    out = []
    for c, _ in walk(tu.cursor):
        if c.kind == cx.CursorKind.FUNCTION_DECL and in_main_file(c) and c.is_definition():
            out.append((c.spelling, loc(c)))   # plain data, picklable
    return out


def main():
    proj = MANIFESTS / "project"
    files = [proj / "mathlib.c", proj / "app.c"]

    # Compute compiler flags ONCE in the parent (clang_args() shells out to
    # xcrun/clang) and pass the resolved list down to every worker.
    args = clang_args(extra_includes=[proj])
    jobs = [(f, args) for f in files]

    # Pool under the __main__ guard (required for spawn). Each task is one file.
    with mp.Pool(processes=2) as pool:
        per_file = pool.map(extract_functions, jobs)

    merged = sorted({item for sub in per_file for item in sub})
    print(f"parsed {len(files)} files across {2} worker processes")
    print("functions (merged, sorted):")
    for name, where in merged:
        print(f"  {name:10} {where}")


if __name__ == "__main__":
    main()

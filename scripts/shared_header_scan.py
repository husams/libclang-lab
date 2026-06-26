#!/usr/bin/env python3
"""shared_header_scan.py -- cheaply find the shared-header core of a codebase.

Why: cidx cold-indexing is *parse-bound* (~67% of real per-TU time is libclang
parse/sema, which re-parses the same headers for every TU). The only way to batch
that away is a precompiled header (PCH) of the headers shared by most TUs. But a
PCH is only worth building if you first know *what* is shared -- and that differs
per codebase. A `clang -M` dependency scan answers that ~12x cheaper than a full
parse (it lexes + resolves #includes only -- no sema/AST/template work), so the
whole-codebase scan costs minutes, not the 10h a full index costs.

This script pulls each TU's real compile flags + driver straight from a cidx
index.db (so it works on ANY codebase you've already imported), decodes cidx's
portable <label>/$VAR include tokens exactly as the indexer does at parse time,
runs `clang -M` per TU in parallel, and reports the header-frequency histogram
plus the >=N% "shared core" you'd feed into a PCH umbrella.

Usage:
    python3 shared_header_scan.py --db ~/.cache/cidx/index.db [options]

    --db PATH         cidx index database (default: $INDEXER_CACHE/index.db or
                      ~/.cache/cidx/index.db)
    --indexer PATH    dir containing the `indexer` package (default: auto-detect
                      relative to this script: ../project)
    --jobs N          parallel scans (default: CPU count)
    --limit N         scan only the first N TUs (0 = all; for a quick smoke test)
    --top N           print the N most-shared headers (default: 30)
    --coverage F ...  coverage thresholds to bucket-count (default: .9 .7 .5 .3)
    --out PATH        write the >=(first --coverage) headers, freq-sorted, here
                      (this is your PCH-umbrella candidate list)
    --cc CC           compiler to use when a TU row has no stored driver
                      (default: c++ on PATH)

The DB is opened READ-ONLY -- this never mutates your index.
"""
import argparse, json, os, sqlite3, subprocess, sys, time, collections
from concurrent.futures import ThreadPoolExecutor

# Source extensions cidx treats as TUs (everything else in `file` is a header
# indexed via its including TU and must NOT be scanned standalone).
SRC_EXT = {".c", ".cc", ".cpp", ".cxx", ".c++", ".cp", ".m", ".mm"}


def find_indexer(explicit: str | None) -> str:
    """Locate the dir that contains the `indexer` package so we can reuse cidx's
    own option-decode (compiledb.resolve_options) and path-resolve (pathx)."""
    cands = []
    if explicit:
        cands.append(explicit)
    here = os.path.dirname(os.path.abspath(__file__))
    cands += [os.path.join(here, "..", "project"), os.path.join(here, "project")]
    for c in cands:
        if os.path.isdir(os.path.join(c, "indexer")):
            return os.path.abspath(c)
    sys.exit(
        "error: cannot find the `indexer` package; pass --indexer <dir that "
        "contains 'indexer/'> (e.g. .../libclang-lab/project)"
    )


def build_lookup(con: sqlite3.Connection):
    """Replicate cidx's alias decode (db.get_alias) read-only: a <name> token
    resolves to an explicit label's path, else to a same-named component's
    effective root (path + highest version). Returns a lookup(name)->path|None."""
    labels = {}
    try:
        for name, path in con.execute("SELECT name, path FROM label"):
            labels[name] = path
    except sqlite3.OperationalError:
        pass  # pre-label-table schema

    # component name -> effective root (path joined with version segment if any)
    comps: dict[str, list[tuple[str, str | None]]] = {}
    cols = {r[1] for r in con.execute("PRAGMA table_info(component)")}
    has_ver = "version" in cols
    sql = "SELECT name, path%s FROM component" % (", version" if has_ver else "")
    for row in con.execute(sql):
        name, path = row[0], row[1]
        ver = row[2] if has_ver else None
        comps.setdefault(name, []).append((path, ver))

    def lookup(name: str):
        if name in labels:
            return labels[name]
        ents = comps.get(name)
        if not ents or len(ents) > 1:  # absent or ambiguous -> let caller derive
            return None
        path, ver = ents[0]
        return os.path.join(path, ver) if ver else path

    return lookup


def reconstruct_root(comp_path, comp_ver, pathx, lookup):
    eff = os.path.join(comp_path, comp_ver) if comp_ver else comp_path
    return os.path.abspath(pathx.resolve_fs_path(eff, lookup=lookup))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    default_db = os.path.join(
        os.path.expanduser(os.environ.get("INDEXER_CACHE", "~/.cache/cidx")),
        "index.db",
    )
    ap.add_argument("--db", default=default_db)
    ap.add_argument("--indexer", default=None)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--coverage", type=float, nargs="+", default=[0.9, 0.7, 0.5, 0.3])
    ap.add_argument("--out", default=None)
    ap.add_argument("--cc", default="c++")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"error: no index DB at {args.db} (pass --db)")

    sys.path.insert(0, find_indexer(args.indexer))
    from indexer import compiledb, pathx  # type: ignore

    con = sqlite3.connect(f"file:{os.path.abspath(args.db)}?mode=ro", uri=True)
    lookup = build_lookup(con)

    # Pull every file row with its component/dir so we can rebuild the abs path.
    rows = con.execute(
        "SELECT c.path, "
        + ("c.version, " if "version" in {r[1] for r in con.execute('PRAGMA table_info(component)')} else "NULL, ")
        + "d.path, f.name, f.compile_options, f.driver "
        "FROM file f "
        "JOIN directory d ON f.directory_id = d.id "
        "JOIN component c ON d.component_id = c.id"
    ).fetchall()

    tus = []
    for comp_path, comp_ver, dir_path, name, opts_json, driver in rows:
        if os.path.splitext(name)[1].lower() not in SRC_EXT:
            continue  # header / non-source
        root = reconstruct_root(comp_path, comp_ver, pathx, lookup)
        fdir = os.path.normpath(os.path.join(root, dir_path)) if dir_path else root
        fpath = os.path.join(fdir, name)
        opts = json.loads(opts_json) if opts_json else []
        opts = compiledb.resolve_options(opts, lookup=lookup)  # decode <label>/$VAR
        tus.append((fpath, fdir, opts, driver or args.cc))

    if args.limit:
        tus = tus[: args.limit]
    if not tus:
        sys.exit("error: no source TUs found in this DB (only headers?)")

    def scan(tu):
        fpath, fdir, opts, driver = tu
        cmd = [driver, *opts, "-M", "-MG", fpath]
        try:
            r = subprocess.run(cmd, cwd=fdir if os.path.isdir(fdir) else None,
                               capture_output=True, text=True, timeout=180)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return fpath, set(), False
        deps = set()
        for tok in r.stdout.replace("\\\n", " ").split():
            if tok.endswith(":") or tok.endswith(".o"):
                continue
            if "/" in tok and os.path.splitext(tok)[1].lower() not in SRC_EXT:
                deps.add(os.path.normpath(tok))
        return fpath, deps, r.returncode == 0

    n = len(tus)
    print(f"scanning {n} TUs with `clang -M` (jobs={args.jobs}) ...", flush=True)
    t0 = time.time()
    freq = collections.Counter()
    ok = fail = 0
    ndeps = []
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for fpath, deps, good in ex.map(scan, tus):
            ok += good
            fail += not good
            ndeps.append(len(deps))
            freq.update(deps)
    wall = time.time() - t0

    print(f"\n=== clang -M scan: {n} TUs ===")
    print(f"wall: {wall:.1f}s   per-TU: {wall / n * 1000:.0f}ms   ok={ok} fail={fail}")
    if ndeps:
        print(f"avg headers/TU: {sum(ndeps) / len(ndeps):.0f}   unique headers: {len(freq)}")
    print(f"\n=== top {args.top} most-shared headers ===")
    for h, c in freq.most_common(args.top):
        print(f"  {c:6d}  {100 * c / n:5.1f}%  {h}")
    print()
    for thr in sorted(args.coverage, reverse=True):
        k = sum(1 for v in freq.values() if v >= thr * n)
        print(f"headers in >= {int(thr * 100):>3}% of TUs: {k}")

    if args.out:
        thr = max(args.coverage)
        core = [h for h, c in freq.most_common() if c >= thr * n]
        with open(args.out, "w") as fh:
            fh.write("\n".join(core) + "\n")
        print(f"\nwrote {len(core)} shared-core headers (>= {int(thr*100)}%) to {args.out}")
        print("  -> PCH-umbrella candidate; feed to `cidx pch build --include ...`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

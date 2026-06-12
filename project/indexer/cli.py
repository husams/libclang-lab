"""indexer.cli -- cidx command-line skeleton.

Subcommands:
    add-source  register a component (a git repo or an external library)
    import      import a compile_commands.json: component + directories +
                files (with stripped compile options, md5, indexed=0)
    index       parse imported C/C++ files and store symbols (main TU +
                headers; already-indexed files are skipped via md5)
    search      fuzzy-search symbols by qualified name ('conf::set' matches
                RdKafka::Conf::set); prints qual name + USR per match
    show        full details for one record: 'show symbol' by id or USR,
                'show file' by id or path (import state, options, symbols)
    list (ls)   browse the index: 'list components', 'list dirs', 'list
                files', 'list symbols' -- scoped by --component / --dir /
                --file, with an optional free-text fuzzy name PATTERN

All generated files (the SQLite index, the cidx.log warning log, and later
PCH/cache artifacts) live in the cache directory -- $INDEXER_CACHE if set,
else ~/.cache/cidx -- never the current directory:

    python3 -m indexer add-source --path /path/to/repo [--name myrepo]
    python3 -m indexer import --db /path/to/build/compile_commands.json
    python3 -m indexer index
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

if __package__ in (None, ""):                       # direct execution
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from indexer.storage import SYMBOL_KINDS, File, Storage  # noqa: E402
    from indexer import compiledb                   # noqa: E402
    from indexer.clang import ClangParseError, index_source  # noqa: E402
    from indexer.utils import (                     # noqa: E402
        git_root, index_status, md5_of, repo_name, resolve_file_arg,
    )
else:
    from .storage import SYMBOL_KINDS, File, Storage
    from . import compiledb
    from .clang import ClangParseError, index_source
    from .utils import git_root, index_status, md5_of, repo_name, resolve_file_arg

CACHE_ENV = "INDEXER_CACHE"
DEFAULT_CACHE = "~/.cache/cidx"
INDEX_NAME = "index.db"
LOG_NAME = "cidx.log"


def cache_dir() -> str:
    """Cache directory for all generated files: $INDEXER_CACHE, else ~/.cache/cidx."""
    return os.path.expanduser(os.environ.get(CACHE_ENV) or DEFAULT_CACHE)


def index_path() -> str:
    return os.path.join(cache_dir(), INDEX_NAME)


def log_path() -> str:
    return os.path.join(cache_dir(), LOG_NAME)


class _WarningCounter(logging.Filter):
    """Counts warning records passing the file handler, so the index summary
    can point at the log without re-reading it."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            self.count += 1
        return True


_warnings = _WarningCounter()


def _setup_logging() -> None:
    """Send indexer warnings (tolerated diagnostics, toolchain fallbacks) to
    $INDEXER_CACHE/cidx.log instead of dumping them on the terminal.

    delay=True keeps read-only subcommands from creating an empty log file.
    """
    logger = logging.getLogger("cidx")
    if logger.handlers:                             # already configured
        return
    os.makedirs(cache_dir(), exist_ok=True)
    handler = logging.FileHandler(log_path(), delay=True)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_warnings)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# -- subcommands -----------------------------------------------------------------

def cmd_add_source(args) -> int:
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        print(f"error: {path} is not a directory", file=sys.stderr)
        return 1
    root = git_root(path) if args.kind == "repo" else None
    if root is not None:
        path = root
    name = args.name or (repo_name(path) if args.kind == "repo"
                         else os.path.basename(path))
    with Storage(args.index) as db:
        cid = db.add_component(name, path, kind=args.kind)
    print(f"component #{cid}: {name} ({args.kind}) at {path}")
    return 0


def cmd_import(args) -> int:
    try:
        commands = compiledb.load_commands(args.db)
    except Exception as e:                          # cindex raises a plain error
        print(f"error: cannot load compilation database from {args.db}: {e}",
              file=sys.stderr)
        return 1
    if not commands:
        print("error: compilation database is empty", file=sys.stderr)
        return 1

    # Component root: the git repo owning the sources, else their common dir.
    first_src = compiledb.source_path(commands[0])
    root = git_root(first_src) or os.path.dirname(first_src)
    name = args.name or (repo_name(root) if git_root(first_src)
                         else os.path.basename(root))

    imported, skipped = 0, 0
    with Storage(args.index) as db:
        cid = db.add_component(name, root)
        print(f"component #{cid}: {name} at {root}")
        with db.transaction():
            for cmd in commands:
                src = compiledb.source_path(cmd)
                if db.component_for_path(src) is None:
                    print(f"  skip (outside any component): {src}",
                          file=sys.stderr)
                    skipped += 1
                    continue
                mtime = os.path.getmtime(src) if os.path.exists(src) else None
                db.add_file_path(
                    src,
                    mtime=mtime,
                    md5=md5_of(src),
                    compile_options=compiledb.strip_for_libclang(cmd),
                    driver=compiledb.driver(cmd),
                )
                imported += 1
    print(f"imported {imported} file(s), skipped {skipped}")
    return 0


def _lookup_component(db: Storage, name: str | None):
    """Component for a --component/--source NAME, or None when no name given.

    Raises LookupError on an unknown name."""
    if not name:
        return None
    comp = db.get_component_by_name(name)
    if comp is None:
        raise LookupError(f"no component named {name!r}")
    return comp


def _source_root(db: Storage, name: str | None) -> str | None:
    """Component root for --source NAME; raises LookupError on an unknown name."""
    comp = _lookup_component(db, name)
    return comp.path if comp else None


def _index_one(db: Storage, rec: File, path: str) -> int:
    """Parse + index one pending file (main TU + its headers); returns 0/1."""
    if rec.id is None:
        return 1
    try:
        result = index_source(db, path,
                              compiledb.sanitize(rec.compile_options or []),
                              rec.id, driver=rec.driver)
    except ClangParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    mtime = os.path.getmtime(path) if os.path.exists(path) else None
    db.mark_file_indexed(rec.id, mtime=mtime)
    h = result["headers"]
    print(f"  -> {result['symbols']} symbols; headers: {h['indexed']} indexed "
          f"(+{h['symbols']} symbols), {h['already']} already, "
          f"{h['system']} system, {h['unowned']} unowned")
    return 0


def _index_files(db: Storage, files: list[str], root: str | None) -> int:
    """index FILE...: look each file up and index it unless already indexed."""
    rc = 0
    for f in files:
        path = resolve_file_arg(f, root)
        rec = db.get_file(path)
        if rec is None:
            print(f"error: not in index database: {path}", file=sys.stderr)
            rc = 1
            continue
        print(f"file: {path}")
        if index_status(rec, path)[0]:
            print("  already indexed")
            continue
        rc |= _index_one(db, rec, path)
    return rc


def _index_pending(db: Storage) -> int:
    """index (no args): index every file still pending in the database."""
    done, skipped, failed = 0, 0, 0
    for rec, path in db.files():
        if index_status(rec, path)[0]:
            skipped += 1
            continue
        print(f"indexing {path}")
        if _index_one(db, rec, path) == 0:
            done += 1
        else:
            failed += 1
    print(f"index: {done} indexed, {failed} failed, {skipped} already indexed")
    return 1 if failed else 0


def cmd_index(args) -> int:
    with Storage(args.index) as db:
        try:
            root = _source_root(db, args.source)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        rc = (_index_files(db, args.files, root) if args.files
              else _index_pending(db))
    if _warnings.count:
        print(f"{_warnings.count} warning(s) logged to {log_path()}")
    return rc


def _print_symbols(db: Storage, hits, limit: int) -> None:
    """The symbol table shared by 'search' and 'list symbols'."""
    shown = hits[:limit] if limit else hits
    width = max((len(s.qual_name or s.spelling) for s in shown), default=0)
    for s in shown:
        name = s.qual_name or s.spelling
        mark = "pure" if s.is_pure else "def " if s.is_definition else "decl"
        path = db.file_abs_path(s.file_id) if s.file_id is not None else "?"
        print(f"{s.id:>6}  {name:<{width}}  {s.kind:<17} {mark}  {path}:{s.line}")
        if s.is_definition and s.decl_file_id is not None:
            dpath = db.file_abs_path(s.decl_file_id)
            print(f"{'':>6}  {'':<{width}}  {'':<17} decl  {dpath}:{s.decl_line}")
    extra = f" (showing {len(shown)})" if len(shown) < len(hits) else ""
    print(f"{len(hits)} match(es){extra}")


def cmd_search(args) -> int:
    with Storage(args.index) as db:
        hits = db.search_symbols(args.pattern, kind=args.kind)
        _print_symbols(db, hits, args.limit)
    return 0 if hits else 1


# -- list ---------------------------------------------------------------------

def cmd_list_components(args) -> int:
    with Storage(args.index) as db:
        comps = db.list_components(name=args.pattern, kind=args.kind)
        width = max((len(c.name) for c in comps), default=0)
        for c in comps:
            print(f"{c.id:>4}  {c.name:<{width}}  {c.kind:<8}  {c.path}")
    print(f"{len(comps)} component(s)")
    return 0 if comps else 1


def cmd_list_dirs(args) -> int:
    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        rows = db.list_directories(
            component_id=comp.id if comp else None, name=args.pattern)
        width = max((len(cname) for _, cname in rows), default=0)
        for d, cname in rows:
            print(f"{d.id:>4}  {cname:<{width}}  {d.path or '.'}")
    print(f"{len(rows)} directory(ies)")
    return 0 if rows else 1


def cmd_list_files(args) -> int:
    if args.dir is not None and not args.component:
        print("error: --dir requires --component (directory paths are "
              "relative to a component root)", file=sys.stderr)
        return 1
    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        indexed = True if args.indexed else False if args.pending else None
        rows = db.list_files(
            component_id=comp.id if comp else None,
            dir_path=args.dir, name=args.pattern, indexed=indexed)
        for rec, path in rows:
            mark = "idx " if rec.indexed else "pend"
            print(f"{rec.id:>4}  {mark}  {path}")
    print(f"{len(rows)} file(s)")
    return 0 if rows else 1


def cmd_list_symbols(args) -> int:
    if args.dir is not None and not args.component:
        print("error: --dir requires --component (directory paths are "
              "relative to a component root)", file=sys.stderr)
        return 1
    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        file_id = None
        if args.file:
            path = resolve_file_arg(args.file, comp.path if comp else None)
            rec = db.get_file(path)
            if rec is None:
                print(f"error: not in index database: {path}", file=sys.stderr)
                return 1
            file_id = rec.id
        hits = db.list_symbols(
            component_id=comp.id if comp else None,
            dir_path=args.dir, file_id=file_id,
            name=args.pattern, kind=args.kind)
        _print_symbols(db, hits, args.limit)
    return 0 if hits else 1


def cmd_show_symbol(args) -> int:
    with Storage(args.index) as db:
        ref = args.symbol
        s = db.lookup_symbol_by_id(int(ref)) if ref.isdigit() \
            else db.lookup_symbol(ref)
        if s is None:
            print(f"error: no symbol with id/USR {ref!r}", file=sys.stderr)
            return 1

        def loc(file_id, line, col):
            if file_id is None:
                return None
            return f"{db.file_abs_path(file_id)}:{line}:{col}"

        parent = db.lookup_symbol(s.parent_usr) if s.parent_usr else None
        fields = [
            ("id", s.id),
            ("usr", s.usr),
            ("name", s.spelling),
            ("qualified", s.qual_name),
            ("display", s.display_name),
            ("kind", s.kind),
            ("type", s.type_info),
            ("visibility", {"external": "program-wide (usable from any .cpp)",
                            "internal": "file-local (static / anonymous namespace)",
                            "no-linkage": "local scope only",
                            }.get(s.linkage or "", s.linkage)),
            ("access", s.access),
            ("parent", f"{parent.qual_name}  [{s.parent_usr}]"
                       if parent else s.parent_usr),
            ("pure", "yes (pure virtual; implemented by overriders)"
                     if s.is_pure else None),
            ("definition", loc(s.file_id, s.line, s.col)
                           if s.is_definition else None),
            ("declaration", loc(s.decl_file_id, s.decl_line, s.decl_col)),
            ("resolved", "yes" if s.resolved else
                         "n/a (pure virtual)" if s.is_pure else
                         "no (definition not seen)"),
        ]
        for key, value in fields:
            if value is not None:
                print(f"{key:<12} {value}")
    return 0


def cmd_show_file(args) -> int:
    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        ref = args.file
        if ref.isdigit():                       # first column of 'list files'
            rec = db.get_file_by_id(int(ref))
            path = db.file_abs_path(rec.id) if rec and rec.id else None
        else:
            path = resolve_file_arg(ref, comp.path if comp else None)
            rec = db.get_file(path)
        if rec is None or rec.id is None or path is None:
            print(f"error: not in index database: {ref}", file=sys.stderr)
            return 1

        d = db.get_directory_by_id(rec.directory_id)
        owner = db.get_component_by_id(d.component_id) if d else None
        syms = db.list_symbols(file_id=rec.id)
        defined = sum(1 for s in syms
                      if s.file_id == rec.id and s.is_definition)
        declared = sum(1 for s in syms if s.decl_file_id == rec.id)
        by_kind = {}
        for s in syms:
            by_kind[s.kind] = by_kind.get(s.kind, 0) + 1

        def ts(epoch):
            if epoch is None:
                return None
            return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")

        fields = [
            ("id", rec.id),
            ("path", path),
            ("component", f"{owner.name} ({owner.kind})  {owner.path}"
                          if owner else None),
            ("directory", (d.path or ".") if d else None),
            ("mtime", ts(rec.mtime)),
            ("md5", rec.md5),
            ("driver", rec.driver),
            ("options", " ".join(rec.compile_options)
                        if rec.compile_options else
                        "(none -- header indexed via an including TU)"),
            ("indexed", index_status(rec, path)[1]),
            ("indexed at", f"{rec.indexed_at} UTC" if rec.indexed_at else None),
            ("symbols", f"{len(syms)} ({defined} defined here, "
                        f"{declared} declared here)"),
            ("by kind", ", ".join(f"{k}: {n}"
                                  for k, n in sorted(by_kind.items()))
                        if by_kind else None),
        ]
        for key, value in fields:
            if value is not None:
                print(f"{key:<12} {value}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cidx",
                                 description="cidx command-line skeleton")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-source", help="register a component")
    p.add_argument("--path", required=True, help="repo root or library header dir")
    p.add_argument("--name", help="component name (default: from .git/config)")
    p.add_argument("--kind", choices=("repo", "external"), default="repo")
    p.set_defaults(fn=cmd_add_source)

    p = sub.add_parser("import", help="import a compile_commands.json")
    p.add_argument("--db", required=True,
                   help="compile_commands.json (or the directory holding it)")
    p.add_argument("--name", help="component name override")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("index", help="index imported C/C++ files")
    p.add_argument("files", nargs="*", help="restrict to these files (default: all pending)")
    p.add_argument("--source", metavar="COMPONENT",
                   help="resolve relative FILE paths against this component's root")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="fuzzy-search symbols by qualified name")
    p.add_argument("pattern",
                   help="'::'-separated substrings matched in order, "
                        "e.g. 'conf::set' hits RdKafka::Conf::set")
    p.add_argument("--kind", choices=sorted(SYMBOL_KINDS),
                   help="restrict to one symbol kind")
    p.add_argument("--limit", type=int, default=25, metavar="N",
                   help="show at most N matches (0 = all; default 25)")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("show", help="show full details of one symbol or file")
    ssub = p.add_subparsers(dest="what", required=True)

    q = ssub.add_parser("symbol", help="one symbol, by id or USR")
    q.add_argument("symbol",
                   help="numeric id (first column of 'search') or a clang USR; "
                        "USRs contain $ and * so single-quote them in the shell")
    q.set_defaults(fn=cmd_show_symbol)

    q = ssub.add_parser("file", help="one file, by id or path")
    q.add_argument("file",
                   help="numeric id (first column of 'list files') or a path; "
                        "relative paths resolve against the --component root "
                        "(else the current directory)")
    q.add_argument("--component", "-c", metavar="NAME",
                   help="component root for resolving a relative path")
    q.set_defaults(fn=cmd_show_file)

    p = sub.add_parser("list", aliases=["ls"],
                       help="browse the index: components, dirs, files, symbols")
    lsub = p.add_subparsers(dest="what", required=True)
    fuzzy = ("optional free-text fuzzy filter: characters must appear "
             "in order, e.g. 'shp' matches shapes.c")

    q = lsub.add_parser("components", help="list registered components")
    q.add_argument("pattern", nargs="?", help=fuzzy)
    q.add_argument("--kind", choices=("repo", "external"),
                   help="restrict to one component kind")
    q.set_defaults(fn=cmd_list_components)

    q = lsub.add_parser("dirs", help="list directories (all, or one component's)")
    q.add_argument("pattern", nargs="?", help=fuzzy)
    q.add_argument("--component", "-c", metavar="NAME",
                   help="restrict to this component")
    q.set_defaults(fn=cmd_list_dirs)

    q = lsub.add_parser("files",
                        help="list files for a component or a directory in it")
    q.add_argument("pattern", nargs="?", help=fuzzy)
    q.add_argument("--component", "-c", metavar="NAME",
                   help="restrict to this component")
    q.add_argument("--dir", "-d", metavar="PATH",
                   help="directory (relative to the component root) "
                        "including its subtree; needs --component")
    g = q.add_mutually_exclusive_group()
    g.add_argument("--indexed", action="store_true",
                   help="only files already indexed")
    g.add_argument("--pending", action="store_true",
                   help="only files not yet indexed")
    q.set_defaults(fn=cmd_list_files)

    q = lsub.add_parser("symbols",
                        help="list symbols for a component, directory, or file")
    q.add_argument("pattern", nargs="?",
                   help=fuzzy + " (matched against the qualified name)")
    q.add_argument("--component", "-c", metavar="NAME",
                   help="restrict to this component")
    q.add_argument("--dir", "-d", metavar="PATH",
                   help="directory (relative to the component root) "
                        "including its subtree; needs --component")
    q.add_argument("--file", "-f", metavar="FILE",
                   help="one file; relative paths resolve against the "
                        "--component root (else the current directory)")
    q.add_argument("--kind", choices=sorted(SYMBOL_KINDS),
                   help="restrict to one symbol kind")
    q.add_argument("--limit", type=int, default=50, metavar="N",
                   help="show at most N matches (0 = all; default 50)")
    q.set_defaults(fn=cmd_list_symbols)

    args = ap.parse_args(argv)
    args.index = index_path()
    _setup_logging()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

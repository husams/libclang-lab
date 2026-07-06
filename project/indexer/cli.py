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
import json
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime

if __package__ in (None, ""):  # direct execution
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from indexer.storage import SCHEMA_VERSION, SYMBOL_KINDS, File, Storage  # noqa: E402
    from indexer import compiledb  # noqa: E402
    from indexer import pathx  # noqa: E402
    from indexer.clang import ClangParseError, index_source  # noqa: E402
    from indexer.query import (  # noqa: E402
        EDGE_KINDS,
        GraphQuery,
        NoEdgesError,
        NoIndexError,
    )
    from indexer.utils import (  # noqa: E402
        git_remote_url,
        git_root,
        index_status,
        md5_of,
        repo_name,
        resolve_file_arg,
    )
else:
    from .storage import SCHEMA_VERSION, SYMBOL_KINDS, File, Storage
    from . import astcmd, compiledb, pathx
    from .clang import ClangParseError, index_source
    from .query import EDGE_KINDS, GraphQuery, NoEdgesError, NoIndexError
    from .utils import (
        git_remote_url,
        git_root,
        index_status,
        md5_of,
        repo_name,
        resolve_file_arg,
    )

CACHE_ENV = "INDEXER_CACHE"
DEFAULT_CACHE = "~/.cache/cidx"
INDEX_NAME = "index.db"
LOG_NAME = "cidx.log"

# Keep in sync with pyproject.toml [project].version and the C++ tool
# (cidx-cpp/src/cli/args.hpp kVersion).
VERSION = "0.50.0"

# Header extensions: a pending file with one of these (or no extension, e.g. a
# bare libstdc++ header) is indexed via its including TU's index_headers() pass,
# never parsed standalone. A TU source (.c/.cpp/...) is ALWAYS indexed even when
# its compile command sanitizes to no flags (e.g. `cc -c x.c -o x.o` -> []), so
# its parse diagnostics are recorded. (Was keyed off empty compile_options, which
# wrongly deferred minimal-flag sources -- they recorded nothing on `cidx index`.)
_HEADER_SUFFIXES = frozenset(
    {".h", ".hh", ".hpp", ".hxx", ".h++", ".hp", ".inc", ".tcc", ".ipp", ".ixx"}
)


def _is_header(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext == "" or ext in _HEADER_SUFFIXES


# clang diagnostic severities (clang.cindex.Diagnostic.Warning/Error/Fatal).
_DIAG_SEVERITY_LABELS = {2: "warning", 3: "error", 4: "fatal"}


def _diag_flag(counts: dict[int, int]) -> str:
    """Compact `list files` indicator from {severity: n}: '-' when clean, else
    e.g. '2E', '3W', '1E2W' (errors+fatals fold into E, warnings into W)."""
    errs = counts.get(3, 0) + counts.get(4, 0)
    warns = counts.get(2, 0)
    if not errs and not warns:
        return "-"
    out = ""
    if errs:
        out += f"{errs}E"
    if warns:
        out += f"{warns}W"
    return out


def _diag_summary(counts: dict[int, int]) -> str:
    """Human summary for `show file`: '2 error(s), 1 warning(s)' (severity
    desc, nonzero buckets only)."""
    parts = [
        f"{counts[sev]} {_DIAG_SEVERITY_LABELS[sev]}(s)"
        for sev in (4, 3, 2)
        if counts.get(sev)
    ]
    return ", ".join(parts)


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
    if logger.handlers:  # already configured
        return
    os.makedirs(cache_dir(), exist_ok=True)
    handler = logging.FileHandler(log_path(), delay=True)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(_warnings)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# -- subcommands -----------------------------------------------------------------


def cmd_init(args) -> int:
    """Create a blank index database (schema v6, no rows) in the cache dir.

    Opening a Storage applies the schema, so this just materializes an empty
    index.db. Refuses to clobber an existing database unless --force."""
    path = args.index
    existed = os.path.exists(path)
    if existed and not args.force:
        print(
            f"error: index database already exists at {path} (use --force to recreate)",
            file=sys.stderr,
        )
        return 1
    if existed:
        os.remove(path)
    with Storage(path):
        pass  # constructing Storage applies the schema
    action = "recreated" if existed else "initialized"
    print(f"{action} empty index database at {path}")
    return 0


def cmd_migrate(args) -> int:
    """Upgrade an existing index database to the current schema version, in place.

    Opening a Storage runs the schema migration (e.g. v15 -> v16 converts
    symbol.kind from a TEXT name to its CXCursorKind integer). This command just
    does that deliberately and reports the before/after version -- it preserves
    all data (symbols, edges) and never re-indexes. Use --db to target a
    non-standard index.
    """
    import sqlite3

    path = args.index
    if not os.path.exists(path):
        print(
            f"error: no index database at {path} "
            "(run `cidx init` / `cidx import` first)",
            file=sys.stderr,
        )
        return 1

    def _schema_version() -> int | None:
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row[0]) if row and row[0] else None
        finally:
            con.close()

    def _entity_kinds_stale() -> bool:
        # A leftover 'nests' row (removed relation) or 'realizes' row (renamed to
        # 'implements') marks a DB whose entity_edge_kind seed must be refreshed,
        # even when schema_version is already current (an earlier build bumped the
        # version without reconciling the seed).
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT 1 FROM entity_edge_kind "
                "WHERE name IN ('nests', 'realizes') LIMIT 1"
            ).fetchone()
            return row is not None
        except sqlite3.OperationalError:
            return False  # no entity_edge_kind table (pre-v17 DB)
        finally:
            con.close()

    before = _schema_version()
    if before is not None and before > SCHEMA_VERSION:
        print(
            f"index at {path} is schema v{before}, newer than this build "
            f"(v{SCHEMA_VERSION}); refusing to touch it",
            file=sys.stderr,
        )
        return 1
    stale = _entity_kinds_stale()
    with Storage(path):  # constructing Storage applies the in-place migration
        pass
    after = _schema_version()
    if before != after:
        print(f"migrated {path}: schema v{before} -> v{after}")
    elif stale:
        print(f"migrated {path}: refreshed entity relation kinds (schema v{after})")
    else:
        print(f"{path} already at schema v{after}; nothing to migrate")
    return 0


def _resolve_version(args, root: str) -> tuple[str, str | None]:
    """Apply --version / --no-detect-version / auto-detect to produce (base, version).

    *root* is the already-normalised component root (before splitting).
    """
    if hasattr(args, "version") and args.version is not None:
        # --version "" means clear; "" treated as no version
        v = args.version if args.version else None
        return root, v
    if getattr(args, "no_detect_version", False):
        return root, None
    return compiledb.split_base_version(root)


def cmd_add_source(args) -> int:
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        print(f"error: {path} is not a directory", file=sys.stderr)
        return 1
    use_git = args.kind == "repo" and not args.no_git
    root = git_root(path) if use_git else None
    if root is not None:
        path = root
    # Component name is derived from ORIGINAL path (before version split).
    name = args.name or (repo_name(path) if use_git else os.path.basename(path))
    base, version = _resolve_version(args, path)
    with Storage(args.index) as db:
        # v24: a grouped component stores a clone-relative path, so re-adding the
        # same source resolves the EXISTING component clone-aware (its stored
        # path is no longer the absolute base) and refreshes its metadata in
        # place; only a genuinely new source mints a row.
        existing = db.get_component(base)
        if existing is not None and existing.id is not None:
            cid = existing.id
            db.update_component_meta(cid, name, args.kind, version)
        else:
            cid = db.add_component(name, base, kind=args.kind, version=version)
        # v23: group the component under a repository (same kind). The source
        # directory is its first clone and becomes active.
        repo_name_val = getattr(args, "repo", None) or name
        remote_url = git_remote_url(root) if root else None
        rid = db.add_repository(repo_name_val, args.kind, remote_url)
        clone_id = db.add_clone(rid, path)
        repo = db.get_repository_by_id(rid)
        if repo is not None and repo.active_clone_id is None:
            db.set_active_clone(rid, clone_id)
        db.set_component_repository(cid, rid)
        # v24: store the component path RELATIVE to its clone root so a later
        # `repo switch` only repoints the active clone (no path rewrite).
        db.relativize_component(cid, path)
    print(f"component #{cid}: {name} ({args.kind}) at {base}")
    return 0


def _bump_component_versions(db: Storage, commands, label_map) -> None:
    """Advance a component's stored version when a ported compile command's -I
    sits under its version-stripped base and carries a numerically HIGHER
    version segment than the one registered.

    Version selection (per the import contract): for each matched component take
    the highest include version that (a) is strictly higher than the registered
    version AND (b) names a directory that actually EXISTS on disk — "check the
    path exists before you replace, even when it is newer". An include whose
    version is missing on disk (or not higher) leaves the component version as
    registered, so its `<name>` token decodes to the registered version.

    The write goes through set_component_effective_version, which handles BOTH
    the version-as-property and version-embedded-in-path representations and is
    a no-op for ambiguous multi-row names. Mirrors the C++ port."""
    idx = db.component_alias_index()
    seen: dict[str, str] = {}
    for cmd in commands:
        for val in compiledb.include_values(compiledb.strip_for_libclang(cmd)):
            if "<" in val or "$" in val or not os.path.isabs(val):
                continue
            m = compiledb.match_alias(os.path.normpath(val), label_map)
            if m is None:
                continue
            name, vseg, _rem = m
            if vseg is None:
                continue
            cur = seen.get(name)
            if cur is None or pathx.version_key(vseg) > pathx.version_key(cur):
                seen[name] = vseg
    for name, vseg in seen.items():
        entry = idx.get(name)
        if entry is None:
            continue
        base, maxver, _bumpable = entry
        # Only a strictly-higher version is a candidate for replacement.
        if maxver is not None and pathx.version_key(vseg) <= pathx.version_key(maxver):
            continue
        # Existence guard: never repoint a component at a version dir that is
        # not present on disk, even when it is numerically newer. `base` is the
        # already-resolved, absolute, version-stripped component base.
        if not os.path.isdir(os.path.join(base, vseg)):
            continue
        db.set_component_effective_version(name, vseg)


def cmd_import(args) -> int:
    try:
        commands = compiledb.load_commands(args.db)
    except Exception as e:  # cindex raises a plain error
        print(
            f"error: cannot load compilation database from {args.db}: {e}",
            file=sys.stderr,
        )
        return 1
    if not commands:
        print("error: compilation database is empty", file=sys.stderr)
        return 1

    # Component root: the git repo owning the sources, else the directory
    # holding compile_commands.json (its basename names the component). The
    # db dir — not the first source's dir — keeps git-worktree checkouts, whose
    # `.git` is a file rather than a directory, rooted where their build db lives.
    first_src = compiledb.source_path(commands[0])
    groot = git_root(first_src)
    root = groot or compiledb.db_directory(args.db)
    name = args.name or (repo_name(root) if groot else os.path.basename(root))

    # Version is a per-component property: import only AUTO-DETECTS a trailing
    # version segment (e.g. .../1.4.0). Manual version control lives in
    # `cidx component set-version`, not on import.
    base, version = compiledb.split_base_version(root)

    imported, skipped = 0, 0
    with Storage(args.index) as db:
        # Encode include paths against the alias registry unless --no-alias.
        # The registry is explicit labels PLUS uniquely-named components, so
        # an -I under a component root auto-aliases to <component-name> with no
        # `cidx label add` needed; labels cover non-component (toolchain/system)
        # prefixes. Decode (get_alias) mirrors this same registry.
        label_map = (
            []
            if getattr(args, "no_alias", False)
            else compiledb.build_label_map(db.list_alias_pairs(), lookup=db.get_alias)
        )
        # Version-agnostic port: a ported -I under a versioned component base may
        # carry a HIGHER version than the one registered — advance the stored
        # component version so <name> decodes to it. Only bumpable components
        # (single row, version-as-property) are touched.
        if label_map:
            _bump_component_versions(db, commands, label_map)
        if args.force:
            existing = db.get_component(root)
            if existing is not None:
                # Resolved base (stored path is clone-relative for a grouped
                # component, v24); show the absolute path either way.
                existing_base = db.component_abs_base(existing)
                db.delete_component(existing.id)
                print(
                    f"force: removed existing component #{existing.id} "
                    f"at {existing_base} (files and indexed symbols)"
                )
        # The db-dir/git-root component is created LAZILY: only when a source
        # matches no already-registered component. Matching first means an
        # import whose sources are already covered by existing components
        # (e.g. sub-components Comp_1/Comp_2) does not spawn a spurious project
        # component and re-home those files under it.
        root_cid = None
        with db.transaction():
            for cmd in commands:
                src = compiledb.source_path(cmd)
                if db.component_for_path(src) is None:
                    if root_cid is None:
                        root_cid = db.add_component(name, base, version=version)
                        print(f"component #{root_cid}: {name} at {base}")
                        # The label_map was built BEFORE this lazily-created
                        # component existed, so its own -I paths would store as
                        # absolute. Rebuild the registry (now including it) and
                        # re-run the version bump so its includes encode to
                        # <name> and resolve under the right (existing) version.
                        if not getattr(args, "no_alias", False):
                            label_map = compiledb.build_label_map(
                                db.list_alias_pairs(), lookup=db.get_alias
                            )
                            _bump_component_versions(db, commands, label_map)
                    if db.component_for_path(src) is None:
                        print(
                            f"  skip (outside any component): {src}",
                            file=sys.stderr,
                        )
                        skipped += 1
                        continue
                mtime = os.path.getmtime(src) if os.path.exists(src) else None
                opts = compiledb.strip_for_libclang(cmd)
                if label_map:
                    opts = compiledb.alias_options(opts, label_map)
                db.add_file_path(
                    src,
                    mtime=mtime,
                    md5=md5_of(src),
                    compile_options=opts,
                    driver=compiledb.driver(cmd),
                )
                imported += 1
        # v23: group the imported components under a repository. Identity =
        # --repo if given, else the git/dir-derived name; remote_url (when the
        # root is a git checkout) lets two worktrees map to one repository. The
        # checkout directory (`root`) is registered as a clone and made active
        # when the repository has none yet; `repo switch` repoints it later.
        repo_name_val = getattr(args, "repo", None) or name
        remote_url = git_remote_url(groot) if groot else None
        rid = db.add_repository(repo_name_val, "repo", remote_url)
        clone_id = db.add_clone(rid, root)
        repo = db.get_repository_by_id(rid)
        if repo is not None and repo.active_clone_id is None:
            db.set_active_clone(rid, clone_id)
        attached: set[int] = set()
        for cmd in commands:
            comp = db.component_for_path(compiledb.source_path(cmd))
            if comp is None or comp.id in attached:
                continue
            attached.add(comp.id)
            if comp.repository_id is None:
                db.set_component_repository(comp.id, rid)
            # v24: store each grouped component's path RELATIVE to the clone
            # root, so `repo switch` repoints one pointer instead of N rows.
            db.relativize_component(comp.id, root)
        print(f"repository '{repo_name_val}': {len(attached)} component(s)")
    print(f"imported {imported} file(s), skipped {skipped}")
    return 0


def cmd_realias(args) -> int:
    """cidx realias [COMPONENT] -- rewrite stored include paths to <label> tokens.

    Applies the alias registry (explicit labels + uniquely-named components,
    longest match) to every file's stored compile_options, in place, so an
    index imported before labels/components existed becomes portable without a
    re-import."""
    with Storage(args.index) as db:
        label_map = compiledb.build_label_map(
            db.list_alias_pairs(), lookup=db.get_alias
        )
        if not label_map:
            print(
                "error: no aliases available (register a label with "
                "'cidx label add', or add a component)",
                file=sys.stderr,
            )
            return 1
        cid = None
        if args.component:
            try:
                comp = _lookup_component(db, args.component)
            except LookupError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            cid = comp.id
        changed, scanned = 0, 0
        for rec, _path in db.list_files(component_id=cid):
            if not rec.compile_options or rec.id is None:
                continue
            scanned += 1
            cur = list(rec.compile_options)
            new = compiledb.alias_options(cur, label_map)
            if new != cur:
                db.update_file_compile_options(rec.id, new)
                changed += 1
    print(f"realias: {changed} file(s) updated, {scanned} scanned")
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
    return db.component_abs_base(comp) if comp else None


def _index_one(db: Storage, rec: File, path: str, no_graph: bool = False) -> int:
    """Parse + index one pending file (main TU + its headers); returns 0/1."""
    if rec.id is None:
        return 1
    try:
        result = index_source(
            db,
            path,
            compiledb.resolve_options(
                compiledb.sanitize(rec.compile_options or []), db.get_alias
            ),
            rec.id,
            driver=rec.driver,
            no_graph=no_graph,
            header_options=rec.compile_options,
        )
    except ClangParseError as e:
        # The file failed to parse (e.g. a fatal header-not-found): record the
        # diagnostics so `show file` / `list files` explain why, even though no
        # AST was indexed and the file stays pending. A hard load failure (a
        # TranslationUnitLoadError) carries no per-diagnostic detail -- synthesize
        # one fatal row from the error so the failure is never silently dropped.
        diags = e.diagnostics or [
            {
                "severity": 4,  # clang.cindex.Diagnostic.Fatal
                "spelling": str(e),
                "file_path": path,
                "line": None,
                "col": None,
            }
        ]
        db.replace_diagnostics(rec.id, diags)
        print(f"error: {e}", file=sys.stderr)
        return 1
    mtime = os.path.getmtime(path) if os.path.exists(path) else None
    db.replace_diagnostics(rec.id, result["diagnostics"])
    db.mark_file_indexed(rec.id, mtime=mtime)
    h = result["headers"]
    print(
        f"  -> {result['symbols']} symbols; headers: {h['indexed']} indexed "
        f"(+{h['symbols']} symbols), {h['already']} already, "
        f"{h['system']} system, {h['unowned']} unowned"
    )
    return 0


def _index_files(
    db: Storage, files: list[str], root: str | None, no_graph: bool = False
) -> int:
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
        rc |= _index_one(db, rec, path, no_graph=no_graph)
    return rc


def _index_pending(db: Storage, no_graph: bool = False) -> int:
    """index (no args): index every pending translation-unit source.

    A header (by extension; see _is_header) is indexed via its including TU's
    index_headers() pass -- where the TU's -I/-std context resolves its types
    and a single live-DB dedup scans it exactly once per run -- never parsed
    standalone (no flags = a broken, truncated AST). So pending headers are
    deferred here; the sources that include them regenerate their edges. A TU
    source is indexed even when its compile command sanitizes to no flags (it is
    still a real TU), so its parse diagnostics land on its row.
    """
    done, skipped, failed, deferred = 0, 0, 0, 0
    for rec, path in db.files():
        if index_status(rec, path)[0]:
            skipped += 1
            continue
        if _is_header(path):
            deferred += 1  # header: indexed via its TU, not standalone
            continue
        print(f"indexing {path}")
        if _index_one(db, rec, path, no_graph=no_graph) == 0:
            done += 1
        else:
            failed += 1
    tail = f", {deferred} headers via TUs" if deferred else ""
    print(f"index: {done} indexed, {failed} failed, {skipped} already indexed{tail}")
    return 1 if failed else 0


def cmd_index(args) -> int:
    no_graph = getattr(args, "no_graph", False)
    with Storage(args.index) as db:
        try:
            root = _source_root(db, args.source)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        rc = (
            _index_files(db, args.files, root, no_graph=no_graph)
            if args.files
            else _index_pending(db, no_graph=no_graph)
        )
    if _warnings.count:
        print(f"{_warnings.count} warning(s)/error(s) logged to {log_path()}")
    return rc


def cmd_resolve(args) -> int:
    """DB-only pass: roll up edge counts, finalize cross-repo edges."""
    with Storage(args.index) as db:
        stubs, cross = db.resolve_pass()
    print(f"resolve: {stubs} still-stub, {cross} cross-repo edge(s)")
    return 0


def cmd_pch_build(args) -> int:
    """`cidx pch build`: build & cache the shared system/C++ PCH."""
    from indexer import pch

    return pch.cmd_build(
        args.index,
        add_flags=args.add_flags,
        add_headers=args.add_headers,
        driver=args.driver,
        std=args.std,
        force=args.force,
        from_corpus=args.from_corpus,
        coverage=args.coverage,
        min_tus=args.min_tus,
        jobs=args.jobs,
    )


def cmd_pch_status(args) -> int:
    """`cidx pch status`: show the cached system PCH (size, flags, validity)."""
    from indexer import pch

    return pch.cmd_status()


def cmd_pch_clear(args) -> int:
    """`cidx pch clear`: remove the cached system PCH + sidecar + umbrella."""
    from indexer import pch

    return pch.cmd_clear()


# -- set ----------------------------------------------------------------------

_SET_TRUE = {"true", "1", "yes", "on", "t", "y"}
_SET_FALSE = {"false", "0", "no", "off", "f", "n"}

# field -> (db column, invert): 'pending' is the inverse of the 'indexed' flag.
_SET_FIELDS = {
    "pending": ("indexed", True),
    "indexed": ("indexed", False),
}


def _parse_set_bool(tok: str) -> bool:
    t = tok.strip().lower()
    if t in _SET_TRUE:
        return True
    if t in _SET_FALSE:
        return False
    raise ValueError(f"expected a boolean (true/false), got {tok!r}")


def _parse_assignment(tokens: list[str]) -> tuple[str, str]:
    """'FIELD = VALUE' in any spacing ('pending=False', 'pending = False',
    'pending False') -> (field, value)."""
    expr = " ".join(tokens)
    if "=" in expr:
        key, _, val = expr.partition("=")
    else:
        parts = expr.split()
        if len(parts) != 2:
            raise ValueError("expected 'FIELD=VALUE' (e.g. pending=False)")
        key, val = parts
    key, val = key.strip().lower(), val.strip()
    if not key or not val:
        raise ValueError("expected 'FIELD=VALUE' (e.g. pending=False)")
    return key, val


def cmd_set(args) -> int:
    """Set a mutable file attribute (currently the pending/indexed flag) over a
    component's files or one file, without deleting any symbols."""
    try:
        key, raw_val = _parse_assignment(args.assignment)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if key not in _SET_FIELDS:
        print(
            f"error: unknown field {key!r}; supported: "
            f"{', '.join(sorted(_SET_FIELDS))}",
            file=sys.stderr,
        )
        return 1
    try:
        bval = _parse_set_bool(raw_val)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _column, invert = _SET_FIELDS[key]
    indexed_value = (not bval) if invert else bval  # value for the 'indexed' col

    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.file is not None:
            ap = resolve_file_arg(args.file, db.component_abs_base(comp) if comp else None)
            rec = db.get_file(ap)
            matches = [(rec.id, ap)] if rec and _under_component(db, ap, comp) else []
        else:
            matches = [
                (f.id, ap)
                for f, ap in db.list_files(component_id=comp.id if comp else None)
            ]
        if not matches:
            print("error: no files match the given selector", file=sys.stderr)
            return 1
        for fid, ap in matches:
            print(f"  #{fid}  {ap}")
        if not args.dry_run:
            for fid, _ in matches:
                db.set_file_indexed(fid, indexed_value)
        verb = "would set" if args.dry_run else "set"
        print(
            f"{verb} {key}={bval} on {len(matches)} "
            f"{_plural(len(matches), 'file', 'files')}"
        )
    return 0


# -- file (per-file compile-flag editor) --------------------------------------

_FILE_OPS = ("-set-flag", "-unset-flag", "-import-args", "-dump-args")


def _parse_file_target(db: Storage, target: str):
    """'COMPONENT://RELPATH' -> (Component, abs_path).

    Raises ValueError on a malformed target, LookupError on an unknown
    component. The relative path is resolved against the component root; a
    leading '/' is stripped so the address can never escape the component."""
    sep = "://"
    comp_name, found, rel = target.partition(sep)
    if not found or not comp_name or not rel:
        raise ValueError(
            f"expected COMPONENT://PATH (e.g. 'mylib://src/foo.c'), got {target!r}"
        )
    comp = db.get_component_by_name(comp_name)
    if comp is None:
        raise LookupError(f"no component named {comp_name!r}")
    resolved_root = db.component_abs_base(comp)
    abs_path = os.path.normpath(os.path.join(resolved_root, rel.lstrip("/")))
    return comp, abs_path


def cmd_file(args) -> int:
    """Inspect or edit one file's stored compile flags, addressed as
    COMPONENT://RELPATH. Edits mark the file args_overridden so a later
    `import` (without --force) keeps them."""
    op = list(args.op)
    if not op:
        op = ["-dump-args"]
    action, rest = op[0], op[1:]
    if action not in _FILE_OPS:
        print(
            f"error: unknown operation {action!r}; supported: {', '.join(_FILE_OPS)}",
            file=sys.stderr,
        )
        return 2

    with Storage(args.index) as db:
        try:
            _comp, ap = _parse_file_target(db, args.target)
        except (ValueError, LookupError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        rec = db.get_file(ap)
        if rec is None:
            print(f"error: not in index database: {ap}", file=sys.stderr)
            return 1
        opts = list(rec.compile_options or [])

        if action == "-dump-args":
            print(json.dumps(opts))
            return 0

        if action in ("-set-flag", "-unset-flag"):
            if len(rest) != 1:
                print(f"error: {action} takes exactly one FLAG", file=sys.stderr)
                return 2
            flag = rest[0]
            if action == "-set-flag":
                if flag in opts:
                    print(f"flag already present on {ap}: {flag}")
                    return 0
                opts.append(flag)
                db.set_file_compile_options(rec.id, opts)
                print(f"added flag to {ap}: {flag}")
                return 0
            n = opts.count(flag)
            if n == 0:
                print(f"flag not present on {ap}: {flag}")
                return 0
            opts = [o for o in opts if o != flag]
            db.set_file_compile_options(rec.id, opts)
            print(
                f"removed flag from {ap}: {flag} "
                f"({n} {_plural(n, 'occurrence', 'occurrences')})"
            )
            return 0

        # -import-args
        if len(rest) != 1:
            print(
                "error: -import-args takes exactly one JSON entry (or @FILE)",
                file=sys.stderr,
            )
            return 2
        raw = rest[0]
        if raw.startswith("@"):
            try:
                with open(raw[1:]) as fh:
                    raw = fh.read()
            except OSError as e:
                print(f"error: cannot read {raw[1:]}: {e}", file=sys.stderr)
                return 1
        try:
            commands = compiledb.commands_from_text(raw)
        except Exception as e:  # cindex raises a plain error
            print(
                f"error: -import-args: cannot parse compile command: {e}",
                file=sys.stderr,
            )
            return 1
        if not commands:
            print(
                "error: -import-args: no compile command found (need "
                "directory, file, and arguments/command)",
                file=sys.stderr,
            )
            return 1
        cmd = commands[0]
        new_opts = compiledb.strip_for_libclang(cmd)
        db.set_file_compile_options(
            rec.id, new_opts, driver=compiledb.driver(cmd), update_driver=True
        )
        print(f"imported {len(new_opts)} arg(s) for {ap}")
        return 0


def cmd_dump_compile_commands(args) -> int:
    """Emit a compile_commands.json for a component: one entry per file that has
    stored compile flags, reconstructed as {directory, file, arguments}."""
    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        entries = []
        for f, ap in db.list_files(component_id=comp.id if comp else None):
            if not f.compile_options:
                continue
            driver = f.driver or "cc"
            # directory uses the resolved effective root (real fs path), so
            # compile_commands.json consumers get real paths for directory+file.
            # The stored (indirected) -I tokens in arguments are kept verbatim.
            if comp is not None:
                directory = db.component_abs_base(comp)
            else:
                directory = os.path.dirname(ap)
            entries.append(
                {
                    "directory": directory,
                    "file": ap,
                    "arguments": [driver] + list(f.compile_options) + [ap],
                }
            )
    print(json.dumps(entries, indent=2))
    return 0


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
        repos = {r.id: r.name for r in db.list_repositories()}
        width = max((len(c.name) for c in comps), default=0)
        vw = max((len(c.version) for c in comps if c.version), default=1)
        rnames = {c.id: repos.get(c.repository_id) or "-" for c in comps}
        rw = max((len(v) for v in rnames.values()), default=1)
        for c in comps:
            ver = c.version if c.version else "-"
            rep = rnames[c.id]
            # Show the resolved (clone-anchored) base, not the stored path,
            # which is relative for a grouped component (v24).
            print(
                f"{c.id:>4}  {c.name:<{width}}  {c.kind:<8}  "
                f"{ver:<{vw}}  {rep:<{rw}}  {db.component_abs_base(c)}"
            )
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
            component_id=comp.id if comp else None, name=args.pattern
        )
        width = max((len(cname) for _, cname in rows), default=0)
        for d, cname in rows:
            print(f"{d.id:>4}  {cname:<{width}}  {d.path or '.'}")
    print(f"{len(rows)} directory(ies)")
    return 0 if rows else 1


def cmd_list_files(args) -> int:
    if args.dir is not None and not args.component:
        print(
            "error: --dir requires --component (directory paths are "
            "relative to a component root)",
            file=sys.stderr,
        )
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
            dir_path=args.dir,
            name=args.pattern,
            indexed=indexed,
        )
        # Version is a per-component property; show each file's owning-component
        # version. Map file -> directory -> component -> version (two queries).
        comp_ver = {c.id: c.version for c in db.list_components()}
        dir_comp = {d.id: d.component_id for d, _ in db.list_directories()}
        vers = [comp_ver.get(dir_comp.get(rec.directory_id)) or "-" for rec, _ in rows]
        vw = max((len(v) for v in vers), default=1)
        # Parse-diagnostic indicator: '-' clean, else e.g. '2E'/'3W'/'1E2W'.
        diag_counts = db.diagnostic_counts()
        flags = [_diag_flag(diag_counts.get(rec.id, {})) for rec, _ in rows]
        fw = max((len(f) for f in flags), default=1)
        for (rec, path), ver, flag in zip(rows, vers, flags):
            mark = "idx " if rec.indexed else "pend"
            print(f"{rec.id:>4}  {mark}  {flag:<{fw}}  {ver:<{vw}}  {path}")
    print(f"{len(rows)} file(s)")
    return 0 if rows else 1


def cmd_list_symbols(args) -> int:
    if args.dir is not None and not args.component:
        print(
            "error: --dir requires --component (directory paths are "
            "relative to a component root)",
            file=sys.stderr,
        )
        return 1
    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        file_id = None
        if args.file:
            path = resolve_file_arg(args.file, db.component_abs_base(comp) if comp else None)
            rec = db.get_file(path)
            if rec is None:
                print(f"error: not in index database: {path}", file=sys.stderr)
                return 1
            file_id = rec.id
        hits = db.list_symbols(
            component_id=comp.id if comp else None,
            dir_path=args.dir,
            file_id=file_id,
            name=args.pattern,
            kind=args.kind,
        )
        _print_symbols(db, hits, args.limit)
    return 0 if hits else 1


def cmd_show_symbol(args) -> int:
    with Storage(args.index) as db:
        ref = args.symbol
        s = db.lookup_symbol_by_id(int(ref)) if ref.isdigit() else db.lookup_symbol(ref)
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
            (
                "visibility",
                {
                    "external": "program-wide (usable from any .cpp)",
                    "internal": "file-local (static / anonymous namespace)",
                    "no-linkage": "local scope only",
                }.get(s.linkage or "", s.linkage),
            ),
            ("access", s.access),
            (
                "parent",
                f"{parent.qual_name}  [{s.parent_usr}]" if parent else s.parent_usr,
            ),
            (
                "pure",
                "yes (pure virtual; implemented by overriders)" if s.is_pure else None,
            ),
            ("definition", loc(s.file_id, s.line, s.col) if s.is_definition else None),
            (
                "declaration",
                loc(s.decl_file_id, s.decl_line, s.decl_col)
                # external/unregistered (system/stdlib) decl: raw path
                or (
                    f"{s.decl_path}:{s.decl_line}:{s.decl_col}" if s.decl_path else None
                ),
            ),
            (
                "resolved",
                "yes"
                if s.resolved
                else "n/a (pure virtual)"
                if s.is_pure
                else "no (definition not seen)",
            ),
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
        if ref.isdigit():  # first column of 'list files'
            rec = db.get_file_by_id(int(ref))
            path = db.file_abs_path(rec.id) if rec and rec.id else None
        else:
            path = resolve_file_arg(ref, db.component_abs_base(comp) if comp else None)
            rec = db.get_file(path)
        if rec is None or rec.id is None or path is None:
            print(f"error: not in index database: {ref}", file=sys.stderr)
            return 1

        d = db.get_directory_by_id(rec.directory_id)
        owner = db.get_component_by_id(d.component_id) if d else None
        syms = db.list_symbols(file_id=rec.id)
        defined = sum(1 for s in syms if s.file_id == rec.id and s.is_definition)
        declared = sum(1 for s in syms if s.decl_file_id == rec.id)
        by_kind = {}
        for s in syms:
            by_kind[s.kind] = by_kind.get(s.kind, 0) + 1

        diags = db.get_diagnostics(rec.id)
        diag_counts: dict[int, int] = {}
        for dg in diags:
            diag_counts[dg.severity] = diag_counts.get(dg.severity, 0) + 1

        def ts(epoch):
            if epoch is None:
                return None
            return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")

        fields = [
            ("id", rec.id),
            ("path", path),
            (
                "component",
                f"{owner.name} ({owner.kind})  {owner.path}" if owner else None,
            ),
            ("directory", (d.path or ".") if d else None),
            ("mtime", ts(rec.mtime)),
            ("md5", rec.md5),
            ("driver", rec.driver),
            (
                "options",
                " ".join(rec.compile_options)
                if rec.compile_options
                else "(none -- header indexed via an including TU)",
            ),
            ("indexed", index_status(rec, path)[1]),
            ("indexed at", f"{rec.indexed_at} UTC" if rec.indexed_at else None),
            (
                "symbols",
                f"{len(syms)} ({defined} defined here, {declared} declared here)",
            ),
            (
                "by kind",
                ", ".join(f"{k}: {n}" for k, n in sorted(by_kind.items()))
                if by_kind
                else None,
            ),
            ("diagnostics", _diag_summary(diag_counts) if diags else None),
        ]
        for key, value in fields:
            if value is not None:
                print(f"{key:<12} {value}")
        # Each captured parse diagnostic, in TU order, under the summary.
        for dg in diags:
            label = _DIAG_SEVERITY_LABELS.get(dg.severity, str(dg.severity))
            locstr = (
                f"{dg.file_path}:{dg.line}:{dg.col}"
                if dg.file_path is not None
                else "<no location>"
            )
            print(f"  {label:<7} {locstr}: {dg.spelling}")
    return 0


# -- delete -------------------------------------------------------------------


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def _selector_str(args) -> str:
    """The selector the user passed, for error messages: '--name foo'."""
    for flag in ("id", "name", "path", "usr"):
        val = getattr(args, flag, None)
        if val is not None:
            return f"--{flag} {val}"
    return "<no selector>"


def _under_component(db: Storage, abs_path: str | None, comp) -> bool:
    """True when comp is None, or abs_path lies within the component's effective
    root (clone-anchored for a grouped component, v24)."""
    if comp is None:
        return True
    if abs_path is None:
        return False
    root = db.component_abs_base(comp).rstrip(os.sep)
    return abs_path == root or abs_path.startswith(root + os.sep)


def _finish_delete(args, ids, lines, delete_fn, singular, plural) -> int:
    """Shared tail: print the matched rows, delete (unless --dry-run), summarize."""
    for line in lines:
        print(line)
    if not args.dry_run:
        for row_id in ids:
            delete_fn(row_id)
    verb = "would delete" if args.dry_run else "deleted"
    print(f"{verb} {len(ids)} {_plural(len(ids), singular, plural)}")
    return 0


def cmd_delete_component(args) -> int:
    with Storage(args.index) as db:
        if args.id is not None:
            c = db.get_component_by_id(args.id)
            matches = [c] if c else []
        elif args.path is not None:
            c = db.get_component(os.path.abspath(args.path))
            matches = [c] if c else []
        else:
            matches = [c for c in db.list_components() if c.name == args.name]
        if not matches:
            print(f"error: no component matches {_selector_str(args)}", file=sys.stderr)
            return 1
        lines = [f"  #{c.id}  {c.name} ({c.kind})  {c.path}" for c in matches]
        return _finish_delete(
            args,
            [c.id for c in matches],
            lines,
            db.delete_component,
            "component",
            "components",
        )


def cmd_delete_dir(args) -> int:
    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.id is not None:
            d = db.get_directory_by_id(args.id)
            matches = (
                [d] if d and _under_component(db, db.directory_abs_path(d.id), comp) else []
            )
        else:
            target = os.path.abspath(args.path)
            matches = [
                d
                for d, _cn in db.list_directories(
                    component_id=comp.id if comp else None
                )
                if db.directory_abs_path(d.id) == target
            ]
        if not matches:
            print(f"error: no directory matches {_selector_str(args)}", file=sys.stderr)
            return 1
        lines = [f"  #{d.id}  {db.directory_abs_path(d.id)}" for d in matches]
        return _finish_delete(
            args,
            [d.id for d in matches],
            lines,
            db.delete_directory,
            "directory",
            "directories",
        )


def cmd_delete_file(args) -> int:
    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        matches = []  # list of (file_id, abs_path)
        if args.id is not None:
            rec = db.get_file_by_id(args.id)
            ap = db.file_abs_path(rec.id) if rec else None
            if rec and _under_component(db, ap, comp):
                matches = [(rec.id, ap)]
        elif args.path is not None:
            ap = resolve_file_arg(args.path, db.component_abs_base(comp) if comp else None)
            rec = db.get_file(ap)
            if rec and _under_component(db, ap, comp):
                matches = [(rec.id, ap)]
        else:
            matches = [
                (f.id, ap)
                for f, ap in db.files()
                if os.path.basename(ap) == args.name and _under_component(db, ap, comp)
            ]
        if not matches:
            print(f"error: no file matches {_selector_str(args)}", file=sys.stderr)
            return 1
        lines = [f"  #{fid}  {ap}" for fid, ap in matches]
        return _finish_delete(
            args, [fid for fid, _ in matches], lines, db.delete_file, "file", "files"
        )


def cmd_delete_symbol(args) -> int:
    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.id is not None:
            s = db.lookup_symbol_by_id(args.id)
            matches = [s] if s else []
        elif args.usr is not None:
            s = db.lookup_symbol(args.usr)
            matches = [s] if s else []
        else:
            matches = db.lookup_symbols_by_name(args.name)
        if comp is not None:

            def in_comp(s):
                here = db.file_abs_path(s.file_id) if s.file_id else None
                decl = db.file_abs_path(s.decl_file_id) if s.decl_file_id else None
                return (here is not None and _under_component(db, here, comp)) or (
                    decl is not None and _under_component(db, decl, comp)
                )

            matches = [s for s in matches if in_comp(s)]
        if not matches:
            print(f"error: no symbol matches {_selector_str(args)}", file=sys.stderr)
            return 1
        lines = [f"  #{s.id}  {s.kind}  {s.qual_name or s.spelling}" for s in matches]
        return _finish_delete(
            args, [s.id for s in matches], lines, db.delete_symbol, "symbol", "symbols"
        )


# -- graph --------------------------------------------------------------------
#
# Query the relationship graph built by `index`/`resolve`. Every graph command
# operates on the standard index (cache_dir()/index.db) unless --db overrides it,
# and refuses to run against a missing or edge-less DB (no silent substitution).


def _open_graph(args) -> "GraphQuery | None":
    """Open the standard index read-only, enforcing the no-edges rule.

    Returns a GraphQuery, or None after printing a clear error (the caller then
    returns a non-zero exit code via _GRAPH_ERR)."""
    try:
        return GraphQuery(args.index, require_edges=True)
    except (NoIndexError, NoEdgesError) as e:
        print(f"error: {e}", file=sys.stderr)
        return None


def _edge_kinds(spec: str | None) -> tuple[str, ...] | None:
    """Parse a comma-separated edge-kind list ('calls,uses') into a tuple."""
    if not spec:
        return None
    kinds = tuple(k.strip() for k in spec.split(",") if k.strip())
    return kinds or None


def _select_one(g: "GraphQuery", usr, sym_id, name, kind, first):
    """Resolve a shared --usr/--id/--name selector to a single Sym.

    Returns (sym, exit_code). On an ambiguous --name, prints the candidates and
    returns (None, 2) unless `first` is set."""
    if usr is not None:
        s = g.get(usr)
        if s is None:
            print(f"error: no symbol with USR {usr!r}", file=sys.stderr)
            return None, 1
        return s, 0
    if sym_id is not None:
        s = g.get(int(sym_id))
        if s is None:
            print(f"error: no symbol with id {sym_id}", file=sys.stderr)
            return None, 1
        return s, 0
    hits = g.find(name, kind=kind)
    if not hits:
        print(
            f"error: no symbol matches --name {name!r}"
            + (f" (kind {kind})" if kind else ""),
            file=sys.stderr,
        )
        return None, 1
    if len(hits) > 1 and not first:
        print(
            f"error: --name {name!r} matches {len(hits)} symbols; "
            f"disambiguate with --usr/--id (or pass --first):",
            file=sys.stderr,
        )
        for s in hits[:25]:
            print(
                f"  #{s.id}  {s.kind:<14} {s.name}  @{s.loc}  [{s.usr}]",
                file=sys.stderr,
            )
        if len(hits) > 25:
            print(f"  ... and {len(hits) - 25} more", file=sys.stderr)
        return None, 2
    return hits[0], 0


def _select_symbol(g, args):
    """The primary subject symbol from the shared selector flags."""
    return _select_one(g, args.usr, args.id, args.name, args.kind, args.first)


def _emit_edges(g: "GraphQuery", edges, args, header: str) -> None:
    """Print a list of Edge results -- JSON (peer + edge_kind/count/sites) or a
    human table with xN counts and a sample site."""
    if args.json:
        print(json.dumps([e.to_dict(sites=g.sites(e)) for e in edges], indent=2))
        return
    print(header)
    width = max((len(e.peer.name or e.peer.usr) for e in edges), default=0)
    for e in edges:
        cnt = f"  x{e.count}" if e.count and e.count != 1 else ""
        sample = g.sites(e, limit=1)
        site = f"  ({sample[0].loc})" if sample else ""
        stub = "  [stub]" if e.peer.is_stub else ""
        nm = e.peer.name or e.peer.usr
        print(f"  {e.peer.kind:<14} {nm:<{width}}  @{e.peer.loc}{cnt}{site}{stub}")
    print(f"{len(edges)} result(s)")


def _emit_syms(syms, args, header: str, depths: dict | None = None) -> None:
    """Print a list of Sym results -- JSON (stable schema) or a human table.
    `depths` (id -> distance) annotates walk output."""
    if args.json:
        out = []
        for s in syms:
            d = s.to_dict()
            if depths is not None:
                d["depth"] = depths.get(s.id)
            out.append(d)
        print(json.dumps(out, indent=2))
        return
    print(header)
    width = max((len(s.name or s.usr) for s in syms), default=0)
    for s in syms:
        dep = f"  d{depths[s.id]}" if depths is not None and s.id in depths else ""
        stub = "  [stub]" if s.is_stub else ""
        nm = s.name or s.usr
        print(f"  {s.kind:<14} {nm:<{width}}  @{s.loc}{dep}{stub}")
    print(f"{len(syms)} result(s)")


def cmd_graph_callers(args) -> int:
    g = _open_graph(args)
    if g is None:
        return 1
    sym, rc = _select_symbol(g, args)
    if sym is None:
        return rc
    # Virtual-dispatch callers (materialised dispatch_calls edges) are included
    # by default; --direct-only restricts to the literal incoming calls edges.
    kinds = ("calls",) if args.direct_only else ("calls", "dispatch_calls")
    edges = g.edges_in(sym, kinds, limit=args.limit)
    _emit_edges(g, edges, args, f"callers of {sym.name} (@{sym.loc}):")
    return 0


def cmd_graph_callees(args) -> int:
    g = _open_graph(args)
    if g is None:
        return 1
    sym, rc = _select_symbol(g, args)
    if sym is None:
        return rc
    # Virtual-dispatch targets (materialised dispatch_calls edges) are included
    # by default; --direct-only restricts to the literal outgoing calls edges.
    kinds = ("calls",) if args.direct_only else ("calls", "dispatch_calls")
    edges = g.edges_out(sym, kinds, limit=args.limit)
    _emit_edges(g, edges, args, f"callees of {sym.name} (@{sym.loc}):")
    return 0


def cmd_graph_refs(args) -> int:
    g = _open_graph(args)
    if g is None:
        return 1
    sym, rc = _select_symbol(g, args)
    if sym is None:
        return rc
    edges = g.references(sym, limit=args.limit)
    _emit_edges(g, edges, args, f"references to {sym.name} (@{sym.loc}):")
    return 0


def cmd_graph_neighbors(args) -> int:
    g = _open_graph(args)
    if g is None:
        return 1
    sym, rc = _select_symbol(g, args)
    if sym is None:
        return rc
    try:
        edges = g._edges(sym, args.direction, _edge_kinds(args.edge), args.limit)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    kinds = args.edge or "all"
    _emit_edges(
        g,
        edges,
        args,
        f"{args.direction}-neighbors of {sym.name} (@{sym.loc}) over {kinds}:",
    )
    return 0


def cmd_graph_walk(args) -> int:
    g = _open_graph(args)
    if g is None:
        return 1
    sym, rc = _select_symbol(g, args)
    if sym is None:
        return rc
    kinds = _edge_kinds(args.edge) or ("calls",)
    try:
        tr = g.walk(
            sym, kinds, direction=args.direction, depth=args.depth, max_nodes=args.limit
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    nodes = [n for n in tr.nodes if n.id != sym.id]  # exclude the start node
    _emit_syms(
        nodes,
        args,
        f"reachable from {sym.name} (@{sym.loc}) over "
        f"{','.join(kinds)} {args.direction}, depth<={args.depth}:",
        depths=tr.depth_by_id,
    )
    return 0


def cmd_graph_path(args) -> int:
    g = _open_graph(args)
    if g is None:
        return 1
    src, rc = _select_symbol(g, args)
    if src is None:
        return rc
    dst, rc = _select_one(
        g, args.to_usr, args.to_id, args.to_name, args.to_kind, args.first
    )
    if dst is None:
        return rc
    kinds = _edge_kinds(args.edge) or ("calls",)
    try:
        chain = g.reaches(
            src, dst, kinds=kinds, direction=args.direction, max_depth=args.depth
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if chain is None:
        if args.json:
            print("null")
        else:
            print(
                f"no path from {src.name} to {dst.name} over "
                f"{','.join(kinds)} {args.direction} within depth {args.depth}"
            )
        return 1
    _emit_syms(chain, args, f"path {src.name} -> {dst.name} ({len(chain) - 1} hop(s)):")
    return 0


def cmd_graph_hierarchy(args) -> int:
    g = _open_graph(args)
    if g is None:
        return 1
    sym, rc = _select_symbol(g, args)
    if sym is None:
        return rc
    direct = not args.transitive
    access = getattr(args, "access", None)
    bases = g.bases(sym, direct=direct)
    subs = g.subclasses(sym, direct=direct)
    try:
        mems = g.members(sym, access=None if access == "all" else access)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                {
                    "symbol": sym.to_dict(),
                    "bases": [s.to_dict() for s in bases],
                    "subclasses": [s.to_dict() for s in subs],
                    "members": [s.to_dict() for s in mems],
                },
                indent=2,
            )
        )
        return 0
    scope = "all" if args.transitive else "direct"
    print(f"hierarchy of {sym.name} (@{sym.loc}):")
    _emit_syms(bases, args, f"  bases ({scope}):")
    _emit_syms(subs, args, f"  subclasses ({scope}):")
    _emit_syms(mems, args, "  members:")
    return 0


def cmd_graph_dispatch(args) -> int:
    g = _open_graph(args)
    if g is None:
        return 1
    sym, rc = _select_symbol(g, args)
    if sym is None:
        return rc
    targets = g.dispatch_targets(sym)
    virtual = g.is_virtual_method(sym)
    if args.json:
        print(
            json.dumps(
                {
                    "method": sym.to_dict(),
                    "is_virtual": virtual,
                    "targets": [s.to_dict() for s in targets],
                },
                indent=2,
            )
        )
        return 0
    note = "" if virtual else "  (not a virtual method -- only itself)"
    _emit_syms(
        targets, args, f"run-time dispatch targets of {sym.name} (@{sym.loc}){note}:"
    )
    return 0


def cmd_graph_redefined(args) -> int:
    """cidx graph redefined -- symbols defined in >1 backend (multi_def > 1)."""
    g = _open_graph(args)
    if g is None:
        return 1
    syms = g.redefined(limit=args.limit)
    _emit_syms(syms, args, "symbols redefined per backend (multi_def > 1):")
    return 0


def cmd_graph_definitions(args) -> int:
    """cidx graph definitions SYM -- each backend body of the symbol, and (unless
    --direct-only) the possible-call target bodies it fans out to."""
    g = _open_graph(args)
    if g is None:
        return 1
    sym, rc = _select_symbol(g, args)
    if sym is None:
        return rc
    defs = g.definitions(sym)
    possible = [] if args.direct_only else g.possible_callees(sym)
    if args.json:
        print(
            json.dumps(
                {
                    "symbol": sym.to_dict(),
                    "multi_def": sym.multi_def,
                    "definitions": [d.to_dict() for d in defs],
                    "possible_callees": [d.to_dict() for d in possible],
                },
                indent=2,
            )
        )
        return 0
    print(f"definitions of {sym.name} ({len(defs)} backend body/bodies):")
    for d in defs:
        print(f"  {d.sym.kind:<14} @{d.loc}  component={d.component}")
    if possible:
        print(f"possible-call targets from {sym.name}:")
        w = max((len(d.sym.name or d.sym.usr) for d in possible), default=0)
        for d in possible:
            print(f"  {(d.sym.name or d.sym.usr):<{w}}  @{d.loc}")
    return 0


# -- component show / set-version --------------------------------------------


def cmd_component_show(args) -> int:
    """cidx component show NAME -- print six fields for a component."""
    with Storage(args.index) as db:
        comp = db.get_component_by_name(args.name)
        if comp is None:
            print(f"error: no component named {args.name!r}", file=sys.stderr)
            return 1
        eff = Storage.effective_root(comp)
        resolved = db.component_abs_base(comp)
        version_str = comp.version if comp.version is not None else "(none)"
        fields = [
            ("name", comp.name),
            ("kind", comp.kind),
            ("base path", comp.path),
            ("version", version_str),
            ("effective root", eff),
            ("resolved root", resolved),
        ]
        for key, value in fields:
            print(f"{key:<14} {value}")
    return 0


def cmd_component_set_version(args) -> int:
    """cidx component set-version NAME [V] -- set or clear a component's version."""
    # V is optional; absent or "" means clear.
    version = args.version if args.version else None
    with Storage(args.index) as db:
        ok = db.set_component_version(args.name, version)
    if not ok:
        print(f"error: no component named '{args.name}'", file=sys.stderr)
        return 1
    if version is not None:
        print(f"component '{args.name}' version set to {version}")
    else:
        print(f"component '{args.name}' version cleared")
    return 0


# -- repo list / show / add-clone / switch / rm (v23) ------------------------


def _active_clone_path(db: Storage, repo) -> str | None:
    """Resolved path of a repository's active clone, or None."""
    if repo.active_clone_id is None:
        return None
    cl = db.get_clone_by_id(repo.active_clone_id)
    return cl.path if cl is not None else None


def cmd_repo_list(args) -> int:
    """cidx repo list -- one line per repository with component/clone counts."""
    with Storage(args.index) as db:
        repos = db.list_repositories(name=args.pattern, kind=args.kind)
        width = max((len(r.name) for r in repos), default=0)
        for r in repos:
            ncomp = len(db.components_for_repository(r.id))
            nclone = len(db.list_clones(r.id))
            active = _active_clone_path(db, r) or "-"
            print(
                f"{r.id:>4}  {r.name:<{width}}  {r.kind:<8}  "
                f"{ncomp} component(s)  {nclone} clone(s)  {active}"
            )
    print(f"{len(repos)} repositories")
    return 0 if repos else 1


def cmd_repo_show(args) -> int:
    """cidx repo show NAME -- repository detail, its clones and components."""
    with Storage(args.index) as db:
        repo = db.get_repository_by_name(args.name)
        if repo is None:
            print(f"error: no repository named '{args.name}'", file=sys.stderr)
            return 1
        clones = db.list_clones(repo.id)
        comps = db.components_for_repository(repo.id)
        remote = repo.remote_url if repo.remote_url else "(none)"
        active = _active_clone_path(db, repo) or "(none)"
        for key, value in (
            ("name", repo.name),
            ("kind", repo.kind),
            ("remote", remote),
            ("active clone", active),
            ("clones", str(len(clones))),
            ("components", str(len(comps))),
        ):
            print(f"{key:<14} {value}")
        if clones:
            lw = max((len(c.label) for c in clones if c.label), default=1)
            print("clones:")
            for c in clones:
                mark = "*" if c.id == repo.active_clone_id else " "
                lbl = c.label if c.label else "-"
                print(f"  {mark} {c.id:>4}  {lbl:<{lw}}  {c.path}")
        if comps:
            cw = max((len(c.name) for c in comps), default=0)
            print("components:")
            for c in comps:
                print(f"    {c.id:>4}  {c.name:<{cw}}  {db.component_abs_base(c)}")
    return 0


def cmd_repo_add_clone(args) -> int:
    """cidx repo add-clone NAME PATH [--label L] -- register a checkout dir."""
    with Storage(args.index) as db:
        repo = db.get_repository_by_name(args.name)
        if repo is None:
            print(f"error: no repository named '{args.name}'", file=sys.stderr)
            return 1
        if not os.path.isdir(os.path.abspath(args.path)):
            print(
                f"warning: {args.path} is not a directory (registered anyway)",
                file=sys.stderr,
            )
        clone_id = db.add_clone(repo.id, args.path, label=args.label)
        if repo.active_clone_id is None:
            db.set_active_clone(repo.id, clone_id)
        cl = db.get_clone_by_id(clone_id)
    print(f"clone #{clone_id}: {cl.path} added to '{args.name}'")
    return 0


def cmd_repo_switch(args) -> int:
    """cidx repo switch NAME TARGET -- point the repo at another clone.

    TARGET matches a registered clone by exact path or by label. v24: grouped
    component paths are stored RELATIVE to the active clone, so a switch is a
    single `active_clone_id` update -- no component row is rewritten; every
    path resolves against the new clone root automatically."""
    with Storage(args.index) as db:
        repo = db.get_repository_by_name(args.name)
        if repo is None:
            print(f"error: no repository named '{args.name}'", file=sys.stderr)
            return 1
        clones = db.list_clones(repo.id)
        target = None
        want = os.path.abspath(args.target)
        for c in clones:
            if c.path == want or c.label == args.target:
                target = c
                break
        if target is None:
            print(
                f"error: '{args.target}' is not a clone of '{args.name}' "
                f"(add it with 'cidx repo add-clone')",
                file=sys.stderr,
            )
            return 1
        if not os.path.isdir(target.path):
            print(
                f"warning: clone path {target.path} is not present on disk",
                file=sys.stderr,
            )
        ncomp = len(db.components_for_repository(repo.id))
        db.set_active_clone(repo.id, target.id)
    print(f"switched '{args.name}' to {target.path} ({ncomp} component(s))")
    return 0


def cmd_repo_rm(args) -> int:
    """cidx repo rm NAME [--delete-components] -- remove a repository."""
    with Storage(args.index) as db:
        repo = db.get_repository_by_name(args.name)
        if repo is None:
            print(f"error: no repository named '{args.name}'", file=sys.stderr)
            return 1
        comps = db.components_for_repository(repo.id)
        if args.delete_components:
            for c in comps:
                db.delete_component(c.id)
        db.delete_repository(repo.id)
    if args.delete_components:
        print(
            f"removed repository '{args.name}' and {len(comps)} component(s)"
        )
    else:
        print(
            f"removed repository '{args.name}'; "
            f"{len(comps)} component(s) detached"
        )
    return 0


# -- label add / rm / list / resolve -----------------------------------------


def cmd_label_add(args) -> int:
    """cidx label add NAME PATH -- upsert a label."""
    with Storage(args.index) as db:
        existing = db.get_label(args.name)
        db.add_label(args.name, args.path)
    if existing is None:
        print(f"added label {args.name} -> {args.path}")
    else:
        print(f"updated label {args.name} -> {args.path}")
    return 0


def cmd_label_rm(args) -> int:
    """cidx label rm NAME -- remove a label."""
    with Storage(args.index) as db:
        removed = db.remove_label(args.name)
    if not removed:
        print(f"error: no label named '{args.name}'", file=sys.stderr)
        return 1
    print(f"removed label {args.name}")
    return 0


def cmd_label_list(args) -> int:
    """cidx label list -- print all labels sorted by name."""
    with Storage(args.index) as db:
        labels = db.list_labels()
    if not labels:
        print("0 label(s)")
        return 0
    width = max(len(name) for name, _ in labels)
    for name, path in labels:
        print(f"{name:<{width}}  {path}")
    print(f"{len(labels)} label(s)")
    return 0


def cmd_label_resolve(args) -> int:
    """cidx label resolve TOKEN -- resolve a label/component name or <...>/$... token.

    Uses the same alias lookup as parse-time decode (get_alias): explicit
    labels first, then uniquely-named components."""
    with Storage(args.index) as db:
        lookup = db.get_alias
        autoderive = not getattr(args, "no_autoderive_labels", False)
        token = args.token
        # If no < or $ in the token, treat it as a bare label name.
        if "<" not in token and "$" not in token:
            token = f"<{token}>"
        result = pathx.resolve_fs_path(token, lookup=lookup, autoderive=autoderive)
        # Apply abspath only for bare paths (not compound tokens like -I<...>).
        # resolve_fs_path docstring: abspath is the caller's responsibility.
        if not result.startswith("-"):
            result = os.path.abspath(result)
    print(result)
    return 0


def cmd_verify(args) -> int:
    """cidx verify -- check that component roots and files exist on disk.

    For each component the effective root (base path joined with its version) is
    resolved through the portable-path chain and confirmed to be a directory. A
    *versioned* component whose base path exists but whose versioned sub-directory
    is absent is reported as VER-MISS (the version no longer matches what is on
    disk). For each file the reconstructed absolute path is confirmed to be a
    regular file. Only failing files are listed unless --all is given; every
    component is always listed. Exit status is non-zero if anything is missing.
    """
    with Storage(args.index) as db:
        try:
            comp = _lookup_component(db, args.component)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        components = [comp] if comp else db.list_components()

        c_ok = c_missing = c_vermiss = 0
        for c in components:
            resolved = db.component_abs_base(c)
            if os.path.isdir(resolved):
                status = "ok"
                c_ok += 1
            elif c.version and os.path.isdir(
                db.component_abs_base(replace(c, version=None))
            ):
                status = "VER-MISS"
                c_vermiss += 1
            else:
                status = "MISSING"
                c_missing += 1
            print(f"component  {status:<8}  {c.name}  {resolved}")

        f_ok = f_missing = 0
        rows = db.list_files(component_id=comp.id if comp else None)
        for rec, path in rows:
            if os.path.isfile(path):
                f_ok += 1
                if args.all:
                    print(f"file  ok        {path}")
            else:
                f_missing += 1
                print(f"file  MISSING   {path}")

        print()
        print(
            f"components: {c_ok} ok, {c_missing} missing, "
            f"{c_vermiss} version-mismatch"
        )
        print(f"files: {f_ok} ok, {f_missing} missing")
    return 0 if (c_missing == 0 and c_vermiss == 0 and f_missing == 0) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cidx", description="cidx command-line skeleton")
    ap.add_argument("--version", action="version", version=f"cidx {VERSION}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a blank index database")
    p.add_argument(
        "--force", action="store_true", help="overwrite an existing index database"
    )
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser(
        "migrate",
        help="upgrade an existing index to the current schema (in place, no re-index)",
    )
    p.add_argument(
        "--db",
        dest="graph_db",
        metavar="PATH",
        help="index database (default: the standard cache index)",
    )
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("add-source", help="register a component")
    p.add_argument("--path", required=True, help="repo root or library header dir")
    p.add_argument("--name", help="component name (default: from .git/config)")
    p.add_argument(
        "--repo", help="repository name to group under (default: component name)"
    )
    p.add_argument("--kind", choices=("repo", "external"), default="repo")
    p.add_argument(
        "--no-git",
        action="store_true",
        help="use --path as-is; do not promote to the enclosing git root",
    )
    p.add_argument(
        "--version",
        metavar="V",
        default=None,
        help="set component version to V (overrides auto-detection; '' clears)",
    )
    p.add_argument(
        "--no-detect-version",
        action="store_true",
        help="disable trailing-segment version detection",
    )
    p.set_defaults(fn=cmd_add_source)

    p = sub.add_parser("import", help="import a compile_commands.json")
    p.add_argument(
        "--db",
        required=True,
        help="compile_commands.json (or the directory holding it)",
    )
    p.add_argument("--name", help="component name override")
    p.add_argument(
        "--repo",
        help="repository name to group the imported components under "
        "(default: the git/dir-derived name)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="reimport: delete the existing component (its files "
        "and indexed symbols) before importing",
    )
    p.add_argument(
        "--no-alias",
        dest="no_alias",
        action="store_true",
        help="do not rewrite include paths to <label> tokens via the registry",
    )
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser(
        "realias",
        help="rewrite stored include paths to <label> tokens via the registry",
    )
    p.add_argument(
        "component",
        nargs="?",
        metavar="COMPONENT",
        help="restrict to one component (default: all files)",
    )
    p.add_argument(
        "--db",
        dest="graph_db",
        metavar="PATH",
        help="index database (default: the standard cache index)",
    )
    p.set_defaults(fn=cmd_realias)

    p = sub.add_parser("index", help="index imported C/C++ files")
    p.add_argument(
        "files", nargs="*", help="restrict to these files (default: all pending)"
    )
    p.add_argument(
        "--source",
        metavar="COMPONENT",
        help="resolve relative FILE paths against this component's root",
    )
    p.add_argument(
        "--no-graph",
        dest="no_graph",
        action="store_true",
        help="skip relationship-graph extraction (calls, inherits, …)",
    )
    p.add_argument(
        "--no-autoderive-labels",
        dest="no_autoderive_labels",
        action="store_true",
        help="disable label autoderive fallback at parse time",
    )
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser(
        "resolve", help="finalize cross-repo edges and roll up edge counts"
    )
    p.set_defaults(fn=cmd_resolve)

    # -- pch: build / status / clear ------------------------------------------
    p = sub.add_parser(
        "pch",
        help="build & cache one shared system/C++ PCH to speed up indexing",
    )
    psub = p.add_subparsers(dest="pch_action", required=True)

    q = psub.add_parser(
        "build", help="compile a system/C++ umbrella header into a cached PCH"
    )
    q.add_argument(
        "--db",
        dest="graph_db",
        metavar="PATH",
        help="index database to derive the common C++ flags from "
        "(default: the standard cache index)",
    )
    q.add_argument(
        "--add",
        dest="add_flags",
        action="append",
        default=[],
        metavar="FLAG",
        help="extra compile flag to bake into the PCH (repeatable)",
    )
    q.add_argument(
        "--include",
        dest="add_headers",
        action="append",
        default=[],
        metavar="HEADER",
        help="extra header to add to the umbrella, e.g. boost/optional.hpp "
        "(repeatable)",
    )
    q.add_argument(
        "--driver",
        help="compiler driver to replicate search paths from "
        "(default: the index's dominant C++ driver)",
    )
    q.add_argument("--std", help="override the C++ standard, e.g. c++17")
    q.add_argument(
        "--force", action="store_true", help="rebuild even if a PCH already exists"
    )
    q.add_argument(
        "--from-corpus",
        action="store_true",
        help="build the umbrella from the headers actually shared by the index's "
        "C++ TUs (a `clang -M` survey), retaining -I so project headers are "
        "included -- the lever for parse-bound cold indexing",
    )
    q.add_argument(
        "--coverage",
        type=float,
        default=0.7,
        metavar="FRAC",
        help="with --from-corpus: include a header if shared by >= this fraction "
        "of C++ TUs (default: 0.7)",
    )
    q.add_argument(
        "--min-tus",
        type=int,
        default=0,
        metavar="N",
        help="with --from-corpus: also require a header in >= N TUs (default: 0)",
    )
    q.add_argument(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help="with --from-corpus: parallel `clang -M` scans (default: CPU count)",
    )
    q.set_defaults(fn=cmd_pch_build)

    q = psub.add_parser(
        "status", help="show the cached system PCH (size, flags, validity)"
    )
    q.set_defaults(fn=cmd_pch_status)

    q = psub.add_parser("clear", help="remove the cached system PCH")
    q.set_defaults(fn=cmd_pch_clear)

    # -- component: show / set-version ----------------------------------------
    p = sub.add_parser("component", help="inspect or modify a component")
    csub = p.add_subparsers(dest="comp_action", required=True)

    def _db_arg(q):
        """Add a standard --db (index database override) argument."""
        q.add_argument(
            "--db",
            dest="graph_db",
            metavar="PATH",
            help="index database (default: the standard cache index)",
        )

    q = csub.add_parser("show", help="show details for a component")
    q.add_argument("name", metavar="NAME", help="component name")
    _db_arg(q)
    q.set_defaults(fn=cmd_component_show)

    q = csub.add_parser("set-version", help="set or clear a component's version")
    q.add_argument("name", metavar="NAME", help="component name")
    q.add_argument(
        "version",
        nargs="?",
        default="",
        metavar="V",
        help="version string; omit or pass '' to clear",
    )
    _db_arg(q)
    q.set_defaults(fn=cmd_component_set_version)

    # -- repo: list / show / add-clone / switch / rm (v23) --------------------
    p = sub.add_parser(
        "repo", help="group components into repositories; switch clones"
    )
    rsub = p.add_subparsers(dest="repo_action", required=True)

    q = rsub.add_parser("list", help="list repositories", aliases=["ls"])
    q.add_argument(
        "pattern", nargs="?", default=None, help="fuzzy-filter by repository name"
    )
    q.add_argument(
        "--kind", choices=("repo", "external"), help="filter by repository kind"
    )
    _db_arg(q)
    q.set_defaults(fn=cmd_repo_list)

    q = rsub.add_parser("show", help="show a repository's clones and components")
    q.add_argument("name", metavar="NAME", help="repository name")
    _db_arg(q)
    q.set_defaults(fn=cmd_repo_show)

    q = rsub.add_parser("add-clone", help="register another checkout directory")
    q.add_argument("name", metavar="NAME", help="repository name")
    q.add_argument("path", metavar="PATH", help="checkout/worktree directory")
    q.add_argument("--label", help="optional label (e.g. branch/worktree name)")
    _db_arg(q)
    q.set_defaults(fn=cmd_repo_add_clone)

    q = rsub.add_parser(
        "switch", help="rebase the repository onto another clone (by path/label)"
    )
    q.add_argument("name", metavar="NAME", help="repository name")
    q.add_argument("target", metavar="TARGET", help="clone path or label")
    _db_arg(q)
    q.set_defaults(fn=cmd_repo_switch)

    q = rsub.add_parser("rm", help="remove a repository")
    q.add_argument("name", metavar="NAME", help="repository name")
    q.add_argument(
        "--delete-components",
        action="store_true",
        help="also delete the grouped components and their indexed symbols",
    )
    _db_arg(q)
    q.set_defaults(fn=cmd_repo_rm)

    # -- label: add / rm / list / resolve -------------------------------------
    p = sub.add_parser("label", help="manage include/arg label registry")
    lsub = p.add_subparsers(dest="label_action", required=True)

    q = lsub.add_parser("add", help="add or update a label")
    q.add_argument("name", metavar="NAME", help="label name (e.g. libfoo-include)")
    q.add_argument("path", metavar="PATH", help="stored path (may contain $VAR)")
    _db_arg(q)
    q.set_defaults(fn=cmd_label_add)

    q = lsub.add_parser("rm", help="remove a label")
    q.add_argument("name", metavar="NAME", help="label name")
    _db_arg(q)
    q.set_defaults(fn=cmd_label_rm)

    q = lsub.add_parser("list", help="list all labels")
    _db_arg(q)
    q.set_defaults(fn=cmd_label_list)

    q = lsub.add_parser("resolve", help="resolve a label name or <...>/$... token")
    q.add_argument(
        "token",
        metavar="TOKEN",
        help="bare label name or a token containing <...> / $...",
    )
    q.add_argument(
        "--no-autoderive-labels",
        dest="no_autoderive_labels",
        action="store_true",
        help="disable the /name/replace-dash autoderive fallback",
    )
    _db_arg(q)
    q.set_defaults(fn=cmd_label_resolve)

    # -- verify: check component roots and files exist on disk -----------------
    p = sub.add_parser(
        "verify", help="check that component roots and files exist on disk"
    )
    p.add_argument(
        "--component",
        "-c",
        metavar="NAME",
        help="restrict to one component (default: all)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="also list files that exist (default: only failures)",
    )
    _db_arg(p)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("set", help="set a mutable file attribute (e.g. pending status)")
    p.add_argument(
        "assignment",
        nargs="+",
        metavar="FIELD=VALUE",
        help="attribute assignment, e.g. 'pending=False' (fields: pending, indexed)",
    )
    p.add_argument(
        "--component", "-c", metavar="NAME", help="restrict to this component's files"
    )
    p.add_argument(
        "--file",
        metavar="REL_PATH",
        help="restrict to one file (path relative to component root)",
    )
    p.add_argument(
        "--db",
        dest="graph_db",
        metavar="PATH",
        help="operate on this index DB (default: the standard index)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="preview the matches without changing anything",
    )
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("file", help="inspect or edit one file's stored compile flags")
    p.add_argument(
        "target",
        metavar="COMPONENT://PATH",
        help="file address, e.g. 'mylib://src/foo.c'",
    )
    p.add_argument(
        "op",
        nargs=argparse.REMAINDER,
        metavar="OP",
        help="-set-flag FLAG | -unset-flag FLAG | -import-args JSON "
        "| -dump-args (default when omitted)",
    )
    p.add_argument(
        "--db",
        dest="graph_db",
        metavar="PATH",
        help="operate on this index DB (default: the standard index)",
    )
    p.set_defaults(fn=cmd_file)

    p = sub.add_parser(
        "dump-compile-commands", help="emit a compile_commands.json for a component"
    )
    p.add_argument(
        "component", metavar="COMPONENT", help="component whose files to emit"
    )
    p.add_argument(
        "--db",
        dest="graph_db",
        metavar="PATH",
        help="operate on this index DB (default: the standard index)",
    )
    p.set_defaults(fn=cmd_dump_compile_commands)

    p = sub.add_parser("search", help="fuzzy-search symbols by qualified name")
    p.add_argument(
        "pattern",
        help="'::'-separated substrings matched in order, "
        "e.g. 'conf::set' hits RdKafka::Conf::set",
    )
    p.add_argument(
        "--kind", choices=sorted(SYMBOL_KINDS), help="restrict to one symbol kind"
    )
    p.add_argument(
        "--limit",
        type=int,
        default=25,
        metavar="N",
        help="show at most N matches (0 = all; default 25)",
    )
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("show", help="show full details of one symbol or file")
    ssub = p.add_subparsers(dest="what", required=True)

    q = ssub.add_parser("symbol", help="one symbol, by id or USR")
    q.add_argument(
        "symbol",
        help="numeric id (first column of 'search') or a clang USR; "
        "USRs contain $ and * so single-quote them in the shell",
    )
    q.set_defaults(fn=cmd_show_symbol)

    q = ssub.add_parser("file", help="one file, by id or path")
    q.add_argument(
        "file",
        help="numeric id (first column of 'list files') or a path; "
        "relative paths resolve against the --component root "
        "(else the current directory)",
    )
    q.add_argument(
        "--component",
        "-c",
        metavar="NAME",
        help="component root for resolving a relative path",
    )
    q.set_defaults(fn=cmd_show_file)

    p = sub.add_parser(
        "list",
        aliases=["ls"],
        help="browse the index: components, dirs, files, symbols",
    )
    lsub = p.add_subparsers(dest="what", required=True)
    fuzzy = (
        "optional free-text fuzzy filter: characters must appear "
        "in order, e.g. 'shp' matches shapes.c"
    )

    q = lsub.add_parser("components", help="list registered components")
    q.add_argument("pattern", nargs="?", help=fuzzy)
    q.add_argument(
        "--kind", choices=("repo", "external"), help="restrict to one component kind"
    )
    q.set_defaults(fn=cmd_list_components)

    q = lsub.add_parser("dirs", help="list directories (all, or one component's)")
    q.add_argument("pattern", nargs="?", help=fuzzy)
    q.add_argument(
        "--component", "-c", metavar="NAME", help="restrict to this component"
    )
    q.set_defaults(fn=cmd_list_dirs)

    q = lsub.add_parser("files", help="list files for a component or a directory in it")
    q.add_argument("pattern", nargs="?", help=fuzzy)
    q.add_argument(
        "--component", "-c", metavar="NAME", help="restrict to this component"
    )
    q.add_argument(
        "--dir",
        "-d",
        metavar="PATH",
        help="directory (relative to the component root) "
        "including its subtree; needs --component",
    )
    g = q.add_mutually_exclusive_group()
    g.add_argument("--indexed", action="store_true", help="only files already indexed")
    g.add_argument("--pending", action="store_true", help="only files not yet indexed")
    q.set_defaults(fn=cmd_list_files)

    q = lsub.add_parser(
        "symbols", help="list symbols for a component, directory, or file"
    )
    q.add_argument(
        "pattern", nargs="?", help=fuzzy + " (matched against the qualified name)"
    )
    q.add_argument(
        "--component", "-c", metavar="NAME", help="restrict to this component"
    )
    q.add_argument(
        "--dir",
        "-d",
        metavar="PATH",
        help="directory (relative to the component root) "
        "including its subtree; needs --component",
    )
    q.add_argument(
        "--file",
        "-f",
        metavar="FILE",
        help="one file; relative paths resolve against the "
        "--component root (else the current directory)",
    )
    q.add_argument(
        "--kind", choices=sorted(SYMBOL_KINDS), help="restrict to one symbol kind"
    )
    q.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="show at most N matches (0 = all; default 50)",
    )
    q.set_defaults(fn=cmd_list_symbols)

    p = sub.add_parser("delete", help="delete a component, directory, file, or symbol")
    dsub = p.add_subparsers(dest="what", required=True)

    def _dry_run(q):
        q.add_argument(
            "--dry-run",
            action="store_true",
            help="preview the matches without deleting anything",
        )

    q = dsub.add_parser(
        "component", help="delete a component and everything indexed from it"
    )
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, metavar="ID", help="component id")
    g.add_argument("--name", metavar="NAME", help="component name")
    g.add_argument("--path", metavar="PATH", help="component root path")
    _dry_run(q)
    q.set_defaults(fn=cmd_delete_component)

    q = dsub.add_parser("dir", help="delete a directory, its files, and their symbols")
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, metavar="ID", help="directory id")
    g.add_argument("--path", metavar="PATH", help="directory path")
    q.add_argument(
        "--component", "-c", metavar="NAME", help="restrict the match to this component"
    )
    _dry_run(q)
    q.set_defaults(fn=cmd_delete_dir)

    q = dsub.add_parser("file", help="delete a file and its symbols")
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, metavar="ID", help="file id")
    g.add_argument("--name", metavar="NAME", help="file basename")
    g.add_argument("--path", metavar="PATH", help="file path")
    q.add_argument(
        "--component", "-c", metavar="NAME", help="restrict the match to this component"
    )
    _dry_run(q)
    q.set_defaults(fn=cmd_delete_file)

    q = dsub.add_parser("symbol", help="delete a symbol")
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, metavar="ID", help="symbol id")
    g.add_argument("--name", metavar="NAME", help="symbol spelling")
    g.add_argument("--usr", metavar="USR", help="clang USR")
    q.add_argument(
        "--component", "-c", metavar="NAME", help="restrict the match to this component"
    )
    _dry_run(q)
    q.set_defaults(fn=cmd_delete_symbol)

    # -- graph: query the relationship graph ----------------------------------
    p = sub.add_parser(
        "graph",
        help="query the relationship graph (callers, callees, "
        "refs, neighbors, walk, path, hierarchy, dispatch)",
    )
    gsub = p.add_subparsers(dest="what", required=True)
    edge_help = "comma-separated edge kinds (" + ", ".join(sorted(EDGE_KINDS)) + ")"

    def _selector(q):
        """Shared subject selector + common output flags for a graph command."""
        sel = q.add_mutually_exclusive_group(required=True)
        sel.add_argument("--usr", metavar="USR", help="exact clang USR")
        sel.add_argument("--id", type=int, metavar="N", help="numeric symbol id")
        sel.add_argument(
            "--name", metavar="FUZZY", help="fuzzy qualified-name match ('conf::set')"
        )
        q.add_argument(
            "--kind",
            choices=sorted(SYMBOL_KINDS),
            help="restrict a --name match to one symbol kind",
        )
        q.add_argument(
            "--first",
            action="store_true",
            help="if --name is ambiguous, take the closest match",
        )
        q.add_argument(
            "--db",
            dest="graph_db",
            metavar="PATH",
            help="index database to query (default: the standard cache index)",
        )
        q.add_argument(
            "--json", action="store_true", help="emit stable machine-readable JSON"
        )
        q.add_argument(
            "--limit",
            type=int,
            default=50,
            metavar="N",
            help="cap the number of results (default 50)",
        )

    q = gsub.add_parser("callers", help="functions that call the symbol")
    _selector(q)
    q.add_argument(
        "--direct-only",
        action="store_true",
        help="only literal incoming calls (exclude virtual-dispatch callers "
        "reached via materialised dispatch_calls edges, which are on by default)",
    )
    q.set_defaults(fn=cmd_graph_callers)

    q = gsub.add_parser("callees", help="functions the symbol calls")
    _selector(q)
    q.add_argument(
        "--direct-only",
        action="store_true",
        help="only literal outgoing calls (exclude virtual-dispatch targets "
        "reached via materialised dispatch_calls edges, which are on by default)",
    )
    q.set_defaults(fn=cmd_graph_callees)

    q = gsub.add_parser("refs", help="incoming references (calls + uses) to the symbol")
    _selector(q)
    q.set_defaults(fn=cmd_graph_refs)

    q = gsub.add_parser("neighbors", help="one-hop typed neighbors")
    _selector(q)
    q.add_argument("--edge", metavar="KINDS", help=edge_help + " (default: all)")
    q.add_argument(
        "--direction",
        choices=("in", "out"),
        default="out",
        help="edge direction (default out)",
    )
    q.set_defaults(fn=cmd_graph_neighbors)

    q = gsub.add_parser("walk", help="bounded BFS over typed edges")
    _selector(q)
    q.add_argument("--edge", metavar="KINDS", help=edge_help + " (default: calls)")
    q.add_argument(
        "--direction",
        choices=("in", "out"),
        default="out",
        help="edge direction (default out)",
    )
    q.add_argument(
        "--depth", type=int, default=3, metavar="N", help="max BFS depth (default 3)"
    )
    q.set_defaults(fn=cmd_graph_walk)

    q = gsub.add_parser("path", help="shortest path between two symbols, or none")
    _selector(q)
    dst = q.add_mutually_exclusive_group(required=True)
    dst.add_argument("--to-usr", metavar="USR", help="destination by USR")
    dst.add_argument("--to-id", type=int, metavar="N", help="destination by id")
    dst.add_argument("--to-name", metavar="FUZZY", help="destination by name")
    q.add_argument(
        "--to-kind",
        choices=sorted(SYMBOL_KINDS),
        help="restrict a --to-name match to one symbol kind",
    )
    q.add_argument("--edge", metavar="KINDS", help=edge_help + " (default: calls)")
    q.add_argument(
        "--direction",
        choices=("in", "out"),
        default="out",
        help="edge direction (default out)",
    )
    q.add_argument(
        "--depth", type=int, default=8, metavar="N", help="max search depth (default 8)"
    )
    q.set_defaults(fn=cmd_graph_path)

    q = gsub.add_parser("hierarchy", help="class bases, subclasses, and members")
    _selector(q)
    q.add_argument(
        "--transitive",
        action="store_true",
        help="walk the whole inheritance tree, not just direct edges",
    )
    q.add_argument(
        "--access",
        choices=("public", "protected", "private", "all"),
        default="all",
        help="filter members by C++ access specifier (default all)",
    )
    q.set_defaults(fn=cmd_graph_hierarchy)

    q = gsub.add_parser("dispatch", help="run-time targets of a virtual-method call")
    _selector(q)
    q.set_defaults(fn=cmd_graph_dispatch)

    # v27: multi-definition (per-backend redefinitions).
    q = gsub.add_parser(
        "redefined", help="symbols defined in more than one backend (multi_def>1)"
    )
    q.add_argument(
        "--db",
        dest="graph_db",
        metavar="PATH",
        help="index database to query (default: the standard cache index)",
    )
    q.add_argument(
        "--json", action="store_true", help="emit stable machine-readable JSON"
    )
    q.add_argument(
        "--limit", type=int, default=200, metavar="N",
        help="cap the number of results (default 200)",
    )
    q.set_defaults(fn=cmd_graph_redefined)

    q = gsub.add_parser(
        "definitions", help="each backend body of a symbol + its possible-call fan-out"
    )
    _selector(q)
    q.add_argument(
        "--direct-only",
        action="store_true",
        help="list the backend bodies only (omit the possible-call fan-out, "
        "which is shown by default)",
    )
    q.set_defaults(fn=cmd_graph_definitions)

    # -- ast (on-demand AST analysis; reads only the symbol/file tables) --------
    p = sub.add_parser("ast", help="on-demand AST analysis (dump, locals, conditions)")
    asub = p.add_subparsers(dest="what", required=True)

    def _ast_common(q):
        """Shared target/selector flags. Put options BEFORE the target; ad-hoc
        compile flags go after '--' (like the `file` subcommand)."""
        q.add_argument("--usr", metavar="USR", help="exact clang USR")
        q.add_argument("--id", type=int, metavar="N", help="numeric symbol id")
        q.add_argument(
            "--name",
            metavar="FUZZY",
            help="fuzzy qualified-name match (indexed), or an exact "
            "spelling to find in an ad-hoc file",
        )
        q.add_argument(
            "--kind",
            choices=sorted(SYMBOL_KINDS),
            help="restrict a --name match to one symbol kind",
        )
        q.add_argument(
            "--first",
            action="store_true",
            help="if --name is ambiguous, take the closest match",
        )
        q.add_argument(
            "--db",
            dest="graph_db",
            metavar="PATH",
            help="index database to read (default: the standard index)",
        )
        q.add_argument("--json", action="store_true", help="emit machine-readable JSON")
        q.add_argument(
            "target",
            nargs="?",
            metavar="FILE|COMPONENT://PATH",
            help="a source file, an indexed COMPONENT://PATH, or "
            "(with '-- <flags>') an ad-hoc file",
        )
        q.add_argument(
            "rest",
            nargs=argparse.REMAINDER,
            metavar="-- FLAGS",
            help="ad-hoc compile flags after '--' for un-imported files",
        )

    def _cache_toggle(q):
        """Add mutually-exclusive --cache / --no-cache (default: cache ON).

        Added to dump/locals/conditions only -- NOT to ``cache build|status|clear``
        (those operate on the cache itself and do not carry the toggle).
        """
        g = q.add_mutually_exclusive_group()
        g.add_argument(
            "--cache",
            dest="cache",
            action="store_true",
            default=True,
            help="use the on-disk AST cache (default)",
        )
        g.add_argument(
            "--no-cache",
            dest="cache",
            action="store_false",
            help="ignore the cache: always reparse (no cache read or write)",
        )

    q = asub.add_parser("dump", help="dump the AST subtree of a symbol or file")
    q.add_argument(
        "--depth",
        type=int,
        default=0,
        metavar="N",
        help="limit the dump to N levels (0 = unlimited)",
    )
    q.add_argument("--tokens", action="store_true", help="show each node's tokens")
    q.add_argument("--types", action="store_true", help="annotate cursor types")
    _ast_common(q)
    _cache_toggle(q)
    q.set_defaults(fn=astcmd.cmd_dump)

    q = asub.add_parser("locals", help="list a function's local variables")
    q.add_argument(
        "--params", action="store_true", help="include parameters, not just body locals"
    )
    _ast_common(q)
    _cache_toggle(q)
    q.set_defaults(fn=astcmd.cmd_locals)

    q = asub.add_parser(
        "conditions", help="conditionals guarding a call, with their condition"
    )
    q.add_argument(
        "--ast", action="store_true", help="also emit the condition's AST subtree"
    )
    _ast_common(q)
    _cache_toggle(q)
    q.set_defaults(fn=astcmd.cmd_conditions)

    # -- ast cache subcommands -------------------------------------------------
    qc = asub.add_parser("cache", help="manage the on-disk AST cache")
    csub = qc.add_subparsers(dest="cache_action", required=True)

    cb = csub.add_parser("build", help="parse + cache the target's AST (force-reparse)")
    _ast_common(cb)
    cb.set_defaults(fn=astcmd.cmd_cache)

    cstat = csub.add_parser("status", help="list cache entries, sizes, validity")
    _ast_common(cstat)
    cstat.set_defaults(fn=astcmd.cmd_cache)

    cclr = csub.add_parser("clear", help="remove cached AST(s) for a target, or all")
    _ast_common(cclr)
    cclr.set_defaults(fn=astcmd.cmd_cache)

    args = ap.parse_args(argv)
    # The standard index path, unless a graph command overrides it with --db.
    args.index = index_path()
    graph_db = getattr(args, "graph_db", None)
    if graph_db:
        args.index = os.path.abspath(os.path.expanduser(graph_db))
    _setup_logging()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

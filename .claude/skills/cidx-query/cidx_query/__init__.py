"""cidx_query — thin bootstrap so a skill snippet can use the repo's own
indexer query APIs without hardcoding paths.

This module does NOT reimplement anything. It locates the repo's `project/`
directory (where the `indexer` package lives), puts it on sys.path, and
re-exports the three query entry points the indexer already ships:

    open_query        -> indexer.query.GraphQuery     (low-level symbol graph)
    open_codebase     -> indexer.model.CodeBase       (high-level OO entities)
    open_entity_graph -> indexer.entity_graph.EntityGraph  (design-entity graph)

Usage from a snippet:

    import sys
    sys.path.insert(0, "/path/to/repo/.claude/skills/cidx-query")
    from cidx_query import open_query, open_codebase, open_entity_graph
    g = open_query()                # reads $INDEXER_CACHE/index.db or ~/.cache/cidx/index.db
    print(g.stats())

Override discovery with the CIDX_PROJECT_DIR env var if the repo is elsewhere.
"""
import os
import sys
from pathlib import Path


def _find_project_dir():
    """Return the dir containing the `indexer` package (i.e. <repo>/project)."""
    env = os.environ.get("CIDX_PROJECT_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve()
    # this file is <repo>/.claude/skills/cidx-query/cidx_query/__init__.py
    for parent in here.parents:
        candidates.append(parent / "project")
    candidates.append(Path.cwd() / "project")
    for c in candidates:
        if (c / "indexer" / "__init__.py").exists():
            return c
    raise RuntimeError(
        "could not locate the cidx `indexer` package. Set CIDX_PROJECT_DIR to "
        "the directory that contains `indexer/` (usually <repo>/project)."
    )


PROJECT_DIR = _find_project_dir()
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# Re-export the indexer's real query APIs. We import the package, not copies.
from indexer import (  # noqa: E402
    open_query,
    open_codebase,
    open_entity_graph,
    default_db_path,
    GraphQuery,
    CodeBase,
    EntityGraph,
    Sym,
    Edge,
    Site,
    Traversal,
    NoIndexError,
    NoEdgesError,
)

__all__ = [
    "PROJECT_DIR",
    "open_query",
    "open_codebase",
    "open_entity_graph",
    "default_db_path",
    "GraphQuery",
    "CodeBase",
    "EntityGraph",
    "Sym",
    "Edge",
    "Site",
    "Traversal",
    "NoIndexError",
    "NoEdgesError",
]

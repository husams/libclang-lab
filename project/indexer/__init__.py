"""cidx indexer package.

Modules:
    storage -- SQLite-backed symbol index (components / directories / files / symbols).
    query   -- read-only graph-query API over the index (GraphQuery).
"""

from .storage import Storage, Component, Directory, File, Symbol, SYMBOL_KINDS
from .query import (
    GraphQuery, Sym, Edge, Site, Traversal,
    NoIndexError, NoEdgesError, open_query, default_db_path,
)

__all__ = [
    "Storage", "Component", "Directory", "File", "Symbol", "SYMBOL_KINDS",
    "GraphQuery", "Sym", "Edge", "Site", "Traversal",
    "NoIndexError", "NoEdgesError", "open_query", "default_db_path",
]

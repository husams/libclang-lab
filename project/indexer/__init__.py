"""cidx indexer package.

Modules:
    storage -- SQLite-backed symbol index (components / directories / files / symbols).
    query   -- low-level read-only graph-query API over the index (GraphQuery).
    model   -- high-level OO view over the graph (CodeBase + typed entities).
"""

from .storage import Storage, Component, Directory, File, Symbol, SYMBOL_KINDS
from .query import (
    CallerWithContext,
    GraphQuery,
    Sym,
    Edge,
    Site,
    Traversal,
    NoIndexError,
    NoEdgesError,
    open_query,
    default_db_path,
)
from .model import (
    CallerWithContextModel,
    CodeBase,
    open_codebase,
    Location,
    Type,
    Reference,
    Entity,
    Callable,
    Function,
    Method,
    Constructor,
    Destructor,
    Record,
    Class,
    Field,
    Enum,
    EnumConstant,
    Typedef,
    Namespace,
    Variable,
    Macro,
    FunctionTemplate,
    ClassTemplate,
)

__all__ = [
    "Storage",
    "Component",
    "Directory",
    "File",
    "Symbol",
    "SYMBOL_KINDS",
    "CallerWithContext",
    "CallerWithContextModel",
    "GraphQuery",
    "Sym",
    "Edge",
    "Site",
    "Traversal",
    "NoIndexError",
    "NoEdgesError",
    "open_query",
    "default_db_path",
    # high-level model layer
    "CodeBase",
    "open_codebase",
    "Location",
    "Type",
    "Reference",
    "Entity",
    "Callable",
    "Function",
    "Method",
    "Constructor",
    "Destructor",
    "Record",
    "Class",
    "Field",
    "Enum",
    "EnumConstant",
    "Typedef",
    "Namespace",
    "Variable",
    "Macro",
    "FunctionTemplate",
    "ClassTemplate",
]

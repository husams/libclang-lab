"""indexer.clang -- libclang parsing layer for the cidx indexer."""

from .ast import index_headers, index_source, index_symbols
from .util import (
    ClangParseError,
    driver_flags,
    fatal_diagnostics,
    is_cpp,
    parse,
    toolchain_flags,
)

__all__ = [
    "ClangParseError",
    "driver_flags",
    "fatal_diagnostics",
    "index_headers",
    "index_source",
    "index_symbols",
    "is_cpp",
    "parse",
    "toolchain_flags",
]

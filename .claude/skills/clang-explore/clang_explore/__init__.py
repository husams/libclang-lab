"""clang_explore — live libclang (clang.cindex) exploration of C/C++ codebases.

Parse source on demand and answer structural questions (symbols, types, calls,
references, diagnostics) by querying the AST, instead of reading or grepping.
"""
from .core import (
    Repo,
    open_repo,
    clang_args,
    parse,
    walk,
    loc,
    in_main_file,
    top_level,
    fatal_diagnostics,
    diagnostics,
    dump_ast,
    find_symbols,
    callees_of,
    callers_of,
    references_to,
    SOURCE_EXTS,
    HEADER_EXTS,
)

__all__ = [
    "Repo",
    "open_repo",
    "clang_args",
    "parse",
    "walk",
    "loc",
    "in_main_file",
    "top_level",
    "fatal_diagnostics",
    "diagnostics",
    "dump_ast",
    "find_symbols",
    "callees_of",
    "callers_of",
    "references_to",
    "SOURCE_EXTS",
    "HEADER_EXTS",
]

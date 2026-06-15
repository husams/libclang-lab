"""cidx_graph -- ground an LLM in a large code graph without reading source.

Open the prebuilt cidx index and reason over symbols/edges with compact queries:

    from cidx_graph import open_graph
    g = open_graph()
    fn = g.find("rd_kafka_new")[0]
    print(g.callers(fn))
    print(g.dispatch_targets(some_virtual_method))

See SKILL.md for the rules and references/ for the full API, recipes, and schema.
The `live` submodule is the libclang escape hatch (last resort only).
"""

from .graph import (
    Graph, Sym, Edge, Site, Traversal,
    open_graph, default_db_path, EDGE_KINDS, EDGE_NAMES,
)

__all__ = [
    "Graph", "Sym", "Edge", "Site", "Traversal",
    "open_graph", "default_db_path", "EDGE_KINDS", "EDGE_NAMES",
]

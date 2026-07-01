"""cxq — CXQ V2 query language for the cidx code graph.

V2 grammar (declarative, no loops):

  # V1 core: match / where / select
  match function f where f.name ~ "parse" select f
  match class c where c inherits+ "geo::Shape" select c
  match function f where "main" calls+ f select f

  # V2 path: show path from A to B via edge
  show path from "main" to "org::project::net::connect" via calls

  # V2 rank: rank VAR in MATCH by METRIC [desc] [limit N]
  rank f in (match function f where "main" calls+ f select f)
        by count(callers+ f) desc limit 10
"""

from .parser import parse, ParseError
from .executor import execute, ExecutorError

__all__ = ["parse", "execute", "ParseError", "ExecutorError"]

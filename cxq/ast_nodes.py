"""cxq.ast_nodes — Immutable AST nodes for the V2 query language.

V2 adds PathQuery and RankQuery on top of the V1 core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MatchVar:
    """A single (kind, variable-name) binding in a match clause."""

    kind: str  # function | method | class | struct | interface | record | ...
    var: str  # the bound variable name


@dataclass(frozen=True)
class AttrPred:
    """Attribute predicate: var.attr op value.

    op: '=' | '!=' | '~' (regex) | 'in'
    value: str for '='/'!='/'~'; list[str] for 'in'
    """

    var: str
    attr: str
    op: str
    value: str | list[str]


@dataclass(frozen=True)
class ClosurePred:
    """Transitive-closure predicate.

    Forms:
      STRING calls+ var      -> all functions reachable from STRING via calls
      var    calls+ STRING   -> all functions that can call STRING (inverse)
      var    inherits+ STRING -> all subclasses of STRING
      STRING inherits+ var   -> all ancestors of STRING
      var    implements+ STRING -> var implements the named interface/base
    """

    lhs: str  # variable name or quoted string (literal entity name)
    op: str  # 'calls+' | 'calls*' | 'inherits+' | 'inherits*' | 'implements+'
    rhs: str  # variable name or quoted string
    lhs_is_var: bool  # True if lhs is a variable reference
    rhs_is_var: bool  # True if rhs is a variable reference


@dataclass(frozen=True)
class NotPred:
    """Logical negation of a predicate."""

    pred: "Pred"


@dataclass(frozen=True)
class AndPred:
    """Conjunction of two predicates."""

    left: "Pred"
    right: "Pred"


@dataclass(frozen=True)
class OrPred:
    """Disjunction of two predicates."""

    left: "Pred"
    right: "Pred"


Pred = AttrPred | ClosurePred | NotPred | AndPred | OrPred


@dataclass(frozen=True)
class VarProj:
    """Project a bound variable (returns the entity object)."""

    var: str


@dataclass(frozen=True)
class AttrProj:
    """Project an attribute of a bound variable."""

    var: str
    attr: str


@dataclass(frozen=True)
class CountProj:
    """Project count(var) — number of matching entities."""

    var: str


Projection = VarProj | AttrProj | CountProj


@dataclass
class Query:
    """The full parsed V1/V2 match query."""

    bindings: list[MatchVar]  # all (kind, var) pairs from match clause(s)
    conditions: Optional[Pred]  # combined condition tree, or None
    projections: list[Projection]  # select list


# ---- V2 extensions -------------------------------------------------------


@dataclass(frozen=True)
class RankMetric:
    """A metric used in a rank clause.

    Supported forms:
      count(callers+ VAR)   -- transitive caller count (blast radius)
      count(callees+ VAR)   -- transitive callee count (fan-out)
      count(callers VAR)    -- direct caller count (fan-in)
      count(callees VAR)    -- direct callee count
    """

    func: str  # 'count'
    direction: str  # 'callers+' | 'callers' | 'callees+' | 'callees'
    var: str  # variable being measured


@dataclass
class RankQuery:
    """rank VAR in MATCH_QUERY by METRIC [desc] [limit N]

    Wraps an inner match query, adds ordering + top-N.
    """

    inner: Query  # the underlying match/where query
    rank_var: str  # which bound variable to rank
    metric: RankMetric  # how to score each element
    desc: bool  # True = descending (default for most useful metrics)
    limit: Optional[int]  # top-N cap, or None for all


@dataclass(frozen=True)
class PathQuery:
    """show path from FROM to TO via EDGE

    Finds the shortest witness route between two named entities.
    """

    from_name: str  # source entity name (string literal)
    to_name: str  # destination entity name (string literal)
    via: str  # 'calls' | 'inherits'

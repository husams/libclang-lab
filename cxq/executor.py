"""cxq.executor — Executes a parsed V2 CXQ query against a CodeBase.

Entry point: execute(query, cb) -> list[dict]

Handles V1 Query (match/where/select), V2 PathQuery (show path), and
V2 RankQuery (rank by metric).  Each result row is a dict mapping
projection key -> value; path queries return a single row with a
'path' key.
"""

from __future__ import annotations

import re
from typing import Any

from .ast_nodes import (
    AndPred,
    AttrPred,
    AttrProj,
    ClosurePred,
    CountProj,
    NotPred,
    OrPred,
    PathQuery,
    Pred,
    Projection,
    Query,
    RankMetric,
    RankQuery,
    VarProj,
)


class ExecutorError(RuntimeError):
    """Raised when a query cannot be executed against the given codebase."""


# ------------------------------------------------------------------ #
# Attribute resolution (entity -> value)
# ------------------------------------------------------------------ #


def _get_attr(entity: Any, attr: str) -> Any:
    """Resolve a named attribute from an entity.

    Supports the common model attributes + synthetic conveniences.
    """
    if attr == "name":
        return getattr(entity, "name", None)
    if attr == "fqn":
        # fqn = fully-qualified name; model uses .name for that already
        return getattr(entity, "name", None)
    if attr == "spelling":
        return getattr(entity, "spelling", None)
    if attr == "kind":
        return getattr(entity, "kind", None)
    if attr == "signature":
        return getattr(entity, "signature", None)
    if attr == "return_type":
        rt = getattr(entity, "return_type", None)
        return rt.spelling if rt is not None else None
    if attr == "is_virtual":
        return getattr(entity, "is_virtual", False)
    if attr == "is_pure":
        return getattr(entity, "is_pure", False)
    if attr == "is_static":
        return getattr(entity, "is_static", False)
    if attr == "is_abstract":
        return getattr(entity, "is_abstract", False)
    if attr == "is_interface":
        return getattr(entity, "is_interface", False)
    if attr == "access":
        return getattr(entity, "access", None)
    if attr == "namespace":
        # Extract namespace from fully-qualified name: "a::b::c" -> "a::b"
        fqn = getattr(entity, "name", None) or ""
        parts = fqn.rsplit("::", 1)
        return parts[0] if len(parts) > 1 else ""
    if attr == "location":
        loc = getattr(entity, "location", None)
        return loc.loc if loc else None
    if attr == "file":
        f = getattr(entity, "file", None)
        return f.path if f else None
    # Fall back to direct attribute access
    val = getattr(entity, attr, _MISSING)
    if val is _MISSING:
        raise ExecutorError(f"Unknown attribute {attr!r} on {entity!r}")
    return val


_MISSING = object()


def _attr_matches(entity: Any, pred: AttrPred) -> bool:
    """Evaluate a single attribute predicate against an entity."""
    val = _get_attr(entity, pred.attr)
    if val is None:
        return pred.op == "!="

    val_str = str(val)

    if pred.op == "=":
        return val_str == str(pred.value)
    if pred.op == "!=":
        return val_str != str(pred.value)
    if pred.op == "~":
        return bool(re.search(str(pred.value), val_str))
    if pred.op == "in":
        assert isinstance(pred.value, list)
        return val_str in pred.value
    raise ExecutorError(f"Unknown operator {pred.op!r}")


# ------------------------------------------------------------------ #
# Closure expansion
# ------------------------------------------------------------------ #


def _calls_closure(entity: Any, reflexive: bool) -> set[Any]:
    """All entities reachable from entity via calls edges (transitive)."""
    from indexer.model import Callable  # local import to avoid circular dep

    visited: set[int] = set()
    result: set[Any] = set()
    if reflexive:
        result.add(entity)
        visited.add(entity.id)

    def _walk(e: Any) -> None:
        if not isinstance(e, Callable):
            return
        for callee, _depth in e.callgraph():
            if callee.id not in visited:
                visited.add(callee.id)
                result.add(callee)

    _walk(entity)
    return result


def _callers_closure(entity: Any, reflexive: bool) -> set[Any]:
    """All entities that can transitively call entity (reverse closure)."""
    visited: set[int] = set()
    result: set[Any] = set()
    if reflexive:
        result.add(entity)
        visited.add(entity.id)

    frontier = [entity]
    while frontier:
        cur = frontier.pop()
        callers = getattr(cur, "callers", lambda: [])()
        for caller in callers:
            if caller.id not in visited:
                visited.add(caller.id)
                result.add(caller)
                frontier.append(caller)

    return result


def _inherits_closure(entity: Any, reflexive: bool) -> set[Any]:
    """All subclasses (transitively) of entity."""
    result: set[Any] = set()
    if reflexive:
        result.add(entity)
    children = getattr(entity, "children", [])
    if callable(children):
        children = children()
    for child in children:
        result.add(child)
        result.update(_inherits_closure(child, reflexive=False))
    return result


def _ancestors_closure(entity: Any, reflexive: bool) -> set[Any]:
    """All ancestors (transitively) of entity."""
    result: set[Any] = set()
    if reflexive:
        result.add(entity)
    parents = getattr(entity, "parents", [])
    if callable(parents):
        parents = parents()
    for parent in parents:
        result.add(parent)
        result.update(_ancestors_closure(parent, reflexive=False))
    return result


# ------------------------------------------------------------------ #
# Predicate evaluator
# ------------------------------------------------------------------ #


def _eval_pred(pred: Pred, bindings: dict[str, Any], cb: Any) -> bool:
    """Evaluate a predicate in the context of current variable bindings."""
    if isinstance(pred, AttrPred):
        entity = bindings.get(pred.var)
        if entity is None:
            raise ExecutorError(f"Variable {pred.var!r} not bound")
        return _attr_matches(entity, pred)

    if isinstance(pred, ClosurePred):
        return _eval_closure_pred(pred, bindings, cb)

    if isinstance(pred, NotPred):
        return not _eval_pred(pred.pred, bindings, cb)

    if isinstance(pred, AndPred):
        return _eval_pred(pred.left, bindings, cb) and _eval_pred(
            pred.right, bindings, cb
        )

    if isinstance(pred, OrPred):
        return _eval_pred(pred.left, bindings, cb) or _eval_pred(
            pred.right, bindings, cb
        )

    raise ExecutorError(f"Unknown predicate type: {type(pred).__name__}")


def _eval_closure_pred(pred: ClosurePred, bindings: dict[str, Any], cb: Any) -> bool:
    """Evaluate a transitive-closure predicate."""
    op = pred.op
    reflexive = op.endswith("*")

    # Resolve lhs
    if pred.lhs_is_var:
        lhs_entity = bindings.get(pred.lhs)
        if lhs_entity is None:
            raise ExecutorError(f"Variable {pred.lhs!r} not bound in closure pred")
        lhs_literal = None
    else:
        lhs_entity = None
        lhs_literal = pred.lhs

    # Resolve rhs
    if pred.rhs_is_var:
        rhs_entity = bindings.get(pred.rhs)
        if rhs_entity is None:
            raise ExecutorError(f"Variable {pred.rhs!r} not bound in closure pred")
        rhs_literal = None
    else:
        rhs_entity = None
        rhs_literal = pred.rhs

    if op in ("calls+", "calls*"):
        if lhs_entity is not None and rhs_entity is not None:
            reachable = _calls_closure(lhs_entity, reflexive)
            return rhs_entity in reachable
        if lhs_entity is not None and rhs_literal is not None:
            reachable = _calls_closure(lhs_entity, reflexive)
            return any(
                e.spelling == rhs_literal or e.name == rhs_literal for e in reachable
            )
        if lhs_literal is not None and rhs_entity is not None:
            roots = cb.find(lhs_literal, limit=20)
            for root in roots:
                reachable = _calls_closure(root, reflexive)
                if rhs_entity in reachable:
                    return True
            return False
        raise ExecutorError("Cannot evaluate calls+/calls*: both sides are literals")

    if op in ("inherits+", "inherits*"):
        if lhs_entity is not None and rhs_literal is not None:
            bases = cb.find(rhs_literal, limit=20)
            for base in bases:
                descendants = _inherits_closure(base, reflexive)
                if lhs_entity in descendants:
                    return True
            return False
        if lhs_literal is not None and rhs_entity is not None:
            children_entities = cb.find(lhs_literal, limit=20)
            for child in children_entities:
                ancestors = _ancestors_closure(child, reflexive)
                if rhs_entity in ancestors:
                    return True
            return False
        if lhs_entity is not None and rhs_entity is not None:
            descendants = _inherits_closure(rhs_entity, reflexive)
            return lhs_entity in descendants
        raise ExecutorError(
            "Cannot evaluate inherits+/inherits*: both sides are literals"
        )

    if op == "implements+":
        if lhs_entity is not None and rhs_literal is not None:
            bases = cb.find(rhs_literal, limit=20)
            for base in bases:
                descendants = _inherits_closure(base, reflexive=True)
                if lhs_entity in descendants:
                    return True
            return False
        if lhs_entity is not None and rhs_entity is not None:
            descendants = _inherits_closure(rhs_entity, reflexive=True)
            return lhs_entity in descendants
        raise ExecutorError("Cannot evaluate implements+: unsupported form")

    raise ExecutorError(f"Unknown closure op: {op!r}")


# ------------------------------------------------------------------ #
# Entity enumerator by kind
# ------------------------------------------------------------------ #


def _entities_for_kind(kind: str, cb: Any, limit: int = 500) -> list[Any]:
    """Enumerate all indexed entities of the given kind."""
    if kind == "interface":
        raw = cb.graph.find("", kind="class", limit=limit)
        wrapped = cb._wrap_all(raw)
        return [e for e in wrapped if getattr(e, "is_interface", False)]

    if kind == "record":
        result: list[Any] = []
        seen: set[int] = set()
        for db_kind in ("class", "struct", "union"):
            for e in cb._wrap_all(cb.graph.find("", kind=db_kind, limit=limit)):
                if e.id not in seen:
                    seen.add(e.id)
                    result.append(e)
        return result

    if kind in ("callable", "any"):
        result = []
        seen = set()
        kinds = (
            ("function", "method", "constructor", "destructor", "function-template")
            if kind == "callable"
            else ("function", "method", "class", "struct", "enum", "namespace")
        )
        for db_kind in kinds:
            for e in cb._wrap_all(cb.graph.find("", kind=db_kind, limit=limit)):
                if e.id not in seen:
                    seen.add(e.id)
                    result.append(e)
        return result

    db_kind = {"field": "member"}.get(kind, kind)
    raw = cb.graph.find("", kind=db_kind, limit=limit)
    return cb._wrap_all(raw)


# ------------------------------------------------------------------ #
# V2: path executor
# ------------------------------------------------------------------ #


def execute_path(query: PathQuery, cb: Any) -> list[dict]:
    """Execute a 'show path from A to B via edge' query.

    Returns a list with one row: {'path': 'A -> B -> C', 'chain': [...]}.
    Returns empty list when no path exists.
    """
    src_entities = cb.find(query.from_name, limit=10)
    dst_entities = cb.find(query.to_name, limit=10)

    if not src_entities:
        raise ExecutorError(f"No entity found for 'from' name {query.from_name!r}")
    if not dst_entities:
        raise ExecutorError(f"No entity found for 'to' name {query.to_name!r}")

    # Try each (src, dst) pair; return the first successful path.
    for src in src_entities:
        for dst in dst_entities:
            path = _bfs_path(src, dst, query.via, cb)
            if path:
                chain = [_entity_to_dict(e) for e in path]
                route = " -> ".join(e["name"] for e in chain)
                return [{"path": route, "chain": chain, "length": len(path) - 1}]

    return []


def _bfs_path(src: Any, dst: Any, via: str, cb: Any) -> list[Any] | None:
    """BFS shortest path from src to dst via the given edge type.

    via='calls'    -> follow callees() outbound
    via='inherits' -> follow children outbound (subclass direction)
    """
    if src.id == dst.id:
        return [src]

    # BFS: frontier holds (entity, path_so_far)
    visited: set[int] = {src.id}
    queue: list[tuple[Any, list[Any]]] = [(src, [src])]

    while queue:
        cur, path = queue.pop(0)
        neighbors = _edge_neighbors(cur, via)
        for nxt in neighbors:
            if nxt.id in visited:
                continue
            visited.add(nxt.id)
            new_path = path + [nxt]
            if nxt.id == dst.id:
                return new_path
            queue.append((nxt, new_path))

    return None


def _edge_neighbors(entity: Any, via: str) -> list[Any]:
    """One-hop neighbors over the given edge type."""
    if via == "calls":
        callees_fn = getattr(entity, "callees", None)
        if callees_fn is None:
            return []
        return callees_fn(limit=200)
    if via == "inherits":
        children = getattr(entity, "children", [])
        if callable(children):
            children = children()
        return list(children)
    return []


# ------------------------------------------------------------------ #
# V2: rank executor
# ------------------------------------------------------------------ #


def execute_rank(query: RankQuery, cb: Any, limit: int = 500) -> list[dict]:
    """Execute a rank query: evaluate inner match, score each element, sort."""
    # Re-run the inner query to get actual entity objects (not serialized dicts).
    entities = _execute_match_entities(query.inner, query.rank_var, cb, limit=limit)

    if not entities:
        return []

    # Score each entity by the metric.
    scored: list[tuple[Any, int]] = []
    for entity in entities:
        score = _compute_metric(entity, query.metric, cb)
        scored.append((entity, score))

    # Sort by score.
    scored.sort(key=lambda x: x[1], reverse=query.desc)

    # Apply limit.
    if query.limit is not None:
        scored = scored[: query.limit]

    # Build result dicts, including the metric score.
    result = []
    for entity, score in scored:
        row = _entity_to_dict(entity)
        row["score"] = score
        row["metric"] = f"{query.metric.direction}({entity.name})"
        result.append(row)

    return result


def _compute_metric(entity: Any, metric: RankMetric, cb: Any) -> int:
    """Compute a numeric score for an entity based on a RankMetric."""
    direction = metric.direction

    if direction == "callers+":
        # Transitive caller count (blast radius)
        return len(_callers_closure(entity, reflexive=False))
    if direction == "callers":
        # Direct caller count (fan-in)
        callers_fn = getattr(entity, "callers", None)
        if callers_fn is None:
            return 0
        return len(callers_fn(limit=500))
    if direction == "callees+":
        # Transitive callee count (fan-out)
        return len(_calls_closure(entity, reflexive=False))
    if direction == "callees":
        # Direct callee count
        callees_fn = getattr(entity, "callees", None)
        if callees_fn is None:
            return 0
        return len(callees_fn(limit=500))

    return 0


def _execute_match_entities(
    query: Query, rank_var: str, cb: Any, limit: int = 500
) -> list[Any]:
    """Like _execute_match but return the entity objects for rank_var."""
    candidates: dict[str, list[Any]] = {}
    for binding in query.bindings:
        candidates[binding.var] = _entities_for_kind(binding.kind, cb, limit=limit)

    var_names = [b.var for b in query.bindings]
    var_lists = [candidates[v] for v in var_names]

    results: list[Any] = []
    seen_ids: set[int] = set()

    def _cartesian(i: int, row: dict[str, Any]) -> None:
        if i == len(var_names):
            if query.conditions is None or _eval_pred(query.conditions, row, cb):
                entity = row.get(rank_var)
                if entity is not None and entity.id not in seen_ids:
                    seen_ids.add(entity.id)
                    results.append(entity)
            return
        var = var_names[i]
        for entity in var_lists[i]:
            row[var] = entity
            _cartesian(i + 1, row)

    _cartesian(0, {})
    return results


# ------------------------------------------------------------------ #
# V1: match executor (internal helper)
# ------------------------------------------------------------------ #


def _execute_match(query: Query, cb: Any, limit: int = 500) -> list[dict]:
    """Execute a match/where/select query; returns serialized result dicts."""
    candidates: dict[str, list[Any]] = {}
    for binding in query.bindings:
        candidates[binding.var] = _entities_for_kind(binding.kind, cb, limit=limit)

    var_names = [b.var for b in query.bindings]
    var_lists = [candidates[v] for v in var_names]

    results: list[dict] = []

    def _cartesian(i: int, row: dict[str, Any]) -> None:
        if i == len(var_names):
            if query.conditions is None or _eval_pred(query.conditions, row, cb):
                results.append(_project(row, query.projections))
            return
        var = var_names[i]
        for entity in var_lists[i]:
            row[var] = entity
            _cartesian(i + 1, row)

    _cartesian(0, {})
    return results


# ------------------------------------------------------------------ #
# Main executor (public dispatch)
# ------------------------------------------------------------------ #


def execute(
    query: "Query | PathQuery | RankQuery",
    cb: Any,
    limit: int = 500,
) -> list[dict]:
    """Execute any CXQ V2 query against a CodeBase.

    Returns a list of result dicts.
    """
    if isinstance(query, PathQuery):
        return execute_path(query, cb)
    if isinstance(query, RankQuery):
        return execute_rank(query, cb, limit=limit)
    # V1 match query
    return _execute_match(query, cb, limit=limit)


def _project(row: dict[str, Any], projections: list[Projection]) -> dict:
    """Build a result dict from a matching row."""
    out: dict = {}
    for proj in projections:
        if isinstance(proj, VarProj):
            entity = row[proj.var]
            out[proj.var] = _entity_to_dict(entity)
        elif isinstance(proj, AttrProj):
            entity = row[proj.var]
            key = f"{proj.var}.{proj.attr}"
            out[key] = _get_attr(entity, proj.attr)
        elif isinstance(proj, CountProj):
            out[f"count({proj.var})"] = 1  # placeholder
        else:
            raise ExecutorError(f"Unknown projection type: {type(proj).__name__}")
    return out


def _entity_to_dict(entity: Any) -> dict:
    """Serialize an entity to a JSON-serializable dict."""
    return {
        "name": getattr(entity, "name", None),
        "kind": getattr(entity, "kind", None),
        "location": getattr(getattr(entity, "location", None), "loc", None),
    }

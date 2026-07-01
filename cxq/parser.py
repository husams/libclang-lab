"""cxq.parser — Hand-rolled recursive-descent parser for the V2 grammar.

V2 is a strict superset of V1.

Grammar (simplified):

    query        ::= path_query | rank_query | match_query
    match_query  ::= match_block ('where' conditions)? 'select' proj_list [rank_suffix]
    match_block  ::= 'match' binding (',' binding)*
    binding      ::= KIND VAR
    KIND         ::= 'function' | 'method' | 'class' | 'struct' | 'interface'
                   | 'record' | 'namespace' | 'enum' | 'field' | 'callable' | 'any'
    conditions   ::= or_expr
    or_expr      ::= and_expr ('or' and_expr)*
    and_expr     ::= not_expr ('and' not_expr)*
    not_expr     ::= 'not' not_expr | primary_pred
    primary_pred ::= closure_pred | attr_pred | '(' or_expr ')'
    closure_pred ::= lhs_token closure_op rhs_token
    closure_op   ::= 'calls+' | 'calls*' | 'inherits+' | 'inherits*' | 'implements+'
    attr_pred    ::= VAR '.' ATTR op value
    op           ::= '=' | '!=' | '~' | 'in'
    value        ::= STRING | '[' string_list ']'
    string_list  ::= STRING (',' STRING)*
    proj_list    ::= proj (',' proj)*
    proj         ::= 'count' '(' VAR ')' | VAR '.' ATTR | VAR

    # V2: path
    path_query   ::= 'show' 'path' 'from' STRING 'to' STRING 'via' via_edge
    via_edge     ::= 'calls' | 'inherits'

    # V2: rank (prefix or postfix)
    rank_query   ::= 'rank' VAR 'in' match_query 'by' metric ['desc'] ['limit' INT]
    rank_suffix  ::= 'rank' VAR 'by' metric ['desc'] ['limit' INT]
    metric       ::= 'count' '(' dir VAR ')'
    dir          ::= 'callers+' | 'callers' | 'callees+' | 'callees'
"""

from __future__ import annotations

from typing import Optional

from .ast_nodes import (
    AndPred,
    AttrPred,
    AttrProj,
    ClosurePred,
    CountProj,
    MatchVar,
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

# ------------------------------------------------------------------ #
# Token types
# ------------------------------------------------------------------ #

_KEYWORDS = frozenset(
    {
        "match",
        "where",
        "select",
        "and",
        "or",
        "not",
        "in",
        "calls+",
        "calls*",
        "inherits+",
        "inherits*",
        "implements+",
        "function",
        "method",
        "class",
        "struct",
        "interface",
        "record",
        "namespace",
        "enum",
        "field",
        "count",
        # V2 keywords
        "show",
        "path",
        "from",
        "to",
        "via",
        "rank",
        "by",
        "desc",
        "limit",
        "callers+",
        "callers",
        "callees+",
        "callees",
        "callable",
        "any",
    }
)

_KIND_KEYWORDS = frozenset(
    {
        "function",
        "method",
        "class",
        "struct",
        "interface",
        "record",
        "namespace",
        "enum",
        "field",
        "callable",
        "any",
        "constructor",
        "destructor",
        "function-template",
        "class-template",
    }
)

_CLOSURE_OPS = frozenset({"calls+", "calls*", "inherits+", "inherits*", "implements+"})


class ParseError(ValueError):
    """Raised when the input does not conform to the V1 grammar."""


# ------------------------------------------------------------------ #
# Tokeniser
# ------------------------------------------------------------------ #

_METRIC_DIRS = frozenset({"callers+", "callers", "callees+", "callees"})


def _tokenize(text: str) -> list[str]:
    """Split input into tokens, handling quoted strings, multi-char ops."""
    tokens: list[str] = []
    i = 0
    while i < len(text):
        # Skip whitespace
        if text[i].isspace():
            i += 1
            continue
        # Quoted string
        if text[i] in ('"', "'"):
            quote = text[i]
            j = i + 1
            while j < len(text) and text[j] != quote:
                j += 1
            if j >= len(text):
                raise ParseError(f"Unterminated string at position {i}")
            tokens.append(text[i : j + 1])
            i = j + 1
            continue
        # Two-char ops
        if text[i : i + 2] in ("!=",):
            tokens.append(text[i : i + 2])
            i += 2
            continue
        # Single-char punctuation
        if text[i] in "=~.,()[]":
            tokens.append(text[i])
            i += 1
            continue
        # Integer literals
        if text[i].isdigit():
            j = i
            while j < len(text) and text[j].isdigit():
                j += 1
            tokens.append(text[i:j])
            i = j
            continue
        # Word / keyword (may include +/* suffix for closure ops / metric dirs)
        if text[i].isalpha() or text[i] == "_":
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] in "_:"):
                j += 1
            word = text[i:j]
            # Check for closure-op or metric-dir suffix: calls+, inherits+, etc.
            if j < len(text) and text[j] in "+*":
                candidate = word + text[j]
                if candidate in _CLOSURE_OPS or candidate in _METRIC_DIRS:
                    tokens.append(candidate)
                    i = j + 1
                    continue
            tokens.append(word)
            i = j
            continue
        # Unknown char
        raise ParseError(f"Unexpected character {text[i]!r} at position {i}")
    return tokens


# ------------------------------------------------------------------ #
# Recursive-descent parser
# ------------------------------------------------------------------ #


class _Parser:
    def __init__(self, tokens: list[str]):
        self._tokens = tokens
        self._pos = 0

    # -- helpers --------------------------------------------------------- #

    def _peek(self) -> Optional[str]:
        if self._pos < len(self._tokens):
            return self._tokens[self._pos]
        return None

    def _peek2(self) -> Optional[str]:
        if self._pos + 1 < len(self._tokens):
            return self._tokens[self._pos + 1]
        return None

    def _advance(self) -> str:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, *expected: str) -> str:
        tok = self._peek()
        if tok not in expected:
            raise ParseError(
                f"Expected {expected!r} but got {tok!r} at position {self._pos}"
            )
        return self._advance()

    def _at_end(self) -> bool:
        return self._pos >= len(self._tokens)

    def _is_string_tok(self, tok: Optional[str]) -> bool:
        return tok is not None and (tok.startswith('"') or tok.startswith("'"))

    # -- top-level (V2 dispatch) ----------------------------------------- #

    def parse_query(self) -> "Query | PathQuery | RankQuery":
        tok = self._peek()
        if tok == "show":
            result = self._parse_path_query()
        elif tok == "rank":
            result = self._parse_rank_prefix()
        else:
            result = self._parse_match_query()
            # postfix rank: '... select f rank f by ...'
            if self._peek() == "rank":
                result = self._parse_rank_postfix(result)

        if not self._at_end():
            raise ParseError(
                f"Unexpected trailing tokens starting at {self._tokens[self._pos]!r}"
            )
        return result

    # -- V2: path query -------------------------------------------------- #

    def _parse_path_query(self) -> "PathQuery":
        self._expect("show")
        self._expect("path")
        self._expect("from")
        from_tok = self._advance()
        if not self._is_string_tok(from_tok):
            raise ParseError(f"expected quoted string after 'from', got {from_tok!r}")
        self._expect("to")
        to_tok = self._advance()
        if not self._is_string_tok(to_tok):
            raise ParseError(f"expected quoted string after 'to', got {to_tok!r}")
        self._expect("via")
        via_tok = self._advance()
        if via_tok not in ("calls", "inherits"):
            raise ParseError(f"'via' expects 'calls' or 'inherits', got {via_tok!r}")
        return PathQuery(
            from_name=from_tok.strip("\"'"),
            to_name=to_tok.strip("\"'"),
            via=via_tok,
        )

    # -- V2: rank (prefix: 'rank VAR in match...') ----------------------- #

    def _parse_rank_prefix(self) -> "RankQuery":
        self._expect("rank")
        rank_var = self._advance()
        self._expect("in")
        inner = self._parse_match_query()
        return self._finish_rank(rank_var, inner)

    # -- V2: rank (postfix: '...select VAR rank VAR by ...') ------------- #

    def _parse_rank_postfix(self, inner: Query) -> "RankQuery":
        self._expect("rank")
        rank_var = self._advance()
        return self._finish_rank(rank_var, inner)

    def _finish_rank(self, rank_var: str, inner: Query) -> "RankQuery":
        self._expect("by")
        metric = self._parse_metric()
        desc = self._peek() == "desc"
        if desc:
            self._advance()
        limit: Optional[int] = None
        if self._peek() == "limit":
            self._advance()
            ltok = self._advance()
            try:
                limit = int(ltok)
            except ValueError:
                raise ParseError(f"'limit' expects an integer, got {ltok!r}")
        return RankQuery(
            inner=inner, rank_var=rank_var, metric=metric, desc=desc, limit=limit
        )

    def _parse_metric(self) -> "RankMetric":
        func = self._advance()
        if func != "count":
            raise ParseError(f"rank metric must start with 'count', got {func!r}")
        self._expect("(")
        direction = self._advance()
        if direction not in _METRIC_DIRS:
            raise ParseError(
                f"metric direction must be callers+/callers/callees+/callees, "
                f"got {direction!r}"
            )
        var = self._advance()
        self._expect(")")
        return RankMetric(func=func, direction=direction, var=var)

    # -- V1: match query ------------------------------------------------- #

    def _parse_match_query(self) -> Query:
        # match <binding> (, <binding>)* [where <conditions>] select <proj_list>
        self._expect("match")
        bindings = self._parse_bindings()

        conditions: Optional[Pred] = None
        if self._peek() == "where":
            self._advance()
            conditions = self._parse_or_expr()

        self._expect("select")
        projections = self._parse_proj_list()

        return Query(bindings=bindings, conditions=conditions, projections=projections)

    # -- match bindings --------------------------------------------------- #

    def _parse_bindings(self) -> list[MatchVar]:
        bindings = [self._parse_one_binding()]
        while self._peek() == ",":
            self._advance()
            # Peek ahead: if the next token is a kind keyword, it's another binding.
            # If not (it could be a projection list in a different layout), stop.
            if self._peek() in _KIND_KEYWORDS:
                bindings.append(self._parse_one_binding())
            else:
                # Put the comma back isn't possible; this shouldn't happen in valid queries
                break
        return bindings

    def _parse_one_binding(self) -> MatchVar:
        kind = self._peek()
        if kind not in _KIND_KEYWORDS:
            raise ParseError(
                f"Expected a kind keyword (function/class/...) but got {kind!r}"
            )
        self._advance()
        var = self._peek()
        if var is None or var in _KEYWORDS:
            raise ParseError(f"Expected variable name after {kind!r}, got {var!r}")
        self._advance()
        return MatchVar(kind=kind, var=var)

    # -- conditions ------------------------------------------------------- #

    def _parse_or_expr(self) -> Pred:
        left = self._parse_and_expr()
        while self._peek() == "or":
            self._advance()
            right = self._parse_and_expr()
            left = OrPred(left=left, right=right)
        return left

    def _parse_and_expr(self) -> Pred:
        left = self._parse_not_expr()
        while self._peek() == "and":
            self._advance()
            right = self._parse_not_expr()
            left = AndPred(left=left, right=right)
        return left

    def _parse_not_expr(self) -> Pred:
        if self._peek() == "not":
            self._advance()
            inner = self._parse_not_expr()
            return NotPred(pred=inner)
        return self._parse_primary_pred()

    def _parse_primary_pred(self) -> Pred:
        if self._peek() == "(":
            self._advance()
            expr = self._parse_or_expr()
            self._expect(")")
            return expr

        # Peek ahead to distinguish attr_pred from closure_pred.
        # closure_pred: IDENT/STRING closure_op IDENT/STRING
        # attr_pred:    VAR . ATTR op value
        #
        # Strategy: check if token+2 is a closure op or token+1 is '.'
        tok0 = self._peek()
        tok1 = (
            self._tokens[self._pos + 1] if self._pos + 1 < len(self._tokens) else None
        )
        tok2 = (
            self._tokens[self._pos + 2] if self._pos + 2 < len(self._tokens) else None
        )

        if tok1 in _CLOSURE_OPS:
            return self._parse_closure_pred()
        if tok1 == "." and tok2 is not None:
            return self._parse_attr_pred()
        raise ParseError(
            f"Cannot parse predicate starting at {tok0!r} (token index {self._pos})"
        )

    def _parse_closure_pred(self) -> ClosurePred:
        lhs_tok = self._advance()
        op = self._advance()  # closure op
        rhs_tok = self._advance()

        lhs_is_var = not (lhs_tok.startswith('"') or lhs_tok.startswith("'"))
        rhs_is_var = not (rhs_tok.startswith('"') or rhs_tok.startswith("'"))

        lhs = lhs_tok.strip("\"'") if not lhs_is_var else lhs_tok
        rhs = rhs_tok.strip("\"'") if not rhs_is_var else rhs_tok

        return ClosurePred(
            lhs=lhs, op=op, rhs=rhs, lhs_is_var=lhs_is_var, rhs_is_var=rhs_is_var
        )

    def _parse_attr_pred(self) -> AttrPred:
        var = self._advance()
        self._expect(".")
        attr = self._advance()

        op_tok = self._peek()
        if op_tok in ("=", "!=", "~"):
            op = self._advance()
            value: str | list[str] = self._parse_string_value()
        elif op_tok == "in":
            op = self._advance()
            value = self._parse_list_value()
        else:
            raise ParseError(f"Expected operator (=, !=, ~, in) but got {op_tok!r}")

        return AttrPred(var=var, attr=attr, op=op, value=value)

    def _parse_string_value(self) -> str:
        tok = self._advance()
        if tok.startswith('"') or tok.startswith("'"):
            return tok.strip("\"'")
        return tok  # bare word (e.g. true/false)

    def _parse_list_value(self) -> list[str]:
        self._expect("[")
        items: list[str] = []
        while self._peek() != "]":
            items.append(self._parse_string_value())
            if self._peek() == ",":
                self._advance()
        self._expect("]")
        return items

    # -- projections ------------------------------------------------------ #

    def _parse_proj_list(self) -> list[Projection]:
        projs = [self._parse_one_proj()]
        while self._peek() == ",":
            self._advance()
            projs.append(self._parse_one_proj())
        return projs

    def _parse_one_proj(self) -> Projection:
        tok = self._peek()
        if tok == "count":
            self._advance()
            self._expect("(")
            var = self._advance()
            self._expect(")")
            return CountProj(var=var)

        var = self._advance()
        if self._peek() == ".":
            self._advance()
            attr = self._advance()
            return AttrProj(var=var, attr=attr)

        return VarProj(var=var)


# ------------------------------------------------------------------ #
# Public entry point
# ------------------------------------------------------------------ #


def parse(query_text: str) -> "Query | PathQuery | RankQuery":
    """Parse a CXQ V2 query string and return the AST.

    Returns a Query (match), PathQuery (show path), or RankQuery (rank).
    Raises ParseError on syntax errors.
    """
    tokens = _tokenize(query_text.strip())
    return _Parser(tokens).parse_query()

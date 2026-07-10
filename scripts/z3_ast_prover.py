#!/usr/bin/env python3
"""DRAFT: prove value properties of C code — libclang AST -> Z3 formula.

Pipeline
    1. libclang parses the C source (in-memory buffer, no files needed)
    2. a tiny symbolic evaluator walks the AST:
         - every parameter becomes a Z3 symbolic constant
         - assignments update an environment  var -> Z3 expression
         - `if` merges branches with Z3 If(cond, then_val, else_val)
         - `return e` yields ONE closed-form Z3 expression for the result
    3. to prove `forall inputs: P(result)` we assert NOT P and ask Z3:
         - unsat  -> property PROVED for all inputs
         - sat    -> Z3 hands back a concrete COUNTEREXAMPLE

Fidelity knob: the same evaluator runs over mathematical integers (z3.Int)
or 32-bit machine integers (z3.BitVec). The abs() demo below shows why it
matters: `-x` is safe over Z, but overflows at INT_MIN over int32.

Requires: pip install z3-solver
Run from repo root: python3 libclang-lab/scripts/z3_ast_prover.py

Draft limits (kept small on purpose): straight-line code + if/else + ternary,
int-typed locals, one return at the end of the function. No loops (loops need
invariants/CHC — that is the next tool), no pointers, no calls.
"""
from clang.cindex import CursorKind

import z3

from _helpers import clang_args, fatal_diagnostics, parse, top_level

SOURCE = """
int clamp(int x) {
    int y = x;
    if (y < 0)   { y = 0; }
    if (y > 100) { y = 100; }
    return y;
}

int my_abs(int x) {
    int r;
    if (x < 0) { r = -x; } else { r = x; }
    return r;
}
"""


# --------------------------------------------------------------------------
# Two "machine models": pick what an `int` means.
class IntModel:
    name = "mathematical integers (z3.Int)"
    var = staticmethod(z3.Int)
    lit = staticmethod(z3.IntVal)


class BitVec32Model:
    name = "32-bit machine ints (z3.BitVec)"
    var = staticmethod(lambda name: z3.BitVec(name, 32))
    lit = staticmethod(lambda v: z3.BitVecVal(v, 32))


# --------------------------------------------------------------------------
# AST -> Z3
WRAPPERS = {CursorKind.UNEXPOSED_EXPR, CursorKind.PAREN_EXPR}


def unwrap(cur):
    """Skip implicit casts / parens — pure AST plumbing, no semantics."""
    while cur.kind in WRAPPERS:
        cur = next(cur.get_children())
    return cur


def binop_spelling(cur):
    """Operator token of a BINARY_OPERATOR: the token between its operands."""
    lhs, rhs = cur.get_children()
    gap_start = lhs.extent.end.offset
    gap_end = rhs.extent.start.offset
    for tok in cur.get_tokens():
        if gap_start <= tok.extent.start.offset < gap_end:
            return tok.spelling
    raise ValueError(f"no operator token found at {cur.extent}")


class SymbolicEvaluator:
    """Walk one function definition, produce a Z3 expression for its return."""

    BINOPS = {
        "+": lambda a, b: a + b,
        "-": lambda a, b: a - b,
        "*": lambda a, b: a * b,
        "/": lambda a, b: a / b,
        "%": lambda a, b: a % b,
        "<": lambda a, b: a < b,
        ">": lambda a, b: a > b,
        "<=": lambda a, b: a <= b,
        ">=": lambda a, b: a >= b,
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        "&&": z3.And,
        "||": z3.Or,
    }

    def __init__(self, model):
        self.model = model
        self.ret = None

    def run(self, fn_cursor):
        """Symbolically execute a FUNCTION_DECL; return (params, ret_expr)."""
        env, body = {}, None
        for child in fn_cursor.get_children():
            if child.kind == CursorKind.PARM_DECL:
                env[child.spelling] = self.model.var(child.spelling)
            elif child.kind == CursorKind.COMPOUND_STMT:
                body = child
        params = dict(env)
        self.exec_stmt(body, env)
        if self.ret is None:
            raise ValueError(f"{fn_cursor.spelling}: no return statement found")
        return params, self.ret

    # -- statements ---------------------------------------------------------
    def exec_stmt(self, cur, env):
        kind = cur.kind
        if kind == CursorKind.COMPOUND_STMT:
            for child in cur.get_children():
                self.exec_stmt(child, env)
        elif kind == CursorKind.DECL_STMT:
            for var in cur.get_children():
                inits = [c for c in var.get_children() if c.kind != CursorKind.TYPE_REF]
                env[var.spelling] = (
                    self.eval_expr(inits[-1], env) if inits
                    else self.model.var(f"{var.spelling}!uninit")
                )
        elif kind == CursorKind.IF_STMT:
            kids = list(cur.get_children())
            cond = self.eval_expr(kids[0], env)
            then_env = dict(env)
            self.exec_stmt(kids[1], then_env)
            else_env = dict(env)
            if len(kids) == 3:
                self.exec_stmt(kids[2], else_env)
            for name in env:  # merge: each var becomes If(cond, then, else)
                t, e = then_env[name], else_env[name]
                env[name] = t if t is e else z3.If(cond, t, e)
        elif kind == CursorKind.RETURN_STMT:
            self.ret = self.eval_expr(next(cur.get_children()), env)
        elif kind == CursorKind.BINARY_OPERATOR:  # expression statement `x = e;`
            self.eval_expr(cur, env)
        else:
            raise NotImplementedError(f"statement kind {kind} at {cur.extent}")

    # -- expressions ---------------------------------------------------------
    def eval_expr(self, cur, env):
        cur = unwrap(cur)
        kind = cur.kind
        if kind == CursorKind.DECL_REF_EXPR:
            return env[cur.spelling]
        if kind == CursorKind.INTEGER_LITERAL:
            return self.model.lit(int(next(cur.get_tokens()).spelling, 0))
        if kind == CursorKind.UNARY_OPERATOR:
            op = next(cur.get_tokens()).spelling
            operand = self.eval_expr(next(cur.get_children()), env)
            if op == "-":
                return -operand
            if op == "+":
                return operand
            if op == "!":
                return z3.Not(operand)
            raise NotImplementedError(f"unary operator {op!r}")
        if kind == CursorKind.CONDITIONAL_OPERATOR:
            c, a, b = (self.eval_expr(k, env) for k in cur.get_children())
            return z3.If(c, a, b)
        if kind == CursorKind.BINARY_OPERATOR:
            op = binop_spelling(cur)
            lhs_cur, rhs_cur = cur.get_children()
            rhs = self.eval_expr(rhs_cur, env)
            if op == "=":  # assignment: update the environment
                env[unwrap(lhs_cur).spelling] = rhs
                return rhs
            return self.BINOPS[op](self.eval_expr(lhs_cur, env), rhs)
        raise NotImplementedError(f"expression kind {kind} at {cur.extent}")


# --------------------------------------------------------------------------
# The prover: forall-inputs proof by refutation.
def check(label, prop):
    solver = z3.Solver()
    solver.add(z3.Not(prop))  # a counterexample to P is a model of NOT P
    verdict = solver.check()
    if verdict == z3.unsat:
        print(f"    PROVED          {label}")
    elif verdict == z3.sat:
        print(f"    COUNTEREXAMPLE  {label}   at {solver.model()}")
    else:
        print(f"    UNKNOWN         {label}")


def main():
    tu = parse("demo.c", args=clang_args(), unsaved_files=[("demo.c", SOURCE)])
    assert not fatal_diagnostics(tu), [str(d) for d in tu.diagnostics]
    functions = {
        c.spelling: c
        for c in top_level(tu)
        if c.kind == CursorKind.FUNCTION_DECL and c.is_definition()
    }

    for model in (IntModel, BitVec32Model):
        print(f"\n=== int modelled as {model.name} ===")

        params, ret = SymbolicEvaluator(model).run(functions["clamp"])
        x = params["x"]
        print(f"  clamp(x) as one Z3 term:\n    {ret}")
        check("forall x: 0 <= clamp(x) <= 100", z3.And(ret >= 0, ret <= 100))
        check("forall x: clamp(x) > 0       ", ret > 0)

        params, ret = SymbolicEvaluator(model).run(functions["my_abs"])
        check("forall x: my_abs(x) >= 0     ", ret >= 0)


if __name__ == "__main__":
    main()

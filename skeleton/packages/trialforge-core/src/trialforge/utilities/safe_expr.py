"""Restricted arithmetic expression evaluator for declared derived metrics.

Only numeric literals, metric names, ``+ - * / ** %``, unary minus and a small
whitelist of pure functions are accepted. There is no attribute access, no
subscripting and no calls to anything outside the whitelist, so declared
formulas cannot execute arbitrary code.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

from trialforge.errors import ValidationError

_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"abs": abs, "min": min, "max": max, "sqrt": math.sqrt, "log": math.log, "exp": math.exp}


def parse_expression(expression: str) -> ast.Expression:
    """Parse and structurally validate ``expression``; raise ValidationError if unsafe."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValidationError(f"invalid expression '{expression}': {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load, ast.operator, ast.unaryop)):
            continue
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            continue
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            continue
        if isinstance(node, ast.Name):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            continue
        raise ValidationError(f"expression '{expression}' uses unsupported syntax: {type(node).__name__}")
    return tree


def expression_names(expression: str) -> set[str]:
    """Return the metric names referenced by ``expression`` (excluding function names)."""
    tree = parse_expression(expression)
    func_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - func_names


def evaluate_expression(expression: str, values: dict[str, float]) -> float:
    """Evaluate a validated expression against ``values``."""
    tree = parse_expression(expression)

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise KeyError(node.id)
            return values[node.id]
        if isinstance(node, ast.BinOp):
            return _BIN[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp):
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call):
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValidationError("unsupported node")  # pragma: no cover

    return float(ev(tree))

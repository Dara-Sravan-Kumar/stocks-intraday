"""Whitelist-only boolean expression interpreter for strategy entry_exprs.

SAFETY IS THE POINT. LLM/bred text is DATA, never code:
  * NO eval / exec / compile-to-code — a hand-written AST walker.
  * NO builtins, attributes, subscripts, lambdas, comprehensions, f-strings.
  * Calls limited to min / max / abs.
  * Names must be in the caller-supplied whitelist (snapshot fields only).
Anything else is refused at validate() time and therefore NEVER executed.

None-safe by construction: a None operand makes a comparison False and poisons
arithmetic to None, so a strategy referencing a not-yet-computed indicator simply
does not fire — it can neither error out of a scan nor trade on garbage.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

_ALLOWED_CALLS = {"min", "max", "abs"}

# Node types permitted anywhere in the tree. Everything not listed is refused.
_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.Mod, ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq,
    ast.NotEq, ast.Call, ast.Name, ast.Load, ast.Constant,
)


class ExprError(ValueError):
    """entry_expr failed validation and must never be evaluated."""


@dataclass(frozen=True)
class CompiledExpr:
    source: str
    tree: ast.Expression
    names: frozenset[str]


def _check(node: ast.AST, allowed_names: frozenset[str]) -> None:
    if not isinstance(node, _ALLOWED_NODES):
        raise ExprError(f"disallowed syntax: {type(node).__name__}")

    if isinstance(node, ast.Call):
        # only bare min/max/abs(...), no keywords, no *args/**kwargs
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
            raise ExprError("only min/max/abs calls are allowed")
        if node.keywords:
            raise ExprError("keyword arguments are not allowed")
        # func Name is a call target, not a variable — validate only its args
        for arg in node.args:
            _check(arg, allowed_names)
        return

    if isinstance(node, ast.Name):
        if node.id not in allowed_names:
            raise ExprError(f"unknown name: {node.id!r}")
        return

    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, bool)):
            raise ExprError(f"disallowed constant: {node.value!r}")
        return

    for child in ast.iter_child_nodes(node):
        _check(child, allowed_names)


def validate_expr(expr: str, allowed_names: frozenset[str] | set[str]) -> CompiledExpr:
    """Parse and fully validate; raise ExprError on anything unsafe/unknown.
    Field names containing digits (rsi14, vwap_dist_pct) are ordinary Name
    nodes and are preserved exactly — never split or corrupted."""
    if not expr or not expr.strip():
        raise ExprError("empty expression")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"syntax error: {exc.msg}") from exc
    names = frozenset(allowed_names)
    _check(tree, names)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and not _is_call_target(n, tree)}
    return CompiledExpr(source=expr, tree=tree, names=frozenset(used))


def _is_call_target(name: ast.Name, tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.func is name:
            return True
    return False


def compile_expr(expr: str, allowed_names: frozenset[str] | set[str]) -> CompiledExpr:
    return validate_expr(expr, allowed_names)


# --- evaluation -------------------------------------------------------------

_CMP = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


def _truthy(v) -> bool:
    return bool(v) if v is not None else False


def _ev(node: ast.AST, env: dict):
    if isinstance(node, ast.Expression):
        return _ev(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.BoolOp):
        vals = node.values
        if isinstance(node.op, ast.And):
            for v in vals:
                if not _truthy(_ev(v, env)):
                    return False
            return True
        for v in vals:                      # Or
            if _truthy(_ev(v, env)):
                return True
        return False
    if isinstance(node, ast.UnaryOp):
        val = _ev(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not _truthy(val)
        if val is None:
            return None
        return -val if isinstance(node.op, ast.USub) else +val
    if isinstance(node, ast.BinOp):
        a, b = _ev(node.left, env), _ev(node.right, env)
        if a is None or b is None:
            return None
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Div):
            return a / b if b != 0 else None
        if isinstance(node.op, ast.Mod):
            return a % b if b != 0 else None
    if isinstance(node, ast.Compare):
        left = _ev(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, env)
            if left is None or right is None:
                return False              # can't confirm -> no signal
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        args = [_ev(a, env) for a in node.args]
        fn = node.func.id
        if fn == "abs":
            return abs(args[0]) if args and args[0] is not None else None
        vals = [a for a in args if a is not None]
        if not vals:
            return None
        return min(vals) if fn == "min" else max(vals)
    raise ExprError(f"unevaluable node: {type(node).__name__}")   # pragma: no cover


def eval_expr(compiled: CompiledExpr, env: dict) -> bool:
    """Evaluate a *validated* expression against a snapshot env. Any residual
    runtime error is swallowed to False so a bad spec never fires or crashes."""
    try:
        return _truthy(_ev(compiled.tree, env))
    except Exception:      # noqa: BLE001 — safety net; a spec must never crash a scan
        return False

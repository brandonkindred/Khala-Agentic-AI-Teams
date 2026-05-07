"""Runtime AST instrumenter for Strategy Lab coverage probe (#449).

Rewrites a generated strategy module so that every ``if`` / ``elif``
predicate inside ``on_bar`` records the truth of each subcondition via
``__probe_record__(rule_id, __probe_bar_index__, value)``. The default
``__probe_record__`` defined by the bootstrap prelude is the identity
function, so the rewritten module behaves identically to the original
when no harness has injected a real recorder. The runtime probe harness
(#450) will rebind ``__probe_record__`` and ``__probe_bar_index__`` in
the exec globals before each bar.

The dunder-style names are deliberate. A plain ``__probe_record`` /
``__bar_index`` inside the strategy class body would be subject to
Python's class-private name mangling and become
``_StrategyClass__probe_record`` etc., breaking the harness contract.
Equally, a non-prefixed name like ``bar_index`` would be captured by any
``on_bar`` local of the same name (Python resolves identifiers via
function-scope symbol tables, not by lexical order), turning a probe
read into an ``UnboundLocalError`` or — worse — silently recording the
strategy's loop counter. Trailing underscores opt out of mangling, and
the dunder prefix makes collision with hand-written strategy locals
practically impossible.

Pure source-to-source: no execution, no I/O, no LLM.

The code emitted by this module is intended to be executed only by the
probe harness — it is **not** re-fed through ``CodeSafetyChecker``. The
prelude uses ``try/except NameError`` rather than ``globals()`` because the
safety gate bans the latter.
"""

from __future__ import annotations

import ast
import warnings
from typing import List, Tuple

from investment_team.models import RuleIndex

_PROBE_NAME = "__probe_record__"
_BAR_INDEX_NAME = "__probe_bar_index__"
_LABEL_MAX_LEN = 120


def instrument_strategy_code(code: str) -> Tuple[str, RuleIndex]:
    """Wrap each subcondition inside ``on_bar`` if-tests with a probe call.

    Returns ``(rewritten_code, rule_index)``. On malformed source, on a
    module without an ``on_bar`` method, or when the source is already
    instrumented, returns a sensible no-op pair (see acceptance criteria
    in #449). Never raises.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        warnings.warn(
            f"runtime_instrument: source did not parse ({exc}); returning original",
            UserWarning,
            stacklevel=2,
        )
        return code, RuleIndex()

    if _is_already_instrumented(tree):
        return code, _rebuild_index_from_existing(tree)

    on_bar = _find_on_bar(tree)
    if on_bar is None:
        return code, RuleIndex()

    rules: dict[str, str] = {}
    counter = [0]
    transformer = _IfTransformer(rules, counter)
    on_bar.body = [transformer.visit(stmt) for stmt in on_bar.body]
    ast.fix_missing_locations(on_bar)

    if not rules:
        return code, RuleIndex()

    prelude = _bootstrap_prelude()
    insert_at = _prelude_insertion_index(tree)
    tree.body = tree.body[:insert_at] + prelude + tree.body[insert_at:]
    ast.fix_missing_locations(tree)

    try:
        rewritten = ast.unparse(tree)
    except Exception as exc:  # pragma: no cover — ast.unparse is robust on parsed trees
        warnings.warn(
            f"runtime_instrument: ast.unparse failed ({exc}); returning original",
            UserWarning,
            stacklevel=2,
        )
        return code, RuleIndex()

    return rewritten, RuleIndex(rules=rules)


def _is_already_instrumented(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == _PROBE_NAME
        ):
            return True
    return False


def _rebuild_index_from_existing(tree: ast.AST) -> RuleIndex:
    rules: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == _PROBE_NAME
            and len(node.args) == 3
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            rid = node.args[0].value
            if rid in rules:
                continue
            rules[rid] = _label_for(node.args[2])
    return RuleIndex(rules=rules)


def _find_on_bar(tree: ast.AST) -> ast.FunctionDef | None:
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _is_strategy_subclass(node):
            continue
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef) and child.name == "on_bar":
                return child
    return None


def _is_strategy_subclass(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id == "Strategy":
            return True
        if (
            isinstance(base, ast.Attribute)
            and base.attr == "Strategy"
            and isinstance(base.value, ast.Name)
            and base.value.id == "contract"
        ):
            return True
    return False


class _IfTransformer(ast.NodeTransformer):
    """Wraps subconditions of ``If.test`` nodes within ``on_bar``.

    Descends through compound bodies (``If``, ``For``, ``While``, ``With``,
    ``Try``) but stops at nested function / lambda boundaries so helper
    functions defined inside ``on_bar`` are left untouched.
    """

    def __init__(self, rules: dict[str, str], counter: List[int]) -> None:
        self._rules = rules
        self._counter = counter

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:  # noqa: N802
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:  # noqa: N802
        return node

    def visit_Lambda(self, node: ast.Lambda) -> ast.AST:  # noqa: N802
        return node

    def visit_If(self, node: ast.If) -> ast.AST:  # noqa: N802
        node.test = _wrap_expr(node.test, self._rules, self._counter)
        node.body = [self.visit(child) for child in node.body]
        node.orelse = [self.visit(child) for child in node.orelse]
        return node


def _wrap_expr(expr: ast.expr, rules: dict[str, str], counter: List[int]) -> ast.expr:
    if isinstance(expr, ast.BoolOp):
        expr.values = [_wrap_expr(v, rules, counter) for v in expr.values]
        return expr
    if _is_already_wrapped(expr):
        return expr
    if isinstance(expr, ast.Constant):
        return expr
    return _wrap_leg(expr, rules, counter)


def _wrap_leg(leg: ast.expr, rules: dict[str, str], counter: List[int]) -> ast.Call:
    rid = f"r{counter[0]}"
    counter[0] += 1
    rules[rid] = _label_for(leg)
    call = ast.Call(
        func=ast.Name(id=_PROBE_NAME, ctx=ast.Load()),
        args=[
            ast.Constant(value=rid),
            ast.Name(id=_BAR_INDEX_NAME, ctx=ast.Load()),
            leg,
        ],
        keywords=[],
    )
    ast.copy_location(call, leg)
    return call


def _is_already_wrapped(expr: ast.expr) -> bool:
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id == _PROBE_NAME
    )


def _label_for(expr: ast.expr) -> str:
    try:
        text = ast.unparse(expr)
    except Exception:  # pragma: no cover
        return ""
    text = " ".join(text.split())
    if len(text) > _LABEL_MAX_LEN:
        text = text[: _LABEL_MAX_LEN - 1] + "…"
    return text


def _bootstrap_prelude() -> List[ast.stmt]:
    """Return AST statements that define no-op defaults for the probe API.

    Uses ``try/except NameError`` rather than a ``globals()`` check because
    the strategy code-safety gate bans ``globals(`` patterns. Both names
    are only defined when not already present, so the harness (#450) can
    pre-bind real implementations in the exec globals.
    """
    src = (
        "try:\n"
        "    __probe_record__  # noqa: F821\n"
        "except NameError:\n"
        "    def __probe_record__(_rid, _bidx, _value):\n"
        "        return _value\n"
        "try:\n"
        "    __probe_bar_index__  # noqa: F821\n"
        "except NameError:\n"
        "    __probe_bar_index__ = 0\n"
    )
    return ast.parse(src).body


def _prelude_insertion_index(tree: ast.Module) -> int:
    """Return the index in ``tree.body`` after which the prelude is safe to
    insert. Skips a leading module docstring and any ``from __future__``
    imports — those statements have placement constraints (``__future__``
    must be the first non-docstring statement) and prepending the prelude
    ahead of them would emit a module that no longer compiles.
    """
    idx = 0
    body = tree.body
    if (
        idx < len(body)
        and isinstance(body[idx], ast.Expr)
        and isinstance(body[idx].value, ast.Constant)
        and isinstance(body[idx].value.value, str)
    ):
        idx += 1
    while (
        idx < len(body)
        and isinstance(body[idx], ast.ImportFrom)
        and body[idx].module == "__future__"
    ):
        idx += 1
    return idx

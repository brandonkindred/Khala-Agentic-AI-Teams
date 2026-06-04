"""Predicate DSL for the Agent Cognition Core rules engine.

An ``enforced`` rule carries a non-executable JSON ``predicate`` that the
enforcement layer evaluates deterministically into an allow/block decision —
the Design-by-Contract pre/post/tool-gate conditions the cognition design
mandates. This module is the engine: it parses a stored predicate dict into a
frozen, validated tree and evaluates that tree against a phase-specific root
mapping.

It is intentionally pure: no Postgres, no LLM, no filesystem, and — the
load-bearing safety property — **no** ``eval`` / ``exec`` / ``getattr`` on
arbitrary objects. The only operators that exist are a fixed allowlist; anything
else is rejected at parse time. Path resolution walks plain ``dict`` keys only.

Schema (stored in ``Rule.predicate`` JSONB)::

    {"phase": "precondition" | "postcondition" | "tool_gate", "check": <node>}

    comparison leaf : {"op": "<"|"<="|">"|">="|"=="|"!="|"in",
                       "path": "<dotted>", "value": <scalar|array>}
    tool leaf       : {"op": "forbid_tool", "tool_id": "<str>" | ["<str>", ...]}
                      (valid only inside a "tool_gate" predicate)
    composite       : {"op": "all"|"any", "of": [<node>, ...]}   # non-empty
                      {"op": "not", "of": [<node>]}               # exactly one

Semantics are uniform: every node evaluates to ``holds: bool`` meaning "the
constraint is satisfied ⇒ allow". A phase allows iff *all* applicable rules'
predicates hold (see :mod:`agent_cognition.rules.enforcement`).

Design by Contract:

* :func:`parse_predicate` / :func:`validate_predicate` raise
  :class:`PredicateError` on any malformed/unknown construct, with **no
  evaluation and no side effects** — so an enforced rule can be validated at
  approve time before it is ever stored active.
* :func:`evaluate` never raises on input *data* shape — and raises only on
  programmer misuse (a non-``Predicate`` argument).

Missing-path semantics are **per-operator**, so a missing value is an ordinary
distinct value (not a special fail-closed state that would invert under
``not``):

* ``==`` / ``in`` against a missing path are ``False`` (a missing value equals
  nothing and is a member of nothing) — so a required ``output.status == "ok"``
  blocks when ``status`` is absent.
* ``!=`` against a missing path is ``True`` — so ``output.error != "fatal"``
  *allows* when ``error`` is simply absent (the success case), and the same
  result holds whether the check is written directly or wrapped in ``not``.
* ordered numeric ops (``< <= > >=``) require both operands to be real numbers
  (``bool`` excluded); a missing or non-numeric operand is ``False`` — so a
  ``input.temperature <= 0.7`` precondition blocks when ``temperature`` is
  absent.

Equality (``==`` / ``!=`` / ``in``) is **strict**: ``bool`` never coerces to or
from ``int`` (``True`` is not ``1``), matching the ordered ops' bool exclusion.
"""

from __future__ import annotations

import operator
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PredicateError",
    "Predicate",
    "parse_predicate",
    "validate_predicate",
    "is_valid_predicate",
    "evaluate",
]

# Fixed allowlists — the only constructs the DSL recognises. Anything outside
# these is rejected at parse time (no silent default, never evaluated).
_PHASES = frozenset({"precondition", "postcondition", "tool_gate"})
_COMPARISON_OPS = frozenset({"<", "<=", ">", ">=", "==", "!=", "in"})
_NUMERIC_OPS = frozenset({"<", "<=", ">", ">="})
_COMPOSITE_OPS = frozenset({"all", "any", "not"})

_NUMERIC_CMP = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


class PredicateError(ValueError):
    """A predicate dict is malformed or uses a construct outside the allowlist."""


class _Missing:
    """Sentinel for a path that does not resolve in the evaluation root."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "MISSING"


MISSING = _Missing()


# ---------------------------------------------------------------------------
# Parsed (validated, frozen) node tree. Internal — callers store the dict form.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Comparison:
    op: str
    path: tuple[str, ...]
    value: Any


@dataclass(frozen=True)
class _ForbidTool:
    tool_ids: frozenset[str]


@dataclass(frozen=True)
class _Composite:
    op: str  # "all" | "any" | "not"
    children: tuple[Any, ...]  # tuple[_Node, ...]


@dataclass(frozen=True)
class Predicate:
    """A parsed, validated predicate: a phase plus a single check node."""

    phase: str
    check: Any  # _Node


# ---------------------------------------------------------------------------
# Parsing / validation (no evaluation, no side effects)
# ---------------------------------------------------------------------------
def parse_predicate(predicate: dict[str, Any]) -> Predicate:
    """Validate ``predicate`` and build its frozen node tree.

    Preconditions:
        * ``predicate`` is the stored dict form (a non-empty object with a valid
          ``phase`` and a ``check`` node).
    Postconditions:
        * Returns a :class:`Predicate` whose tree uses only allowlisted ops, or
          raises :class:`PredicateError`. No evaluation occurs and nothing is
          mutated.
    """
    if not isinstance(predicate, dict):
        raise PredicateError(f"predicate must be a dict, got {type(predicate).__name__}")
    extra = set(predicate) - {"phase", "check"}
    if extra:
        raise PredicateError(f"unexpected predicate keys: {sorted(extra)}")
    phase = predicate.get("phase")
    if phase not in _PHASES:
        raise PredicateError(f"unknown or missing phase {phase!r}; allowed: {sorted(_PHASES)}")
    if "check" not in predicate:
        raise PredicateError("predicate is missing 'check'")
    return Predicate(phase=phase, check=_parse_node(predicate["check"], phase=phase))


def validate_predicate(predicate: dict[str, Any]) -> None:
    """Raise :class:`PredicateError` iff ``predicate`` is not a valid predicate.

    Postconditions: returns ``None`` when :func:`parse_predicate` would succeed;
    otherwise raises. Used by the rules store to gate an *enforced* rule before
    it is approved active.
    """
    parse_predicate(predicate)


def is_valid_predicate(predicate: dict[str, Any]) -> bool:
    """Return whether ``predicate`` parses cleanly (never raises)."""
    try:
        parse_predicate(predicate)
    except PredicateError:
        return False
    return True


def _parse_node(node: Any, *, phase: str) -> Any:
    if not isinstance(node, dict):
        raise PredicateError(f"predicate node must be a dict, got {type(node).__name__}")
    op = node.get("op")
    if not isinstance(op, str):
        raise PredicateError(f"node is missing a string 'op': {node!r}")
    if op in _COMPOSITE_OPS:
        return _parse_composite(node, op=op, phase=phase)
    if op == "forbid_tool":
        if phase != "tool_gate":
            raise PredicateError("'forbid_tool' is only valid inside a 'tool_gate' predicate")
        return _parse_forbid_tool(node)
    if op in _COMPARISON_OPS:
        return _parse_comparison(node, op=op)
    allowed = sorted(_COMPARISON_OPS | _COMPOSITE_OPS | {"forbid_tool"})
    raise PredicateError(f"unknown op {op!r}; allowed: {allowed}")


def _parse_composite(node: dict[str, Any], *, op: str, phase: str) -> _Composite:
    extra = set(node) - {"op", "of"}
    if extra:
        raise PredicateError(f"unexpected keys on '{op}' node: {sorted(extra)}")
    of = node.get("of")
    if not isinstance(of, list) or not of:
        raise PredicateError(f"'{op}' requires a non-empty 'of' list")
    if op == "not" and len(of) != 1:
        raise PredicateError(f"'not' requires exactly one child, got {len(of)}")
    return _Composite(op=op, children=tuple(_parse_node(child, phase=phase) for child in of))


def _parse_comparison(node: dict[str, Any], *, op: str) -> _Comparison:
    extra = set(node) - {"op", "path", "value"}
    if extra:
        raise PredicateError(f"unexpected keys on '{op}' node: {sorted(extra)}")
    path = node.get("path")
    if not isinstance(path, str) or not path:
        raise PredicateError(f"comparison '{op}' requires a non-empty string 'path'")
    segments = tuple(path.split("."))
    if any(not seg for seg in segments):
        raise PredicateError(f"comparison '{op}' path {path!r} has an empty segment")
    if "value" not in node:
        raise PredicateError(f"comparison '{op}' requires a 'value'")
    value = node["value"]
    if op == "in":
        if not isinstance(value, list):
            raise PredicateError("'in' requires an array 'value'")
        value = tuple(value)
    elif isinstance(value, (list, dict)):
        raise PredicateError(
            f"comparison '{op}' requires a scalar 'value', got {type(value).__name__}"
        )
    return _Comparison(op=op, path=segments, value=value)


def _parse_forbid_tool(node: dict[str, Any]) -> _ForbidTool:
    extra = set(node) - {"op", "tool_id"}
    if extra:
        raise PredicateError(f"unexpected keys on 'forbid_tool' node: {sorted(extra)}")
    tool_id = node.get("tool_id")
    if isinstance(tool_id, str):
        if not tool_id:
            raise PredicateError("'forbid_tool' tool_id must be a non-empty string")
        ids: tuple[str, ...] = (tool_id,)
    elif isinstance(tool_id, list):
        if not tool_id or not all(isinstance(t, str) and t for t in tool_id):
            raise PredicateError("'forbid_tool' tool_id list must be non-empty strings")
        ids = tuple(tool_id)
    else:
        raise PredicateError("'forbid_tool' requires 'tool_id' as a string or list of strings")
    return _ForbidTool(tool_ids=frozenset(ids))


# ---------------------------------------------------------------------------
# Evaluation (total over data shape — fails closed, never raises on bad data)
# ---------------------------------------------------------------------------
def evaluate(pred: Predicate, root: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Evaluate ``pred`` against ``root``.

    Preconditions:
        * ``pred`` is a :class:`Predicate` from :func:`parse_predicate`.
        * ``root`` is a mapping shaped for ``pred.phase`` (see module docstring).
    Postconditions:
        * Returns ``(holds, reason)``: ``holds`` is ``True`` when the constraint
          is satisfied (⇒ allow) and ``reason`` is ``None``; on failure ``holds``
          is ``False`` and ``reason`` is a short human string. A missing path or
          a type-incompatible operand resolves per-operator (see the module
          docstring) and never raises.
        * Raises ``TypeError`` only on programmer misuse (``pred`` not a
          ``Predicate``).
    """
    if not isinstance(pred, Predicate):
        raise TypeError(f"evaluate expects a parsed Predicate, got {type(pred).__name__}")
    return _eval_node(pred.check, root)


def _eval_node(node: Any, root: Mapping[str, Any]) -> tuple[bool, str | None]:
    if isinstance(node, _Comparison):
        return _eval_comparison(node, root)
    if isinstance(node, _ForbidTool):
        return _eval_forbid_tool(node, root)
    return _eval_composite(node, root)


def _eval_composite(node: _Composite, root: Mapping[str, Any]) -> tuple[bool, str | None]:
    if node.op == "not":
        holds, _reason = _eval_node(node.children[0], root)
        return (False, "negated condition held") if holds else (True, None)
    if node.op == "all":
        for child in node.children:
            holds, reason = _eval_node(child, root)
            if not holds:
                return False, reason
        return True, None
    # "any"
    reasons: list[str] = []
    for child in node.children:
        holds, reason = _eval_node(child, root)
        if holds:
            return True, None
        reasons.append(reason or "condition not met")
    return False, "no alternative satisfied: " + "; ".join(reasons)


def _eval_forbid_tool(node: _ForbidTool, root: Mapping[str, Any]) -> tuple[bool, str | None]:
    tool_id = root.get("tool_id") if isinstance(root, Mapping) else None
    # Only a string tool_id can match a forbidden (string) id; guarding the
    # type also keeps an unhashable tool_id from raising in the set membership.
    if isinstance(tool_id, str) and tool_id in node.tool_ids:
        return False, f"tool {tool_id!r} is forbidden"
    return True, None


def _eval_comparison(node: _Comparison, root: Mapping[str, Any]) -> tuple[bool, str | None]:
    actual = _resolve_path(node.path, root)
    dotted = ".".join(node.path)
    missing = actual is MISSING
    shown = "<missing>" if missing else repr(actual)
    op = node.op
    if op == "in":
        if any(_strict_eq(actual, candidate) for candidate in node.value):
            return True, None
        return False, f"path {dotted!r} value {shown} not in {list(node.value)!r}"
    if op == "==":
        ok = _strict_eq(actual, node.value)
    elif op == "!=":
        # A missing/distinct value is genuinely "not equal", so this allows when
        # the path is absent — and composes correctly under ``not`` because the
        # result is a concrete bool, not a fail-closed sentinel.
        ok = not _strict_eq(actual, node.value)
    else:  # numeric: < <= > >=
        if not _is_number(actual) or not _is_number(node.value):
            detail = "is missing" if missing else f"is not numeric ({actual!r})"
            return False, f"path {dotted!r} {detail}; {op!r} needs numeric operands"
        ok = _NUMERIC_CMP[op](actual, node.value)
    if ok:
        return True, None
    return False, f"path {dotted!r} value {shown} fails {op} {node.value!r}"


def _resolve_path(path: tuple[str, ...], root: Mapping[str, Any]) -> Any:
    """Walk a dotted path through dict keys only; return ``MISSING`` if absent."""
    current: Any = root
    for segment in path:
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
        else:
            return MISSING
    return current


def _is_number(value: Any) -> bool:
    """Numeric for ordered comparison — ``bool`` is excluded (it is an ``int``)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _strict_eq(a: Any, b: Any) -> bool:
    """Equality that never coerces ``bool`` to/from ``int`` (``True`` is not ``1``).

    Mirrors the ordered ops' ``bool`` exclusion so equality/membership checks
    can't be satisfied by a loose ``1``/``True`` (or ``0``/``False``) crossover.
    A ``MISSING`` operand is just a distinct value: it equals nothing concrete.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b

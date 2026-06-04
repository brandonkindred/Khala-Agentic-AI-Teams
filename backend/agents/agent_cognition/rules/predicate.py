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
    exists leaf     : {"op": "exists", "path": "<dotted>"}   # is the path present?
    tool leaf       : {"op": "forbid_tool", "tool_id": "<str>" | ["<str>", ...]}
                      (valid only inside a "tool_gate" predicate)
    composite       : {"op": "all"|"any", "of": [<node>, ...]}   # non-empty
                      {"op": "not", "of": [<node>]}               # exactly one

A phase allows iff *all* applicable rules' predicates allow (see
:mod:`agent_cognition.rules.enforcement`).

Design by Contract:

* :func:`parse_predicate` / :func:`validate_predicate` raise
  :class:`PredicateError` on any malformed/unknown construct, with **no
  evaluation and no side effects** — so an enforced rule can be validated at
  approve time before it is ever stored active.
* :func:`evaluate` never raises on input *data* shape — and raises only on
  programmer misuse (a non-``Predicate`` argument).

Missing/undecidable data is **fail-closed** via three-valued (Kleene) logic. A
node evaluates to ALLOW / BLOCK / UNKNOWN; :func:`evaluate` collapses UNKNOWN to
a block, so a predicate that cannot be decided never silently allows:

* A missing path, a present-but-non-numeric operand for an ordered op, a
  non-string ``tool_id`` at a tool gate, or a value whose comparison raises →
  UNKNOWN (never a guessed True/False).
* ``not`` / ``all`` / ``any`` propagate UNKNOWN (Kleene): ``not UNKNOWN`` is
  UNKNOWN; ``all`` blocks if any child blocks, else is UNKNOWN if any child is
  unknown; ``any`` allows if any child allows, else is UNKNOWN if any child is
  unknown. So ``output.error != "fatal"`` *blocks* when ``error`` is absent
  (fail closed), and wrapping a check in ``not`` cannot invert a missing value
  into an allow.

To deliberately allow when a field is absent, use the ``exists`` leaf — the only
operator that returns a concrete present/absent verdict (never UNKNOWN). E.g.
``any(not(exists(output.error)), output.error != "fatal")`` allows when
``error`` is absent OR is not ``"fatal"``, and blocks only when it equals
``"fatal"``.

Equality (``==`` / ``!=`` / ``in``) is **strict**: ``bool`` never coerces to or
from ``int`` (``True`` is not ``1``). Ordered ops (``< <= > >=``) require a
numeric stored ``value`` (rejected at parse otherwise) and a numeric runtime
operand (else UNKNOWN).
"""

from __future__ import annotations

import operator
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
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


class _Verdict(Enum):
    """Kleene three-valued evaluation result (UNKNOWN fails closed at the gate)."""

    ALLOW = "allow"
    BLOCK = "block"
    UNKNOWN = "unknown"


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
class _Exists:
    path: tuple[str, ...]


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
    if op == "exists":
        return _parse_exists(node)
    if op in _COMPARISON_OPS:
        return _parse_comparison(node, op=op)
    allowed = sorted(_COMPARISON_OPS | _COMPOSITE_OPS | {"forbid_tool", "exists"})
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
    segments = _parse_path(node, op=op)
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
    elif op in _NUMERIC_OPS and not _is_number(value):
        # An ordered comparison against a non-number can never hold at runtime, so
        # a typo'd threshold (e.g. the string "0.7") would silently become a
        # permanent block. Reject it at the write boundary instead.
        raise PredicateError(
            f"ordered comparison '{op}' requires a numeric 'value', got {type(value).__name__}"
        )
    return _Comparison(op=op, path=segments, value=value)


def _parse_exists(node: dict[str, Any]) -> _Exists:
    extra = set(node) - {"op", "path"}
    if extra:
        raise PredicateError(f"unexpected keys on 'exists' node: {sorted(extra)}")
    return _Exists(path=_parse_path(node, op="exists"))


def _parse_path(node: dict[str, Any], *, op: str) -> tuple[str, ...]:
    path = node.get("path")
    if not isinstance(path, str) or not path:
        raise PredicateError(f"'{op}' requires a non-empty string 'path'")
    segments = tuple(path.split("."))
    if any(not seg for seg in segments):
        raise PredicateError(f"'{op}' path {path!r} has an empty segment")
    return segments


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
# Evaluation (three-valued, fail-closed; never raises on bad data)
# ---------------------------------------------------------------------------
def evaluate(pred: Predicate, root: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Evaluate ``pred`` against ``root``.

    Preconditions:
        * ``pred`` is a :class:`Predicate` from :func:`parse_predicate`.
        * ``root`` is a mapping shaped for ``pred.phase`` (see module docstring).
    Postconditions:
        * Returns ``(allow, reason)``: ``allow`` is ``True`` only when the
          predicate evaluates to ALLOW (``reason`` is then ``None``). A BLOCK or
          an undecidable UNKNOWN both collapse to ``(False, reason)`` — UNKNOWN
          fails **closed**. Never raises on input data shape (a missing path,
          type mismatch, or a value whose comparison raises → UNKNOWN).
        * Raises ``TypeError`` only on programmer misuse (``pred`` not a
          ``Predicate``).
    """
    if not isinstance(pred, Predicate):
        raise TypeError(f"evaluate expects a parsed Predicate, got {type(pred).__name__}")
    verdict, reason = _eval_node(pred.check, root)
    if verdict is _Verdict.ALLOW:
        return True, None
    return False, reason


def _eval_node(node: Any, root: Mapping[str, Any]) -> tuple[_Verdict, str | None]:
    if isinstance(node, _Comparison):
        return _eval_comparison(node, root)
    if isinstance(node, _Exists):
        return _eval_exists(node, root)
    if isinstance(node, _ForbidTool):
        return _eval_forbid_tool(node, root)
    return _eval_composite(node, root)


def _eval_composite(node: _Composite, root: Mapping[str, Any]) -> tuple[_Verdict, str | None]:
    if node.op == "not":
        verdict, reason = _eval_node(node.children[0], root)
        if verdict is _Verdict.ALLOW:
            return _Verdict.BLOCK, "negated condition held"
        if verdict is _Verdict.BLOCK:
            return _Verdict.ALLOW, None
        return _Verdict.UNKNOWN, reason  # not(unknown) = unknown
    if node.op == "all":
        # all: block if any child blocks; else unknown if any child is unknown.
        unknown_reason: str | None = None
        for child in node.children:
            verdict, reason = _eval_node(child, root)
            if verdict is _Verdict.BLOCK:
                return _Verdict.BLOCK, reason
            if verdict is _Verdict.UNKNOWN and unknown_reason is None:
                unknown_reason = reason
        if unknown_reason is not None:
            return _Verdict.UNKNOWN, unknown_reason
        return _Verdict.ALLOW, None
    # "any": allow if any child allows; else unknown if any child is unknown.
    block_reasons: list[str] = []
    unknown_reason = None
    for child in node.children:
        verdict, reason = _eval_node(child, root)
        if verdict is _Verdict.ALLOW:
            return _Verdict.ALLOW, None
        if verdict is _Verdict.UNKNOWN:
            if unknown_reason is None:
                unknown_reason = reason
        else:
            block_reasons.append(reason or "condition not met")
    if unknown_reason is not None:
        return _Verdict.UNKNOWN, unknown_reason
    return _Verdict.BLOCK, "no alternative satisfied: " + "; ".join(block_reasons)


def _eval_exists(node: _Exists, root: Mapping[str, Any]) -> tuple[_Verdict, str | None]:
    # The only operator with a concrete present/absent verdict (never UNKNOWN),
    # so authors can deliberately allow-on-missing via not(exists(x)).
    if _resolve_path(node.path, root) is MISSING:
        return _Verdict.BLOCK, f"path {'.'.join(node.path)!r} does not exist"
    return _Verdict.ALLOW, None


def _eval_forbid_tool(node: _ForbidTool, root: Mapping[str, Any]) -> tuple[_Verdict, str | None]:
    tool_id = root.get("tool_id") if isinstance(root, Mapping) else None
    if not isinstance(tool_id, str):
        # Malformed input to the pre-dispatch tool gate. Fail closed (UNKNOWN
        # collapses to block) rather than miss the forbidden set and allow the
        # handler to run. Guarding the type also avoids hashing an unhashable id.
        return _Verdict.UNKNOWN, f"tool_gate: tool_id is not a string ({type(tool_id).__name__})"
    if tool_id in node.tool_ids:
        return _Verdict.BLOCK, f"tool {tool_id!r} is forbidden"
    return _Verdict.ALLOW, None


def _eval_comparison(node: _Comparison, root: Mapping[str, Any]) -> tuple[_Verdict, str | None]:
    actual = _resolve_path(node.path, root)
    dotted = ".".join(node.path)
    if actual is MISSING:
        return _Verdict.UNKNOWN, f"path {dotted!r} is missing"
    op = node.op
    try:
        if op == "in":
            if any(_strict_eq(actual, candidate) for candidate in node.value):
                return _Verdict.ALLOW, None
            return (
                _Verdict.BLOCK,
                f"path {dotted!r} value {_shown(actual)} not in {list(node.value)!r}",
            )
        if op == "==":
            ok = _strict_eq(actual, node.value)
        elif op == "!=":
            ok = not _strict_eq(actual, node.value)
        else:  # numeric: < <= > >= (stored value is numeric — enforced at parse)
            if not _is_number(actual):
                return (
                    _Verdict.UNKNOWN,
                    f"path {dotted!r} value {_shown(actual)} is not numeric for {op!r}",
                )
            ok = _NUMERIC_CMP[op](actual, node.value)
    except Exception:
        # A value whose __eq__/comparison raises is malformed runtime data; the
        # evaluator must never raise on data shape, so it is undecidable → UNKNOWN
        # (which fails closed at the gate).
        return (
            _Verdict.UNKNOWN,
            f"path {dotted!r} value {_shown(actual)} could not be compared with {op!r}",
        )
    if ok:
        return _Verdict.ALLOW, None
    return _Verdict.BLOCK, f"path {dotted!r} value {_shown(actual)} fails {op} {node.value!r}"


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
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _shown(value: Any) -> str:
    """Render a value for a failure reason; ``repr`` is exception-guarded.

    Only called on the BLOCK/UNKNOWN path, and a value whose ``__repr__`` raises
    can never turn a verdict into an exception (the evaluator never raises on
    input data shape).
    """
    try:
        return repr(value)
    except Exception:
        return "<unrepresentable>"

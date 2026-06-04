"""Rule enforcement for the Agent Cognition Core — pure, DB-free.

Turns a set of already-fetched :class:`~agent_cognition.models.Rule` rows into
deterministic allow/block decisions and an advisory prompt block. The DB fetch
lives in :mod:`agent_cognition.rules.store` (and, later, the CognitiveContext
facade); keeping this module pure makes the enforcement logic fully unit-testable
without Postgres and free of import side effects.

Three enforced-rule gates and one advisory renderer:

* :func:`build_rule_prompt_block` — render advisory rules into a prompt block the
  agent runtime injects into its system prompt (best-effort steering).
* :func:`evaluate_precondition` / :func:`evaluate_postcondition` /
  :func:`evaluate_tool_call` — evaluate the active *enforced* rules of the
  matching phase and return ``(allow, reason)``.

Uniform contract: a phase is allowed iff **every** applicable enforced predicate
holds; the returned ``reason`` is the first failing rule's text plus the
predicate's own reason. A stored predicate that fails to parse — **or that
declares no recognized phase**, so it belongs to no gate — makes its rule
**block** (fail closed) at every gate. Enforced rules are a safety boundary, so a
malformed one must never silently allow, even if it bypassed the store's
write-time validation.

Evaluation roots (see :mod:`agent_cognition.rules.predicate`):

* precondition — the ``ctx`` mapping is used as-is (the caller passes the
  namespaced shape ``{"input": ..., "agent_id": ...}``); paths read ``input.*``.
* postcondition — the agent result is wrapped as ``{"output": output}``; paths
  read ``output.*``.
* tool_gate — ``{"tool_id": tool_id, "args": args}``; ``forbid_tool`` reads
  ``tool_id`` and comparisons read ``args.*`` / ``tool_id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_cognition.models import Rule, RuleMode, RuleStatus
from agent_cognition.rules.predicate import (
    PredicateError,
    evaluate,
    is_valid_predicate,
    parse_predicate,
    validate_predicate,
)

__all__ = [
    "build_rule_prompt_block",
    "evaluate_precondition",
    "evaluate_postcondition",
    "evaluate_tool_call",
    "is_valid_predicate",
    "validate_predicate",
    "PredicateError",
]

_PROMPT_HEADER = "## Operating rules"

# The phases a predicate may declare; an active enforced rule whose predicate
# declares none of these is un-enforceable and fails closed (see _evaluate_phase).
_KNOWN_PHASES = frozenset({"precondition", "postcondition", "tool_gate"})


def build_rule_prompt_block(advisory_rules: list[Rule]) -> str:
    """Render active advisory rules into a deterministic prompt block.

    Preconditions:
        * ``advisory_rules`` is a list of :class:`Rule`. Non-advisory or
          non-active rules are filtered out defensively (the function never
          renders an enforced or retired rule).
    Postconditions:
        * Returns a ``## Operating rules`` block with one ``- <text>`` line per
          active advisory rule, ordered ``(priority DESC, id ASC)``, each with an
          optional ``(rationale: …)`` suffix. Returns ``""`` when no advisory rule
          applies, so the injector can omit the section. Pure — no side effects.
    """
    ordered = sorted(
        (
            r
            for r in advisory_rules
            if r.mode == RuleMode.ADVISORY and r.status == RuleStatus.ACTIVE
        ),
        key=lambda r: (-r.priority, r.id),
    )
    if not ordered:
        return ""
    return _PROMPT_HEADER + "\n" + "\n".join(_render_rule_line(r) for r in ordered)


def _render_rule_line(rule: Rule) -> str:
    suffix = f" (rationale: {rule.rationale})" if rule.rationale else ""
    return f"- {rule.text}{suffix}"


def evaluate_precondition(ctx: Mapping[str, Any], rules: list[Rule]) -> tuple[bool, str | None]:
    """Evaluate active enforced precondition rules against the invoke ``ctx``.

    Preconditions:
        * ``ctx`` is the namespaced precondition root (``{"input": ...,
          "agent_id": ...}``); ``rules`` are candidate rules (any phase/mode — the
          function filters).
    Postconditions:
        * Returns ``(True, None)`` iff every active enforced ``precondition`` rule
          holds; otherwise ``(False, "<rule.text>: <reason>")`` for the first
          failing rule in ``(priority DESC, id ASC)`` order. A rule whose stored
          predicate fails to parse fails **closed** (blocks).
    """
    return _evaluate_phase(rules, "precondition", ctx)


def evaluate_postcondition(output: Mapping[str, Any], rules: list[Rule]) -> tuple[bool, str | None]:
    """Evaluate active enforced postcondition rules against an agent ``output``.

    Preconditions:
        * ``output`` is the agent's result mapping; ``rules`` are candidate rules.
    Postconditions:
        * Same allow/block contract as :func:`evaluate_precondition`, against the
          root ``{"output": output}`` for the ``postcondition`` phase.
    """
    return _evaluate_phase(rules, "postcondition", {"output": output})


def evaluate_tool_call(
    tool_id: str, args: Mapping[str, Any], rules: list[Rule]
) -> tuple[bool, str | None]:
    """Pre-dispatch gate: may ``tool_id`` be called with ``args``?

    This is the gate the tools broker consults **before** dispatching a handler,
    so a forbidden tool never runs its side effect.

    Preconditions:
        * ``tool_id`` is non-empty; ``args`` is a mapping; ``rules`` are candidate
          rules.
    Postconditions:
        * Returns ``(True, None)`` iff every active enforced ``tool_gate`` rule
          holds for the root ``{"tool_id": tool_id, "args": args}``; otherwise
          ``(False, reason)`` naming the first failing rule. Fails closed on a
          malformed predicate.
    """
    assert tool_id, "evaluate_tool_call: tool_id must be non-empty"
    return _evaluate_phase(rules, "tool_gate", {"tool_id": tool_id, "args": args})


def _evaluate_phase(
    rules: list[Rule], phase: str, root: Mapping[str, Any]
) -> tuple[bool, str | None]:
    """Allow iff every active enforced rule of ``phase`` holds against ``root``.

    Defense in depth: an active enforced rule whose predicate declares **no
    recognized phase** (e.g. an empty/`{}` or otherwise malformed predicate)
    belongs to no gate, so dropping it would silently un-enforce a safety rule.
    Such a rule fails **closed** at every gate — the store validates enforced
    predicates on write, but the boundary must not trust that for a row that was
    inserted directly or predates the validation.
    """
    applicable: list[Rule] = []
    for rule in rules:
        if rule.mode != RuleMode.ENFORCED or rule.status != RuleStatus.ACTIVE:
            continue
        rule_phase = rule.predicate.get("phase") if isinstance(rule.predicate, dict) else None
        if rule_phase == phase:
            applicable.append(rule)
        elif rule_phase not in _KNOWN_PHASES:
            return False, f"{rule.text}: enforced rule has no enforceable phase ({rule_phase!r})"
    applicable.sort(key=lambda r: (-r.priority, r.id))
    for rule in applicable:
        try:
            pred = parse_predicate(rule.predicate)
        except PredicateError as exc:
            # Fail closed: a malformed enforced predicate blocks rather than allows.
            return False, f"{rule.text}: invalid predicate ({exc})"
        holds, reason = evaluate(pred, root)
        if not holds:
            return False, f"{rule.text}: {reason}" if reason else rule.text
    return True, None

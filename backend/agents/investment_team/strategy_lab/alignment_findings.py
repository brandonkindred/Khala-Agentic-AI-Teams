"""Lightweight shared models for the deterministic trade-alignment gate.

Lives at the ``strategy_lab`` package level (not inside
``quality_gates/``) so :class:`AlignmentFinding` can be imported from
``investment_team.models`` without triggering ``quality_gates/__init__``,
whose eager imports of :class:`AcceptanceGate` and friends would loop
back into ``investment_team.models`` mid-construction.

The deterministic gate, the trade-alignment agent, and
:class:`BacktestRecord` all import the types from here.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel

Severity = Literal["info", "warning", "critical"]

_ENTRY_RULE_ID_RE = re.compile(r"^entry\[(\d+)]$")
_SIGNAL_EXIT_RULE_ID_RE = re.compile(r"^exit:signal_exit\[(\d+)]$")


def entry_rule_id(rule_index: int) -> str:
    """``AlignmentFinding.rule_id`` for an ``EntryRule`` at ``rule_index``.

    Preconditions: ``rule_index`` is the rule's absolute index in
    ``spec.entry_rules`` (never a position in a filtered subset).
    Postconditions: returns ``f"entry[{rule_index}]"`` — the single
    format string every producer/consumer of entry-rule findings must
    share (``DeterministicAlignmentChecker`` produces it,
    ``RuleFiringRateGate`` parses it via :func:`parse_entry_rule_id`).
    """
    assert rule_index >= 0, "rule_index must be a valid spec.entry_rules index"
    return f"entry[{rule_index}]"


def signal_exit_rule_id(rule_index: int) -> str:
    """``AlignmentFinding.rule_id`` for a ``SignalExitRule`` at ``rule_index``.

    Preconditions: ``rule_index`` is the rule's absolute index in
    ``spec.exit_rules`` (never a position in the kind-filtered subset of
    just the ``SignalExitRule``s) — this matches the engine's own
    ``engine_exit:signal_exit[N]`` stamp (``trading_service/service.py``),
    which also indexes off the unfiltered ``exit_rules`` list.
    Postconditions: returns ``f"exit:signal_exit[{rule_index}]"``, the
    shared format both ``DeterministicAlignmentChecker`` (producer) and
    ``RuleFiringRateGate`` (consumer, via :func:`parse_signal_exit_rule_id`)
    must agree on.
    """
    assert rule_index >= 0, "rule_index must be a valid spec.exit_rules index"
    return f"exit:signal_exit[{rule_index}]"


def parse_entry_rule_id(rule_id: Optional[str]) -> Optional[int]:
    """Inverse of :func:`entry_rule_id`.

    Postconditions: returns the rule index for a ``rule_id`` produced by
    :func:`entry_rule_id`; ``None`` for ``rule_id=None`` or any string
    that isn't an exact match (e.g. ``"entry:side_mismatch"``).
    """
    if not rule_id:
        return None
    m = _ENTRY_RULE_ID_RE.match(rule_id)
    return int(m.group(1)) if m else None


def parse_signal_exit_rule_id(rule_id: Optional[str]) -> Optional[int]:
    """Inverse of :func:`signal_exit_rule_id`.

    Postconditions: returns the rule index for a ``rule_id`` produced by
    :func:`signal_exit_rule_id`; ``None`` for ``rule_id=None`` or any
    string that isn't an exact match (e.g. the unindexed
    ``"exit:signal_exit"`` skip/critical marker).
    """
    if not rule_id:
        return None
    m = _SIGNAL_EXIT_RULE_ID_RE.match(rule_id)
    return int(m.group(1)) if m else None


class AlignmentFinding(BaseModel):
    """One row in the per-trade, per-rule alignment ledger.

    A trade produces zero-or-more findings (one per check the
    deterministic gate ran against it). The aggregate ``aligned``
    verdict is the conjunction of every ``passed`` flag whose
    ``severity == "critical"`` — ``info`` / ``warning`` findings are
    diagnostic only.

    Contract:
      - ``trade_num`` indexes into the trade ledger (1-based, mirrors
        :class:`TradeRecord.trade_num`).
      - ``rule_id`` is an optional human-readable handle for the spec
        rule the check tested (e.g. ``"entry[0]"``,
        ``"exit:stop_loss"``, ``"sizing"``). ``None`` for checks that
        test trade-level invariants rather than a specific rule.
      - ``check_name`` is one of: ``"universe"``, ``"side"``,
        ``"sizing"``, ``"stop_loss"``, ``"take_profit"``,
        ``"signal_exit"``, ``"entry_signal"``.
      - ``computed_value`` / ``expected_value`` are populated by
        numeric checks so the UI can render "got X, expected ±tol of Y".
    """

    trade_num: int
    rule_id: Optional[str] = None
    check_name: str
    passed: bool
    severity: Severity = "critical"
    details: str = ""
    computed_value: Optional[float] = None
    expected_value: Optional[float] = None


class NearMissVerdict(BaseModel):
    """Narrow LLM adjudication of a tight entry-signal predicate miss.

    The deterministic gate evaluates predicates strictly. When a miss
    is within ``STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT`` (default 1%
    relative), the gate consults a single-shot LLM with a focused
    yes/no prompt: was the signal fire legitimate?
    """

    legitimate: bool
    rationale: str = ""

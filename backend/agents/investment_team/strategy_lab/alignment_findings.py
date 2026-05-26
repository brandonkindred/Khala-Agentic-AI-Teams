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

from typing import Literal, Optional

from pydantic import BaseModel

Severity = Literal["info", "warning", "critical"]


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

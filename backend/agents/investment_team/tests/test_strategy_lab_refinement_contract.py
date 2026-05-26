"""Refinement contract narrowing (#547 item 2, extended in #543).

After #547, the refinement agent's output is code-only. Even if a
stubbed agent returns spec-mutating keys (legacy schema or
hallucinated fields), ``StrategyLabOrchestrator._apply_updates`` must
not merge them into the spec — only ``strategy_code`` is updated.

#543 strengthens the contract: stray spec-mutating keys now log a
warning, ``risk_limits`` is the lone exception with tighten-only
semantics, and ``_apply_updates`` is an instance method.
"""

from __future__ import annotations

import logging

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.exceptions import SpecImplementabilityError
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-refine-contract",
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI mean reversion",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            )
        ],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        strategy_code="# original code",
    )


def test_apply_updates_swaps_code_only(caplog: pytest.LogCaptureFixture) -> None:
    """Spec fields stay untouched even when the refinement dict requests changes."""
    original = _spec()
    legacy_updates = {
        "entry_rules": ["should be ignored — refinement cannot mutate spec"],
        "exit_rules": ["should be ignored"],
        "sizing_rules": ["should be ignored"],
        "hypothesis": "rewritten hypothesis",
        "changes_made": "tightened RSI guard",
    }

    orch = StrategyLabOrchestrator()
    with caplog.at_level(logging.WARNING, logger="investment_team.strategy_lab.orchestrator"):
        result = orch._apply_updates(original, legacy_updates, "# refined code")

    assert result.entry_rules == original.entry_rules
    assert result.exit_rules == original.exit_rules
    assert result.sizing == original.sizing
    assert result.hypothesis == original.hypothesis
    assert result.risk_limits == original.risk_limits
    assert result.strategy_code == "# refined code"
    assert any("Refinement discarded spec-mutating keys" in rec.message for rec in caplog.records)


def test_apply_updates_with_empty_dict_only_swaps_code() -> None:
    """Empty ``updates`` (the zero-trade-repair path) still swaps code."""
    original = _spec()
    orch = StrategyLabOrchestrator()
    result = orch._apply_updates(original, {}, "# repaired code")
    assert result.strategy_code == "# repaired code"
    assert result.entry_rules == original.entry_rules


def test_apply_updates_raises_on_risk_limits_loosening() -> None:
    """A loosening risk_limits proposal trips ``SpecImplementabilityError``."""
    original = _spec()
    orch = StrategyLabOrchestrator()
    with pytest.raises(SpecImplementabilityError) as exc_info:
        orch._apply_updates(
            original,
            {"risk_limits": {"max_position_pct": 99}, "changes_made": "loosen sizing"},
            "# refined code",
            failure_phase="execution",
        )
    assert "loosen" in exc_info.value.evidence
    assert exc_info.value.failure_phase == "execution"
    assert exc_info.value.last_spec is original
    assert exc_info.value.last_code == "# refined code"

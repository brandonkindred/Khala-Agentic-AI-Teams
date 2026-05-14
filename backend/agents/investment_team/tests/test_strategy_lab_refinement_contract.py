"""Refinement contract narrowing (#547 item 2).

After #547, the refinement agent's output is code-only. Even if a
stubbed agent returns spec-mutating keys (legacy schema or
hallucinated fields), ``StrategyLabOrchestrator._apply_updates`` must
not merge them into the spec — only ``strategy_code`` is updated.
"""

from __future__ import annotations

from investment_team.models import StrategySpec
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-refine-contract",
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI mean reversion",
        signal_definition="sig",
        entry_rules=["enter when RSI < 30"],
        exit_rules=["exit when RSI > 70"],
        sizing_rules=["risk 2% per trade"],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        strategy_code="# original code",
    )


def test_apply_updates_swaps_code_only() -> None:
    """Spec fields stay untouched even when the refinement dict requests changes."""
    original = _spec()
    legacy_updates = {
        "entry_rules": ["should be ignored — refinement cannot mutate spec"],
        "exit_rules": ["should be ignored"],
        "sizing_rules": ["should be ignored"],
        "risk_limits": {"max_position_pct": 99},
        "hypothesis": "rewritten hypothesis",
        "changes_made": "tightened RSI guard",
    }

    result = StrategyLabOrchestrator._apply_updates(original, legacy_updates, "# refined code")

    assert result.entry_rules == original.entry_rules
    assert result.exit_rules == original.exit_rules
    assert result.sizing_rules == original.sizing_rules
    assert result.hypothesis == original.hypothesis
    assert result.risk_limits == original.risk_limits
    assert result.strategy_code == "# refined code"


def test_apply_updates_with_empty_dict_only_swaps_code() -> None:
    """Empty ``updates`` (the zero-trade-repair path) still swaps code."""
    original = _spec()
    result = StrategyLabOrchestrator._apply_updates(original, {}, "# repaired code")
    assert result.strategy_code == "# repaired code"
    assert result.entry_rules == original.entry_rules

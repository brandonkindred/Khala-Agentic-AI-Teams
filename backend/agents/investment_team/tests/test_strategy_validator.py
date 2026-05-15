"""Unit tests for ``StrategySpecValidator`` (#547 item 6).

The gate runs deterministic checks against a ``StrategySpec`` before
code execution. These tests focus on the hypothesis-vs-rules
consistency check added in #547 — every other check has implicit
coverage via the orchestrator integration tests.
"""

from __future__ import annotations

from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.strategy_validator import (
    StrategySpecValidator,
)


def _spec(*, hypothesis: str, entry: list[str], exit_: list[str]) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-validator-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis=hypothesis,
        signal_definition="sig",
        entry_rules=entry,
        exit_rules=exit_,
        sizing_rules=["risk 2% per trade"],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        strategy_code="from contract import Strategy\nclass S(Strategy):\n    def on_bar(self, ctx, bar):\n        pass\n",
    )


def _warnings(results) -> list[str]:
    return [r.details for r in results if r.severity == "warning" and not r.passed]


def test_hypothesis_term_missing_from_rules_emits_warning() -> None:
    """Hypothesis names RSI; rules never reference it → consistency warning."""
    spec = _spec(
        hypothesis="RSI mean reversion on oversold conditions",
        entry=["enter on volume spike above 2x average"],
        exit_=["exit after 5 bars"],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("Hypothesis/rules consistency" in w and "rsi" in w.lower() for w in warnings), (
        warnings
    )


def test_rules_term_missing_from_hypothesis_emits_warning() -> None:
    """Rules reference MACD; hypothesis doesn't → consistency warning."""
    spec = _spec(
        hypothesis="Catch short-term trend continuation",
        entry=["enter when MACD crosses above signal line"],
        exit_=["exit when MACD crosses below signal line"],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert any("Hypothesis/rules consistency" in w and "macd" in w.lower() for w in warnings), (
        warnings
    )


def test_aligned_hypothesis_and_rules_emit_no_consistency_warning() -> None:
    """When hypothesis and rules share concept vocabulary, no warning fires."""
    spec = _spec(
        hypothesis="RSI signal strategy",
        entry=["enter when RSI < 30"],
        exit_=["exit when RSI > 70"],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings


def test_no_recognised_terms_emits_no_consistency_warning() -> None:
    """Hypothesis and rules without any recognised vocabulary stay silent."""
    spec = _spec(
        hypothesis="Buy low, sell high on a fixed schedule",
        entry=["enter every Monday open"],
        exit_=["exit every Friday close"],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings


def test_word_boundary_prevents_thematic_matching_ema() -> None:
    """The compiled regex uses ``\\b`` anchors so 'thematic' does not match 'ema'."""
    spec = _spec(
        hypothesis="Thematic exposure to growth sectors",
        entry=["enter on quarterly rebalance into themes"],
        exit_=["exit on rebalance day"],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings

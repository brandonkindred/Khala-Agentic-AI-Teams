"""Unit tests for ``StrategySpecValidator`` (#547 item 6).

The gate runs deterministic checks against a ``StrategySpec`` before
code execution. These tests focus on the hypothesis-vs-rules
consistency check added in #547 — every other check has implicit
coverage via the orchestrator integration tests.
"""

from __future__ import annotations

from typing import List

from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.strategy_validator import (
    StrategySpecValidator,
)
from investment_team.strategy_lab.spec_dsl import (
    ConstRef,
    EntryRule,
    MACDRef,
    Predicate,
    PriceRef,
    RSIRef,
    SignalExitRule,
    SMARef,
    TimeStopRule,
)


def _spec(*, hypothesis: str, entry: List, exit_: List) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-validator-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis=hypothesis,
        signal_definition="sig",
        entry_rules=entry,
        exit_rules=exit_,
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        strategy_code="from contract import Strategy\nclass S(Strategy):\n    def on_bar(self, ctx, bar):\n        pass\n",
    )


def _warnings(results) -> list[str]:
    return [r.details for r in results if r.severity == "warning" and not r.passed]


def test_hypothesis_term_missing_from_rules_emits_warning() -> None:
    """Hypothesis names RSI; rules use SMA → consistency warning fires.

    Issue #551/#552: rule consistency now reads through
    ``format_rules_for_prompt``; structured SMARef formats to ``sma(20)``,
    which does not contain ``rsi``.
    """
    spec = _spec(
        hypothesis="RSI mean reversion on oversold conditions",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=SMARef(period=20)),
            ),
        ],
        exit_=[TimeStopRule(n_bars=5)],
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
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=MACDRef(), op="gt", rhs=ConstRef(value=0)),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(lhs=MACDRef(), op="lt", rhs=ConstRef(value=0)),
            ),
        ],
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
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=RSIRef(period=14), op="lt", rhs=ConstRef(value=30)),
            ),
        ],
        exit_=[
            SignalExitRule(
                when=Predicate(lhs=RSIRef(period=14), op="gt", rhs=ConstRef(value=70)),
            ),
        ],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings


def test_no_recognised_terms_emits_no_consistency_warning() -> None:
    """Hypothesis and rules without any recognised vocabulary stay silent.

    ``EntryRule`` with bare ``PriceRef`` / ``ConstRef`` formats to text like
    ``long when close > 0`` — no concept tokens.
    """
    spec = _spec(
        hypothesis="Buy low, sell high on a fixed schedule",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=ConstRef(value=0)),
            ),
        ],
        exit_=[TimeStopRule(n_bars=5)],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings


def test_word_boundary_prevents_thematic_matching_ema() -> None:
    """The compiled regex uses ``\\b`` anchors so 'thematic' does not match 'ema'."""
    spec = _spec(
        hypothesis="Thematic exposure to growth sectors",
        entry=[
            EntryRule(
                side="long",
                when=Predicate(lhs=PriceRef(), op="gt", rhs=ConstRef(value=0)),
            ),
        ],
        exit_=[TimeStopRule(n_bars=5)],
    )
    results = StrategySpecValidator().validate(spec)
    warnings = _warnings(results)
    assert not any("Hypothesis/rules consistency" in w for w in warnings), warnings

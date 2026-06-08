"""Unit tests for ``SpecReadinessGate`` Rule 10 — predicate reachability.

Two defect classes are decidable in closed form without market data:

  (a) a bounded indicator (RSI/ADX/Stochastic, all 0–100) compared against an
      out-of-range constant — always-false (dead rule) or always-true (vacuous);
  (b) a predicate whose two sides are the same reference — contradiction
      (``x < x``) or tautology (``x <= x``), incl. identical-ref ``cross_*``.

The classifier helpers are tested directly for full branch coverage, and the
gate is exercised end-to-end to confirm severity routing (critical for an
always-false ENTRY predicate, warning for a SIGNAL-EXIT predicate or any
vacuous predicate).
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from investment_team.models import BacktestConfig, StrategySpec
from investment_team.strategy_lab.quality_gates.spec_readiness import (
    SpecReadinessGate,
    _bounded_indicator_verdict,
    _classify_predicate,
    _identical_ref_verdict,
    _ref_identity,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)


def _pred(lhs, op: str, rhs) -> Predicate:
    return Predicate(lhs=lhs, op=op, rhs=rhs)


def _rsi(period: int = 14) -> IndicatorRef:
    return IndicatorRef(name="rsi", params={"period": period})


# ---------------------------------------------------------------------------
# (a) Bounded indicator vs out-of-range constant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op,const,expected",
    [
        (">", 100.0, "false"),   # rsi can equal 100 but never exceed it
        (">", 99.0, None),       # reachable (99 < rsi <= 100)
        (">=", 100.0, None),     # reachable (rsi can hit 100)
        (">=", 101.0, "false"),  # rsi never reaches 101
        ("<", 0.0, "false"),     # rsi never below 0
        ("<", 1.0, None),        # reachable (rsi can be 0 < 1)
        ("<=", 0.0, None),       # reachable (rsi can be 0)
        ("<=", -1.0, "false"),   # rsi never <= -1
        ("<", 101.0, "true"),    # every rsi value < 101
        (">", -1.0, "true"),     # every rsi value > -1
        ("<=", 100.0, "true"),   # every rsi value <= 100
        (">=", 0.0, "true"),     # every rsi value >= 0
        ("==", 150.0, "false"),  # equality to an out-of-range value
        ("==", 50.0, None),      # reachable
        ("==", -5.0, "false"),
    ],
)
def test_bounded_rsi_vs_constant(op: str, const: float, expected: Optional[str]) -> None:
    verdict = _bounded_indicator_verdict(_pred(_rsi(), op, const))
    if expected is None:
        assert verdict is None
    else:
        assert verdict is not None and verdict[0] == expected, verdict


def test_bounded_cross_above_out_of_range_is_unreachable() -> None:
    assert _bounded_indicator_verdict(_pred(_rsi(), "cross_above", 150.0))[0] == "false"
    assert _bounded_indicator_verdict(_pred(_rsi(), "cross_below", -5.0))[0] == "false"


def test_bounded_cross_inside_range_is_reachable() -> None:
    assert _bounded_indicator_verdict(_pred(_rsi(), "cross_above", 70.0)) is None
    assert _bounded_indicator_verdict(_pred(_rsi(), "cross_below", 30.0)) is None


def test_adx_and_stochastic_are_bounded_too() -> None:
    assert _bounded_indicator_verdict(_pred(IndicatorRef(name="adx"), ">", 100.0))[0] == "false"
    assert _bounded_indicator_verdict(_pred(IndicatorRef(name="stochastic"), ">", 100.0))[0] == "false"


def test_unbounded_indicator_is_undecidable() -> None:
    # SMA / EMA / MACD / ATR / VWAP / Bollinger are price-scaled — no constant
    # is trivially out of range, so the rule abstains.
    assert _bounded_indicator_verdict(_pred(IndicatorRef(name="sma", params={"period": 20}), ">", 1e9)) is None
    assert _bounded_indicator_verdict(_pred(IndicatorRef(name="macd"), "<", -1e9)) is None


def test_bounded_vs_non_constant_rhs_is_undecidable() -> None:
    # rsi vs a price ref or another indicator is data-dependent.
    assert _bounded_indicator_verdict(_pred(_rsi(), ">", "bar.close")) is None
    assert _bounded_indicator_verdict(_pred(_rsi(), ">", IndicatorRef(name="adx"))) is None


def test_price_ref_lhs_is_not_a_bounded_indicator() -> None:
    assert _bounded_indicator_verdict(_pred("bar.close", ">", 100.0)) is None


# ---------------------------------------------------------------------------
# (b) Identical-reference tautology / contradiction
# ---------------------------------------------------------------------------


def test_ref_identity_keys() -> None:
    assert _ref_identity("bar.close") == "price:bar.close"
    assert _ref_identity(_rsi()) == f"ind:{_rsi().sig_id}"
    assert _ref_identity(30.0) is None  # a float constant is not a reference


@pytest.mark.parametrize(
    "op,expected",
    [
        ("<", "false"),
        (">", "false"),
        ("<=", "true"),
        (">=", "true"),
        ("==", "true"),
        ("cross_above", "false"),
        ("cross_below", "false"),
    ],
)
def test_identical_price_ref(op: str, expected: str) -> None:
    assert _identical_ref_verdict(_pred("bar.close", op, "bar.close"))[0] == expected


def test_identical_indicator_ref_tautology_and_contradiction() -> None:
    assert _identical_ref_verdict(_pred(_rsi(), ">=", _rsi()))[0] == "true"
    assert _identical_ref_verdict(_pred(_rsi(), "<", _rsi()))[0] == "false"


def test_distinct_refs_are_not_identical() -> None:
    assert _identical_ref_verdict(_pred("bar.close", "<", "bar.high")) is None
    assert _identical_ref_verdict(_pred(_rsi(14), ">", _rsi(21))) is None
    assert _identical_ref_verdict(_pred(_rsi(), ">", 30.0)) is None  # rhs is a float


# ---------------------------------------------------------------------------
# _classify_predicate dispatch (identical-ref check takes precedence)
# ---------------------------------------------------------------------------


def test_classify_prefers_identical_ref_over_bounds() -> None:
    # rsi == rsi is a tautology; the identical-ref check fires before the
    # bounded-indicator check would even apply (rhs is not a constant).
    assert _classify_predicate(_pred(_rsi(), "==", _rsi()))[0] == "true"


def test_classify_reachable_predicate_is_none() -> None:
    assert _classify_predicate(_pred(_rsi(), "<", 30.0)) is None


# ---------------------------------------------------------------------------
# Gate-level routing
# ---------------------------------------------------------------------------


def _spec(*, entry: List, exit_: List) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-reachability-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI momentum on AAPL.",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=entry,
        exit_rules=exit_,
        target_symbols=["AAPL"],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        strategy_code="from contract import Strategy\nclass S(Strategy):\n    def on_bar(self, ctx, bar):\n        pass\n",
    )


def _reachable_exit() -> SignalExitRule:
    return SignalExitRule(when=_pred(_rsi(), ">", 70.0))


def _results_with_rule_id(results, rule_id: str):
    return [r for r in results if getattr(r, "rule_id", None) == rule_id]


def test_unreachable_entry_predicate_is_critical() -> None:
    spec = _spec(
        entry=[EntryRule(side="long", when=_pred(_rsi(), ">", 100.0))],
        exit_=[_reachable_exit()],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    flagged = _results_with_rule_id(results, "predicate:unreachable")
    assert flagged, "expected a predicate:unreachable result"
    assert any(r.severity == "critical" and not r.passed for r in flagged), [
        (r.severity, r.details) for r in flagged
    ]


def test_unreachable_signal_exit_is_warning_not_critical() -> None:
    spec = _spec(
        entry=[EntryRule(side="long", when=_pred(_rsi(), "<", 30.0))],
        exit_=[SignalExitRule(when=_pred(_rsi(), ">", 100.0))],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    flagged = _results_with_rule_id(results, "predicate:unreachable")
    assert flagged
    assert all(r.severity == "warning" for r in flagged), [(r.severity, r.details) for r in flagged]
    # And no critical anywhere keyed to the reachability rule.
    assert not any(
        getattr(r, "rule_id", None) == "predicate:unreachable" and r.severity == "critical"
        for r in results
    )


def test_vacuous_entry_predicate_is_warning() -> None:
    spec = _spec(
        entry=[EntryRule(side="long", when=_pred(_rsi(), "<", 101.0))],
        exit_=[_reachable_exit()],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    flagged = _results_with_rule_id(results, "predicate:vacuous")
    assert flagged
    assert all(r.severity == "warning" for r in flagged)


def test_reachable_spec_emits_no_reachability_findings() -> None:
    spec = _spec(
        entry=[EntryRule(side="long", when=_pred(_rsi(), "<", 30.0))],
        exit_=[_reachable_exit()],
    )
    results = SpecReadinessGate().validate(spec, backtest_config=_config())
    assert not _results_with_rule_id(results, "predicate:unreachable")
    assert not _results_with_rule_id(results, "predicate:vacuous")


def _config() -> BacktestConfig:
    return BacktestConfig(start_date="2024-01-01", end_date="2024-06-01")

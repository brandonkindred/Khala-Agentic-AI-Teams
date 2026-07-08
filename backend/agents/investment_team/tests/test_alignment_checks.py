"""Tests for the deterministic ``DeterministicAlignmentChecker``.

Hand-built ``StrategySpec`` + ``TradeRecord`` fixtures exercise each of
the seven checks in isolation, with at least one positive and one
negative case per check. The acceptance scenarios from the issue are
kept distinct: a wrong-universe trade fails check #1 without
constructing the near-miss LLM, and a tight ``rsi`` near-miss routes
to the LLM adjudicator.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from investment_team.market_data_service import OHLCVBar
from investment_team.models import StrategySpec, TradeRecord
from investment_team.strategy_lab.alignment_findings import (
    NearMissVerdict,
)
from investment_team.strategy_lab.quality_gates.alignment_checks import (
    DeterministicAlignmentChecker,
)
from investment_team.strategy_lab.spec_dsl import (
    DEFAULT_SIZING_PAYLOAD,
    AllOf,
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    VolatilityTargetSizing,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _spec(
    *,
    entry_rules: Optional[List[EntryRule]] = None,
    exit_rules: Optional[List[Any]] = None,
    target_symbols: Optional[List[str]] = None,
    sizing: Optional[Any] = None,
) -> StrategySpec:
    return StrategySpec(
        strategy_id="t-1",
        authored_by="test",
        asset_class="stocks",
        hypothesis="rsi oversold reversal",
        signal_definition="rsi(14) < 30",
        timeframe="1d",
        entry_rules=list(entry_rules or [_rsi_lt_30()]),
        exit_rules=list(exit_rules or []),
        sizing=sizing if sizing is not None else DEFAULT_SIZING_PAYLOAD,
        target_symbols=list(target_symbols or []),
    )


def _rsi_lt_30(side: str = "long", rhs: float = 30.0) -> EntryRule:
    return EntryRule(
        side=side,
        when=Predicate(
            lhs=IndicatorRef(name="rsi", params={"period": 14}),
            op="<",
            rhs=rhs,
        ),
    )


def _trade(
    *,
    trade_num: int = 1,
    symbol: str = "AAPL",
    side: str = "long",
    entry_date: str = "2023-02-01",
    exit_date: str = "2023-02-10",
    entry_price: float = 100.0,
    exit_price: float = 102.0,
    shares: float = 100.0,
    position_value: Optional[float] = None,
    return_pct: float = 2.0,
    net_pnl: float = 200.0,
    exit_reason: Optional[str] = None,
    participation_clipped: Optional[bool] = None,
    partial_fill_count: Optional[int] = None,
) -> TradeRecord:
    pv = position_value if position_value is not None else entry_price * shares
    return TradeRecord(
        trade_num=trade_num,
        entry_date=entry_date,
        exit_date=exit_date,
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        exit_price=exit_price,
        shares=shares,
        position_value=pv,
        gross_pnl=net_pnl,
        net_pnl=net_pnl,
        return_pct=return_pct,
        hold_days=5,
        outcome="win" if net_pnl > 0 else "loss",
        cumulative_pnl=net_pnl,
        exit_reason=exit_reason,
        participation_clipped=participation_clipped,
        partial_fill_count=partial_fill_count,
    )


def _market_data_rsi_oversold(symbol: str = "AAPL") -> Dict[str, List[OHLCVBar]]:
    """OHLCV that drives the RSI below 30 at the entry-date bar.

    32 bars on a strict downtrend so RSI=14 reaches the oversold zone
    by the final bar. ``2023-02-01`` is the last bar — used as the
    entry date by check #7 fixtures.
    """
    bars: List[OHLCVBar] = []
    base = 200.0
    for i in range(32):
        close = base - i * 2.5  # straight-line down
        d = i + 1
        date = f"2023-01-{d:02d}" if d <= 31 else "2023-02-01"
        bars.append(
            OHLCVBar(
                date=date,
                open=close + 1.0,
                high=close + 1.5,
                low=close - 1.5,
                close=close,
                volume=1_000_000,
            )
        )
    return {symbol: bars}


def _market_data_rsi_overbought(symbol: str = "AAPL") -> Dict[str, List[OHLCVBar]]:
    """Symmetric uptrend so RSI ends well above 30 (around 70+)."""
    bars: List[OHLCVBar] = []
    base = 100.0
    for i in range(32):
        close = base + i * 2.5
        d = i + 1
        date = f"2023-01-{d:02d}" if d <= 31 else "2023-02-01"
        bars.append(
            OHLCVBar(
                date=date,
                open=close - 1.0,
                high=close + 1.5,
                low=close - 1.5,
                close=close,
                volume=1_000_000,
            )
        )
    return {symbol: bars}


def _market_data_rsi_at_threshold(symbol: str = "AAPL") -> Dict[str, List[OHLCVBar]]:
    """OHLCV crafted so RSI(14) lands at ~30.05 on the entry bar — a
    tight near-miss that exercises the 1% adjudication path.
    """
    bars: List[OHLCVBar] = []
    # First 22 bars: strict downtrend so RSI heads below 30.
    for i in range(22):
        close = 200.0 - i * 2.5
        d = i + 1
        bars.append(
            OHLCVBar(
                date=f"2023-01-{d:02d}",
                open=close + 1.0,
                high=close + 1.5,
                low=close - 1.5,
                close=close,
                volume=1_000_000,
            )
        )
    # Then 10 mild green bars to nudge RSI back toward the threshold.
    last_close = 200.0 - 21 * 2.5
    for i in range(10):
        last_close += 1.6  # modest up moves
        d = i + 23
        date = f"2023-01-{d:02d}" if d <= 31 else f"2023-02-{(d - 31):02d}"
        bars.append(
            OHLCVBar(
                date=date,
                open=last_close - 0.5,
                high=last_close + 0.5,
                low=last_close - 1.0,
                close=last_close,
                volume=1_000_000,
            )
        )
    return {symbol: bars}


_NEVER_CALLED = object()


def _counting_adjudicator():
    """Returns ``(record, callable)``. Each call appends kwargs to
    ``record["calls"]`` and returns the next scripted verdict.

    Used by check #7 tests to assert the LLM is / is not consulted.
    """
    record: Dict[str, Any] = {"calls": [], "scripted": []}

    def adjudicator(**kwargs) -> NearMissVerdict:
        record["calls"].append(dict(kwargs))
        if not record["scripted"]:
            return NearMissVerdict(legitimate=False, rationale="default-deny")
        return record["scripted"].pop(0)

    return record, adjudicator


# ---------------------------------------------------------------------------
# Check 1 — universe
# ---------------------------------------------------------------------------


def test_universe_passes_when_symbol_in_target_symbols() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(target_symbols=["AAPL"])
    trade = _trade(symbol="AAPL")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold("AAPL"),
        initial_capital=100_000.0,
        near_miss_adjudicator=None,
    )
    universe_finding = next(f for f in result.findings if f.check_name == "universe")
    assert universe_finding.passed is True
    assert universe_finding.severity == "info"


def test_universe_fails_when_symbol_not_in_target_symbols() -> None:
    """Issue acceptance: a TSLA trade against a spec pinned to SPY is
    flagged critical without instantiating the LLM adjudicator."""
    record, adjudicator = _counting_adjudicator()
    gate = DeterministicAlignmentChecker()
    spec = _spec(target_symbols=["SPY"])
    trade = _trade(symbol="TSLA")

    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold("TSLA"),
        initial_capital=100_000.0,
        near_miss_adjudicator=adjudicator,
    )

    universe_finding = next(f for f in result.findings if f.check_name == "universe")
    assert universe_finding.passed is False
    assert universe_finding.severity == "critical"
    assert "TSLA" in universe_finding.details
    assert result.aligned is False
    # Critical: LLM adjudicator is never consulted for a universe miss.
    assert record["calls"] == []


# ---------------------------------------------------------------------------
# Check 2 — side
# ---------------------------------------------------------------------------


def test_side_passes_when_trade_side_matches_entry_rule() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(entry_rules=[_rsi_lt_30(side="long")])
    trade = _trade(side="long")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    side_finding = next(f for f in result.findings if f.check_name == "side")
    assert side_finding.passed is True


def test_side_fails_when_trade_direction_diverges_from_spec() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(entry_rules=[_rsi_lt_30(side="long")])
    trade = _trade(side="short")  # spec only declares long entries
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    side_finding = next(f for f in result.findings if f.check_name == "side")
    assert side_finding.passed is False
    assert side_finding.severity == "critical"
    assert result.aligned is False


# ---------------------------------------------------------------------------
# Check 3 — sizing
# ---------------------------------------------------------------------------


def test_sizing_passes_when_position_value_within_one_percent() -> None:
    """FixedFractionSizing(0.02) on $100k equity → expected $2,000 ±1%."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(sizing=FixedFractionSizing(fraction=0.02))
    trade = _trade(position_value=2_010.0)  # 0.5% over — within tolerance
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sizing = next(f for f in result.findings if f.check_name == "sizing")
    assert sizing.passed is True
    assert sizing.severity == "info"


def test_sizing_critical_when_position_value_outside_tolerance_and_no_caveat() -> None:
    """``participation_clipped=False`` AND ``partial_fill_count=0`` is
    an explicit no-caveat annotation from the engine; an out-of-
    tolerance sizing miss then is a real misalignment → critical."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(sizing=FixedFractionSizing(fraction=0.02))
    trade = _trade(
        position_value=2_800.0,
        participation_clipped=False,
        partial_fill_count=0,
    )
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sizing = next(f for f in result.findings if f.check_name == "sizing")
    assert sizing.passed is False
    assert sizing.severity == "critical"
    assert sizing.expected_value == 2_000.0
    assert sizing.computed_value == 2_800.0


def test_sizing_warning_when_outside_tolerance_with_unknown_fill_metadata() -> None:
    """``participation_clipped=None`` / ``partial_fill_count=None`` means
    the engine hasn't annotated the trade — distinct from "annotated
    as no caveat". Sizing drift on an unannotated trade downgrades
    from critical to warning so legacy / pre-annotation trades don't
    trigger needless alignment-fix iterations. Regression for PR #613
    review."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(sizing=FixedFractionSizing(fraction=0.02))
    # Both fill fields default to None on _trade() — unknown metadata.
    trade = _trade(position_value=2_800.0)
    assert trade.participation_clipped is None
    assert trade.partial_fill_count is None
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sizing = next(f for f in result.findings if f.check_name == "sizing")
    assert sizing.passed is False
    assert sizing.severity == "warning"
    assert "execution metadata not annotated" in sizing.details
    # Critical aggregation: warning does NOT trip ``aligned=False``
    # — only critical does.
    assert result.aligned is True


def test_sizing_info_when_outside_tolerance_but_participation_clipped() -> None:
    """Execution caveat downgrades an out-of-tolerance sizing miss
    from critical to info — the engine legitimately clipped the
    realised position size."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(sizing=FixedFractionSizing(fraction=0.02))
    trade = _trade(position_value=1_400.0, participation_clipped=True)
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sizing = next(f for f in result.findings if f.check_name == "sizing")
    assert sizing.passed is True
    assert sizing.severity == "info"
    assert "participation_clipped" in sizing.details


def test_sizing_info_when_outside_tolerance_but_partial_fill_count() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(sizing=FixedFractionSizing(fraction=0.02))
    trade = _trade(position_value=1_400.0, partial_fill_count=3)
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sizing = next(f for f in result.findings if f.check_name == "sizing")
    assert sizing.passed is True
    assert sizing.severity == "info"
    assert "partial_fill_count" in sizing.details


def test_sizing_skipped_for_volatility_target_variant() -> None:
    """VolatilityTargetSizing can't be reproduced at trade-level — the
    gate emits an info skip rather than a false positive critical."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(sizing=VolatilityTargetSizing(target_annual_vol=0.15))
    trade = _trade(position_value=9_999.0)
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sizing = next(f for f in result.findings if f.check_name == "sizing")
    assert sizing.passed is True
    assert sizing.severity == "info"
    assert "VolatilityTargetSizing" in sizing.details


def test_sizing_fixed_notional_within_tolerance() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(sizing=FixedNotionalSizing(notional_usd=5_000.0))
    trade = _trade(position_value=5_040.0)  # 0.8% over
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sizing = next(f for f in result.findings if f.check_name == "sizing")
    assert sizing.passed is True


# ---------------------------------------------------------------------------
# Check 4 — stop-loss
# ---------------------------------------------------------------------------


def test_stop_loss_passes_when_engine_attribution_present() -> None:
    """``exit_reason='engine_exit:stop_loss'`` is the strongest signal
    that the engine honoured the rule, regardless of return rounding."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    trade = _trade(return_pct=-5.10, exit_reason="engine_exit:stop_loss")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sl = next(f for f in result.findings if f.check_name == "stop_loss")
    assert sl.passed is True
    assert sl.severity == "info"


def test_stop_loss_past_floor_is_informational_pass() -> None:
    """A non-attributed close past the nominal floor is informational and
    passing — a stop is a trigger, so realized loss beyond the threshold is
    expected, not a breach."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    trade = _trade(return_pct=-5.30)  # 0.30pp past the 5% floor
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sl = next(f for f in result.findings if f.check_name == "stop_loss")
    assert sl.passed is True
    assert sl.severity == "info"


def test_stop_loss_trailing_basis_emits_info_skip_only() -> None:
    """Trailing-high / trailing-low stops are path-dependent and cannot
    be validated from terminal ``return_pct`` alone. The check must
    emit an info-severity skip per trailing rule rather than running
    the entry-price floor logic, otherwise a strategy that legitimately
    closed at a deep drawdown under a generous trailing stop could be
    falsely marked critical (or vice versa). Regression for PR #613
    review.
    """
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")])
    # Trade closed at a return well past the naive ``-5%`` floor — if
    # basis were ignored, this would be marked critical. With
    # trailing-high basis, the check is informational only.
    trade = _trade(return_pct=-12.0, exit_reason=None)
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sl_findings = [f for f in result.findings if f.check_name == "stop_loss"]
    assert len(sl_findings) == 1
    assert sl_findings[0].passed is True
    assert sl_findings[0].severity == "info"
    assert sl_findings[0].rule_id == "exit:stop_loss:trailing_high"
    assert "path-dependent" in sl_findings[0].details


def test_stop_loss_mixed_basis_runs_only_entry_basis_floor_check() -> None:
    """When the spec mixes entry-price and trailing stops, the trailing
    rule(s) emit info skips and the entry-price rule drives the
    attribution check. With no engine stop attribution and a return past
    the floor, both rows are informational (a stop is a trigger, not a
    price cap), so the run stays aligned."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(
        exit_rules=[
            StopLossRule(pct=0.05, basis="entry_price"),
            StopLossRule(pct=0.10, basis="trailing_high"),
        ]
    )
    # Return well past the -5% entry floor on a non-attributed close —
    # informational, not a misalignment.
    trade = _trade(return_pct=-12.0, exit_reason=None)
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sl_findings = sorted(
        (f for f in result.findings if f.check_name == "stop_loss"),
        key=lambda f: f.rule_id or "",
    )
    # One trailing-info skip + one entry-price info row; neither gates.
    assert len(sl_findings) == 2
    severities = {f.severity for f in sl_findings}
    rule_ids = {f.rule_id for f in sl_findings}
    assert severities == {"info"}
    assert all(f.passed for f in sl_findings)
    assert result.aligned is True
    assert "exit:stop_loss" in rule_ids
    assert "exit:stop_loss:trailing_high" in rule_ids


def test_stop_loss_past_floor_without_attribution_is_informational_not_critical() -> None:
    """A realized loss past the nominal stop floor on a non-engine-attributed
    close is NOT a misalignment. A stop-loss is a trigger, not a price cap:
    the fill can gap past the threshold, or another exit closed the position
    first. The position was still exited, so the gate emits a passing info row
    (no "breach" / risk-limit language) and the run stays aligned. Genuine
    "stop never fired" enforcement leaks are owned by ExitRuleConformanceGate.
    """
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    trade = _trade(return_pct=-12.0, exit_reason=None)
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sl = next(f for f in result.findings if f.check_name == "stop_loss")
    assert sl.passed is True
    assert sl.severity == "info"
    assert result.aligned is True
    # The narrative-driving details must not call this a breach / risk-limit
    # violation; it must frame the stop as a trigger.
    assert "breach" not in sl.details.lower()
    assert "trigger" in sl.details.lower()


# ---------------------------------------------------------------------------
# Check 5 — take-profit
# ---------------------------------------------------------------------------


def test_take_profit_passes_when_engine_attribution_present() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[TakeProfitRule(pct=0.05)])
    trade = _trade(return_pct=5.10, exit_reason="engine_exit:take_profit")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    tp = next(f for f in result.findings if f.check_name == "take_profit")
    assert tp.passed is True


def test_take_profit_past_ceiling_without_attribution_is_informational_not_critical() -> None:
    """Symmetric to the stop-loss case: a realized gain past the nominal
    take-profit ceiling on a non-engine-attributed close is expected market
    behaviour (gap-up fills past the trigger, or another exit fired first),
    not a misalignment. The gate emits a passing info row and the run stays
    aligned."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[TakeProfitRule(pct=0.05)])
    trade = _trade(return_pct=12.0, exit_reason=None)
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    tp = next(f for f in result.findings if f.check_name == "take_profit")
    assert tp.passed is True
    assert tp.severity == "info"
    assert result.aligned is True
    assert "breach" not in tp.details.lower()
    assert "trigger" in tp.details.lower()


# ---------------------------------------------------------------------------
# Check 6 — entry signal correlation
# ---------------------------------------------------------------------------


def test_entry_signal_passes_when_predicate_satisfied_at_entry_bar() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(entry_rules=[_rsi_lt_30()])
    # ``_market_data_rsi_oversold`` drives RSI below 30 by 2023-02-01.
    trade = _trade(entry_date="2023-02-01")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is True


def test_entry_signal_cross_above_satisfied_only_on_real_transition() -> None:
    """``cross_above`` requires prev_lhs <= prev_rhs AND curr_lhs > curr_rhs
    AT THE SIGNAL BAR. With the engine's ``signal-on-T / fill-on-T+1``
    contract, ``trade.entry_date`` is the fill bar; the gate evaluates
    one bar earlier (the signal bar).

    Three-bar fixture:
      - T-1 (pre-signal): close=99 (below 100 threshold)
      - T   (signal): close=101 (above) → cross fires HERE
      - T+1 (fill): close=101.5 (sustained above), ``trade.entry_date``

    Regression for PR #613 reviews: cross-op requires prior-bar
    transition (not single-bar inequality), and the gate must
    evaluate at the signal bar (fill_idx - 1), not the fill bar.
    """
    gate = DeterministicAlignmentChecker()
    rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op="cross_above", rhs=100.0),
    )
    spec = _spec(entry_rules=[rule])
    md = {
        "AAPL": [
            OHLCVBar(  # T-1
                date="2023-01-01",
                open=98.0,
                high=99.5,
                low=98.0,
                close=99.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T (signal — cross fires)
                date="2023-01-02",
                open=99.5,
                high=101.5,
                low=99.5,
                close=101.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T+1 (fill — entry_date)
                date="2023-01-03",
                open=101.0,
                high=102.0,
                low=100.5,
                close=101.5,
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(entry_date="2023-01-03")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is True


def test_entry_signal_cross_above_misses_on_sustained_above() -> None:
    """``cross_above`` must NOT fire when both pre-signal and signal bars
    are already above the threshold — that's a sustained state, not a
    crossover. Three-bar fixture (T-1 / T / T+1) reflects the
    ``signal-on-T / fill-on-T+1`` engine contract.
    """
    gate = DeterministicAlignmentChecker()
    rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op="cross_above", rhs=100.0),
    )
    spec = _spec(entry_rules=[rule])
    md = {
        "AAPL": [
            OHLCVBar(  # T-1: already above threshold
                date="2023-01-01",
                open=101.0,
                high=102.0,
                low=100.5,
                close=101.5,
                volume=1_000_000,
            ),
            OHLCVBar(  # T (signal): still above — no crossover
                date="2023-01-02",
                open=101.5,
                high=103.0,
                low=101.0,
                close=102.5,
                volume=1_000_000,
            ),
            OHLCVBar(  # T+1 (fill): still above, entry_date
                date="2023-01-03",
                open=102.5,
                high=103.5,
                low=101.5,
                close=103.0,
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(entry_date="2023-01-03")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is False
    assert entry.severity == "critical"


def test_entry_signal_cross_below_satisfied_only_on_real_transition() -> None:
    """Mirror of cross_above: T-1 above, T (signal) below → fire. The
    ``signal-on-T / fill-on-T+1`` engine contract puts ``entry_date``
    at T+1."""
    gate = DeterministicAlignmentChecker()
    rule = EntryRule(
        side="short",
        when=Predicate(lhs="bar.close", op="cross_below", rhs=100.0),
    )
    spec = _spec(entry_rules=[rule])
    md = {
        "AAPL": [
            OHLCVBar(  # T-1
                date="2023-01-01",
                open=101.0,
                high=102.0,
                low=100.5,
                close=101.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T (signal — cross fires)
                date="2023-01-02",
                open=100.5,
                high=100.5,
                low=98.0,
                close=99.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T+1 (fill — entry_date)
                date="2023-01-03",
                open=99.0,
                high=99.5,
                low=97.5,
                close=98.0,
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(side="short", entry_date="2023-01-03")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is True


def test_entry_signal_satisfied_by_combinator_has_no_scalar_tail() -> None:
    """A confirmation-stacked (``all_of``) entry rule that fires produces
    ``lhs``/``rhs`` of ``None`` (a combinator has no single scalar pair).

    Regression: the "satisfied" finding formatted ``lhs``/``rhs`` with
    ``:.6g`` unconditionally, so a satisfied combinator rule raised
    ``TypeError: unsupported format string passed to NoneType.__format__``
    and failed the whole strategy-generation cycle. The scalar tail must
    be omitted, not formatted, when the pair is absent.
    """
    gate = DeterministicAlignmentChecker()
    rule = EntryRule(
        side="long",
        when=AllOf(
            of=[
                Predicate(lhs="bar.close", op="cross_above", rhs=100.0),
                Predicate(lhs="bar.volume", op=">", rhs=500_000.0),
            ]
        ),
    )
    spec = _spec(entry_rules=[rule])
    md = {
        "AAPL": [
            OHLCVBar(  # T-1: below threshold
                date="2023-01-01",
                open=98.0,
                high=99.5,
                low=98.0,
                close=99.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T (signal — cross fires and volume confirms)
                date="2023-01-02",
                open=99.5,
                high=101.5,
                low=99.5,
                close=101.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T+1 (fill — entry_date)
                date="2023-01-03",
                open=101.0,
                high=102.0,
                low=100.5,
                close=101.5,
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(entry_date="2023-01-03")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is True
    # Combinator → no scalar pair → tail is a bare period, never "lhs=".
    assert "lhs=" not in entry.details
    assert entry.details.rstrip().endswith(".")


def test_entry_signal_missed_combinator_has_no_scalar_tail() -> None:
    """The hard-miss path shares the same latent bug: a combinator that
    does NOT fire has ``lhs``/``rhs`` of ``None`` too. The critical
    finding must render without a scalar tail rather than crash.
    """
    gate = DeterministicAlignmentChecker()
    rule = EntryRule(
        side="long",
        when=AllOf(
            of=[
                Predicate(lhs="bar.close", op="cross_above", rhs=100.0),
                Predicate(lhs="bar.volume", op=">", rhs=500_000.0),
            ]
        ),
    )
    spec = _spec(entry_rules=[rule])
    md = {
        "AAPL": [
            OHLCVBar(  # T-1: already above — no crossover possible
                date="2023-01-01",
                open=101.0,
                high=102.0,
                low=100.5,
                close=101.5,
                volume=1_000_000,
            ),
            OHLCVBar(  # T (signal): still above — cross_above misses
                date="2023-01-02",
                open=101.5,
                high=103.0,
                low=101.0,
                close=102.5,
                volume=1_000_000,
            ),
            OHLCVBar(  # T+1 (fill — entry_date)
                date="2023-01-03",
                open=102.5,
                high=103.5,
                low=101.5,
                close=103.0,
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(entry_date="2023-01-03")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is False
    assert entry.severity == "critical"
    assert "lhs=" not in entry.details
    assert entry.details.rstrip().endswith(".")


def test_entry_signal_cross_falls_closed_when_entry_is_first_bar() -> None:
    """Cross at the very first bar in market_data has no previous-bar
    context — fall closed (warmup-style) rather than fabricating a
    satisfied outcome from the current bar alone."""
    gate = DeterministicAlignmentChecker()
    rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op="cross_above", rhs=100.0),
    )
    spec = _spec(entry_rules=[rule])
    md = {
        "AAPL": [
            OHLCVBar(
                date="2023-01-02",
                open=99.0,
                high=101.0,
                low=99.0,
                close=101.0,  # above threshold, but no prior bar
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(entry_date="2023-01-02")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is False


def test_cross_predicate_miss_never_routes_to_near_miss_adjudicator(monkeypatch) -> None:
    """A sustained-above strategy can present a tiny
    ``|curr_lhs - curr_rhs|`` and superficially look like a tight
    near-miss — but no cross actually happened. The near-miss path
    must NOT consult the LLM for cross-op misses, regardless of the
    numerical gap. Regression for PR #613 review.
    """
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT", "0.05")

    record, adjudicator = _counting_adjudicator()
    record["scripted"] = [
        NearMissVerdict(legitimate=True, rationale="should not be consulted"),
    ]
    gate = DeterministicAlignmentChecker()
    rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op="cross_above", rhs=100.0),
    )
    spec = _spec(entry_rules=[rule])
    # Sustained-above: both bars sit just above 100, so the cross
    # never happened, but the per-bar gap is tiny enough that a
    # numeric near-miss check would route to the LLM.
    md = {
        "AAPL": [
            OHLCVBar(
                date="2023-01-01",
                open=100.5,
                high=100.7,
                low=100.2,
                close=100.4,
                volume=1_000_000,
            ),
            OHLCVBar(
                date="2023-01-02",
                open=100.4,
                high=100.6,
                low=100.1,
                close=100.5,
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(entry_date="2023-01-02")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
        near_miss_adjudicator=adjudicator,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    # The cross miss is critical and the LLM was never consulted.
    assert entry.passed is False
    assert entry.severity == "critical"
    assert record["calls"] == []


def test_entry_signal_critical_when_predicate_far_off() -> None:
    """RSI well above 30 at entry → critical finding, LLM never invoked."""
    record, adjudicator = _counting_adjudicator()
    gate = DeterministicAlignmentChecker()
    spec = _spec(entry_rules=[_rsi_lt_30()])
    trade = _trade(entry_date="2023-02-01")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_overbought(),
        initial_capital=100_000.0,
        near_miss_adjudicator=adjudicator,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is False
    assert entry.severity == "critical"
    # Magnitude well outside 1% — LLM is NOT consulted.
    assert record["calls"] == []


def test_entry_signal_near_miss_consults_adjudicator(monkeypatch) -> None:
    """Issue acceptance: a tight predicate miss is within tolerance and
    routes through the LLM near-miss adjudicator. Uses ``bar.close``
    against a literal threshold so the computed value is directly
    controllable (RSI-driven near-miss is harder to land at a known
    relative magnitude without a longer fixture)."""
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT", "0.01")

    record, adjudicator = _counting_adjudicator()
    record["scripted"] = [
        NearMissVerdict(legitimate=True, rationale="one-cent rounding noise"),
    ]
    gate = DeterministicAlignmentChecker()
    # Spec: enter long when ``bar.close < 100``. The SIGNAL bar's close
    # is 100.5 — a 0.5% relative miss, inside the 1% tolerance. With
    # the engine's signal-on-T / fill-on-T+1 contract, ``entry_date``
    # is the fill bar (T+1).
    rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op="<", rhs=100.0),
    )
    spec = _spec(entry_rules=[rule])
    md = {
        "AAPL": [
            OHLCVBar(  # T (signal — close=100.5 is the near-miss)
                date="2023-01-01",
                open=99.0,
                high=101.0,
                low=98.0,
                close=100.5,
                volume=1_000_000,
            ),
            OHLCVBar(  # T+1 (fill — entry_date)
                date="2023-01-02",
                open=100.5,
                high=101.0,
                low=99.5,
                close=100.0,
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(entry_date="2023-01-02")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
        near_miss_adjudicator=adjudicator,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    # Adjudicator was consulted exactly once.
    assert len(record["calls"]) == 1
    call = record["calls"][0]
    assert call["rule_id"] == "entry[0]"
    assert call["symbol"] == "AAPL"
    assert call["computed_value"] == 100.5
    assert call["threshold"] == 100.0
    # The legitimate verdict flips the finding to passing info.
    assert entry.passed is True
    assert entry.severity == "info"


def test_entry_signal_near_miss_skipped_when_pct_zero(monkeypatch) -> None:
    """``STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT=0`` disables the LLM
    adjudicator entirely — any miss is a hard fail."""
    monkeypatch.setenv("STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT", "0")

    record, adjudicator = _counting_adjudicator()
    gate = DeterministicAlignmentChecker()
    rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op="<", rhs=100.0),
    )
    spec = _spec(entry_rules=[rule])
    md = {
        "AAPL": [
            OHLCVBar(  # T (signal — tight miss; would invoke LLM at >0 tol)
                date="2023-01-01",
                open=99.0,
                high=101.0,
                low=98.0,
                close=100.5,
                volume=1_000_000,
            ),
            OHLCVBar(  # T+1 (fill — entry_date)
                date="2023-01-02",
                open=100.5,
                high=101.0,
                low=99.5,
                close=100.0,
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(entry_date="2023-01-02")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
        near_miss_adjudicator=adjudicator,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is False
    assert entry.severity == "critical"
    # Adjudicator was never called.
    assert record["calls"] == []


def test_entry_signal_rule_id_uses_original_spec_index() -> None:
    """Mixed long/short spec: a SHORT trade's finding must cite the
    rule's index in ``spec.entry_rules``, not its position in the
    side-filtered subset. Regression for the rule-renumbering bug
    raised in PR review (#613 review)."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(
        entry_rules=[
            _rsi_lt_30(side="long"),  # spec.entry_rules[0]
            _rsi_lt_30(side="short"),  # spec.entry_rules[1]
            _rsi_lt_30(side="long"),  # spec.entry_rules[2]
        ],
    )
    # SHORT trade — only entry_rules[1] is side-matching. The finding
    # rule_id must read ``entry[1]``, not ``entry[0]``.
    trade = _trade(side="short", entry_date="2023-02-01")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.rule_id == "entry[1]"


def test_entry_signal_missing_bars_flagged_critical() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(entry_rules=[_rsi_lt_30()])
    trade = _trade(symbol="AAPL", entry_date="2023-02-01")
    # No market_data for AAPL — should flag critical without crashing.
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data={},
        initial_capital=100_000.0,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    assert entry.passed is False
    assert entry.severity == "critical"
    assert "no market_data bars" in entry.details


# ---------------------------------------------------------------------------
# Check 8 — signal-exit correlation
# ---------------------------------------------------------------------------


def _rsi_gt_70_signal_exit() -> SignalExitRule:
    return SignalExitRule(
        when=Predicate(
            lhs=IndicatorRef(name="rsi", params={"period": 14}),
            op=">",
            rhs=70.0,
        ),
    )


def test_signal_exit_no_finding_when_spec_has_none() -> None:
    """Spec has no SignalExitRule — the check is a no-op."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    trade = _trade(entry_date="2023-02-01", exit_date="2023-02-10")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    signal_exit_findings = [f for f in result.findings if f.check_name == "signal_exit"]
    assert signal_exit_findings == []


def test_signal_exit_info_skip_when_engine_attributed_close() -> None:
    """Engine-attributed close (``exit_reason='engine_exit:...''``) → info
    skip; the matching structured-exit check carries the alignment."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[_rsi_gt_70_signal_exit()])
    trade = _trade(
        entry_date="2023-02-01",
        exit_date="2023-02-10",
        exit_reason="engine_exit:stop_loss",
    )
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    signal_exit = next(f for f in result.findings if f.check_name == "signal_exit")
    assert signal_exit.passed is True
    assert signal_exit.severity == "info"
    assert "engine_exit:stop_loss" in signal_exit.details


def test_entry_signal_evaluated_at_signal_bar_not_fill_bar() -> None:
    """Regression for PR #613 review: the engine uses
    ``signal-on-T / fill-on-T+1``, so ``trade.entry_date`` is the
    fill bar. The gate must evaluate the entry predicate at the
    signal bar (one earlier), or transient signals get marked
    misaligned even when execution was correct.

    Three-bar fixture: signal predicate ``bar.close < 30`` fires at T
    (signal bar). By T+1 (fill bar) the close has popped back above
    30 — a sustained-non-signal state. If the gate evaluated at the
    fill bar, this would be flagged critical even though the entry
    was a legitimate, executed-correctly signal fire.
    """
    gate = DeterministicAlignmentChecker()
    rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op="<", rhs=30.0),
    )
    spec = _spec(entry_rules=[rule])
    md = {
        "AAPL": [
            OHLCVBar(
                date="2023-01-01",
                open=40.0,
                high=42.0,
                low=39.0,
                close=41.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T (signal — predicate fires here)
                date="2023-01-02",
                open=30.5,
                high=31.0,
                low=28.0,
                close=29.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T+1 (fill — entry_date; close popped back above 30)
                date="2023-01-03",
                open=29.5,
                high=33.0,
                low=29.5,
                close=32.0,
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(entry_date="2023-01-03")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    entry = next(f for f in result.findings if f.check_name == "entry_signal")
    # Evaluating at the SIGNAL bar (close=29.0 < 30) → satisfied.
    # If the gate were evaluating at the fill bar (close=32.0), this
    # would be flagged critical.
    assert entry.passed is True
    assert entry.severity == "info"


def test_signal_exit_evaluated_at_signal_bar_not_fill_bar() -> None:
    """Mirror of the entry test for the signal-exit check #8. The
    exit predicate fires at the signal bar; ``trade.exit_date`` is
    the fill bar one later. The gate must evaluate at the signal bar.
    """
    gate = DeterministicAlignmentChecker()
    exit_rule = SignalExitRule(
        when=Predicate(lhs="bar.close", op=">", rhs=70.0),
    )
    # Entry rule satisfied at the entry bar (close=20 < 30 well below).
    entry_rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op="<", rhs=30.0),
    )
    spec = _spec(entry_rules=[entry_rule], exit_rules=[exit_rule])
    md = {
        "AAPL": [
            OHLCVBar(
                date="2023-01-01",
                open=25.0,
                high=26.0,
                low=18.0,
                close=20.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T (entry-fill bar) — entry_date
                date="2023-01-02",
                open=20.0,
                high=22.0,
                low=19.0,
                close=21.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T (exit-signal bar) — predicate fires here
                date="2023-01-03",
                open=68.0,
                high=72.0,
                low=67.0,
                close=71.0,
                volume=1_000_000,
            ),
            OHLCVBar(  # T+1 (exit-fill bar) — exit_date; close reverted below 70
                date="2023-01-04",
                open=71.0,
                high=72.0,
                low=65.0,
                close=68.0,
                volume=1_000_000,
            ),
        ]
    }
    trade = _trade(
        entry_date="2023-01-02",
        exit_date="2023-01-04",
        exit_reason=None,  # strategy-emitted close
    )
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    signal_exit = next(f for f in result.findings if f.check_name == "signal_exit")
    # Evaluating at the SIGNAL bar (close=71.0 > 70) → satisfied.
    # If the gate were evaluating at the fill bar (close=68.0), this
    # would be flagged critical.
    assert signal_exit.passed is True
    assert signal_exit.severity == "info"


def test_signal_exit_satisfied_when_predicate_fires_at_exit_bar() -> None:
    """SignalExitRule's predicate fires at exit bar → info pass.

    Spec exits when RSI > 70. The fixture's overbought market drives
    RSI above 70 by the last bar; the trade exits on that bar with
    no engine attribution.
    """
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[_rsi_gt_70_signal_exit()])
    md = _market_data_rsi_overbought()
    last_date = md["AAPL"][-1].date
    trade = _trade(
        entry_date=md["AAPL"][20].date,  # enter mid-uptrend
        exit_date=last_date,
        exit_reason=None,  # strategy-emitted close
    )
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    signal_exit = next(f for f in result.findings if f.check_name == "signal_exit")
    assert signal_exit.passed is True
    assert signal_exit.severity == "info"
    assert "signal-exit satisfied" in signal_exit.details


def test_signal_exit_critical_when_no_predicate_fires_at_exit_bar() -> None:
    """SignalExitRule defined, no engine attribution, predicate does NOT
    fire at the exit bar → critical. The strategy closed for some
    other reason than the declared exit signal — alignment broken.
    """
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[_rsi_gt_70_signal_exit()])
    # Oversold market keeps RSI well below 70 throughout → exit
    # predicate never fires.
    md = _market_data_rsi_oversold()
    last_date = md["AAPL"][-1].date
    trade = _trade(
        entry_date=md["AAPL"][20].date,
        exit_date=last_date,
        exit_reason=None,
    )
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    signal_exit = next(f for f in result.findings if f.check_name == "signal_exit")
    assert signal_exit.passed is False
    assert signal_exit.severity == "critical"
    assert "no SignalExitRule predicate fires" in signal_exit.details


def test_signal_exit_critical_when_exit_bar_missing_from_market_data() -> None:
    """Strategy-emitted close on a date the gate has no bars for —
    cannot reproduce, fall closed."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[_rsi_gt_70_signal_exit()])
    md = _market_data_rsi_oversold()
    trade = _trade(
        entry_date=md["AAPL"][20].date,
        exit_date="2099-12-31",  # not in market_data
        exit_reason=None,
    )
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=md,
        initial_capital=100_000.0,
    )
    signal_exit = next(f for f in result.findings if f.check_name == "signal_exit")
    assert signal_exit.passed is False
    assert signal_exit.severity == "critical"


# ---------------------------------------------------------------------------
# Gate-level invariants
# ---------------------------------------------------------------------------


def test_check_returns_aligned_for_empty_trade_ledger() -> None:
    """Vacuously aligned when there are no trades to check; the
    zero-trade critical is owned by other gates upstream."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(target_symbols=["AAPL"])
    result = gate.check(
        spec=spec,
        trades=[],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    assert result.aligned is True
    assert result.findings == []
    assert len(result.gate_results) == 1
    assert result.gate_results[0].severity == "info"


def test_check_emits_quality_gate_result_per_finding() -> None:
    """Every :class:`AlignmentFinding` has a paired
    :class:`QualityGateResult` row for the dashboard stream, stamped
    with phase=verification and gate_name=alignment_finding.

    The distinct gate_name (vs. the cycle-level ``trade_alignment``
    aggregate the orchestrator emits separately) prevents the
    per-trade × per-check fan-out from inflating
    ``ConvergenceTracker``'s cycle-level failure counts.
    """
    gate = DeterministicAlignmentChecker()
    spec = _spec(target_symbols=["AAPL"])
    trade = _trade(symbol="AAPL", entry_date="2023-02-01")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    assert len(result.findings) == len(result.gate_results) > 0
    for g in result.gate_results:
        assert g.gate_name == "alignment_finding"
        assert g.phase == "verification"


def test_check_per_finding_gate_name_does_not_collide_with_cycle_aggregate() -> None:
    """Regression for PR #613 review: per-finding rows must NOT use
    ``gate_name="trade_alignment"``.

    ``ConvergenceTracker.record`` increments ``_failure_modes[gate_name]``
    per failed row. The orchestrator separately appends a single
    cycle-level ``trade_alignment`` aggregate row per alignment round;
    sharing the name on per-finding rows would multiply the cycle-level
    failure count by the per-trade × per-check fan-out and prematurely
    trip ``get_failure_directives(min_occurrences=3)``.
    """
    gate = DeterministicAlignmentChecker()
    # Multiple misaligned trades to ensure many failed per-finding rows.
    spec = _spec(target_symbols=["SPY"])  # all trades on AAPL fail universe
    trades = [_trade(trade_num=i + 1, symbol="AAPL", entry_date="2023-02-01") for i in range(3)]
    result = gate.check(
        spec=spec,
        trades=trades,
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    # No row from the gate may claim the cycle-level ``trade_alignment``
    # name — the orchestrator owns that label.
    gate_names = {g.gate_name for g in result.gate_results}
    assert "trade_alignment" not in gate_names
    assert gate_names == {"alignment_finding"}


def test_check_aligned_true_when_every_critical_passes() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(
        entry_rules=[_rsi_lt_30()],
        target_symbols=["AAPL"],
        sizing=FixedFractionSizing(fraction=0.02),
    )
    trade = _trade(
        symbol="AAPL",
        entry_date="2023-02-01",
        position_value=2_000.0,
    )
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    assert result.aligned is True
    # Only info findings (universe, side, sizing) plus the
    # passing entry_signal info row remain.
    criticals = [f for f in result.findings if f.severity == "critical"]
    assert criticals == []


def test_check_aligned_false_when_any_critical_fails() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(target_symbols=["SPY"], entry_rules=[_rsi_lt_30()])
    trade = _trade(symbol="TSLA", entry_date="2023-02-01")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold("TSLA"),
        initial_capital=100_000.0,
    )
    assert result.aligned is False
    assert any(f.severity == "critical" and not f.passed for f in result.findings)


def test_findings_carry_per_rule_rule_ids() -> None:
    """Each finding has a stable ``rule_id`` so the dashboard can show
    which spec rule produced the row."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(
        entry_rules=[_rsi_lt_30()],
        exit_rules=[StopLossRule(pct=0.05), TakeProfitRule(pct=0.05)],
        target_symbols=["AAPL"],
    )
    trade = _trade(symbol="AAPL", entry_date="2023-02-01")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    rule_ids = {f.rule_id for f in result.findings}
    assert "universe" in rule_ids
    assert "exit:stop_loss" in rule_ids
    assert "exit:take_profit" in rule_ids


def test_finding_aggregate_aligned_ignores_info_failures() -> None:
    """An ``info`` severity finding with ``passed=False`` (e.g. an
    entry-side mismatch already covered by check #2) does NOT drag
    ``aligned`` to ``False`` — only critical-severity findings count."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(
        entry_rules=[_rsi_lt_30(side="short")],  # spec only has SHORT entries
        target_symbols=["AAPL"],
    )
    trade = _trade(symbol="AAPL", side="long", entry_date="2023-02-01")
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    # The check #2 (side) row IS critical; check #7 also produces an
    # informational "no matching-side entry rule" row.
    side_findings = [f for f in result.findings if f.check_name == "side"]
    entry_findings = [f for f in result.findings if f.check_name == "entry_signal"]
    assert any(f.severity == "critical" and not f.passed for f in side_findings)
    assert any(f.severity == "info" for f in entry_findings)
    assert result.aligned is False


def test_findings_have_consistent_trade_num_column() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(target_symbols=["AAPL"])
    trades = [
        _trade(trade_num=1, symbol="AAPL", entry_date="2023-02-01"),
        _trade(trade_num=2, symbol="AAPL", entry_date="2023-02-01"),
    ]
    result = gate.check(
        spec=spec,
        trades=trades,
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    trade_nums = {f.trade_num for f in result.findings}
    assert trade_nums == {1, 2}


def test_sizing_equity_walk_realizes_pnl_only_after_exit() -> None:
    """Overlapping trades: trade A enters first but exits AFTER trade B's
    entry. Trade B's sizing must NOT see trade A's net_pnl yet — that
    PnL is unrealized at B's entry. Regression for the entry-date
    walk that leaked future PnL into overlapping entries (PR #613
    review)."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.10),
        target_symbols=["AAPL"],
    )
    # Trade A: 2023-01-05 → 2023-01-20 (still open when B enters)
    # Trade B: 2023-01-10 → 2023-01-15 (overlaps with A)
    # At B's entry, A is still open → B's equity baseline = initial only.
    trade_a = _trade(
        trade_num=1,
        position_value=10_000.0,  # 10% of $100k → expected at entry
        net_pnl=10_000.0,
        entry_date="2023-01-05",
        exit_date="2023-01-20",
    )
    trade_b = _trade(
        trade_num=2,
        position_value=10_000.0,  # 10% of $100k (A's PnL not yet realized)
        net_pnl=500.0,
        entry_date="2023-01-10",
        exit_date="2023-01-15",
    )
    result = gate.check(
        spec=spec,
        trades=[trade_a, trade_b],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sizing_findings = sorted(
        (f for f in result.findings if f.check_name == "sizing"),
        key=lambda f: f.trade_num,
    )
    # Both trades sized against $100k, not $110k — A's PnL is still
    # unrealized at B's entry.
    assert sizing_findings[0].expected_value == 10_000.0
    assert sizing_findings[1].expected_value == 10_000.0
    assert all(f.passed for f in sizing_findings)


def test_findings_emit_for_multiple_trades_track_running_equity() -> None:
    """Sequential trades' sizing checks use a running-equity walk so
    each trade's expected position value reflects accumulated net pnl
    at that trade's entry."""
    gate = DeterministicAlignmentChecker()
    spec = _spec(
        sizing=FixedFractionSizing(fraction=0.10),
        target_symbols=["AAPL"],
    )
    # Trade 1 closes with +$10k pnl → equity climbs to $110k before
    # trade 2's entry. Trade 2's expected position value should be
    # ~$11,000, not $10,000.
    trade1 = _trade(
        trade_num=1,
        position_value=10_000.0,
        net_pnl=10_000.0,
        entry_date="2023-01-05",
        exit_date="2023-01-10",
    )
    trade2 = _trade(
        trade_num=2,
        position_value=11_000.0,
        net_pnl=500.0,
        entry_date="2023-01-15",
        exit_date="2023-01-20",
    )
    result = gate.check(
        spec=spec,
        trades=[trade1, trade2],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sizing_findings = sorted(
        (f for f in result.findings if f.check_name == "sizing"),
        key=lambda f: f.trade_num,
    )
    assert sizing_findings[0].expected_value == 10_000.0
    assert sizing_findings[1].expected_value == 11_000.0
    assert all(f.passed for f in sizing_findings)

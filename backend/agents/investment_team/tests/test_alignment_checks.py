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
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    IndicatorRef,
    Predicate,
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
    gate = DeterministicAlignmentChecker()
    spec = _spec(sizing=FixedFractionSizing(fraction=0.02))
    # Expected $2,000; trade at $2,800 = 40% off, no execution caveat.
    trade = _trade(position_value=2_800.0)
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


def test_stop_loss_passes_when_return_within_floor_with_slack() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    trade = _trade(return_pct=-5.30)  # 0.30pp past the 5% floor, within 0.5pp slack
    result = gate.check(
        spec=spec,
        trades=[trade],
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    sl = next(f for f in result.findings if f.check_name == "stop_loss")
    assert sl.passed is True


def test_stop_loss_critical_when_return_breaches_floor_without_attribution() -> None:
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
    assert sl.passed is False
    assert sl.severity == "critical"


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


def test_take_profit_critical_when_return_exceeds_ceiling_without_attribution() -> None:
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
    assert tp.passed is False
    assert tp.severity == "critical"


# ---------------------------------------------------------------------------
# Check 6 — time-stop (guarded no-op until the DSL grows the rule)
# ---------------------------------------------------------------------------


def test_time_stop_emits_single_info_finding_per_trade() -> None:
    gate = DeterministicAlignmentChecker()
    spec = _spec()
    trades = [_trade(trade_num=1), _trade(trade_num=2)]
    result = gate.check(
        spec=spec,
        trades=trades,
        market_data=_market_data_rsi_oversold(),
        initial_capital=100_000.0,
    )
    time_stops = [f for f in result.findings if f.check_name == "time_stop"]
    assert len(time_stops) == 2
    assert all(f.passed and f.severity == "info" for f in time_stops)
    assert all("TimeStopRule not in current DSL" in f.details for f in time_stops)


# ---------------------------------------------------------------------------
# Check 7 — entry signal correlation
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
    # Spec: enter long when ``bar.close < 100``. The entry bar's close
    # will be 100.5 — a 0.5% relative miss, inside the 1% tolerance.
    rule = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op="<", rhs=100.0),
    )
    spec = _spec(entry_rules=[rule])
    md = {
        "AAPL": [
            OHLCVBar(
                date="2023-01-01",
                open=99.0,
                high=101.0,
                low=98.0,
                close=100.5,  # 0.5% above the 100 threshold — tight miss
                volume=1_000_000,
            )
        ]
    }
    trade = _trade(entry_date="2023-01-01")
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
            OHLCVBar(
                date="2023-01-01",
                open=99.0,
                high=101.0,
                low=98.0,
                close=100.5,  # tight miss — would invoke LLM with tolerance > 0
                volume=1_000_000,
            )
        ]
    }
    trade = _trade(entry_date="2023-01-01")
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
    with phase=verification and gate_name=trade_alignment."""
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
        assert g.gate_name == "trade_alignment"
        assert g.phase == "verification"


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
    # Only info findings (universe, side, sizing, time_stop) plus the
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

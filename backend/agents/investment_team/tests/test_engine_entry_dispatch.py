"""Unit tests for ``_EngineEntryDispatcher`` in ``trading_service.service``."""

from __future__ import annotations

from unittest.mock import MagicMock

from investment_team.execution.risk_filter import RiskLimits
from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    Predicate,
    StopLossRule,
    VolatilityTargetSizing,
)
from investment_team.trading_service.service import (
    TradingServiceResult,
    _EngineEntryDispatcher,
)


def _make_bar(
    symbol="AAA", close=100.0, high=101.0, low=99.0, volume=1000.0, timestamp="2024-01-10"
):
    bar = MagicMock()
    bar.symbol = symbol
    bar.close = close
    bar.high = high
    bar.low = low
    bar.open = close
    bar.volume = volume
    bar.timestamp = timestamp
    return bar


def _make_portfolio(capital=100000.0, positions=None):
    port = MagicMock()
    port.positions = positions or {}
    port.mark_to_market.return_value = capital
    return port


def _build_view(closes: list[float]) -> StreamingHistoryView:
    view = StreamingHistoryView()
    for i, c in enumerate(closes):
        view.append(
            BarRecord(
                timestamp=f"2024-01-{i + 1:02d}",
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=1000.0,
            )
        )
    return view


def test_entry_fires_when_predicate_satisfied():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=90.0)),
    ]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio()
    pending: list = []
    views = {"AAA": _build_view([80.0, 90.0, 100.0])}
    result = TradingServiceResult()

    dispatcher.maybe_emit(
        cur_bar=bar,
        portfolio=portfolio,
        pending_for_prev=pending,
        views=views,
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].side.value == "long"
    assert pending[0].reason.startswith("engine_entry:")


def test_entry_skipped_when_position_exists():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=90.0)),
    ]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio(positions={"AAA": MagicMock()})
    pending: list = []
    views = {"AAA": _build_view([80.0, 90.0, 100.0])}
    result = TradingServiceResult()

    dispatcher.maybe_emit(
        cur_bar=bar,
        portfolio=portfolio,
        pending_for_prev=pending,
        views=views,
        result=result,
    )
    assert len(pending) == 0


def test_entry_skipped_when_predicate_not_satisfied():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=200.0)),
    ]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio()
    pending: list = []
    views = {"AAA": _build_view([80.0, 90.0, 100.0])}
    result = TradingServiceResult()

    dispatcher.maybe_emit(
        cur_bar=bar,
        portfolio=portfolio,
        pending_for_prev=pending,
        views=views,
        result=result,
    )
    assert len(pending) == 0


def test_entry_disabled_when_no_rules():
    dispatcher = _EngineEntryDispatcher(entry_rules=[], sizing=None)
    bar = _make_bar()
    portfolio = _make_portfolio()
    pending: list = []
    result = TradingServiceResult()
    dispatcher.maybe_emit(
        cur_bar=bar,
        portfolio=portfolio,
        pending_for_prev=pending,
        views={"AAA": _build_view([100.0])},
        result=result,
    )
    assert len(pending) == 0


def test_sizing_fixed_notional():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=50.0)),
    ]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedNotionalSizing(notional_usd=5000.0),
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio()
    pending: list = []
    views = {"AAA": _build_view([60.0, 70.0, 80.0])}
    result = TradingServiceResult()

    dispatcher.maybe_emit(
        cur_bar=bar,
        portfolio=portfolio,
        pending_for_prev=pending,
        views=views,
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].qty == 50  # 5000 / 100


# --- Runtime risk-limit enforcement in _compute_qty / cap helpers ---


def test_cap_qty_to_position_clamps_to_max_position_pct():
    """A raw share count whose notional exceeds max_position_pct is clamped."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=None,
        risk_limits=RiskLimits(max_position_pct=5),
    )
    # equity=100k, close=100, cap 5% -> max notional 5000 -> 50 shares.
    assert disp._cap_qty_to_position(300.0, equity=100000.0, close=100.0) == 50.0


def test_cap_qty_to_position_noop_without_limits():
    disp = _EngineEntryDispatcher(entry_rules=[], sizing=None)  # risk_limits None
    assert disp._cap_qty_to_position(300.0, equity=100000.0, close=100.0) == 300.0


def test_cap_qty_to_loss_sizes_down_to_tolerance_with_stop():
    """qty is capped so deployed x tightest stop <= max_loss_per_trade_pct."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=None,
        exit_rules=[StopLossRule(basis="entry_price", pct=0.05)],
        risk_limits=RiskLimits(max_loss_per_trade_pct=1),
    )
    # max_loss 1% / (stop 5%) = 20% deployable -> 200 shares @ $100 on $100k.
    assert disp._cap_qty_to_loss(1000.0, side="long", equity=100000.0, close=100.0) == 200.0


def test_cap_qty_to_loss_no_stop_caps_full_deployment():
    """Without an effective stop the whole position is at risk, so deployment
    is capped at max_loss_per_trade_pct itself (stop factor 1.0)."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=None,
        exit_rules=[],
        risk_limits=RiskLimits(max_loss_per_trade_pct=1),
    )
    # 1% of $100k = $1000 -> 10 shares @ $100.
    assert disp._cap_qty_to_loss(1000.0, side="long", equity=100000.0, close=100.0) == 10.0


def test_cap_qty_to_loss_ignores_side_incompatible_stop():
    """A trailing_low stop cannot fire for a long, so it does not cap the loss
    and the full-deployment (stop factor 1.0) cap applies."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=None,
        exit_rules=[StopLossRule(basis="trailing_low", pct=0.05)],
        risk_limits=RiskLimits(max_loss_per_trade_pct=1),
    )
    assert disp._cap_qty_to_loss(1000.0, side="long", equity=100000.0, close=100.0) == 10.0


def test_cap_qty_to_loss_noop_without_tolerance():
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=None,
        exit_rules=[StopLossRule(basis="entry_price", pct=0.05)],
        risk_limits=RiskLimits(),  # max_loss_per_trade_pct defaults to None
    )
    assert disp._cap_qty_to_loss(1000.0, side="long", equity=100000.0, close=100.0) == 1000.0


def test_compute_qty_applies_loss_cap_for_fixed_fraction():
    """End-to-end: a 50% fixed fraction with a 5% stop and a 1% loss tolerance
    is sized down from 500 to 200 shares."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=FixedFractionSizing(fraction=0.50),
        exit_rules=[StopLossRule(basis="entry_price", pct=0.05)],
        risk_limits=RiskLimits(max_loss_per_trade_pct=1, max_position_pct=50),
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio(capital=100000.0)
    assert disp._compute_qty("long", bar, portfolio, {}) == 200


def test_compute_qty_vol_target_clamped_to_position_cap():
    """End-to-end: vol-target sizing that would deploy past max_position_pct is
    clamped to the cap (50 shares = 5% of $100k @ $100)."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=VolatilityTargetSizing(target_annual_vol=0.15),
        risk_limits=RiskLimits(max_position_pct=5),
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio(capital=100000.0)
    # Build a view with a small ATR so raw vol-target sizing is large.
    view = _build_view([float(100 + i * 0.1) for i in range(40)])
    qty = disp._compute_qty("long", bar, portfolio, {"AAA": view})
    assert qty == 50


def test_compute_qty_returns_zero_when_cap_below_one_share():
    """When a risk cap reduces the order below one whole share, skip the entry
    rather than floor to 1 (which would breach the declared cap). $1k equity,
    $500 stock, 1% loss tolerance, no stop -> 0.02 shares -> skip."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=FixedFractionSizing(fraction=0.50),
        exit_rules=[],
        risk_limits=RiskLimits(max_loss_per_trade_pct=1, max_position_pct=50),
    )
    bar = _make_bar(close=500.0)
    portfolio = _make_portfolio(capital=1000.0)
    assert disp._compute_qty("long", bar, portfolio, {}) == 0


def test_compute_qty_sub_one_raw_without_cap_keeps_legacy_floor():
    """A sub-1 raw size that no risk cap touched keeps the legacy 1-share floor
    (no risk_limits attached)."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=FixedFractionSizing(fraction=0.50),
    )  # risk_limits None -> no caps
    bar = _make_bar(close=500.0)
    portfolio = _make_portfolio(capital=100.0)  # 0.1 shares raw
    assert disp._compute_qty("long", bar, portfolio, {}) == 1


def test_compute_qty_vol_target_warmup_fallback_is_capped():
    """The vol-target ATR-warmup fallback (no view yet) is a 1-share probe, but
    it still runs through the caps: a 1% loss tolerance with no stop on a
    high-priced stock forbids even one share, so the entry is skipped."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=VolatilityTargetSizing(target_annual_vol=0.15),
        exit_rules=[],
        risk_limits=RiskLimits(max_loss_per_trade_pct=1, max_position_pct=50),
    )
    bar = _make_bar(close=500.0)
    portfolio = _make_portfolio(capital=1000.0)
    # No view -> ATR warmup fallback (raw 1 share); loss cap -> 0.02 -> skip.
    assert disp._compute_qty("long", bar, portfolio, {}) == 0


def test_compute_qty_vol_target_warmup_fallback_emits_one_when_within_caps():
    """When the 1-share warmup probe is within the caps, it still emits 1."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=VolatilityTargetSizing(target_annual_vol=0.15),
        risk_limits=RiskLimits(max_position_pct=50),  # no loss tolerance
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio(capital=100000.0)
    assert disp._compute_qty("long", bar, portfolio, {}) == 1


def test_cap_qty_to_loss_uncovered_short_is_skipped():
    """A short with no effective stop has unbounded loss, so no size honours the
    tolerance — the entry is skipped (qty 0) rather than sized at full
    deployment."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=None,
        exit_rules=[],  # no stop
        risk_limits=RiskLimits(max_loss_per_trade_pct=1),
    )
    assert disp._cap_qty_to_loss(1000.0, side="short", equity=100000.0, close=100.0) == 0.0


def test_cap_qty_to_loss_short_with_effective_stop_sizes_down():
    """A short WITH a side-compatible stop (trailing_low) is bounded, so it is
    sized down rather than skipped."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=None,
        exit_rules=[StopLossRule(basis="trailing_low", pct=0.05)],
        risk_limits=RiskLimits(max_loss_per_trade_pct=1),
    )
    # 1% / 5% stop = 20% deployable -> 200 shares @ $100 on $100k.
    assert disp._cap_qty_to_loss(1000.0, side="short", equity=100000.0, close=100.0) == 200.0


def test_compute_qty_fixed_notional_clamped_to_position_cap_on_equity_drop():
    """A fixed-notional order that fit max_position_pct at initial capital can
    breach it after equity falls (notional is a fixed $ amount, so its share of
    a shrunken account rises). It is clamped to the live position cap. $5k
    notional, 5% cap: valid at $100k equity (5%), clamped at $50k (would be
    10%)."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=FixedNotionalSizing(notional_usd=5000.0),
        risk_limits=RiskLimits(max_position_pct=5),
    )
    bar = _make_bar(close=100.0)
    # At $100k equity: 5000/100 = 50 shares = $5k = 5% -> within cap, unchanged.
    assert disp._compute_qty("long", bar, _make_portfolio(capital=100000.0), {}) == 50
    # At $50k equity: cap is 5% * 50k = $2.5k = 25 shares; raw 50 -> clamped 25.
    assert disp._compute_qty("long", bar, _make_portfolio(capital=50000.0), {}) == 25

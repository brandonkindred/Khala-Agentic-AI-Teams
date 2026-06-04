"""Unit tests for ``_EngineEntryDispatcher`` in ``trading_service.service``."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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


def test_entry_emits_risk_presized_order():
    """Dispatcher-emitted entries are flagged risk_presized so RiskFilter does
    not re-reject them on a gap-up (the dispatcher already clamped to the cap)."""
    rules = [EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=90.0))]
    dispatcher = _EngineEntryDispatcher(entry_rules=rules, sizing=FixedFractionSizing(fraction=0.02))
    pending: list = []
    result = TradingServiceResult()
    dispatcher.maybe_emit(
        cur_bar=_make_bar(close=100.0),
        portfolio=_make_portfolio(),
        pending_for_prev=pending,
        views={"AAA": _build_view([80.0, 90.0, 100.0])},
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].risk_presized is True


def test_maybe_emit_records_risk_capped_skip_for_uncovered_short():
    """A matched signal that risk-sizing reduces to zero (uncovered short with a
    declared loss tolerance) is recorded as a 'risk_capped_skip' diagnostic event
    rather than a silent no-emit, so a zero-trade run is explainable."""
    rules = [EntryRule(side="short", when=Predicate(lhs="bar.close", op=">", rhs=90.0))]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
        exit_rules=[],  # no stop -> uncovered short -> sized to 0
        risk_limits=RiskLimits(max_loss_per_trade_pct=1),
    )
    pending: list = []
    result = TradingServiceResult()
    dispatcher.maybe_emit(
        cur_bar=_make_bar(close=100.0),
        portfolio=_make_portfolio(),
        pending_for_prev=pending,
        views={"AAA": _build_view([80.0, 90.0, 100.0])},
        result=result,
    )
    assert len(pending) == 0
    events = result.execution_diagnostics.last_order_events
    assert any(e.event_type == "risk_capped_skip" for e in events)


def test_cap_qty_to_position_skips_on_non_positive_equity():
    """A percent-of-equity cap admits no positive position on a non-positive
    account, so _cap_qty_to_position returns 0 (skip) rather than a negative
    max_qty. Without risk limits it returns qty unchanged regardless."""
    disp = _EngineEntryDispatcher(
        entry_rules=[], sizing=None, risk_limits=RiskLimits(max_position_pct=6)
    )
    assert disp._cap_qty_to_position(100.0, equity=0.0, close=100.0) == 0.0
    assert disp._cap_qty_to_position(100.0, equity=-5000.0, close=100.0) == 0.0
    no_limits = _EngineEntryDispatcher(entry_rules=[], sizing=None, risk_limits=None)
    assert no_limits._cap_qty_to_position(100.0, equity=-5000.0, close=100.0) == 100.0


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


def test_cap_qty_to_loss_uses_first_stop_in_spec_order_not_tightest():
    """The engine fires the FIRST side-compatible stop in spec order, so a
    looser stop ahead of a tighter one governs the worst-case loss (it wins on
    a gap crossing both). With [20%, 5%], the 20% stop sets the cap: max_loss 1%
    / 20% = 5% deployable = 50 shares @ $100 on $100k — NOT 200 (which the 5%
    min would wrongly allow, breaching the tolerance on a gap)."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=None,
        exit_rules=[
            StopLossRule(basis="entry_price", pct=0.20),  # loose FIRST
            StopLossRule(basis="entry_price", pct=0.05),
        ],
        risk_limits=RiskLimits(max_loss_per_trade_pct=1),
    )
    assert disp._cap_qty_to_loss(1000.0, side="long", equity=100000.0, close=100.0) == 50.0


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


def test_compute_qty_fixed_fraction_sub1_skips_when_one_share_breaches_position_cap():
    """fixed_fraction is exempt from the main-path position clamp, but flooring a
    sub-1 fixed-fraction order up to one share can still exceed max_position_pct.
    The one-share probe checks the position cap unconditionally, so the
    dispatcher skips the entry (0) rather than emitting a 1-share order that
    RiskFilter.can_enter would reject. fraction=5% (<= the 6% cap) on a $500
    stock at $1k equity is 0.1 shares raw; one share ($500) is 50% of equity,
    far above the 6% cap -> skip."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=FixedFractionSizing(fraction=0.05),
        risk_limits=RiskLimits(max_position_pct=6),
    )
    bar = _make_bar(close=500.0)
    assert disp._compute_qty("long", bar, _make_portfolio(capital=1000.0), {}) == 0


def test_compute_qty_fixed_fraction_sub1_floors_when_one_share_fits_position_cap():
    """When one share fits the position cap, the legacy 1-share floor still
    stands for a sub-1 fixed-fraction order. fraction=5% on a $100 stock at $1k
    equity is 0.5 shares raw; one share ($100) is 10% of equity, within a roomy
    50% cap -> floors to 1."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=FixedFractionSizing(fraction=0.05),
        risk_limits=RiskLimits(max_position_pct=50),
    )
    bar = _make_bar(close=100.0)
    assert disp._compute_qty("long", bar, _make_portfolio(capital=1000.0), {}) == 1


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


def test_compute_qty_fractional_asset_preserves_capped_sub1_order():
    """For a fractional asset class (crypto/forex), a risk-capped order below one
    unit is a valid fractional trade, not a no-op. With 50% of $100k deployed on
    a $60k asset (0.833 units raw) and a 5% stop under a 1% loss tolerance, the
    loss cap clamps to 0.333 units — a crypto run submits 0.333, while the
    whole-share path would skip it (return 0)."""
    common = dict(
        entry_rules=[],
        sizing=FixedFractionSizing(fraction=0.50),
        exit_rules=[StopLossRule(basis="entry_price", pct=0.05)],
        risk_limits=RiskLimits(max_loss_per_trade_pct=1),
    )
    bar = _make_bar(close=60000.0)
    port = _make_portfolio(capital=100000.0)

    crypto = _EngineEntryDispatcher(asset_class="crypto", **common)
    assert crypto._compute_qty("long", bar, port, {}) == pytest.approx(1.0 / 3.0)

    forex = _EngineEntryDispatcher(asset_class="forex", **common)
    assert forex._compute_qty("long", bar, port, {}) == pytest.approx(1.0 / 3.0)

    # Whole-share (equities) skips the sub-1 capped order, as before.
    equity_disp = _EngineEntryDispatcher(asset_class="stocks", **common)
    assert equity_disp._compute_qty("long", bar, port, {}) == 0


def test_compute_qty_skips_sub1_whole_share_when_one_share_breaches_cap():
    """A whole-share order below one share must not floor up to a full share when
    one share would breach a cap — even if the cap did not reduce qty. A $50
    fixed-notional order on a $100 stock is 0.5 shares raw; with a position cap
    admitting only 0.8 shares ($80), one whole share ($100) breaches it, so the
    entry is skipped (0) rather than floored to 1."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=FixedNotionalSizing(notional_usd=50.0),  # 0.5 shares @ $100 raw
        # 0.8% of $10k = $80 cap = 0.8 shares @ $100 (below one share).
        risk_limits=RiskLimits(max_position_pct=0.8),
    )
    bar = _make_bar(close=100.0)
    assert disp._compute_qty("long", bar, _make_portfolio(capital=10000.0), {}) == 0


def test_compute_qty_floors_sub1_to_one_share_when_one_share_fits_caps():
    """A naturally-sub-1 whole-share order still floors to one share when one
    share is within the caps (legacy behaviour preserved). A $50 notional on a
    $100 stock with a roomy 50% cap admits well over one share, so the 0.5-share
    raw order floors to 1."""
    disp = _EngineEntryDispatcher(
        entry_rules=[],
        sizing=FixedNotionalSizing(notional_usd=50.0),
        risk_limits=RiskLimits(max_position_pct=50),
    )
    bar = _make_bar(close=100.0)
    assert disp._compute_qty("long", bar, _make_portfolio(capital=10000.0), {}) == 1


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

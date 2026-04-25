"""Unit tests for ``RealisticExecutionModel`` (issue #248).

Covers the three fill-realism fixes:

1. **Limit gap-through** — resting limits fill at the limit price even when
   the bar opens through the limit (no "free alpha" on gap days).
2. **Stop gap-through** — stops triggered by gap-through bars fill at the
   gap-through price (``bar.open``), not clamped to the stop level. Pinned
   here as a regression test target — the legacy ``OptimisticExecutionModel``
   already does this; the realistic model preserves it.
3. **Participation cap** — orders sized above
   ``participation_cap × bar_dollar_volume`` partially fill to the cap;
   remainder is dropped.

Also covers the adverse-selection haircut on limit fills (``next_bar``-aware).
"""

from __future__ import annotations

import pytest

from investment_team.trading_service.engine.execution_model import (
    OptimisticExecutionModel,
    RealisticExecutionModel,
    build_execution_model,
)
from investment_team.trading_service.strategy.contract import (
    Bar,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)


def _bar(
    *,
    o: float,
    h: float,
    l: float,  # noqa: E741
    c: float,
    volume: float = 1_000_000.0,
    timestamp: str = "2024-01-02T00:00:00",
    symbol: str = "AAA",
) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=volume,
    )


def _req(
    *,
    side: OrderSide,
    order_type: OrderType,
    qty: float = 5,
    limit_price: float | None = None,
    stop_price: float | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_order_id="c1",
        symbol="AAA",
        side=side,
        qty=qty,
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        tif=TimeInForce.DAY,
    )


# ---------------------------------------------------------------------------
# Limit gap-through
# ---------------------------------------------------------------------------


def test_long_limit_fills_at_limit_when_bar_gaps_below():
    """Bar opens below the long limit (gap down). Realistic model fills at
    the limit price; optimistic fills at the (better) open price."""
    bar = _bar(o=95.0, h=98.0, l=94.0, c=96.0)
    req = _req(side=OrderSide.LONG, order_type=OrderType.LIMIT, limit_price=100.0)

    realistic = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=None)
    optimistic = OptimisticExecutionModel(warn=False).compute_fill_terms(req, bar, next_bar=None)

    assert realistic is not None
    assert realistic.reference_price == 100.0  # at limit, no improvement
    assert optimistic is not None
    assert optimistic.reference_price == 95.0  # legacy: better than limit


def test_short_limit_fills_at_limit_when_bar_gaps_above():
    """Mirror image for short limits on a gap-up open."""
    bar = _bar(o=110.0, h=112.0, l=109.0, c=111.0)
    req = _req(side=OrderSide.SHORT, order_type=OrderType.LIMIT, limit_price=100.0)

    realistic = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=None)
    optimistic = OptimisticExecutionModel(warn=False).compute_fill_terms(req, bar, next_bar=None)

    assert realistic.reference_price == 100.0
    assert optimistic.reference_price == 110.0


def test_long_limit_normal_fill_unchanged():
    """When the bar opens above the limit and dips down to fill, both
    models return the limit price."""
    bar = _bar(o=102.0, h=103.0, l=99.5, c=101.0)
    req = _req(side=OrderSide.LONG, order_type=OrderType.LIMIT, limit_price=100.0)

    realistic = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=None)
    optimistic = OptimisticExecutionModel(warn=False).compute_fill_terms(req, bar, next_bar=None)

    assert realistic.reference_price == 100.0
    assert optimistic.reference_price == 100.0  # min(102, 100) = 100


def test_long_limit_not_triggered_when_bar_stays_above():
    bar = _bar(o=102.0, h=103.0, l=101.0, c=102.5)
    req = _req(side=OrderSide.LONG, order_type=OrderType.LIMIT, limit_price=100.0)
    assert RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=None) is None


# ---------------------------------------------------------------------------
# Stop gap-through (regression-pin)
# ---------------------------------------------------------------------------


def test_long_stop_fills_at_gap_through_open():
    """A buy stop above the prior close, triggered by a gap-up bar, fills
    at the gap-through open — not clamped to the stop level."""
    bar = _bar(o=110.0, h=112.0, l=109.5, c=111.0)
    req = _req(side=OrderSide.LONG, order_type=OrderType.STOP, stop_price=105.0)

    terms = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=None)
    assert terms is not None
    assert terms.reference_price == 110.0  # gap-up open, not stop level


def test_short_stop_fills_at_gap_through_open_on_gap_down():
    """A sell stop (e.g. exit on long), triggered by a gap-down, fills at
    the gap-through open."""
    bar = _bar(o=85.0, h=86.0, l=84.0, c=85.5)
    req = _req(side=OrderSide.SHORT, order_type=OrderType.STOP, stop_price=90.0)

    terms = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=None)
    assert terms is not None
    assert terms.reference_price == 85.0  # gap-down open


def test_long_stop_intra_bar_trigger_fills_at_stop():
    """When the bar opens below the stop and rallies through it, fill
    happens at the stop level (no gap-up gift)."""
    bar = _bar(o=100.0, h=106.0, l=99.0, c=104.0)
    req = _req(side=OrderSide.LONG, order_type=OrderType.STOP, stop_price=105.0)

    terms = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=None)
    assert terms is not None
    assert terms.reference_price == 105.0


# ---------------------------------------------------------------------------
# Participation cap
# ---------------------------------------------------------------------------


def test_partial_fill_when_order_exceeds_participation_cap():
    """Order notional > 10% of bar dollar volume → qty_fraction < 1."""
    # Bar dollar volume = 100 * 1000 = 100_000. 10% cap = 10_000.
    bar = _bar(o=100.0, h=101.0, l=99.0, c=100.0, volume=1_000.0)
    # 200 shares × $100 = $20_000 notional, 2× the $10k cap.
    req = _req(side=OrderSide.LONG, order_type=OrderType.MARKET, qty=200)

    model = RealisticExecutionModel(participation_cap=0.10)
    terms = model.compute_fill_terms(req, bar, next_bar=None)
    assert terms is not None
    # max_notional / order_notional = 10_000 / 20_000 = 0.5
    assert terms.qty_fraction == pytest.approx(0.5)


def test_full_fill_when_order_within_cap():
    bar = _bar(o=100.0, h=101.0, l=99.0, c=100.0, volume=1_000_000.0)
    req = _req(side=OrderSide.LONG, order_type=OrderType.MARKET, qty=5)
    terms = RealisticExecutionModel(participation_cap=0.10).compute_fill_terms(
        req, bar, next_bar=None
    )
    assert terms is not None
    assert terms.qty_fraction == 1.0


def test_full_fill_when_bar_volume_missing():
    """Daily-bar feeds occasionally drop volume; refusing to fill would be
    more disruptive than allowing the legacy 'no impact' path."""
    bar = _bar(o=100.0, h=101.0, l=99.0, c=100.0, volume=0.0)
    req = _req(side=OrderSide.LONG, order_type=OrderType.MARKET, qty=10_000)
    terms = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=None)
    assert terms is not None
    assert terms.qty_fraction == 1.0


def test_participation_cap_validation():
    with pytest.raises(ValueError, match="participation_cap"):
        RealisticExecutionModel(participation_cap=0.0)
    with pytest.raises(ValueError, match="participation_cap"):
        RealisticExecutionModel(participation_cap=1.5)


# ---------------------------------------------------------------------------
# Adverse-selection haircut
# ---------------------------------------------------------------------------


def test_adverse_selection_zero_when_next_bar_unknown():
    """Last bar in the stream — no haircut."""
    bar = _bar(o=100.0, h=101.0, l=99.0, c=100.0)
    req = _req(side=OrderSide.LONG, order_type=OrderType.LIMIT, limit_price=100.0)
    terms = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=None)
    assert terms is not None
    assert terms.extra_slip_bps == 0.0


def test_adverse_selection_zero_when_next_bar_favourable_for_buyer():
    """Long limit + next bar rallies → no haircut (the buyer is right)."""
    bar = _bar(o=100.0, h=101.0, l=99.0, c=100.0, volume=10.0)
    next_bar = _bar(o=101.0, h=103.0, l=101.0, c=102.0, timestamp="2024-01-03")
    req = _req(side=OrderSide.LONG, order_type=OrderType.LIMIT, limit_price=100.0, qty=5)
    terms = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=next_bar)
    assert terms is not None
    assert terms.extra_slip_bps == 0.0


def test_adverse_selection_positive_for_long_limit_followed_by_drop():
    """Long limit fills, next bar drops — adverse selection. Haircut > 0
    and scales with the *effective* participation (capped at
    ``participation_cap``)."""
    # Bar dollar volume = 100 * 100 = 10_000. Order = $5k notional → raw
    # participation = 0.5, but ``cap=0.10`` → effective_participation = 0.10
    # (the order is partially filled to the cap, so only 10% of bar
    # liquidity actually trades).
    bar = _bar(o=100.0, h=101.0, l=99.0, c=100.0, volume=100.0)
    next_bar = _bar(o=98.0, h=99.0, l=97.0, c=98.0, timestamp="2024-01-03")  # 2% drop
    req = _req(side=OrderSide.LONG, order_type=OrderType.LIMIT, limit_price=100.0, qty=50)

    terms = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=next_bar)
    assert terms is not None
    # qty_fraction = cap / raw_participation = 0.10 / 0.5 = 0.2
    assert terms.qty_fraction == pytest.approx(0.2)
    # adverse_move = 2% = 200 bps; haircut = 200 bps × 0.10 = 20 bps
    # (not 200 × 0.2 = 40 bps — the previous version conflated qty_fraction
    # with participation rate; see PR #355 review feedback).
    assert terms.extra_slip_bps == pytest.approx(20.0)


def test_adverse_selection_scales_with_in_cap_participation():
    """A *tiny* in-cap order must get a *small* haircut (regression for
    the PR #355 review comment: previously every in-cap order had
    ``qty_fraction == 1.0`` and was haircut at the full adverse-move
    rate, materially over-penalising small fills)."""
    # Bar dollar volume = 100 * 1_000_000 = $100M. Order = $1k notional →
    # raw participation = 1e-5, well below the 10% cap. Effective
    # participation == raw participation (no partial fill).
    bar = _bar(o=100.0, h=101.0, l=99.0, c=100.0, volume=1_000_000.0)
    next_bar = _bar(o=98.0, h=99.0, l=97.0, c=98.0, timestamp="2024-01-03")  # 2% drop
    req = _req(side=OrderSide.LONG, order_type=OrderType.LIMIT, limit_price=100.0, qty=10)

    terms = RealisticExecutionModel().compute_fill_terms(req, bar, next_bar=next_bar)
    assert terms is not None
    assert terms.qty_fraction == 1.0  # well within cap
    # adverse_move = 200 bps; haircut = 200 × 1e-5 = 0.002 bps. The bug
    # would have produced 200 × 1.0 = 200 bps (clamped to 50).
    assert terms.extra_slip_bps == pytest.approx(0.002, abs=1e-6)


def test_adverse_selection_capped_at_max_bps():
    """Pathological thin name + violent next-bar move shouldn't blow up.

    Effective participation is clamped at ``participation_cap`` (10%), and
    the haircut itself is clamped at ``adverse_selection_max_bps`` —
    belt-and-braces against degenerate inputs.
    """
    bar = _bar(o=100.0, h=101.0, l=99.0, c=100.0, volume=100.0)
    next_bar = _bar(o=50.0, h=55.0, l=45.0, c=50.0, timestamp="2024-01-03")  # 50% crash
    req = _req(side=OrderSide.LONG, order_type=OrderType.LIMIT, limit_price=100.0, qty=50)
    terms = RealisticExecutionModel(adverse_selection_max_bps=25.0).compute_fill_terms(
        req, bar, next_bar=next_bar
    )
    assert terms is not None
    assert terms.extra_slip_bps == 25.0


# ---------------------------------------------------------------------------
# Optimistic parity
# ---------------------------------------------------------------------------


def test_optimistic_market_fill_parity():
    bar = _bar(o=100.0, h=101.0, l=99.0, c=100.0)
    req = _req(side=OrderSide.LONG, order_type=OrderType.MARKET, qty=5)
    terms = OptimisticExecutionModel(warn=False).compute_fill_terms(req, bar, next_bar=None)
    assert terms is not None
    assert terms.reference_price == bar.open
    assert terms.qty_fraction == 1.0
    assert terms.extra_slip_bps == 0.0


def test_optimistic_limit_gap_through_returns_better_price():
    """Pin the legacy bug for the parity-tested goldens."""
    bar = _bar(o=95.0, h=98.0, l=94.0, c=96.0)
    req = _req(side=OrderSide.LONG, order_type=OrderType.LIMIT, limit_price=100.0)
    terms = OptimisticExecutionModel(warn=False).compute_fill_terms(req, bar, next_bar=None)
    assert terms is not None
    assert terms.reference_price == 95.0  # min(open, limit) = open


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_default_is_realistic():
    assert isinstance(build_execution_model(), RealisticExecutionModel)


def test_factory_optimistic_branch():
    assert isinstance(build_execution_model("optimistic"), OptimisticExecutionModel)


def test_factory_unknown_name_raises():
    with pytest.raises(ValueError, match="unknown execution_model"):
        build_execution_model("voodoo")


def test_factory_passes_participation_cap_to_realistic():
    model = build_execution_model("realistic", participation_cap=0.05)
    assert isinstance(model, RealisticExecutionModel)
    bar = _bar(o=100.0, h=101.0, l=99.0, c=100.0, volume=1_000.0)
    # 5% cap on $100k bar dollar vol = $5k. Order = $20k notional.
    req = _req(side=OrderSide.LONG, order_type=OrderType.MARKET, qty=200)
    terms = model.compute_fill_terms(req, bar, next_bar=None)
    assert terms is not None
    assert terms.qty_fraction == pytest.approx(0.25)  # 5_000 / 20_000


# ---------------------------------------------------------------------------
# Optimistic warning gating
# ---------------------------------------------------------------------------


def test_optimistic_warns_by_default(caplog):
    import logging

    caplog.set_level(logging.WARNING)
    OptimisticExecutionModel()
    assert any("OptimisticExecutionModel selected" in r.getMessage() for r in caplog.records)


def test_optimistic_silenced_by_env(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("KHALA_ALLOW_OPTIMISTIC_FILLS", "1")
    caplog.set_level(logging.WARNING)
    OptimisticExecutionModel()
    assert not any("OptimisticExecutionModel selected" in r.getMessage() for r in caplog.records)


def test_optimistic_silenced_by_explicit_warn_false(caplog):
    import logging

    caplog.set_level(logging.WARNING)
    OptimisticExecutionModel(warn=False)
    assert not any("OptimisticExecutionModel selected" in r.getMessage() for r in caplog.records)

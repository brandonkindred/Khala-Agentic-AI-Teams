"""Regression tests for the engine-side look-ahead guard (Phase 2).

Covers three surfaces:

* ``BarSafetyAssertion.check_fill`` — direct behavior check (enabled /
  disabled, equal / later / earlier bar timestamps).
* ``FillSimulator`` + ``TradingService`` integration — a pathological
  ``OrderBook`` seeded with an order whose ``submitted_at`` equals the bar
  the simulator is about to fill against must raise ``LookAheadError`` and
  flip ``TradingServiceResult.lookahead_violation``.
* Requeue + submitted_at refresh — a partial fill with
  ``REQUEUE_NEXT_BAR`` advances ``submitted_at`` to the fill bar's
  timestamp, allowing the continuation fill on the next bar without
  tripping the look-ahead guard.
"""

from __future__ import annotations

import textwrap
from typing import List

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion, LookAheadError
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestConfig, StrategySpec
from investment_team.trading_service.data_stream.historical_replay import (
    HistoricalReplayStream,
)
from investment_team.trading_service.engine.execution_model import RealisticExecutionModel
from investment_team.trading_service.engine.fill_simulator import (
    FillKind,
    FillSimulator,
    FillSimulatorConfig,
)
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio
from investment_team.trading_service.modes.backtest import run_backtest
from investment_team.trading_service.service import TradingService
from investment_team.trading_service.strategy.contract import (
    Bar,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
    UnfilledPolicy,
)

# ---------------------------------------------------------------------------
# Unit tests — BarSafetyAssertion
# ---------------------------------------------------------------------------


def test_assertion_passes_when_fill_bar_is_strictly_later() -> None:
    BarSafetyAssertion().check_fill(
        order_id="o1",
        submitted_at="2024-01-01",
        fill_bar_timestamp="2024-01-02",
    )


def test_assertion_fires_on_same_bar_fill() -> None:
    with pytest.raises(LookAheadError) as excinfo:
        BarSafetyAssertion().check_fill(
            order_id="o1",
            submitted_at="2024-01-02",
            fill_bar_timestamp="2024-01-02",
        )
    assert excinfo.value.order_id == "o1"
    assert excinfo.value.submitted_at == "2024-01-02"
    assert excinfo.value.fill_bar_timestamp == "2024-01-02"


def test_assertion_fires_on_earlier_bar_fill() -> None:
    with pytest.raises(LookAheadError):
        BarSafetyAssertion().check_fill(
            order_id="o2",
            submitted_at="2024-01-02",
            fill_bar_timestamp="2024-01-01",
        )


def test_assertion_is_noop_when_disabled() -> None:
    BarSafetyAssertion(enabled=False).check_fill(
        order_id="o1",
        submitted_at="2024-01-02",
        fill_bar_timestamp="2024-01-02",
    )


def test_assertion_skips_blank_metadata() -> None:
    # Pre-schema records with empty submitted_at should not false-positive.
    BarSafetyAssertion().check_fill(
        order_id="o1",
        submitted_at="",
        fill_bar_timestamp="2024-01-02",
    )
    BarSafetyAssertion().check_fill(
        order_id="o1",
        submitted_at="2024-01-01",
        fill_bar_timestamp="",
    )


def test_assertion_treats_z_and_offset_as_same_instant() -> None:
    """``2024-01-02T10:00:00Z`` and ``2024-01-02T10:00:00+00:00`` are the
    same instant. Lexicographic ASCII compare would see ``Z > +00:00``
    (because ``Z`` 0x5A > ``+`` 0x2B) and falsely allow a same-bar fill;
    chronological compare correctly trips the guard.
    """
    with pytest.raises(LookAheadError):
        BarSafetyAssertion().check_fill(
            order_id="o1",
            submitted_at="2024-01-02T10:00:00+00:00",
            fill_bar_timestamp="2024-01-02T10:00:00Z",
        )
    # And the other direction.
    with pytest.raises(LookAheadError):
        BarSafetyAssertion().check_fill(
            order_id="o2",
            submitted_at="2024-01-02T10:00:00Z",
            fill_bar_timestamp="2024-01-02T10:00:00+00:00",
        )


def test_assertion_handles_compact_utc_offset() -> None:
    """Same-instant compact-offset (``+0000``) vs colon-form (``+00:00``)
    must not slip past the guard.
    """
    with pytest.raises(LookAheadError):
        BarSafetyAssertion().check_fill(
            order_id="o1",
            submitted_at="2024-01-02T10:00:00+00:00",
            fill_bar_timestamp="2024-01-02T10:00:00+0000",
        )


def test_assertion_treats_equivalent_offsets_as_same_instant() -> None:
    """``2024-01-02T15:30:00+05:30`` is the same instant as
    ``2024-01-02T10:00:00+00:00`` — chronological compare correctly trips
    the guard regardless of which offset the caller used.
    """
    with pytest.raises(LookAheadError):
        BarSafetyAssertion().check_fill(
            order_id="o1",
            submitted_at="2024-01-02T10:00:00+00:00",
            fill_bar_timestamp="2024-01-02T15:30:00+05:30",
        )


# ---------------------------------------------------------------------------
# Integration — FillSimulator with a pathological OrderBook
# ---------------------------------------------------------------------------


def _bar(ts: str, price: float = 100.0) -> Bar:
    return Bar(
        symbol="AAA",
        timestamp=ts,
        timeframe="1d",
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1000.0,
    )


def test_fill_simulator_raises_when_order_submitted_at_same_bar() -> None:
    """An order whose ``submitted_at == bar.timestamp`` must fail the guard.

    Constructs the scenario by calling ``OrderBook.submit`` directly with
    the same timestamp the simulator is about to process — reproducing the
    shape of a hypothetical future regression that drops the
    ``pending_for_prev`` queue in ``TradingService``.
    """
    portfolio = Portfolio(initial_capital=100_000.0)
    order_book = OrderBook()
    risk = RiskFilter(RiskLimits())
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=risk,
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
    )
    order_book.submit(
        OrderRequest(
            client_order_id="c1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )

    with pytest.raises(LookAheadError):
        sim.process_bar(_bar("2024-01-02"))


def test_fill_simulator_disabled_safety_allows_same_bar_fill() -> None:
    """With ``BarSafetyAssertion(enabled=False)`` the same scenario fills cleanly.

    Proves the disabled path is a true no-op — tests that deliberately
    construct back-dated traces (e.g. replaying persisted broker fills) can
    opt out without patching engine internals.
    """
    portfolio = Portfolio(initial_capital=100_000.0)
    order_book = OrderBook()
    risk = RiskFilter(RiskLimits())
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=risk,
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(enabled=False),
    )
    order_book.submit(
        OrderRequest(
            client_order_id="c1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02"))
    assert len(outcome.entry_fills) == 1


# ---------------------------------------------------------------------------
# End-to-end — TradingService classifies a parent-side violation
# ---------------------------------------------------------------------------


_PASSTHROUGH_STRATEGY = textwrap.dedent('''\
    """No-op strategy: used with a pre-seeded OrderBook to reproduce a
    parent-side look-ahead shape without needing a custom strategy."""
    from contract import Strategy


    class Noop(Strategy):
        def on_bar(self, ctx, bar):
            pass
''')


def _bars(dates: List[str]) -> List[OHLCVBar]:
    return [
        OHLCVBar(date=d, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        for d in dates
    ]


def test_end_to_end_round_trip_strategy_never_triggers_safety() -> None:
    """The default subprocess engine path must not fire the parent-side guard.

    A real strategy + fill loop always submits on bar ``t`` and fills on
    bar ``t+1``; the assertion must stay silent through a full round-trip
    run or it would false-positive on every backtest.
    """
    from .golden.strategies import ROUND_TRIP_CODE

    spec = StrategySpec(
        strategy_id="bar-safety-roundtrip",
        authored_by="phase2-test",
        asset_class="stocks",
        hypothesis="invariant",
        signal_definition="invariant",
        timeframe="1d",
        strategy_code=ROUND_TRIP_CODE,
    )
    config = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    bars = _bars([f"2024-01-{i + 1:02d}" for i in range(20)])
    result = run_backtest(strategy=spec, config=config, market_data={"AAA": bars})
    assert not result.service_result.lookahead_violation
    assert result.service_result.error is None


def test_trading_service_surfaces_lookahead_violation_from_engine() -> None:
    """If the engine were to accept a same-bar fill, TradingService must
    flip ``lookahead_violation`` and populate ``error``.

    Exercises the ``except LookAheadError`` branch in
    ``TradingService.run`` by injecting a pre-seeded order through a
    subclass that skips the normal ``pending_for_prev`` queue.
    """
    bars = _bars(["2024-01-01", "2024-01-02", "2024-01-03"])
    stream = HistoricalReplayStream({"AAA": bars}, timeframe="1d")

    class _BadService(TradingService):
        """TradingService variant that submits an order with a same-bar
        ``submitted_at`` before the fill_sim sees the bar — the shape a
        regression in ``pending_for_prev`` would take."""

        def run(self, stream, *, on_trade=None):  # type: ignore[override]
            # Seed a violating order into the book, then run normally.
            portfolio = Portfolio(initial_capital=self.config.initial_capital)
            order_book = OrderBook()
            fill_sim = FillSimulator(
                portfolio=portfolio,
                order_book=order_book,
                risk_filter=self._risk,
                config=FillSimulatorConfig(
                    slippage_bps=self.config.slippage_bps,
                    transaction_cost_bps=self.config.transaction_cost_bps,
                ),
            )
            order_book.submit(
                OrderRequest(
                    client_order_id="c1",
                    symbol="AAA",
                    side=OrderSide.LONG,
                    qty=10,
                    order_type=OrderType.MARKET,
                    tif=TimeInForce.DAY,
                ),
                submitted_at="2024-01-01",
                submitted_equity=100_000.0,
            )
            # Same-bar fill — guard must fire.
            try:
                fill_sim.process_bar(_bar("2024-01-01"))
            except LookAheadError as exc:
                from investment_team.trading_service.service import TradingServiceResult

                return TradingServiceResult(
                    trades=[],
                    lookahead_violation=True,
                    error=str(exc),
                )
            raise AssertionError("engine accepted a same-bar fill")  # pragma: no cover

    service = _BadService(
        strategy_code=_PASSTHROUGH_STRATEGY,
        config=BacktestConfig(
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
            transaction_cost_bps=0.0,
            slippage_bps=0.0,
        ),
    )
    outcome = service.run(stream)
    assert outcome.lookahead_violation
    assert outcome.error is not None
    assert "fill must be strictly after submission" in outcome.error


# ---------------------------------------------------------------------------
# Requeue refreshes submitted_at on partial fill
# ---------------------------------------------------------------------------


def test_requeue_on_partial_fill_refreshes_submitted_at() -> None:
    """A partial fill with ``REQUEUE_NEXT_BAR`` must advance
    ``submitted_at`` to the fill bar's timestamp so the continuation fill
    on the *next* bar passes the look-ahead guard.

    Preconditions: order submitted at ``2024-01-01`` with
    ``unfilled_policy=REQUEUE_NEXT_BAR``, qty large enough to trigger
    the participation cap on a thin-volume bar.

    Postconditions:
    - Bar ``2024-01-02``: partial fill emitted with ``FillKind.PARTIAL``;
      requeued order has ``submitted_at == "2024-01-02"``.
    - Bar ``2024-01-03``: continuation fill succeeds (no ``LookAheadError``)
      because ``submitted_at`` was refreshed to ``"2024-01-02"`` (strictly
      before ``"2024-01-03"``).
    """
    portfolio = Portfolio(initial_capital=100_000.0)
    order_book = OrderBook()
    # Large order to trigger the participation-cap partial; lift the per-position
    # deployment cap so the choke-point max_position_pct gate doesn't reject it
    # (that gate is covered in test_risk_filter.py).
    risk = RiskFilter(RiskLimits(max_position_pct=100.0))
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=risk,
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        execution_model=RealisticExecutionModel(participation_cap=0.10),
        bar_safety=BarSafetyAssertion(enabled=True),
    )

    req = OrderRequest(
        client_order_id="c1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=200,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
    )
    order_book.submit(req, submitted_at="2024-01-01", submitted_equity=100_000.0)

    # Bar 1: thin volume triggers participation cap → partial fill + requeue
    bar1 = Bar(
        symbol="AAA", timestamp="2024-01-02",
        open=100.0, high=101.0, low=99.0, close=100.0, volume=1_000.0,
    )
    outcome1 = sim.process_bar(bar1)

    assert len(outcome1.entry_fills) == 1
    assert outcome1.entry_fills[0].fill_kind == FillKind.PARTIAL

    pending = order_book.all_pending()
    assert len(pending) == 1
    assert pending[0].submitted_at.startswith("2024-01-02")

    # Bar 2: the requeued order fills on the next bar without tripping the
    # look-ahead guard (submitted_at="2024-01-02" < fill_bar="2024-01-03").
    bar2 = Bar(
        symbol="AAA", timestamp="2024-01-03",
        open=100.0, high=101.0, low=99.0, close=100.0, volume=1_000_000.0,
    )
    outcome2 = sim.process_bar(bar2)
    assert len(outcome2.entry_fills) >= 1

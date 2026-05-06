"""FillSimulator diagnostic-event tests for #410.

Covers the lifecycle/rejection events surfaced via
``FillOutcome.diagnostic_events``:

- ``entry_filled`` and ``exit_filled`` lifecycle events on success.
- ``rejected`` events for ``zero_fill_qty``, ``risk_gate:<reason>``,
  ``insufficient_capital``, and ``same_side_order_ignored``.
- End-to-end propagation through ``TradingService`` so refinement-loop
  callers see the events on ``BacktestExecutionDiagnostics``.
"""

from __future__ import annotations

import textwrap
from typing import Dict, List

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestConfig, StrategySpec
from investment_team.trading_service.engine.execution_model import (
    FillTerms,
    OptimisticExecutionModel,
    RealisticExecutionModel,
)
from investment_team.trading_service.engine.fill_simulator import (
    FillSimulator,
    FillSimulatorConfig,
)
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio
from investment_team.trading_service.modes.backtest import run_backtest
from investment_team.trading_service.strategy.contract import (
    Bar,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)

# ---------------------------------------------------------------------------
# Direct FillSimulator fixtures — exercise each rejection branch in isolation
# ---------------------------------------------------------------------------


def _bar(ts: str, *, price: float = 100.0, volume: float = 1_000_000.0) -> Bar:
    return Bar(
        symbol="AAA",
        timestamp=ts,
        timeframe="1d",
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=volume,
    )


class _ZeroQtyExecutionModel:
    """Forces ``qty_fraction=0.0`` so ``_fill_entry`` lands in its
    no-liquidity / zero_fill_qty branch deterministically."""

    name = "zero_qty_test_model"

    def compute_fill_terms(self, req, bar, next_bar) -> FillTerms:  # type: ignore[no-untyped-def]
        return FillTerms(reference_price=bar.close, qty_fraction=0.0)


def _make_simulator(
    *,
    initial_capital: float = 10_000_000.0,
    risk_limits: RiskLimits | None = None,
    realistic: bool = False,
    zero_qty: bool = False,
) -> tuple[FillSimulator, OrderBook, Portfolio]:
    portfolio = Portfolio(initial_capital=initial_capital)
    order_book = OrderBook()
    if zero_qty:
        execution_model = _ZeroQtyExecutionModel()
    elif realistic:
        execution_model = RealisticExecutionModel(participation_cap=0.10)
    else:
        execution_model = OptimisticExecutionModel(warn=False)
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(
            risk_limits or RiskLimits(max_position_pct=100, max_gross_leverage=10.0)
        ),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=execution_model,
    )
    return sim, order_book, portfolio


def _long(qty: float) -> OrderRequest:
    return OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=qty,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
    )


def _short(qty: float, *, client_order_id: str = "exit-1") -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=qty,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
    )


# ---------------------------------------------------------------------------
# entry_filled / exit_filled lifecycle events
# ---------------------------------------------------------------------------


def test_successful_entry_emits_entry_filled_event() -> None:
    """A clean entry produces an ``entry_filled`` event tagged with the bar."""
    sim, order_book, _ = _make_simulator()
    order_book.submit(
        _long(10),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02"))

    assert len(outcome.entry_fills) == 1
    assert len(outcome.diagnostic_events) == 1
    ev = outcome.diagnostic_events[0]
    assert ev.kind == "entry_filled"
    assert ev.symbol == "AAA"
    assert ev.side == OrderSide.LONG.value
    assert ev.order_type == OrderType.MARKET.value
    assert ev.timestamp == "2024-01-02"


def test_successful_exit_emits_exit_filled_event() -> None:
    """A clean exit against an open position produces an ``exit_filled`` event."""
    sim, order_book, _ = _make_simulator()
    order_book.submit(
        _long(10),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02"))

    order_book.submit(
        _short(10),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )
    outcome = sim.process_bar(_bar("2024-01-03", price=105.0))

    assert len(outcome.exit_fills) == 1
    exit_events = [e for e in outcome.diagnostic_events if e.kind == "exit_filled"]
    assert len(exit_events) == 1
    assert exit_events[0].symbol == "AAA"
    assert exit_events[0].side == OrderSide.SHORT.value


# ---------------------------------------------------------------------------
# Rejection events
# ---------------------------------------------------------------------------


def test_zero_fill_qty_emits_rejected_event() -> None:
    """A bar with no liquidity yields a REJECTED Fill *and* a ``zero_fill_qty``
    rejection event so downstream consumers can categorize the failure."""
    # ``_ZeroQtyExecutionModel`` deterministically forces ``qty_fraction=0``
    # so the engine takes the no-liquidity branch in ``_fill_entry``.
    sim, order_book, _ = _make_simulator(zero_qty=True)
    order_book.submit(
        _long(10),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02"))

    assert len(outcome.entry_fills) == 1
    fill = outcome.entry_fills[0]
    assert fill.qty == 0.0  # REJECTED Fill
    assert fill.fill_kind.value == "rejected"
    rejections = [e for e in outcome.diagnostic_events if e.kind == "rejected"]
    assert len(rejections) == 1
    assert rejections[0].reason == "zero_fill_qty"


def test_risk_gate_emits_rejected_event_with_prefix() -> None:
    """A risk-gate rejection emits a ``risk_gate:<reason>`` event and no Fill."""
    # max_position_pct=0.001 trips the symbol-concentration gate on any
    # meaningful notional → deterministic gate rejection without poisoning
    # ``max_open_positions`` (which is bounded ≥ 1).
    sim, order_book, _ = _make_simulator(
        risk_limits=RiskLimits(
            max_position_pct=0.001,
            max_gross_leverage=10.0,
            max_symbol_concentration_pct=0.001,
        ),
    )
    order_book.submit(
        _long(10),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02"))

    assert outcome.entry_fills == []
    rejections = [e for e in outcome.diagnostic_events if e.kind == "rejected"]
    assert len(rejections) == 1
    assert rejections[0].reason.startswith("risk_gate:")


def test_insufficient_capital_emits_rejected_event() -> None:
    """When portfolio capital falls below the order notional, the entry is
    rejected with ``insufficient_capital`` (no Fill emitted).

    The risk gate's concentration check trips before the capital check
    whenever notional exceeds available equity, so we first soak up most
    of capital with a clean entry on one symbol, then submit a second
    entry on a *different* symbol whose notional fits inside the equity
    envelope (passes concentration/leverage) but exceeds remaining cash.
    """
    sim, order_book, portfolio = _make_simulator(
        initial_capital=1_000.0,
        risk_limits=RiskLimits(
            max_position_pct=100,
            max_gross_leverage=100.0,
            max_symbol_concentration_pct=100.0,
        ),
    )
    # 1) Open AAA at $100 × 9 = $900 notional, leaving $100 capital and
    #    ~$1_000 equity once the position is marked.
    order_book.submit(
        OrderRequest(
            client_order_id="aaa-entry",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=9,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-01",
        submitted_equity=1_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0))
    assert portfolio.capital == pytest.approx(100.0, abs=0.01)

    # 2) Submit BBB for 5 × $100 = $500 notional. Concentration = 50%,
    #    leverage = (900 + 500)/1000 = 1.4 — both well under limits — but
    #    capital ($100) < notional ($500) → ``insufficient_capital``.
    order_book.submit(
        OrderRequest(
            client_order_id="bbb-entry",
            symbol="BBB",
            side=OrderSide.LONG,
            qty=5,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-02",
        submitted_equity=portfolio.mark_to_market(),
    )
    bbb_bar = Bar(
        symbol="BBB",
        timestamp="2024-01-03",
        timeframe="1d",
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1_000_000,
    )
    outcome = sim.process_bar(bbb_bar)

    assert outcome.entry_fills == []
    rejections = [e for e in outcome.diagnostic_events if e.kind == "rejected"]
    assert len(rejections) == 1
    assert rejections[0].reason == "insufficient_capital"
    assert rejections[0].symbol == "BBB"


def test_ioc_no_trigger_emits_rejected_event() -> None:
    """An IOC order whose limit price never crosses on the next bar is
    cancelled cancel-on-this-bar, with a ``ioc_no_trigger`` rejection event
    so the zero-trade refinement loop can see the failure category."""
    sim, order_book, _ = _make_simulator()
    # IOC LIMIT at $50 (far below market) → ExecutionModel returns None
    # because the limit price doesn't cross the next bar's price action.
    # The IOC pre-empts DAY/GTC behaviour and rejects on this bar.
    order_book.submit(
        OrderRequest(
            client_order_id="ioc-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=5,
            order_type=OrderType.LIMIT,
            limit_price=50.0,
            tif=TimeInForce.IOC,
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02", price=100.0))

    rejections = [e for e in outcome.diagnostic_events if e.kind == "rejected"]
    assert len(rejections) == 1
    assert rejections[0].reason == "ioc_no_trigger"


def test_fok_partial_emits_rejected_event() -> None:
    """A FOK order that can only partially fill (participation cap clipped
    below 100%) is rejected outright with a ``fok_partial`` event."""
    sim, order_book, _ = _make_simulator(realistic=True)
    # Same shape as the participation-cap math at the top of test_partial_fills:
    # qty=2_000 against a low-volume bar drives qty_fraction=0.5; FOK rejects.
    order_book.submit(
        OrderRequest(
            client_order_id="fok-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=2_000,
            order_type=OrderType.MARKET,
            tif=TimeInForce.FOK,
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))

    rejections = [e for e in outcome.diagnostic_events if e.kind == "rejected"]
    assert len(rejections) == 1
    assert rejections[0].reason == "fok_partial"
    # The REJECTED Fill is still emitted on the entry side.
    assert len(outcome.entry_fills) == 1
    assert outcome.entry_fills[0].fill_kind.value == "rejected"


def test_same_side_addon_emits_rejected_event() -> None:
    """Submitting another long against an already-open long position is
    silently dropped at the order book; the event surfaces the suppression."""
    sim, order_book, _ = _make_simulator()
    # Open a long position via a clean entry on bar 1.
    order_book.submit(
        _long(10),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02"))

    # Second long order against the existing long → same-side suppression.
    addon = OrderRequest(
        client_order_id="addon-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=5,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
    )
    order_book.submit(addon, submitted_at="2024-01-02", submitted_equity=10_000_000.0)
    outcome = sim.process_bar(_bar("2024-01-03"))

    assert outcome.entry_fills == []
    assert outcome.exit_fills == []
    rejections = [e for e in outcome.diagnostic_events if e.kind == "rejected"]
    assert len(rejections) == 1
    assert rejections[0].reason == "same_side_order_ignored"


# ---------------------------------------------------------------------------
# End-to-end TradingService propagation — events land on diagnostics
# ---------------------------------------------------------------------------


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-02-15",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )


def _uptrend_then_down_bars(symbol_bars: Dict[str, List[OHLCVBar]]) -> None:
    bars: List[OHLCVBar] = []
    base = 100.0
    for i in range(15):
        price = base + i * 2.0
        bars.append(_mkbar(i + 1, price))
    for i in range(15):
        price = (base + 28.0) - (i + 1) * 2.5
        bars.append(_mkbar(16 + i, price))
    symbol_bars["AAA"] = bars


def _mkbar(day_of_month: int, close: float) -> OHLCVBar:
    month = 1 if day_of_month <= 31 else 2
    day = day_of_month if month == 1 else day_of_month - 31
    return OHLCVBar(
        date=f"2024-{month:02d}-{day:02d}",
        open=close - 0.2,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=1_000_000,
    )


_SMA_STRATEGY_CODE = textwrap.dedent('''\
    """Tiny SMA(5) crossover used to drive a clean round-trip."""
    from contract import OrderSide, OrderType, Strategy


    class SmaCrossover(Strategy):
        WINDOW = 5

        def on_bar(self, ctx, bar):
            history = ctx.history(bar.symbol, self.WINDOW)
            if len(history) < self.WINDOW:
                return
            sma = sum(b.close for b in history) / self.WINDOW
            pos = ctx.position(bar.symbol)
            if pos is None and bar.close > sma:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=10,
                    order_type=OrderType.MARKET,
                    reason="sma_cross_up",
                )
            elif pos is not None and bar.close < sma:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.SHORT,
                    qty=pos.qty,
                    order_type=OrderType.MARKET,
                    reason="sma_cross_down",
                )
''')


_DOUBLE_LONG_STRATEGY_CODE = textwrap.dedent('''\
    """Strategy that opens a long, then attempts another long on the next bar.

    Used to trigger the ``same_side_order_ignored`` rejection event from the
    fill simulator in an end-to-end TradingService run.
    """
    from contract import OrderSide, OrderType, Strategy


    class DoubleLongStrategy(Strategy):
        _emit_count = 0

        def on_bar(self, ctx, bar):
            if self._emit_count >= 2:
                return
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.MARKET,
                reason=f"emit-{self._emit_count}",
            )
            self._emit_count += 1
''')


def test_round_trip_propagates_entry_and_exit_filled_events() -> None:
    """End-to-end SMA round-trip surfaces both lifecycle events on diagnostics."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-410-roundtrip",
        authored_by="tests",
        asset_class="equity",
        hypothesis="round-trip",
        signal_definition="sma",
        entry_rules=["close > sma(5)"],
        exit_rules=["close < sma(5)"],
        strategy_code=_SMA_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    assert run.service_result.error is None, run.service_result.error
    assert diagnostics.entries_filled == 1
    event_types = [e.event_type for e in diagnostics.last_order_events]
    assert event_types.count("entry_filled") == 1
    assert event_types.count("exit_filled") == 1
    # Lifecycle events carry the bar timestamp, not just the order metadata.
    entry_ev = next(e for e in diagnostics.last_order_events if e.event_type == "entry_filled")
    assert entry_ev.timestamp is not None
    assert entry_ev.symbol == "AAA"


def test_same_side_addon_propagates_rejection_to_diagnostics() -> None:
    """A second long emitted while a long is open lands as a fill-side rejection."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-410-double-long",
        authored_by="tests",
        asset_class="equity",
        hypothesis="same-side suppression",
        signal_definition="double-long",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_DOUBLE_LONG_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    assert run.service_result.error is None, run.service_result.error
    assert diagnostics.entries_filled >= 1
    assert diagnostics.orders_rejection_reasons.get("same_side_order_ignored", 0) == 1
    rejected_events = [
        e
        for e in diagnostics.last_order_events
        if e.event_type == "rejected" and e.reason == "same_side_order_ignored"
    ]
    assert len(rejected_events) == 1


def test_fill_side_rejection_propagates_end_to_end() -> None:
    """End-to-end: a fill-side rejection (here, a risk-gate breach because
    the order's notional dwarfs equity) lands on diagnostics with the
    ``risk_gate:`` prefix and bumps ``orders_rejected``.

    This guards the wire-up across both sides of the run loop without
    depending on a specific gate reason — ``risk_gate``, ``insufficient_capital``,
    and ``zero_fill_qty`` all share the same ``_apply_fill_outcome_events``
    code path inside ``TradingService``.
    """
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    config = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-02-15",
        initial_capital=10.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    strategy = StrategySpec(
        strategy_id="strat-410-fill-reject",
        authored_by="tests",
        asset_class="equity",
        hypothesis="fill-side rejection",
        signal_definition="sma",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_SMA_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=config, market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    assert run.service_result.error is None, run.service_result.error
    assert run.trades == []
    # At least one fill-side rejection landed on the diagnostics envelope.
    assert diagnostics.orders_rejected >= 1
    rejected_events = [e for e in diagnostics.last_order_events if e.event_type == "rejected"]
    assert rejected_events
    assert any(e.reason.startswith("risk_gate:") for e in rejected_events), (
        f"expected at least one risk_gate rejection, got {rejected_events}"
    )
    # No entry ever filled, so the counter must still be zero.
    assert diagnostics.entries_filled == 0


@pytest.mark.parametrize("realistic", [True, False])
def test_diagnostic_events_default_to_empty_list(realistic: bool) -> None:
    """A no-order bar produces an empty ``diagnostic_events`` list."""
    sim, _, _ = _make_simulator(realistic=realistic)

    outcome = sim.process_bar(_bar("2024-01-02"))

    assert outcome.entry_fills == []
    assert outcome.exit_fills == []
    assert outcome.closed_trades == []
    assert outcome.diagnostic_events == []

"""Streaming equity buffer in ``TradingService.run`` (#430).

Covers:
* The streaming curve is populated on a successful run and skips the
  closed-trade-ledger reconstruction inside ``compute_performance_metrics``.
* Sub-daily timeframes collapse to one EOD entry per trading day (last MTM
  of the day wins).
* Early aborts (``harness.send_start`` failure) leave the curve unset.
* The chunked-bar protocol path produces the same curve as the per-bar path.
"""

from __future__ import annotations

import os
import textwrap
from datetime import date as date_cls
from typing import List
from unittest.mock import patch

import pytest

from investment_team.execution.metrics import (
    EquityCurve,
    compute_performance_metrics,
)
from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestConfig, StrategySpec
from investment_team.trading_service.data_stream.protocol import (
    BarEvent,
    EndOfStreamEvent,
)
from investment_team.trading_service.modes.backtest import run_backtest
from investment_team.trading_service.service import (
    TradingService,
    _StreamingEquityBuffer,
)
from investment_team.trading_service.strategy.contract import Bar

_NOOP_STRATEGY_CODE = textwrap.dedent('''\
    """No-op strategy: emits no orders, so the streaming curve is just cash MTM."""
    from contract import Strategy


    class NoopStrategy(Strategy):
        def on_bar(self, ctx, bar):
            return
''')


_BROKEN_START_CODE = textwrap.dedent('''\
    """Strategy that fails before any bars are processed."""
    from contract import Strategy


    class BrokenStartStrategy(Strategy):
        def on_start(self, ctx):
            raise RuntimeError("boom on start")

        def on_bar(self, ctx, bar):
            return
''')


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )


def _bar(symbol: str, ts: str, close: float = 100.0) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=ts,
        open=close - 0.1,
        high=close + 0.1,
        low=close - 0.2,
        close=close,
        volume=1_000_000,
    )


def test_streaming_equity_curve_populated_on_noop_run() -> None:
    """A no-op strategy across N daily bars yields N EOD samples at initial capital."""
    days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    # Pin config to the bar window so ``weekday_range`` matches the
    # bars exactly — keeps this test focused on the populate-on-noop
    # contract, not the gap-fill behavior covered by its own test.
    cfg = BacktestConfig(
        start_date=days[0],
        end_date=days[-1],
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    service = TradingService(strategy_code=_NOOP_STRATEGY_CODE, config=cfg)
    stream = [BarEvent(bar=_bar("AAA", d), is_warmup=False) for d in days]
    stream.append(EndOfStreamEvent())

    result = service.run(stream)

    assert result.error is None, result.error
    curve = result.streaming_equity_curve
    assert curve is not None
    assert curve.dates == [date_cls.fromisoformat(d) for d in days]
    # No trades, no positions → equity sits at initial capital every EOD.
    assert curve.equity == [100_000.0] * len(days)
    assert curve.initial_capital == 100_000.0


def test_streaming_equity_curve_subdaily_keeps_last_mtm_per_day() -> None:
    """Multiple intraday bars on a single trading day collapse to one EOD entry.

    Spec invariant: the *last* MTM value of each calendar day wins. With a
    no-op strategy holding no positions, every per-bar MTM equals initial
    capital, so the dict ends up with one entry per day regardless of how
    many intraday bars were processed.
    """
    # Pin config to the two-day intraday window so ``weekday_range``
    # doesn't add gap-fill days that aren't being exercised here.
    cfg = BacktestConfig(
        start_date="2024-01-02",
        end_date="2024-01-03",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    service = TradingService(strategy_code=_NOOP_STRATEGY_CODE, config=cfg)
    intraday_ts = [
        "2024-01-02T09:30:00",
        "2024-01-02T10:00:00",
        "2024-01-02T15:55:00",
        "2024-01-03T09:30:00",
        "2024-01-03T15:55:00",
    ]
    stream = [BarEvent(bar=_bar("AAA", ts), is_warmup=False) for ts in intraday_ts]
    stream.append(EndOfStreamEvent())

    result = service.run(stream)

    assert result.error is None
    curve = result.streaming_equity_curve
    assert curve is not None
    assert curve.dates == [date_cls(2024, 1, 2), date_cls(2024, 1, 3)]
    assert len(curve.equity) == 2


def test_compute_performance_metrics_skips_rebuild_when_curve_supplied() -> None:
    """Acceptance #2: passing ``equity_curve=`` bypasses ``build_equity_curve_from_trades``."""
    # A minimal closed-trade ledger so the ``not trades`` short-circuit
    # doesn't fire — the rebuild call we're guarding against lives on the
    # post-short-circuit path.
    from investment_team.models import TradeRecord

    trades: List[TradeRecord] = [
        TradeRecord(
            trade_num=1,
            symbol="AAA",
            side="long",
            entry_date="2024-01-02",
            exit_date="2024-01-05",
            entry_price=100.0,
            exit_price=101.0,
            shares=10.0,
            position_value=1_000.0,
            gross_pnl=10.0,
            net_pnl=10.0,
            return_pct=1.0,
            hold_days=3,
            outcome="win",
            cumulative_pnl=10.0,
        )
    ]
    streaming = EquityCurve(
        dates=[
            date_cls(2024, 1, 2),
            date_cls(2024, 1, 3),
            date_cls(2024, 1, 4),
            date_cls(2024, 1, 5),
        ],
        equity=[100_000.0, 100_000.0, 100_000.0, 100_010.0],
        initial_capital=100_000.0,
    )

    target = "investment_team.execution.metrics.build_equity_curve_from_trades"
    with patch(target) as rebuilt:
        compute_performance_metrics(trades, 100_000.0, equity_curve=streaming, risk_free_rate=0.0)
        rebuilt.assert_not_called()

    # Sanity: without the kwarg, the rebuild *is* called.
    with patch(
        target,
        wraps=__import__(
            "investment_team.execution.metrics", fromlist=["build_equity_curve_from_trades"]
        ).build_equity_curve_from_trades,
    ) as rebuilt:
        compute_performance_metrics(trades, 100_000.0, risk_free_rate=0.0)
        rebuilt.assert_called_once()


def test_streaming_equity_curve_none_on_send_start_failure() -> None:
    """Acceptance: aborts before any bar produce no curve (and don't crash)."""
    service = TradingService(strategy_code=_BROKEN_START_CODE, config=_config())

    result = service.run([EndOfStreamEvent()])

    assert result.error is not None
    assert result.streaming_equity_curve is None


def test_streaming_buffer_materialize_returns_none_when_empty() -> None:
    """The preallocated buffer materializes ``None`` when no bars were stamped.

    Mirrors the no-op-on-empty contract the old dict-based helper had: aborts
    before any bar produces ``streaming_equity_curve = None`` rather than an
    empty :class:`EquityCurve`.
    """
    buf = _StreamingEquityBuffer([date_cls(2024, 1, 2), date_cls(2024, 1, 3)], 100_000.0)
    assert buf.materialize() is None


def test_streaming_buffer_forward_fills_gap_weekdays() -> None:
    """Unfilled preallocated weekdays carry forward the last EOD equity.

    Regression test for a chatgpt-codex-connector review on #518: the
    streaming curve must cover every weekday in ``[start_date, end_date]``
    so it aligns with ``build_equity_curve_from_trades`` (which forward-
    fills cash through gaps). Without this, market-holiday days or
    missing-bar days were silently dropped from the streaming curve and
    ``compute_performance_metrics`` operated on a different date set than
    the reconstructed-from-trades path.
    """
    # Mon–Fri preallocated. Record only Mon, Wed, Fri (gaps on Tue, Thu).
    preallocated = [
        date_cls(2024, 1, 1),
        date_cls(2024, 1, 2),
        date_cls(2024, 1, 3),
        date_cls(2024, 1, 4),
        date_cls(2024, 1, 5),
    ]
    buf = _StreamingEquityBuffer(preallocated, 100_000.0)
    buf.record("2024-01-01", 100_100.0)
    buf.record("2024-01-03", 100_300.0)
    buf.record("2024-01-05", 100_500.0)

    curve = buf.materialize()
    assert curve is not None
    assert curve.dates == preallocated
    # Gap days (Tue, Thu) carry forward the previous EOD value.
    assert curve.equity == [100_100.0, 100_100.0, 100_300.0, 100_300.0, 100_500.0]


def test_streaming_buffer_carries_initial_capital_before_first_fill() -> None:
    """Weekdays before the first filled slot stamp ``initial_capital``."""
    preallocated = [
        date_cls(2024, 1, 1),
        date_cls(2024, 1, 2),
        date_cls(2024, 1, 3),
        date_cls(2024, 1, 4),
    ]
    buf = _StreamingEquityBuffer(preallocated, 100_000.0)
    # Start filling at Wed (the run effectively warms up through Mon-Tue).
    buf.record("2024-01-03", 100_500.0)
    buf.record("2024-01-04", 100_600.0)

    curve = buf.materialize()
    assert curve is not None
    assert curve.dates == preallocated
    assert curve.equity == [100_000.0, 100_000.0, 100_500.0, 100_600.0]


def test_streaming_buffer_overflow_carry_propagates_to_gap_weekday() -> None:
    """A weekend overflow bar must propagate its equity into a following
    gap weekday — otherwise the materialized curve moves backward at sort.

    Regression test for a chatgpt-codex-connector review on #518: with
    preallocated weekdays [Fri, Mon], a stamped Friday, weekend overflow
    on Sat/Sun, and *no* Monday bar, the old fix forward-filled Mon to
    the Friday value before merging overflow. After sorting, the curve
    read ``[Fri=100k, Sat=100.5k, Sun=100.8k, Mon=100k]`` — a backward
    jump on Monday that ``compute_performance_metrics`` would translate
    into a bogus negative return.
    """
    preallocated = [date_cls(2024, 1, 5), date_cls(2024, 1, 8)]  # Fri, Mon
    buf = _StreamingEquityBuffer(preallocated, 100_000.0)
    buf.record("2024-01-05", 100_000.0)  # Fri (in-range)
    buf.record("2024-01-06", 100_500.0)  # Sat (overflow)
    buf.record("2024-01-07", 100_800.0)  # Sun (overflow)
    # No Monday bar — Mon must carry forward from the most recent
    # *chronological* explicit sample (Sun=100_800.0), not from Fri.

    curve = buf.materialize()
    assert curve is not None
    assert curve.dates == [
        date_cls(2024, 1, 5),
        date_cls(2024, 1, 6),
        date_cls(2024, 1, 7),
        date_cls(2024, 1, 8),
    ]
    assert curve.equity == [100_000.0, 100_500.0, 100_800.0, 100_800.0]


def test_streaming_buffer_interleaves_overflow_chronologically() -> None:
    """Overflow dates (e.g. weekend crypto bars) merge into the curve in
    chronological position, not appended at the tail.

    Regression test for a chatgpt-codex-connector review on #518: the
    old materialize() emitted ``[Mon, Tue, Wed, Thu, Fri, Sat, Sun]`` →
    ``[Mon, Tue, Wed, Thu, Fri] + sorted([Sat, Sun])`` which is already
    chronological when the overflow lies *after* the in-range slice,
    but breaks when the overflow lies *between* two in-range days
    (e.g. a weekend bar between two weeks of weekday bars).
    """
    # Preallocate Fri Jan 5 + Mon Jan 8 + Tue Jan 9 (skipping the weekend).
    preallocated = [date_cls(2024, 1, 5), date_cls(2024, 1, 8), date_cls(2024, 1, 9)]
    buf = _StreamingEquityBuffer(preallocated, 100_000.0)

    # Stamp the in-range Friday and Tuesday, plus a Saturday that lands
    # in overflow (Sat Jan 6 is not in the preallocated weekday set).
    buf.record("2024-01-05", 100.0)
    buf.record("2024-01-06", 101.0)  # weekend → overflow
    buf.record("2024-01-08", 102.0)
    buf.record("2024-01-09", 103.0)

    curve = buf.materialize()
    assert curve is not None
    # All four samples must appear in chronological order, with the
    # Saturday slotted between Friday and the following Monday.
    assert curve.dates == [
        date_cls(2024, 1, 5),
        date_cls(2024, 1, 6),
        date_cls(2024, 1, 8),
        date_cls(2024, 1, 9),
    ]
    assert curve.equity == [100.0, 101.0, 102.0, 103.0]


def test_streaming_curve_matches_between_per_bar_and_chunked_paths() -> None:
    """Acceptance #3: per-bar and chunked replays produce the same EOD curve.

    Same strategy, same fixture, switched only by ``BAR_CHUNK_SIZE``.
    """
    bars = [
        OHLCVBar(
            date=f"2024-01-{i + 1:02d}",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10_000.0,
        )
        for i in range(10)
    ]
    spec = StrategySpec(
        strategy_id="streaming-curve-chunked-parity",
        authored_by="430-test",
        asset_class="stocks",
        hypothesis="parity",
        signal_definition="noop",
        strategy_code=_NOOP_STRATEGY_CODE,
    )
    cfg = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )

    def _run(chunk_size: str) -> EquityCurve:
        prev = os.environ.get("BAR_CHUNK_SIZE")
        os.environ["BAR_CHUNK_SIZE"] = chunk_size
        try:
            res = run_backtest(strategy=spec, config=cfg, market_data={"AAA": bars})
        finally:
            if prev is None:
                os.environ.pop("BAR_CHUNK_SIZE", None)
            else:
                os.environ["BAR_CHUNK_SIZE"] = prev
        curve = res.service_result.streaming_equity_curve
        assert curve is not None, "streaming curve should populate under both paths"
        return curve

    per_bar = _run("1")
    chunked = _run("4")

    assert per_bar.dates == chunked.dates
    assert per_bar.equity == pytest.approx(chunked.equity, rel=0, abs=1e-9)
    assert per_bar.initial_capital == chunked.initial_capital


def test_streaming_buffer_matches_reconstructed_curve_byte_for_byte() -> None:
    """Acceptance for #378: streaming buffer == ``build_equity_curve_from_trades``.

    With a no-op strategy and zero costs, the streaming MTM curve and the
    reconstructed-from-trades curve both reduce to ``[initial_capital] * D``
    over the same weekday set, so equality is exact — not approximate. The
    ``np.float64`` slot writes in ``_StreamingEquityBuffer`` produce the same
    Python ``float`` values as the old ``dict`` path, so this catches any
    future drift in how the buffer materializes its payload.

    The bar fixture is restricted to weekdays so the streaming buffer's
    overflow path (weekend dates → tail dict) doesn't fire; both curves
    align on the same weekday set within the config window.
    """
    from investment_team.execution.metrics import (
        build_equity_curve_from_trades,
        weekday_range,
    )

    cfg = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    weekdays = weekday_range(
        date_cls.fromisoformat(cfg.start_date),
        date_cls.fromisoformat(cfg.end_date),
    )
    bars = [
        OHLCVBar(
            date=d.isoformat(),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10_000.0,
        )
        for d in weekdays
    ]
    spec = StrategySpec(
        strategy_id="streaming-parity-378",
        authored_by="378-test",
        asset_class="stocks",
        hypothesis="parity",
        signal_definition="noop",
        strategy_code=_NOOP_STRATEGY_CODE,
    )

    res = run_backtest(strategy=spec, config=cfg, market_data={"AAA": bars})
    streaming = res.service_result.streaming_equity_curve
    assert streaming is not None, "streaming curve must populate for a successful no-op run"

    reconstructed = build_equity_curve_from_trades(
        res.trades,
        cfg.initial_capital,
        start_date=cfg.start_date,
        end_date=cfg.end_date,
    )

    # Byte-for-byte equality on the full weekday set: same dates in the
    # same order, exact-float equity (no ``pytest.approx``).
    assert streaming.initial_capital == reconstructed.initial_capital
    assert streaming.dates == reconstructed.dates
    assert streaming.equity == reconstructed.equity

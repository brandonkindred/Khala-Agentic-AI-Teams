"""Property-based invariants for the backtest simulator.

Runs a small, deterministic round-trip strategy against Hypothesis-generated
bar sequences and asserts the core contracts hold:

* **Fill-at-next-bar** — every closed trade's ``entry_date`` is strictly after
  the first bar in the input sequence.  Because the strategy subprocess has no
  accessor for future bars (see ``strategy/contract.py``) and the service
  queues orders in ``pending_for_prev`` to fill on bar *t+1*'s open, no trade
  can entry on the first bar of the run.
* **Trade consistency** — for every trade, ``gross_pnl`` reconciles against
  ``(exit_price - entry_price) * shares`` for longs (signs flipped for
  shorts), and ``net_pnl`` equals ``gross_pnl`` when costs are zero.
* **Look-ahead red-team** — a strategy that reads a non-existent ``Bar``
  attribute is classified as ``lookahead_violation`` by the harness.

Hypothesis is kept minimal (``max_examples`` is small, ``deadline`` is
``None``) because each example spawns a Python subprocess; the goal is to
guard against structural regressions, not to run exhaustive fuzzing.
"""

from __future__ import annotations

import textwrap
from typing import List

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from investment_team.market_data_service import OHLCVBar  # noqa: E402
from investment_team.models import BacktestConfig, StrategySpec  # noqa: E402
from investment_team.trading_service.data_stream.historical_replay import (  # noqa: E402
    HistoricalReplayStream,
)
from investment_team.trading_service.modes.backtest import run_backtest  # noqa: E402
from investment_team.trading_service.service import TradingService  # noqa: E402
from investment_team.trading_service.strategy.streaming_harness import (  # noqa: E402
    StrategyRuntimeError,
)

from .strategies import ROUND_TRIP_CODE  # noqa: E402

LOOKAHEAD_CODE = textwrap.dedent('''\
    """Red-team strategy that touches a forward field that does not exist."""
    from contract import Strategy


    class Peeker(Strategy):
        def on_bar(self, ctx, bar):
            _ = bar.next_close  # noqa: F841
''')


# ---------------------------------------------------------------------------
# Hypothesis bar generators
# ---------------------------------------------------------------------------


@st.composite
def _bar_sequences(draw) -> List[OHLCVBar]:
    n = draw(st.integers(min_value=12, max_value=28))
    closes = draw(
        st.lists(
            st.floats(min_value=10.0, max_value=500.0, allow_nan=False, allow_infinity=False),
            min_size=n,
            max_size=n,
        )
    )
    bars: List[OHLCVBar] = []
    for i, close in enumerate(closes):
        spread = max(0.01, close * 0.005)
        bars.append(
            OHLCVBar(
                date=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                open=round(close - spread * 0.25, 4),
                high=round(close + spread, 4),
                low=round(close - spread, 4),
                close=round(close, 4),
                volume=1_000_000.0,
            )
        )
    return bars


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _config() -> BacktestConfig:
    # Pin to the optimistic (legacy) fill geometry — the structural
    # invariants tested here (fill-on-t+1, gross-PnL math, lookahead
    # red-team) hold under either model, but pinning makes the engine's
    # parity-tested code path the test target.
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_model="optimistic",
    )


def _spec(code: str, *, strategy_id: str = "hyp") -> StrategySpec:
    return StrategySpec(
        strategy_id=strategy_id,
        authored_by="hypothesis",
        asset_class="stocks",
        hypothesis="invariant",
        signal_definition="invariant",
        strategy_code=code,
    )


@given(bars=_bar_sequences())
@settings(max_examples=8, deadline=None)
def test_fill_is_never_on_the_decision_bar(bars: List[OHLCVBar]) -> None:
    """Entry date must be strictly after the first bar.

    The first ``on_bar`` delivery happens on ``bars[0]``; any order submitted
    there is queued into ``pending_for_prev`` and filled against ``bars[1]``'s
    open.  A trade entering on ``bars[0]``'s date would imply the engine
    accepted a same-bar fill, which is the Phase 2 look-ahead regression.
    """
    first_date = bars[0].date
    spec = _spec(ROUND_TRIP_CODE)
    result = run_backtest(strategy=spec, config=_config(), market_data={"AAA": bars})
    for trade in result.trades:
        assert trade.entry_date > first_date, (
            f"trade {trade.trade_num} filled on the first bar ({trade.entry_date}) — "
            "engine accepted a same-bar fill"
        )


@given(bars=_bar_sequences())
@settings(max_examples=8, deadline=None)
def test_trade_pnl_internally_consistent(bars: List[OHLCVBar]) -> None:
    """``gross_pnl`` reconciles to price-delta × shares and ``net_pnl == gross_pnl`` when costs are zero."""
    spec = _spec(ROUND_TRIP_CODE)
    result = run_backtest(strategy=spec, config=_config(), market_data={"AAA": bars})
    for trade in result.trades:
        if trade.side == "long":
            expected_gross = (trade.exit_price - trade.entry_price) * trade.shares
        else:
            expected_gross = (trade.entry_price - trade.exit_price) * trade.shares
        assert trade.gross_pnl == pytest.approx(expected_gross, rel=1e-4, abs=1e-4)
        assert trade.net_pnl == pytest.approx(trade.gross_pnl, rel=1e-4, abs=1e-4)


def test_lookahead_red_team_is_classified_as_violation() -> None:
    """Touching a non-existent ``Bar`` attribute surfaces as ``lookahead_violation``.

    This locks in the classification that the subprocess harness emits so the
    upstream backtest path can gate on it for user feedback.  The
    ``TradingService`` swallows the exception and surfaces it on the result's
    ``lookahead_violation`` flag; the engine's 422 mapping lives in
    ``api.main`` and is covered separately.
    """
    bars = [
        OHLCVBar(
            date=f"2024-01-{i + 1:02d}",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1000.0,
        )
        for i in range(6)
    ]
    stream = HistoricalReplayStream({"AAA": bars}, timeframe="1d")
    service = TradingService(
        strategy_code=LOOKAHEAD_CODE,
        config=_config(),
    )
    outcome = service.run(stream)
    assert outcome.lookahead_violation, (
        "the subprocess harness must classify forward-attribute access as "
        "lookahead_violation; got error=%r, trades=%d" % (outcome.error, len(outcome.trades))
    )


def test_round_trip_strategy_produces_closed_trades() -> None:
    """Sanity check: the round-trip reference strategy produces closed trades.

    If this starts failing it usually means an engine change broke the
    ``pending_for_prev`` queue — the Hypothesis invariants above depend on
    the strategy actually generating trades to validate.
    """
    bars = [
        OHLCVBar(
            date=f"2024-01-{i + 1:02d}",
            open=100.0 + i * 0.1,
            high=100.5 + i * 0.1,
            low=99.5 + i * 0.1,
            close=100.0 + i * 0.1,
            volume=1_000_000.0,
        )
        for i in range(24)
    ]
    spec = _spec(ROUND_TRIP_CODE, strategy_id="hyp-sanity")
    result = run_backtest(strategy=spec, config=_config(), market_data={"AAA": bars})
    assert len(result.trades) >= 1, "round-trip strategy produced no trades"


# Re-export to silence unused-import lint when the harness classifier changes.
_ = StrategyRuntimeError

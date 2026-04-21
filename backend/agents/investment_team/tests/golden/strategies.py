"""Reference strategy code strings used by the golden tests.

Each string is valid Python that the subprocess harness copies into its
isolated working directory as ``strategy.py``.  Strategies must subclass
``contract.Strategy`` and interact with the engine exclusively through the
``StrategyContext`` API.
"""

from __future__ import annotations

import textwrap

# ---------------------------------------------------------------------------
# Round-trip reference strategy: deterministic SMA crossover.
# ---------------------------------------------------------------------------
#
# Keeps a rolling short/long mean; enters LONG on golden cross, exits on
# death cross.  Fully closes each position before opening a new one so
# trade PnL is conserved for the Hypothesis invariants.

SMA_CROSSOVER_CODE = textwrap.dedent('''\
    """SMA crossover — long-only, closes out on the opposing signal."""
    from contract import OrderSide, OrderType, Strategy


    SHORT_WINDOW = 5
    LONG_WINDOW = 15


    class SmaCrossover(Strategy):
        def on_bar(self, ctx, bar):
            history = ctx.history(bar.symbol, LONG_WINDOW)
            if len(history) < LONG_WINDOW:
                return

            short_window = history[-SHORT_WINDOW:]
            long_window = history[-LONG_WINDOW:]
            short_ma = sum(b.close for b in short_window) / SHORT_WINDOW
            long_ma = sum(b.close for b in long_window) / LONG_WINDOW

            pos = ctx.position(bar.symbol)
            if short_ma > long_ma and pos is None:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=10,
                    order_type=OrderType.MARKET,
                    reason="golden-cross",
                )
            elif short_ma < long_ma and pos is not None:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.SHORT,
                    qty=pos.qty,
                    order_type=OrderType.MARKET,
                    reason="death-cross",
                )
''')


# ---------------------------------------------------------------------------
# Buy-and-hold: enters on the first available bar, never exits.
# ---------------------------------------------------------------------------
#
# Used to lock in the "open position at end-of-run" behavior so any future
# engine change that force-flattens at EOS shows up as a snapshot diff.

BUY_AND_HOLD_CODE = textwrap.dedent('''\
    """Enter LONG on the first bar, never exit."""
    from contract import OrderSide, OrderType, Strategy


    class BuyAndHold(Strategy):
        def on_bar(self, ctx, bar):
            if ctx.position(bar.symbol) is None:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=10,
                    order_type=OrderType.MARKET,
                    reason="enter",
                )
''')


# ---------------------------------------------------------------------------
# Deterministic round-trip strategy used for PnL-conservation invariants.
# ---------------------------------------------------------------------------
#
# Opens a position every ``HOLD`` bars and closes it ``HOLD`` bars later.
# Guarantees no position is left open at end-of-stream when the bar count is
# a multiple of ``2 * HOLD``.

ROUND_TRIP_CODE = textwrap.dedent('''\
    """Open/close at fixed intervals so every trade is a closed round trip."""
    from contract import OrderSide, OrderType, Strategy


    HOLD = 3


    class RoundTrip(Strategy):
        def __init__(self):
            self._counter = 0

        def on_bar(self, ctx, bar):
            self._counter += 1
            pos = ctx.position(bar.symbol)
            if pos is None and self._counter % (2 * HOLD) == 1:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=5,
                    order_type=OrderType.MARKET,
                    reason="enter",
                )
            elif pos is not None and self._counter % (2 * HOLD) == HOLD + 1:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.SHORT,
                    qty=pos.qty,
                    order_type=OrderType.MARKET,
                    reason="exit",
                )
''')


# ---------------------------------------------------------------------------
# Curve-fit reference strategy (issue #247): hard-coded calendar-date entries
# selected after the fact to coincide with troughs/peaks in the AAA sinusoidal
# fixture. The whole point is that the strategy is textbook overfitting —
# single-window backtests produce a near-perfect Sharpe, but in walk-forward
# evaluation the held-out test folds intersect only a few (or zero) of the
# hard-coded dates and the OOS Sharpe collapses. Consumed by
# test_orchestrator_walk_forward.py (step 8) to verify the acceptance gate
# rejects it.
# ---------------------------------------------------------------------------

OVERFIT_HARDCODED_DATES_CODE = textwrap.dedent('''\
    """Curve-fit reference: hard-coded entry/exit dates aligned with BBB peaks/troughs."""
    from contract import OrderSide, OrderType, Strategy


    # Dates chosen in hindsight to straddle profitable cycles on the "BBB"
    # symbol of golden_market_data (sinusoid with period=25 weekdays starting
    # 2024-01-01). These dates are NOT a learned signal — they are data-
    # snooped; the whole point is to exercise the acceptance gate's rejection
    # of curve-fit strategies.
    ENTRY_DATES = {
        "2024-02-12",
        "2024-04-08",
        "2024-06-03",
        "2024-07-29",
        "2024-09-23",
    }
    EXIT_DATES = {
        "2024-03-11",
        "2024-05-06",
        "2024-07-01",
        "2024-08-26",
        "2024-10-21",
    }
    TARGET_SYMBOL = "BBB"


    class OverfitHardcodedDates(Strategy):
        def on_bar(self, ctx, bar):
            if bar.symbol != TARGET_SYMBOL:
                return
            d = bar.timestamp[:10]
            pos = ctx.position(bar.symbol)
            if d in ENTRY_DATES and pos is None:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=10,
                    order_type=OrderType.MARKET,
                    reason="hardcoded-entry",
                )
            elif d in EXIT_DATES and pos is not None:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.SHORT,
                    qty=pos.qty,
                    order_type=OrderType.MARKET,
                    reason="hardcoded-exit",
                )
''')

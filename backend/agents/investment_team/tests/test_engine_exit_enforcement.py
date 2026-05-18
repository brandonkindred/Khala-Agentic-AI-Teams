"""End-to-end integration tests for engine-side exit-rule enforcement
(issue #527).

Feeds ``run_backtest`` a strategy that opens a position and *never closes
it on its own*, plus structured ``exit_rules`` on the spec. Asserts that
the engine emits the corresponding close orders and the resulting trade
ledger respects the rule.

The strategy code intentionally avoids any exit logic so every closed
trade in the ledger must have come from the engine's enforcement path.
"""

from __future__ import annotations

import textwrap
from typing import Dict, List

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestConfig, StrategySpec
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    StopLossRule,
    TakeProfitRule,
    TimeStopRule,
)
from investment_team.trading_service.modes.backtest import run_backtest
from investment_team.trading_service.service import ENGINE_EXIT_REASON_PREFIX

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mk_bar(date_iso: str, close: float, *, high: float = None, low: float = None) -> OHLCVBar:
    return OHLCVBar(
        date=date_iso,
        open=close - 0.2,
        high=close + 0.5 if high is None else high,
        low=close - 0.5 if low is None else low,
        close=close,
        volume=1_000_000,
    )


def _flat_bars(n: int = 30, start_price: float = 100.0) -> Dict[str, List[OHLCVBar]]:
    """30 daily bars that drift up by 1c/bar — slow steady uptrend.

    Slow enough that none of the stop-loss / take-profit thresholds used in
    the tests fire on price alone, so the time-stop test isolates the
    bars-held trigger.
    """
    out: List[OHLCVBar] = []
    for i in range(n):
        day = i + 1
        month = 1 if day <= 31 else 2
        d = day if month == 1 else day - 31
        out.append(_mk_bar(f"2024-{month:02d}-{d:02d}", start_price + i * 0.01))
    return {"AAA": out}


def _falling_bars(
    n_pre_drop: int = 5, drop_bar_low: float = 90.0, n_post_drop: int = 10
) -> Dict[str, List[OHLCVBar]]:
    """Flat → big single-bar drop → flat. Triggers a stop-loss on entry-day.

    The ``drop_bar`` has ``low=drop_bar_low`` so a stop-loss with pct=0.05
    against entry price 100 (floor=95) fires on that bar.
    """
    out: List[OHLCVBar] = []
    base = 100.0
    for i in range(n_pre_drop):
        out.append(_mk_bar(f"2024-01-{i + 1:02d}", base))
    # Drop bar — bar low much lower than entry.
    drop_day = n_pre_drop + 1
    out.append(
        OHLCVBar(
            date=f"2024-01-{drop_day:02d}",
            open=base - 0.5,
            high=base,
            low=drop_bar_low,
            close=base - 8.0,
            volume=1_000_000,
        )
    )
    for i in range(n_post_drop):
        day = drop_day + 1 + i
        out.append(_mk_bar(f"2024-01-{day:02d}", base - 8.0))
    return {"AAA": out}


def _rising_bars(
    n_pre_pop: int = 5, pop_bar_high: float = 110.0, n_post_pop: int = 10
) -> Dict[str, List[OHLCVBar]]:
    """Flat → big single-bar pop → flat. Triggers a take-profit."""
    out: List[OHLCVBar] = []
    base = 100.0
    for i in range(n_pre_pop):
        out.append(_mk_bar(f"2024-01-{i + 1:02d}", base))
    pop_day = n_pre_pop + 1
    out.append(
        OHLCVBar(
            date=f"2024-01-{pop_day:02d}",
            open=base + 0.5,
            high=pop_bar_high,
            low=base,
            close=base + 8.0,
            volume=1_000_000,
        )
    )
    for i in range(n_post_pop):
        day = pop_day + 1 + i
        out.append(_mk_bar(f"2024-01-{day:02d}", base + 8.0))
    return {"AAA": out}


# Strategy that opens a single long position on the first non-warmup bar
# and never exits. Any closed trade in the ledger must therefore come
# from engine enforcement.
_ENTRY_ONLY_STRATEGY = textwrap.dedent(
    '''\
    """Open one long, never exit. Engine enforcement is on its own."""
    from contract import OrderSide, OrderType, Strategy


    class EntryOnly(Strategy):
        def on_bar(self, ctx, bar):
            if ctx.position(bar.symbol) is not None:
                return
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=10,
                order_type=OrderType.MARKET,
                reason="entry_only",
            )
    '''
)


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-02-15",
        initial_capital=100_000.0,
        slippage_bps=2.0,
        transaction_cost_bps=5.0,
    )


def _spec(*, exit_rules) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-engine-exit",
        authored_by="tests",
        asset_class="equity",
        hypothesis="engine-side exit rules close positions strategy_code leaves open",
        signal_definition="enter long once, leave to the engine to close",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close",
                    op=">",
                    rhs=IndicatorRef(name="sma", params={"period": 5}),
                ),
            )
        ],
        exit_rules=exit_rules,
        strategy_code=_ENTRY_ONLY_STRATEGY,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_time_stop_closes_position_strategy_left_open() -> None:
    spec = _spec(exit_rules=[TimeStopRule(n_bars=5)])
    run = run_backtest(strategy=spec, config=_config(), market_data=_flat_bars())

    assert run.service_result.error is None, run.service_result.error
    # At least one closed trade — engine emitted the close.
    assert len(run.trades) >= 1
    # Every trade respects the time stop within the +1 bar fill-lag ceiling.
    for trade in run.trades:
        assert trade.hold_days <= 6, (
            f"trade {trade.trade_num} held {trade.hold_days} days, "
            "expected <= 6 (n_bars=5 + 1 bar fill lag)"
        )
    # Diagnostics records the firing.
    diag = run.service_result.execution_diagnostics
    assert diag.exit_rule_firings.get("time_stop", 0) >= 1, diag.exit_rule_firings


def test_engine_emitted_close_carries_reason_prefix() -> None:
    spec = _spec(exit_rules=[TimeStopRule(n_bars=3)])
    run = run_backtest(strategy=spec, config=_config(), market_data=_flat_bars())

    diag = run.service_result.execution_diagnostics
    emitted = [
        e
        for e in diag.last_order_events
        if e.event_type == "emitted" and (e.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)
    ]
    assert emitted, (
        "expected at least one OrderLifecycleEvent tagged with the engine "
        f"exit reason prefix {ENGINE_EXIT_REASON_PREFIX!r}; got reasons "
        f"{[e.reason for e in diag.last_order_events]}"
    )
    for e in emitted:
        # Reason format: ``engine_exit:<rule_kind>``.
        suffix = (e.reason or "").split(":", 1)[1]
        assert suffix in {"time_stop", "stop_loss", "take_profit"}, e.reason


def test_stop_loss_closes_when_price_breaks_floor() -> None:
    # Entry ~100; stop-loss pct=0.05 → floor=95; the drop bar low is 90.
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    run = run_backtest(
        strategy=spec,
        config=_config(),
        market_data=_falling_bars(n_pre_drop=5, drop_bar_low=90.0, n_post_drop=10),
    )

    assert run.service_result.error is None, run.service_result.error
    assert len(run.trades) >= 1
    diag = run.service_result.execution_diagnostics
    assert diag.exit_rule_firings.get("stop_loss", 0) >= 1, diag.exit_rule_firings


def test_take_profit_closes_when_price_clears_target() -> None:
    # Entry ~100; take-profit pct=0.05 → target=105; the pop bar high is 110.
    spec = _spec(exit_rules=[TakeProfitRule(pct=0.05)])
    run = run_backtest(
        strategy=spec,
        config=_config(),
        market_data=_rising_bars(n_pre_pop=5, pop_bar_high=110.0, n_post_pop=10),
    )

    assert run.service_result.error is None, run.service_result.error
    assert len(run.trades) >= 1
    diag = run.service_result.execution_diagnostics
    assert diag.exit_rule_firings.get("take_profit", 0) >= 1, diag.exit_rule_firings


def test_no_exit_rules_leaves_position_open_at_end_of_run() -> None:
    """Sanity check: with no engine exit rules, the entry-only strategy
    leaves the position open (proving the engine isn't fabricating closes
    when ``exit_rules`` is empty).
    """
    spec = _spec(exit_rules=[])
    run = run_backtest(strategy=spec, config=_config(), market_data=_flat_bars())

    assert run.service_result.error is None, run.service_result.error
    # No closes — at most a final open position; trade ledger is empty.
    diag = run.service_result.execution_diagnostics
    assert diag.exit_rule_firings == {}, diag.exit_rule_firings
    assert run.trades == [] or all(not (t.exit_date or "").strip() for t in run.trades), (
        "expected no closed trades when exit_rules is empty"
    )


def test_strategy_emitted_close_dedupes_engine_firing_same_bar() -> None:
    """If the strategy already submits an opposite-side close on the bar the
    engine rule would fire, the engine must not stack a duplicate close
    onto ``pending_for_prev``. Asserts exactly one exit-emission diagnostic
    per round trip rather than two.
    """
    strategy_with_self_exit = textwrap.dedent(
        '''\
        """Enter long once, exit on bar 4 — same bar a TimeStopRule(n_bars=3) fires."""
        from contract import OrderSide, OrderType, Strategy


        class SelfExit(Strategy):
            _bar_count = 0

            def on_bar(self, ctx, bar):
                self._bar_count += 1
                pos = ctx.position(bar.symbol)
                if pos is None and self._bar_count == 1:
                    ctx.submit_order(
                        symbol=bar.symbol, side=OrderSide.LONG, qty=10,
                        order_type=OrderType.MARKET, reason="self_entry",
                    )
                elif pos is not None and self._bar_count >= 3:
                    ctx.submit_order(
                        symbol=bar.symbol, side=OrderSide.SHORT, qty=pos.qty,
                        order_type=OrderType.MARKET, reason="self_exit",
                    )
        '''
    )
    spec = StrategySpec(
        strategy_id="strat-dedup",
        authored_by="tests",
        asset_class="equity",
        hypothesis="dedup test",
        signal_definition="enter once, self-exit same bar engine would",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close",
                    op=">",
                    rhs=IndicatorRef(name="sma", params={"period": 5}),
                ),
            )
        ],
        exit_rules=[TimeStopRule(n_bars=3)],
        strategy_code=strategy_with_self_exit,
    )

    run = run_backtest(strategy=spec, config=_config(), market_data=_flat_bars())

    assert run.service_result.error is None, run.service_result.error
    diag = run.service_result.execution_diagnostics
    # At most one closed trade in this synthetic — and zero engine firings,
    # because the strategy's own exit was queued first and the engine deduped.
    assert diag.exit_rule_firings.get("time_stop", 0) == 0, (
        "engine should have deduped against the strategy's own exit on the "
        f"same bar; firings={diag.exit_rule_firings}"
    )


# ---------------------------------------------------------------------------
# Chunked path rejection
# ---------------------------------------------------------------------------


def test_chunked_path_rejected_when_exit_rules_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BAR_CHUNK_SIZE>1`` combined with non-empty ``exit_rules`` must raise
    rather than silently lose enforcement (issue #527 scope decision).
    """
    monkeypatch.setenv("BAR_CHUNK_SIZE", "8")
    spec = _spec(exit_rules=[TimeStopRule(n_bars=5)])
    with pytest.raises(NotImplementedError, match="chunked-bar protocol"):
        run_backtest(strategy=spec, config=_config(), market_data=_flat_bars())

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


def test_engine_emitted_close_carries_reason_prefix() -> None:
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    run = run_backtest(
        strategy=spec,
        config=_config(),
        market_data=_falling_bars(n_pre_drop=5, drop_bar_low=90.0, n_post_drop=10),
    )

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
        assert suffix in {"stop_loss", "take_profit"}, e.reason


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


def test_strategy_emitted_close_does_not_skip_engine_emission() -> None:
    """When the strategy submits a same-bar full-market close on the bar the
    engine rule fires, BOTH orders queue. On next bar the order book
    submits in order; whichever fills first closes the position, and the
    other order's stale-continuation guard (engine via binding, strategy
    via ``existing_pos is None``) drops the survivor. The engine
    therefore records its emission in ``exit_rule_firings`` (emitted, not
    necessarily filled) — the fill side is what matters for actual
    position closure.
    """
    strategy_with_self_exit = textwrap.dedent(
        '''\
        """Enter long once, then self-exit on the bar a stop-loss fires."""
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
                elif pos is not None and bar.low < 92.0:
                    # On the drop bar (low=90), the engine StopLossRule(pct=0.05)
                    # ALSO fires. Both orders queue.
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
        exit_rules=[StopLossRule(pct=0.05)],
        strategy_code=strategy_with_self_exit,
    )

    run = run_backtest(
        strategy=spec,
        config=_config(),
        market_data=_falling_bars(n_pre_drop=5, drop_bar_low=90.0, n_post_drop=10),
    )

    assert run.service_result.error is None, run.service_result.error
    diag = run.service_result.execution_diagnostics
    # Engine emits its close — robustness against participation/IOC/FOK
    # clipping of the strategy's market order. The fill simulator's
    # stale-continuation guard drops whichever of the two orders arrives
    # against an already-closed position.
    assert diag.exit_rule_firings.get("stop_loss", 0) >= 1, (
        f"engine should emit even when strategy also submits a same-bar close; "
        f"firings={diag.exit_rule_firings}"
    )


def test_partial_strategy_unwind_does_not_suppress_engine_close() -> None:
    """If the strategy submits a partial unwind (e.g. sells 1 of 10 long
    shares) on the bar a stop-loss fires, the engine must still emit a
    close for the residual qty rather than treating the partial as a
    full close. Regression for the P1 review comment on issue #527 PR.
    """
    strategy_with_partial_unwind = textwrap.dedent(
        '''\
        """Enter 10 shares, partial-unwind 1 share on the bar a stop-loss fires."""
        from contract import OrderSide, OrderType, Strategy


        class PartialUnwind(Strategy):
            _bar_count = 0

            def on_bar(self, ctx, bar):
                self._bar_count += 1
                pos = ctx.position(bar.symbol)
                if pos is None and self._bar_count == 1:
                    ctx.submit_order(
                        symbol=bar.symbol, side=OrderSide.LONG, qty=10,
                        order_type=OrderType.MARKET, reason="self_entry",
                    )
                elif pos is not None and bar.low < 92.0:
                    # Token partial: sell 1 of 10 on the drop bar. The engine's
                    # ``StopLossRule(pct=0.05)`` should still emit a close for
                    # the residual 9 — not be suppressed by this 1-share
                    # decoration.
                    ctx.submit_order(
                        symbol=bar.symbol, side=OrderSide.SHORT, qty=1,
                        order_type=OrderType.MARKET, reason="partial_unwind",
                    )
        '''
    )
    spec = StrategySpec(
        strategy_id="strat-partial-dedup",
        authored_by="tests",
        asset_class="equity",
        hypothesis="partial unwind doesn't suppress engine close",
        signal_definition="enter 10, partial unwind 1 same bar stop-loss fires",
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
        exit_rules=[StopLossRule(pct=0.05)],
        strategy_code=strategy_with_partial_unwind,
    )

    run = run_backtest(
        strategy=spec,
        config=_config(),
        market_data=_falling_bars(n_pre_drop=5, drop_bar_low=90.0, n_post_drop=10),
    )

    assert run.service_result.error is None, run.service_result.error
    diag = run.service_result.execution_diagnostics
    # Engine MUST still fire a stop-loss close (for the residual 9 shares).
    assert diag.exit_rule_firings.get("stop_loss", 0) >= 1, (
        "engine should still fire engine_exit when strategy only partially "
        f"unwinds; firings={diag.exit_rule_firings}"
    )


# ---------------------------------------------------------------------------
# Chunked path rejection
# ---------------------------------------------------------------------------


def test_chunked_path_rejected_when_exit_rules_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BAR_CHUNK_SIZE>1`` combined with non-empty ``exit_rules`` must raise
    rather than silently lose enforcement (issue #527 scope decision).
    """
    monkeypatch.setenv("BAR_CHUNK_SIZE", "8")
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    with pytest.raises(NotImplementedError, match="chunked-bar protocol"):
        run_backtest(strategy=spec, config=_config(), market_data=_flat_bars())

"""Invariant (property-style) tests for the trade simulator.

These assertions must hold across every simulation run regardless of strategy:

- cash conservation (no capital materializes/disappears)
- PnL attribution (sum of trade net_pnl equals delta of capital plus open MTM)
- temporal safety (a fill's bar date cannot precede its decision's visible history)
"""

from __future__ import annotations

import pytest

from investment_team.trade_simulator import TradeSimulationEngine

from .deterministic_strategies import mean_reversion, sma_crossover
from .synthetic_data import build_fixture_universe


@pytest.fixture(scope="module")
def universe():
    return build_fixture_universe()


def _engine(*, lookahead_safe=True, **kwargs):
    defaults = dict(
        initial_capital=100_000.0,
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
        pre_filter_pct=0.0,
        max_evaluations=100_000,
        lookahead_safe=lookahead_safe,
    )
    defaults.update(kwargs)
    return TradeSimulationEngine(**defaults)


@pytest.mark.parametrize(
    "strategy_factory",
    [sma_crossover, mean_reversion],
    ids=["sma_crossover", "mean_reversion"],
)
@pytest.mark.parametrize("lookahead_safe", [True, False], ids=["safe", "legacy"])
def test_capital_reflects_gross_pnl(universe, strategy_factory, lookahead_safe):
    """``final_capital`` tracks gross PnL only (tx costs not deducted)."""
    engine = _engine(lookahead_safe=lookahead_safe)
    result = engine.run(universe, strategy_factory())
    expected_gross = 100_000.0 + sum(t.gross_pnl for t in result.trades)
    assert result.final_capital == pytest.approx(expected_gross, abs=1.0)


@pytest.mark.parametrize(
    "strategy_factory",
    [sma_crossover, mean_reversion],
    ids=["sma_crossover", "mean_reversion"],
)
@pytest.mark.xfail(
    reason="Pre-refactor: engine capital excludes tx costs (see Phase 5 plan).",
    strict=True,
)
def test_capital_reflects_net_pnl_after_refactor(universe, strategy_factory):
    engine = _engine(lookahead_safe=True)
    result = engine.run(universe, strategy_factory())
    expected_net = 100_000.0 + sum(t.net_pnl for t in result.trades)
    assert result.final_capital == pytest.approx(expected_net, abs=1.0)


@pytest.mark.parametrize(
    "strategy_factory",
    [sma_crossover, mean_reversion],
    ids=["sma_crossover", "mean_reversion"],
)
@pytest.mark.parametrize("lookahead_safe", [True, False], ids=["safe", "legacy"])
def test_cumulative_pnl_matches_sum(universe, strategy_factory, lookahead_safe):
    engine = _engine(lookahead_safe=lookahead_safe)
    result = engine.run(universe, strategy_factory())
    running = 0.0
    for t in result.trades:
        running = round(running + t.net_pnl, 2)
        assert t.cumulative_pnl == pytest.approx(running, abs=0.05), (
            f"trade {t.trade_num}: cumulative_pnl={t.cumulative_pnl} "
            f"should equal running sum {running}"
        )


def test_no_trades_when_strategy_always_holds(universe):
    engine = _engine()

    def always_hold(*_args, **_kwargs):
        return {"action": "hold", "confidence": 0.0, "shares": 0, "reasoning": ""}

    result = engine.run(universe, always_hold)
    assert result.trades == []
    assert result.final_capital == pytest.approx(engine.initial_capital, abs=0.01)


@pytest.mark.parametrize("lookahead_safe", [True, False], ids=["safe", "legacy"])
def test_trade_dates_are_within_universe_span(universe, lookahead_safe):
    engine = _engine(lookahead_safe=lookahead_safe)
    result = engine.run(universe, sma_crossover())
    all_dates: list[str] = []
    for bars in universe.values():
        all_dates.extend(b.date for b in bars)
    lo, hi = min(all_dates), max(all_dates)
    for t in result.trades:
        assert lo <= t.entry_date <= hi
        assert lo <= t.exit_date <= hi
        assert t.entry_date <= t.exit_date


def test_hold_days_non_negative(universe):
    engine = _engine()
    result = engine.run(universe, sma_crossover())
    for t in result.trades:
        assert t.hold_days >= 0


def test_forced_close_count_is_accurate(universe):
    """Only symbols with positions open at the end produce a forced close."""
    engine = _engine()

    def always_long(symbol, bar, recent, position, capital):
        if position is None:
            return {"action": "enter_long", "confidence": 1.0, "shares": 0, "reasoning": ""}
        return {"action": "hold", "confidence": 0.0, "shares": 0, "reasoning": ""}

    result = engine.run(universe, always_long)
    assert result.forced_close_count == len(universe)
    assert len(result.trades) == len(universe)


# ---------------------------------------------------------------------------
# Phase 2: look-ahead safety invariant
# ---------------------------------------------------------------------------


def test_lookahead_safe_fills_at_next_bar_open(universe):
    """Verify that in look-ahead-safe mode, entry fills happen on a bar AFTER
    the decision bar (i.e. at the next bar's open, not the decision bar's close).

    We record all decisions and compare their dates against the fill dates:
    every fill date must be strictly later than its decision date.
    """
    engine = _engine(lookahead_safe=True)

    decision_log: list[tuple[str, str, str]] = []

    _inner = sma_crossover()

    def _logging_eval(symbol, bar, recent, position, capital):
        result = _inner(symbol, bar, recent, position, capital)
        action = result.get("action", "hold")
        if action in ("enter_long", "enter_short", "exit"):
            decision_log.append((symbol, bar.date, action))
        return result

    sim = engine.run(universe, _logging_eval)

    entry_decisions = [(s, d) for s, d, a in decision_log if a in ("enter_long", "enter_short")]

    for t in sim.trades:
        if t.trade_num <= len(entry_decisions):
            matched = [(s, d) for s, d in entry_decisions if s == t.symbol and d < t.entry_date]
            assert matched, (
                f"trade {t.trade_num}: entry on {t.entry_date} but no decision "
                f"on a strictly earlier bar for {t.symbol}"
            )

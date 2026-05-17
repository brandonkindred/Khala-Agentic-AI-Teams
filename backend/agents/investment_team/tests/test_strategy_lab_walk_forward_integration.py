"""Orchestrator wiring tests for issue #247 — walk-forward + acceptance gate.

The lower-level building blocks (fold construction, DSR, bootstrap CI, regime
sub-windows, the acceptance gate itself) have their own dedicated test files.
This file exercises the wiring inside :class:`StrategyLabOrchestrator`:

- :meth:`_evaluate_walk_forward` populates every new ``BacktestResult`` field
  the acceptance gate consumes.
- :meth:`_daily_returns_from_trades` and :meth:`_equity_to_returns` produce
  sensible series for the DSR / regime helpers.
- :meth:`_build_benchmark_equity` blends 60/40 SPY+AGG when the market-data
  service returns both, and falls back to a single-symbol benchmark when the
  blend cannot be assembled.
- The trial counter monotonically deflates DSR for an unchanged raw Sharpe.

The full ``run_cycle`` is not exercised here; the alignment-loop and ideation
agents are covered in ``test_strategy_lab_alignment.py``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pytest

from investment_team.execution.metrics import compute_deflated_sharpe
from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestConfig, BacktestResult, StrategySpec, TradeRecord
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.quality_gates.acceptance_gate import AcceptanceGate
from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    SignalExitRule,
    TimeStopRule,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-wf-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0))],
        risk_limits={},
        speculative=False,
        strategy_code=(
            "from contract import Strategy\n\nclass S(Strategy):\n"
            "    def on_bar(self, ctx, bar):\n"
            "        ctx.submit_order(symbol='X', qty=1, side='LONG')\n"
            "        ctx.submit_order(symbol='X', qty=1, side='FLAT')\n"
        ),
    )


def _config(**overrides: Any) -> BacktestConfig:
    base: Dict[str, Any] = dict(
        start_date="2022-01-03",
        end_date="2022-12-30",
        initial_capital=100_000.0,
    )
    base.update(overrides)
    return BacktestConfig(**base)


def _mk_trade(
    *,
    entry: str,
    exit_: str,
    net: float,
    symbol: str = "AAPL",
    hold: int = 5,
) -> TradeRecord:
    return TradeRecord(
        trade_num=1,
        entry_date=entry,
        exit_date=exit_,
        symbol=symbol,
        side="long" if net >= 0 else "short",
        entry_price=100.0,
        exit_price=100.0 + net / 10.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=net,
        net_pnl=net,
        return_pct=net / 1000.0 * 100,
        hold_days=hold,
        outcome="win" if net > 0 else "loss",
        cumulative_pnl=net,
    )


def _trades_across_year(n_per_month: int = 4, base_pnl: float = 50.0) -> List[TradeRecord]:
    """Spread roughly ``n_per_month`` winning trades across the calendar so that
    every walk-forward fold gets a handful of OOS observations."""
    out: List[TradeRecord] = []
    cum = 0.0
    for month in range(1, 13):
        for j in range(n_per_month):
            day = (j * 7) + 3  # 3, 10, 17, 24
            entry = date(2022, month, day)
            exit_ = entry + timedelta(days=5)
            net = base_pnl if (month + j) % 2 == 0 else -base_pnl * 0.6
            cum += net
            out.append(
                TradeRecord(
                    trade_num=len(out) + 1,
                    entry_date=entry.isoformat(),
                    exit_date=exit_.isoformat(),
                    symbol="AAPL",
                    side="long",
                    entry_price=100.0,
                    exit_price=100.0 + net / 10.0,
                    shares=10.0,
                    position_value=1000.0,
                    gross_pnl=net,
                    net_pnl=net,
                    return_pct=net / 1000.0 * 100,
                    hold_days=5,
                    outcome="win" if net > 0 else "loss",
                    cumulative_pnl=cum,
                )
            )
    return out


def _stub_bars(symbol: str, *, drift: float = 0.0003, n: int = 250) -> List[OHLCVBar]:
    """Synthetic OHLCV bars starting 2022-01-03; price drifts at ``drift`` per day."""
    bars: List[OHLCVBar] = []
    d = date(2022, 1, 3)
    price = 100.0
    while len(bars) < n:
        if d.weekday() < 5:
            bars.append(
                OHLCVBar(
                    date=d.isoformat(),
                    open=price,
                    high=price * 1.005,
                    low=price * 0.995,
                    close=price,
                    volume=1_000_000,
                )
            )
            price *= 1.0 + drift
        d += timedelta(days=1)
    return bars


class _StubMarketDataService:
    """Returns canned SPY/AGG bars for the 60/40 blend; no network access."""

    def __init__(self, *, has_spy: bool = True, has_agg: bool = True) -> None:
        self.has_spy = has_spy
        self.has_agg = has_agg
        self.calls: List[Dict[str, Any]] = []

    def fetch_multi_symbol_range(
        self,
        *,
        symbols: List[str],
        asset_class: str,
        start_date: str,
        end_date: str,
        as_of: Optional[str] = None,
        frequency: str = "1d",
    ) -> Dict[str, List[OHLCVBar]]:
        self.calls.append(
            {
                "symbols": list(symbols),
                "asset_class": asset_class,
                "start_date": start_date,
                "end_date": end_date,
                "as_of": as_of,
                "frequency": frequency,
            }
        )
        out: Dict[str, List[OHLCVBar]] = {}
        for s in symbols:
            if s == "SPY" and self.has_spy:
                out[s] = _stub_bars("SPY", drift=0.0004)
            elif s == "AGG" and self.has_agg:
                out[s] = _stub_bars("AGG", drift=0.0001)
            elif s not in {"SPY", "AGG"}:
                out[s] = _stub_bars(s, drift=0.0002)
        return out


def _orchestrator(
    market_data_service: Optional[_StubMarketDataService] = None,
) -> StrategyLabOrchestrator:
    orch = StrategyLabOrchestrator()
    if market_data_service is not None:
        orch.market_data_service = market_data_service  # type: ignore[assignment]
    return orch


# ---------------------------------------------------------------------------
# _evaluate_walk_forward
# ---------------------------------------------------------------------------


def test_evaluate_walk_forward_populates_oos_fields():
    """All new BacktestResult fields are set; fold count matches config."""
    orch = _orchestrator(_StubMarketDataService())
    config = _config(walk_forward_enabled=True, n_folds=5)
    trades = _trades_across_year(n_per_month=4)
    base_metrics = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=12.0,
        volatility_pct=8.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=4.0,
        win_rate_pct=55.0,
        profit_factor=1.4,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    market_data = {"AAPL": _stub_bars("AAPL")}

    result = orch._evaluate_walk_forward(_spec(), market_data, config, trades, base_metrics)

    assert result.deflated_sharpe is not None
    assert 0.0 <= result.deflated_sharpe <= 1.0
    assert result.oos_sharpe is not None
    assert result.is_sharpe is not None
    assert result.is_oos_degradation_pct is not None
    assert result.is_oos_degradation_pct >= 0.0  # clamped to non-negative
    assert result.oos_trade_count is not None
    assert result.oos_trade_count > 0
    assert result.fold_results is not None and len(result.fold_results) == 5
    # Per-fold dicts carry the keys the persistence layer expects.
    for fr in result.fold_results:
        assert {
            "fold_index",
            "test_start",
            "test_end",
            "oos_sharpe",
            "is_sharpe",
            "oos_trade_count",
        } <= set(fr.keys())
    # Bootstrap CI populated (may collapse to (0, 0) on tiny series; we just
    # confirm the fields are not None).
    assert result.sharpe_ci_low is not None
    assert result.sharpe_ci_high is not None
    # Regime evaluation ran and produced four entries (matching REGIME_LABELS).
    assert result.regime_results is not None and len(result.regime_results) == 4
    for rr in result.regime_results:
        assert "beat_benchmark" in rr


def test_evaluate_walk_forward_with_empty_trades_does_not_crash():
    """Empty trade list still yields a populated BacktestResult; OOS fields
    fall back to neutral values rather than raising."""
    orch = _orchestrator(_StubMarketDataService())
    config = _config(walk_forward_enabled=True, n_folds=5)
    trades: List[TradeRecord] = []
    base_metrics = BacktestResult(
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        volatility_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        win_rate_pct=0.0,
        profit_factor=0.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )

    result = orch._evaluate_walk_forward(_spec(), {}, config, trades, base_metrics)

    # At raw Sharpe = 0 and ``n_trials = 0``, DSR collapses to the
    # Probabilistic Sharpe Ratio against a zero benchmark, which is 0.5 —
    # not 0.0. We assert the field is populated and bounded; the gate
    # rejects it via the dsr_threshold default of 1.0.
    assert result.deflated_sharpe is not None
    assert 0.0 <= result.deflated_sharpe <= 1.0
    assert result.oos_sharpe == 0.0
    assert result.oos_trade_count == 0
    assert result.fold_results is not None and len(result.fold_results) == 5


def test_evaluate_walk_forward_falls_back_when_60_40_unavailable():
    """When SPY+AGG are unavailable, regime evaluation falls back to the
    single-symbol benchmark path. The overall walk-forward call must still
    return a populated result."""
    stub = _StubMarketDataService(has_agg=False)
    orch = _orchestrator(stub)
    config = _config(walk_forward_enabled=True, n_folds=5, benchmark_composition="60_40")
    trades = _trades_across_year()
    base_metrics = BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=6.0,
        volatility_pct=8.0,
        sharpe_ratio=0.7,
        max_drawdown_pct=3.0,
        win_rate_pct=52.0,
        profit_factor=1.2,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )

    result = orch._evaluate_walk_forward(
        _spec(), {"AAPL": _stub_bars("AAPL")}, config, trades, base_metrics
    )

    assert result.deflated_sharpe is not None
    # Fallback path may surface zero-length regime results when neither blend
    # nor single-symbol resolve; we accept either an empty list or four
    # entries — the gate handles missing data via its own warning result.
    assert result.regime_results is not None
    assert len(result.regime_results) in (0, 4)


# ---------------------------------------------------------------------------
# Trial counter deflates DSR
# ---------------------------------------------------------------------------


def test_trial_count_deflates_dsr_for_identical_raw_sharpe():
    """For a fixed raw Sharpe and observation count, increasing ``n_trials``
    must not increase the DSR. This is the multiple-testing correction the
    issue calls out."""
    sharpe = 1.5
    n_obs = 250
    dsr_one = compute_deflated_sharpe(sharpe, n_trials=1, n_obs=n_obs, skew=0.0, kurtosis=3.0)
    dsr_fifty = compute_deflated_sharpe(sharpe, n_trials=50, n_obs=n_obs, skew=0.0, kurtosis=3.0)
    assert 0.0 <= dsr_fifty <= dsr_one <= 1.0
    assert dsr_one - dsr_fifty > 0.05  # meaningful (not just floating-point noise)


def test_increment_trials_on_orchestrator_tracker_is_visible_to_dsr():
    """The orchestrator's convergence tracker exposes ``trial_count`` via the
    same property the walk-forward helper feeds to ``compute_deflated_sharpe``.
    This guards against a future refactor accidentally bypassing the counter.
    """
    tracker = ConvergenceTracker()
    orch = StrategyLabOrchestrator(convergence_tracker=tracker)
    assert orch.convergence_tracker.trial_count == 0
    orch.convergence_tracker.increment_trials(3)
    orch.convergence_tracker.increment_trials(2)
    assert orch.convergence_tracker.trial_count == 5


# ---------------------------------------------------------------------------
# Acceptance gate drives is_winning end-to-end (composition test)
# ---------------------------------------------------------------------------


def test_acceptance_gate_passes_winning_walk_forward_result():
    """A walk-forward result that clears all four sub-criteria produces an
    all-passing gate result; the orchestrator's ``is_winning`` rule treats
    that as a win."""
    cfg = _config(
        walk_forward_enabled=True,
        dsr_threshold=0.5,
        max_is_oos_degradation_pct=40.0,
        min_oos_trades=10,
    )
    res = BacktestResult(
        total_return_pct=12.0,
        annualized_return_pct=14.0,
        volatility_pct=9.0,
        sharpe_ratio=1.4,
        max_drawdown_pct=5.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        deflated_sharpe=0.8,
        oos_sharpe=1.2,
        is_sharpe=1.3,
        is_oos_degradation_pct=10.0,
        oos_trade_count=40,
        regime_results=[
            {"regime": "vix_q1", "beat_benchmark": True},
            {"regime": "vix_q2", "beat_benchmark": True},
            {"regime": "vix_q3", "beat_benchmark": False},
            {"regime": "vix_q4", "beat_benchmark": False},
        ],
        calmar_ratio=0.0,
        sortino_ratio=0.0,
    )
    results = AcceptanceGate().check(res, cfg, n_trials=10)
    assert all(r.passed for r in results)
    assert (True and all(r.passed for r in results)) is True  # the orchestrator's rule


def test_acceptance_gate_rejects_overfit_pattern():
    """High IS Sharpe + collapsed OOS Sharpe + insufficient regime breadth
    must trip multiple sub-criteria, so the orchestrator marks the cycle
    as not-winning."""
    cfg = _config(
        walk_forward_enabled=True,
        dsr_threshold=0.9,
        max_is_oos_degradation_pct=30.0,
        min_oos_trades=30,
    )
    res = BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=22.0,
        volatility_pct=11.0,
        sharpe_ratio=2.5,  # IS-only sharpe (the headline single-window number)
        max_drawdown_pct=6.0,
        win_rate_pct=63.0,
        profit_factor=1.9,
        deflated_sharpe=0.3,  # low — overfit suspicion
        oos_sharpe=0.4,
        is_sharpe=2.5,
        is_oos_degradation_pct=84.0,  # huge IS→OOS gap
        oos_trade_count=12,  # below min_oos_trades
        regime_results=[
            {"regime": "vix_q1", "beat_benchmark": False},
            {"regime": "vix_q2", "beat_benchmark": False},
            {"regime": "vix_q3", "beat_benchmark": False},
            {"regime": "vix_q4", "beat_benchmark": False},
        ],
        calmar_ratio=0.0,
        sortino_ratio=0.0,
    )
    results = AcceptanceGate().check(res, cfg, n_trials=50)
    assert not all(r.passed for r in results)
    failed_reasons = [r.details for r in results if not r.passed]
    # Each of the four sub-criteria fails on this fixture.
    assert len(failed_reasons) == 4


# ---------------------------------------------------------------------------
# Helper purity: _daily_returns_from_trades and _equity_to_returns
# ---------------------------------------------------------------------------


def test_daily_returns_from_trades_handles_empty_input():
    """Returns an empty list rather than raising on an empty trade ledger."""
    out = StrategyLabOrchestrator._daily_returns_from_trades(
        [], 100_000.0, "2022-01-03", "2022-12-30"
    )
    assert out == [] or all(r == 0.0 for r in out)


def test_daily_returns_from_trades_emits_log_returns():
    """OOS-Sharpe / DSR / bootstrap CI need the same return convention as the
    in-sample Sharpe (log basis). A single +1k step on 100k equity should
    produce ``log(101_000 / 100_000)`` on the exit-date step, not the simple
    ``1_000 / 100_000`` ratio."""
    import math as _math

    trades = [
        _mk_trade(
            entry="2023-01-03",
            exit_="2023-01-04",
            net=1_000.0,
            symbol="TST",
        )
    ]
    out = StrategyLabOrchestrator._daily_returns_from_trades(
        trades, 100_000.0, "2023-01-03", "2023-01-05"
    )
    assert len(out) >= 1
    nonzero = [r for r in out if r != 0.0]
    assert len(nonzero) == 1
    assert nonzero[0] == pytest.approx(_math.log(101_000.0 / 100_000.0), rel=1e-12)
    # Sanity: simple-return basis would yield exactly 0.01, which differs
    # from log(1.01) ≈ 0.00995 — confirm we are NOT on simple basis.
    assert nonzero[0] != pytest.approx(0.01, abs=1e-6)


def test_daily_returns_from_trades_invalidates_ruin_series():
    """A run whose equity curve crosses zero is ruin: the OOS return series
    must NOT zero-pad the ruin step (which would let DSR / Sharpe CI report
    the strategy as materially safer than it is). The helper returns an
    empty list so every downstream consumer falls through its no-data
    path."""
    # Loss > initial capital drives equity negative on the exit date.
    trades = [
        _mk_trade(
            entry="2023-01-03",
            exit_="2023-01-04",
            net=-150_000.0,
            symbol="TST",
        )
    ]
    out = StrategyLabOrchestrator._daily_returns_from_trades(
        trades, 100_000.0, "2023-01-03", "2023-01-06"
    )
    assert out == []


def test_equity_to_returns_skips_zero_or_negative_prev():
    """Zero/negative previous equity yields a 0.0 return at that step (no
    ZeroDivisionError, no NaN)."""
    out = StrategyLabOrchestrator._equity_to_returns([100.0, 0.0, 50.0])
    assert len(out) == 2
    assert out[0] == pytest.approx(-1.0)
    assert out[1] == 0.0


def test_closes_to_equity_scales_to_initial_capital():
    out = StrategyLabOrchestrator._closes_to_equity([10.0, 11.0, 12.0], 100_000.0)
    assert out[0] == pytest.approx(100_000.0)
    assert out[-1] == pytest.approx(100_000.0 * 12.0 / 10.0)


# ---------------------------------------------------------------------------
# Review-comment fixes — IS Sharpe per training segment
# ---------------------------------------------------------------------------


def test_is_sharpe_uses_training_segments_not_full_span():
    """Per-fold IS Sharpe must be computed on the actual training date
    ranges. If we used ``config.start_date``/``config.end_date`` instead,
    the test+purge+embargo gap would show up as flat zero-return days and
    dilute the Sharpe — materially understating IS→OOS degradation.

    This test compares two scenarios with identical OOS trades but very
    different IS trade distributions (front-loaded vs back-loaded). The
    full-span computation would yield similar IS Sharpes for both because
    the gaps dominate; the per-segment computation produces visibly
    different IS Sharpes.
    """
    orch = _orchestrator(_StubMarketDataService())
    config = _config(walk_forward_enabled=True, n_folds=5)
    base_metrics = BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=6.0,
        volatility_pct=8.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=4.0,
        win_rate_pct=55.0,
        profit_factor=1.4,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )

    # Two trades per month, evenly distributed → at least one IS trade per
    # fold's training segments.
    trades = _trades_across_year(n_per_month=4, base_pnl=80.0)
    market_data = {"AAPL": _stub_bars("AAPL")}
    result = orch._evaluate_walk_forward(_spec(), market_data, config, trades, base_metrics)

    # Per-fold IS Sharpe must come from the segment computation: when
    # ``is_trade_count > 0``, the recorded ``is_sharpe`` should not be
    # zero except by genuine flat-return coincidence. With four winning-
    # losing alternations per month and 5 folds, at least one fold should
    # produce a strictly nonzero IS Sharpe under the per-segment fix.
    nonzero_is_sharpes = [
        fr["is_sharpe"]
        for fr in (result.fold_results or [])
        if fr.get("is_trade_count", 0) > 0 and fr.get("is_sharpe", 0.0) != 0.0
    ]
    assert nonzero_is_sharpes, (
        "Per-fold IS Sharpe should be nonzero for at least one fold once we "
        "compute on training segments instead of the full backtest span."
    )


# ---------------------------------------------------------------------------
# Review-comment fixes — walk-forward fallback re-checks anomalies
# ---------------------------------------------------------------------------


def test_walk_forward_fallback_rejects_overfit_via_anomaly_recheck(monkeypatch):
    """When walk-forward evaluation raises and we fall back to the legacy
    threshold path, the orchestrator must re-run anomaly checks with
    ``dsr_aware=False`` so a downgraded ``Sharpe > 5`` flag becomes
    critical again — preventing an obvious overfit from being marked
    winning on annualized return alone."""

    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.agents.alignment import TradeAlignmentReport

    orch = _orchestrator(_StubMarketDataService())

    # Force walk-forward to raise so we exercise the fallback path.
    def _raise(*args, **kwargs):
        raise RuntimeError("walk-forward fold construction failed (synthetic)")

    monkeypatch.setattr(orch, "_evaluate_walk_forward", _raise)

    # Stub the agents that would otherwise call the LLM.
    spec_dict = {
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(lhs="bar.close", op=">", rhs=0),
            ).model_dump()
        ],
        "exit_rules": [TimeStopRule(n_bars=5).model_dump()],
        "risk_limits": {},
        "speculative": False,
    }
    overfit_code = (
        "from contract import Strategy\n\nclass S(Strategy):\n"
        "    def on_bar(self, ctx, bar):\n"
        "        ctx.submit_order(symbol='X', qty=1, side='LONG')\n"
        "        ctx.submit_order(symbol='X', qty=1, side='FLAT')\n"
    )
    monkeypatch.setattr(
        orch.ideation_agent, "run", lambda **kw: (spec_dict, overfit_code, "rationale")
    )
    monkeypatch.setattr(
        orch.refinement_agent, "run", lambda **kw: ({"changes_made": "x"}, overfit_code)
    )
    monkeypatch.setattr(
        orch.alignment_agent, "run", lambda **kw: TradeAlignmentReport(aligned=True, rationale="ok")
    )
    monkeypatch.setattr(orch.analysis_agent, "run", lambda *a, **k: "narrative")
    # Issue #525 — _fetch_market_data returns a _MarketDataFetch envelope.
    from investment_team.strategy_lab.orchestrator import _MarketDataFetch

    monkeypatch.setattr(
        orch,
        "_fetch_market_data",
        lambda spec, config: _MarketDataFetch(
            data={"AAPL": _stub_bars("AAPL")},
            requested_symbols=["AAPL"],
            fetched_symbols=["AAPL"],
        ),
    )

    # Synthesize an "overfit" backtest: high Sharpe (>5) and high
    # annualized return (>WINNING_THRESHOLD). With dsr_aware=False the
    # Sharpe>5 critical is what we expect to upgrade severity on
    # fallback.
    overfit_result = BacktestResult(
        total_return_pct=80.0,
        annualized_return_pct=60.0,
        volatility_pct=8.0,
        sharpe_ratio=6.5,
        max_drawdown_pct=4.0,
        win_rate_pct=60.0,
        profit_factor=2.4,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    overfit_trades = _trades_across_year(n_per_month=4, base_pnl=80.0)

    class _StubExecResult:
        def __init__(self):
            self.success = True
            self.trades = overfit_trades
            self.execution_time_seconds = 0.01
            self.error_type = None
            self.stderr = ""
            self.execution_diagnostics = None

    monkeypatch.setattr(
        "investment_team.strategy_lab.orchestrator.run_strategy_code",
        lambda *a, **k: _StubExecResult(),
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.orchestrator.compute_metrics",
        lambda *a, **k: overfit_result,
    )

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    # The fallback path must reject this on the upgraded Sharpe>5 critical
    # even though annualized return clears WINNING_THRESHOLD.
    assert record.is_winning is False
    # The persisted gate-result history reflects the upgraded severity so
    # downstream consumers can audit the rejection reason.
    fallback_gates = [
        g for g in record.quality_gate_results if g.get("gate_name", "").startswith("fallback_")
    ]
    assert any(
        g.get("severity") == "critical" and "Sharpe ratio" in g.get("details", "")
        for g in fallback_gates
    )


# ---------------------------------------------------------------------------
# #529 — alignment-loop failure forces is_winning=False
# ---------------------------------------------------------------------------


def _wire_run_cycle_stubs(
    orch: StrategyLabOrchestrator,
    monkeypatch,
    *,
    alignment_aligned: bool,
    alignment_rationale: str = "ok",
    metrics: Optional[BacktestResult] = None,
    trades_override: Optional[List[TradeRecord]] = None,
) -> None:
    """Common stub wiring for end-to-end ``run_cycle`` tests below.

    Bypasses every LLM- or sandbox-touching call site so the orchestrator
    falls through to the is_winning resolution block with a deterministic
    set of inputs.
    """
    from investment_team.strategy_lab.agents.alignment import TradeAlignmentReport
    from investment_team.strategy_lab.orchestrator import _MarketDataFetch

    spec_dict = {
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(lhs="bar.close", op=">", rhs=0),
            ).model_dump()
        ],
        "exit_rules": [TimeStopRule(n_bars=5).model_dump()],
        "risk_limits": {},
        "speculative": False,
    }
    code = (
        "from contract import Strategy\n\nclass S(Strategy):\n"
        "    def on_bar(self, ctx, bar):\n"
        "        ctx.submit_order(symbol='X', qty=1, side='LONG')\n"
        "        ctx.submit_order(symbol='X', qty=1, side='FLAT')\n"
    )
    monkeypatch.setattr(orch.ideation_agent, "run", lambda **kw: (spec_dict, code, "rationale"))
    monkeypatch.setattr(orch.refinement_agent, "run", lambda **kw: ({"changes_made": "x"}, code))
    monkeypatch.setattr(
        orch.alignment_agent,
        "run",
        lambda **kw: TradeAlignmentReport(
            aligned=alignment_aligned, rationale=alignment_rationale, proposed_code=None
        ),
    )
    monkeypatch.setattr(orch.analysis_agent, "run", lambda *a, **k: "narrative")
    monkeypatch.setattr(
        orch,
        "_fetch_market_data",
        lambda spec, config: _MarketDataFetch(
            data={"AAPL": _stub_bars("AAPL")},
            requested_symbols=["AAPL"],
            fetched_symbols=["AAPL"],
        ),
    )

    sample_trades = (
        trades_override
        if trades_override is not None
        else _trades_across_year(n_per_month=4, base_pnl=80.0)
    )
    sample_metrics = (
        metrics
        if metrics is not None
        else BacktestResult(
            total_return_pct=18.0,
            annualized_return_pct=15.0,
            volatility_pct=8.0,
            sharpe_ratio=1.4,
            max_drawdown_pct=4.0,
            win_rate_pct=60.0,
            profit_factor=2.0,
            calmar_ratio=0.0,
            deflated_sharpe=0.0,
            sortino_ratio=0.0,
        )
    )

    class _StubExecResult:
        def __init__(self):
            self.success = True
            self.trades = sample_trades
            self.execution_time_seconds = 0.01
            self.error_type = None
            self.stderr = ""
            self.execution_diagnostics = None

    monkeypatch.setattr(
        "investment_team.strategy_lab.orchestrator.run_strategy_code",
        lambda *a, **k: _StubExecResult(),
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.orchestrator.compute_metrics",
        lambda *a, **k: sample_metrics,
    )


def test_failed_alignment_forces_is_winning_false_on_acceptance_path(monkeypatch):
    """#529: even when the walk-forward acceptance gate passes every check,
    a final alignment report with ``aligned=False`` must veto publication.
    The audit trail keeps the ``trade_alignment`` gate (passed=False) on
    ``quality_gate_results`` and surfaces ``alignment_failed`` on
    ``acceptance_reason`` for downstream consumers."""

    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult

    orch = _orchestrator(_StubMarketDataService())

    # Stub the acceptance gate to return all-passing (so alignment is the
    # only veto in play). Also stub walk-forward evaluation to leave the
    # metrics untouched so we don't have to construct a full OOS payload.
    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    monkeypatch.setattr(
        orch.acceptance_gate,
        "check",
        lambda metrics, config, n_trials: [
            QualityGateResult(
                gate_name="oos_deflated_sharpe",
                passed=True,
                severity="info",
                details="DSR passes",
            ),
            QualityGateResult(
                gate_name="is_oos_degradation",
                passed=True,
                severity="info",
                details="IS->OOS within tolerance",
            ),
        ],
    )

    _wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=False,
        alignment_rationale="entries fire before signal triggers",
    )

    # Pin the orchestrator → AnalysisAgent wiring (PR #573 review): if the
    # call ever drops the ``is_winning`` kwarg the narrative would silently
    # re-derive WINNING from metrics. The wide-stub above (``lambda *a, **k:
    # "narrative"``) would happily absorb that regression, so capture and
    # assert the kwarg directly here.
    captured_analysis_kwargs: dict = {}

    def _recording_analysis(*_args, **kwargs):
        captured_analysis_kwargs.update(kwargs)
        return "narrative"

    monkeypatch.setattr(orch.analysis_agent, "run", _recording_analysis)

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    assert record.is_winning is False
    alignment_gates = [
        g for g in record.quality_gate_results if g.get("gate_name") == "trade_alignment"
    ]
    assert alignment_gates, "trade_alignment gate must appear in quality_gate_results"
    assert all(g["passed"] is False for g in alignment_gates)
    assert record.analysis_narrative  # narrative still generated for context
    reason = record.backtest.result.acceptance_reason or ""
    assert "alignment_failed" in reason
    assert "entries fire before signal triggers" in reason
    # When the acceptance gate fully passed but alignment vetoed, the
    # ``"all four criteria met"`` summary is no longer truthful — the
    # alignment cause must REPLACE it, not appear alongside it (Bug 1).
    assert "all four criteria met" not in reason, (
        "acceptance_reason must not contradict itself: when acceptance "
        "fully passed but alignment vetoed, the alignment cause replaces "
        "the now-stale 'all four criteria met' summary."
    )
    # The orchestrator must thread the resolved verdict through so the
    # narrative template + outcome_label match the persisted record.
    assert captured_analysis_kwargs.get("is_winning") is False, (
        "orchestrator must pass is_winning=False to AnalysisAgent.run when "
        "alignment vetoes the publication (PR #573 review follow-up)."
    )
    # PR #573 round-4 regression guard: the alignment-augmentation block
    # used to bind a local ``rationale`` that shadowed the strategy
    # rationale, corrupting both the analysis prompt and
    # ``StrategyLabRecord.strategy_rationale`` on every alignment-failure
    # path. The ideation stub returns ``"rationale"`` as the strategy
    # rationale; if the shadowing comes back, ``record.strategy_rationale``
    # would be ``"entries fire before signal triggers"`` instead.
    assert record.strategy_rationale == "rationale", (
        "alignment-augmentation must not shadow the strategy rationale "
        f"(got {record.strategy_rationale!r})."
    )


def test_failed_alignment_forces_is_winning_false_on_walk_forward_fallback(monkeypatch):
    """#529: the walk-forward fallback branch (entered when
    ``_evaluate_walk_forward`` raised) must also honour the alignment
    veto. Even with annualized return > WINNING_THRESHOLD and no critical
    anomalies, ``is_winning`` is False when alignment fails."""

    from investment_team.models import StrategyLabRecord

    orch = _orchestrator(_StubMarketDataService())

    def _raise(*args, **kwargs):
        raise RuntimeError("walk-forward fold construction failed (synthetic)")

    monkeypatch.setattr(orch, "_evaluate_walk_forward", _raise)

    # Stub anomaly detector to return only info-severity (no critical) so
    # the fallback branch would have marked is_winning=True absent the
    # alignment check.
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult as _QGR

    monkeypatch.setattr(
        orch.anomaly_detector,
        "check",
        lambda *a, **kw: [
            _QGR(
                gate_name="sharpe_sane",
                passed=True,
                severity="info",
                details="ok",
            )
        ],
    )

    _wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=False,
        alignment_rationale="trades hit wrong symbol",
    )

    # Pin the orchestrator → AnalysisAgent wiring on this branch too
    # (Gap 6): the call site is shared, so a regression on either path
    # is the same root cause, but defensive duplication makes the
    # failure mode obvious from either test.
    captured_analysis_kwargs: dict = {}

    def _recording_analysis(*_args, **kwargs):
        captured_analysis_kwargs.update(kwargs)
        return "narrative"

    monkeypatch.setattr(orch.analysis_agent, "run", _recording_analysis)

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    assert record.is_winning is False
    alignment_gates = [
        g for g in record.quality_gate_results if g.get("gate_name") == "trade_alignment"
    ]
    assert alignment_gates and all(g["passed"] is False for g in alignment_gates)
    reason = record.backtest.result.acceptance_reason or ""
    assert "alignment_failed" in reason
    # Bug 1 symmetry on the fallback path: the anomaly recheck admitted
    # the run (no criticals + return > threshold), so its
    # ``"walk_forward_fallback_passed: ..."`` summary is no longer
    # truthful once alignment vetoes. The augmentation block must
    # REPLACE it (using ``upstream_admitted``) rather than append.
    assert "walk_forward_fallback_passed" not in reason, (
        "fallback success summary must be replaced (not appended) when "
        "alignment vetoes a fallback-admitted run."
    )
    assert captured_analysis_kwargs.get("is_winning") is False, (
        "orchestrator must pass is_winning=False on the walk-forward fallback path too (Gap 6)."
    )
    # PR #573 round-4 regression guard (rationale-shadowing). See the
    # parallel assertion in the acceptance-path test for the full story.
    assert record.strategy_rationale == "rationale", (
        "alignment-augmentation must not shadow the strategy rationale "
        f"(got {record.strategy_rationale!r})."
    )


def test_acceptance_failures_and_alignment_failure_both_recorded(monkeypatch):
    """#529 / PR #573 Bug 1: when the acceptance gate has real failure
    reasons AND alignment also fails, both must survive on the audit
    trail joined with ``" | "`` so a downstream parser can disambiguate
    the alignment boundary from ``summarize_acceptance_reason``'s
    internal ``"; "`` joiner."""

    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult

    orch = _orchestrator(_StubMarketDataService())

    monkeypatch.setattr(
        orch, "_evaluate_walk_forward", lambda spec, md, cfg, trades, metrics: metrics
    )
    # Acceptance gate returns two failing entries — their ``details``
    # strings get joined with ``"; "`` by ``summarize_acceptance_reason``.
    monkeypatch.setattr(
        orch.acceptance_gate,
        "check",
        lambda metrics, config, n_trials: [
            QualityGateResult(
                gate_name="oos_deflated_sharpe",
                passed=False,
                severity="critical",
                details="DSR below threshold",
            ),
            QualityGateResult(
                gate_name="oos_trade_count",
                passed=False,
                severity="critical",
                details="trade count below floor",
            ),
        ],
    )

    _wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=False,
        alignment_rationale="entries fire on wrong symbol",
    )

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    assert record.is_winning is False
    reason = record.backtest.result.acceptance_reason or ""
    # Both acceptance failures preserved (joined internally with "; ")
    assert "DSR below threshold" in reason
    assert "trade count below floor" in reason
    # Alignment cause appended after a " | " boundary so the categories
    # are distinguishable.
    assert " | alignment_failed:" in reason, (
        f"expected ' | alignment_failed:' boundary in {reason!r}"
    )
    assert "entries fire on wrong symbol" in reason
    # PR #573 round-4 regression guard (rationale-shadowing).
    assert record.strategy_rationale == "rationale", (
        "alignment-augmentation must not shadow the strategy rationale "
        f"(got {record.strategy_rationale!r})."
    )


def test_legacy_walk_forward_disabled_cannot_publish(monkeypatch):
    """#529: the legacy ``walk_forward_enabled=False`` publication path
    was removed. A run that skips walk-forward (still permitted to
    execute) cannot be marked winning even when alignment passes and
    annualized return clears the old WINNING_THRESHOLD.

    Gap 4: the audit trail must explain WHY publication was blocked —
    a record with ``is_winning=False`` and ``acceptance_reason=None``
    leaves a user guessing."""

    from investment_team.models import StrategyLabRecord

    orch = _orchestrator(_StubMarketDataService())

    _wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=True,
        alignment_rationale="trades match spec",
    )

    config = _config(walk_forward_enabled=False)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    assert record.is_winning is False
    # Narrative still generated — the run isn't a failure, it just can't
    # publish through the removed legacy path.
    assert record.analysis_narrative
    # Gap 4: the persisted record must explain why publication was
    # blocked. Permissive substring match so future wording tweaks
    # (e.g. adding remediation guidance) don't break the test —
    # the structural prefix is the contract.
    reason = record.backtest.result.acceptance_reason or ""
    assert "publication_disabled" in reason and "walk_forward_enabled=False" in reason, (
        f"expected publication_disabled / walk_forward_enabled=False in {reason!r}"
    )


def _raise_walk_forward(*_args, **_kwargs):
    raise RuntimeError("walk-forward fold construction failed (synthetic)")


def test_walk_forward_fallback_rejected_records_acceptance_reason(monkeypatch):
    """Gap 7: when the walk-forward fallback rejects via a critical
    anomaly (e.g. upgraded ``Sharpe > 5.0``), the persisted
    ``acceptance_reason`` must mirror that rejection — a consumer
    reading the field alone shouldn't have to know to grep
    ``quality_gate_results`` for ``fallback_`` entries."""

    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult as _QGR

    orch = _orchestrator(_StubMarketDataService())

    monkeypatch.setattr(orch, "_evaluate_walk_forward", _raise_walk_forward)

    # ``anomaly_detector.check`` is called twice in this flow: once
    # inside the refinement loop with ``dsr_aware=True`` (because
    # ``walk_forward_enabled=True``), and again on the fallback path
    # with ``dsr_aware=False``. We only want a critical to surface on
    # the second call; on the first, the loop would otherwise refine
    # forever and exhaust ``MAX_CODE_REFINEMENT_ROUNDS``.
    def _anomaly_stub(*_a, **kw):
        if kw.get("dsr_aware", False):
            return []  # refinement-time: pass through
        return [
            _QGR(
                gate_name="sharpe_sane",
                passed=False,
                severity="critical",
                details="Sharpe ratio 6.40 > 5.0 (overfit suspect)",
            )
        ]

    monkeypatch.setattr(orch.anomaly_detector, "check", _anomaly_stub)

    _wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=True,
        alignment_rationale="trades match spec",
    )

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    assert record.is_winning is False
    reason = record.backtest.result.acceptance_reason or ""
    assert reason.startswith("walk_forward_fallback_rejected:"), (
        f"expected walk_forward_fallback_rejected prefix in {reason!r}"
    )
    assert "Sharpe ratio 6.40" in reason


def test_walk_forward_fallback_passed_records_provenance(monkeypatch):
    """Gap 9: when the walk-forward fallback admits a run (no critical
    anomalies, return above threshold, alignment passes), the
    persisted ``acceptance_reason`` must record that provenance so
    the audit trail distinguishes a primary-acceptance-gate winner
    from a fallback-admitted winner."""

    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.quality_gates.models import QualityGateResult as _QGR

    orch = _orchestrator(_StubMarketDataService())

    monkeypatch.setattr(orch, "_evaluate_walk_forward", _raise_walk_forward)
    # Anomalies all info-severity — fallback_criticals stays empty.
    monkeypatch.setattr(
        orch.anomaly_detector,
        "check",
        lambda *a, **kw: [
            _QGR(gate_name="sharpe_sane", passed=True, severity="info", details="ok")
        ],
    )

    _wire_run_cycle_stubs(
        orch,
        monkeypatch,
        alignment_aligned=True,
        alignment_rationale="trades match spec",
    )

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    assert record.is_winning is True
    reason = record.backtest.result.acceptance_reason or ""
    assert "walk_forward_fallback_passed" in reason, (
        f"expected fallback-passed provenance in {reason!r}"
    )


def test_no_trades_produced_records_acceptance_reason(monkeypatch):
    """Gap 8: a run that drives ``execution_succeeded=True`` with zero
    trades (e.g. when the upstream gates have been stubbed out, or in
    production when an alignment-loop fix produces an empty ledger)
    can't publish — walk-forward and alignment both require trades.
    The persisted ``acceptance_reason`` must explain this rather than
    leave an empty field."""

    from investment_team.models import StrategyLabRecord

    orch = _orchestrator(_StubMarketDataService())

    # Bypass the gates that normally veto zero-trade runs during
    # refinement so we can drive ``execution_succeeded=True`` with
    # ``trades=[]`` and exercise the Gap 8 else-branch entry path.
    monkeypatch.setattr(orch.target_symbol_coverage_gate, "check_trades", lambda *a, **k: [])
    monkeypatch.setattr(orch.anomaly_detector, "check", lambda *a, **kw: [])

    _wire_run_cycle_stubs(
        orch,
        monkeypatch,
        # ``alignment_aligned`` is irrelevant — the alignment loop is
        # skipped when ``trades`` is empty.
        alignment_aligned=True,
        alignment_rationale="n/a",
        trades_override=[],
    )

    config = _config(walk_forward_enabled=True)
    record: StrategyLabRecord = orch.run_cycle(prior_records=[], config=config)

    assert record.is_winning is False
    reason = record.backtest.result.acceptance_reason or ""
    assert "publication_disabled" in reason and "no trades produced" in reason, (
        f"expected publication_disabled / no trades produced in {reason!r}"
    )

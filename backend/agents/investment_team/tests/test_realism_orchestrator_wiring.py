"""Realism-gate wiring inside :class:`StrategyLabOrchestrator`.

Exercises two seams:

* :meth:`_run_realism_gates` — pure helper that runs the verification-phase
  realism gates against the final spec + trade ledger. Empty inputs short-
  circuit; a multi-target spec with a single-symbol ledger emits the
  ``target_symbol_coverage`` breadth warning.
* :meth:`_run_verification_phase` — when realism produces a ``critical``
  result, ``acceptance_reason`` gains a ``realism_failed: ...`` suffix
  following the same caveat convention as ``exit_rule_conformance_failed``
  and ``alignment_failed``. Under the deterministic verdict this is a caveat
  only: it is recorded on ``acceptance_reason`` (and surfaces in the
  narrative) but never flips ``is_winning``, which follows the
  return-vs-benchmark rule.

Lower-level gate behaviour is covered by
``test_backtest_anomaly_realism.py`` and the breadth extensions in
``test_target_symbol_coverage.py``. This file only proves the orchestrator
threads them through to the publication decision.
"""

from __future__ import annotations

from typing import List, Optional

from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestResult,
    StrategySpec,
    TradeRecord,
)
from investment_team.strategy_lab.agents.alignment import TradeAlignmentReport
from investment_team.strategy_lab.alignment_findings import AlignmentFinding
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.quality_gates.realism.rule_firing import RuleFiringRateGate
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, SignalExitRule

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec(
    target_symbols: Optional[List[str]] = None, *, requires_custom_code: bool = False
) -> StrategySpec:
    return StrategySpec(
        strategy_id="realism-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0))],
        risk_limits={},
        speculative=False,
        target_symbols=target_symbols or [],
        requires_custom_code=requires_custom_code,
        strategy_code="from contract import Strategy\nclass S(Strategy):\n    def on_bar(self, ctx, bar):\n        pass\n",
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2020-01-01",
        end_date="2025-01-01",
        initial_capital=100_000.0,
        walk_forward_enabled=True,
    )


def _cost_stress_payload_passing() -> List[dict]:
    """Cost-stress sweep with a passing 2.0× row so the realism cost-stress
    gate doesn't veto in tests focused on other behaviour."""
    return [
        {
            "multiplier": 1.0,
            "sharpe_ratio": 1.2,
            "annualized_return_pct": 12.0,
            "max_drawdown_pct": 6.0,
            "trade_count": 100,
        },
        {
            "multiplier": 2.0,
            "sharpe_ratio": 0.5,
            "annualized_return_pct": 7.0,
            "max_drawdown_pct": 8.0,
            "trade_count": 100,
        },
        {
            "multiplier": 3.0,
            "sharpe_ratio": 0.1,
            "annualized_return_pct": 2.0,
            "max_drawdown_pct": 10.0,
            "trade_count": 100,
        },
    ]


def _metrics() -> BacktestResult:
    return BacktestResult(
        total_return_pct=20.0,
        annualized_return_pct=10.0,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=10.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        cost_stress_results=_cost_stress_payload_passing(),
        acceptance_reason="walk_forward_passed: all four criteria met",
    )


def _trade(symbol: str, trade_num: int) -> TradeRecord:
    return TradeRecord(
        trade_num=trade_num,
        entry_date=f"2024-{((trade_num % 12) + 1):02d}-{((trade_num % 27) + 1):02d}",
        exit_date=f"2024-{((trade_num % 12) + 1):02d}-{((trade_num % 27) + 2):02d}",
        symbol=symbol,
        side="long",
        entry_price=100.0,
        exit_price=101.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=10.0,
        net_pnl=10.0,
        return_pct=1.0,
        hold_days=14,
        outcome="win",
        cumulative_pnl=10.0 * trade_num,
        entry_reason="compiled_entry:entry[0]",
    )


def _orch() -> StrategyLabOrchestrator:
    """Minimal orchestrator instance for testing pure helpers.

    The constructor walks through agent and gate initialisation; nothing
    here hits the network or LLM provider, so the bare instance is safe to
    construct in tests.
    """
    return StrategyLabOrchestrator(convergence_tracker=ConvergenceTracker())


# ---------------------------------------------------------------------------
# _run_realism_gates
# ---------------------------------------------------------------------------


def test_run_realism_gates_returns_empty_when_no_trades():
    orch = _orch()
    results = orch._run_realism_gates(
        spec=_spec(target_symbols=["QQQ", "SPY"]),
        trades=[],
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )
    assert results == []


def test_run_realism_gates_returns_empty_when_execution_failed():
    orch = _orch()
    results = orch._run_realism_gates(
        spec=_spec(target_symbols=["QQQ", "SPY"]),
        trades=[_trade("QQQ", 1)],
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=False,
    )
    assert results == []


def test_run_realism_gates_emits_breadth_warning_for_multi_target_single_symbol_ledger():
    orch = _orch()
    spec = _spec(target_symbols=["QQQ", "SPY", "IWM"])
    trades = [_trade("QQQ", i + 1) for i in range(5)]

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    breadth_warnings = [
        r
        for r in results
        if not r.passed and r.severity == "warning" and r.gate_name == "target_symbol_coverage"
    ]
    assert len(breadth_warnings) == 1
    assert breadth_warnings[0].phase == "verification"
    assert "QQQ" in breadth_warnings[0].details


def test_run_realism_gates_passes_when_ledger_uses_full_universe():
    orch = _orch()
    spec = _spec(target_symbols=["QQQ", "SPY"])
    trades = [_trade("QQQ", 1), _trade("SPY", 2)]

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    # No criticals from any gate; breadth specifically passes (the
    # universe test is the focus here — other gates may emit warnings
    # from stub fixture gaps like unfired signal-exit rules).
    breadth = [r for r in results if r.gate_name == "target_symbol_coverage"]
    assert all(r.passed for r in breadth)
    assert not any(not r.passed and r.severity == "critical" for r in results)


def test_run_realism_gates_cost_stress_self_skips_when_not_requested_by_config():
    """When ``config.cost_stress=False`` (legacy single-window OR
    walk-forward-fallback path where the operator hand-built the config
    without enabling the sweep), the cost-stress realism gate must
    self-skip with an info result — never produce a critical that would
    veto a path the realism cycle isn't responsible for.

    Enforcement of "mandatory cost-stress on winning-candidate runs"
    lives at the production entrypoint (``_strategy_lab_worker``
    force-enables the flag), not inside the gate. This test is the
    regression guard against an earlier draft where any
    ``walk_forward_enabled=True`` config without cost-stress would have
    fired critical and broken every hand-built Strategy Lab run.
    """
    orch = _orch()
    spec = _spec(target_symbols=["QQQ"])
    trades = [_trade("QQQ", i + 1) for i in range(5)]
    metrics = BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=10.0,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=8.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        cost_stress_results=None,
    )
    # ``_config()`` here defaults to cost_stress=False (the BacktestConfig
    # default); only ``walk_forward_enabled=True`` is set explicitly.
    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=metrics,
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    cost_stress_results = [r for r in results if r.gate_name == "cost_stress_realism"]
    assert len(cost_stress_results) == 1
    assert cost_stress_results[0].passed is True
    assert cost_stress_results[0].severity == "info"
    assert "not requested" in cost_stress_results[0].details


def test_run_realism_gates_cost_stress_critical_when_2x_sharpe_negative():
    """End-to-end: a cost-stress payload with negative 2.0× Sharpe makes
    the realism cycle emit a critical that the caller will treat as a
    publication veto."""
    orch = _orch()
    spec = _spec(target_symbols=["QQQ"])
    trades = [_trade("QQQ", i + 1) for i in range(5)]
    bad_payload = [
        {
            "multiplier": 1.0,
            "sharpe_ratio": 1.0,
            "annualized_return_pct": 10.0,
            "max_drawdown_pct": 5.0,
            "trade_count": 50,
        },
        {
            "multiplier": 2.0,
            "sharpe_ratio": -0.4,
            "annualized_return_pct": -2.0,
            "max_drawdown_pct": 15.0,
            "trade_count": 50,
        },
    ]
    metrics = BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=10.0,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=8.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        cost_stress_results=bad_payload,
    )

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=metrics,
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    criticals = [r for r in results if not r.passed and r.severity == "critical"]
    assert len(criticals) == 1
    assert criticals[0].gate_name == "cost_stress_realism"


# ---------------------------------------------------------------------------
# _run_verification_phase: realism critical → veto
# ---------------------------------------------------------------------------


def test_verification_phase_records_realism_critical_as_caveat(monkeypatch):
    """A synthetic realism critical must rewrite ``acceptance_reason`` with the
    ``realism_failed:`` suffix so it surfaces as a narrative caveat — but the
    deterministic verdict keeps ``is_winning=True`` for this 10% (>= 8%) run.

    The acceptance gate is stubbed to pass cleanly so realism is the only
    finding in play; walk-forward evaluation is a no-op pass-through.
    """
    orch = _orch()

    # Walk-forward returns metrics unchanged so the acceptance branch fires.
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
                phase="verification",
                details="ok",
            ),
        ],
    )

    # Realism returns a critical finding (synthetic stand-in for any of the
    # eight realism rules — exercising the veto wiring, not the gate's
    # internal logic).
    monkeypatch.setattr(
        orch,
        "_run_realism_gates",
        lambda **_kwargs: [
            QualityGateResult(
                gate_name="liquidity_realism",
                passed=False,
                severity="critical",
                phase="verification",
                details="adjusted profit factor 0.7 < 1.0 after slippage haircut",
            ),
        ],
    )

    spec = _spec(target_symbols=["QQQ"])
    trades = [_trade("QQQ", i + 1) for i in range(20)]
    metrics = _metrics()
    config = _config()
    market_data: dict = {"QQQ": []}
    all_gate_results: List[QualityGateResult] = []

    outcome = orch._run_verification_phase(
        spec=spec,
        trades=trades,
        metrics=metrics,
        market_data=market_data,
        config=config,
        execution_succeeded=True,
        trades_aligned=True,
        alignment_reports=[],
        all_gate_results=all_gate_results,
        emit=lambda *_a, **_k: None,
    )

    # Caveats-only: the 10% return is at/above the 8% benchmark, so the
    # deterministic verdict is winning; realism does not flip the label.
    assert outcome.is_winning is True
    assert outcome.is_publishable is False
    assert outcome.publishability_skip_reason == "realism_failed"
    reason = outcome.metrics.acceptance_reason or ""
    assert "realism_failed:" in reason
    assert "liquidity_realism" not in reason  # detail string, not gate name
    assert "profit factor" in reason
    # The stale ``walk_forward_passed`` success summary must be REPLACED by
    # the caveat cause (matches the conformance + alignment caveat convention).
    assert "all four criteria met" not in reason


def test_verification_phase_does_not_veto_on_realism_warning(monkeypatch):
    """Warning-severity realism findings must NOT flip ``is_winning`` — only
    ``critical`` veto. The persisted gate list still records the warning."""
    orch = _orch()

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
                phase="verification",
                details="ok",
            ),
        ],
    )
    monkeypatch.setattr(
        orch,
        "_run_realism_gates",
        lambda **_kwargs: [
            QualityGateResult(
                gate_name="target_symbol_coverage",
                passed=False,
                severity="warning",
                phase="verification",
                details="single_symbol_breadth",
            ),
        ],
    )

    spec = _spec(target_symbols=["QQQ", "SPY"])
    trades = [_trade("QQQ", i + 1) for i in range(20)]
    all_gate_results: List[QualityGateResult] = []

    outcome = orch._run_verification_phase(
        spec=spec,
        trades=trades,
        metrics=_metrics(),
        market_data={"QQQ": []},
        config=_config(),
        execution_succeeded=True,
        trades_aligned=True,
        alignment_reports=[],
        all_gate_results=all_gate_results,
        emit=lambda *_a, **_k: None,
    )

    assert outcome.is_winning is True
    assert outcome.is_publishable is True
    assert outcome.publishability_skip_reason is None
    breadth_gates = [g for g in all_gate_results if g.gate_name == "target_symbol_coverage"]
    assert len(breadth_gates) == 1
    assert breadth_gates[0].severity == "warning"
    reason = outcome.metrics.acceptance_reason or ""
    assert "realism_failed:" not in reason


# ---------------------------------------------------------------------------
# rule_firing_rate_realism: custom-code alignment-findings signal wiring
# ---------------------------------------------------------------------------


def test_run_realism_gates_forwards_alignment_findings_to_rule_firing_gate():
    """A ``requires_custom_code=True`` spec with a dead entry rule (no
    passed ``entry[0]`` alignment finding) gets a real critical from
    ``rule_firing_rate_realism`` instead of the old info self-skip, once
    ``alignment_findings`` is threaded through."""
    orch = _orch()
    spec = _spec(target_symbols=["QQQ"], requires_custom_code=True)
    trades = [_trade("QQQ", i + 1) for i in range(5)]
    findings = [
        AlignmentFinding(
            trade_num=1,
            rule_id="entry[0]",
            check_name="entry_signal",
            passed=False,
            severity="critical",
            details="near miss",
        )
    ]

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=True,
        alignment_findings=findings,
    )

    rule_firing_criticals = [
        r for r in results if r.gate_name == "rule_firing_rate_realism" and r.severity == "critical"
    ]
    assert len(rule_firing_criticals) == 1
    assert rule_firing_criticals[0].passed is False
    assert rule_firing_criticals[0].rule_id == "entry[0]"


def test_run_realism_gates_custom_code_skips_when_alignment_findings_omitted():
    """Without ``alignment_findings`` (the default), a custom-code spec
    still gets the legacy info self-skip — no behavior change for callers
    that haven't been updated."""
    orch = _orch()
    spec = _spec(target_symbols=["QQQ"], requires_custom_code=True)
    trades = [_trade("QQQ", i + 1) for i in range(5)]

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    rule_firing_results = [r for r in results if r.gate_name == "rule_firing_rate_realism"]
    assert len(rule_firing_results) == 1
    assert rule_firing_results[0].passed is True
    assert rule_firing_results[0].severity == "info"
    assert "custom_code" in rule_firing_results[0].details.lower()


def test_verification_phase_threads_latest_alignment_report_findings(monkeypatch):
    """``_run_verification_phase`` must pass the LATEST alignment report's
    findings (not an earlier one) to the realism cycle, for a custom-code
    spec's dead entry rule to surface as a critical veto."""
    orch = _orch()

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
                phase="verification",
                details="ok",
            ),
        ],
    )

    captured: dict = {}
    real_run_realism_gates = orch._run_realism_gates

    def _spy(**kwargs):
        captured["alignment_findings"] = kwargs.get("alignment_findings")
        return real_run_realism_gates(**kwargs)

    monkeypatch.setattr(orch, "_run_realism_gates", _spy)

    spec = _spec(target_symbols=["QQQ"], requires_custom_code=True)
    trades = [_trade("QQQ", i + 1) for i in range(20)]
    metrics = _metrics()
    config = _config()
    market_data: dict = {"QQQ": []}
    all_gate_results: List[QualityGateResult] = []

    stale_report = TradeAlignmentReport(
        aligned=True,
        alignment_findings=[
            AlignmentFinding(
                trade_num=1,
                rule_id="entry[0]",
                check_name="entry_signal",
                passed=True,
                severity="info",
                details="stale — superseded by the latest report",
            )
        ],
    )
    latest_report = TradeAlignmentReport(
        aligned=True,
        alignment_findings=[
            AlignmentFinding(
                trade_num=1,
                rule_id="entry[0]",
                check_name="entry_signal",
                passed=False,
                severity="critical",
                details="latest report: never satisfied",
            )
        ],
    )

    orch._run_verification_phase(
        spec=spec,
        trades=trades,
        metrics=metrics,
        market_data=market_data,
        config=config,
        execution_succeeded=True,
        trades_aligned=True,
        alignment_reports=[stale_report, latest_report],
        all_gate_results=all_gate_results,
        emit=lambda *_a, **_k: None,
    )

    assert captured["alignment_findings"] == latest_report.alignment_findings
    rule_firing_criticals = [
        g
        for g in all_gate_results
        if g.gate_name == "rule_firing_rate_realism" and g.severity == "critical"
    ]
    assert len(rule_firing_criticals) == 1
    assert rule_firing_criticals[0].rule_id == "entry[0]"


# ---------------------------------------------------------------------------
# Custom-code parity: liquidity / regime coverage / trade clustering / cost
# stress gates take no ``spec`` and never branch on ``requires_custom_code``
# (see each gate's "custom-code parity" docstring note) — their inputs are
# fill-simulator/engine outputs populated identically regardless of compile
# path. These tests prove that concretely at the orchestrator level: each
# gate still fires its critical for a ``requires_custom_code=True`` spec
# exactly as it would for a compiled one.
# ---------------------------------------------------------------------------


def _adv_bar(date_str: str, *, close: float, volume: float) -> OHLCVBar:
    return OHLCVBar(
        date=date_str,
        open=close,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=volume,
    )


def _thin_liquidity_market_data(
    symbol: str, *, daily_dollar_volume: float, lookback: int = 20
) -> dict:
    """Market data whose trailing ADV equals ``daily_dollar_volume`` for
    ``symbol`` over ``lookback`` bars preceding the trade window."""
    bars = []
    close = 100.0
    volume = daily_dollar_volume / close
    for i in range(lookback):
        bars.append(_adv_bar(f"2024-02-{i + 1:02d}", close=close, volume=volume))
    return {symbol: bars}


def _oversized_trade(symbol: str, trade_num: int) -> TradeRecord:
    """A single winning trade whose ``position_value`` dwarfs the thin ADV
    from :func:`_thin_liquidity_market_data`, so the liquidity slippage
    haircut flips its adjusted P&L negative."""
    return TradeRecord(
        trade_num=trade_num,
        entry_date="2024-03-01",
        exit_date="2024-03-02",
        symbol=symbol,
        side="long",
        entry_price=100.0,
        exit_price=101.0,
        shares=500.0,
        position_value=50_000.0,
        gross_pnl=500.0,
        net_pnl=500.0,
        return_pct=1.0,
        hold_days=1,
        outcome="win",
        cumulative_pnl=500.0,
        entry_reason="compiled_entry:entry[0]",
    )


def test_run_realism_gates_liquidity_critical_for_custom_code_spec():
    """A custom-code spec with an oversized fill against thin ADV still
    trips the liquidity realism critical — the gate has no ``spec``
    parameter to skip on, so scrutiny is identical to the compiled path."""
    orch = _orch()
    spec = _spec(target_symbols=["QQQ"], requires_custom_code=True)
    trades = [_oversized_trade("QQQ", 1)]
    market_data = _thin_liquidity_market_data("QQQ", daily_dollar_volume=100_000.0)

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=_metrics(),
        config=_config(),
        market_data=market_data,
        execution_succeeded=True,
    )

    liquidity_criticals = [
        r for r in results if r.gate_name == "liquidity_realism" and r.severity == "critical"
    ]
    assert len(liquidity_criticals) == 1
    assert liquidity_criticals[0].passed is False


def _metrics_with_regime_results(regime_results: List[dict]) -> BacktestResult:
    return BacktestResult(
        total_return_pct=20.0,
        annualized_return_pct=10.0,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=10.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        cost_stress_results=_cost_stress_payload_passing(),
        regime_results=regime_results,
        acceptance_reason="walk_forward_passed: all four criteria met",
    )


def test_run_realism_gates_regime_coverage_critical_for_custom_code_spec():
    """A custom-code spec that lost money in a regime it actually traded
    in still trips the regime-coverage critical."""
    orch = _orch()
    spec = _spec(target_symbols=["QQQ"], requires_custom_code=True)
    trades = [_trade("QQQ", i + 1) for i in range(5)]
    metrics = _metrics_with_regime_results(
        [
            {
                "regime": "vix_q1",
                "n_obs": 20,
                "strategy_cumret": -0.05,
                "benchmark_cumret": 0.05,
                "beat_benchmark": False,
            },
            {
                "regime": "vix_q2",
                "n_obs": 20,
                "strategy_cumret": 0.03,
                "benchmark_cumret": 0.05,
                "beat_benchmark": False,
            },
        ]
    )

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=metrics,
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    regime_criticals = [
        r for r in results if r.gate_name == "regime_coverage_realism" and r.severity == "critical"
    ]
    assert len(regime_criticals) == 1
    assert regime_criticals[0].passed is False


def _dated_trade(symbol: str, trade_num: int, entry_date: str) -> TradeRecord:
    return TradeRecord(
        trade_num=trade_num,
        entry_date=entry_date,
        exit_date=entry_date,
        symbol=symbol,
        side="long",
        entry_price=100.0,
        exit_price=101.0,
        shares=10.0,
        position_value=1000.0,
        gross_pnl=10.0,
        net_pnl=10.0,
        return_pct=1.0,
        hold_days=1,
        outcome="win",
        cumulative_pnl=10.0 * trade_num,
        entry_reason="compiled_entry:entry[0]",
    )


def _clustered_trades(symbol: str) -> List[TradeRecord]:
    """20 trades, 16 bursty within 2020-Q2 (consecutive April days), 4
    spread across 2020-Q3/Q4 — mirrors
    ``test_trade_clustering_gate.py``'s critical-path fixture."""
    trades: List[TradeRecord] = []
    for i in range(16):
        trades.append(_dated_trade(symbol, i + 1, f"2020-04-{i + 1:02d}"))
    trades.append(_dated_trade(symbol, 17, "2020-07-15"))
    trades.append(_dated_trade(symbol, 18, "2020-08-22"))
    trades.append(_dated_trade(symbol, 19, "2020-10-05"))
    trades.append(_dated_trade(symbol, 20, "2020-12-18"))
    return trades


def test_run_realism_gates_trade_clustering_critical_for_custom_code_spec():
    """A custom-code spec whose trades are bursty-clustered in a single
    calendar quarter still trips the trade-clustering critical."""
    orch = _orch()
    spec = _spec(target_symbols=["QQQ"], requires_custom_code=True)
    trades = _clustered_trades("QQQ")

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=_metrics(),
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    clustering_criticals = [
        r for r in results if r.gate_name == "trade_clustering_realism" and r.severity == "critical"
    ]
    assert len(clustering_criticals) == 1
    assert clustering_criticals[0].passed is False


def test_run_realism_gates_cost_stress_critical_for_custom_code_spec():
    """A custom-code spec whose 2.0x cost-stress Sharpe goes negative still
    trips the cost-stress critical — same payload as
    ``test_run_realism_gates_cost_stress_critical_when_2x_sharpe_negative``,
    just with ``requires_custom_code=True``."""
    orch = _orch()
    spec = _spec(target_symbols=["QQQ"], requires_custom_code=True)
    trades = [_trade("QQQ", i + 1) for i in range(5)]
    bad_payload = [
        {
            "multiplier": 1.0,
            "sharpe_ratio": 1.0,
            "annualized_return_pct": 10.0,
            "max_drawdown_pct": 5.0,
            "trade_count": 50,
        },
        {
            "multiplier": 2.0,
            "sharpe_ratio": -0.4,
            "annualized_return_pct": -2.0,
            "max_drawdown_pct": 15.0,
            "trade_count": 50,
        },
    ]
    metrics = BacktestResult(
        total_return_pct=18.0,
        annualized_return_pct=10.0,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=8.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        cost_stress_results=bad_payload,
    )

    results = orch._run_realism_gates(
        spec=spec,
        trades=trades,
        metrics=metrics,
        config=_config(),
        market_data=None,
        execution_succeeded=True,
    )

    cost_stress_criticals = [
        r for r in results if r.gate_name == "cost_stress_realism" and r.severity == "critical"
    ]
    assert len(cost_stress_criticals) == 1
    assert cost_stress_criticals[0].passed is False


# ---------------------------------------------------------------------------
# Regression: the old self-skip bug (#2575/#2601)
#
# Before #2599, ``RuleFiringRateGate`` unconditionally self-skipped at
# ``info`` for any ``requires_custom_code=True`` spec, because its only
# signal (the compiler's ``entry_reason``/``exit_reason`` annotation) is
# absent for LLM-authored ``on_bar`` code. That meant a custom-code
# strategy with a genuinely dead entry rule — the predicate never actually
# satisfied — sailed through verification and could be published, with
# nothing in the realism cycle able to catch it.
# ---------------------------------------------------------------------------


def test_verification_phase_vetoes_custom_code_strategy_with_dead_entry_rule_regression(
    monkeypatch,
):
    """Regression case for the pre-#2599 self-skip bug: a custom-code
    strategy whose only entry rule never actually fires (alignment
    findings confirm zero satisfied ``entry[0]`` checks) is now vetoed by
    the full ``_run_verification_phase`` pipeline instead of shipping
    clean.

    The contrast assertion at the end proves the bug was real and precise:
    calling the gate directly the way pre-#2599 callers did (no
    ``alignment_findings`` kwarg) still reproduces the old ``info``
    self-skip on this exact spec/trade pair — the only thing that changed
    is that the production ``_run_verification_phase`` path now always
    threads the latest alignment report's findings through, closing the
    gap for real runs.
    """
    orch = _orch()

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
                phase="verification",
                details="ok",
            ),
        ],
    )

    spec = _spec(target_symbols=["QQQ"], requires_custom_code=True)
    trades = [_trade("QQQ", i + 1) for i in range(20)]
    metrics = _metrics()
    config = _config()
    market_data: dict = {"QQQ": []}
    all_gate_results: List[QualityGateResult] = []

    dead_rule_report = TradeAlignmentReport(
        aligned=True,
        alignment_findings=[
            AlignmentFinding(
                trade_num=1,
                rule_id="entry[0]",
                check_name="entry_signal",
                passed=False,
                severity="critical",
                details="predicate never satisfied at the trade's actual signal bar",
            )
        ],
    )

    outcome = orch._run_verification_phase(
        spec=spec,
        trades=trades,
        metrics=metrics,
        market_data=market_data,
        config=config,
        execution_succeeded=True,
        trades_aligned=True,
        alignment_reports=[dead_rule_report],
        all_gate_results=all_gate_results,
        emit=lambda *_a, **_k: None,
    )

    # Fixed behavior: the dead entry rule is caught and vetoes publication.
    assert outcome.is_publishable is False
    assert outcome.publishability_skip_reason == "realism_failed"
    reason = outcome.metrics.acceptance_reason or ""
    assert "realism_failed:" in reason
    rule_firing_criticals = [
        g
        for g in all_gate_results
        if g.gate_name == "rule_firing_rate_realism" and g.severity == "critical"
    ]
    assert len(rule_firing_criticals) == 1
    assert rule_firing_criticals[0].rule_id == "entry[0]"

    # Contrast: the pre-#2599 call contract (no alignment_findings kwarg)
    # on this exact spec/trade pair still reproduces the old self-skip —
    # proving the fix is specifically about threading alignment_findings
    # through, not a change to the dead-rule scenario itself.
    legacy_call_results = RuleFiringRateGate().check(spec, trades)
    assert len(legacy_call_results) == 1
    assert legacy_call_results[0].passed is True
    assert legacy_call_results[0].severity == "info"
    assert "custom_code" in legacy_call_results[0].details.lower()

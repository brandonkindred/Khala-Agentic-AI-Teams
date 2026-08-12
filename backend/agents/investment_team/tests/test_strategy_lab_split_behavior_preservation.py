"""Behavior-preservation regression for the `_run_synthesis_loop` and
`_run_design_attempt` splits.

The two functions were decomposed into per-stage helpers (universe
injection, validation, fetch, reachability, execution, trade collection,
evaluation; and design/review, code synthesis, refinement/alignment,
verification/analysis, record assembly). Helper-level contracts live in
``test_strategy_lab_synthesis_helpers.py`` and the design-loop suites.
This module locks the *composed* outputs — spec, code, trades, metrics,
and gate results — for a representative set of scenarios so a later
extraction or control-flow edit cannot silently change what the loops
return.

Collaborators (gates, sandbox, market-data fetch, LLM agents) are stubbed;
the orchestrator methods under test run for real.
"""

from __future__ import annotations

import textwrap
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from investment_team.models import BacktestConfig, BacktestResult, StrategySpec, TradeRecord
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator, _MarketDataFetch
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)
from investment_team.tests.conftest import stub_design_loop, varying_code_refine
from investment_team.tests.test_strategy_lab_alignment import (
    _aligned_check_result,
    _benign_sandbox_trades,
    _code_exec,
)
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

pytestmark = pytest.mark.strategy_lab_integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_CONFORMANT_CODE = textwrap.dedent(
    """
    from contract import Strategy

    class S(Strategy):
        UNIVERSE = ("QQQ",)

        def on_bar(self, ctx, bar):
            if bar.symbol not in self.UNIVERSE:
                return
            if sma(ctx.history(bar.symbol, 50), 50) > 0:
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
    """
).strip()


def _spec_dict() -> Dict[str, Any]:
    """Build a valid RSI spec dict with a QQQ universe.

    Pre: none.
    Post: a fresh dict ``build_spec_from_dict`` / ``StrategySpec.model_validate``
    accepts; ``target_symbols`` is ``["QQQ"]`` so universe injection is a
    no-op against ``_CONFORMANT_CODE``.
    """
    return {
        "asset_class": "stocks",
        "hypothesis": "RSI mean reversion on a small universe",
        "signal_definition": "RSI(14) crossings",
        "timeframe": "1d",
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30,
                ),
            ).model_dump()
        ],
        "exit_rules": [
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70,
                )
            ).model_dump()
        ],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "target_symbols": ["QQQ"],
        "speculative": False,
    }


def _spec() -> StrategySpec:
    """Build a ``StrategySpec`` matching ``_spec_dict()`` plus conformant code.

    Pre: none.
    Post: ``strategy_id`` is set; ``strategy_code`` is ``_CONFORMANT_CODE``.
    """
    return StrategySpec(
        strategy_id="split-regression",
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI mean reversion on a small universe",
        signal_definition="RSI(14) crossings",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30,
                ),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70,
                )
            )
        ],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        target_symbols=["QQQ"],
        strategy_code=_CONFORMANT_CODE,
    )


def _config() -> BacktestConfig:
    """Build the canonical lab window used by the other orchestrator suites.

    Pre: none.
    Post: walk-forward is disabled so verification does not re-partition
    the ledger; fee defaults match the design-phase override guard.
    """
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
        walk_forward_enabled=False,
    )


def _gate(
    name: str,
    *,
    passed: bool,
    severity: str = "critical",
    phase: str = "synthesis",
    details: str = "x",
    refinement_round: int = 0,
) -> QualityGateResult:
    """Build a ``QualityGateResult`` with a stable ``evaluated_at``.

    Pre: ``severity`` is one of info/warning/critical.
    Post: ``evaluated_at`` is the sentinel ``1970-01-01T00:00:00+00:00`` so
    gate snapshots compare without timestamp noise.
    """
    return QualityGateResult(
        gate_name=name,
        passed=passed,
        severity=severity,  # type: ignore[arg-type]
        phase=phase,  # type: ignore[arg-type]
        details=details,
        refinement_round=refinement_round,
        evaluated_at="1970-01-01T00:00:00+00:00",
    )


def _gate_snapshot(gates: List[QualityGateResult]) -> List[Dict[str, Any]]:
    """Project gates to the fields the split must preserve.

    Pre: ``gates`` is a list of ``QualityGateResult``.
    Post: each entry carries ``gate_name`` / ``passed`` / ``severity`` /
    ``phase`` / ``details`` / ``refinement_round`` / ``rule_id``; timestamps
    are dropped.
    """
    return [
        {
            "gate_name": g.gate_name,
            "passed": g.passed,
            "severity": g.severity,
            "phase": g.phase,
            "details": g.details,
            "refinement_round": g.refinement_round,
            "rule_id": g.rule_id,
        }
        for g in gates
    ]


def _record_gate_snapshot(record_gates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Project persisted record gates the same way as ``_gate_snapshot``.

    Pre: ``record_gates`` is ``StrategyLabRecord.quality_gate_results``.
    Post: same keys as ``_gate_snapshot``.
    """
    return [
        {
            "gate_name": g.get("gate_name"),
            "passed": g.get("passed"),
            "severity": g.get("severity"),
            "phase": g.get("phase"),
            "details": g.get("details"),
            "refinement_round": g.get("refinement_round"),
            "rule_id": g.get("rule_id"),
        }
        for g in record_gates
    ]


def _trade_snapshot(trades: List[TradeRecord]) -> List[Tuple[Any, ...]]:
    """Project trades to the identity the split must preserve.

    Pre: ``trades`` is a list of ``TradeRecord``.
    Post: tuples of (trade_num, symbol, side, entry_date, exit_date,
    shares, gross_pnl, net_pnl, outcome).
    """
    return [
        (
            t.trade_num,
            t.symbol,
            t.side,
            t.entry_date,
            t.exit_date,
            round(t.shares, 4),
            round(t.gross_pnl, 2),
            round(t.net_pnl, 2),
            t.outcome,
        )
        for t in trades
    ]


def _metrics_snapshot(metrics: BacktestResult) -> Dict[str, Any]:
    """Project metrics to the identity the split must preserve.

    Pre: ``metrics`` is a ``BacktestResult``.
    Post: every field ``compute_metrics`` / ``_compute_metrics_daily``
    populates. Walk-forward, coverage, and diagnostics attachments are
    excluded so a verification stub cannot mask a synthesis-loop
    regression.
    """
    return {
        "total_return_pct": metrics.total_return_pct,
        "annualized_return_pct": metrics.annualized_return_pct,
        "volatility_pct": metrics.volatility_pct,
        "sharpe_ratio": metrics.sharpe_ratio,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "win_rate_pct": metrics.win_rate_pct,
        "profit_factor": metrics.profit_factor,
        "sortino_ratio": metrics.sortino_ratio,
        "calmar_ratio": metrics.calmar_ratio,
        "max_drawdown_duration_days": metrics.max_drawdown_duration_days,
        "risk_free_rate": metrics.risk_free_rate,
        "alpha_pct": metrics.alpha_pct,
        "beta": metrics.beta,
        "information_ratio": metrics.information_ratio,
        "deflated_sharpe": metrics.deflated_sharpe,
    }


def _populated_fetch() -> Callable[..., _MarketDataFetch]:
    """Build a ``_fetch_market_data`` stub that returns a non-empty envelope.

    Pre: none.
    Post: ``data`` is a truthy dict (so the no-market-data short-circuit
    is not taken) keyed on QQQ; ``provider_used`` is the sentinel ``stub``.
    """

    def _fetch(*_a: Any, **_kw: Any) -> _MarketDataFetch:
        return _MarketDataFetch(
            data={"QQQ": []},
            requested_symbols=["QQQ"],
            fetched_symbols=["QQQ"],
            provider_used={"QQQ": "stub"},
        )

    return _fetch


def _empty_fetch() -> Callable[..., _MarketDataFetch]:
    """Build a ``_fetch_market_data`` stub that takes the no-data path.

    Pre: none.
    Post: ``data`` is ``None``, which ``_fetch_market_data_for_synthesis``
    treats as fatal and records a ``market_data`` gate.
    """

    def _fetch(*_a: Any, **_kw: Any) -> _MarketDataFetch:
        return _MarketDataFetch(
            data=None,
            requested_symbols=["QQQ"],
            fetched_symbols=[],
            provider_used={},
        )

    return _fetch


def _neutralize_synthesis_gates(
    monkeypatch: pytest.MonkeyPatch,
    orch: StrategyLabOrchestrator,
    *,
    anomaly_gates: Optional[List[QualityGateResult]] = None,
) -> None:
    """Stub every synthesis-loop gate collaborator to a clean pass.

    Pre: ``orch`` is a constructed ``StrategyLabOrchestrator``.
    Post: validation, coverage, reachability, and anomaly checks return
    no criticals. ``anomaly_gates`` (default: one info-severity pass)
    is what ``_check_anomalies_cached`` returns, so evaluation records
    a deterministic gate without depending on the real detector.
    ``MAX_CODE_REFINEMENT_ROUNDS`` is pinned to 2 so two-round recovery
    scenarios still reach round 1 when the process env has the valid
    floor value ``STRATEGY_LAB_MAX_CODE_REFINEMENT_ROUNDS=1``.
    """
    monkeypatch.setattr(orchestrator_module, "MAX_CODE_REFINEMENT_ROUNDS", 2)
    monkeypatch.setattr(orch.spec_readiness_gate, "validate", lambda *a, **kw: [])
    monkeypatch.setattr(orch.code_safety_checker, "check", lambda *a, **kw: [])
    monkeypatch.setattr(orch.code_conformance_gate, "check", lambda *a, **kw: [])
    monkeypatch.setattr(orch.predicate_conformance_gate, "check", lambda *a, **kw: [])
    monkeypatch.setattr(orch.target_symbol_coverage_gate, "check_fetch", lambda *a, **kw: [])
    monkeypatch.setattr(orch.target_symbol_coverage_gate, "check_trades", lambda *a, **kw: [])
    monkeypatch.setattr(orch.predicate_reachability_probe, "probe", lambda *a, **kw: [])
    monkeypatch.setattr(
        orch.predicate_reachability_probe,
        "to_gate_results",
        lambda *a, **kw: [_gate("predicate_reachability", passed=True, severity="info")],
    )
    recorded = (
        anomaly_gates
        if anomaly_gates is not None
        else [_gate("backtest_anomaly", passed=True, severity="info")]
    )
    monkeypatch.setattr(orch, "_check_anomalies_cached", lambda *a, **kw: list(recorded))


def _run_synthesis(
    orch: StrategyLabOrchestrator,
    *,
    spec: Optional[StrategySpec] = None,
    code: str = _CONFORMANT_CODE,
) -> Tuple[Any, List[QualityGateResult], List[str], List[str]]:
    """Drive ``_run_synthesis_loop`` and return outcome plus mutated lists.

    Pre: ``orch`` collaborators the loop reaches have already been stubbed.
    Post: the four-tuple is ``(outcome, all_gate_results, refinement_attempts,
    zero_trade_attempts)``; the three lists are the same objects the loop
    mutated in place.
    """
    all_gate_results: List[QualityGateResult] = []
    refinement_attempts: List[str] = []
    zero_trade_attempts: List[str] = []
    outcome = orch._run_synthesis_loop(
        spec=spec or _spec(),
        code=code,
        config=_config(),
        all_gate_results=all_gate_results,
        refinement_attempts=refinement_attempts,
        zero_trade_attempts=zero_trade_attempts,
        emit=lambda *a, **k: None,
    )
    return outcome, all_gate_results, refinement_attempts, zero_trade_attempts


# ---------------------------------------------------------------------------
# _run_synthesis_loop — representative scenarios
# ---------------------------------------------------------------------------


def test_synthesis_happy_path_preserves_spec_code_trades_metrics_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean validation → fetch → execute with trades → evaluation success.

    The composed loop must return the input spec/code (universe injection
    is a no-op on already-conformant source), the sandbox trade ledger,
    metrics computed from that ledger, and the reachability + anomaly
    gates recorded in that order at round 0.
    """
    orch = StrategyLabOrchestrator()
    _neutralize_synthesis_gates(monkeypatch, orch)
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _populated_fetch())
    exec_result = _code_exec(success=True, raw_trades=_benign_sandbox_trades())
    monkeypatch.setattr(orchestrator_module, "run_strategy_code", lambda *a, **k: exec_result)

    outcome, gates, refinements, zero_trade = _run_synthesis(orch)

    assert outcome.execution_succeeded is True
    assert outcome.max_rounds_exhausted is False
    assert outcome.refinement_stalled is False
    assert outcome.code == _CONFORMANT_CODE
    assert outcome.spec.target_symbols == ["QQQ"]
    assert outcome.spec.hypothesis == "RSI mean reversion on a small universe"
    assert outcome.requested_symbols == ["QQQ"]
    assert outcome.fetched_symbols == ["QQQ"]
    assert outcome.provider_used == {"QQQ": "stub"}
    assert outcome.market_data == {"QQQ": []}
    assert _trade_snapshot(outcome.trades) == _trade_snapshot(exec_result.trades)
    expected_metrics = orchestrator_module.compute_metrics(
        exec_result.trades, 100_000.0, "2023-01-01", "2023-12-31"
    )
    assert _metrics_snapshot(outcome.metrics) == _metrics_snapshot(expected_metrics)
    assert refinements == []
    assert zero_trade == []
    assert _gate_snapshot(gates) == _gate_snapshot(
        [
            _gate("predicate_reachability", passed=True, severity="info"),
            _gate("backtest_anomaly", passed=True, severity="info"),
        ]
    )


def test_synthesis_no_market_data_preserves_short_circuit_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``data=None`` fetch records the market_data gate and breaks.

    Spec/code stay at the post-injection values; trades stay empty; the
    loop does not claim success or round-cap exhaustion.
    """
    orch = StrategyLabOrchestrator()
    _neutralize_synthesis_gates(monkeypatch, orch)
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _empty_fetch())
    monkeypatch.setattr(
        orchestrator_module,
        "run_strategy_code",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sandbox must not run")),
    )

    outcome, gates, refinements, _zero_trade = _run_synthesis(orch)

    assert outcome.execution_succeeded is False
    assert outcome.max_rounds_exhausted is False
    assert outcome.code == _CONFORMANT_CODE
    assert outcome.spec.target_symbols == ["QQQ"]
    assert outcome.trades == []
    assert outcome.market_data is None
    assert outcome.requested_symbols == ["QQQ"]
    assert outcome.fetched_symbols == []
    assert refinements == []
    # Fetch short-circuits before the reachability probe, so the only
    # recorded gate is the fatal market_data result.
    assert [g["gate_name"] for g in _gate_snapshot(gates)] == ["market_data"]
    market_gate = next(g for g in gates if g.gate_name == "market_data")
    assert market_gate.passed is False
    assert market_gate.severity == "critical"
    assert market_gate.phase == "synthesis"
    assert "stocks" in market_gate.details
    assert market_gate.refinement_round == 0


def test_synthesis_validation_critical_then_success_preserves_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 0 fails code-safety; round 1 passes and converges.

    The refined code (a trailing comment from ``varying_code_refine``) is
    what the loop returns, together with the sandbox trades and the
    round-1 anomaly gate. Round 0's critical is preserved on the running
    gate list.
    """
    orch = StrategyLabOrchestrator()
    _neutralize_synthesis_gates(monkeypatch, orch)
    safety_calls = {"n": 0}

    def _safety(_code: str, _spec: Any = None) -> List[QualityGateResult]:
        safety_calls["n"] += 1
        if safety_calls["n"] == 1:
            return [
                _gate(
                    "code_safety",
                    passed=False,
                    details="forced critical for test",
                )
            ]
        return []

    monkeypatch.setattr(orch.code_safety_checker, "check", _safety)
    monkeypatch.setattr(orch.refinement_agent, "run", varying_code_refine(_CONFORMANT_CODE))
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _populated_fetch())
    exec_result = _code_exec(success=True, raw_trades=_benign_sandbox_trades())
    monkeypatch.setattr(orchestrator_module, "run_strategy_code", lambda *a, **k: exec_result)

    outcome, gates, refinements, _zero_trade = _run_synthesis(orch)

    assert outcome.execution_succeeded is True
    assert outcome.max_rounds_exhausted is False
    assert outcome.code != _CONFORMANT_CODE
    assert "# refinement round 0" in outcome.code
    assert _trade_snapshot(outcome.trades) == _trade_snapshot(exec_result.trades)
    expected_metrics = orchestrator_module.compute_metrics(
        exec_result.trades, 100_000.0, "2023-01-01", "2023-12-31"
    )
    assert _metrics_snapshot(outcome.metrics) == _metrics_snapshot(expected_metrics)
    assert len(refinements) == 1
    names = [g["gate_name"] for g in _gate_snapshot(gates)]
    assert names[0] == "code_safety"
    assert gates[0].passed is False
    assert gates[0].refinement_round == 0
    assert "predicate_reachability" in names
    assert "backtest_anomaly" in names
    anomaly = next(g for g in gates if g.gate_name == "backtest_anomaly")
    assert anomaly.refinement_round == 1


def test_synthesis_execution_failure_then_success_preserves_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 0 sandbox-fails; round 1 succeeds with the fixture ledger.

    ``varying_code_refine`` keeps the backtest cache from replaying round
    0's failure. The ``code_execution`` critical stays on the gate list
    at round 0; the successful ledger is the returned trades/metrics.
    """
    orch = StrategyLabOrchestrator()
    _neutralize_synthesis_gates(monkeypatch, orch)
    monkeypatch.setattr(orch.refinement_agent, "run", varying_code_refine(_CONFORMANT_CODE))
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _populated_fetch())
    success_result = _code_exec(success=True, raw_trades=_benign_sandbox_trades())
    sandbox_calls = {"n": 0}

    def _sandbox(*_a: Any, **_kw: Any) -> StrategyRunResult:
        sandbox_calls["n"] += 1
        if sandbox_calls["n"] == 1:
            return StrategyRunResult(success=False, error_type="runtime_error", stderr="boom")
        return success_result

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _sandbox)

    outcome, gates, refinements, _zero_trade = _run_synthesis(orch)

    assert outcome.execution_succeeded is True
    assert sandbox_calls["n"] == 2
    assert _trade_snapshot(outcome.trades) == _trade_snapshot(success_result.trades)
    expected_metrics = orchestrator_module.compute_metrics(
        success_result.trades, 100_000.0, "2023-01-01", "2023-12-31"
    )
    assert _metrics_snapshot(outcome.metrics) == _metrics_snapshot(expected_metrics)
    assert len(refinements) == 1
    exec_gate = next(g for g in gates if g.gate_name == "code_execution")
    assert exec_gate.passed is False
    assert exec_gate.severity == "critical"
    assert "runtime_error" in exec_gate.details and "boom" in exec_gate.details
    assert exec_gate.refinement_round == 0


def test_synthesis_trade_coverage_critical_preserves_break_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A critical trade-coverage gate breaks the loop with the collected trades.

    ``max_rounds_exhausted`` is True (the pre-extraction break site) and
    ``execution_succeeded`` stays False; the sandbox trades are still on
    the outcome so callers can inspect what coverage rejected.
    """
    orch = StrategyLabOrchestrator()
    _neutralize_synthesis_gates(monkeypatch, orch)
    monkeypatch.setattr(
        orch.target_symbol_coverage_gate,
        "check_trades",
        lambda spec, trades: [
            _gate("target_symbol_coverage", passed=False, details="no target-symbol trades")
        ],
    )
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _populated_fetch())
    exec_result = _code_exec(success=True, raw_trades=_benign_sandbox_trades())
    monkeypatch.setattr(orchestrator_module, "run_strategy_code", lambda *a, **k: exec_result)

    outcome, gates, refinements, _zero_trade = _run_synthesis(orch)

    assert outcome.execution_succeeded is False
    assert outcome.max_rounds_exhausted is True
    assert _trade_snapshot(outcome.trades) == _trade_snapshot(exec_result.trades)
    assert refinements == []
    coverage = next(g for g in gates if g.gate_name == "target_symbol_coverage")
    assert coverage.passed is False
    assert coverage.severity == "critical"
    assert coverage.refinement_round == 0
    assert not any(g.gate_name == "backtest_anomaly" for g in gates)


def test_synthesis_evaluation_continue_then_success_preserves_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 0 anomaly-critical continues; round 1 succeeds.

    ``_check_anomalies_cached`` is stubbed with a call counter so the
    attempt-scoped anomaly cache cannot freeze the first critical. The
    returned trades/metrics are the successful round's ledger.
    """
    orch = StrategyLabOrchestrator()
    _neutralize_synthesis_gates(monkeypatch, orch)
    anomaly_calls = {"n": 0}

    def _anomalies(*_a: Any, **_kw: Any) -> List[QualityGateResult]:
        anomaly_calls["n"] += 1
        if anomaly_calls["n"] == 1:
            return [
                _gate(
                    "backtest_anomaly",
                    passed=False,
                    details="forced anomaly for test",
                )
            ]
        return [_gate("backtest_anomaly", passed=True, severity="info")]

    monkeypatch.setattr(orch, "_check_anomalies_cached", _anomalies)
    monkeypatch.setattr(orch.refinement_agent, "run", varying_code_refine(_CONFORMANT_CODE))
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _populated_fetch())
    exec_result = _code_exec(success=True, raw_trades=_benign_sandbox_trades())
    monkeypatch.setattr(orchestrator_module, "run_strategy_code", lambda *a, **k: exec_result)

    outcome, gates, refinements, _zero_trade = _run_synthesis(orch)

    assert outcome.execution_succeeded is True
    assert anomaly_calls["n"] == 2
    assert _trade_snapshot(outcome.trades) == _trade_snapshot(exec_result.trades)
    expected_metrics = orchestrator_module.compute_metrics(
        exec_result.trades, 100_000.0, "2023-01-01", "2023-12-31"
    )
    assert _metrics_snapshot(outcome.metrics) == _metrics_snapshot(expected_metrics)
    assert len(refinements) == 1
    anomaly_gates = [g for g in gates if g.gate_name == "backtest_anomaly"]
    assert len(anomaly_gates) == 2
    assert anomaly_gates[0].passed is False
    assert anomaly_gates[0].refinement_round == 0
    assert anomaly_gates[1].passed is True
    assert anomaly_gates[1].refinement_round == 1


# ---------------------------------------------------------------------------
# _run_design_attempt — representative scenarios
# ---------------------------------------------------------------------------


def _stub_design_attempt_downstream(
    monkeypatch: pytest.MonkeyPatch,
    orch: StrategyLabOrchestrator,
) -> None:
    """Stub alignment / verification / analysis leaves for a design attempt.

    Pre: ``orch`` is constructed; synthesis-loop gates are already neutralized.
    Post: the alignment checker reports aligned on the first audit (no LLM
    fix); realism and exit-rule conformance record a single info gate each;
    analysis returns the sentinel narrative ``scripted narrative``.
    """
    monkeypatch.setattr(
        orch.deterministic_alignment_checker,
        "check",
        lambda **_kw: _aligned_check_result(),
    )
    monkeypatch.setattr(
        orch,
        "_run_realism_gates",
        lambda **_kw: [_gate("realism", passed=True, severity="info", phase="verification")],
    )
    monkeypatch.setattr(
        orch,
        "_run_exit_rule_conformance_gate",
        lambda **_kw: True,
    )
    monkeypatch.setattr(
        orch.analysis_agent,
        "run",
        lambda *_a, **_kw: "scripted narrative",
    )


def test_design_attempt_happy_path_preserves_spec_code_trades_metrics_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Design converges → synthesis succeeds → record carries the ledger.

    The thin ``_run_design_attempt`` orchestrator must thread the
    synthesis-loop spec/code/trades/metrics onto the assembled record
    together with the pre-synthesis + synthesis + verification gates.
    """
    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, _spec_dict(), _CONFORMANT_CODE)
    _neutralize_synthesis_gates(monkeypatch, orch)
    _stub_design_attempt_downstream(monkeypatch, orch)
    monkeypatch.setattr(orch.strategy_validator, "validate", lambda _spec: [])
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _populated_fetch())
    exec_result = _code_exec(success=True, raw_trades=_benign_sandbox_trades())
    monkeypatch.setattr(orchestrator_module, "run_strategy_code", lambda *a, **k: exec_result)

    record = orch._run_design_attempt(
        prior_records=[],
        config=_config(),
        signal_brief=None,
        emit=lambda *a, **k: None,
        exclude_asset_classes=None,
        directives=[],
    )

    assert record.strategy.hypothesis == "RSI mean reversion on a small universe"
    assert record.strategy.target_symbols == ["QQQ"]
    assert record.strategy_code == _CONFORMANT_CODE
    assert record.original_code == _CONFORMANT_CODE
    assert _trade_snapshot(record.backtest.trades) == _trade_snapshot(exec_result.trades)
    expected_metrics = orchestrator_module.compute_metrics(
        exec_result.trades, 100_000.0, "2023-01-01", "2023-12-31"
    )
    assert _metrics_snapshot(record.backtest.result) == _metrics_snapshot(expected_metrics)
    assert record.analysis_narrative == "scripted narrative"
    assert record.strategy_rationale == "scripted rationale"
    gate_names = [g["gate_name"] for g in _record_gate_snapshot(record.quality_gate_results)]
    assert "predicate_reachability" in gate_names
    assert "backtest_anomaly" in gate_names
    assert "realism" in gate_names
    assert record.backtest.status == "completed"


def test_design_attempt_no_market_data_preserves_failed_record_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetch returns no data → record keeps the synthesized spec/code and
    empty trades, with the market_data gate on the persisted list.
    """
    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, _spec_dict(), _CONFORMANT_CODE)
    _neutralize_synthesis_gates(monkeypatch, orch)
    _stub_design_attempt_downstream(monkeypatch, orch)
    monkeypatch.setattr(orch.strategy_validator, "validate", lambda _spec: [])
    monkeypatch.setattr(StrategyLabOrchestrator, "_fetch_market_data", _empty_fetch())
    monkeypatch.setattr(
        orchestrator_module,
        "run_strategy_code",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sandbox must not run")),
    )

    record = orch._run_design_attempt(
        prior_records=[],
        config=_config(),
        signal_brief=None,
        emit=lambda *a, **k: None,
        exclude_asset_classes=None,
        directives=[],
    )

    assert record.strategy.hypothesis == "RSI mean reversion on a small universe"
    assert record.strategy_code == _CONFORMANT_CODE
    assert record.backtest.trades == []
    assert record.is_winning is False
    gate_names = [g["gate_name"] for g in _record_gate_snapshot(record.quality_gate_results)]
    assert "market_data" in gate_names
    market_gate = next(
        g for g in record.quality_gate_results if g.get("gate_name") == "market_data"
    )
    assert market_gate.get("passed") is False
    assert market_gate.get("severity") == "critical"
    assert market_gate.get("refinement_round") == 0


def test_design_attempt_design_not_ready_preserves_short_circuit_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec with no entry rules never reaches synthesis.

    The assembled short-circuit record keeps the failed spec, empty
    trades/metrics-adjacent fields, and a critical readiness gate; the
    sandbox is never called.
    """
    bad_spec = {
        "asset_class": "stocks",
        "hypothesis": "test",
        "signal_definition": "sig",
        "entry_rules": [],
        "exit_rules": [
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70,
                )
            ).model_dump()
        ],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "speculative": False,
    }
    orch = StrategyLabOrchestrator()
    stub_design_loop(monkeypatch, orch, bad_spec, _CONFORMANT_CODE)
    monkeypatch.setattr(
        orchestrator_module,
        "run_strategy_code",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sandbox must not run")),
    )

    record = orch._run_design_attempt(
        prior_records=[],
        config=_config(),
        signal_brief=None,
        emit=lambda *a, **k: None,
        exclude_asset_classes=None,
        directives=[],
    )

    assert record.backtest.status in {
        "failed: design_not_ready",
        "failed: design_stalled",
    }
    assert record.is_winning is False
    assert record.backtest.trades == []
    assert record.strategy.hypothesis == "test"
    assert record.strategy.entry_rules == []
    pre_synth = [g for g in record.quality_gate_results if g.get("refinement_round") == -1]
    assert pre_synth
    assert any(g.get("severity") == "critical" and not g.get("passed") for g in pre_synth)

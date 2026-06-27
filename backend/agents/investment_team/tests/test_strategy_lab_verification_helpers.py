"""Direct unit coverage for the verification-phase helpers extracted out of
:meth:`StrategyLabOrchestrator._run_verification_phase`.

``_run_verification_phase`` was decomposed into four named helpers, each with
its own contract:

- :meth:`_run_walk_forward_acceptance` — walk-forward + acceptance gate (or the
  fallback signal when it raises / is ineligible).
- :meth:`_run_exit_rule_conformance_gate` — deterministic exit-rule conformance.
- :meth:`_resolve_publication_decision` — the three publication paths.
- :meth:`_apply_publication_vetoes` — caveat stamps on ``acceptance_reason``.

The whole-phase wiring is still covered by ``test_runtime_lookahead_veto.py``
and ``test_strategy_lab_walk_forward_integration.py``; this file pins the
extracted pieces in isolation so each contract has direct coverage.
"""

from __future__ import annotations

from typing import List

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
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, SignalExitRule

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="verif-helper-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=0))],
        risk_limits={},
        speculative=False,
        target_symbols=["QQQ"],
        strategy_code=(
            "from contract import Strategy\n"
            "class S(Strategy):\n"
            "    def on_bar(self, ctx, bar):\n"
            "        pass\n"
        ),
    )


def _config(**overrides) -> BacktestConfig:
    base = dict(
        start_date="2020-01-01",
        end_date="2025-01-01",
        initial_capital=100_000.0,
        walk_forward_enabled=True,
    )
    base.update(overrides)
    return BacktestConfig(**base)


def _metrics(annualized: float = 10.0, acceptance_reason: str = "") -> BacktestResult:
    return BacktestResult(
        total_return_pct=20.0,
        annualized_return_pct=annualized,
        volatility_pct=12.0,
        sharpe_ratio=0.8,
        max_drawdown_pct=10.0,
        win_rate_pct=58.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
        acceptance_reason=acceptance_reason,
    )


def _trade(trade_num: int = 1) -> TradeRecord:
    return TradeRecord(
        trade_num=trade_num,
        entry_date=f"2024-{((trade_num % 12) + 1):02d}-{((trade_num % 27) + 1):02d}",
        exit_date=f"2024-{((trade_num % 12) + 1):02d}-{((trade_num % 27) + 2):02d}",
        symbol="QQQ",
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
    )


def _gate(
    gate_name: str, *, passed: bool, severity: str = "critical", details: str = "x"
) -> QualityGateResult:
    return QualityGateResult(
        gate_name=gate_name,
        passed=passed,
        severity=severity,
        phase="verification",
        details=details,
    )


def _orch() -> StrategyLabOrchestrator:
    return StrategyLabOrchestrator(convergence_tracker=ConvergenceTracker())


# ---------------------------------------------------------------------------
# _run_walk_forward_acceptance
# ---------------------------------------------------------------------------


def test_walk_forward_acceptance_ineligible_returns_inputs_unchanged() -> None:
    """Ineligible (no execution) → inputs returned verbatim, no gate appended,
    no emit fired."""
    orch = _orch()
    metrics = _metrics()
    gates: List[QualityGateResult] = []
    emitted: List = []

    out_metrics, accept_results, wf_failed = orch._run_walk_forward_acceptance(
        spec=_spec(),
        market_data=None,
        config=_config(),
        trades=[],
        metrics=metrics,
        execution_succeeded=False,
        all_gate_results=gates,
        emit=lambda *a, **k: emitted.append(a),
    )

    assert out_metrics is metrics
    assert accept_results == []
    assert wf_failed is False
    assert gates == []
    assert emitted == []


def test_walk_forward_acceptance_success_appends_gates_and_stamps_reason(monkeypatch) -> None:
    """Eligible + clean run → acceptance results appended, reason stamped,
    ``walk_forward_failed`` False."""
    orch = _orch()
    monkeypatch.setattr(orch, "_evaluate_walk_forward", lambda spec, md, cfg, tr, m: m)
    passing = _gate("oos_deflated_sharpe", passed=True, severity="info", details="ok")
    monkeypatch.setattr(orch.acceptance_gate, "check", lambda metrics, config, n_trials: [passing])
    gates: List[QualityGateResult] = []

    out_metrics, accept_results, wf_failed = orch._run_walk_forward_acceptance(
        spec=_spec(),
        market_data={"QQQ": []},
        config=_config(),
        trades=[_trade()],
        metrics=_metrics(),
        execution_succeeded=True,
        all_gate_results=gates,
        emit=lambda *a, **k: None,
    )

    assert wf_failed is False
    assert accept_results == [passing]
    assert gates == [passing]
    assert out_metrics.acceptance_reason  # summarized, non-empty


def test_walk_forward_acceptance_exception_signals_fallback(monkeypatch) -> None:
    """A raising walk-forward is caught and converted to the fallback signal."""
    orch = _orch()

    def _boom(*_a, **_k):
        raise RuntimeError("walk-forward exploded")

    monkeypatch.setattr(orch, "_evaluate_walk_forward", _boom)
    gates: List[QualityGateResult] = []
    metrics = _metrics()

    out_metrics, accept_results, wf_failed = orch._run_walk_forward_acceptance(
        spec=_spec(),
        market_data={"QQQ": []},
        config=_config(),
        trades=[_trade()],
        metrics=metrics,
        execution_succeeded=True,
        all_gate_results=gates,
        emit=lambda *a, **k: None,
    )

    assert wf_failed is True
    assert accept_results == []
    assert out_metrics is metrics
    assert gates == []


# ---------------------------------------------------------------------------
# _run_exit_rule_conformance_gate
# ---------------------------------------------------------------------------


def test_exit_rule_conformance_skipped_when_no_trades() -> None:
    """No execution / no trades → passes vacuously, nothing appended."""
    orch = _orch()
    gates: List[QualityGateResult] = []

    passed = orch._run_exit_rule_conformance_gate(
        spec=_spec(),
        trades=[],
        metrics=_metrics(),
        config=_config(),
        execution_succeeded=False,
        all_gate_results=gates,
    )

    assert passed is True
    assert gates == []


def test_exit_rule_conformance_runs_and_appends_results() -> None:
    """A run with trades exercises the real gate and appends its results."""
    orch = _orch()
    gates: List[QualityGateResult] = []

    passed = orch._run_exit_rule_conformance_gate(
        spec=_spec(),
        trades=[_trade(i + 1) for i in range(3)],
        metrics=_metrics(),
        config=_config(),
        execution_succeeded=True,
        all_gate_results=gates,
    )

    assert isinstance(passed, bool)
    # The gate ran, so at least one conformance result was recorded.
    assert gates and all(isinstance(g, QualityGateResult) for g in gates)


# ---------------------------------------------------------------------------
# _resolve_publication_decision
# ---------------------------------------------------------------------------


def test_publication_decision_acceptance_path_admits_on_pass() -> None:
    """Non-empty acceptance results that all pass → upstream_admitted True,
    metrics untouched."""
    orch = _orch()
    metrics = _metrics()
    gates: List[QualityGateResult] = []

    out_metrics, admitted = orch._resolve_publication_decision(
        metrics=metrics,
        trades=[_trade()],
        market_data={"QQQ": []},
        config=_config(),
        execution_succeeded=True,
        acceptance_results=[_gate("oos", passed=True, severity="info")],
        walk_forward_failed=False,
        all_gate_results=gates,
    )

    assert admitted is True
    assert out_metrics is metrics
    assert gates == []


def test_publication_decision_else_path_stamps_execution_failed() -> None:
    """No acceptance results, no fallback → publication_disabled reason."""
    orch = _orch()

    out_metrics, admitted = orch._resolve_publication_decision(
        metrics=_metrics(),
        trades=[],
        market_data=None,
        config=_config(),
        execution_succeeded=False,
        acceptance_results=[],
        walk_forward_failed=False,
        all_gate_results=[],
    )

    assert admitted is False
    assert out_metrics.acceptance_reason == "publication_disabled: execution_failed"


def test_publication_decision_else_path_stamps_no_trades() -> None:
    """Executed but produced no trades (no acceptance / no fallback) →
    ``publication_disabled: no trades produced``."""
    orch = _orch()

    out_metrics, admitted = orch._resolve_publication_decision(
        metrics=_metrics(),
        trades=[],
        market_data={"QQQ": []},
        config=_config(),
        execution_succeeded=True,
        acceptance_results=[],
        walk_forward_failed=False,
        all_gate_results=[],
    )

    assert admitted is False
    assert out_metrics.acceptance_reason == "publication_disabled: no trades produced"


def test_publication_decision_else_path_stamps_walk_forward_disabled() -> None:
    """Executed with trades but walk-forward disabled (no acceptance / no
    fallback) → ``publication_disabled: walk_forward_enabled=False``."""
    orch = _orch()

    out_metrics, admitted = orch._resolve_publication_decision(
        metrics=_metrics(),
        trades=[_trade()],
        market_data={"QQQ": []},
        config=_config(walk_forward_enabled=False),
        execution_succeeded=True,
        acceptance_results=[],
        walk_forward_failed=False,
        all_gate_results=[],
    )

    assert admitted is False
    assert out_metrics.acceptance_reason == "publication_disabled: walk_forward_enabled=False"


def test_publication_decision_fallback_passes_on_clean_recheck(monkeypatch) -> None:
    """Fallback path with a clean anomaly recheck and qualifying return →
    admitted, reason stamped, no fallback_ gates appended."""
    orch = _orch()
    monkeypatch.setattr(
        orch.anomaly_detector,
        "check",
        lambda *a, **k: [],
    )
    gates: List[QualityGateResult] = []

    out_metrics, admitted = orch._resolve_publication_decision(
        metrics=_metrics(annualized=12.0),
        trades=[_trade()],
        market_data={"QQQ": []},
        config=_config(),
        execution_succeeded=True,
        acceptance_results=[],
        walk_forward_failed=True,
        all_gate_results=gates,
    )

    assert admitted is True
    assert out_metrics.acceptance_reason == "walk_forward_fallback_passed: anomaly recheck clean"
    assert gates == []


def test_publication_decision_fallback_rejects_on_critical(monkeypatch) -> None:
    """Fallback path with a critical anomaly → rejected, fallback_ gates
    recorded, rejection reason stamped."""
    orch = _orch()
    monkeypatch.setattr(
        orch.anomaly_detector,
        "check",
        lambda *a, **k: [
            _gate("sharpe_too_high", passed=False, severity="critical", details="overfit")
        ],
    )
    gates: List[QualityGateResult] = []

    out_metrics, admitted = orch._resolve_publication_decision(
        metrics=_metrics(annualized=12.0),
        trades=[_trade()],
        market_data={"QQQ": []},
        config=_config(),
        execution_succeeded=True,
        acceptance_results=[],
        walk_forward_failed=True,
        all_gate_results=gates,
    )

    assert admitted is False
    assert out_metrics.acceptance_reason.startswith("walk_forward_fallback_rejected:")
    # The fallback anomalies were recorded with the ``fallback_`` prefix.
    assert gates and any(g.gate_name.startswith("fallback_") for g in gates)


# ---------------------------------------------------------------------------
# _apply_publication_vetoes
# ---------------------------------------------------------------------------


def _veto_kwargs(**overrides):
    base = dict(
        execution_succeeded=True,
        trades=[_trade()],
        exit_rule_conformance_passed=True,
        all_gate_results=[],
        realism_passed=True,
        realism_critical=[],
        alignment_reports=[],
        trades_aligned=True,
        runtime_lookahead_violation=False,
        upstream_admitted=True,
    )
    base.update(overrides)
    return base


def test_apply_vetoes_no_op_when_all_clear() -> None:
    """All gates clear and no look-ahead → metrics and admission untouched."""
    orch = _orch()
    metrics = _metrics(acceptance_reason="walk_forward_passed: ok")

    out_metrics, admitted = orch._apply_publication_vetoes(metrics=metrics, **_veto_kwargs())

    assert out_metrics is metrics
    assert admitted is True


def test_apply_vetoes_conformance_failure_stamps_reason() -> None:
    """A conformance critical stamps ``exit_rule_conformance_failed`` and
    demotes admission."""
    orch = _orch()
    critical = _gate("exit_rule_conformance", passed=False, severity="critical", details="leaked")

    out_metrics, admitted = orch._apply_publication_vetoes(
        metrics=_metrics(acceptance_reason="walk_forward_passed: ok"),
        **_veto_kwargs(
            exit_rule_conformance_passed=False,
            all_gate_results=[critical],
        ),
    )

    assert "exit_rule_conformance_failed: leaked" in (out_metrics.acceptance_reason or "")
    assert admitted is False


def test_apply_vetoes_realism_failure_stamps_reason() -> None:
    """A realism critical stamps ``realism_failed`` and demotes admission."""
    orch = _orch()
    realism_crit = _gate("cost_stress", passed=False, severity="critical", details="too costly")

    out_metrics, admitted = orch._apply_publication_vetoes(
        metrics=_metrics(acceptance_reason="walk_forward_passed: ok"),
        **_veto_kwargs(realism_passed=False, realism_critical=[realism_crit]),
    )

    assert "realism_failed: too costly" in (out_metrics.acceptance_reason or "")
    assert admitted is False


def test_apply_vetoes_alignment_failure_stamps_reason() -> None:
    """Unresolved alignment (``trades_aligned=False`` + a critical alignment
    finding on the last report) stamps ``alignment_unresolved`` and demotes
    admission."""
    orch = _orch()
    finding = AlignmentFinding(
        trade_num=1,
        check_name="entry_signal",
        passed=False,
        severity="critical",
        details="entry fired without a signal",
    )
    report = TradeAlignmentReport(
        aligned=False,
        rationale="off-spec",
        alignment_findings=[finding],
    )

    out_metrics, admitted = orch._apply_publication_vetoes(
        metrics=_metrics(acceptance_reason="walk_forward_passed: ok"),
        **_veto_kwargs(
            trades_aligned=False,
            alignment_reports=[report],
        ),
    )

    assert "alignment_unresolved:" in (out_metrics.acceptance_reason or "")
    assert "entry fired without a signal" in (out_metrics.acceptance_reason or "")
    assert admitted is False


def test_apply_vetoes_runtime_lookahead_stamps_cause() -> None:
    """A runtime look-ahead records its cause as a caveat."""
    orch = _orch()

    out_metrics, _admitted = orch._apply_publication_vetoes(
        metrics=_metrics(acceptance_reason="walk_forward_passed: ok"),
        **_veto_kwargs(runtime_lookahead_violation=True),
    )

    assert "lookahead_violation_at_runtime: subprocess_attribute_error" in (
        out_metrics.acceptance_reason or ""
    )

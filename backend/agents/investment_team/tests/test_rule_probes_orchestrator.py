"""Orchestrator-level wiring tests for the rule-probes gate.

The synthesis loop runs validate → fetch → execute → evaluate. These
tests stub the gates so the loop short-circuits inside the validate
phase, which is where the rule-probes wiring lives. We then assert:

1. ``rule_probes_gate.check`` is invoked when ``CodeConformanceGate`` has
   no critical failures.
2. ``rule_probes_gate.check`` is **not** invoked when conformance returns
   a critical (probing already-broken code yields noisy diagnostics on
   top of the cleaner conformance critical).
3. Probe-critical results are routed through ``_refine_or_exhaust`` with
   ``failure_phase="validation"`` and the failing ``rule_id`` surfaced in
   ``failure_details``.
"""

from __future__ import annotations

from typing import List

from investment_team.models import BacktestConfig, StrategySpec
from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
)


def _info_result(gate_name: str, *, phase: str = "synthesis") -> QualityGateResult:
    return QualityGateResult(
        gate_name=gate_name,
        passed=True,
        details="ok",
        severity="info",
        phase=phase,
    )


def _critical_result(gate_name: str, *, rule_id=None) -> QualityGateResult:
    return QualityGateResult(
        gate_name=gate_name,
        passed=False,
        details=f"forced critical from {gate_name}",
        severity="critical",
        phase="synthesis",
        rule_id=rule_id,
    )


def _minimal_spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="orch-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30.0,
                ),
            )
        ],
        target_symbols=["PROBE"],
        strategy_code="class S:\n    pass\n",
    )


def _minimal_config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-03-01",
        initial_capital=1_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )


def _stub_clean_gates(orch: StrategyLabOrchestrator) -> None:
    """Force every gate that runs before rule_probes to return clean."""
    orch.spec_readiness_gate.validate = lambda spec, **kw: [_info_result("spec_readiness")]
    orch.code_safety_checker.check = lambda code, spec: [_info_result("code_safety")]
    orch.code_conformance_gate.check = lambda code, spec: [_info_result("code_conformance")]


# ---------------------------------------------------------------------------
# Wiring test 1: probe gate runs when conformance is clean
# ---------------------------------------------------------------------------


def test_probe_gate_runs_when_conformance_is_clean(monkeypatch):
    orch = StrategyLabOrchestrator()
    _stub_clean_gates(orch)

    invocations: List[tuple] = []

    def fake_probe_check(code, spec, *, phase="synthesis"):
        invocations.append((code, spec))
        return [_critical_result("rule_probes", rule_id="entry[0]")]

    orch.rule_probes_gate.check = fake_probe_check

    # Capture _refine_or_exhaust calls and immediately exhaust so the loop
    # terminates after round 1.
    captured: List[dict] = []

    def fake_refine(*, spec, code, failure_phase, failure_details, **kw):
        captured.append({"failure_phase": failure_phase, "failure_details": failure_details})
        return spec, code, True  # exhausted

    orch._refine_or_exhaust = fake_refine

    spec = _minimal_spec()
    outcome = orch._run_synthesis_loop(
        spec=spec,
        code=spec.strategy_code,
        config=_minimal_config(),
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *args, **kwargs: None,
    )

    assert invocations, "rule_probes_gate.check should run when conformance is clean"
    assert captured, "_refine_or_exhaust should be invoked on probe-critical failure"
    assert captured[0]["failure_phase"] == "validation"
    # rule_id must be surfaced in the aggregated failure details so the
    # refinement agent can target the failing branch.
    assert "rule_probes:entry[0]" in captured[0]["failure_details"]
    assert outcome.max_rounds_exhausted is True


# ---------------------------------------------------------------------------
# Wiring test 2: probe gate is skipped when conformance is dirty
# ---------------------------------------------------------------------------


def test_probe_gate_skipped_when_any_prior_validation_gate_emits_critical():
    """Probe execution is bounded behind *every* prior validation gate,
    not just conformance. A critical from code_safety, spec_readiness, or
    any earlier emitter must short-circuit the probe to avoid sandbox
    cost on code we already know is rejected."""
    orch = StrategyLabOrchestrator()
    # spec_readiness emits a critical — code_safety/conformance happen to
    # be clean. Probe must not run.
    orch.spec_readiness_gate.validate = lambda spec, **kw: [_critical_result("spec_readiness")]
    orch.code_safety_checker.check = lambda code, spec: [_info_result("code_safety")]
    orch.code_conformance_gate.check = lambda code, spec: [_info_result("code_conformance")]

    probe_called = []
    orch.rule_probes_gate.check = lambda code, spec, **kw: (
        probe_called.append(True) or [_info_result("rule_probes")]
    )
    orch._refine_or_exhaust = lambda **kw: (kw["spec"], kw["code"], True)

    spec = _minimal_spec()
    orch._run_synthesis_loop(
        spec=spec,
        code=spec.strategy_code,
        config=_minimal_config(),
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *args, **kwargs: None,
    )
    assert probe_called == [], (
        "rule_probes_gate.check must not run when an earlier validation gate "
        "(spec_readiness, code_safety) emitted a critical"
    )


def test_probe_gate_skipped_when_code_safety_emits_critical():
    """Same invariant for code_safety: probe must not pay sandbox cost
    when code_safety has already produced a critical that drives the
    refinement loop."""
    orch = StrategyLabOrchestrator()
    orch.spec_readiness_gate.validate = lambda spec, **kw: [_info_result("spec_readiness")]
    orch.code_safety_checker.check = lambda code, spec: [_critical_result("code_safety")]
    orch.code_conformance_gate.check = lambda code, spec: [_info_result("code_conformance")]

    probe_called = []
    orch.rule_probes_gate.check = lambda code, spec, **kw: (
        probe_called.append(True) or [_info_result("rule_probes")]
    )
    orch._refine_or_exhaust = lambda **kw: (kw["spec"], kw["code"], True)

    spec = _minimal_spec()
    orch._run_synthesis_loop(
        spec=spec,
        code=spec.strategy_code,
        config=_minimal_config(),
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *args, **kwargs: None,
    )
    assert probe_called == []


def test_probe_gate_skipped_when_conformance_emits_critical():
    orch = StrategyLabOrchestrator()
    orch.spec_readiness_gate.validate = lambda spec, **kw: [_info_result("spec_readiness")]
    orch.code_safety_checker.check = lambda code, spec: [_info_result("code_safety")]
    # Conformance returns a critical — probe gate must NOT be invoked.
    orch.code_conformance_gate.check = lambda code, spec: [_critical_result("code_conformance")]

    probe_called = []

    def fake_probe_check(code, spec, *, phase="synthesis"):
        probe_called.append(True)
        return [_info_result("rule_probes")]

    orch.rule_probes_gate.check = fake_probe_check
    orch._refine_or_exhaust = lambda **kw: (kw["spec"], kw["code"], True)

    spec = _minimal_spec()
    orch._run_synthesis_loop(
        spec=spec,
        code=spec.strategy_code,
        config=_minimal_config(),
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *args, **kwargs: None,
    )

    assert probe_called == [], (
        "rule_probes_gate.check must not run when CodeConformanceGate emits a critical"
    )


# ---------------------------------------------------------------------------
# Wiring test 3: rule_id flows through to failure_details verbatim
# ---------------------------------------------------------------------------


def test_failure_details_format_includes_rule_id():
    orch = StrategyLabOrchestrator()
    _stub_clean_gates(orch)
    orch.rule_probes_gate.check = lambda code, spec, **kw: [
        _critical_result("rule_probes", rule_id="exit[2]:stop_loss"),
        _critical_result("rule_probes", rule_id="entry[0]"),
    ]

    captured: List[dict] = []
    orch._refine_or_exhaust = lambda **kw: (
        captured.append({"details": kw["failure_details"]}) or (kw["spec"], kw["code"], True)
    )

    spec = _minimal_spec()
    orch._run_synthesis_loop(
        spec=spec,
        code=spec.strategy_code,
        config=_minimal_config(),
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *args, **kwargs: None,
    )

    assert captured, "_refine_or_exhaust must be invoked"
    details = captured[0]["details"]
    # Both rule_ids appear in their bracketed prefix form.
    assert "[rule_probes:exit[2]:stop_loss]" in details
    assert "[rule_probes:entry[0]]" in details


# ---------------------------------------------------------------------------
# Wiring test 4: record_gates captures probe entries (with rule_id) in
# the orchestrator's running gate-results list
# ---------------------------------------------------------------------------


def test_record_gates_captures_probe_results_with_rule_id():
    orch = StrategyLabOrchestrator()
    _stub_clean_gates(orch)
    orch.rule_probes_gate.check = lambda code, spec, **kw: [
        _critical_result("rule_probes", rule_id="entry[0]"),
    ]
    orch._refine_or_exhaust = lambda **kw: (kw["spec"], kw["code"], True)

    all_results: List[QualityGateResult] = []
    spec = _minimal_spec()
    orch._run_synthesis_loop(
        spec=spec,
        code=spec.strategy_code,
        config=_minimal_config(),
        all_gate_results=all_results,
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *args, **kwargs: None,
    )

    probe_results = [r for r in all_results if r.gate_name == "rule_probes"]
    assert probe_results, "rule_probes results must be recorded in all_gate_results"
    assert probe_results[0].rule_id == "entry[0]"


# ---------------------------------------------------------------------------
# ran_on_non_conforming_code is captured at trade-collection time, attributed
# to the round whose backtest is persisted (not any historical demoted round)
# ---------------------------------------------------------------------------


def _demoted_conformance(code, spec, **kw):
    return [
        QualityGateResult(
            gate_name="predicate_conformance",
            passed=False,
            severity="warning",
            phase="synthesis",
            details="rule_id=entry[0]: predicate conformance failed.",
        )
    ]


def _stub_execute_path(orch: StrategyLabOrchestrator, *, exec_results) -> None:
    """Stub fetch → execute → coverage → anomaly so the loop reaches (or passes)
    trade collection without a sandbox or market-data service."""
    from investment_team.market_data_service import OHLCVBar
    from investment_team.strategy_lab import orchestrator as orch_mod
    from investment_team.strategy_lab._orchestrator_helpers import _MarketDataFetch

    bars = [
        OHLCVBar(
            date=f"2023-01-{i + 1:02d}", open=100.0, high=101.0, low=99.0, close=100.0, volume=1
        )
        for i in range(20)
    ]
    orch._fetch_market_data = lambda spec, config: _MarketDataFetch(
        data={"PROBE": bars}, requested_symbols=["PROBE"], fetched_symbols=["PROBE"]
    )
    orch.target_symbol_coverage_gate.check_fetch = lambda *a, **k: [_info_result("coverage_fetch")]
    orch.target_symbol_coverage_gate.check_trades = lambda *a, **k: [
        _info_result("coverage_trades")
    ]
    orch.anomaly_detector.check = lambda *a, **k: [_info_result("anomaly")]
    # Bypass the real coverage-report attachment (needs live market internals).
    orch_mod._maybe_attach_coverage_report = lambda **kw: None

    results = list(exec_results)
    orch._cached_run_strategy_code = lambda code, md, config, *, strategy=None: results.pop(0)


def test_demoted_conformance_with_execution_flags_outcome(monkeypatch):
    """A demoted-conformance round that executes and collects trades flags the
    outcome non-conforming."""
    from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

    orch = StrategyLabOrchestrator()
    _stub_clean_gates(orch)
    orch.rule_probes_gate.check = lambda code, spec, **kw: [_info_result("rule_probes")]
    orch.predicate_conformance_gate.check = _demoted_conformance
    _stub_execute_path(orch, exec_results=[StrategyRunResult(success=True, trades=[])])

    outcome = orch._run_synthesis_loop(
        spec=_minimal_spec(),
        code=_minimal_spec().strategy_code,
        config=_minimal_config(),
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *a, **k: None,
    )

    assert outcome.execution_succeeded is True
    assert outcome.ran_on_non_conforming_code is True


def test_demoted_conformance_without_execution_does_not_flag(monkeypatch):
    """The fix's core: a demoted-conformance warning alone does NOT flag the
    record — the flag is captured only when a round actually collects trades.
    Here execution fails before trade collection, so the persisted (empty)
    backtest never ran on the demoted code."""
    from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

    orch = StrategyLabOrchestrator()
    _stub_clean_gates(orch)
    orch.rule_probes_gate.check = lambda code, spec, **kw: [_info_result("rule_probes")]
    orch.predicate_conformance_gate.check = _demoted_conformance
    _stub_execute_path(
        orch,
        exec_results=[StrategyRunResult(success=False, error_type="runtime", stderr="boom")],
    )
    # Execution failure routes to refinement; exhaust immediately to end the loop.
    orch._refine_or_exhaust = lambda **kw: (kw["spec"], kw["code"], True)

    outcome = orch._run_synthesis_loop(
        spec=_minimal_spec(),
        code=_minimal_spec().strategy_code,
        config=_minimal_config(),
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *a, **k: None,
    )

    assert outcome.execution_succeeded is False
    assert outcome.ran_on_non_conforming_code is False

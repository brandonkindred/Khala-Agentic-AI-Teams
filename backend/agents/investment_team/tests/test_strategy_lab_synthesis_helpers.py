"""Direct unit coverage for the synthesis-loop helpers extracted out of
:meth:`StrategyLabOrchestrator._run_synthesis_loop`.

The per-round body was decomposed into named helpers:

- :meth:`_run_synthesis_universe_injection` — the deterministic UNIVERSE +
  on_bar symbol-guard injection that runs before any gate sees the code.
- :meth:`_run_synthesis_validation_gates` — the round's validation gates,
  including the predicate-conformance gate that only runs when no earlier gate
  fired a critical (the ordering the refactor must preserve exactly).
- :meth:`_fetch_market_data_for_synthesis` — the one-time fetch + coverage,
  returning a ``should_break`` signal.
- :meth:`_run_synthesis_reachability_probe` — the per-round predicate
  reachability probe, re-run only when the entry-rule signature changes.
- :meth:`_run_synthesis_execution` — runs the round's code and records a
  failure gate on error.
- :meth:`_run_synthesis_trade_collection` — collects trades and checks
  target-symbol coverage, returning a ``should_break`` signal.
- :meth:`_evaluate_synthesis_round` — metrics + anomaly gates + recovery
  routing, dispatching to one of three ``action`` outcomes
  (``"success"`` / ``"continue"`` / ``"exhausted"``).

These tests stub the orchestrator's gate collaborators so each helper's
contract is exercised in isolation.
"""

from __future__ import annotations

import textwrap
from typing import Dict, List

from investment_team.models import BacktestConfig, StrategySpec, TradeRecord
from investment_team.strategy_lab._orchestrator_helpers import _DesignAttemptState
from investment_team.strategy_lab.orchestrator import (
    RefinementStallTracker,
    StrategyLabOrchestrator,
    _AnomalyRecoveryOutcome,
    _DriftCollector,
    _MarketDataFetch,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, SignalExitRule
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="synth-helper-test",
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
        strategy_code="from contract import Strategy\n",
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2020-01-01",
        end_date="2025-01-01",
        initial_capital=100_000.0,
    )


def _gate(name: str, *, passed: bool, severity: str = "critical") -> QualityGateResult:
    return QualityGateResult(
        gate_name=name,
        passed=passed,
        severity=severity,
        phase="synthesis",
        details="x",
    )


def _orch() -> StrategyLabOrchestrator:
    return StrategyLabOrchestrator()


# Strategy class targeting QQQ (matching ``_spec()``'s target_symbols) with
# neither the UNIVERSE constant nor the on_bar symbol guard.
_GUARDLESS_CODE = textwrap.dedent(
    """
    from contract import Strategy

    class S(Strategy):
        def on_bar(self, ctx, bar):
            if sma(ctx.history(bar.symbol, 50), 50) > 0:
                ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")
    """
)

# Already-canonical form of the same strategy — inject_universe_and_guard
# returns this verbatim (no-op).
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
)


# ---------------------------------------------------------------------------
# _run_synthesis_universe_injection
# ---------------------------------------------------------------------------


def test_universe_injection_rewrites_code_updates_spec_and_records_drift() -> None:
    """Non-conformant code is rewritten, ``spec.strategy_code`` is kept in
    lockstep, and the change is recorded on the drift collector."""
    orch = _orch()
    spec = _spec()
    spec.strategy_code = _GUARDLESS_CODE
    collector = _DriftCollector()

    result = orch._run_synthesis_universe_injection(
        spec=spec, code=_GUARDLESS_CODE, drift_collector=collector
    )

    assert result != _GUARDLESS_CODE
    assert "UNIVERSE" in result
    assert spec.strategy_code == result
    assert len(collector.code_history) == 1
    revision = collector.code_history[0]
    assert revision.phase == "synthesis"
    assert revision.agent == "universe_injector"


def test_universe_injection_is_noop_on_already_conformant_code() -> None:
    """Already-canonical code is returned verbatim; ``spec.strategy_code`` is
    left untouched and nothing is recorded on the drift collector."""
    orch = _orch()
    spec = _spec()
    spec.strategy_code = "sentinel — must not be overwritten"
    collector = _DriftCollector()

    result = orch._run_synthesis_universe_injection(
        spec=spec, code=_CONFORMANT_CODE, drift_collector=collector
    )

    assert result == _CONFORMANT_CODE
    assert spec.strategy_code == "sentinel — must not be overwritten"
    assert collector.code_history == []


def test_universe_injection_without_drift_collector() -> None:
    """A ``None`` drift collector does not crash the injection path."""
    orch = _orch()
    spec = _spec()

    result = orch._run_synthesis_universe_injection(
        spec=spec, code=_GUARDLESS_CODE, drift_collector=None
    )

    assert result != _GUARDLESS_CODE
    assert spec.strategy_code == result


# ---------------------------------------------------------------------------
# _run_synthesis_validation_gates
# ---------------------------------------------------------------------------


def test_validation_gates_runs_predicate_conformance_when_prior_clean(monkeypatch) -> None:
    """All prior gates clean → predicate conformance runs and its results are
    folded into the round's gates and recorded."""
    orch = _orch()
    pred_calls: List[int] = []
    monkeypatch.setattr(orch.code_safety_checker, "check", lambda code, spec: [])
    monkeypatch.setattr(orch.code_conformance_gate, "check", lambda code, spec: [])

    def _pred(code, spec, attempt):
        pred_calls.append(attempt)
        return [_gate("predicate_conformance", passed=True, severity="info")]

    monkeypatch.setattr(orch.predicate_conformance_gate, "check", _pred)
    all_gate_results: List[QualityGateResult] = []

    gates, attempts = orch._run_synthesis_validation_gates(
        spec=_spec(),
        code="code",
        config=_config(),
        round_num=1,  # round != 0 → spec readiness skipped
        predicate_conformance_attempts=0,
        all_gate_results=all_gate_results,
        emit=lambda *a, **k: None,
    )

    assert pred_calls == [0], "predicate conformance must run exactly once"
    assert any(g.gate_name == "predicate_conformance" for g in gates)
    assert len(all_gate_results) == len(gates) and all_gate_results
    assert attempts == 0, "a passing predicate gate does not bump the attempt counter"


def test_validation_gates_skips_predicate_conformance_after_critical(monkeypatch) -> None:
    """A critical from an earlier gate suppresses the predicate-conformance
    check — the ordering the refactor must preserve (issue note)."""
    orch = _orch()
    pred_calls: List[int] = []
    monkeypatch.setattr(
        orch.code_safety_checker,
        "check",
        lambda code, spec: [_gate("code_safety", passed=False, severity="critical")],
    )
    monkeypatch.setattr(orch.code_conformance_gate, "check", lambda code, spec: [])
    monkeypatch.setattr(
        orch.predicate_conformance_gate,
        "check",
        lambda code, spec, attempt: pred_calls.append(attempt) or [],
    )
    all_gate_results: List[QualityGateResult] = []

    gates, attempts = orch._run_synthesis_validation_gates(
        spec=_spec(),
        code="code",
        config=_config(),
        round_num=1,
        predicate_conformance_attempts=2,
        all_gate_results=all_gate_results,
        emit=lambda *a, **k: None,
    )

    assert pred_calls == [], "predicate conformance must be skipped after a critical"
    assert attempts == 2, "attempt counter is untouched when the gate is skipped"
    assert any(not g.passed and g.severity == "critical" for g in gates)


def test_validation_gates_bumps_attempts_on_predicate_critical(monkeypatch) -> None:
    """A critical predicate-conformance finding increments the attempt counter
    so the retry budget advances."""
    orch = _orch()
    monkeypatch.setattr(orch.code_safety_checker, "check", lambda code, spec: [])
    monkeypatch.setattr(orch.code_conformance_gate, "check", lambda code, spec: [])
    monkeypatch.setattr(
        orch.predicate_conformance_gate,
        "check",
        lambda code, spec, attempt: [
            _gate("predicate_conformance", passed=False, severity="critical")
        ],
    )

    _gates, attempts = orch._run_synthesis_validation_gates(
        spec=_spec(),
        code="code",
        config=_config(),
        round_num=1,
        predicate_conformance_attempts=0,
        all_gate_results=[],
        emit=lambda *a, **k: None,
    )

    assert attempts == 1


# ---------------------------------------------------------------------------
# _fetch_market_data_for_synthesis
# ---------------------------------------------------------------------------


def test_fetch_for_synthesis_breaks_on_empty_data(monkeypatch) -> None:
    """No data → should_break True and the ``market_data`` gate is recorded."""
    orch = _orch()
    monkeypatch.setattr(
        orch,
        "_fetch_market_data",
        lambda spec, config: _MarketDataFetch(
            data={}, requested_symbols=["QQQ"], fetched_symbols=[], provider_used={}
        ),
    )
    all_gate_results: List[QualityGateResult] = []

    result = orch._fetch_market_data_for_synthesis(
        spec=_spec(),
        config=_config(),
        round_num=0,
        all_gate_results=all_gate_results,
        emit=lambda *a, **k: None,
    )

    assert result.should_break is True
    assert result.requested_symbols == ["QQQ"]
    assert any(g.gate_name == "market_data" for g in all_gate_results)


def test_fetch_for_synthesis_proceeds_when_coverage_clean(monkeypatch) -> None:
    """Data present + clean coverage → should_break False, data carried back."""
    orch = _orch()
    data = {"QQQ": []}
    monkeypatch.setattr(
        orch,
        "_fetch_market_data",
        lambda spec, config: _MarketDataFetch(
            data=data,
            requested_symbols=["QQQ"],
            fetched_symbols=["QQQ"],
            provider_used={"QQQ": "stub"},
        ),
    )
    monkeypatch.setattr(orch.target_symbol_coverage_gate, "check_fetch", lambda *a, **k: [])

    result = orch._fetch_market_data_for_synthesis(
        spec=_spec(),
        config=_config(),
        round_num=0,
        all_gate_results=[],
        emit=lambda *a, **k: None,
    )

    assert result.should_break is False
    assert result.data is data
    assert result.provider_used == {"QQQ": "stub"}


def test_fetch_for_synthesis_breaks_on_critical_coverage(monkeypatch) -> None:
    """Data present but a critical fetch-coverage failure → should_break True."""
    orch = _orch()
    monkeypatch.setattr(
        orch,
        "_fetch_market_data",
        lambda spec, config: _MarketDataFetch(
            data={"QQQ": []},
            requested_symbols=["QQQ", "SPY"],
            fetched_symbols=["QQQ"],
            provider_used={},
        ),
    )
    monkeypatch.setattr(
        orch.target_symbol_coverage_gate,
        "check_fetch",
        lambda *a, **k: [_gate("target_symbol_coverage", passed=False, severity="critical")],
    )
    all_gate_results: List[QualityGateResult] = []

    result = orch._fetch_market_data_for_synthesis(
        spec=_spec(),
        config=_config(),
        round_num=0,
        all_gate_results=all_gate_results,
        emit=lambda *a, **k: None,
    )

    assert result.should_break is True
    assert any(g.gate_name == "target_symbol_coverage" for g in all_gate_results)


# ---------------------------------------------------------------------------
# _run_synthesis_reachability_probe
# ---------------------------------------------------------------------------


def test_reachability_probe_noop_without_market_data(monkeypatch) -> None:
    """No market data yet → returns the input signature unchanged and the
    probe collaborator is never called."""
    orch = _orch()
    calls: List[str] = []
    monkeypatch.setattr(
        orch.predicate_reachability_probe, "probe", lambda *a, **k: calls.append("probe")
    )
    all_gate_results: List[QualityGateResult] = []

    result = orch._run_synthesis_reachability_probe(
        spec=_spec(),
        market_data=None,
        round_num=0,
        last_reachability_sig=None,
        all_gate_results=all_gate_results,
    )

    assert result is None
    assert calls == []
    assert all_gate_results == []


def test_reachability_probe_noop_when_signature_unchanged(monkeypatch) -> None:
    """Market data present but the entry-rule signature matches the prior
    round's → the probe is not re-run and no gates are recorded."""
    orch = _orch()
    calls: List[str] = []
    monkeypatch.setattr(
        orch.predicate_reachability_probe, "probe", lambda *a, **k: calls.append("probe")
    )
    spec = _spec()
    prior_sig = (
        tuple(str(getattr(r, "when", r)) for r in (spec.entry_rules or [])),
        bool(spec.requires_custom_code),
    )
    all_gate_results: List[QualityGateResult] = []

    result = orch._run_synthesis_reachability_probe(
        spec=spec,
        market_data={"QQQ": []},
        round_num=0,
        last_reachability_sig=prior_sig,
        all_gate_results=all_gate_results,
    )

    assert result == prior_sig
    assert calls == []
    assert all_gate_results == []


def test_reachability_probe_runs_on_changed_signature(monkeypatch) -> None:
    """A changed entry-rule signature (or first round, ``None`` prior) runs
    the probe and records its gate results."""
    orch = _orch()
    probe_calls: List[tuple] = []
    monkeypatch.setattr(
        orch.predicate_reachability_probe,
        "probe",
        lambda spec, market_data: probe_calls.append((spec, market_data)) or "reachability",
    )
    monkeypatch.setattr(
        orch.predicate_reachability_probe,
        "to_gate_results",
        lambda reachability, spec, phase: [
            _gate("predicate_reachability", passed=True, severity="info")
        ],
    )
    spec = _spec()
    all_gate_results: List[QualityGateResult] = []

    result = orch._run_synthesis_reachability_probe(
        spec=spec,
        market_data={"QQQ": []},
        round_num=0,
        last_reachability_sig=None,
        all_gate_results=all_gate_results,
    )

    expected_sig = (
        tuple(str(getattr(r, "when", r)) for r in (spec.entry_rules or [])),
        bool(spec.requires_custom_code),
    )
    assert result == expected_sig
    assert len(probe_calls) == 1
    assert any(g.gate_name == "predicate_reachability" for g in all_gate_results)


def test_reachability_probe_also_records_starvation_findings_on_changed_signature(
    monkeypatch,
) -> None:
    """A changed signature also runs the union-based starvation check and
    records its structurally-starved findings alongside the dead-rule ones —
    the new finding kind must be visible in the same run's gate results, not
    require a separate wiring step."""
    orch = _orch()
    monkeypatch.setattr(
        orch.predicate_reachability_probe, "probe", lambda spec, market_data: "reachability"
    )
    monkeypatch.setattr(
        orch.predicate_reachability_probe,
        "to_gate_results",
        lambda reachability, spec, phase: [
            _gate("predicate_reachability", passed=True, severity="info")
        ],
    )
    verdict_calls: List[tuple] = []
    monkeypatch.setattr(
        orch.predicate_reachability_probe,
        "probe_starvation",
        lambda spec, market_data: verdict_calls.append((spec, market_data)) or "verdicts",
    )
    starvation_calls: List[tuple] = []
    monkeypatch.setattr(
        orch.predicate_reachability_probe,
        "to_starvation_gate_results",
        lambda verdicts, spec, phase: (
            starvation_calls.append((verdicts, spec, phase))
            or [_gate("predicate_reachability_probe", passed=False, severity="critical")]
        ),
    )
    spec = _spec()
    all_gate_results: List[QualityGateResult] = []

    orch._run_synthesis_reachability_probe(
        spec=spec,
        market_data={"QQQ": []},
        round_num=0,
        last_reachability_sig=None,
        all_gate_results=all_gate_results,
    )

    assert len(verdict_calls) == 1
    assert verdict_calls[0] == (spec, {"QQQ": []})
    assert len(starvation_calls) == 1
    assert starvation_calls[0][0] == "verdicts"
    assert starvation_calls[0][2] == "synthesis"
    starved = [g for g in all_gate_results if g.gate_name == "predicate_reachability_probe"]
    assert starved and starved[0].severity == "critical"
    # Still recorded alongside the (distinct) dead-rule gate name from this round.
    assert any(g.gate_name == "predicate_reachability" for g in all_gate_results)


def test_reachability_probe_noop_cases_never_call_pairwise_collaborators(monkeypatch) -> None:
    """The two existing no-op guards (no market data yet / unchanged
    signature) must short-circuit before EITHER collaborator pair runs —
    dead-rule and starvation reporting share one signature-gated re-probe."""
    orch = _orch()
    for name in ("probe", "probe_starvation"):

        def _forbidden(*_a, _name=name, **_k):
            raise AssertionError(f"{_name} must not be called")

        monkeypatch.setattr(orch.predicate_reachability_probe, name, _forbidden)

    orch._run_synthesis_reachability_probe(
        spec=_spec(),
        market_data=None,
        round_num=0,
        last_reachability_sig=None,
        all_gate_results=[],
    )

    spec = _spec()
    prior_sig = (
        tuple(str(getattr(r, "when", r)) for r in (spec.entry_rules or [])),
        bool(spec.requires_custom_code),
    )
    orch._run_synthesis_reachability_probe(
        spec=spec,
        market_data={"QQQ": []},
        round_num=0,
        last_reachability_sig=prior_sig,
        all_gate_results=[],
    )


# ---------------------------------------------------------------------------
# _run_synthesis_execution
# ---------------------------------------------------------------------------


def test_execution_returns_result_without_gate_on_success(monkeypatch) -> None:
    """A successful run returns the ``StrategyRunResult`` and appends no gate."""
    orch = _orch()
    exec_result = StrategyRunResult(success=True, trades=[])
    monkeypatch.setattr(orch, "_cached_run_strategy_code", lambda *a, **k: exec_result)
    all_gate_results: List[QualityGateResult] = []

    result = orch._run_synthesis_execution(
        spec=_spec(),
        code="code",
        market_data={"QQQ": []},
        config=_config(),
        round_num=0,
        all_gate_results=all_gate_results,
        emit=lambda *a, **k: None,
    )

    assert result is exec_result
    assert all_gate_results == []


def test_execution_appends_gate_on_failure(monkeypatch) -> None:
    """A failed run's error type/stderr are folded into the recorded gate."""
    orch = _orch()
    exec_result = StrategyRunResult(success=False, error_type="runtime_error", stderr="boom")
    monkeypatch.setattr(orch, "_cached_run_strategy_code", lambda *a, **k: exec_result)
    all_gate_results: List[QualityGateResult] = []

    result = orch._run_synthesis_execution(
        spec=_spec(),
        code="code",
        market_data={"QQQ": []},
        config=_config(),
        round_num=2,
        all_gate_results=all_gate_results,
        emit=lambda *a, **k: None,
    )

    assert result is exec_result
    assert len(all_gate_results) == 1
    gate = all_gate_results[0]
    assert gate.gate_name == "code_execution"
    assert gate.severity == "critical"
    assert "runtime_error" in gate.details and "boom" in gate.details
    assert gate.refinement_round == 2


# ---------------------------------------------------------------------------
# _run_synthesis_trade_collection
# ---------------------------------------------------------------------------


def test_trade_collection_completes_on_clean_coverage(monkeypatch) -> None:
    """Clean coverage → should_break False, trades/flags carried through,
    and the "completed" backtesting event is emitted."""
    orch = _orch()
    monkeypatch.setattr(orch.target_symbol_coverage_gate, "check_trades", lambda spec, trades: [])
    trade = TradeRecord(
        trade_num=1,
        entry_date="2024-01-01",
        exit_date="2024-01-02",
        symbol="QQQ",
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
        cumulative_pnl=10.0,
    )
    exec_result = StrategyRunResult(
        success=True,
        trades=[trade],
        execution_time_seconds=1.5,
        open_position_entry_reasons=["reason"],
    )
    round_gate_results = [_gate("predicate_conformance", passed=False, severity="warning")]
    events: List[Dict[str, object]] = []
    all_gate_results: List[QualityGateResult] = []

    result = orch._run_synthesis_trade_collection(
        spec=_spec(),
        exec_result=exec_result,
        round_gate_results=round_gate_results,
        round_num=0,
        all_gate_results=all_gate_results,
        emit=lambda phase, data: events.append({"phase": phase, **data}),
    )

    assert result.should_break is False
    assert result.trades == [trade]
    assert result.ran_on_non_conforming_code is True
    assert result.open_position_entry_reasons == ["reason"]
    assert events == [
        {
            "phase": "backtesting",
            "sub_phase": "completed",
            "trades_count": 1,
            "execution_time": 1.5,
        }
    ]


def test_trade_collection_breaks_on_critical_coverage_without_completed_event(
    monkeypatch,
) -> None:
    """A critical coverage failure → should_break True and no "completed"
    event is emitted (mirroring the pre-extraction code's break-before-emit
    ordering)."""
    orch = _orch()
    monkeypatch.setattr(
        orch.target_symbol_coverage_gate,
        "check_trades",
        lambda spec, trades: [_gate("target_symbol_coverage", passed=False, severity="critical")],
    )
    exec_result = StrategyRunResult(success=True, trades=[])
    events: List[Dict[str, object]] = []
    all_gate_results: List[QualityGateResult] = []

    result = orch._run_synthesis_trade_collection(
        spec=_spec(),
        exec_result=exec_result,
        round_gate_results=[],
        round_num=0,
        all_gate_results=all_gate_results,
        emit=lambda phase, data: events.append({"phase": phase, **data}),
    )

    assert result.should_break is True
    assert events == []
    assert any(g.gate_name == "target_symbol_coverage" for g in all_gate_results)


# ---------------------------------------------------------------------------
# _evaluate_synthesis_round
# ---------------------------------------------------------------------------


def test_evaluate_synthesis_round_success_when_no_critical_anomaly(monkeypatch) -> None:
    """A clean anomaly check → ``action="success"``, the round's spec/code/
    trades pass through unchanged, and the anomaly gate is recorded."""
    orch = _orch()
    monkeypatch.setattr(
        orch,
        "_check_anomalies_cached",
        lambda metrics, trades, **kw: [_gate("backtest_anomaly", passed=True, severity="info")],
    )
    spec = _spec()
    trades: List[TradeRecord] = []
    exec_result = StrategyRunResult(success=True, trades=trades)
    all_gate_results: List[QualityGateResult] = []

    result = orch._evaluate_synthesis_round(
        state=_DesignAttemptState(spec=spec, code="code-v0", trades=trades, metrics=None),
        exec_result=exec_result,
        market_data={"QQQ": []},
        config=_config(),
        round_num=0,
        ran_on_non_conforming_code=True,
        all_gate_results=all_gate_results,
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *a, **k: None,
        stall_tracker=RefinementStallTracker(),
        drift_collector=None,
    )

    assert result.action == "success"
    assert result.spec is spec
    assert result.code == "code-v0"
    assert result.trades == trades
    assert result.exec_result is exec_result
    assert result.ran_on_non_conforming_code is True, (
        "the trade-collection verdict passed in must survive untouched on the success path"
    )
    assert result.runtime_lookahead_violation is False
    assert result.stalled is False
    assert any(g.gate_name == "backtest_anomaly" for g in all_gate_results)


def test_evaluate_synthesis_round_continue_on_recovered_anomaly(monkeypatch) -> None:
    """A critical anomaly whose recovery does not exhaust the round budget →
    ``action="continue"`` carrying the recovered state; an unset
    (``None``) recovery verdict leaves the caller's ``ran_on_non_conforming_code``
    untouched."""
    orch = _orch()
    critical_gate = _gate("backtest_anomaly", passed=False, severity="critical")
    monkeypatch.setattr(
        orch, "_check_anomalies_cached", lambda metrics, trades, **kw: [critical_gate]
    )

    recovered_spec = _spec()
    recovered_trades: List[TradeRecord] = []
    recovered_metrics = object()
    recovered_exec_result = StrategyRunResult(
        success=True, trades=recovered_trades, error_type="lookahead_violation"
    )
    recovery_calls: List[Dict[str, object]] = []

    def _fake_handle_critical_anomalies(**kwargs):
        recovery_calls.append(kwargs)
        return _AnomalyRecoveryOutcome(
            spec=recovered_spec,
            code="code-refined",
            trades=recovered_trades,
            metrics=recovered_metrics,
            exec_result=recovered_exec_result,
            exhausted=False,
            ran_on_non_conforming_code=None,
            stalled=False,
        )

    monkeypatch.setattr(orch, "_handle_critical_anomalies", _fake_handle_critical_anomalies)

    result = orch._evaluate_synthesis_round(
        state=_DesignAttemptState(spec=_spec(), code="code-v0", trades=[], metrics=None),
        exec_result=StrategyRunResult(success=True, trades=[]),
        market_data={"QQQ": []},
        config=_config(),
        round_num=1,
        ran_on_non_conforming_code=False,
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *a, **k: None,
        stall_tracker=RefinementStallTracker(),
        drift_collector=None,
    )

    assert len(recovery_calls) == 1
    assert recovery_calls[0]["critical_anomalies"] == [critical_gate]
    assert result.action == "continue"
    assert result.spec is recovered_spec
    assert result.code == "code-refined"
    assert result.trades is recovered_trades
    assert result.metrics is recovered_metrics
    assert result.exec_result is recovered_exec_result
    assert result.ran_on_non_conforming_code is False, (
        "a None recovery verdict must not overwrite the caller's flag"
    )
    assert result.runtime_lookahead_violation is True, (
        "must derive from the RECOVERED exec_result, not the original"
    )


def test_evaluate_synthesis_round_exhausted_propagates_stall_and_verdict(monkeypatch) -> None:
    """A critical anomaly whose recovery exhausts the round budget →
    ``action="exhausted"``, ``stalled`` threaded from the recovery outcome,
    and a non-``None`` recovery verdict does overwrite the caller's flag."""
    orch = _orch()
    monkeypatch.setattr(
        orch,
        "_check_anomalies_cached",
        lambda metrics, trades, **kw: [
            _gate("backtest_anomaly", passed=False, severity="critical")
        ],
    )
    monkeypatch.setattr(
        orch,
        "_handle_critical_anomalies",
        lambda **kw: _AnomalyRecoveryOutcome(
            spec=_spec(),
            code="code-v0",
            trades=[],
            metrics=object(),
            exec_result=StrategyRunResult(success=False, trades=[]),
            exhausted=True,
            ran_on_non_conforming_code=True,
            stalled=True,
        ),
    )

    result = orch._evaluate_synthesis_round(
        state=_DesignAttemptState(spec=_spec(), code="code-v0", trades=[], metrics=None),
        exec_result=StrategyRunResult(success=True, trades=[]),
        market_data={"QQQ": []},
        config=_config(),
        round_num=2,
        ran_on_non_conforming_code=False,
        all_gate_results=[],
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=lambda *a, **k: None,
        stall_tracker=RefinementStallTracker(),
        drift_collector=None,
    )

    assert result.action == "exhausted"
    assert result.stalled is True
    assert result.ran_on_non_conforming_code is True, (
        "a non-None recovery verdict must overwrite the caller's flag"
    )

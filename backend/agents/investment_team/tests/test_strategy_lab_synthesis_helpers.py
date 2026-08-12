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
- :meth:`_evaluate_synthesis_round` — metrics + anomaly gates + recovery
  routing (covered end-to-end by the ``_run_synthesis_loop`` suites; not
  re-pinned here).

These tests stub the orchestrator's gate collaborators so each helper's
contract is exercised in isolation.
"""

from __future__ import annotations

import textwrap
from typing import List

from investment_team.models import BacktestConfig, StrategySpec
from investment_team.strategy_lab.orchestrator import (
    StrategyLabOrchestrator,
    _DriftCollector,
    _MarketDataFetch,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate, SignalExitRule


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

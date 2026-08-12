"""Tests for the anomaly-check signature guard (``_check_anomalies_cached``).

Mirrors the pre-backtest reachability probe's signature guard
(``reachability_sig`` in ``_run_synthesis_loop``), but scoped to the whole
design attempt rather than one loop: the synthesis loop and the
trade-alignment loop both evaluate the same ``anomaly_detector.check(...)``
gate, and when the ``(metrics, trades)`` fed to it are unchanged, the
detector must not run — and must not re-record — a second time.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from investment_team.strategy_lab._orchestrator_helpers import _DesignAttemptState
from investment_team.strategy_lab.orchestrator import (
    RefinementStallTracker,
    StrategyLabOrchestrator,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.tests.test_strategy_lab_alignment import (
    _config,
    _market_data,
    _spec,
    _trade_records,
)
from investment_team.trade_simulator import compute_metrics
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult


def _counting_clean_check() -> Tuple[Dict[str, int], Any]:
    """A stand-in ``anomaly_detector.check`` that always passes and counts calls."""
    calls = {"n": 0}

    def _check(*_a: Any, **_kw: Any) -> List[QualityGateResult]:
        calls["n"] += 1
        return [
            QualityGateResult(
                gate_name="backtest_anomaly",
                passed=True,
                severity="info",
                phase="synthesis",
                details="Backtest results passed all anomaly checks.",
            )
        ]

    return calls, _check


def _collect_emit() -> Tuple[List[Tuple[str, Dict[str, Any]]], Any]:
    events: List[Tuple[str, Dict[str, Any]]] = []

    def _emit(phase: str, data: Dict[str, Any]) -> None:
        events.append((phase, data))

    return events, _emit


def test_check_anomalies_cached_skips_repeat_call_for_unchanged_metrics_and_trades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two calls with byte-identical ``(metrics, trades)`` content invoke
    ``anomaly_detector.check`` once; the second call's gates are copies
    reused from the first, restamped to the caller's ``phase``."""
    orch = StrategyLabOrchestrator()
    calls, check = _counting_clean_check()
    monkeypatch.setattr(orch.anomaly_detector, "check", check)

    trades = _trade_records()
    config = _config()
    metrics = compute_metrics(trades, config.initial_capital, config.start_date, config.end_date)

    gates_first = orch._check_anomalies_cached(
        metrics,
        trades,
        dsr_aware=False,
        diagnostics=None,
        coverage_report=None,
        market_data=_market_data(),
        phase="synthesis",
    )
    assert calls["n"] == 1

    # A structurally-identical (but distinct-object) trades list — the cache
    # keys on content, not identity.
    trades_again = _trade_records()
    metrics_again = compute_metrics(
        trades_again, config.initial_capital, config.start_date, config.end_date
    )
    gates_second = orch._check_anomalies_cached(
        metrics_again,
        trades_again,
        dsr_aware=False,
        diagnostics=None,
        coverage_report=None,
        market_data=_market_data(),
        phase="verification",
    )
    assert calls["n"] == 1, "unchanged (metrics, trades) must not re-invoke check()"
    assert gates_second[0].phase == "verification"
    assert gates_first[0].phase == "synthesis"
    # The two calls returned distinct objects — mutating one's gate_name
    # (as record_gates would) must not affect the other or the cache.
    assert gates_first[0] is not gates_second[0]
    gates_second[0].gate_name = "alignment_backtest_anomaly"
    assert gates_first[0].gate_name == "backtest_anomaly"

    # A genuinely different ledger invalidates the cache.
    trades_changed = _trade_records(n=8)
    metrics_changed = compute_metrics(
        trades_changed, config.initial_capital, config.start_date, config.end_date
    )
    orch._check_anomalies_cached(
        metrics_changed,
        trades_changed,
        dsr_aware=False,
        diagnostics=None,
        coverage_report=None,
        market_data=_market_data(),
        phase="synthesis",
    )
    assert calls["n"] == 2, "a changed ledger must re-invoke check()"


def test_anomaly_check_shared_across_synthesis_and_alignment_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``anomaly_detector.check`` is called exactly once across a synthesis
    round and a subsequent alignment-proposal evaluation when the re-executed
    ledger reproduces the same trades, and again when it changes — the
    acceptance criterion in the issue this guards against."""
    orch = StrategyLabOrchestrator()
    calls, check = _counting_clean_check()
    monkeypatch.setattr(orch.anomaly_detector, "check", check)

    spec = _spec()
    config = _config()
    market_data = _market_data()
    events, emit = _collect_emit()

    ledger = _trade_records()
    exec_result = StrategyRunResult(success=True, trades=ledger, stderr="", error_type=None)
    all_gate_results: List[QualityGateResult] = []
    metrics = compute_metrics(ledger, config.initial_capital, config.start_date, config.end_date)

    synthesis_result = orch._evaluate_synthesis_round(
        state=_DesignAttemptState(spec=spec, code="code-v0", trades=ledger, metrics=metrics),
        exec_result=exec_result,
        market_data=market_data,
        config=config,
        round_num=0,
        ran_on_non_conforming_code=False,
        all_gate_results=all_gate_results,
        refinement_attempts=[],
        zero_trade_attempts=[],
        emit=emit,
        stall_tracker=RefinementStallTracker(),
        drift_collector=None,
    )
    assert synthesis_result.action == "success"
    assert calls["n"] == 1

    # The alignment re-execution reproduces the identical ledger content
    # (a proposed fix that changes nothing observable) — the cache must
    # reuse the synthesis round's verdict rather than re-running the check.
    unchanged_align_exec = StrategyRunResult(
        success=True, trades=_trade_records(), stderr="", error_type=None
    )
    evaluated = orch._evaluate_alignment_proposal(
        proposed_spec=spec,
        align_exec=unchanged_align_exec,
        market_data=market_data,
        config=config,
        all_gate_results=all_gate_results,
        align_round=0,
        spec=spec,
        emit=emit,
    )
    assert evaluated is not None
    assert calls["n"] == 1, (
        "anomaly_detector.check must not run again when the alignment "
        "re-execution reproduces the synthesis round's ledger"
    )
    # Both rounds' gates were recorded, distinctly stamped.
    anomaly_entries = [g for g in all_gate_results if g.gate_name.endswith("backtest_anomaly")]
    assert len(anomaly_entries) == 2
    assert {g.refinement_round for g in anomaly_entries} == {0}
    assert any(g.gate_name.startswith("alignment_") for g in anomaly_entries)
    assert any(not g.gate_name.startswith("alignment_") for g in anomaly_entries)

    # A genuinely different fix (different trades) must re-run the check.
    changed_align_exec = StrategyRunResult(
        success=True, trades=_trade_records(n=8), stderr="", error_type=None
    )
    evaluated_changed = orch._evaluate_alignment_proposal(
        proposed_spec=spec,
        align_exec=changed_align_exec,
        market_data=market_data,
        config=config,
        all_gate_results=all_gate_results,
        align_round=1,
        spec=spec,
        emit=emit,
    )
    assert evaluated_changed is not None
    assert calls["n"] == 2, "a changed alignment ledger must re-invoke check()"

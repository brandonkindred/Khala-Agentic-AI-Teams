"""Unit tests for ``strategy_lab.temporal.workflows.StrategyLabCycleWorkflow``.

Mirrors the workflow-testing pattern already used in this codebase (e.g.
``agentic_team_provisioning/tests/test_temporal_activity.py``): monkeypatch
``temporalio.workflow.execute_activity`` to dispatch by activity-function
identity to canned responses, patch ``workflow.uuid4``/``workflow.now`` to
deterministic values, and drive the workflow's coroutines directly with
``asyncio.run`` — no live Temporal server or sandbox needed.

These tests isolate the workflow's own control flow (loop bounds, activity
call sequencing, short-circuit/exception handling) from its collaborators:
every activity call is mocked, and the plain deterministic gate classes the
workflow calls directly are also patched to canned verdicts, since their own
correctness is covered by their dedicated test suites elsewhere in this
codebase (``test_alignment_checks.py``, ``test_acceptance_gate.py``, etc.).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict
from unittest import mock

from investment_team.strategy_lab.temporal import workflows as wf

_FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _spec_dict(**overrides: Any) -> Dict[str, Any]:
    base = {
        "strategy_id": "strat-1",
        "authored_by": "DesignAgent",
        "asset_class": "stocks",
        "hypothesis": "h",
        "signal_definition": "s",
        "timeframe": "1d",
        "entry_rules": [],
        "exit_rules": [],
        "target_symbols": ["AAPL"],
    }
    base.update(overrides)
    return base


def _config_dict(**overrides: Any) -> Dict[str, Any]:
    base = {"start_date": "2023-01-01", "end_date": "2023-12-31"}
    base.update(overrides)
    return base


_WF_CONFIG = {
    "design_review_rounds": 20,
    "design_review_stall_rounds": 3,
    "mechanical_repair_enabled": False,
    "code_conformance_retries": 2,
    "design_max_llm_calls": 120,
    "regime_summary_enabled": False,
}


def _patch_execute(handlers: Dict[str, Any]):
    """Returns a context manager patching ``workflow.execute_activity``.

    ``handlers`` maps activity function *name* (not object identity, so
    callers don't need to import ``activities`` themselves) to a callable
    ``(args_or_kwargs) -> result`` or a plain return value.
    """

    async def _fake_exec(fn, *, args, **_kw):
        name = fn.__name__
        if name not in handlers:
            raise AssertionError(f"unexpected activity call: {name}")
        handler = handlers[name]
        if callable(handler):
            return handler(args)
        return handler

    return mock.patch("temporalio.workflow.execute_activity", _fake_exec)


def _patch_uuid():
    return mock.patch("temporalio.workflow.uuid4", lambda: _FIXED_UUID)


def _patch_now():
    from datetime import datetime, timezone

    return mock.patch("temporalio.workflow.now", lambda: datetime(2024, 1, 1, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# _run_design_loop
# ---------------------------------------------------------------------------


def test_design_loop_ready_on_first_round():
    from investment_team.strategy_lab.quality_gates.spec_readiness import SpecReadinessGate

    spec = _spec_dict()
    handlers = {
        "design_generate_activity": lambda a: {"strategy_dict": spec, "rationale": "why"},
        "resolve_symbols_activity": lambda a: ["AAPL"],
        "resolve_readiness_prices_activity": lambda a: {"AAPL": 150.0},
        "design_review_activity": lambda a: {
            "ready": True,
            "rationale": "good",
            "issues": [],
            "readiness_findings": [],
            "round": 0,
        },
    }
    cw = wf.StrategyLabCycleWorkflow()
    with (
        _patch_execute(handlers),
        _patch_uuid(),
        mock.patch.object(SpecReadinessGate, "validate", return_value=[]),
    ):
        result = asyncio.run(
            cw._run_design_loop(
                prior_records=[],
                signal_brief=None,
                directives=[],
                exclude_asset_classes=None,
                config_dict=_config_dict(),
                all_gate_results=[],
                drift=wf._DriftState(),
                regime_summary=None,
                wf_config=_WF_CONFIG,
                llm_calls_made_box=[0],
                llm_call_limit=120,
            )
        )
    assert result["ready"] is True
    assert result["rounds"] == 1
    assert result["spec"]["strategy_id"] == "strat-12345678"


def test_design_loop_stalls_when_open_issues_never_change():
    from investment_team.strategy_lab.quality_gates.spec_readiness import SpecReadinessGate

    spec = _spec_dict()
    critique = {
        "ready": False,
        "rationale": "not ready",
        "issues": [
            {"field": "hypothesis", "severity": "warning", "description": "d", "issue_id": "x"}
        ],
        "readiness_findings": [],
        "round": 0,
    }
    handlers = {
        "design_generate_activity": lambda a: {"strategy_dict": spec, "rationale": "why"},
        "resolve_symbols_activity": lambda a: ["AAPL"],
        "resolve_readiness_prices_activity": lambda a: {"AAPL": 150.0},
        "design_review_activity": lambda a: critique,
        "design_revise_activity": lambda a: {"strategy_dict": spec, "rationale": "revised"},
    }
    cw = wf.StrategyLabCycleWorkflow()
    wf_config = dict(_WF_CONFIG, design_review_stall_rounds=2)
    with (
        _patch_execute(handlers),
        _patch_uuid(),
        mock.patch.object(SpecReadinessGate, "validate", return_value=[]),
    ):
        result = asyncio.run(
            cw._run_design_loop(
                prior_records=[],
                signal_brief=None,
                directives=[],
                exclude_asset_classes=None,
                config_dict=_config_dict(),
                all_gate_results=[],
                drift=wf._DriftState(),
                regime_summary=None,
                wf_config=wf_config,
                llm_calls_made_box=[0],
                llm_call_limit=120,
            )
        )
    assert result["ready"] is False
    assert result["stop_reason"] == "stalled"


def test_design_loop_reports_budget_exhausted():
    handlers = {
        "design_generate_activity": lambda a: (_ for _ in ()).throw(
            RuntimeError("llm envelope exhausted")
        ),
    }
    cw = wf.StrategyLabCycleWorkflow()
    with _patch_execute(handlers), _patch_uuid():
        result = asyncio.run(
            cw._run_design_loop(
                prior_records=[],
                signal_brief=None,
                directives=[],
                exclude_asset_classes=None,
                config_dict=_config_dict(),
                all_gate_results=[],
                drift=wf._DriftState(),
                regime_summary=None,
                wf_config=_WF_CONFIG,
                llm_calls_made_box=[120],
                llm_call_limit=120,
            )
        )
    assert result["ready"] is False
    assert result["budget_exhausted"] is True
    assert result["stop_reason"] == "budget_exhausted"


# ---------------------------------------------------------------------------
# Top-level run() — SpecImplementabilityError retry loop
# ---------------------------------------------------------------------------


def test_run_retries_on_spec_implementability_error_then_short_circuits(monkeypatch):
    """The outer design-reentry loop retries MAX_DESIGN_REENTRIES+1 times, then
    builds a short-circuit record via build_short_circuit_record_activity."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError
    from investment_team.strategy_lab.temporal import workflows as wf_mod

    spec_obj = StrategySpec.parse_persisted(_spec_dict())
    call_count = {"n": 0}

    async def _fake_run_design_attempt(self, **kwargs):
        call_count["n"] += 1
        raise SpecImplementabilityError(
            "always fails",
            failure_phase="evaluation",
            last_spec=spec_obj,
            last_code="code",
        )

    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "build_short_circuit_record_activity": lambda a: {
            "record": {
                "lab_record_id": "sc-1",
                "strategy_lab_status": a[0]["short_circuit_status"],
            },
            "convergence_tracker_state": {"trial_count": a[0]["phase_back_count"]},
        },
    }
    monkeypatch.setattr(
        wf_mod.StrategyLabCycleWorkflow, "_run_design_attempt", _fake_run_design_attempt
    )

    cw = wf_mod.StrategyLabCycleWorkflow()
    with _patch_execute(handlers), _patch_uuid():
        result = asyncio.run(
            cw.run(
                {
                    "prior_records": [],
                    "config": _config_dict(),
                    "signal_brief": None,
                    "exclude_asset_classes": None,
                    "convergence_tracker_state": {},
                }
            )
        )

    # MAX_DESIGN_REENTRIES=2 -> 3 attempts total.
    assert call_count["n"] == wf_mod.MAX_DESIGN_REENTRIES + 1
    assert result["record"]["lab_record_id"] == "sc-1"


def test_run_returns_record_from_first_successful_attempt(monkeypatch):
    from investment_team.strategy_lab.temporal import workflows as wf_mod

    async def _fake_run_design_attempt(self, **kwargs):
        return {"lab_record_id": "rec-1"}, {"trial_count": 1}

    monkeypatch.setattr(
        wf_mod.StrategyLabCycleWorkflow, "_run_design_attempt", _fake_run_design_attempt
    )
    handlers = {"resolve_workflow_config_activity": lambda a: _WF_CONFIG}

    cw = wf_mod.StrategyLabCycleWorkflow()
    with _patch_execute(handlers), _patch_uuid():
        result = asyncio.run(
            cw.run(
                {
                    "prior_records": [],
                    "config": _config_dict(),
                    "signal_brief": None,
                    "exclude_asset_classes": None,
                    "convergence_tracker_state": {},
                }
            )
        )
    assert result["record"]["lab_record_id"] == "rec-1"
    assert result["convergence_tracker_state"]["trial_count"] == 1


def test_run_skips_config_resolution_when_already_supplied():
    """When cycle_input already carries workflow_config, no resolve activity fires."""
    from investment_team.strategy_lab.temporal import workflows as wf_mod

    async def _fake_run_design_attempt(self, **kwargs):
        assert kwargs["wf_config"] == _WF_CONFIG
        return {"lab_record_id": "rec-1"}, {}

    cw = wf_mod.StrategyLabCycleWorkflow()
    with (
        mock.patch.object(
            wf_mod.StrategyLabCycleWorkflow, "_run_design_attempt", _fake_run_design_attempt
        ),
        _patch_execute({}),
        _patch_uuid(),
    ):
        result = asyncio.run(
            cw.run(
                {
                    "prior_records": [],
                    "config": _config_dict(),
                    "convergence_tracker_state": {},
                    "workflow_config": _WF_CONFIG,
                }
            )
        )
    assert result["record"]["lab_record_id"] == "rec-1"


# ---------------------------------------------------------------------------
# Code synthesis phase
# ---------------------------------------------------------------------------


def test_synthesize_initial_code_uses_compiler_when_not_custom_code():
    spec = _spec_dict(requires_custom_code=False)
    cw = wf.StrategyLabCycleWorkflow()
    with (
        mock.patch.object(wf, "compile_strategy", return_value="compiled code"),
        _patch_execute({}),
        _patch_now(),
    ):
        result = asyncio.run(
            cw._synthesize_initial_code(
                spec_dict=spec,
                config_dict=_config_dict(),
                rationale="why",
                all_gate_results=[],
                design_attempt=0,
                phase_back_count=0,
                drift=wf._DriftState(),
                design_context={"rounds": 1, "critiques": [], "stop_reason": "ready"},
                tracker_state={},
            )
        )
    assert result["record"] is None
    assert result["code"] == "compiled code"


def test_synthesize_initial_code_falls_back_to_llm_on_compiler_error():
    from investment_team.strategy_lab.synthesis import CompilerError

    spec = _spec_dict(requires_custom_code=False)
    handlers = {"code_synthesis_activity": lambda a: {"code": "llm code"}}
    cw = wf.StrategyLabCycleWorkflow()
    with (
        mock.patch.object(wf, "compile_strategy", side_effect=CompilerError("nope")),
        _patch_execute(handlers),
        _patch_now(),
    ):
        result = asyncio.run(
            cw._synthesize_initial_code(
                spec_dict=spec,
                config_dict=_config_dict(),
                rationale="why",
                all_gate_results=[],
                design_attempt=0,
                phase_back_count=0,
                drift=wf._DriftState(),
                design_context={"rounds": 1, "critiques": [], "stop_reason": "ready"},
                tracker_state={},
            )
        )
    assert result["record"] is None
    assert result["code"] == "llm code"


def test_synthesize_initial_code_short_circuits_on_synthesis_failure():
    from temporalio.exceptions import ApplicationError

    spec = _spec_dict(requires_custom_code=True)
    handlers = {
        "build_short_circuit_record_activity": lambda a: {
            "record": {"lab_record_id": "sc-1", "status": a[0]["short_circuit_status"]},
            "convergence_tracker_state": {},
        },
    }

    async def _fake_exec(fn, *, args, **_kw):
        if fn.__name__ == "code_synthesis_activity":
            raise ApplicationError("empty response", type="CodeSynthesisError", non_retryable=True)
        return handlers[fn.__name__](args)

    cw = wf.StrategyLabCycleWorkflow()
    with mock.patch("temporalio.workflow.execute_activity", _fake_exec):
        result = asyncio.run(
            cw._synthesize_initial_code(
                spec_dict=spec,
                config_dict=_config_dict(),
                rationale="why",
                all_gate_results=[],
                design_attempt=0,
                phase_back_count=0,
                drift=wf._DriftState(),
                design_context={"rounds": 1, "critiques": [], "stop_reason": "ready"},
                tracker_state={},
            )
        )
    assert result["record"]["lab_record_id"] == "sc-1"


# ---------------------------------------------------------------------------
# Synthesis loop
# ---------------------------------------------------------------------------


def _no_gate_failures(*_a, **_kw):
    return []


def test_synthesis_loop_succeeds_on_first_round():
    from investment_team.strategy_lab.quality_gates.backtest_anomaly import BacktestAnomalyDetector
    from investment_team.strategy_lab.quality_gates.code_conformance import CodeConformanceGate
    from investment_team.strategy_lab.quality_gates.code_safety import CodeSafetyChecker
    from investment_team.strategy_lab.quality_gates.predicate_conformance import (
        PredicateConformanceGate,
    )
    from investment_team.strategy_lab.quality_gates.target_symbol_coverage import (
        TargetSymbolCoverageGate,
    )

    spec = _spec_dict()
    bar = {
        "date": "2023-01-01",
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 100.0,
    }
    trade = {
        "trade_num": 1,
        "entry_date": "2023-01-05",
        "exit_date": "2023-01-10",
        "symbol": "AAPL",
        "side": "long",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "shares": 10.0,
        "position_value": 1000.0,
        "gross_pnl": 50.0,
        "net_pnl": 48.0,
        "return_pct": 5.0,
        "hold_days": 5,
        "outcome": "win",
        "cumulative_pnl": 48.0,
    }
    handlers = {
        "resolve_symbols_activity": lambda a: ["AAPL"],
        "fetch_market_data_activity": lambda a: {
            "data": {"AAPL": [bar]},
            "provider_used": {"AAPL": "yahoo"},
        },
        "run_strategy_code_activity": lambda a: {
            "success": True,
            "trades": [trade],
            "stdout": "",
            "stderr": "",
            "execution_time_seconds": 1.0,
            "error_type": None,
            "execution_diagnostics": None,
            "probe_events": None,
            "open_position_entry_reasons": [],
        },
    }
    cw = wf.StrategyLabCycleWorkflow()
    with (
        _patch_execute(handlers),
        mock.patch.object(CodeSafetyChecker, "check", _no_gate_failures),
        mock.patch.object(CodeConformanceGate, "check", _no_gate_failures),
        mock.patch.object(PredicateConformanceGate, "check", _no_gate_failures),
        mock.patch.object(TargetSymbolCoverageGate, "check_fetch", _no_gate_failures),
        mock.patch.object(TargetSymbolCoverageGate, "check_trades", _no_gate_failures),
        mock.patch.object(BacktestAnomalyDetector, "check", _no_gate_failures),
    ):
        result = asyncio.run(
            cw._run_synthesis_loop(
                spec_dict=spec,
                code="def on_bar(): pass",
                config_dict=_config_dict(),
                all_gate_results=[],
                refinement_attempts=[],
                zero_trade_attempts=[],
                drift=wf._DriftState(),
                backtest_cache={},
                consecutive_spec_mutation_rounds={},
            )
        )
    assert result["execution_succeeded"] is True
    assert result["max_rounds_exhausted"] is False
    assert len(result["trades"]) == 1


def test_synthesis_loop_breaks_when_no_market_data():
    from investment_team.strategy_lab.quality_gates.code_conformance import CodeConformanceGate
    from investment_team.strategy_lab.quality_gates.code_safety import CodeSafetyChecker
    from investment_team.strategy_lab.quality_gates.predicate_conformance import (
        PredicateConformanceGate,
    )

    spec = _spec_dict()
    handlers = {
        "resolve_symbols_activity": lambda a: ["AAPL"],
        "fetch_market_data_activity": lambda a: {"data": {}, "provider_used": {}},
    }
    cw = wf.StrategyLabCycleWorkflow()
    with (
        _patch_execute(handlers),
        mock.patch.object(CodeSafetyChecker, "check", _no_gate_failures),
        mock.patch.object(CodeConformanceGate, "check", _no_gate_failures),
        mock.patch.object(PredicateConformanceGate, "check", _no_gate_failures),
    ):
        result = asyncio.run(
            cw._run_synthesis_loop(
                spec_dict=spec,
                code="def on_bar(): pass",
                config_dict=_config_dict(),
                all_gate_results=[],
                refinement_attempts=[],
                zero_trade_attempts=[],
                drift=wf._DriftState(),
                backtest_cache={},
                consecutive_spec_mutation_rounds={},
            )
        )
    assert result["execution_succeeded"] is False


# ---------------------------------------------------------------------------
# Trade alignment loop
# ---------------------------------------------------------------------------


def test_alignment_loop_skips_when_execution_did_not_succeed():
    cw = wf.StrategyLabCycleWorkflow()
    with _patch_execute({}):
        result = asyncio.run(
            cw._run_trade_alignment_loop(
                spec_dict=_spec_dict(),
                code="code",
                trades=[],
                metrics={},
                market_data=None,
                config_dict=_config_dict(),
                execution_succeeded=False,
                all_gate_results=[],
                ran_on_non_conforming_code=False,
                drift=wf._DriftState(),
                backtest_cache={},
            )
        )
    assert result["trades_aligned"] is False
    assert result["alignment_attempts"] == []


def test_alignment_loop_aligned_on_first_round():
    trade = {
        "trade_num": 1,
        "entry_date": "2023-01-05",
        "exit_date": "2023-01-10",
        "symbol": "AAPL",
        "side": "long",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "shares": 10.0,
        "position_value": 1000.0,
        "gross_pnl": 50.0,
        "net_pnl": 48.0,
        "return_pct": 5.0,
        "hold_days": 5,
        "outcome": "win",
        "cumulative_pnl": 48.0,
    }
    metrics = {
        "total_return_pct": 10.0,
        "annualized_return_pct": 10.0,
        "volatility_pct": 5.0,
        "sharpe_ratio": 1.2,
        "max_drawdown_pct": -5.0,
        "win_rate_pct": 55.0,
        "profit_factor": 1.5,
        "sortino_ratio": 1.5,
        "calmar_ratio": 2.0,
        "deflated_sharpe": 0.9,
    }
    bar = {
        "date": "2023-01-01",
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 100.0,
    }
    handlers = {
        "run_alignment_audit_activity": lambda a: {
            "report": {
                "aligned": True,
                "rationale": "clean",
                "issues": [],
                "proposed_code": None,
                "predicted_aligned_after_fix": False,
                "changes_made": "",
                "alignment_findings": [],
            },
            "gate_results": [],
        },
    }
    cw = wf.StrategyLabCycleWorkflow()
    with _patch_execute(handlers):
        result = asyncio.run(
            cw._run_trade_alignment_loop(
                spec_dict=_spec_dict(),
                code="code",
                trades=[trade],
                metrics=metrics,
                market_data={"AAPL": [bar]},
                config_dict=_config_dict(),
                execution_succeeded=True,
                all_gate_results=[],
                ran_on_non_conforming_code=False,
                drift=wf._DriftState(),
                backtest_cache={},
            )
        )
    assert result["trades_aligned"] is True
    assert len(result["alignment_reports"]) == 1


# ---------------------------------------------------------------------------
# Module-level exports
# ---------------------------------------------------------------------------


def test_module_exports_workflow_and_activities():
    assert wf.WORKFLOWS == [wf.StrategyLabCycleWorkflow]
    assert wf.TASK_QUEUE == "strategy-lab-queue"
    from investment_team.strategy_lab.temporal import activities as act

    assert wf.ACTIVITIES == act.ACTIVITIES

"""Unit tests for ``strategy_lab.temporal.activities`` — the fine-grained,
per-side-effect Temporal activities that wrap the Strategy Lab's LLM calls,
sandboxed backtest execution, market-data fetches, and persistence writes.

Mirrors the mock-at-the-boundary style used across the codebase's other
Temporal integrations (e.g. ``market_research_team/tests/test_temporal_activity.py``):
each ``@activity.defn``-decorated function is called directly as a plain
Python function (no Temporal test harness), with the underlying agent-class
method monkeypatched so the test asserts (a) the activity reconstructs the
right Pydantic types from its JSON-shaped input, (b) it calls the *real*
class/method rather than duplicating its logic, and (c) failures map to the
correct ``ApplicationError`` / ``non_retryable`` outcome.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from temporalio.exceptions import ApplicationError

from investment_team.strategy_lab.temporal import activities as act

# ---------------------------------------------------------------------------
# Fixture builders — minimal valid JSON-shaped payloads for the models each
# activity reconstructs.
# ---------------------------------------------------------------------------


def _spec_dict(**overrides: Any) -> Dict[str, Any]:
    base = {
        "strategy_id": "strat-1",
        "authored_by": "DesignAgent",
        "asset_class": "stocks",
        "hypothesis": "test hypothesis",
        "signal_definition": "test signal",
        "timeframe": "1d",
    }
    base.update(overrides)
    return base


def _backtest_config_dict(**overrides: Any) -> Dict[str, Any]:
    base = {"start_date": "2023-01-01", "end_date": "2023-12-31"}
    base.update(overrides)
    return base


def _backtest_result_dict(**overrides: Any) -> Dict[str, Any]:
    base = {
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
    base.update(overrides)
    return base


def _strategy_lab_record_dict(
    *, asset_class: str = "stocks", lab_record_id: str = "rec-1", **overrides: Any
) -> Dict[str, Any]:
    """A minimal ``StrategyLabRecord`` JSON dump for round-trip / merge tests."""
    from investment_team.models import StrategyLabRecord

    record = StrategyLabRecord(
        lab_record_id=lab_record_id,
        strategy=_spec_dict(asset_class=asset_class),
        backtest={
            "backtest_id": f"bt-{lab_record_id}",
            "strategy_id": "strat-1",
            "strategy": _spec_dict(asset_class=asset_class),
            "config": _backtest_config_dict(),
            "submitted_by": "test",
            "submitted_at": "2023-01-01T00:00:00Z",
            "completed_at": "2023-01-01T01:00:00Z",
            "result": _backtest_result_dict(),
        },
        is_winning=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2023-01-01T00:00:00Z",
        **overrides,
    )
    return record.model_dump(mode="json")


# ---------------------------------------------------------------------------
# _map_exception_to_application_error
# ---------------------------------------------------------------------------


def test_map_exception_fatal_llm_error_is_non_retryable():
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    exc = StrategyLabLLMError("bad request", outcome="fatal")
    mapped = act._map_exception_to_application_error(exc)
    assert isinstance(mapped, ApplicationError)
    assert mapped.non_retryable is True
    assert mapped.type == "fatal"


@pytest.mark.parametrize("outcome", ["exhausted", "budget_exhausted"])
def test_map_exception_non_fatal_llm_error_is_retryable(outcome):
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError

    exc = StrategyLabLLMError("timed out", outcome=outcome)
    mapped = act._map_exception_to_application_error(exc)
    assert mapped.non_retryable is False
    assert mapped.type == outcome


def test_map_exception_generic_exception_is_non_retryable():
    mapped = act._map_exception_to_application_error(ValueError("bad json"))
    assert mapped.non_retryable is True
    assert mapped.type == "ValueError"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def test_compute_regime_summary_activity_reuses_compute_regime_summary(monkeypatch):
    from investment_team.strategy_lab import market_regime
    from investment_team.strategy_lab.temporal import activities as act_mod

    def _fake_compute(fetch_ohlcv, *, computed_at, benchmarks=None, days=400):
        return market_regime.RegimeSummary(
            computed_at=computed_at, degraded=True, degraded_reason="no data"
        )

    monkeypatch.setattr(market_regime, "compute_regime_summary", _fake_compute)

    result = act_mod.compute_regime_summary_activity()
    assert result["degraded"] is True


def test_resolve_workflow_config_activity_resolves_every_expected_key(monkeypatch):
    monkeypatch.delenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", raising=False)
    monkeypatch.delenv("STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS", raising=False)
    monkeypatch.delenv("STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED", raising=False)
    monkeypatch.delenv("STRATEGY_LAB_CODE_CONFORMANCE_RETRIES", raising=False)
    monkeypatch.delenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", raising=False)
    monkeypatch.delenv("STRATEGY_LAB_REGIME_SUMMARY_ENABLED", raising=False)

    result = act.resolve_workflow_config_activity()
    assert result == {
        "design_review_rounds": 20,
        "design_review_stall_rounds": 3,
        "mechanical_repair_enabled": True,
        "code_conformance_retries": 2,
        "design_max_llm_calls": 120,
        "regime_summary_enabled": True,
        "max_design_reentries": 2,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_persist_run_state_activity_delegates_to_api_main(monkeypatch):
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 1)

    captured = {}
    monkeypatch.setattr(
        api_main,
        "_persist_run_state",
        lambda run_id, state, *, create=False: captured.update(
            run_id=run_id, state=state, create=create
        ),
    )

    # Exercises the new threaded-generation contract (an explicit generation
    # matching the monkeypatched persisted value), not just the
    # backward-compat omitted-generation default -- that path has its own
    # dedicated test below.
    act.persist_run_state_activity("run-1", {"status": "running"}, create=True, generation=1)
    assert captured == {"run_id": "run-1", "state": {"status": "running"}, "create": True}


# ---------------------------------------------------------------------------
# Generation fencing (#4029)
# ---------------------------------------------------------------------------


def test_persist_run_state_activity_rejects_stale_generation(monkeypatch):
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 2)

    persisted = []
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main, "_persist_run_state", lambda *a, **k: persisted.append((a, k))
    )

    with pytest.raises(ApplicationError) as exc_info:
        act.persist_run_state_activity("run-1", {"status": "running"}, generation=1)

    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "StaleFencingTokenError"
    assert persisted == []  # the write never happened


def test_persist_run_state_activity_accepts_current_or_newer_generation(monkeypatch):
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 2)

    captured = {}
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_persist_run_state",
        lambda run_id, state, *, create=False: captured.update(
            run_id=run_id, state=state, create=create
        ),
    )

    # Same generation: accepted (fan-out from the same incarnation).
    act.persist_run_state_activity("run-1", {"status": "running"}, generation=2)
    assert captured["state"] == {"status": "running"}

    # Newer generation: also accepted.
    act.persist_run_state_activity("run-1", {"status": "completed"}, generation=3)
    assert captured["state"] == {"status": "completed"}


def test_persist_run_state_activity_default_generation_backward_compat(monkeypatch):
    """Omitting generation entirely (a pre-fencing caller) defaults to 1, which is
    accepted against a fresh run's persisted generation of 1."""
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 1)

    captured = {}
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_persist_run_state",
        lambda run_id, state, *, create=False: captured.update(state=state),
    )

    act.persist_run_state_activity("run-1", {"status": "running"})
    assert captured["state"] == {"status": "running"}


def test_persist_run_state_activity_fails_closed_on_generation_lookup_failure(monkeypatch):
    """Regression: a transient durable-read failure inside the fencing check must
    raise (rejecting the write), not silently accept it via a lenient default --
    but stays RETRYABLE (unlike an actual StaleFencingTokenError), since a
    momentary job-service outage should let Temporal retry rather than
    permanently failing the workflow."""
    from investment_team.strategy_lab import run_state

    def _broken(run_id):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(run_state, "get_run_generation_strict", _broken)

    persisted = []
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main, "_persist_run_state", lambda *a, **k: persisted.append((a, k))
    )

    with pytest.raises(ApplicationError) as exc_info:
        act.persist_run_state_activity("run-1", {"status": "running"}, generation=5)

    assert exc_info.value.non_retryable is False
    assert persisted == []  # the write never happened despite a legitimately fresh generation


def test_snapshot_prior_records_activity_delegates_to_api_main(monkeypatch):
    from investment_team.api import main as api_main
    from investment_team.models import StrategyLabRecord

    record = StrategyLabRecord(
        lab_record_id="rec-1",
        strategy=_spec_dict(),
        backtest={
            "backtest_id": "bt-1",
            "strategy_id": "strat-1",
            "strategy": _spec_dict(),
            "config": _backtest_config_dict(),
            "submitted_by": "test",
            "submitted_at": "2023-01-01T00:00:00Z",
            "completed_at": "2023-01-01T01:00:00Z",
            "result": _backtest_result_dict(),
        },
        is_winning=True,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2023-01-01T00:00:00Z",
    )
    monkeypatch.setattr(api_main, "_snapshot_prior_records", lambda *, reverse=False: [record])

    result = act.snapshot_prior_records_activity()
    assert result[0]["lab_record_id"] == "rec-1"


# ---------------------------------------------------------------------------
# Composite activities (wrap a whole orchestrator sub-pipeline verbatim)
# ---------------------------------------------------------------------------


def test_build_short_circuit_record_activity_reuses_orchestrator_method(monkeypatch):
    """build_short_circuit_record_activity delegates record construction to
    StrategyLabOrchestrator._build_short_circuit_record and returns the
    serialized record together with the updated convergence tracker state."""
    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _fake_build(self, **kwargs):
        self.convergence_tracker.increment_trials(1)
        return StrategyLabRecord(
            lab_record_id="rec-sc-1",
            strategy=kwargs["spec"],
            backtest={
                "backtest_id": "bt-sc-1",
                "strategy_id": kwargs["spec"].strategy_id,
                "strategy": kwargs["spec"],
                "config": kwargs["config"],
                "submitted_by": "test",
                "submitted_at": "2023-01-01T00:00:00Z",
                "completed_at": "2023-01-01T01:00:00Z",
                "result": _backtest_result_dict(),
                "status": kwargs["short_circuit_status"],
            },
            is_winning=False,
            strategy_rationale=kwargs["rationale"],
            analysis_narrative=kwargs["short_circuit_reason"],
            created_at="2023-01-01T00:00:00Z",
        )

    monkeypatch.setattr(StrategyLabOrchestrator, "_build_short_circuit_record", _fake_build)

    result = act.build_short_circuit_record_activity(
        {
            "spec": _spec_dict(),
            "config": _backtest_config_dict(),
            "code": "",
            "original_spec": _spec_dict(),
            "original_code": "",
            "rationale": "why",
            "all_gate_results": [],
            "refinement_attempts": [],
            "short_circuit_status": "failed: design_not_ready",
            "short_circuit_reason": "not ready",
            "convergence_tracker_state": {},
        }
    )
    assert result["record"]["lab_record_id"] == "rec-sc-1"
    assert result["record"]["is_winning"] is False
    assert result["convergence_tracker_state"]["trial_count"] == 1


# ---------------------------------------------------------------------------
# run_design_attempt_activity — wraps the whole per-attempt pipeline verbatim
# ---------------------------------------------------------------------------


def _run_design_attempt_params(**overrides: Any) -> Dict[str, Any]:
    """Return a baseline parameter dict for run_design_attempt_activity tests.

    The base dict contains the minimal inputs the activity expects; callers
    override or extend fields via ``**overrides``.
    """
    base = {
        "prior_records": [],
        "config": _backtest_config_dict(),
        "signal_brief": None,
        "exclude_asset_classes": None,
        "directives": ["seed directive"],
        "design_attempt": 0,
        "phase_back_count": 0,
        "drift": {"spec_history": [], "code_history": [], "gate_timeline": []},
        "gate_results": [],
        "budget_calls": 7,
        "regime_summary": None,
        "convergence_tracker_state": {},
    }
    base.update(overrides)
    return base


def test_run_design_attempt_activity_returns_record_outcome(monkeypatch):
    """On a terminal record, the activity returns ``kind='record'`` plus the
    threaded whole-cycle accumulators (tracker state, gate results, budget)."""
    from investment_team.strategy_lab.agents._llm_budget import active_budget
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        # The activity only serializes the record via ``model_dump(mode="json")``;
        # a full ``StrategyLabRecord`` (with its many required fields) isn't needed.
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    record = _FakeRecord()
    captured: Dict[str, Any] = {}

    class _FakeGate:
        gate_name = "g"
        passed = True

        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"gate_name": "g", "passed": True}

    def _fake_attempt(self, **kwargs):
        # The activity must run us inside the pre-charged budget context.
        budget = active_budget()
        captured["budget_calls_seen"] = budget.calls_made if budget else None
        captured["directives"] = kwargs["directives"]
        # Mutate the passed-in gate-results list in place, as the real method does.
        kwargs["cumulative_gate_results"].append(_FakeGate())
        return record

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(_run_design_attempt_params())
    assert out["kind"] == "record"
    assert out["record"]["lab_record_id"] == "rec-1"
    assert out["budget_calls"] == 7  # pre-charged, no further LLM calls made
    assert captured["budget_calls_seen"] == 7
    assert captured["directives"] == ["seed directive"]
    assert "convergence_tracker_state" in out
    assert "drift" in out


def test_run_design_attempt_activity_returns_reentry_outcome(monkeypatch):
    """A ``SpecImplementabilityError`` is caught and surfaced as a structured
    ``kind='reentry'`` outcome carrying last spec/code/evidence + design context
    — never re-raised across the activity boundary."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    last_spec = StrategySpec.parse_persisted(_spec_dict(strategy_id="strat-x"))

    def _fake_attempt(self, **kwargs):
        raise SpecImplementabilityError(
            "risk limits loosened",
            failure_phase="evaluation",
            last_spec=last_spec,
            last_code="def x(): pass",
            design_context=_DesignPersistContext(
                rounds=3, critiques=[], stop_reason="ready", loop_telemetry={"k": 1}
            ),
        )

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(_run_design_attempt_params())
    assert out["kind"] == "reentry"
    assert out["evidence"] == "risk limits loosened"
    assert out["failure_phase"] == "evaluation"
    assert out["last_spec"]["strategy_id"] == "strat-x"
    assert out["last_code"] == "def x(): pass"
    assert out["design_context"]["rounds"] == 3
    assert out["design_context"]["loop_telemetry"] == {"k": 1}
    assert out["budget_calls"] == 7


def test_run_design_attempt_activity_maps_unexpected_error(monkeypatch):
    """Any non-control-flow exception maps to a non-retryable ApplicationError."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _fake_attempt(self, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    with pytest.raises(ApplicationError) as exc_info:
        act.run_design_attempt_activity(_run_design_attempt_params())
    assert exc_info.value.non_retryable is True


def test_run_design_attempt_activity_returns_skipped_outcome_for_502(monkeypatch):
    """A 502 ("no market data") HTTPException is caught and surfaced as a
    structured ``kind='skipped'`` outcome — cycle-terminal, never re-raised —
    mirroring thread mode's soft-skip handling of the same status code."""
    from fastapi import HTTPException

    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _fake_attempt(self, **kwargs):
        raise HTTPException(status_code=502, detail="Failed to fetch historical market data.")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(_run_design_attempt_params())
    assert out["kind"] == "skipped"
    assert out["reason"] == "no_market_data"
    assert out["budget_calls"] == 7
    assert "convergence_tracker_state" in out
    assert "drift" in out


def test_run_design_attempt_activity_returns_skipped_outcome_for_market_data_gate(monkeypatch):
    """The real production signal: no exception at all — a failed
    "market_data" gate recorded on this attempt's own gate additions is
    detected and reported as a skip instead of a "record" outcome, matching
    what ``_fetch_market_data``/``_fetch_market_data_for_synthesis`` actually
    do (they never raise; they record this gate and let the design attempt
    return a normal-shaped record)."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-nodata"}

    class _FakeGate:
        def __init__(self, gate_name: str, passed: bool) -> None:
            self.gate_name = gate_name
            self.passed = passed

        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"gate_name": self.gate_name, "passed": self.passed}

    def _fake_attempt(self, **kwargs):
        kwargs["cumulative_gate_results"].append(_FakeGate("market_data", False))
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(_run_design_attempt_params())
    assert out["kind"] == "skipped"
    assert out["reason"] == "no_market_data"
    assert "record" not in out


def test_run_design_attempt_activity_ignores_prior_attempts_market_data_gate(monkeypatch):
    """A market_data gate failure from an earlier (already re-entered)
    attempt, carried forward in the seeded ``gate_results``, must not cause a
    later, genuinely successful attempt to be misreported as skipped — only
    gates appended during THIS call count."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-ok"}

    def _fake_attempt(self, **kwargs):
        # No new gate appended this attempt — data fetch succeeded this time.
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    params = _run_design_attempt_params(
        gate_results=[
            {
                "gate_name": "market_data",
                "passed": False,
                "phase": "synthesis",
                "severity": "critical",
                "details": "prior attempt had no data",
            }
        ]
    )
    out = act.run_design_attempt_activity(params)
    assert out["kind"] == "record"
    assert out["record"]["lab_record_id"] == "rec-ok"


def test_run_design_attempt_activity_maps_non_502_http_exception_as_fatal(monkeypatch):
    """A non-502 HTTPException is still a deep failure (matches thread mode's
    "non-502 HTTPException from a cycle is a deep failure" branch) — mapped
    to a non-retryable ApplicationError, not a skip."""
    from fastapi import HTTPException

    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _fake_attempt(self, **kwargs):
        raise HTTPException(status_code=500, detail="unexpected")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    with pytest.raises(ApplicationError) as exc_info:
        act.run_design_attempt_activity(_run_design_attempt_params())
    assert exc_info.value.non_retryable is True


# ---------------------------------------------------------------------------
# Batch-level activities (Stage 4)
# ---------------------------------------------------------------------------


def test_compute_signal_brief_activity_serializes_brief_and_storage(monkeypatch):
    """compute_signal_brief_activity returns the signal brief as a serialized
    dict and passes the storage metadata through unchanged."""
    from investment_team.api import main as api_main

    class _FakeBrief:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"brief_version": "v1"}

    monkeypatch.setattr(
        api_main,
        "_compute_signal_brief_snapshot",
        lambda benchmark_symbol: (_FakeBrief(), {"stored": True, "sym": benchmark_symbol}),
    )
    out = act.compute_signal_brief_activity("SPY")
    assert out["signal_brief"] == {"brief_version": "v1"}
    assert out["signal_brief_storage"] == {"stored": True, "sym": "SPY"}


def test_compute_signal_brief_activity_handles_none_brief(monkeypatch):
    """compute_signal_brief_activity correctly surfaces a None brief without
    failing and still returns the storage metadata."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_compute_signal_brief_snapshot",
        lambda benchmark_symbol: (None, {"skipped": True}),
    )
    out = act.compute_signal_brief_activity("SPY")
    assert out["signal_brief"] is None
    assert out["signal_brief_storage"] == {"skipped": True}


def test_is_run_cancelled_activity_delegates(monkeypatch):
    """is_run_cancelled_activity delegates to the api_main helper and returns
    the helper's boolean result unchanged."""
    from investment_team.api import main as api_main

    seen = {}

    def _fake(run_id):
        seen["run_id"] = run_id
        return True

    monkeypatch.setattr(api_main, "_is_strategy_lab_run_cancelled", _fake)
    assert act.is_run_cancelled_activity("run-42") is True
    assert seen["run_id"] == "run-42"


def test_finalize_cycle_record_activity_delegates_and_serializes(monkeypatch):
    """A non-stale call parses the persisted record, delegates to
    _finalize_strategy_lab_cycle_record with the record and every
    paper_trading_*/signal_brief_storage param forwarded verbatim, and
    returns the finalized record's JSON dump."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    seen_run_ids: List[str] = []
    monkeypatch.setattr(
        run_state,
        "get_run_generation_strict",
        lambda run_id: seen_run_ids.append(run_id) or 1,
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-final"}

    captured = {}

    def _fake_finalize(record, **kwargs):
        captured.update(kwargs)
        captured["record"] = record
        return _FakeRecord()

    monkeypatch.setattr(api_main, "_finalize_strategy_lab_cycle_record", _fake_finalize)
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    out = act.finalize_cycle_record_activity(
        {
            "run_id": "run-final-1",
            "generation": 1,
            "record": {"lab_record_id": "raw-1"},
            "signal_brief_storage": {"s": 1},
            "paper_trading_enabled": False,
            "paper_trading_lookback_days": 90,
        }
    )
    assert out["record"] == {"lab_record_id": "rec-final"}
    assert captured["record"] == "parsed:raw-1"
    assert captured["signal_brief_storage"] == {"s": 1}
    assert captured["paper_trading_enabled"] is False
    assert captured["paper_trading_lookback_days"] == 90
    # The fencing check ran against the correct run, both before and after
    # the delegate call (the pre-check's cheap early exit and the
    # post-check that catches a restart minting a newer generation while
    # this call was in flight) -- if a regression removed either lookup,
    # this test would otherwise still pass because the patched function
    # would simply go unused for that call.
    assert seen_run_ids == ["run-final-1", "run-final-1"]


def test_finalize_cycle_record_activity_rejects_stale_generation(monkeypatch):
    """When the payload's generation (1) is older than the persisted
    generation (2), check_fencing_token raises StaleFencingTokenError, the
    pre-check maps it to a non-retryable ApplicationError, and
    _finalize_strategy_lab_cycle_record is never called."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    seen_run_ids: List[str] = []
    monkeypatch.setattr(
        run_state,
        "get_run_generation_strict",
        lambda run_id: seen_run_ids.append(run_id) or 2,
    )

    finalize_calls = []
    monkeypatch.setattr(
        api_main,
        "_finalize_strategy_lab_cycle_record",
        lambda *a, **k: finalize_calls.append((a, k)),
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    with pytest.raises(ApplicationError) as exc_info:
        act.finalize_cycle_record_activity(
            {"run_id": "run-final-2", "generation": 1, "record": {"lab_record_id": "raw-1"}}
        )

    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "StaleFencingTokenError"
    assert finalize_calls == []  # the durable record write never happened
    # Confirms the fencing lookup was performed against the run this call
    # actually targeted, not a hardcoded or otherwise-wrong run_id.
    assert seen_run_ids == ["run-final-2"]


def test_finalize_cycle_record_activity_accepts_current_generation(monkeypatch):
    """A payload generation equal to the persisted generation is accepted
    (check_fencing_token's >= semantics) and the record is finalized
    normally -- distinct from the strictly-newer case covered by
    test_finalize_cycle_record_activity_accepts_generation_newer_than_persisted."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 2)

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-final"}

    monkeypatch.setattr(
        api_main, "_finalize_strategy_lab_cycle_record", lambda *a, **k: _FakeRecord()
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    out = act.finalize_cycle_record_activity(
        {"run_id": "run-final-3", "generation": 2, "record": {"lab_record_id": "raw-1"}}
    )
    assert out["record"] == {"lab_record_id": "rec-final"}


def test_finalize_cycle_record_activity_accepts_generation_newer_than_persisted(monkeypatch):
    """check_fencing_token only rejects provided < current; a provided
    generation strictly NEWER than the persisted one (not just equal) must
    also be accepted -- distinct from the equal-tokens case already covered
    by test_finalize_cycle_record_activity_accepts_current_generation."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 2)

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-final-newer"}

    monkeypatch.setattr(
        api_main, "_finalize_strategy_lab_cycle_record", lambda *a, **k: _FakeRecord()
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    out = act.finalize_cycle_record_activity(
        {"run_id": "run-final-newer", "generation": 5, "record": {"lab_record_id": "raw-1"}}
    )
    assert out["record"] == {"lab_record_id": "rec-final-newer"}


def test_finalize_cycle_record_activity_defaults_generation_when_omitted(monkeypatch):
    """A payload with run_id but no "generation" key (a caller predating the
    field, distinct from the no-run_id-at-all backward-compat path already
    covered elsewhere) must default to generation 1 -- accepted against a
    fresh/never-restarted run's persisted generation of 1."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 1)

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-final-default"}

    monkeypatch.setattr(
        api_main, "_finalize_strategy_lab_cycle_record", lambda *a, **k: _FakeRecord()
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    # No "generation" key at all.
    out = act.finalize_cycle_record_activity(
        {"run_id": "run-final-default", "record": {"lab_record_id": "raw-1"}}
    )
    assert out["record"] == {"lab_record_id": "rec-final-default"}


def test_finalize_cycle_record_activity_omitted_generation_rejected_against_restarted_run(
    monkeypatch,
):
    """Regression: an omitted "generation" key defaults to 1 (the legacy
    generation) -- that default must be REJECTED as stale when the run has
    since been restarted (persisted generation > 1), not just accepted
    against a fresh run's persisted generation of 1. Without this, a
    regression that defaulted the missing generation to the current value
    (or otherwise bypassed the fencing check) would go undetected."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 3)

    finalize_calls = []
    monkeypatch.setattr(
        api_main,
        "_finalize_strategy_lab_cycle_record",
        lambda *a, **k: finalize_calls.append((a, k)),
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    with pytest.raises(ApplicationError) as exc_info:
        act.finalize_cycle_record_activity(
            {"run_id": "run-final-stale-default", "record": {"lab_record_id": "raw-1"}}
        )

    assert exc_info.value.type == "StaleFencingTokenError"
    assert exc_info.value.non_retryable is True
    assert finalize_calls == []  # the durable record write never happened


def test_finalize_cycle_record_activity_rejects_generation_that_went_stale_during_finalize(
    monkeypatch,
):
    """Regression: the post-check (after _finalize_strategy_lab_cycle_record
    returns) must catch a restart that mints a newer generation WHILE that call
    was running -- market-data fetch + paper-trading execution is a real amount
    of time, so a pre-check alone (which passes here) is not enough. Also
    verifies the delegate call actually happened between the two checks: a
    regression that performed both fencing lookups but skipped the durable
    write would otherwise still pass a test that only checks the raised
    exception, without proving the post-check guards a real finalize
    operation."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    # Pre-check sees generation 2 (current, matches); post-check sees 5 (a
    # restart minted a newer one while the finalize call below was "running").
    generation_reads = iter([2, 5])
    monkeypatch.setattr(
        run_state, "get_run_generation_strict", lambda run_id: next(generation_reads)
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-final"}

    finalize_calls = []

    def _fake_finalize(*a, **k):
        finalize_calls.append((a, k))
        return _FakeRecord()

    monkeypatch.setattr(api_main, "_finalize_strategy_lab_cycle_record", _fake_finalize)
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    with pytest.raises(ApplicationError) as exc_info:
        act.finalize_cycle_record_activity(
            {"run_id": "run-final-5", "generation": 2, "record": {"lab_record_id": "raw-1"}}
        )

    assert exc_info.value.type == "StaleFencingTokenError"
    assert exc_info.value.non_retryable is True
    assert finalize_calls  # the durable write was attempted before the post-check caught it


def test_finalize_cycle_record_activity_fails_closed_on_generation_lookup_failure(monkeypatch):
    """Regression: a transient durable-read failure inside the fencing check must
    raise (rejecting the finalize/persist), not silently accept it via a lenient
    default -- otherwise an outage could mask a genuinely superseded generation.
    Stays RETRYABLE (unlike an actual StaleFencingTokenError), matching
    persist_run_state_activity's classification of the same failure mode."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    def _broken(run_id):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(run_state, "get_run_generation_strict", _broken)

    finalize_calls = []
    monkeypatch.setattr(
        api_main,
        "_finalize_strategy_lab_cycle_record",
        lambda *a, **k: finalize_calls.append((a, k)),
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    with pytest.raises(ApplicationError) as exc_info:
        act.finalize_cycle_record_activity(
            {"run_id": "run-final-4", "generation": 5, "record": {"lab_record_id": "raw-1"}}
        )

    assert exc_info.value.non_retryable is False
    assert finalize_calls == []


def test_finalize_cycle_record_activity_tolerates_missing_run_id(monkeypatch):
    """Backward compat: a strategy_lab_finalize_cycle_record task Temporal already
    scheduled from a pre-upgrade workflow history (its recorded input predates
    run_id/generation entirely) must still succeed on retry, not KeyError. When
    even the activity-context run_id recovery fails (as here -- this test calls
    the activity directly, outside any real Temporal execution, so
    activity.info() raises) both fencing checks no-op rather than crash."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    def _fail_if_called(run_id):
        raise AssertionError("get_run_generation_strict must not be called when run_id is absent")

    monkeypatch.setattr(run_state, "get_run_generation_strict", _fail_if_called)

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-legacy"}

    monkeypatch.setattr(
        api_main, "_finalize_strategy_lab_cycle_record", lambda *a, **k: _FakeRecord()
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    # No run_id, no generation -- exactly the pre-#4029 payload shape.
    out = act.finalize_cycle_record_activity(
        {
            "record": {"lab_record_id": "raw-legacy"},
            "signal_brief_storage": None,
            "paper_trading_enabled": True,
            "paper_trading_lookback_days": 365,
        }
    )
    assert out["record"] == {"lab_record_id": "rec-legacy"}


# ---------------------------------------------------------------------------
# _infer_run_id_from_activity_context (#4029 review round 4)
# ---------------------------------------------------------------------------


def test_infer_run_id_from_activity_context_recovers_from_workflow_id(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        act.activity, "info", lambda: SimpleNamespace(workflow_id="strategy-lab-run-42")
    )
    assert act._infer_run_id_from_activity_context() == "run-42"


def test_infer_run_id_from_activity_context_returns_none_outside_activity_execution(monkeypatch):
    def _no_context():
        raise RuntimeError("Not in activity context")

    monkeypatch.setattr(act.activity, "info", _no_context)
    assert act._infer_run_id_from_activity_context() is None


def test_infer_run_id_from_activity_context_returns_none_for_mismatched_prefix(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        act.activity, "info", lambda: SimpleNamespace(workflow_id="some-other-workflow-id")
    )
    assert act._infer_run_id_from_activity_context() is None


def test_finalize_cycle_record_activity_recovers_run_id_from_activity_context(monkeypatch):
    """A pre-upgrade payload missing run_id must still be fenced when a real
    Temporal execution context is available -- recovering run_id from
    workflow_id rather than skipping the check outright."""
    from types import SimpleNamespace

    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(
        act.activity, "info", lambda: SimpleNamespace(workflow_id="strategy-lab-run-legacy2")
    )

    seen_run_ids = []

    def _get_generation(run_id):
        seen_run_ids.append(run_id)
        return 1  # matches params["generation"] below -- not stale

    monkeypatch.setattr(run_state, "get_run_generation_strict", _get_generation)

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-legacy2"}

    monkeypatch.setattr(
        api_main, "_finalize_strategy_lab_cycle_record", lambda *a, **k: _FakeRecord()
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    # No run_id in params -- must be recovered from the activity context above.
    out = act.finalize_cycle_record_activity(
        {"generation": 1, "record": {"lab_record_id": "raw-legacy2"}}
    )
    assert out["record"] == {"lab_record_id": "rec-legacy2"}
    assert seen_run_ids == ["run-legacy2", "run-legacy2"]  # pre-check + post-check, both fenced


def test_finalize_cycle_record_activity_recovered_run_id_still_rejects_stale_generation(
    monkeypatch,
) -> None:
    """Companion to the success-path test above: recovering run_id from the
    activity context must not merely be tolerated -- it must actually fence.
    A pre-upgrade payload (no run_id) whose recovered run_id's durable
    generation has since advanced must still be rejected, not silently
    accepted just because the payload predates run_id."""
    from types import SimpleNamespace

    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(
        act.activity, "info", lambda: SimpleNamespace(workflow_id="strategy-lab-run-stale2")
    )
    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 3)

    finalize_calls = []
    monkeypatch.setattr(
        api_main,
        "_finalize_strategy_lab_cycle_record",
        lambda *a, **k: finalize_calls.append(1),
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    # No run_id in params -- must be recovered from the activity context above,
    # and its (stale, generation=1) payload must be rejected before
    # _finalize_strategy_lab_cycle_record ever runs.
    with pytest.raises(ApplicationError) as exc_info:
        act.finalize_cycle_record_activity(
            {"generation": 1, "record": {"lab_record_id": "raw-stale2"}}
        )
    assert exc_info.value.type == "StaleFencingTokenError"
    assert exc_info.value.non_retryable is True
    assert finalize_calls == []  # rejected at the pre-check, never reached the write


def test_finalize_cycle_record_activity_post_check_lookup_failure_is_not_retryable(monkeypatch):
    """Regression: unlike the pre-check, the post-check's lookup failure must
    NOT be Temporal-retryable once its bounded local retries are exhausted --
    _finalize_strategy_lab_cycle_record already durably committed by that
    point, so retrying the whole activity would re-execute its non-idempotent
    side effects (a fresh paper-trading session) again. The local retries
    themselves are exercised here too: every post-check attempt fails, so all
    of them (1 initial + len(_POST_WRITE_LOOKUP_RETRY_DELAYS_SECONDS) retries)
    must run before the ApplicationError is raised."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(act.time, "sleep", lambda seconds: None)

    # First call (pre-check) succeeds; every subsequent call (post-check,
    # including its local retries) fails.
    call_count = {"n": 0}

    def _get_generation(run_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return 2
        raise ConnectionError("connection refused")

    monkeypatch.setattr(run_state, "get_run_generation_strict", _get_generation)

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-final"}

    monkeypatch.setattr(
        api_main, "_finalize_strategy_lab_cycle_record", lambda *a, **k: _FakeRecord()
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    with pytest.raises(ApplicationError) as exc_info:
        act.finalize_cycle_record_activity(
            {"run_id": "run-final-6", "generation": 2, "record": {"lab_record_id": "raw-1"}}
        )

    assert exc_info.value.non_retryable is True
    # 1 pre-check + (1 initial + 2 retries) post-check attempts.
    assert call_count["n"] == 1 + (1 + len(act._POST_WRITE_LOOKUP_RETRY_DELAYS_SECONDS))


def test_finalize_cycle_record_activity_post_check_recovers_from_transient_lookup_failure(
    monkeypatch,
):
    """A post-check lookup failure that succeeds on a bounded local retry must
    NOT fail the activity at all -- this is the actual availability fix: a
    momentary job-service blip no longer permanently fails an
    otherwise-successful run."""
    from investment_team.api import main as api_main
    from investment_team.strategy_lab import run_state

    sleeps: List[float] = []
    monkeypatch.setattr(act.time, "sleep", sleeps.append)

    # Pre-check succeeds (call 1); post-check fails once then succeeds (calls 2-3).
    call_count = {"n": 0}

    def _get_generation(run_id):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ConnectionError("connection refused")
        return 2

    monkeypatch.setattr(run_state, "get_run_generation_strict", _get_generation)

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-final"}

    monkeypatch.setattr(
        api_main, "_finalize_strategy_lab_cycle_record", lambda *a, **k: _FakeRecord()
    )
    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: f"parsed:{r['lab_record_id']}"),
    )

    out = act.finalize_cycle_record_activity(
        {"run_id": "run-final-7", "generation": 2, "record": {"lab_record_id": "raw-1"}}
    )

    assert out["record"] == {"lab_record_id": "rec-final"}
    assert call_count["n"] == 3  # pre-check + 1 failed + 1 successful post-check attempt
    assert sleeps == [act._POST_WRITE_LOOKUP_RETRY_DELAYS_SECONDS[0]]


def test_merge_wave_results_activity_merges_in_cycle_index_order():
    """The activity records each cycle's spec + folds its trial-count delta,
    processing settled cycles in cycle-index order (reproducible directives)."""
    from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker

    # Primary tracker with 2 prior trials.
    primary = ConvergenceTracker()
    primary.increment_trials(2)
    primary_state = primary.to_wire_dict()

    def _cycle_tracker_state(extra_trials: int) -> Dict[str, Any]:
        # A snapshot of the primary that ran `extra_trials` more trials in-cycle.
        snap = ConvergenceTracker.from_wire_dict(primary_state).snapshot()
        snap.increment_trials(extra_trials)
        return snap.to_wire_dict()

    def _record_dump(asset_class: str) -> Dict[str, Any]:
        return _strategy_lab_record_dict(asset_class=asset_class)

    params = {
        "primary_tracker_state": primary_state,
        # Deliberately out of order to prove the activity sorts.
        "wave_results": [
            {
                "cycle_index": 1,
                "record": _record_dump("crypto"),
                "cycle_tracker_state": _cycle_tracker_state(3),
            },
            {
                "cycle_index": 0,
                "record": _record_dump("stocks"),
                "cycle_tracker_state": _cycle_tracker_state(1),
            },
        ],
    }
    out = act.merge_wave_results_activity(params)
    merged = ConvergenceTracker.from_wire_dict(out["primary_tracker_state"])
    # 2 (primary) + 1 + 3 (deltas), never double-counting the pre-snapshot total.
    assert merged.trial_count == 6
    # Both cycles' asset classes recorded for diversity steering, in index order.
    assert merged._asset_class_history == ["stocks", "crypto"]
    assert out["merge_errors"] == []


def test_merge_wave_results_activity_isolates_single_merge_failure(monkeypatch):
    """A single record's ``merge_from`` failure is captured, not fatal: the
    activity still succeeds, the other record's merge still lands, and the
    failure is reported as a structured ``merge_errors`` entry."""
    from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker

    primary_state = ConvergenceTracker().to_wire_dict()

    def _record_dump(asset_class: str) -> Dict[str, Any]:
        return _strategy_lab_record_dict(asset_class=asset_class)

    real_merge_from = ConvergenceTracker.merge_from
    calls = {"n": 0}

    def _flaky_merge_from(self, other):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("merge boom")
        return real_merge_from(self, other)

    monkeypatch.setattr(ConvergenceTracker, "merge_from", _flaky_merge_from)

    params = {
        "primary_tracker_state": primary_state,
        # Non-adjacent indices so the reported ``cycle_index`` below can only
        # match via the documented ``+ 1`` offset, not by coincidence with a
        # neighboring record's index.
        "wave_results": [
            {
                "cycle_index": 5,
                "record": _record_dump("stocks"),
                "cycle_tracker_state": primary_state,
            },
            {
                "cycle_index": 12,
                "record": _record_dump("crypto"),
                "cycle_tracker_state": primary_state,
            },
        ],
    }
    out = act.merge_wave_results_activity(params)
    # Both records still recorded for diversity steering (outside the isolated try).
    merged = ConvergenceTracker.from_wire_dict(out["primary_tracker_state"])
    assert merged._asset_class_history == ["stocks", "crypto"]
    # The failing record is the first processed in sorted (cycle-index) order,
    # i.e. the one with input cycle_index=5; the reported cycle_index is the
    # 1-based cycle number (input + 1), not the raw 0-based input value.
    assert out["merge_errors"] == [
        {
            "cycle_index": 6,
            "error": "merge boom",
            "exception_type": "ValueError",
            "reason": "tracker_merge_failed",
        }
    ]


# -- Direct tests for the extracted api.main helpers the batch activities wrap --


def test_compute_signal_brief_snapshot_disabled_returns_skip(monkeypatch):
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_signal_expert_enabled", lambda: False)
    brief, storage = api_main._compute_signal_brief_snapshot("SPY")
    assert brief is None
    assert storage == {"skipped": True, "skipped_reason": "signal_expert_disabled"}


def test_is_strategy_lab_run_cancelled_reads_job_status(monkeypatch):
    from investment_team.api import main as api_main

    class _FakeClient:
        def __init__(self, status):
            self._status = status

        def get_job(self, run_id):
            return {"status": self._status} if self._status is not None else None

    def _use(status):
        monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _FakeClient(status))

    _use("cancelled")
    assert api_main._is_strategy_lab_run_cancelled("r") is True
    _use("failed")
    assert api_main._is_strategy_lab_run_cancelled("r") is True
    _use("running")
    assert api_main._is_strategy_lab_run_cancelled("r") is False
    _use(None)  # no persisted job
    assert api_main._is_strategy_lab_run_cancelled("r") is False
    # completed is a terminal *success*, not a cancellation.
    _use("completed")
    assert api_main._is_strategy_lab_run_cancelled("r") is False


def test_is_strategy_lab_run_cancelled_swallows_errors(monkeypatch):
    from investment_team.api import main as api_main

    def _boom():
        raise RuntimeError("job service down")

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", _boom)
    assert api_main._is_strategy_lab_run_cancelled("r") is False


def test_compute_signal_brief_activity_maps_unexpected_error(monkeypatch):
    from investment_team.api import main as api_main

    def _boom(benchmark_symbol):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(api_main, "_compute_signal_brief_snapshot", _boom)
    with pytest.raises(ApplicationError):
        act.compute_signal_brief_activity("SPY")


def test_finalize_cycle_record_activity_maps_malformed_record_parse_error():
    """A malformed `record` payload (missing required fields) raises inside
    `StrategyLabRecord.parse_persisted`, which now runs inside the same
    try/except as the finalize delegate call -- it must map to
    ApplicationError like any other failure in this activity's body, not
    propagate as a raw, unmapped pydantic ValidationError."""
    with pytest.raises(ApplicationError):
        act.finalize_cycle_record_activity({"record": {"lab_record_id": "incomplete"}})


def test_finalize_cycle_record_activity_maps_unexpected_error(monkeypatch):
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        "investment_team.models.StrategyLabRecord.parse_persisted",
        staticmethod(lambda r: r),
    )

    def _boom(record, **kwargs):
        raise RuntimeError("finalize exploded")

    monkeypatch.setattr(api_main, "_finalize_strategy_lab_cycle_record", _boom)
    with pytest.raises(ApplicationError):
        act.finalize_cycle_record_activity({"record": {"lab_record_id": "x"}})


def test_merge_wave_results_activity_maps_unexpected_error():
    # A malformed wave_results entry (missing keys) trips the reconstruction and
    # maps to ApplicationError rather than crashing the worker opaquely.
    with pytest.raises(ApplicationError):
        act.merge_wave_results_activity(
            {"primary_tracker_state": {}, "wave_results": [{"cycle_index": 0}]}
        )


def test_activities_list_contains_every_activity():
    assert len(act.ACTIVITIES) == 11
    assert act.compute_regime_summary_activity in act.ACTIVITIES
    assert act.persist_run_state_activity in act.ACTIVITIES
    assert act.snapshot_prior_records_activity in act.ACTIVITIES
    assert act.build_short_circuit_record_activity in act.ACTIVITIES
    assert act.run_design_attempt_activity in act.ACTIVITIES
    assert act.resolve_workflow_config_activity in act.ACTIVITIES
    # Batch-level activities (Stage 4).
    assert act.compute_signal_brief_activity in act.ACTIVITIES
    assert act.is_run_cancelled_activity in act.ACTIVITIES
    assert act.external_terminal_status_activity in act.ACTIVITIES
    assert act.finalize_cycle_record_activity in act.ACTIVITIES
    assert act.merge_wave_results_activity in act.ACTIVITIES

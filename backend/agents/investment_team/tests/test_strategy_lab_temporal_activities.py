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
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)
    monkeypatch.delenv("LLM_BACKOFF_MAX", raising=False)

    result = act.resolve_workflow_config_activity()
    assert result == {
        "design_review_rounds": 20,
        "design_review_stall_rounds": 3,
        "mechanical_repair_enabled": True,
        "code_conformance_retries": 2,
        "design_max_llm_calls": 120,
        "regime_summary_enabled": True,
        "max_design_reentries": 2,
        "llm_timeout_s": 3600.0,
        "llm_max_retries": 10,
        "llm_backoff_cap_s": 120.0,
    }


def test_resolve_workflow_config_activity_reflects_llm_timeout_override(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT", "5400")

    result = act.resolve_workflow_config_activity()
    assert result["llm_timeout_s"] == 5400.0


def test_resolve_workflow_config_activity_reflects_llm_max_retries_override(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RETRIES", "4")

    result = act.resolve_workflow_config_activity()
    assert result["llm_max_retries"] == 4


def test_resolve_workflow_config_activity_reflects_llm_backoff_cap_override(monkeypatch):
    monkeypatch.setenv("LLM_BACKOFF_MAX", "60")

    result = act.resolve_workflow_config_activity()
    assert result["llm_backoff_cap_s"] == 60.0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_persist_run_state_activity_delegates_to_orchestrator_api(monkeypatch):
    from investment_team.strategy_lab import orchestrator_api, run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 1)

    captured = {}
    monkeypatch.setattr(
        orchestrator_api,
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
    from investment_team.strategy_lab import orchestrator_api

    monkeypatch.setattr(
        orchestrator_api, "_persist_run_state", lambda *a, **k: persisted.append((a, k))
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
    from investment_team.strategy_lab import orchestrator_api

    monkeypatch.setattr(
        orchestrator_api,
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
    from investment_team.strategy_lab import orchestrator_api

    monkeypatch.setattr(
        orchestrator_api,
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
    from investment_team.strategy_lab import orchestrator_api

    monkeypatch.setattr(
        orchestrator_api, "_persist_run_state", lambda *a, **k: persisted.append((a, k))
    )

    with pytest.raises(ApplicationError) as exc_info:
        act.persist_run_state_activity("run-1", {"status": "running"}, generation=5)

    assert exc_info.value.non_retryable is False
    assert persisted == []  # the write never happened despite a legitimately fresh generation


# ---------------------------------------------------------------------------
# Design-attempt checkpoint (DesignAttemptCheckpoint / persist_design_attempt_checkpoint)
# ---------------------------------------------------------------------------


def _design_attempt_checkpoint(**overrides: Any):
    from investment_team.models import DesignAttemptCheckpoint

    base = dict(
        run_id="run-1",
        cycle_scope="run-1-c0",
        design_attempt=0,
        generation=1,
        spec=_spec_dict(),
        rationale="because backtests said so",
        design_context={
            "rounds": 2,
            "critiques": [],
            "stop_reason": "converged",
            "loop_telemetry": {},
        },
        spec_history=[],
        code_history=[],
        gate_timeline=[],
        gate_results=[],
        budget_calls=12,
    )
    base.update(overrides)
    return DesignAttemptCheckpoint(**base)


def test_design_attempt_checkpoint_round_trips_through_model_dump():
    from investment_team.models import DesignAttemptCheckpoint

    checkpoint = _design_attempt_checkpoint(
        spec_history=[
            {
                "phase": "design",
                "agent": "DesignAgent",
                "timestamp": "2023-01-01T00:00:00Z",
                "before_hash": "a" * 64,
                "after_hash": "b" * 64,
                "diff": "- old\n+ new",
                "reason": "refinement",
            }
        ],
        gate_timeline=[
            {
                "phase": "design",
                "gate_name": "spec_completeness",
                "passed": True,
                "severity": "info",
                "details": "ok",
                "timestamp": "2023-01-01T00:00:00Z",
            }
        ],
        gate_results=[{"gate_name": "spec_completeness", "passed": True}],
    )

    dumped = checkpoint.model_dump(mode="json")
    restored = DesignAttemptCheckpoint.model_validate(dumped)

    assert restored == checkpoint
    assert restored.cycle_scope == "run-1-c0"


def test_design_attempt_checkpoint_is_frozen():
    from pydantic import ValidationError

    checkpoint = _design_attempt_checkpoint()
    with pytest.raises(ValidationError, match="frozen"):
        checkpoint.rationale = "mutated"


def test_persist_design_attempt_checkpoint_delegates_to_persist_run_state(monkeypatch):
    from investment_team.strategy_lab import orchestrator_api, run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 1)

    captured = {}
    monkeypatch.setattr(
        orchestrator_api,
        "_persist_run_state",
        lambda run_id, state, *, create=False: captured.update(
            run_id=run_id, state=state, create=create
        ),
    )

    checkpoint = _design_attempt_checkpoint(run_id="run-1", cycle_scope="run-1-c0", generation=1)
    act.persist_design_attempt_checkpoint(checkpoint)

    assert captured["run_id"] == "run-1"
    assert captured["create"] is False
    assert captured["state"] == {
        f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c0": checkpoint.model_dump(mode="json")
    }


def test_persist_design_attempt_checkpoint_rejects_stale_generation(monkeypatch):
    from investment_team.strategy_lab import orchestrator_api, run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 2)

    persisted = []
    monkeypatch.setattr(
        orchestrator_api, "_persist_run_state", lambda *a, **k: persisted.append((a, k))
    )

    checkpoint = _design_attempt_checkpoint(run_id="run-1", generation=1)
    with pytest.raises(ApplicationError) as exc_info:
        act.persist_design_attempt_checkpoint(checkpoint)

    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "StaleFencingTokenError"
    assert persisted == []  # the write never happened


def test_persist_design_attempt_checkpoint_accepts_current_or_newer_generation(monkeypatch):
    from investment_team.strategy_lab import orchestrator_api, run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 2)

    captured = {}
    monkeypatch.setattr(
        orchestrator_api,
        "_persist_run_state",
        lambda run_id, state, *, create=False: captured.update(state=state),
    )

    # Same generation: accepted (fan-out from the same incarnation).
    act.persist_design_attempt_checkpoint(
        _design_attempt_checkpoint(generation=2, design_attempt=0, cycle_scope="run-1-c0")
    )
    assert (
        captured["state"][f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c0"][
            "design_attempt"
        ]
        == 0
    )

    # Newer generation: also accepted.
    act.persist_design_attempt_checkpoint(
        _design_attempt_checkpoint(generation=3, design_attempt=1, cycle_scope="run-1-c0")
    )
    assert (
        captured["state"][f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c0"][
            "design_attempt"
        ]
        == 1
    )


def test_persist_design_attempt_checkpoint_scopes_field_by_cycle(monkeypatch):
    """Two checkpoints sharing (run_id, design_attempt) but different
    cycle_scope must land in two distinct fields -- the whole point of
    baking cycle_scope into the field name: two StrategyLabCycleWorkflow
    children racing the same run_id in one wave (StrategyLabBatchWorkflow's
    max_parallel) must never clobber each other's checkpoint."""
    from investment_team.strategy_lab import orchestrator_api, run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 1)

    captured_states = []
    monkeypatch.setattr(
        orchestrator_api,
        "_persist_run_state",
        lambda run_id, state, *, create=False: captured_states.append(state),
    )

    act.persist_design_attempt_checkpoint(
        _design_attempt_checkpoint(run_id="run-1", cycle_scope="run-1-c0", design_attempt=0)
    )
    act.persist_design_attempt_checkpoint(
        _design_attempt_checkpoint(run_id="run-1", cycle_scope="run-1-c1", design_attempt=0)
    )

    field_names = {list(state.keys())[0] for state in captured_states}
    assert field_names == {
        f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c0",
        f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c1",
    }


def test_persist_design_attempt_checkpoint_fails_closed_on_generation_lookup_failure(monkeypatch):
    """A transient durable-read failure inside the fencing check must raise
    (rejecting the write) but stays RETRYABLE, mirroring
    persist_run_state_activity's own fail-closed-but-retryable contract."""
    from investment_team.strategy_lab import orchestrator_api, run_state

    def _broken(run_id):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(run_state, "get_run_generation_strict", _broken)

    persisted = []
    monkeypatch.setattr(
        orchestrator_api, "_persist_run_state", lambda *a, **k: persisted.append((a, k))
    )

    checkpoint = _design_attempt_checkpoint(run_id="run-1", generation=5)
    with pytest.raises(ApplicationError) as exc_info:
        act.persist_design_attempt_checkpoint(checkpoint)

    assert exc_info.value.non_retryable is False
    assert persisted == []  # the write never happened despite a legitimately fresh generation


def test_delete_design_attempt_checkpoint_clears_the_field(monkeypatch):
    """A valid delete clears the same field persist_design_attempt_checkpoint
    writes, by setting it to None (read-equivalent to deletion, per
    load_design_attempt_checkpoint's own falsy check on read)."""
    from investment_team.strategy_lab import orchestrator_api, run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 3)

    captured = {}
    monkeypatch.setattr(
        orchestrator_api,
        "_persist_run_state",
        lambda run_id, state, *, create=False: captured.update(
            run_id=run_id, state=state, create=create
        ),
    )

    act.delete_design_attempt_checkpoint("run-1", "run-1-c0", 3)

    assert captured["run_id"] == "run-1"
    assert captured["create"] is False
    assert captured["state"] == {f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c0": None}


def test_delete_design_attempt_checkpoint_rejects_stale_generation(monkeypatch):
    """A stale generation raises non-retryable and never writes -- deleting
    under a superseded incarnation's generation would risk clobbering a
    newer incarnation's own checkpoint under the same field key."""
    from investment_team.strategy_lab import orchestrator_api, run_state

    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 2)

    persisted = []
    monkeypatch.setattr(
        orchestrator_api, "_persist_run_state", lambda *a, **k: persisted.append((a, k))
    )

    with pytest.raises(ApplicationError) as exc_info:
        act.delete_design_attempt_checkpoint("run-1", "run-1-c0", 1)

    assert exc_info.value.non_retryable is True
    assert exc_info.value.type == "StaleFencingTokenError"
    assert persisted == []


def test_delete_design_attempt_checkpoint_fails_closed_on_generation_lookup_failure(monkeypatch):
    """A transient durable-read failure inside the fencing check raises but
    stays RETRYABLE, mirroring persist_design_attempt_checkpoint's own
    fail-closed-but-retryable contract -- the caller decides whether to
    swallow it."""
    from investment_team.strategy_lab import orchestrator_api, run_state

    def _broken(run_id):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(run_state, "get_run_generation_strict", _broken)

    persisted = []
    monkeypatch.setattr(
        orchestrator_api, "_persist_run_state", lambda *a, **k: persisted.append((a, k))
    )

    with pytest.raises(ApplicationError) as exc_info:
        act.delete_design_attempt_checkpoint("run-1", "run-1-c0", 5)

    assert exc_info.value.non_retryable is False
    assert persisted == []


# ---------------------------------------------------------------------------
# _infer_cycle_scope_from_activity_context
# ---------------------------------------------------------------------------


def test_infer_cycle_scope_from_activity_context_recovers_workflow_id(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(act.activity, "info", lambda: SimpleNamespace(workflow_id="run-1-c0"))
    assert act._infer_cycle_scope_from_activity_context() == "run-1-c0"


def test_infer_cycle_scope_from_activity_context_returns_none_outside_activity_execution(
    monkeypatch,
):
    def _no_context():
        raise RuntimeError("Not in activity context")

    monkeypatch.setattr(act.activity, "info", _no_context)
    assert act._infer_cycle_scope_from_activity_context() is None


# ---------------------------------------------------------------------------
# load_design_attempt_checkpoint
# ---------------------------------------------------------------------------


def test_load_design_attempt_checkpoint_returns_valid_checkpoint(monkeypatch):
    from investment_team.strategy_lab import run_state

    checkpoint = _design_attempt_checkpoint(
        run_id="run-1", cycle_scope="run-1-c0", design_attempt=0, generation=2
    )
    monkeypatch.setattr(
        run_state,
        "load_run_from_job_service",
        lambda run_id: {
            f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c0": checkpoint.model_dump(
                mode="json"
            )
        },
    )
    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 2)

    result = act.load_design_attempt_checkpoint("run-1", "run-1-c0", 0)
    assert result == checkpoint


def test_load_design_attempt_checkpoint_returns_none_when_no_field_for_cycle_scope(monkeypatch):
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(
        run_state, "load_run_from_job_service", lambda run_id: {"status": "running"}
    )
    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 1)

    assert act.load_design_attempt_checkpoint("run-1", "run-1-c0", 0) is None


def test_load_design_attempt_checkpoint_returns_none_when_no_job_record(monkeypatch):
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(run_state, "load_run_from_job_service", lambda run_id: None)

    assert act.load_design_attempt_checkpoint("run-1", "run-1-c0", 0) is None


def test_load_design_attempt_checkpoint_returns_none_when_cycle_scope_unavailable(monkeypatch):
    """cycle_scope=None (recovery failed outside real Temporal context) must
    short-circuit before any durable read -- proves the no-op-safely contract."""
    from investment_team.strategy_lab import run_state

    def _fail_if_called(run_id):
        raise AssertionError(
            "load_run_from_job_service must not be called when cycle_scope is None"
        )

    monkeypatch.setattr(run_state, "load_run_from_job_service", _fail_if_called)

    assert act.load_design_attempt_checkpoint("run-1", None, 0) is None


def test_load_design_attempt_checkpoint_returns_none_for_wrong_design_attempt(monkeypatch):
    from investment_team.strategy_lab import run_state

    checkpoint = _design_attempt_checkpoint(
        run_id="run-1", cycle_scope="run-1-c0", design_attempt=1, generation=1
    )
    monkeypatch.setattr(
        run_state,
        "load_run_from_job_service",
        lambda run_id: {
            f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c0": checkpoint.model_dump(
                mode="json"
            )
        },
    )
    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 1)

    # Stored checkpoint is for design_attempt=1; caller is asking about 0.
    assert act.load_design_attempt_checkpoint("run-1", "run-1-c0", 0) is None


def test_load_design_attempt_checkpoint_returns_none_for_stale_generation(monkeypatch):
    from investment_team.strategy_lab import run_state

    checkpoint = _design_attempt_checkpoint(
        run_id="run-1", cycle_scope="run-1-c0", design_attempt=0, generation=1
    )
    monkeypatch.setattr(
        run_state,
        "load_run_from_job_service",
        lambda run_id: {
            f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c0": checkpoint.model_dump(
                mode="json"
            )
        },
    )
    # A restart minted generation 2 since this checkpoint was written under 1.
    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 2)

    assert act.load_design_attempt_checkpoint("run-1", "run-1-c0", 0) is None


def test_load_design_attempt_checkpoint_fails_open_on_generation_lookup_failure(monkeypatch):
    """Unlike the write side's fail-closed contract, a read-side generation
    lookup failure returns None (proceed as if no checkpoint exists) rather
    than raising -- the worst case is one unnecessary Phase-1 re-run, not a
    correctness violation, since nothing has been mutated yet at this point."""
    from investment_team.strategy_lab import run_state

    checkpoint = _design_attempt_checkpoint(
        run_id="run-1", cycle_scope="run-1-c0", design_attempt=0, generation=1
    )
    monkeypatch.setattr(
        run_state,
        "load_run_from_job_service",
        lambda run_id: {
            f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c0": checkpoint.model_dump(
                mode="json"
            )
        },
    )

    def _broken(run_id):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(run_state, "get_run_generation_strict", _broken)

    assert act.load_design_attempt_checkpoint("run-1", "run-1-c0", 0) is None


def test_load_design_attempt_checkpoint_fails_open_on_durable_read_failure(monkeypatch):
    from investment_team.strategy_lab import run_state

    def _broken(run_id):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(run_state, "load_run_from_job_service", _broken)

    assert act.load_design_attempt_checkpoint("run-1", "run-1-c0", 0) is None


def test_load_design_attempt_checkpoint_returns_none_for_malformed_payload(monkeypatch):
    from investment_team.strategy_lab import run_state

    monkeypatch.setattr(
        run_state,
        "load_run_from_job_service",
        lambda run_id: {
            f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-1-c0": {"not": "a checkpoint"}
        },
    )
    monkeypatch.setattr(run_state, "get_run_generation_strict", lambda run_id: 1)

    assert act.load_design_attempt_checkpoint("run-1", "run-1-c0", 0) is None


# ---------------------------------------------------------------------------
# _design_context_to_wire / _design_context_from_wire
# ---------------------------------------------------------------------------


def test_design_context_to_wire_returns_none_for_none_input():
    assert act._design_context_to_wire(None) is None


def test_design_context_from_wire_returns_none_for_none_or_empty_input():
    assert act._design_context_from_wire(None) is None
    assert act._design_context_from_wire({}) is None


def test_design_context_from_wire_rejects_partial_payload():
    with pytest.raises(ValueError, match="design_context"):
        act._design_context_from_wire({"rounds": 2})


def test_design_context_from_wire_rejects_wrong_field_types():
    with pytest.raises((TypeError, ValueError)):
        act._design_context_from_wire(
            {
                "rounds": 2,
                "critiques": {},
                "stop_reason": "ready",
                "loop_telemetry": {},
            }
        )


def test_design_context_round_trips_through_wire_helpers():
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.agents.design_review import SpecCritique

    design_context = _DesignPersistContext(
        rounds=2,
        critiques=[SpecCritique(ready=False, rationale="needs work")],
        stop_reason="round_cap",
        loop_telemetry={"k": 1},
    )
    wire = act._design_context_to_wire(design_context)
    restored = act._design_context_from_wire(wire)

    assert restored.rounds == 2
    assert restored.stop_reason == "round_cap"
    assert restored.loop_telemetry == {"k": 1}
    assert len(restored.critiques) == 1
    assert restored.critiques[0].model_dump() == design_context.critiques[0].model_dump()


def test_snapshot_prior_records_activity_delegates_to_orchestrator_api(monkeypatch):
    from investment_team.models import StrategyLabRecord
    from investment_team.strategy_lab import orchestrator_api

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
    monkeypatch.setattr(
        orchestrator_api, "_snapshot_prior_records", lambda *, reverse=False: [record]
    )

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


def test_run_design_attempt_activity_treats_null_budget_calls_as_zero(monkeypatch):
    from investment_team.strategy_lab.agents._llm_budget import active_budget
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured["budget_calls_seen"] = active_budget().calls_made
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(_run_design_attempt_params(budget_calls=None))

    assert captured["budget_calls_seen"] == 0
    assert out["budget_calls"] == 0


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
    assert out["pipeline_checkpoints"] == []


def test_run_design_attempt_activity_serializes_shared_pipeline_capture(monkeypatch):
    """Temporal supplies its real run/cycle/generation identity to the same
    shared capture helper and returns the inert checkpoint payload unchanged."""

    from investment_team.models import StrategySpec
    from investment_team.strategy_lab._orchestrator_helpers import (
        _DesignPersistContext,
        _DriftCollector,
    )
    from investment_team.strategy_lab.checkpoints import DesignCheckpoint
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    spec = StrategySpec.parse_persisted(_spec_dict())

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        self._capture_pipeline_checkpoint(
            DesignCheckpoint,
            capture=kwargs["checkpoint_capture"],
            design_attempt=kwargs["design_attempt"],
            spec=spec,
            code="",
            rationale="because",
            design_context=_DesignPersistContext(),
            all_gate_results=kwargs["cumulative_gate_results"],
            drift_collector=_DriftCollector(),
        )
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act, "load_design_attempt_checkpoint", lambda run_id, cycle_scope, design_attempt: None
    )

    out = act.run_design_attempt_activity(
        _run_design_attempt_params(run_id="run-1", generation=3, design_attempt=2)
    )

    assert len(out["pipeline_checkpoints"]) == 1
    checkpoint = out["pipeline_checkpoints"][0]
    assert checkpoint["stage"] == "design"
    assert checkpoint["run_id"] == "run-1"
    assert checkpoint["cycle_scope"] == "run-1-c0"
    assert checkpoint["generation"] == 3
    assert checkpoint["design_attempt"] == 2


_BATCH_CACHE_ENV_VAR = "STRATEGY_LAB_BATCH_INDICATOR_CACHE_ENABLED"


def _run_attempt_capturing_bound_cache(monkeypatch, **param_overrides):
    """Run the activity with ``_run_design_attempt`` monkeypatched to record the
    batch cache bound at the time it runs; return that recorded value."""
    from investment_team.strategy_lab.batch_cache_context import active_batch_indicator_cache
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured["bound_cache"] = active_batch_indicator_cache()
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    act.run_design_attempt_activity(_run_design_attempt_params(**param_overrides))
    return captured["bound_cache"]


def test_run_design_attempt_activity_binds_shared_cache_when_flag_on(monkeypatch):
    """Flag on + a batch_cache_key ⇒ the attempt runs with the batch's shared
    BatchIndicatorCache bound, and it is the same instance
    ``get_or_create_batch_cache`` hands out for that key."""
    from investment_team.strategy_lab import batch_cache_context as bcc

    monkeypatch.setenv(_BATCH_CACHE_ENV_VAR, "true")
    bcc._caches.clear()

    bound = _run_attempt_capturing_bound_cache(monkeypatch, batch_cache_key="run-1-b0")
    assert bound is not None
    assert bound is bcc.get_or_create_batch_cache("run-1-b0")
    bcc._caches.clear()


def test_run_design_attempt_activity_no_cache_when_flag_off(monkeypatch):
    """Flag off ⇒ nothing is bound and the process registry stays empty even when
    a key is supplied (behavior unchanged)."""
    from investment_team.strategy_lab import batch_cache_context as bcc

    monkeypatch.setenv(_BATCH_CACHE_ENV_VAR, "false")
    bcc._caches.clear()

    bound = _run_attempt_capturing_bound_cache(monkeypatch, batch_cache_key="run-1-b0")
    assert bound is None
    assert "run-1-b0" not in bcc._caches


def test_run_design_attempt_activity_no_cache_when_key_absent(monkeypatch):
    """Flag on but no batch_cache_key (old-shaped params) ⇒ nothing bound, no crash."""
    monkeypatch.setenv(_BATCH_CACHE_ENV_VAR, "true")
    bound = _run_attempt_capturing_bound_cache(monkeypatch)
    assert bound is None


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
    # SpecImplementabilityError.spec_implicated's own default (exceptions.py)
    # -- the reentry outcome must mirror it so the calling workflow's
    # cross-attempt resume gate sees the same value.
    assert out["spec_implicated"] is True


def test_run_design_attempt_activity_reentry_outcome_reports_spec_implicated_false(monkeypatch):
    """A raise site that has proven its failure doesn't implicate the
    checkpointed spec (``spec_implicated=False`` -- none does in production
    today) must have that declaration surface on the reentry outcome, since
    the calling workflow gates cross-attempt resume on it."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    last_spec = StrategySpec.parse_persisted(_spec_dict(strategy_id="strat-x"))

    def _fake_attempt(self, **kwargs):
        raise SpecImplementabilityError(
            "not spec-implicated",
            failure_phase="synthesis",
            last_spec=last_spec,
            last_code="",
            spec_implicated=False,
        )

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(_run_design_attempt_params())
    assert out["spec_implicated"] is False


def test_run_design_attempt_activity_maps_unexpected_error(monkeypatch):
    """Any non-control-flow exception maps to a non-retryable ApplicationError."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _fake_attempt(self, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    with pytest.raises(ApplicationError) as exc_info:
        act.run_design_attempt_activity(_run_design_attempt_params())
    assert exc_info.value.non_retryable is True


def test_run_design_attempt_activity_raises_cancelled_between_checkpoints(monkeypatch):
    """``activity.is_cancelled()`` flipping True at an ``emit`` checkpoint
    (the "between steps" cancellation check) raises Temporal's
    ``CancelledError`` and stops the attempt immediately — code past the
    checkpoint that observed cancellation never runs."""
    from temporalio.exceptions import CancelledError

    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    reached_past_checkpoint = False

    def _is_cancelled_after_first_call():
        calls = {"n": 0}

        def _check():
            calls["n"] += 1
            return calls["n"] > 1

        return _check

    monkeypatch.setattr(act, "is_cancelled", _is_cancelled_after_first_call())

    def _fake_attempt(self, **kwargs):
        nonlocal reached_past_checkpoint
        kwargs["emit"]("design", {"sub_phase": "round_1"})  # not cancelled yet
        kwargs["emit"]("design", {"sub_phase": "round_2"})  # cancellation observed here
        reached_past_checkpoint = True
        raise AssertionError("should not run past the cancelled checkpoint")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    with pytest.raises(CancelledError):
        act.run_design_attempt_activity(_run_design_attempt_params())
    assert reached_past_checkpoint is False


def test_run_design_attempt_activity_unaffected_when_never_cancelled(monkeypatch):
    """With ``is_cancelled()`` always False, ``no_thread_cancel_exception=True``
    and the new ``BackgroundHeartbeat`` wrapping don't change the ordinary
    (non-cancelled) outcome — regression coverage for those additions."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-uncancelled"}

    def _fake_attempt(self, **kwargs):
        kwargs["emit"]("design", {"sub_phase": "round_1"})
        kwargs["emit"]("coding", {"sub_phase": "completed"})
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(_run_design_attempt_params())
    assert out["kind"] == "record"
    assert out["record"]["lab_record_id"] == "rec-uncancelled"


def test_run_design_attempt_activity_stops_promptly_after_cancellation_mid_loop(monkeypatch):
    """A terminate/cancel landing well into a long attempt (not just at the
    very first checkpoint, as ``..._raises_cancelled_between_checkpoints``
    covers) must still stop the activity within a small, bounded number of
    checkpoints — not let it run anywhere close to completion.

    Simulates a long attempt as a loop of many ``emit`` checkpoints, each
    separated by a small real sleep (standing in for the many LLM calls the
    real pipeline makes over up to two hours). ``is_cancelled()`` flips True
    only after a handful of checkpoints, mimicking cancellation observed
    mid-run rather than instantly. The assertion is the exact checkpoint
    count at which cancellation was observed, not elapsed wall-clock time —
    under a loaded/parallel (xdist) CI runner the process can be descheduled
    for longer than any fixed wall-clock budget between two checkpoints even
    though cancellation still lands at exactly the expected one, so a real-time
    deadline here would be scheduler-dependent flakiness with no additional
    deterministic coverage.
    """
    import time

    from temporalio.exceptions import CancelledError

    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    TOTAL_CHECKPOINTS = 200  # would take TOTAL_CHECKPOINTS * _STEP_SLEEP_S if never cancelled
    CANCEL_AFTER = 5  # is_cancelled() starts returning True on call number CANCEL_AFTER + 1
    _STEP_SLEEP_S = 0.02

    calls = {"n": 0}

    def _is_cancelled_after_n():
        calls["n"] += 1
        return calls["n"] > CANCEL_AFTER

    monkeypatch.setattr(act, "is_cancelled", _is_cancelled_after_n)

    def _fake_attempt(self, **kwargs):
        emit = kwargs["emit"]
        for i in range(TOTAL_CHECKPOINTS):
            emit("design", {"sub_phase": f"round_{i}"})
            time.sleep(_STEP_SLEEP_S)
        raise AssertionError(
            "fake design attempt ran to completion despite cancellation — "
            "the checkpoint mechanism failed to stop it promptly"
        )

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    with pytest.raises(CancelledError):
        act.run_design_attempt_activity(_run_design_attempt_params())

    # Stopped at exactly the checkpoint where cancellation was first observed —
    # nowhere close to the full TOTAL_CHECKPOINTS loop.
    assert calls["n"] == CANCEL_AFTER + 1


def test_run_design_attempt_activity_publishes_mapped_progress_event(monkeypatch):
    """With run_id/cycle_index present, an ``emit("designing", ...)`` checkpoint
    best-effort publishes a `progress` SSE event whose phase is mapped onto the
    frontend's phase-stepper vocabulary and whose data is whitelisted to the
    fields ``StrategyLabProgressEvent`` actually models — unmodeled keys (e.g.
    ``unmapped_field`` here) are dropped, not forwarded."""
    from investment_team.api import job_event_bus
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    published: List[Any] = []
    monkeypatch.setattr(
        job_event_bus,
        "publish",
        lambda run_id, event, event_type=None: published.append((run_id, event, event_type)),
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        kwargs["emit"]("designing", {"sub_phase": "started", "unmapped_field": "should be dropped"})
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1", cycle_index=3))

    assert len(published) == 1
    run_id, event, event_type = published[0]
    assert run_id == "run-1"
    assert event_type == "progress"
    assert event == {
        "type": "progress",
        "cycle_index": 3,
        "phase": "ideating",  # "designing" mapped onto the phase-stepper's "ideating" step
        "sub_phase": "started",
    }


def test_run_design_attempt_activity_progress_checkpoint_maps_every_known_phase(monkeypatch):
    """Every internal phase name in ``_PROGRESS_PHASE_MAP`` publishes under its
    mapped frontend phase-stepper id."""
    from investment_team.api import job_event_bus
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    published: List[Any] = []
    monkeypatch.setattr(
        job_event_bus,
        "publish",
        lambda run_id, event, event_type=None: published.append(event),
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        emit = kwargs["emit"]
        for phase in act._PROGRESS_PHASE_MAP:
            emit(phase, {})
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1", cycle_index=0))

    assert [event["phase"] for event in published] == [
        act._PROGRESS_PHASE_MAP[phase] for phase in act._PROGRESS_PHASE_MAP
    ]


def test_run_design_attempt_activity_progress_checkpoint_drops_unmapped_phase(monkeypatch):
    """A phase not in ``_PROGRESS_PHASE_MAP`` (e.g. the internal-only
    ``telemetry``/``phase_transition`` diagnostics) is never published — no SSE
    payload at all, not a fallback/default phase."""
    from investment_team.api import job_event_bus
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    published: List[Any] = []
    monkeypatch.setattr(
        job_event_bus, "publish", lambda run_id, event, event_type=None: published.append(event)
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        emit = kwargs["emit"]
        emit("telemetry", {"scope": "design_loop"})
        emit("phase_transition", {"from_phase": "design", "to_phase": "design_review"})
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1", cycle_index=0))

    assert published == []


def test_run_design_attempt_activity_progress_checkpoint_disabled_without_cycle_index(
    monkeypatch,
):
    """``run_id`` present but ``cycle_index`` absent (a params dict from a
    workflow-history replay predating that field) ⇒ no progress publish at
    all, since ``StrategyLabProgressEvent.cycle_index`` is required on the
    frontend and a malformed event is worse than none."""
    from investment_team.api import job_event_bus
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    published: List[Any] = []
    monkeypatch.setattr(
        job_event_bus, "publish", lambda run_id, event, event_type=None: published.append(event)
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        kwargs["emit"]("designing", {"sub_phase": "started"})
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))

    assert published == []


def test_run_design_attempt_activity_progress_checkpoint_swallows_publish_failure(monkeypatch):
    """A ``job_event_bus.publish`` failure is logged and swallowed — a lost
    live-progress update must never fail the design attempt."""
    from investment_team.api import job_event_bus
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _boom(run_id, event, event_type=None):
        raise RuntimeError("event bus exploded")

    monkeypatch.setattr(job_event_bus, "publish", _boom)

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        kwargs["emit"]("designing", {"sub_phase": "started"})
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1", cycle_index=0))
    assert out["kind"] == "record"


def test_run_design_attempt_activity_progress_checkpoint_still_raises_cancelled_on_publish_failure(
    monkeypatch,
):
    """Cancellation is checked BEFORE any publish attempt, so a publish
    failure can never mask a real cancellation."""
    from temporalio.exceptions import CancelledError

    from investment_team.api import job_event_bus
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    monkeypatch.setattr(act, "is_cancelled", lambda: True)

    def _boom(run_id, event, event_type=None):
        raise AssertionError("publish should never be reached once cancelled")

    monkeypatch.setattr(job_event_bus, "publish", _boom)

    def _fake_attempt(self, **kwargs):
        kwargs["emit"]("designing", {"sub_phase": "started"})
        raise AssertionError("should not run past the cancelled checkpoint")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    with pytest.raises(CancelledError):
        act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1", cycle_index=0))


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
# run_design_attempt_activity — checkpoint resume (ADR-012)
# ---------------------------------------------------------------------------


def test_run_design_attempt_activity_without_run_id_never_checks_for_a_checkpoint(monkeypatch):
    """No run_id in params (today's baseline) must behave byte-for-byte
    unchanged: checkpointing is disabled entirely, no lookup happens, and no
    resume kwargs are passed."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    captured: Dict[str, Any] = {}

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        return _FakeRecord()

    def _fail_if_called(run_id, cycle_scope, design_attempt):
        raise AssertionError("load_design_attempt_checkpoint must not be called without run_id")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "load_design_attempt_checkpoint", _fail_if_called)

    act.run_design_attempt_activity(_run_design_attempt_params())

    assert captured["resume_spec"] is None
    assert captured["resume_rationale"] is None
    assert captured["resume_design_context"] is None


def test_run_design_attempt_activity_write_hook_is_a_no_op_without_run_id(monkeypatch):
    """Without run_id, checkpoint_hook is still passed to _run_design_attempt
    (it fires unconditionally at the design/synthesis boundary), but invoking
    it must no-op rather than attempt a write -- checkpointing is disabled
    end to end, not just on the read side."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    spec = StrategySpec.parse_persisted(_spec_dict())
    design_context = _DesignPersistContext(
        rounds=1, critiques=[], stop_reason="ready", loop_telemetry={}
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        kwargs["checkpoint_hook"](
            "design_synthesis_boundary",
            {"spec": spec, "rationale": "because", "design_context": design_context},
        )
        return _FakeRecord()

    persisted = []
    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(
        act, "persist_design_attempt_checkpoint", lambda checkpoint: persisted.append(checkpoint)
    )

    out = act.run_design_attempt_activity(_run_design_attempt_params())

    assert out["kind"] == "record"
    assert persisted == []


def test_run_design_attempt_activity_resumes_from_valid_checkpoint(monkeypatch):
    """A valid checkpoint's spec/rationale/design_context/drift/gate-results/
    budget strictly dominate the params-seeded pre-attempt state -- the
    concrete 'checkpoint wins' assertion."""
    from investment_team.strategy_lab.agents._llm_budget import active_budget
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    checkpoint = _design_attempt_checkpoint(
        run_id="run-1",
        cycle_scope="run-1-c0",
        design_attempt=0,
        generation=1,
        budget_calls=99,
        gate_results=[
            {
                "gate_name": "checkpointed_gate",
                "passed": True,
                "phase": "design",
                "severity": "info",
                "details": "ok",
            }
        ],
        spec_history=[
            {
                "phase": "design",
                "agent": "DesignAgent",
                "timestamp": "2023-01-01T00:00:00Z",
                "before_hash": "a" * 64,
                "after_hash": "b" * 64,
                "diff": "- old\n+ new",
                "reason": "checkpointed revision",
            }
        ],
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        captured["budget_calls_seen"] = active_budget().calls_made
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act,
        "load_design_attempt_checkpoint",
        lambda run_id, cycle_scope, design_attempt: checkpoint,
    )

    out = act.run_design_attempt_activity(
        _run_design_attempt_params(run_id="run-1", generation=1, budget_calls=7, gate_results=[])
    )

    assert captured["resume_spec"] == checkpoint.spec
    assert captured["resume_rationale"] == checkpoint.rationale
    assert captured["resume_design_context"].rounds == checkpoint.design_context["rounds"]
    # Checkpoint's budget (99), not params' pre-attempt budget_calls (7).
    assert captured["budget_calls_seen"] == 99
    assert out["budget_calls"] == 99
    # Checkpoint's gate results, not params' empty list.
    assert [g["gate_name"] for g in out["gate_results"]] == ["checkpointed_gate"]
    # Checkpoint's drift, not params' empty drift.
    assert len(captured["drift_collector"].spec_history) == 1
    assert captured["drift_collector"].spec_history[0].reason == "checkpointed revision"


# ---------------------------------------------------------------------------
# run_design_attempt_activity — cross-attempt resume (Temporal-mode parity
# with thread mode's gated cross-attempt resume). Distinct from the ADR-012
# same-attempt checkpoint tests above: here ``run_id`` is absent (or the
# same-attempt lookup simply finds nothing), so the only source of resume
# state is the calling workflow's own ``resume_spec``/``resume_rationale``/
# ``resume_design_context`` params.
# ---------------------------------------------------------------------------


def test_run_design_attempt_activity_forwards_cross_attempt_resume_params(monkeypatch):
    """``resume_spec``/``resume_rationale``/``resume_design_context`` supplied
    directly in ``params`` (the workflow's cross-attempt resume state) are
    reconstructed into real ``StrategySpec``/``_DesignPersistContext``
    objects and forwarded into ``_run_design_attempt`` -- with no ``run_id``
    in params, this is the only source of resume state (no ADR-012
    same-attempt checkpoint lookup happens at all)."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(
        _run_design_attempt_params(
            resume_spec=_spec_dict(strategy_id="strat-resumed"),
            resume_rationale="carried forward from checkpoint",
            resume_design_context={
                "rounds": 2,
                "critiques": [],
                "stop_reason": "ready",
                "loop_telemetry": {},
            },
        )
    )

    assert out["kind"] == "record"
    assert captured["resume_spec"] == StrategySpec(**_spec_dict(strategy_id="strat-resumed"))
    assert captured["resume_rationale"] == "carried forward from checkpoint"
    assert captured["resume_design_context"].rounds == 2


def test_run_design_attempt_activity_without_cross_attempt_resume_params_forwards_none(
    monkeypatch,
):
    """Absent ``resume_spec``/``resume_rationale``/``resume_design_context``
    params (today's baseline, and every attempt when the calling workflow
    hasn't determined a usable checkpoint) -- full restart, unchanged."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    act.run_design_attempt_activity(_run_design_attempt_params())

    assert captured["resume_spec"] is None
    assert captured["resume_rationale"] is None
    assert captured["resume_design_context"] is None


def test_run_design_attempt_activity_cross_attempt_resume_fails_open_on_empty_design_context(
    monkeypatch,
):
    """A ``resume_spec`` present without a valid ``resume_design_context``
    (empty dict, or missing entirely) must never adopt ``resume_spec`` alone
    -- that would skip Phase 1 with a blank context and fail identically on
    every retry, the exact crash loop ADR-012's own guard exists to avoid.
    Fails open to "no resume" (full restart) instead."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(
        _run_design_attempt_params(
            resume_spec=_spec_dict(strategy_id="strat-resumed"),
            resume_rationale="carried forward from checkpoint",
            resume_design_context={},
        )
    )

    assert out["kind"] == "record"
    assert captured["resume_spec"] is None
    assert captured["resume_rationale"] is None
    assert captured["resume_design_context"] is None


def test_run_design_attempt_activity_cross_attempt_resume_fails_open_on_malformed_spec(
    monkeypatch,
):
    """A malformed ``resume_spec`` payload (fails ``StrategySpec`` validation)
    must never crash the activity outright -- fails open to "no resume"
    (full restart) instead, mirroring the ADR-012 same-attempt checkpoint's
    own fail-open contract."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(
        _run_design_attempt_params(
            resume_spec={"missing": "required fields"},
            resume_rationale="carried forward from checkpoint",
            resume_design_context={
                "rounds": 2,
                "critiques": [],
                "stop_reason": "ready",
                "loop_telemetry": {},
            },
        )
    )

    assert out["kind"] == "record"
    assert captured["resume_spec"] is None
    assert captured["resume_rationale"] is None
    assert captured["resume_design_context"] is None


def test_run_design_attempt_activity_strips_unused_cross_attempt_seed_from_record(
    monkeypatch,
):
    """The workflow speculatively seeds ``params["drift"]`` with a
    checkpoint's own spec/code/gate history before knowing whether this
    activity's cross-attempt reconstruction will succeed -- a shape check
    alone can't catch a deeper problem, like an invalid critique entry, that
    only surfaces during ``SpecCritique`` reconstruction here. When
    ``_cross_attempt_resume_from_params`` fails open for that reason, this
    attempt runs Phase 1 from scratch, and the persisted record must not
    carry the discarded checkpoint's history as if it were this attempt's
    own provenance -- ``record["spec_history"]`` (etc.) is stripped of the
    seed prefix before being returned.

    The returned ``"drift"`` wire dict is a *different* consumer
    (``StrategyLabCycleWorkflow.run``'s reentry-continuation bookkeeping,
    which always strips exactly the seed length it itself sent when folding
    a reentry's drift into its own parent commit log) and must NOT be
    stripped here -- it needs the seed prefix intact regardless of whether
    this attempt actually resumed from it."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    seeded_spec_entry = {
        "phase": "design",
        "agent": "DesignAgent",
        "timestamp": "2023-01-01T00:00:00Z",
        "before_hash": "a" * 64,
        "after_hash": "b" * 64,
        "diff": "- old\n+ new",
        "reason": "checkpointed revision",
    }
    new_spec_entry = {
        "phase": "design",
        "agent": "DesignAgent",
        "timestamp": "2023-01-02T00:00:00Z",
        "before_hash": "c" * 64,
        "after_hash": "d" * 64,
        "diff": "- a\n+ b",
        "reason": "this attempt's own revision",
    }

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {
                "lab_record_id": "rec-1",
                "spec_history": [seeded_spec_entry, new_spec_entry],
                "code_history": [],
                "gate_timeline": [],
            }

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)

    out = act.run_design_attempt_activity(
        _run_design_attempt_params(
            drift={
                "spec_history": [seeded_spec_entry],
                "code_history": [],
                "gate_timeline": [],
            },
            resume_spec=_spec_dict(strategy_id="strat-resumed"),
            resume_rationale="carried forward from checkpoint",
            # All four top-level keys present and correctly typed -- passes
            # shape validation -- but the one critique entry is missing its
            # required "ready" field, so SpecCritique reconstruction fails.
            resume_design_context={
                "rounds": 1,
                "critiques": [{"rationale": "missing ready field"}],
                "stop_reason": "x",
                "loop_telemetry": {},
            },
        )
    )

    assert out["kind"] == "record"
    assert captured["resume_spec"] is None
    assert captured["resume_rationale"] is None
    assert captured["resume_design_context"] is None
    # Only this attempt's own new revision survives in the persisted record.
    assert out["record"]["spec_history"] == [new_spec_entry]
    # The reentry-bookkeeping wire dict keeps the seed prefix intact -- it
    # reflects ``drift_collector`` itself, which the fake ``_run_design_attempt``
    # never mutates, so it still holds exactly the seed this test supplied
    # (round-tripped through ``SpecRevision``, which adds its own
    # ``gate_failures: []`` default not present in the hand-built dict above).
    assert len(out["drift"]["spec_history"]) == 1
    assert out["drift"]["spec_history"][0]["reason"] == seeded_spec_entry["reason"]


def test_run_design_attempt_activity_strips_discarded_seed_even_when_adr012_wins_on_retry(
    monkeypatch,
):
    """Simulates a crash-then-retry: on the (unobserved) first execution of
    this same design_attempt_index, cross-attempt reconstruction failed
    (params below carry a resume_design_context that will fail the same
    way), so Phase 1 ran from scratch -- but not before an ADR-012
    same-attempt checkpoint was written mid-attempt, capturing
    drift_collector's then-current state: the discarded seed's entry,
    followed by that scratch Phase 1's own new entry. On this retry, the
    ADR-012 checkpoint wins (it reflects strictly more recent state for
    this exact design_attempt_index) and supplies resume_spec -- but the
    persisted record must still have the discarded seed's provenance
    stripped, because the checkpoint's own drift snapshot carries it
    regardless of ADR-012 having since taken over resume_spec for an
    unrelated reason. Regression test for the case a shallower
    `cross_attempt_seed_unused = params.get("resume_spec") is not None and resume_spec is None`
    check misses entirely -- resume_spec is non-None here (from ADR-012),
    so that check alone would leave the discarded seed in place."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    seeded_spec_entry = {
        "phase": "design",
        "agent": "DesignAgent",
        "timestamp": "2023-01-01T00:00:00Z",
        "before_hash": "a" * 64,
        "after_hash": "b" * 64,
        "diff": "- old\n+ new",
        "reason": "checkpointed revision",
    }
    scratch_spec_entry = {
        "phase": "design",
        "agent": "DesignAgent",
        "timestamp": "2023-01-02T00:00:00Z",
        "before_hash": "c" * 64,
        "after_hash": "d" * 64,
        "diff": "- a\n+ b",
        "reason": "scratch Phase 1's own revision, captured into the ADR-012 checkpoint",
    }
    checkpoint = _design_attempt_checkpoint(
        run_id="run-1",
        cycle_scope="run-1-c0",
        design_attempt=1,
        generation=1,
        spec_history=[seeded_spec_entry, scratch_spec_entry],
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {
                "lab_record_id": "rec-1",
                "spec_history": [seeded_spec_entry, scratch_spec_entry],
                "code_history": [],
                "gate_timeline": [],
            }

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act,
        "load_design_attempt_checkpoint",
        lambda run_id, cycle_scope, design_attempt: checkpoint,
    )
    monkeypatch.setattr(act, "delete_design_attempt_checkpoint", lambda *a: None)

    out = act.run_design_attempt_activity(
        _run_design_attempt_params(
            run_id="run-1",
            generation=1,
            design_attempt=1,
            drift={
                "spec_history": [seeded_spec_entry],
                "code_history": [],
                "gate_timeline": [],
            },
            resume_spec=_spec_dict(strategy_id="strat-resumed"),
            resume_rationale="carried forward from checkpoint",
            # Fails SpecCritique reconstruction, same as the sibling test --
            # cross-attempt resume would not have adopted the seed on the
            # original (pre-crash) execution either.
            resume_design_context={
                "rounds": 1,
                "critiques": [{"rationale": "missing ready field"}],
                "stop_reason": "x",
                "loop_telemetry": {},
            },
        )
    )

    assert out["kind"] == "record"
    # ADR-012 legitimately wins for resume_spec -- it's strictly more recent.
    assert captured["resume_spec"] == checkpoint.spec
    # But the discarded cross-attempt seed is still stripped from the
    # persisted record, since ADR-012's own drift snapshot carries it.
    assert out["record"]["spec_history"] == [scratch_spec_entry]


def test_run_design_attempt_activity_malformed_checkpoint_gate_results_falls_back_to_scratch(
    monkeypatch,
):
    """A checkpoint that passes DesignAttemptCheckpoint validation (gate_results
    is typed loosely as List[Dict[str, Any]]) but whose gate_results entries
    don't reconstruct into real QualityGateResult objects (e.g. missing the
    required gate_name field) must be treated the same as no checkpoint found
    -- never raise and never adopt a partial mix of checkpoint/params state."""
    from investment_team.strategy_lab.agents._llm_budget import active_budget
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    checkpoint = _design_attempt_checkpoint(
        run_id="run-1",
        cycle_scope="run-1-c0",
        design_attempt=0,
        generation=1,
        budget_calls=99,
        gate_results=[
            {
                # Missing required "gate_name" -- QualityGateResult.model_validate
                # raises reconstructing this, well after DesignAttemptCheckpoint's
                # own (loosely-typed) validation already accepted it.
                "passed": True,
                "phase": "design",
                "severity": "info",
                "details": "malformed",
            }
        ],
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        captured["budget_calls_seen"] = active_budget().calls_made
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act,
        "load_design_attempt_checkpoint",
        lambda run_id, cycle_scope, design_attempt: checkpoint,
    )

    out = act.run_design_attempt_activity(
        _run_design_attempt_params(run_id="run-1", generation=1, budget_calls=7, gate_results=[])
    )

    # Falls through to a normal Phase 1 re-run: no resume kwargs, params'
    # pre-attempt budget (7), not the unusable checkpoint's (99).
    assert captured["resume_spec"] is None
    assert captured["resume_rationale"] is None
    assert captured["resume_design_context"] is None
    assert captured["budget_calls_seen"] == 7
    assert out["budget_calls"] == 7
    assert out["gate_results"] == []
    assert len(captured["drift_collector"].spec_history) == 0


def test_run_design_attempt_activity_empty_checkpoint_design_context_falls_back_to_scratch(
    monkeypatch,
):
    """A checkpoint whose design_context is {} still passes DesignAttemptCheckpoint
    validation (the field is Dict[str, Any]) and _design_context_from_wire
    returns None without raising. Resume must treat that as reconstruction
    failure and re-run Phase 1 rather than skip it with a missing context."""
    from investment_team.strategy_lab.agents._llm_budget import active_budget
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    checkpoint = _design_attempt_checkpoint(
        run_id="run-1",
        cycle_scope="run-1-c0",
        design_attempt=0,
        generation=1,
        budget_calls=99,
        design_context={},
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        captured["budget_calls_seen"] = active_budget().calls_made
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act,
        "load_design_attempt_checkpoint",
        lambda run_id, cycle_scope, design_attempt: checkpoint,
    )

    out = act.run_design_attempt_activity(
        _run_design_attempt_params(run_id="run-1", generation=1, budget_calls=7, gate_results=[])
    )

    assert captured["resume_spec"] is None
    assert captured["resume_rationale"] is None
    assert captured["resume_design_context"] is None
    assert captured["budget_calls_seen"] == 7
    assert out["budget_calls"] == 7


def test_run_design_attempt_activity_partial_checkpoint_design_context_falls_back_to_scratch(
    monkeypatch,
):
    """A nonempty but incomplete design_context (e.g. only rounds) still
    passes DesignAttemptCheckpoint validation. Resume must fail open and
    re-run Phase 1 rather than skip it with defaulted critiques/telemetry."""
    from investment_team.strategy_lab.agents._llm_budget import active_budget
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    checkpoint = _design_attempt_checkpoint(
        run_id="run-1",
        cycle_scope="run-1-c0",
        design_attempt=0,
        generation=1,
        budget_calls=99,
        design_context={"rounds": 2},
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        captured["budget_calls_seen"] = active_budget().calls_made
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act,
        "load_design_attempt_checkpoint",
        lambda run_id, cycle_scope, design_attempt: checkpoint,
    )

    out = act.run_design_attempt_activity(
        _run_design_attempt_params(run_id="run-1", generation=1, budget_calls=7, gate_results=[])
    )

    assert captured["resume_spec"] is None
    assert captured["resume_rationale"] is None
    assert captured["resume_design_context"] is None
    assert captured["budget_calls_seen"] == 7
    assert out["budget_calls"] == 7


def test_run_design_attempt_activity_no_valid_checkpoint_runs_from_scratch(monkeypatch):
    """run_id present but no valid checkpoint found (e.g. first-ever attempt)
    -- resume kwargs are all None, matching the no-checkpoint case."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    captured: Dict[str, Any] = {}

    def _fake_attempt(self, **kwargs):
        captured.update(kwargs)
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act, "load_design_attempt_checkpoint", lambda run_id, cycle_scope, design_attempt: None
    )

    act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1", generation=1))

    assert captured["resume_spec"] is None
    assert captured["resume_rationale"] is None
    assert captured["resume_design_context"] is None


def test_run_design_attempt_activity_write_hook_persists_checkpoint(monkeypatch):
    """The checkpoint_hook threaded into _run_design_attempt, when invoked
    (simulating Phase 1 converging), must call persist_design_attempt_checkpoint
    with a correctly-populated DesignAttemptCheckpoint."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    spec = StrategySpec.parse_persisted(_spec_dict())
    design_context = _DesignPersistContext(
        rounds=1, critiques=[], stop_reason="ready", loop_telemetry={}
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        kwargs["checkpoint_hook"](
            "design_synthesis_boundary",
            {"spec": spec, "rationale": "because", "design_context": design_context},
        )
        return _FakeRecord()

    persisted = []
    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act, "load_design_attempt_checkpoint", lambda run_id, cycle_scope, design_attempt: None
    )
    monkeypatch.setattr(
        act, "persist_design_attempt_checkpoint", lambda checkpoint: persisted.append(checkpoint)
    )

    act.run_design_attempt_activity(
        _run_design_attempt_params(run_id="run-1", generation=3, design_attempt=0)
    )

    assert len(persisted) == 1
    written = persisted[0]
    assert written.run_id == "run-1"
    assert written.cycle_scope == "run-1-c0"
    assert written.design_attempt == 0
    assert written.generation == 3
    assert written.spec == spec
    assert written.rationale == "because"
    assert written.design_context["rounds"] == 1


def test_run_design_attempt_activity_write_hook_non_retryable_failure_propagates(monkeypatch):
    """A stale-fencing (non-retryable) checkpoint-write failure kills the
    whole activity -- this execution belongs to a superseded incarnation."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    spec = StrategySpec.parse_persisted(_spec_dict())
    design_context = _DesignPersistContext(
        rounds=1, critiques=[], stop_reason="ready", loop_telemetry={}
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        kwargs["checkpoint_hook"](
            "design_synthesis_boundary",
            {"spec": spec, "rationale": "because", "design_context": design_context},
        )
        return _FakeRecord()

    def _raise_stale(checkpoint):
        raise ApplicationError("stale", type="StaleFencingTokenError", non_retryable=True)

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act, "load_design_attempt_checkpoint", lambda run_id, cycle_scope, design_attempt: None
    )
    monkeypatch.setattr(act, "persist_design_attempt_checkpoint", _raise_stale)

    with pytest.raises(ApplicationError) as exc_info:
        act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))
    assert exc_info.value.non_retryable is True


def test_run_design_attempt_activity_write_hook_retryable_failure_is_swallowed(monkeypatch):
    """A transient (retryable) checkpoint-write failure is logged and
    swallowed -- Phase 1's real LLM work already happened, so the activity
    still returns its normal outcome instead of burning a Temporal retry
    purely to recover a checkpoint write."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    spec = StrategySpec.parse_persisted(_spec_dict())
    design_context = _DesignPersistContext(
        rounds=1, critiques=[], stop_reason="ready", loop_telemetry={}
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        kwargs["checkpoint_hook"](
            "design_synthesis_boundary",
            {"spec": spec, "rationale": "because", "design_context": design_context},
        )
        return _FakeRecord()

    def _raise_retryable(checkpoint):
        raise ApplicationError("blip", type="ConnectionError", non_retryable=False)

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act, "load_design_attempt_checkpoint", lambda run_id, cycle_scope, design_attempt: None
    )
    monkeypatch.setattr(act, "persist_design_attempt_checkpoint", _raise_retryable)

    out = act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))
    assert out["kind"] == "record"
    assert out["record"]["lab_record_id"] == "rec-1"


def test_run_design_attempt_activity_write_hook_raw_exception_is_swallowed(monkeypatch):
    """A raw (non-ApplicationError) checkpoint-write failure -- e.g. a
    job-service connection/HTTP error surfacing after _persist_run_state's
    own retries are exhausted -- must be treated exactly like a retryable
    ApplicationError: logged and swallowed, never propagated to discard the
    whole (already-completed) design attempt. Only the fencing pre-check
    raises ApplicationError; the write call itself can fail with an
    ordinary exception."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    spec = StrategySpec.parse_persisted(_spec_dict())
    design_context = _DesignPersistContext(
        rounds=1, critiques=[], stop_reason="ready", loop_telemetry={}
    )

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _fake_attempt(self, **kwargs):
        kwargs["checkpoint_hook"](
            "design_synthesis_boundary",
            {"spec": spec, "rationale": "because", "design_context": design_context},
        )
        return _FakeRecord()

    def _raise_connection_error(checkpoint):
        raise ConnectionError("job service unreachable")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-1-c0")
    monkeypatch.setattr(
        act, "load_design_attempt_checkpoint", lambda run_id, cycle_scope, design_attempt: None
    )
    monkeypatch.setattr(act, "persist_design_attempt_checkpoint", _raise_connection_error)

    out = act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))
    assert out["kind"] == "record"
    assert out["record"]["lab_record_id"] == "rec-1"


# ---------------------------------------------------------------------------
# Checkpoint cleanup on terminal outcome (ADR-012 §4)
# ---------------------------------------------------------------------------


def _enable_checkpointing(monkeypatch, cycle_scope: str = "run-1-c0") -> None:
    """Recover a fixed cycle_scope and make checkpoint resume a no-op, the
    same enablement pattern the checkpoint-write-hook tests above use, so a
    run_id-bearing params dict exercises the checkpoint_enabled path without
    touching the real job-service-backed load."""
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: cycle_scope)
    monkeypatch.setattr(
        act, "load_design_attempt_checkpoint", lambda run_id, cycle_scope, design_attempt: None
    )


def test_run_design_attempt_activity_deletes_checkpoint_on_record_outcome(monkeypatch):
    """A terminal 'record' outcome deletes this attempt's checkpoint before
    returning."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    monkeypatch.setattr(
        StrategyLabOrchestrator, "_run_design_attempt", lambda self, **kwargs: _FakeRecord()
    )
    _enable_checkpointing(monkeypatch)
    deleted = []
    monkeypatch.setattr(act, "delete_design_attempt_checkpoint", lambda *a: deleted.append(a))

    out = act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1", generation=3))

    assert out["kind"] == "record"
    assert deleted == [("run-1", "run-1-c0", 3)]


def test_run_design_attempt_activity_deletes_checkpoint_on_reentry_outcome(monkeypatch):
    """A design re-entry (SpecImplementabilityError) deletes this attempt's
    checkpoint -- the primary motivating case in ADR-012 §4: without this,
    a cycle with multiple re-entries accumulates one stale checkpoint per
    abandoned attempt."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    last_spec = StrategySpec.parse_persisted(_spec_dict(strategy_id="strat-x"))

    def _fake_attempt(self, **kwargs):
        raise SpecImplementabilityError(
            "risk limits loosened",
            failure_phase="evaluation",
            last_spec=last_spec,
            last_code="def x(): pass",
        )

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    _enable_checkpointing(monkeypatch)
    deleted = []
    monkeypatch.setattr(act, "delete_design_attempt_checkpoint", lambda *a: deleted.append(a))

    out = act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))

    assert out["kind"] == "reentry"
    assert deleted == [("run-1", "run-1-c0", 1)]


def test_run_design_attempt_activity_deletes_checkpoint_on_skipped_outcome_for_502(monkeypatch):
    """A 502 ('no market data') HTTPException deletes this attempt's
    checkpoint via the shared _skipped_outcome() helper."""
    from fastapi import HTTPException

    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _fake_attempt(self, **kwargs):
        raise HTTPException(status_code=502, detail="Failed to fetch historical market data.")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    _enable_checkpointing(monkeypatch)
    deleted = []
    monkeypatch.setattr(act, "delete_design_attempt_checkpoint", lambda *a: deleted.append(a))

    out = act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))

    assert out["kind"] == "skipped"
    assert deleted == [("run-1", "run-1-c0", 1)]


def test_run_design_attempt_activity_deletes_checkpoint_on_skipped_outcome_for_market_data_gate(
    monkeypatch,
):
    """A failed 'market_data' gate (the primary no-exception skip signal)
    deletes this attempt's checkpoint via the same _skipped_outcome() path."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-nodata"}

    class _FakeGate:
        gate_name = "market_data"
        passed = False

        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"gate_name": "market_data", "passed": False}

    def _fake_attempt(self, **kwargs):
        kwargs["cumulative_gate_results"].append(_FakeGate())
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    _enable_checkpointing(monkeypatch)
    deleted = []
    monkeypatch.setattr(act, "delete_design_attempt_checkpoint", lambda *a: deleted.append(a))

    out = act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))

    assert out["kind"] == "skipped"
    assert deleted == [("run-1", "run-1-c0", 1)]


def test_run_design_attempt_activity_deletes_checkpoint_on_non_retryable_error(monkeypatch):
    """A non-retryable mapped error deletes this attempt's checkpoint before
    re-raising -- the checkpoint is useless once no retry will ever consult
    it."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _fake_attempt(self, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    _enable_checkpointing(monkeypatch)
    deleted = []
    monkeypatch.setattr(act, "delete_design_attempt_checkpoint", lambda *a: deleted.append(a))

    with pytest.raises(ApplicationError) as exc_info:
        act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))

    assert exc_info.value.non_retryable is True
    assert deleted == [("run-1", "run-1-c0", 1)]


def test_run_design_attempt_activity_does_not_delete_checkpoint_on_retryable_error(monkeypatch):
    """A *retryable* mapped error (StrategyLabLLMError 'exhausted'/
    'budget_exhausted') must NOT delete the checkpoint -- Temporal will
    retry this same attempt, and the checkpoint is exactly what the retry
    needs to resume past Phase 1."""
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _fake_attempt(self, **kwargs):
        raise StrategyLabLLMError("timed out", outcome="exhausted")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    _enable_checkpointing(monkeypatch)
    deleted = []
    monkeypatch.setattr(act, "delete_design_attempt_checkpoint", lambda *a: deleted.append(a))

    with pytest.raises(ApplicationError) as exc_info:
        act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))

    assert exc_info.value.non_retryable is False
    assert deleted == []


def test_run_design_attempt_activity_does_not_delete_checkpoint_on_cancellation(monkeypatch):
    """Cancellation is not one of ADR-012 §4's four cleanup triggers -- the
    checkpoint must survive untouched so a subsequent retry can still
    resume from it."""
    from temporalio.exceptions import CancelledError

    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    def _is_cancelled_after_first_call():
        calls = {"n": 0}

        def _check():
            calls["n"] += 1
            return calls["n"] > 1

        return _check

    monkeypatch.setattr(act, "is_cancelled", _is_cancelled_after_first_call())

    def _fake_attempt(self, **kwargs):
        kwargs["emit"]("design", {"sub_phase": "round_1"})
        kwargs["emit"]("design", {"sub_phase": "round_2"})
        raise AssertionError("should not run past the cancelled checkpoint")

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _fake_attempt)
    _enable_checkpointing(monkeypatch)
    deleted = []
    monkeypatch.setattr(act, "delete_design_attempt_checkpoint", lambda *a: deleted.append(a))

    with pytest.raises(CancelledError):
        act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))

    assert deleted == []


def test_run_design_attempt_activity_never_deletes_checkpoint_without_run_id(monkeypatch):
    """checkpoint_enabled is False when run_id is absent (the default for
    callers predating ADR-012) -- cleanup must be a full no-op, never
    touching delete_design_attempt_checkpoint at all."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    monkeypatch.setattr(
        StrategyLabOrchestrator, "_run_design_attempt", lambda self, **kwargs: _FakeRecord()
    )
    deleted = []
    monkeypatch.setattr(act, "delete_design_attempt_checkpoint", lambda *a: deleted.append(a))

    out = act.run_design_attempt_activity(_run_design_attempt_params())

    assert out["kind"] == "record"
    assert deleted == []


def test_run_design_attempt_activity_checkpoint_delete_failure_is_swallowed(monkeypatch):
    """A delete failure must never turn an already-decided terminal outcome
    into an activity failure -- an orphaned checkpoint is inert clutter, not
    a correctness hazard (ADR-012's 'Best-effort cleanup' risk section).
    This is the test that most directly proves the acceptance criterion
    'no leaked checkpoint state after a successful run' can't come at the
    cost of losing a real result."""
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    class _FakeRecord:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"lab_record_id": "rec-1"}

    def _raise(*a):
        raise ConnectionError("job service unreachable")

    monkeypatch.setattr(
        StrategyLabOrchestrator, "_run_design_attempt", lambda self, **kwargs: _FakeRecord()
    )
    _enable_checkpointing(monkeypatch)
    monkeypatch.setattr(act, "delete_design_attempt_checkpoint", _raise)

    out = act.run_design_attempt_activity(_run_design_attempt_params(run_id="run-1"))

    assert out["kind"] == "record"
    assert out["record"]["lab_record_id"] == "rec-1"


# ---------------------------------------------------------------------------
# Batch-level activities (Stage 4)
# ---------------------------------------------------------------------------


def test_compute_signal_brief_activity_serializes_per_category_briefs(monkeypatch):
    """compute_signal_brief_activity serializes one brief per asset category
    and passes the storage metadata through unchanged."""
    from investment_team.api import main as api_main

    class _FakeBrief:
        def __init__(self, asset_class: str) -> None:
            self._asset_class = asset_class

        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"brief_version": "v1", "asset_class": self._asset_class}

    seen: Dict[str, Any] = {}

    def _snapshot(benchmark_symbol, exclude_asset_classes=None):
        seen["benchmark"] = benchmark_symbol
        seen["exclude"] = exclude_asset_classes
        return ({"stocks": _FakeBrief("stocks")}, {"stored": True})

    monkeypatch.setattr(api_main, "_compute_signal_brief_snapshot", _snapshot)
    out = act.compute_signal_brief_activity(
        {"benchmark_symbol": "SPY", "exclude_asset_classes": ["crypto"]}
    )
    assert out["signal_briefs"] == {"stocks": {"brief_version": "v1", "asset_class": "stocks"}}
    assert out["signal_brief_storage"] == {"stored": True}
    # The user's category restriction must reach the brief producer, so an
    # excluded category never gets a brief the design loop could read.
    assert seen == {"benchmark": "SPY", "exclude": ["crypto"]}


def test_compute_signal_brief_activity_handles_no_briefs(monkeypatch):
    """An empty brief map surfaces without failing, storage still returned."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_compute_signal_brief_snapshot",
        lambda benchmark_symbol, exclude_asset_classes=None: ({}, {"skipped": True}),
    )
    out = act.compute_signal_brief_activity({"benchmark_symbol": "SPY"})
    assert out["signal_briefs"] == {}
    assert out["signal_brief_storage"] == {"skipped": True}


def test_compute_signal_brief_activity_accepts_a_legacy_bare_symbol(monkeypatch):
    """A workflow-history replay predating the params dict passes a bare
    benchmark string; it must still resolve rather than raise."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_compute_signal_brief_snapshot",
        lambda benchmark_symbol, exclude_asset_classes=None: ({}, {"sym": benchmark_symbol}),
    )
    out = act.compute_signal_brief_activity("SPY")
    assert out["signal_brief_storage"] == {"sym": "SPY"}


def test_is_run_cancelled_activity_delegates(monkeypatch):
    """is_run_cancelled_activity delegates to the api_main helper and returns
    the helper's boolean result unchanged."""
    from investment_team.api import main as api_main

    seen = {}

    def _fake(run_id):
        seen["run_id"] = run_id
        return True

    monkeypatch.setattr(api_main, "_is_strategy_lab_run_externally_stopped", _fake)
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
    briefs, storage = api_main._compute_signal_brief_snapshot("SPY")
    assert briefs == {}
    assert storage == {"skipped": True, "skipped_reason": "signal_expert_disabled"}


def test_compute_signal_brief_snapshot_scopes_market_context_per_category(monkeypatch):
    """A stocks brief must never see the shared snapshot's FX rates or crypto
    headline -- rendering them would contradict the brief's own "covers
    stocks and nothing else" scope instruction and hand the model
    cross-category evidence it was told not to use. A category's own
    single-class field (crypto's headline) still reaches its own brief."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_signal_expert_enabled", lambda: True)
    monkeypatch.setattr(
        api_main,
        "_snapshot_prior_records",
        lambda: [_signal_brief_prior_record("stocks"), _signal_brief_prior_record("crypto")],
    )

    class _FakeProvider:
        def fetch_context(self, request):
            from investment_team.market_lab_data import MarketLabContext

            return MarketLabContext(
                fetched_at="2024-01-01T00:00:00Z",
                degraded=False,
                sources_used=["x"],
                fx_rates={"EUR": 1.08},
                crypto_snapshot="BTC=65000",
                macro_snippets=["DGS10=4.2%"],
            )

        def close(self):
            pass

    seen_prompts: Dict[str, str] = {}

    class _FakeExpert:
        def produce_signal_brief(self, prior_records, market_ctx, *, asset_class=None):
            from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1

            seen_prompts[asset_class] = market_ctx.as_prompt_text()
            return SignalIntelligenceBriefV1(brief_version=1)

    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _FakeProvider)
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _FakeExpert)

    api_main._compute_signal_brief_snapshot("SPY", ["forex", "futures", "commodities"])

    assert "FX" not in seen_prompts["stocks"]
    assert "Crypto" not in seen_prompts["stocks"]
    assert "DGS10" in seen_prompts["stocks"]
    assert "Crypto" in seen_prompts["crypto"]
    assert "FX" not in seen_prompts["crypto"]


def test_compute_signal_brief_snapshot_provenance_reflects_the_scoped_degraded_flag(monkeypatch):
    """brief_provenance's market_degraded must reflect the SCOPED context the
    expert actually received (category_market_ctx), not the unscoped
    aggregate market_ctx -- otherwise a forex-source failure would mark a
    stocks brief's provenance degraded even though scoped_to("stocks")
    already stripped that failure reason and the stocks context is not
    actually degraded from what the model saw."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_signal_expert_enabled", lambda: True)
    monkeypatch.setattr(
        api_main,
        "_snapshot_prior_records",
        lambda: [_signal_brief_prior_record("stocks")],
    )

    class _FakeProvider:
        def fetch_context(self, request):
            from investment_team.market_lab_data import MarketLabContext

            # Unscoped context is degraded solely by a forex-source failure --
            # scoped_to("stocks") should strip that reason and clear degraded.
            return MarketLabContext(
                fetched_at="2024-01-01T00:00:00Z",
                degraded=True,
                degraded_reason="frankfurter_failed",
                sources_used=["frankfurter"],
            )

        def close(self):
            pass

    class _FakeExpert:
        def produce_signal_brief(self, prior_records, market_ctx, *, asset_class=None):
            from investment_team.signal_intelligence_models import SignalIntelligenceBriefV1

            return SignalIntelligenceBriefV1(brief_version=1)

    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _FakeProvider)
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _FakeExpert)

    _briefs, storage = api_main._compute_signal_brief_snapshot(
        "SPY", ["crypto", "forex", "futures", "commodities"]
    )

    assert storage["by_asset_class"]["stocks"]["brief_provenance"]["market_degraded"] is False


def test_compute_signal_brief_snapshot_skips_categories_with_no_prior_records(monkeypatch):
    """A category with zero prior records has nothing for the expert to
    analyze -- calling it anyway would be a paid LLM round-trip for generic
    market commentary, up to len(allowed) times on a run's first batch. Must
    skip the expert call entirely and record a ``no_prior_records`` marker."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(api_main, "_strategy_lab_signal_expert_enabled", lambda: True)
    monkeypatch.setattr(api_main, "_snapshot_prior_records", lambda: [])

    class _FakeProvider:
        def fetch_context(self, request):
            from investment_team.market_lab_data import MarketLabContext

            return MarketLabContext(
                fetched_at="2024-01-01T00:00:00Z", degraded=False, sources_used=["x"]
            )

        def close(self):
            pass

    calls = {"n": 0}

    class _FakeExpert:
        def produce_signal_brief(self, prior_records, market_ctx, *, asset_class=None):
            calls["n"] += 1
            raise AssertionError("must not be called for an empty-evidence category")

    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _FakeProvider)
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _FakeExpert)

    briefs, storage = api_main._compute_signal_brief_snapshot("SPY", None)

    assert briefs == {}
    assert calls["n"] == 0
    from investment_team.strategy_lab_context import PROMPT_ASSET_CLASSES

    assert set(storage["by_asset_class"].keys()) == set(PROMPT_ASSET_CLASSES)
    for entry in storage["by_asset_class"].values():
        assert entry == {"skipped": True, "skipped_reason": "no_prior_records"}


def test_compute_signal_brief_snapshot_fails_open_on_provider_init_failure(monkeypatch):
    """Fail-open must cover FreeTierMarketDataProvider() construction itself,
    not just the body of expert.produce_signal_brief -- a provider that
    can't even be constructed (e.g. bad config) must not raise out of this
    function."""
    from investment_team.api import main as api_main

    def _boom_provider():
        raise RuntimeError("provider config invalid")

    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _boom_provider)

    briefs, storage = api_main._compute_signal_brief_snapshot("SPY")

    assert briefs == {}
    assert storage["skipped"] is True
    assert storage["skipped_reason"] == "provider_init_failed"
    assert "provider config invalid" in storage["error"]


def test_compute_signal_brief_snapshot_fails_open_on_expert_init_failure(monkeypatch):
    """Fail-open must cover SignalIntelligenceExpert() construction. Construction
    now happens per category (a fresh expert per category, not one reused
    across the loop -- see the isolation test below), so a construction
    failure is a per-category skip, not a whole-function abort."""
    from investment_team.api import main as api_main

    closed = []

    class _FakeProvider:
        def fetch_context(self, request):
            from investment_team.market_lab_data import MarketLabContext

            return MarketLabContext(
                fetched_at="2024-01-01T00:00:00Z", degraded=False, sources_used=["x"]
            )

        def close(self):
            closed.append(True)

    def _boom_expert():
        raise RuntimeError("expert init failed")

    monkeypatch.setattr(
        api_main, "_snapshot_prior_records", lambda *, reverse=False: [_signal_brief_prior_record("stocks")]
    )
    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _FakeProvider)
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _boom_expert)

    briefs, storage = api_main._compute_signal_brief_snapshot("SPY", ["crypto", "forex", "futures", "commodities"])

    assert briefs == {}
    assert storage["by_asset_class"]["stocks"] == {
        "skipped": True,
        "skipped_reason": "expert_failed",
        "error": "expert init failed",
    }
    # provider.close() still runs even though expert init failed.
    assert closed == [True]


def test_compute_signal_brief_snapshot_survives_provider_close_failure(monkeypatch):
    """A provider.close() failure in the finally block must not replace the
    tuple the try block already decided to return."""
    from investment_team.api import main as api_main

    class _FakeProvider:
        def fetch_context(self, request):
            from investment_team.market_lab_data import MarketLabContext

            return MarketLabContext(
                fetched_at="2024-01-01T00:00:00Z", degraded=False, sources_used=["x"]
            )

        def close(self):
            raise RuntimeError("close boom")

    def _boom_expert():
        raise RuntimeError("expert failed too")

    monkeypatch.setattr(
        api_main, "_snapshot_prior_records", lambda *, reverse=False: [_signal_brief_prior_record("stocks")]
    )
    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _FakeProvider)
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _boom_expert)

    # Must not raise despite close() also failing.
    briefs, storage = api_main._compute_signal_brief_snapshot("SPY", ["crypto", "forex", "futures", "commodities"])

    assert briefs == {}
    assert storage["by_asset_class"]["stocks"]["skipped"] is True
    assert storage["by_asset_class"]["stocks"]["skipped_reason"] == "expert_failed"


def test_compute_signal_brief_snapshot_success_survives_provider_close_failure(monkeypatch):
    """A provider.close() failure in the finally block must not replace an
    already-successful brief with an unhandled exception -- the guard covers
    the happy path, not just the fail-open branches."""
    from investment_team.api import main as api_main

    class _FakeProvider:
        def fetch_context(self, request):
            from investment_team.market_lab_data import MarketLabContext

            return MarketLabContext(
                fetched_at="2024-01-01T00:00:00Z", degraded=False, sources_used=["x"]
            )

        def close(self):
            raise RuntimeError("close boom")

    class _FakeBrief:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"brief_version": "v1"}

    class _FakeExpert:
        def produce_signal_brief(self, prior_records, market_ctx, *, asset_class=None):
            return _FakeBrief()

    # One prior record per allowed category -- a signal brief has nothing to
    # analyze (and is skipped entirely) for a category with zero records, so
    # this seeds the evidence the expert-call path under test actually needs.
    monkeypatch.setattr(
        api_main,
        "_snapshot_prior_records",
        lambda *, reverse=False: [
            _signal_brief_prior_record("stocks"),
            _signal_brief_prior_record("futures"),
            _signal_brief_prior_record("commodities"),
        ],
    )
    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _FakeProvider)
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _FakeExpert)

    # Must not raise despite close() failing, and must return the briefs the
    # try block already produced -- not a fail-open fallback tuple. Only the
    # allowed categories get one, so the restriction is visible in the result.
    briefs, storage = api_main._compute_signal_brief_snapshot("SPY", ["crypto", "forex"])

    assert sorted(briefs) == ["commodities", "futures", "stocks"]
    assert all(isinstance(b, _FakeBrief) for b in briefs.values())
    assert storage.get("skipped") is not True
    assert storage["by_asset_class"]["stocks"]["brief_version"] == "v1"


def _signal_brief_prior_record(asset_class: str):
    """A minimal, non-winning StrategyLabRecord for ``asset_class``, seeding
    prior-result data for ``_compute_signal_brief_snapshot`` tests. Carries a
    single backtest with placeholder metrics -- callers assert on category
    scoping, not on the numbers themselves."""
    from investment_team.models import (
        BacktestConfig,
        BacktestRecord,
        BacktestResult,
        StrategyLabRecord,
        StrategySpec,
    )

    strat = StrategySpec(
        strategy_id=f"strat-{asset_class}",
        authored_by="x",
        asset_class=asset_class,
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    result = BacktestResult(
        total_return_pct=1.0,
        annualized_return_pct=1.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=5.0,
        win_rate_pct=40.0,
        profit_factor=1.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    bt = BacktestRecord(
        backtest_id=f"bt-{asset_class}",
        strategy_id=strat.strategy_id,
        strategy=strat,
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-02-01"),
        submitted_by="x",
        submitted_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T01:00:00Z",
        result=result,
        trades=[],
    )
    return StrategyLabRecord(
        lab_record_id=f"lab-{asset_class}",
        strategy=strat,
        backtest=bt,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="n",
        created_at="2024-01-01T01:00:00Z",
    )


def _signal_brief_fake_provider():
    """A fake FreeTierMarketDataProvider class for signal-brief tests: yields
    a clean, non-degraded MarketLabContext and has a no-op close()."""
    from investment_team.market_lab_data import MarketLabContext

    class _FakeProvider:
        def fetch_context(self, request):
            return MarketLabContext(
                fetched_at="2024-01-01T00:00:00Z", degraded=False, sources_used=["x"]
            )

        def close(self):
            pass

    return _FakeProvider


def test_compute_signal_brief_snapshot_isolates_one_categorys_expert_failure(monkeypatch):
    """produce_signal_brief raising for one category must not cost sibling
    categories their brief -- this is the PR's own stated purpose for the
    per-category try/except, previously unexercised by any test (every
    existing fail-open test only covers construction-time failures, which
    abort the whole function before this per-category loop even starts)."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_snapshot_prior_records",
        lambda *, reverse=False: [
            _signal_brief_prior_record("stocks"),
            _signal_brief_prior_record("crypto"),
        ],
    )

    class _FakeBrief:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"brief_version": "v1"}

    class _FakeExpert:
        def produce_signal_brief(self, prior_records, market_ctx, *, asset_class=None):
            if asset_class == "stocks":
                raise RuntimeError("LLM call failed for stocks")
            return _FakeBrief()

    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _signal_brief_fake_provider())
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _FakeExpert)

    briefs, storage = api_main._compute_signal_brief_snapshot("SPY", ["forex", "futures", "commodities"])

    # The failing category is absent from briefs and carries a skip marker...
    assert "stocks" not in briefs
    assert storage["by_asset_class"]["stocks"] == {
        "skipped": True,
        "skipped_reason": "expert_failed",
        "error": "LLM call failed for stocks",
    }
    # ...while the sibling category's brief is entirely unaffected.
    assert "crypto" in briefs
    assert storage["by_asset_class"]["crypto"]["brief_version"] == "v1"


def test_compute_signal_brief_snapshot_constructs_a_fresh_expert_per_category(monkeypatch):
    """A single SignalIntelligenceExpert wraps a Strands Agent, which retains
    conversation history across calls -- reusing one instance across
    categories would let a stocks brief's prompt/response leak into crypto's
    call as prior conversation turns, silently reintroducing cross-category
    contamination despite the per-category record/market scoping. Must
    construct a distinct instance for every category."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_snapshot_prior_records",
        lambda *, reverse=False: [
            _signal_brief_prior_record("stocks"),
            _signal_brief_prior_record("crypto"),
            _signal_brief_prior_record("forex"),
        ],
    )

    constructed: List[Any] = []

    class _FakeBrief:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"brief_version": "v1"}

    class _FakeExpert:
        def __init__(self):
            constructed.append(self)

        def produce_signal_brief(self, prior_records, market_ctx, *, asset_class=None):
            return _FakeBrief()

    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _signal_brief_fake_provider())
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _FakeExpert)

    briefs, _storage = api_main._compute_signal_brief_snapshot("SPY", ["futures", "commodities"])

    assert sorted(briefs) == ["crypto", "forex", "stocks"]
    # One fresh instance per category -- never fewer (reused), never shared.
    assert len(constructed) == 3
    assert len({id(e) for e in constructed}) == 3


def test_compute_signal_brief_snapshot_isolates_a_scoped_to_failure(monkeypatch):
    """A failure computing the per-category market-snapshot hash (scoped_to /
    as_prompt_text, evaluated before produce_signal_brief) must be caught by
    the same per-category fail-open guard, not escape the function entirely
    and fail the whole batch's Temporal activity over one category."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main, "_snapshot_prior_records", lambda *, reverse=False: [_signal_brief_prior_record("stocks")]
    )

    class _FakeExpert:
        def produce_signal_brief(self, prior_records, market_ctx, *, asset_class=None):
            raise AssertionError("must not be reached -- scoped_to already failed")

    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _signal_brief_fake_provider())
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _FakeExpert)

    from investment_team.market_lab_data import MarketLabContext

    def _boom_scoped_to(self, asset_class):
        raise ValueError("scoped_to boom")

    monkeypatch.setattr(MarketLabContext, "scoped_to", _boom_scoped_to)

    briefs, storage = api_main._compute_signal_brief_snapshot("SPY", ["crypto", "forex", "futures", "commodities"])

    assert briefs == {}
    assert storage["by_asset_class"]["stocks"] == {
        "skipped": True,
        "skipped_reason": "expert_failed",
        "error": "scoped_to boom",
    }


def test_compute_signal_brief_snapshot_propagates_an_assertion_error(monkeypatch):
    """An AssertionError from the per-category expert call is a precondition
    violation in this function's own construction of its call -- per this
    codebase's Design by Contract rule, that must propagate rather than be
    silently folded into a routine "expert_failed" skip marker alongside
    genuine external/transient failures."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main, "_snapshot_prior_records", lambda *, reverse=False: [_signal_brief_prior_record("stocks")]
    )

    class _FakeExpert:
        def produce_signal_brief(self, prior_records, market_ctx, *, asset_class=None):
            raise AssertionError("asset_class must be a canonical PROMPT_ASSET_CLASSES member")

    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _signal_brief_fake_provider())
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _FakeExpert)

    with pytest.raises(AssertionError, match="canonical PROMPT_ASSET_CLASSES member"):
        api_main._compute_signal_brief_snapshot("SPY", ["crypto", "forex", "futures", "commodities"])


def test_compute_signal_brief_snapshot_excludes_an_alias_spelling(monkeypatch):
    """exclude_asset_classes must exclude an alias spelling (e.g. "equity")
    the same way it excludes the canonical label -- this only works because
    the allowed-set computation goes through the shared
    strategy_lab_context.allowed_asset_classes helper (with its
    normalize_asset_class_strict alias handling), not a hand-rolled
    ``set(exclude_asset_classes or ())`` that skips normalization entirely."""
    from investment_team.api import main as api_main

    monkeypatch.setattr(
        api_main,
        "_snapshot_prior_records",
        lambda *, reverse=False: [_signal_brief_prior_record("crypto")],
    )

    class _FakeBrief:
        def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
            return {"brief_version": "v1"}

    class _FakeExpert:
        def produce_signal_brief(self, prior_records, market_ctx, *, asset_class=None):
            assert asset_class != "stocks", "an aliased exclusion must still exclude stocks"
            return _FakeBrief()

    monkeypatch.setattr(api_main, "FreeTierMarketDataProvider", _signal_brief_fake_provider())
    monkeypatch.setattr(api_main, "SignalIntelligenceExpert", _FakeExpert)

    briefs, _storage = api_main._compute_signal_brief_snapshot(
        "SPY", ["equity", "forex", "futures", "commodities"]
    )

    assert "stocks" not in briefs


def test_is_strategy_lab_run_externally_stopped_reads_job_status(monkeypatch):
    """The broad check fires for any external stop signal -- cancelled,
    failed, or interrupted alike -- not just a genuine cancellation."""
    from investment_team.api import main as api_main

    class _FakeClient:
        def __init__(self, status):
            self._status = status

        def get_job(self, run_id):
            return {"status": self._status} if self._status is not None else None

    def _use(status):
        monkeypatch.setattr(api_main, "_get_lab_run_job_client", lambda: _FakeClient(status))

    _use("cancelled")
    assert api_main._is_strategy_lab_run_externally_stopped("r") is True
    _use("failed")
    assert api_main._is_strategy_lab_run_externally_stopped("r") is True
    _use("interrupted")
    assert api_main._is_strategy_lab_run_externally_stopped("r") is True
    _use("running")
    assert api_main._is_strategy_lab_run_externally_stopped("r") is False
    _use(None)  # no persisted job
    assert api_main._is_strategy_lab_run_externally_stopped("r") is False
    # completed is a terminal *success*, not an external stop.
    _use("completed")
    assert api_main._is_strategy_lab_run_externally_stopped("r") is False


def test_is_strategy_lab_run_externally_stopped_swallows_errors(monkeypatch):
    from investment_team.api import main as api_main

    def _boom():
        raise RuntimeError("job service down")

    monkeypatch.setattr(api_main, "_get_lab_run_job_client", _boom)
    assert api_main._is_strategy_lab_run_externally_stopped("r") is False


def test_is_strategy_lab_run_cancelled_is_precise(monkeypatch):
    """Unlike _is_strategy_lab_run_externally_stopped, this must return True
    ONLY for an exact "cancelled" status -- a failed or interrupted run is
    NOT a cancellation and must return False."""
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
    assert api_main._is_strategy_lab_run_cancelled("r") is False
    _use("interrupted")
    assert api_main._is_strategy_lab_run_cancelled("r") is False
    _use("running")
    assert api_main._is_strategy_lab_run_cancelled("r") is False
    _use(None)
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


def test_compute_signal_brief_activity_maps_malformed_dict_params():
    """A dict `params` missing the required `benchmark_symbol` key must raise
    ApplicationError (non-retryable), not a raw KeyError -- extracting the
    fields now runs inside the same try/except as the snapshot delegate
    call, so a malformed payload fails fast instead of being retried by
    Temporal as if it were a transient failure."""
    with pytest.raises(ApplicationError) as exc_info:
        act.compute_signal_brief_activity({"exclude_asset_classes": ["forex"]})
    assert exc_info.value.non_retryable is True


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


# ---------------------------------------------------------------------------
# publish_run_event_activity — the workflow-code side-effect boundary for the
# SSE run-event publishes StrategyLabBatchWorkflow.run makes directly.
# ---------------------------------------------------------------------------


def test_publish_run_event_activity_calls_job_event_bus_publish(monkeypatch):
    from investment_team.api import job_event_bus

    calls: List[Any] = []
    monkeypatch.setattr(
        job_event_bus,
        "publish",
        lambda run_id, event, event_type=None: calls.append((run_id, event, event_type)),
    )

    event = {"type": "cycle_complete", "cycle_index": 2, "completed_cycles": 3}
    result = act.publish_run_event_activity({"run_id": "run-1", "event": event})

    assert result is None
    assert calls == [("run-1", event, "cycle_complete")]


def test_publish_run_event_activity_swallows_publish_failure(monkeypatch):
    """A lost live-progress/results-refresh update must never fail or retry
    the underlying run — the activity always returns None, even on a
    ``job_event_bus.publish`` failure."""
    from investment_team.api import job_event_bus

    def _boom(run_id, event, event_type=None):
        raise RuntimeError("event bus exploded")

    monkeypatch.setattr(job_event_bus, "publish", _boom)

    result = act.publish_run_event_activity({"run_id": "run-1", "event": {"type": "complete"}})
    assert result is None


def test_activities_list_contains_every_activity():
    assert len(act.ACTIVITIES) == 12
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
    assert act.publish_run_event_activity in act.ACTIVITIES


def test_probe_only_resume_failure_logs_info_without_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A probe that fails to reconstruct has not lost anything.

    ADR-012 already supplied the resume state; the probe only re-derives whether
    the cross-attempt seed was ever adopted. Logging that at WARNING with a
    traceback reads as "this run just lost its resume", which is exactly what did
    not happen — and that misreading is the whole reason the branch exists, so it
    needs a test or a refactor could quietly put it back.
    """
    import logging

    from investment_team.strategy_lab.temporal.activities import (
        _cross_attempt_resume_from_params,
    )

    params = {"resume_spec": {"bogus": True}, "resume_design_context": None}

    with caplog.at_level(logging.INFO):
        result = _cross_attempt_resume_from_params(
            params, run_id="run-1", design_attempt_index=2, probe_only=True
        )

    assert result == (None, None, None)
    assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    info_records = [rec for rec in caplog.records if rec.levelno < logging.WARNING]
    assert any("ADR-012 resume is in effect" in rec.getMessage() for rec in info_records)
    # The level assertion alone does not pin the traceback: an implementation
    # that logged the same message at INFO with exc_info=True would still dump
    # the reconstruction failure's stack, which is half of the misreading this
    # branch exists to avoid.
    assert all(rec.exc_info is None for rec in info_records), (
        "probe-only resume failure must not carry a traceback"
    )


def test_non_probe_resume_failure_still_logs_warning_with_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other side of the same branch: a real cross-attempt resume that fails
    to reconstruct HAS lost its resume, so it keeps the warning and the
    traceback. Without this, the probe-path test alone would pass against an
    implementation that downgraded both."""
    import logging

    from investment_team.strategy_lab.temporal.activities import (
        _cross_attempt_resume_from_params,
    )

    params = {"resume_spec": {"bogus": True}, "resume_design_context": None}

    with caplog.at_level(logging.INFO):
        result = _cross_attempt_resume_from_params(params, run_id="run-1", design_attempt_index=2)

    assert result == (None, None, None)
    warnings = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert warnings
    assert any(rec.exc_info is not None for rec in warnings)
    # Level and traceback alone do not pin what the record SAYS: a message that
    # stopped naming the lost resume would still satisfy both, and naming it is
    # the whole point of keeping this branch a warning.
    assert any(
        "failed to reconstruct (treating as no resume)" in rec.getMessage() for rec in warnings
    ), "non-probe resume failure must still name the lost cross-attempt resume"

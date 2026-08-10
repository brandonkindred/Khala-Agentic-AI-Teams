"""Unit tests for the agentic_team_provisioning Temporal activities + workflow driver.

The activities run against the real ``AgenticTestStore`` over the dict-backed
``_fake_postgres`` fake (no live Postgres), and the LLM is stubbed by monkeypatching
``build_agent`` / ``call_agent`` on ``pipeline_runner`` (the module the activity reuses).
The workflow ``run`` is driven with ``workflow.execute_activity`` / ``wait_condition``
patched, so no Temporal server is needed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    manifest_agent_id,
)
from agent_team_studio.agentic_team_provisioning.temporal import workflows as wf
from agent_team_studio.agentic_team_provisioning.testing.store import (
    AgenticTestStore,
    get_test_store,
)
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def _seed_team(db: dict, team_id: str = "t1") -> None:
    _ensure_worker_manifest()
    now = datetime.now(tz=timezone.utc)
    db["teams"][team_id] = {
        "team_id": team_id,
        "name": "T",
        "description": "",
        "created_at": now,
        "updated_at": now,
    }


def _stub_llm(monkeypatch: pytest.MonkeyPatch, output: str = "agent output") -> list[str]:
    """Stub build_agent/call_agent; return a list that records each call_agent prompt."""
    prompts: list[str] = []
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.build_agent",
        lambda *a, **k: object(),
    )

    def _call(_agent, prompt):
        prompts.append(prompt)
        return output

    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.call_agent", _call
    )
    return prompts


_ACTION_PROCESS = {
    "process_id": "p1",
    "name": "P",
    "steps": [
        {
            "step_id": "s1",
            "name": "Do work",
            "description": "d",
            "step_type": "action",
            "agents": [{"agent_name": "worker", "role": "doer"}],
            "next_steps": [],
        }
    ],
}
_TEAM_ID = "t1"
_WORKER_MANIFEST_ID = manifest_agent_id(_TEAM_ID, "worker")
_TEAM_AGENTS = [
    {
        "agent_name": "worker",
        "source": "generated",
        "manifest_id": _WORKER_MANIFEST_ID,
    }
]


def _ensure_worker_manifest() -> None:
    from agent_registry import get_registry

    registry = get_registry()
    if registry.get(_WORKER_MANIFEST_ID) is None:
        registry.register(build_agent_manifest(_TEAM_ID, "worker", summary="doer"))


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


def test_advance_step_activity_reflects_store_cas(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    assert wf.advance_step_activity("r1", "s1") is True
    store.try_cancel_pipeline_run("r1")
    assert wf.advance_step_activity("r1", "s2") is False


def test_run_step_activity_action_records_and_returns(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    _stub_llm(monkeypatch, "hello output")

    out = wf.run_step_activity("r1", _TEAM_AGENTS, _ACTION_PROCESS, "s1", "prev")
    assert out == "hello output"
    row = store.get_pipeline_run("r1")
    assert len(row["step_results"]) == 1
    assert row["step_results"][0]["status"] == "completed"
    assert row["step_results"][0]["output"] == "hello output"


def test_run_step_activity_coerces_fat_history_agents(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-flight Temporal payloads may still serialize fat roster rows with null manifest_id."""
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    _stub_llm(monkeypatch, "coerced ok")

    fat_agents = [
        {
            "agent_name": "worker",
            "role": "doer",
            "skills": ["legacy"],
            "capabilities": [],
            "tools": [],
            "expertise": [],
            "source": "generated",
            "manifest_id": None,
        }
    ]
    out = wf.run_step_activity("r1", fat_agents, _ACTION_PROCESS, "s1", "prev")
    assert out == "coerced ok"
    row = store.get_pipeline_run("r1")
    assert len(row["step_results"]) == 1
    assert row["step_results"][0]["status"] == "completed"
    assert row["step_results"][0]["output"] == "coerced ok"


def test_run_step_activity_is_idempotent_per_step(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-run (mid-activity crash replay) must not re-call the LLM or double-append."""
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    prompts = _stub_llm(monkeypatch, "first output")

    first = wf.run_step_activity("r1", _TEAM_AGENTS, _ACTION_PROCESS, "s1", "prev")
    assert first == "first output"
    assert len(prompts) == 1

    # Second call: the step is already completed -> stored output, no LLM, no append.
    second = wf.run_step_activity("r1", _TEAM_AGENTS, _ACTION_PROCESS, "s1", "prev")
    assert second == "first output"
    assert len(prompts) == 1  # LLM not re-invoked
    assert len(store.get_pipeline_run("r1")["step_results"]) == 1  # no duplicate


def test_run_step_activity_decision(fake_pg: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    _stub_llm(monkeypatch, "step_b")
    process = {
        "process_id": "p1",
        "steps": [
            {
                "step_id": "d1",
                "name": "Decide",
                "step_type": "decision",
                "agents": [{"agent_name": "worker", "role": "doer"}],
                "next_steps": ["step_a", "step_b"],
                "condition": "pick",
            }
        ],
    }
    out = wf.run_step_activity("r1", _TEAM_AGENTS, process, "d1", "prev")
    assert out == "step_b"
    assert store.get_pipeline_run("r1")["step_results"][0]["output"] == "Decision: step_b"

    # P2 regression: replay (crash after the decision recorded, before Temporal marked the
    # activity done) must return the RAW branch id, not the "Decision: ..." display output,
    # so prev_output for downstream steps matches the non-crash path.
    prompts = _stub_llm(monkeypatch, "SHOULD-NOT-BE-CALLED")
    replayed = wf.run_step_activity("r1", _TEAM_AGENTS, process, "d1", "prev")
    assert replayed == "step_b"  # raw id, not "Decision: step_b"
    assert prompts == []  # LLM not re-invoked on replay


def test_wait_setup_activity_publishes_and_is_idempotent(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")

    assert wf.wait_setup_activity("r1", "w1", "Ask", "please answer") is True
    row = store.get_pipeline_run("r1")
    assert row["status"] == "waiting_for_input"
    assert row["human_prompt"] == "please answer"
    assert row["step_results"][0]["status"] == "waiting_for_input"

    # Idempotent: a re-run does not append a second waiting result.
    assert wf.wait_setup_activity("r1", "w1", "Ask", "please answer") is True
    assert len(store.get_pipeline_run("r1")["step_results"]) == 1


def test_wait_setup_activity_skips_terminal_run(fake_pg: dict) -> None:
    """P2 regression: a cancel landing while wait_setup is in flight must not resurrect
    the run to waiting_for_input — the activity returns False and writes nothing."""
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    assert store.try_cancel_pipeline_run("r1") is True

    assert wf.wait_setup_activity("r1", "w1", "Ask", "please answer") is False
    row = store.get_pipeline_run("r1")
    assert row["status"] == "cancelled"  # NOT revived
    assert row["step_results"] == []


def test_wait_finalize_resumes_from_running_row(fake_pg: dict) -> None:
    """When the /input endpoint CAS'd the row to running + persisted the input,
    wait_finalize records the step result and returns the persisted input."""
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    wf.wait_setup_activity("r1", "w1", "Ask", "please answer")
    assert store.try_resume_pipeline_run_temporal("r1", "the answer") is True  # endpoint CAS

    out = wf.wait_finalize_activity("r1", "w1", False, 60)
    assert out == {"state": "resumed", "output": "the answer"}
    row = store.get_pipeline_run("r1")
    assert row["status"] == "running"
    assert row["human_prompt"] is None
    assert row["step_results"][0]["status"] == "completed"
    assert row["step_results"][0]["output"] == "the answer"


def test_wait_finalize_expires_only_when_allowed(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    wf.wait_setup_activity("r1", "w1", "Ask", "please answer")

    # Not yet allowed to expire -> still waiting.
    assert wf.wait_finalize_activity("r1", "w1", False, 60) == {"state": "waiting"}
    assert store.get_pipeline_run("r1")["status"] == "waiting_for_input"

    # Allowed to expire -> fails the run and marks the step timed_out.
    assert wf.wait_finalize_activity("r1", "w1", True, 60) == {"state": "expired"}
    row = store.get_pipeline_run("r1")
    assert row["status"] == "failed"
    assert "wait_timeout" in row["error"]
    assert row["step_results"][0]["status"] == "timed_out"


def test_wait_finalize_reports_terminal_and_does_not_revive(fake_pg: dict) -> None:
    """P1/P2 regression: a cancelled run is reported terminal and never written back to
    running/waiting, even when finalize is asked to expire."""
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    wf.wait_setup_activity("r1", "w1", "Ask", "please answer")
    assert store.try_cancel_pipeline_run("r1") is True

    assert wf.wait_finalize_activity("r1", "w1", True, 60) == {"state": "terminal"}
    assert store.get_pipeline_run("r1")["status"] == "cancelled"  # untouched


def test_wait_finalize_resume_wins_lost_expire_race(fake_pg: dict) -> None:
    """P1 regression: if a resume CAS'd the row to running just as the timer elapsed, the
    expire CAS loses and finalize honours the resume instead of clobbering it."""
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    wf.wait_setup_activity("r1", "w1", "Ask", "please answer")
    # Resume won the race first: row is already running with the input.
    assert store.try_resume_pipeline_run_temporal("r1", "just in time") is True

    out = wf.wait_finalize_activity("r1", "w1", True, 60)  # allow_expire, but row running
    assert out == {"state": "resumed", "output": "just in time"}
    assert store.get_pipeline_run("r1")["status"] == "running"


def test_complete_cancel_fail_activities(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()

    store.create_pipeline_run("r1", "t1", "p1")
    wf.complete_activity("r1")
    assert store.get_pipeline_run("r1")["status"] == "completed"

    store.create_pipeline_run("r2", "t1", "p1")
    wf.cancel_reconcile_activity("r2")
    assert store.get_pipeline_run("r2")["status"] == "cancelled"

    store.create_pipeline_run("r3", "t1", "p1")
    wf.fail_activity("r3", "boom")
    row = store.get_pipeline_run("r3")
    assert row["status"] == "failed"
    assert row["error"] == "boom"


# ---------------------------------------------------------------------------
# Workflow driver (activities + wait_condition patched)
# ---------------------------------------------------------------------------


_FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _patch_execute(monkeypatch, handlers: dict) -> list:
    """Patch workflow.execute_activity to dispatch by activity-function identity, and
    workflow.now() to a fixed instant (the WAIT loop is bounded by it)."""
    calls: list = []

    async def _fake_exec(fn, *, args, **_kw):
        calls.append((fn, args))
        handler = handlers.get(fn)
        return handler(args) if handler else None

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    monkeypatch.setattr("temporalio.workflow.now", lambda: _FIXED_NOW)
    return calls


def test_workflow_run_completes_action_then_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_obj = wf.AgenticPipelineWorkflow()

    process = {
        "process_id": "p1",
        "steps": [
            {"step_id": "s1", "step_type": "action", "name": "A", "next_steps": ["w1"]},
            {
                "step_id": "w1",
                "step_type": "wait",
                "name": "W",
                "description": "?",
                "next_steps": [],
            },
        ],
    }

    calls = _patch_execute(
        monkeypatch,
        {
            wf.advance_step_activity: lambda args: True,
            wf.run_step_activity: lambda args: "action-out",
            wf.wait_setup_activity: lambda args: True,  # published
            # finalize reconciles from the store; the signal woke us -> resumed.
            wf.wait_finalize_activity: lambda args: {"state": "resumed", "output": "human answer"},
            wf.complete_activity: lambda args: None,
        },
    )

    async def _fake_wait(pred, timeout=None):
        # Simulate the submit_input signal arriving (fast-path wake).
        workflow_obj.submit_input("human answer")

    monkeypatch.setattr("temporalio.workflow.wait_condition", _fake_wait)

    result = asyncio.run(workflow_obj.run("r1", _TEAM_AGENTS, process, "seed", 3600))
    assert result == {"run_id": "r1", "terminal": "completed"}

    fns = [c[0] for c in calls]
    assert wf.run_step_activity in fns
    assert wf.wait_setup_activity in fns
    assert wf.wait_finalize_activity in fns
    assert wf.complete_activity in fns
    # finalize was asked NOT to expire (still within the WAIT deadline).
    finalize_call = next(c for c in calls if c[0] is wf.wait_finalize_activity)
    assert finalize_call[1][:2] == ["r1", "w1"]
    assert finalize_call[1][2] is False  # allow_expire


def test_workflow_run_wait_setup_terminal_stops_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """If wait_setup reports the run already went terminal (cancel raced setup), the
    workflow stops without waiting or finalizing."""
    workflow_obj = wf.AgenticPipelineWorkflow()
    process = {
        "process_id": "p1",
        "steps": [
            {
                "step_id": "w1",
                "step_type": "wait",
                "name": "W",
                "description": "?",
                "next_steps": [],
            }
        ],
    }
    calls = _patch_execute(
        monkeypatch,
        {
            wf.advance_step_activity: lambda args: True,
            wf.wait_setup_activity: lambda args: False,  # terminal -> not published
        },
    )

    async def _no_wait(pred, timeout=None):  # pragma: no cover - must not wait
        raise AssertionError("must not wait when setup reports terminal")

    monkeypatch.setattr("temporalio.workflow.wait_condition", _no_wait)

    result = asyncio.run(workflow_obj.run("r1", _TEAM_AGENTS, process, None, 3600))
    assert result == {"run_id": "r1", "terminal": "out_of_band"}
    assert wf.wait_finalize_activity not in [c[0] for c in calls]


def test_workflow_run_wait_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_obj = wf.AgenticPipelineWorkflow()
    process = {
        "process_id": "p1",
        "steps": [
            {
                "step_id": "w1",
                "step_type": "wait",
                "name": "W",
                "description": "?",
                "next_steps": [],
            }
        ],
    }
    calls = _patch_execute(
        monkeypatch,
        {
            wf.advance_step_activity: lambda args: True,
            wf.wait_setup_activity: lambda args: True,
            wf.wait_finalize_activity: lambda args: {"state": "expired"},
        },
    )

    async def _no_wait(pred, timeout=None):  # pragma: no cover - wait_timeout_s=0 skips it
        raise AssertionError("wait_condition should be skipped once the deadline elapsed")

    monkeypatch.setattr("temporalio.workflow.wait_condition", _no_wait)

    # wait_timeout_s=0 -> deadline is now, so the first iteration is allowed to expire.
    result = asyncio.run(workflow_obj.run("r1", _TEAM_AGENTS, process, None, 0))
    assert result == {"run_id": "r1", "terminal": "timed_out"}
    finalize_call = next(c for c in calls if c[0] is wf.wait_finalize_activity)
    assert finalize_call[1][2] is True  # allow_expire


def test_workflow_run_wait_reloops_on_waiting_then_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spurious wake (finalize returns 'waiting') re-arms the wait; a subsequent
    'resumed' finalize threads the input and the run completes."""
    workflow_obj = wf.AgenticPipelineWorkflow()
    process = {
        "process_id": "p1",
        "steps": [
            {
                "step_id": "w1",
                "step_type": "wait",
                "name": "W",
                "description": "?",
                "next_steps": [],
            }
        ],
    }
    states = iter([{"state": "waiting"}, {"state": "resumed", "output": "answer"}])
    calls = _patch_execute(
        monkeypatch,
        {
            wf.advance_step_activity: lambda args: True,
            wf.wait_setup_activity: lambda args: True,
            wf.wait_finalize_activity: lambda args: next(states),
            wf.complete_activity: lambda args: None,
        },
    )

    async def _fake_wait(pred, timeout=None):
        workflow_obj.submit_input("answer")

    monkeypatch.setattr("temporalio.workflow.wait_condition", _fake_wait)

    result = asyncio.run(workflow_obj.run("r1", _TEAM_AGENTS, process, None, 3600))
    assert result == {"run_id": "r1", "terminal": "completed"}
    assert [c[0] for c in calls].count(wf.wait_finalize_activity) == 2


def test_workflow_run_stops_on_out_of_band_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_obj = wf.AgenticPipelineWorkflow()
    _patch_execute(monkeypatch, {wf.advance_step_activity: lambda args: False})
    result = asyncio.run(workflow_obj.run("r1", _TEAM_AGENTS, _ACTION_PROCESS, None, 60))
    assert result == {"run_id": "r1", "terminal": "out_of_band"}


def test_workflow_run_cancellation_reconciles(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_obj = wf.AgenticPipelineWorkflow()

    def _advance(args):
        raise asyncio.CancelledError

    reconciled: list = []
    handlers = {
        wf.advance_step_activity: _advance,
        wf.cancel_reconcile_activity: lambda args: reconciled.append(args),
    }

    async def _fake_exec(fn, *, args, **_kw):
        handler = handlers.get(fn)
        return handler(args) if handler else None

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(workflow_obj.run("r1", _TEAM_AGENTS, _ACTION_PROCESS, None, 60))
    assert reconciled == [["r1"]]


def test_workflow_run_failure_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_obj = wf.AgenticPipelineWorkflow()

    def _boom(args):
        raise RuntimeError("kaboom")

    failed: list = []
    handlers = {
        wf.advance_step_activity: lambda args: True,
        wf.run_step_activity: _boom,
        wf.fail_activity: lambda args: failed.append(args),
    }

    async def _fake_exec(fn, *, args, **_kw):
        handler = handlers.get(fn)
        return handler(args) if handler else None

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)

    with pytest.raises(RuntimeError, match="kaboom"):
        asyncio.run(workflow_obj.run("r1", _TEAM_AGENTS, _ACTION_PROCESS, None, 60))
    assert failed and failed[0][0] == "r1" and "kaboom" in failed[0][1]


def test_retry_policies_allow_crash_recovery() -> None:
    """Activities must allow bounded retries so a worker crash mid-activity is recovered
    (not left to fail after the start-to-close timeout with a single attempt)."""
    assert wf._STORE_RETRY.maximum_attempts > 1
    assert wf._AGENT_RETRY.maximum_attempts > 1


def test_run_step_activity_wraps_failure_as_non_retryable(
    fake_pg: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine handler failure fails fast (non_retryable) so the expensive, non-
    idempotent LLM step is not auto-retried under _AGENT_RETRY."""
    from temporalio.exceptions import ApplicationError

    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.build_agent",
        lambda *a, **k: object(),
    )

    def _boom(_agent, _prompt):
        raise RuntimeError("llm exploded")

    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.call_agent", _boom
    )

    with pytest.raises(ApplicationError) as exc_info:
        wf.run_step_activity("r1", _TEAM_AGENTS, _ACTION_PROCESS, "s1", "prev")
    assert exc_info.value.non_retryable is True
    assert "llm exploded" in str(exc_info.value)


def test_get_test_store_singleton_is_agentic_store(fake_pg: dict) -> None:
    assert isinstance(get_test_store(), AgenticTestStore)

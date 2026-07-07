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

from agentic_team_provisioning.temporal import workflows as wf
from agentic_team_provisioning.testing.store import AgenticTestStore, get_test_store
from agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def _seed_team(db: dict, team_id: str = "t1") -> None:
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
        "agentic_team_provisioning.runtime.pipeline_runner.build_agent",
        lambda *a, **k: object(),
    )

    def _call(_agent, prompt):
        prompts.append(prompt)
        return output

    monkeypatch.setattr("agentic_team_provisioning.runtime.pipeline_runner.call_agent", _call)
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
_TEAM_AGENTS = [{"agent_name": "worker", "role": "doer"}]


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


def test_wait_setup_resume_expire_activities(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")

    wf.wait_setup_activity("r1", "w1", "Ask", "please answer")
    row = store.get_pipeline_run("r1")
    assert row["status"] == "waiting_for_input"
    assert row["human_prompt"] == "please answer"
    assert row["step_results"][0]["status"] == "waiting_for_input"

    # Idempotent: a re-run does not append a second waiting result.
    wf.wait_setup_activity("r1", "w1", "Ask", "please answer")
    assert len(store.get_pipeline_run("r1")["step_results"]) == 1

    returned = wf.wait_resume_activity("r1", "w1", "the answer")
    assert returned == "the answer"
    row = store.get_pipeline_run("r1")
    assert row["status"] == "running"
    assert row["human_prompt"] is None
    assert row["step_results"][0]["status"] == "completed"
    assert row["step_results"][0]["output"] == "the answer"


def test_wait_expire_activity_fails_still_waiting_run(fake_pg: dict) -> None:
    _seed_team(fake_pg)
    store = AgenticTestStore()
    store.create_pipeline_run("r1", "t1", "p1")
    wf.wait_setup_activity("r1", "w1", "Ask", "please answer")

    wf.wait_expire_activity("r1", "w1", 60)
    row = store.get_pipeline_run("r1")
    assert row["status"] == "failed"
    assert "wait_timeout" in row["error"]
    assert row["step_results"][0]["status"] == "timed_out"


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


def _patch_execute(monkeypatch, handlers: dict) -> list:
    """Patch workflow.execute_activity to dispatch by activity-function identity."""
    calls: list = []

    async def _fake_exec(fn, *, args, **_kw):
        calls.append((fn, args))
        handler = handlers.get(fn)
        return handler(args) if handler else None

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
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
            wf.wait_setup_activity: lambda args: None,
            wf.wait_resume_activity: lambda args: args[2],  # echo the human input
            wf.complete_activity: lambda args: None,
        },
    )

    async def _fake_wait(pred, timeout=None):
        # Simulate the submit_input signal arriving.
        workflow_obj.submit_input("human answer")

    monkeypatch.setattr("temporalio.workflow.wait_condition", _fake_wait)

    result = asyncio.run(workflow_obj.run("r1", _TEAM_AGENTS, process, "seed", 3600))
    assert result == {"run_id": "r1", "terminal": "completed"}

    fns = [c[0] for c in calls]
    assert wf.run_step_activity in fns
    assert wf.wait_setup_activity in fns
    assert wf.wait_resume_activity in fns
    assert wf.complete_activity in fns
    # The action step's output threads into the WAIT setup as prev context is internal;
    # assert the resume echoed the signalled input as the next prev_output.
    resume_call = next(c for c in calls if c[0] is wf.wait_resume_activity)
    assert resume_call[1] == ["r1", "w1", "human answer"]


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
            wf.wait_setup_activity: lambda args: None,
            wf.wait_expire_activity: lambda args: None,
        },
    )

    async def _timeout(pred, timeout=None):
        raise asyncio.TimeoutError

    monkeypatch.setattr("temporalio.workflow.wait_condition", _timeout)

    result = asyncio.run(workflow_obj.run("r1", _TEAM_AGENTS, process, None, 60))
    assert result == {"run_id": "r1", "terminal": "timed_out"}
    assert wf.wait_expire_activity in [c[0] for c in calls]


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


def test_single_attempt_retry_policy() -> None:
    assert wf._SINGLE_ATTEMPT.maximum_attempts == 1


def test_get_test_store_singleton_is_agentic_store(fake_pg: dict) -> None:
    assert isinstance(get_test_store(), AgenticTestStore)

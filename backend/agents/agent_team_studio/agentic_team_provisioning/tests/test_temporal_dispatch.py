"""Tests for the Temporal-vs-thread dispatch / signal / cancel branches in api/main.

With ``TEMPORAL_ADDRESS`` unset ``_temporal_enabled()`` is False, so the existing
pipeline tests already cover the thread path end-to-end. These tests cover the Temporal
branches (patched enabled) and the dispatch-failure path, without a running Temporal
server.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    manifest_agent_id,
)
from agent_team_studio.agentic_team_provisioning.models import (
    AgenticTeamAgent,
    ProcessDefinition,
    ProcessStep,
    ProcessStepAgent,
    StepType,
)
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def api_main(monkeypatch: pytest.MonkeyPatch):
    install_fake_postgres(monkeypatch)
    from agent_team_studio.agentic_team_provisioning.api import main as api_main

    return api_main


@pytest.fixture
def client(api_main) -> TestClient:
    return TestClient(api_main.app)


def _seed_team_with_process(api_main) -> tuple[str, str]:
    store = api_main._store
    team = store.create_team(name="Ops", description="")
    team_id = team.team_id
    agent_name = "worker"
    from agent_registry import get_registry

    manifest_id = manifest_agent_id(team_id, agent_name)
    registry = get_registry()
    if registry.get(manifest_id) is None:
        registry.register(build_agent_manifest(team_id, agent_name, summary="doer"))
    store.save_team_agents(
        team_id,
        [
            AgenticTeamAgent(
                agent_name=agent_name,
                source="generated",
                manifest_id=manifest_id,
            )
        ],
    )
    process = ProcessDefinition(
        process_id="proc-1",
        name="P",
        steps=[
            ProcessStep(
                step_id="s1",
                name="Do work",
                step_type=StepType.ACTION,
                agents=[ProcessStepAgent(agent_name="worker", role="doer")],
            )
        ],
    )
    store.save_process(team.team_id, process)
    return team.team_id, process.process_id


def test_dispatch_uses_thread_when_disabled(api_main, client, monkeypatch):
    team_id, process_id = _seed_team_with_process(api_main)
    monkeypatch.setattr(api_main, "_temporal_enabled", lambda: False)

    started: dict = {}
    monkeypatch.setattr(
        api_main._pipeline_runner,
        "start_run",
        lambda run_id, agents, proc: started.update(run_id=run_id),
    )

    resp = client.post(f"/teams/{team_id}/test-pipeline/runs", json={"process_id": process_id})
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]
    assert started["run_id"] == run_id
    # Thread-owned run is NOT marked temporal_owned.
    assert api_main._test_store.is_pipeline_run_temporal_owned(run_id) is False


def test_dispatch_uses_temporal_when_enabled(api_main, client, monkeypatch):
    team_id, process_id = _seed_team_with_process(api_main)
    monkeypatch.setattr(api_main, "_temporal_enabled", lambda: True)

    captured: dict = {}
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.temporal.start_workflow.start_agentic_pipeline_workflow",
        lambda run_id, agents, proc, initial: captured.update(
            run_id=run_id, agents=agents, proc=proc, initial=initial
        ),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - asserts the thread path is skipped
        raise AssertionError("thread path must not run when Temporal is enabled")

    monkeypatch.setattr(api_main._pipeline_runner, "start_run", _no_thread)

    resp = client.post(
        f"/teams/{team_id}/test-pipeline/runs",
        json={"process_id": process_id, "initial_input": "seed"},
    )
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]
    assert captured["run_id"] == run_id
    assert captured["initial"] == "seed"
    # Serialized to plain JSON dicts for the workflow payload (thin roster refs).
    assert captured["agents"] == [
        {
            "agent_name": "worker",
            "source": "generated",
            "manifest_id": manifest_agent_id(team_id, "worker"),
        }
    ]
    assert captured["proc"]["process_id"] == process_id
    assert api_main._test_store.is_pipeline_run_temporal_owned(run_id) is True


def test_dispatch_failure_marks_run_failed(api_main, client, monkeypatch):
    team_id, process_id = _seed_team_with_process(api_main)
    monkeypatch.setattr(api_main, "_temporal_enabled", lambda: True)

    def _boom(*_a, **_k):
        raise RuntimeError("worker down")

    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.temporal.start_workflow.start_agentic_pipeline_workflow",
        _boom,
    )

    resp = client.post(f"/teams/{team_id}/test-pipeline/runs", json={"process_id": process_id})
    assert resp.status_code == 500
    # Exactly one run row was created and it is FAILED (never orphaned "running").
    runs = api_main._test_store.list_pipeline_runs(team_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "Dispatch failed" in runs[0]["error"]


def test_submit_input_signals_workflow_when_temporal_owned(api_main, client, monkeypatch):
    team_id, _ = _seed_team_with_process(api_main)
    run_id = "run-sig"
    api_main._test_store.create_pipeline_run(run_id, team_id, "proc-1", temporal_owned=True)
    api_main._test_store.update_pipeline_run(run_id, status="waiting_for_input")

    signalled: dict = {}
    monkeypatch.setattr(
        "shared.temporal.signal_workflow_sync",
        lambda workflow_id, signal, value: signalled.update(
            workflow_id=workflow_id, signal=signal, value=value
        ),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - thread resume must not run
        raise AssertionError("submit_human_input must not run for a temporal-owned run")

    monkeypatch.setattr(api_main._pipeline_runner, "submit_human_input", _no_thread)

    resp = client.post(f"/teams/{team_id}/test-pipeline/runs/{run_id}/input", json={"input": "hi"})
    assert resp.status_code == 200
    assert signalled == {
        "workflow_id": "agentic-pipeline-run-sig",
        "signal": "submit_input",
        "value": "hi",
    }
    # The endpoint performed the authoritative resume CAS synchronously: the response
    # (and the caller's next poll) no longer shows waiting_for_input, and the input is
    # persisted — the /input contract holds without waiting on the workflow activity.
    assert resp.json()["status"] == "running"
    assert api_main._test_store.get_pipeline_status(run_id)["human_input"] == "hi"


def test_submit_input_conflict_when_resume_cas_lost(api_main, client, monkeypatch):
    """If the resume CAS is lost (e.g. a cancel won the race after the precheck), the
    endpoint returns 409 and never signals the workflow."""
    team_id, _ = _seed_team_with_process(api_main)
    run_id = "run-lost"
    api_main._test_store.create_pipeline_run(run_id, team_id, "proc-1", temporal_owned=True)
    api_main._test_store.update_pipeline_run(run_id, status="waiting_for_input")

    # Simulate losing the atomic transition after the status precheck passed.
    monkeypatch.setattr(
        api_main._test_store, "try_resume_pipeline_run_temporal", lambda *_a, **_k: False
    )

    def _no_signal(*_a, **_k):  # pragma: no cover - must not signal on a lost CAS
        raise AssertionError("must not signal the workflow when the resume CAS is lost")

    monkeypatch.setattr("shared.temporal.signal_workflow_sync", _no_signal)

    resp = client.post(f"/teams/{team_id}/test-pipeline/runs/{run_id}/input", json={"input": "hi"})
    assert resp.status_code == 409


def test_cancel_cancels_workflow_when_temporal_owned(api_main, client, monkeypatch):
    team_id, _ = _seed_team_with_process(api_main)
    run_id = "run-cancel"
    api_main._test_store.create_pipeline_run(run_id, team_id, "proc-1", temporal_owned=True)

    cancelled: dict = {}
    monkeypatch.setattr(
        "shared.temporal.cancel_workflow_sync",
        lambda workflow_id: cancelled.update(workflow_id=workflow_id),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - thread cancel must not run
        raise AssertionError("cancel_run must not run for a temporal-owned run")

    monkeypatch.setattr(api_main._pipeline_runner, "cancel_run", _no_thread)

    resp = client.post(f"/teams/{team_id}/test-pipeline/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert cancelled == {"workflow_id": "agentic-pipeline-run-cancel"}


def test_dispatch_flag_computed_once_even_if_config_changes_mid_request(
    api_main, client, monkeypatch
):
    """``_temporal_enabled`` must be read exactly once per request. Previously
    ``start_pipeline_run`` and ``_dispatch_pipeline_run`` each called it independently,
    so a config change between the two reads could leave the persisted
    ``temporal_owned`` flag out of sync with the dispatch path actually used."""
    team_id, process_id = _seed_team_with_process(api_main)

    readings = [True, False]
    calls: list[bool] = []

    def _flaky_temporal_enabled():
        calls.append(True)
        return readings.pop(0)

    monkeypatch.setattr(api_main, "_temporal_enabled", _flaky_temporal_enabled)

    captured: dict = {}
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.temporal.start_workflow.start_agentic_pipeline_workflow",
        lambda run_id, agents, proc, initial: captured.update(run_id=run_id),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - thread path must not run
        raise AssertionError("thread path must not run when the first reading was True")

    monkeypatch.setattr(api_main._pipeline_runner, "start_run", _no_thread)

    resp = client.post(f"/teams/{team_id}/test-pipeline/runs", json={"process_id": process_id})
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]

    # Only one read of the flag — no second, possibly-divergent recomputation.
    assert len(calls) == 1
    # Dispatch used the same (first) reading that was persisted.
    assert captured["run_id"] == run_id
    assert api_main._test_store.is_pipeline_run_temporal_owned(run_id) is True


def test_temporal_enabled_false_when_shared_temporal_missing(api_main, monkeypatch):
    """_temporal_enabled returns False when shared.temporal is not importable."""
    import sys

    # `from shared.temporal import ...` raises ImportError when the module
    # entry is None (same pattern as sales_team deep-research dispatch tests).
    monkeypatch.setitem(sys.modules, "shared.temporal", None)
    assert api_main._temporal_enabled() is False


def test_temporal_enabled_false_when_temporal_disabled(api_main, monkeypatch):
    """_temporal_enabled returns False when shared.temporal reports disabled."""
    monkeypatch.setattr("shared.temporal.is_temporal_enabled", lambda: False)
    assert api_main._temporal_enabled() is False

"""Unit tests for Planning's thread-mode background worker.

``run_workflow_background`` is exercised directly (no live job service, no
HTTP layer, no FastAPI TestClient) so the ``record_planning_run`` call site
is covered without the live-service integration harness ``test_api.py``
depends on (that file's module-wide ``pytest.mark.integration`` would skip
any test added there by default).
"""

from __future__ import annotations

import pytest

from planning_team.api import main as main_module


@pytest.fixture
def job_store_calls(monkeypatch):
    """Capture update_job/mark_job_completed/mark_job_failed without a live job service."""
    calls = {"update": [], "completed": [], "failed": []}
    monkeypatch.setattr(
        main_module, "update_job", lambda job_id, **f: calls["update"].append((job_id, f))
    )
    monkeypatch.setattr(
        main_module,
        "mark_job_completed",
        lambda job_id, **f: calls["completed"].append((job_id, f)),
    )
    monkeypatch.setattr(
        main_module,
        "mark_job_failed",
        lambda job_id, error: calls["failed"].append((job_id, error)),
    )
    return calls


@pytest.fixture
def recorded_runs(monkeypatch):
    """Capture record_planning_run calls without touching Postgres."""
    calls = []
    monkeypatch.setattr(
        main_module,
        "record_planning_run",
        lambda job_id, **kwargs: calls.append((job_id, kwargs)) or True,
    )
    return calls


def test_success_records_planning_run(monkeypatch, job_store_calls, recorded_runs) -> None:
    handoff = {
        "client_context": {"client_name": "Acme"},
        "summary": "Handoff package produced by Planning.",
        # Deliberately empty, mirroring orchestrator.py's real (load-bearing)
        # behavior — the audit call must NOT source questions from here.
        "open_questions": [],
        "resolved_questions": [],
    }
    monkeypatch.setattr(
        main_module,
        "run_workflow",
        lambda **kwargs: {
            "success": True,
            "handoff_package": handoff,
            "summary": "Planning completed; handoff package ready.",
            "open_questions": [{"id": "q1"}],
            "resolved_questions": [{"question_id": "q1"}],
        },
    )
    monkeypatch.setattr(main_module, "_get_llm", lambda: object())

    main_module.run_workflow_background("job-1", "/tmp/ws", "Acme", "brief", None, True, False)

    assert len(recorded_runs) == 1
    job_id, kwargs = recorded_runs[0]
    assert job_id == "job-1"
    assert kwargs["client_name"] == "Acme"
    assert kwargs["summary"] == "Planning completed; handoff package ready."
    assert kwargs["handoff_summary"] == "Handoff package produced by Planning."
    # Sourced from result's top-level keys, not the handoff's empty copies.
    assert kwargs["open_questions"] == [{"id": "q1"}]
    assert kwargs["resolved_questions"] == [{"question_id": "q1"}]
    assert job_store_calls["completed"][0][0] == "job-1"


def test_failure_does_not_record_planning_run(monkeypatch, job_store_calls, recorded_runs) -> None:
    monkeypatch.setattr(
        main_module, "run_workflow", lambda **kwargs: {"success": False, "failure_reason": "boom"}
    )
    monkeypatch.setattr(main_module, "_get_llm", lambda: object())

    main_module.run_workflow_background("job-1", "/tmp/ws", "Acme", "brief", None, True, False)

    assert recorded_runs == []
    assert job_store_calls["failed"][0] == ("job-1", "boom")

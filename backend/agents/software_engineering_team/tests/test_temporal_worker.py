"""Tests for the SE Temporal worker module.

Exercises the public surface: ``WORKFLOWS``/``ACTIVITIES`` registration and
``start_se_temporal_worker_thread``'s delegation to the shared
``start_team_worker`` bootstrap. No live Temporal cluster is started —
``is_temporal_enabled`` and ``start_team_worker`` are patched.
"""

from __future__ import annotations


def test_workflows_and_activities_are_registered() -> None:
    from software_engineering_team.temporal import worker
    from software_engineering_team.temporal.workflows import (
        RetryFailedWorkflow,
        RunTeamWorkflow,
        RunTeamWorkflowV2,
        StandaloneJobWorkflow,
    )

    assert RunTeamWorkflow in worker.WORKFLOWS
    assert RunTeamWorkflowV2 in worker.WORKFLOWS
    assert RetryFailedWorkflow in worker.WORKFLOWS
    assert StandaloneJobWorkflow in worker.WORKFLOWS
    assert len(worker.ACTIVITIES) == 8
    names = {getattr(a, "__name__", "") for a in worker.ACTIVITIES}
    assert "run_orchestrator_activity" in names
    assert "parse_spec_activity" in names
    assert "plan_project_activity" in names
    assert "execute_coding_team_activity" in names
    assert "retry_failed_activity" in names
    assert "run_frontend_code_v2_activity" in names
    assert "run_backend_code_v2_activity" in names
    assert "run_product_analysis_activity" in names


def test_start_returns_false_when_disabled(monkeypatch) -> None:
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: False)

    def _raise(*args, **kwargs):
        raise AssertionError("start_team_worker must not be called when Temporal is disabled")

    monkeypatch.setattr(worker, "start_team_worker", _raise)
    assert worker.start_se_temporal_worker_thread() is False


def test_start_delegates_to_start_team_worker(monkeypatch) -> None:
    """When enabled, the boot hook delegates to ``start_team_worker`` with the
    SE team's workflows/activities/task queue instead of hand-rolling a
    ThreadPoolExecutor/Worker/daemon-thread bootstrap."""
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(worker, "start_team_worker", _fake_start)

    assert worker.start_se_temporal_worker_thread() is True
    assert captured == {
        "team": "software_engineering",
        "workflows": worker.WORKFLOWS,
        "activities": worker.ACTIVITIES,
        "task_queue": worker.TASK_QUEUE,
    }


def test_start_returns_start_team_worker_result(monkeypatch) -> None:
    """The return value is exactly what ``start_team_worker`` reports (e.g. False
    when the shared client fails to connect), not hardcoded to True."""
    from software_engineering_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(worker, "start_team_worker", lambda *a, **kw: False)
    assert worker.start_se_temporal_worker_thread() is False

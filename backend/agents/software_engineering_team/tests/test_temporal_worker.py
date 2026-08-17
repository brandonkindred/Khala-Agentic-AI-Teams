"""Tests for the SE Temporal worker module.

Exercises the public surface: ``WORKFLOWS``/``ACTIVITIES`` registration and
``start_se_temporal_worker_thread``'s unconditional delegation to the shared
``start_team_worker`` bootstrap (no SE-level ``is_temporal_enabled()``
pre-check — ``start_team_worker`` already performs that check itself). No
live Temporal cluster is started — ``start_team_worker`` is patched, except
for the disabled-path test which leaves ``TEMPORAL_ADDRESS`` unset to
exercise the real check inside shared ``start_team_worker``.
"""

from __future__ import annotations


def test_workflows_and_activities_are_registered() -> None:
    from software_engineering_team.temporal import worker
    from software_engineering_team.temporal.workflows import (
        RetryFailedWorkflow,
        RunTeamWorkflowV2,
        StandaloneJobWorkflow,
    )

    assert RunTeamWorkflowV2 in worker.WORKFLOWS
    assert RetryFailedWorkflow in worker.WORKFLOWS
    assert StandaloneJobWorkflow in worker.WORKFLOWS
    assert len(worker.ACTIVITIES) == 7
    names = {getattr(a, "__name__", "") for a in worker.ACTIVITIES}
    assert "parse_spec_activity" in names
    assert "plan_project_activity" in names
    assert "execute_coding_team_activity" in names
    assert "retry_failed_activity" in names
    assert "run_frontend_code_v2_activity" in names
    assert "run_backend_code_v2_activity" in names
    assert "run_product_analysis_activity" in names


def test_start_returns_false_when_disabled(monkeypatch) -> None:
    """Standalone dev path: with TEMPORAL_ADDRESS unset, start_se_temporal_worker_thread
    delegates straight to start_team_worker, whose own is_temporal_enabled()
    check returns False -- no SE-level pre-check duplicates it."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from software_engineering_team.temporal import worker

    assert worker.start_se_temporal_worker_thread() is False


def test_start_delegates_to_start_team_worker(monkeypatch) -> None:
    """When enabled, the boot hook delegates to ``start_team_worker`` with the
    SE team's workflows/activities/task queue instead of hand-rolling a
    ThreadPoolExecutor/Worker/daemon-thread bootstrap."""
    from software_engineering_team.temporal import worker

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

    monkeypatch.setattr(worker, "start_team_worker", lambda *a, **kw: False)
    assert worker.start_se_temporal_worker_thread() is False

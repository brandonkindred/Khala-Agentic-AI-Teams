"""Tests for the neutral pipeline core (thread-path wrappers + orchestrator singleton).

The cancel-guarded ``run_*_core`` and the RUNNING/COMPLETED writes are exercised
through the Temporal activities in ``test_temporal_activities.py``; these tests
pin the thread-path ``run_*_background`` wrappers (which swallow failures as
FAILED) and the lazy ``get_orchestrator`` singleton.
"""

from __future__ import annotations

import pytest

from nutrition_meal_planning_team import pipeline
from nutrition_meal_planning_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    create_job,
    get_job,
    update_job,
)
from nutrition_meal_planning_team.tests._fakes import FakeResult, patch_orch


@pytest.mark.parametrize(
    "background, arg",
    [
        (pipeline.run_nutrition_plan_background, {"client_id": "client-1"}),
        (pipeline.run_regenerate_background, "client-1"),
        (pipeline.run_meal_plan_background, {"client_id": "client-1"}),
    ],
)
def test_background_marks_completed(monkeypatch, background, arg):
    patch_orch(monkeypatch)
    create_job("job-bg")

    background("job-bg", arg)

    job = get_job("job-bg")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert job["result"]["client_id"] == "client-1"


@pytest.mark.parametrize(
    "background, arg",
    [
        (pipeline.run_nutrition_plan_background, {"client_id": "client-1"}),
        (pipeline.run_regenerate_background, "client-1"),
        (pipeline.run_meal_plan_background, {"client_id": "client-1"}),
    ],
)
def test_background_swallows_failure_as_failed(monkeypatch, background, arg):
    """A daemon thread has no caller to raise to — a failure must land the job
    in FAILED, not propagate."""
    patch_orch(monkeypatch, exc=RuntimeError("pipeline exploded"))
    create_job("job-bg-fail")

    background("job-bg-fail", arg)  # must not raise

    job = get_job("job-bg-fail")
    assert job["status"] == JOB_STATUS_FAILED
    assert "pipeline exploded" in (job.get("error") or "")


def test_background_value_error_marks_not_found(monkeypatch):
    patch_orch(monkeypatch, exc=ValueError("Profile not found"))
    create_job("job-bg-404")

    pipeline.run_nutrition_plan_background("job-bg-404", {"client_id": "client-1"})

    job = get_job("job-bg-404")
    assert job["status"] == JOB_STATUS_FAILED
    assert job["not_found"] is True


def test_mark_job_failed_is_noop_when_cancelled():
    create_job("job-mc")
    update_job("job-mc", status=JOB_STATUS_CANCELLED)

    pipeline.mark_job_failed("job-mc", RuntimeError("ignored"))

    # Cancelled is terminal — the row is not overwritten to FAILED.
    assert get_job("job-mc")["status"] == JOB_STATUS_CANCELLED


@pytest.mark.parametrize(
    "core, arg",
    [
        (pipeline.run_nutrition_plan_core, {"client_id": "c"}),
        (pipeline.run_regenerate_core, "c"),
        (pipeline.run_meal_plan_core, {"client_id": "c"}),
    ],
)
def test_core_skips_completed_write_when_cancelled_mid_run(monkeypatch, core, arg):
    """The post-run cancel guard: if the job is cancelled during the (expensive)
    orchestrator call, the core returns before writing COMPLETED."""

    class _CancellingOrch:
        def _result(self, cid: str) -> FakeResult:
            update_job("job-mid", status=JOB_STATUS_CANCELLED)
            return FakeResult({"client_id": cid})

        def get_nutrition_plan(self, req) -> FakeResult:
            return self._result(req.client_id)

        def regenerate_nutrition_plan(self, client_id: str) -> FakeResult:
            return self._result(client_id)

        def get_meal_plan(self, req) -> FakeResult:
            return self._result(req.client_id)

    monkeypatch.setattr(pipeline, "get_orchestrator", lambda: _CancellingOrch())
    create_job("job-mid")

    core("job-mid", arg)

    # Post-run guard fired: still cancelled, no COMPLETED overwrite.
    assert get_job("job-mid")["status"] == JOB_STATUS_CANCELLED


def test_get_orchestrator_returns_singleton(monkeypatch):
    """The lazy singleton is built once and reused (constructs with a lazy LLM
    model, so it works without a configured provider)."""
    monkeypatch.setattr(pipeline, "_orchestrator", None)

    first = pipeline.get_orchestrator()
    second = pipeline.get_orchestrator()

    assert first is second

"""Tests for the neutral pipeline core (thread-path wrappers + orchestrator singleton).

The cancel-guarded ``run_*_core`` and the RUNNING/COMPLETED writes are exercised
through the Temporal activities in ``test_temporal_activities.py``; these tests
pin the thread-path ``run_*_background`` wrappers (which swallow failures as
FAILED) and the lazy ``get_orchestrator`` singleton.
"""

from __future__ import annotations

import pytest

from nutrition_meal_planning_team import pipeline
from nutrition_meal_planning_team.models import MealPlanRequest, NutritionPlanRequest
from nutrition_meal_planning_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
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


def test_mark_job_failed_skip_cancel_check_writes_failed_even_if_cancelled():
    """``skip_cancel_check=True`` is for callers that already confirmed the job
    isn't cancelled (avoiding a redundant is_job_cancelled round trip) — it must
    not re-derive cancellation state itself."""
    create_job("job-skip")
    update_job("job-skip", status=JOB_STATUS_CANCELLED)

    pipeline.mark_job_failed("job-skip", RuntimeError("boom"), skip_cancel_check=True)

    job = get_job("job-skip")
    assert job["status"] == JOB_STATUS_FAILED
    assert "boom" in (job.get("error") or "")


def test_mark_job_failed_skip_cancel_check_still_applies_not_found_mapping():
    create_job("job-skip-404")

    pipeline.mark_job_failed(
        "job-skip-404", ValueError("Profile not found"), skip_cancel_check=True
    )

    job = get_job("job-skip-404")
    assert job["status"] == JOB_STATUS_FAILED
    assert job["not_found"] is True


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

        def get_meal_plan(self, req, *, cancel_check=None) -> FakeResult:
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


def test_run_meal_plan_core_fails_fast_when_cancelled_mid_run(monkeypatch):
    """A job cancelled between nutrition-plan generation and the meal-planning
    LLM call must not write COMPLETED (or FAILED) — it's a clean early return,
    not a failure. The fake raises ``OperationCancelled`` directly (simulating
    the orchestrator's own cancel_check firing) without the job store itself
    ever being marked cancelled, so this exercises the
    ``except OperationCancelled: return`` branch specifically — not the
    pre-run ``is_job_cancelled`` guard."""
    patch_orch(monkeypatch, cancel_during_meal_plan=True)
    create_job("job-midcancel")

    pipeline.run_meal_plan_core("job-midcancel", {"client_id": "client-1"})

    job = get_job("job-midcancel")
    assert job["status"] == JOB_STATUS_RUNNING
    assert job.get("result") is None


def test_run_meal_plan_background_swallows_mid_run_cancel(monkeypatch):
    """The thread path gets the same fail-fast benefit — OperationCancelled from
    the orchestrator must not be reported as a job failure."""
    patch_orch(monkeypatch, cancel_during_meal_plan=True)
    create_job("job-midcancel-bg")

    pipeline.run_meal_plan_background("job-midcancel-bg", {"client_id": "client-1"})

    job = get_job("job-midcancel-bg")
    assert job["status"] == JOB_STATUS_RUNNING


@pytest.mark.parametrize(
    "core, model_arg",
    [
        (pipeline.run_nutrition_plan_core, NutritionPlanRequest(client_id="client-1")),
        (pipeline.run_meal_plan_core, MealPlanRequest(client_id="client-1")),
    ],
)
def test_core_accepts_an_already_built_model_directly(monkeypatch, core, model_arg):
    """The thread-dispatch path now passes the original Pydantic model (not a
    dict) to avoid a pointless serialize/revalidate round trip — the core
    functions must accept either form."""
    patch_orch(monkeypatch)
    create_job("job-model-arg")

    core("job-model-arg", model_arg)

    job = get_job("job-model-arg")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert job["result"]["client_id"] == "client-1"


@pytest.mark.parametrize(
    "background, core_attr, arg",
    [
        (pipeline.run_nutrition_plan_background, "run_nutrition_plan_core", {"client_id": "c"}),
        (pipeline.run_regenerate_background, "run_regenerate_core", "c"),
        (pipeline.run_meal_plan_background, "run_meal_plan_core", {"client_id": "c"}),
    ],
)
def test_background_wrapper_swallows_when_mark_job_failed_itself_raises(
    monkeypatch, background, core_attr, arg
):
    """Mirrors the Temporal-activity hardening: a job-store failure while
    recording FAILED must not escape the daemon thread uncaught."""

    def _core_boom(*_a, **_k):
        raise RuntimeError("original pipeline error")

    def _mark_boom(*_a, **_k):
        raise RuntimeError("job store unavailable")

    monkeypatch.setattr(pipeline, core_attr, _core_boom)
    monkeypatch.setattr(pipeline, "mark_job_failed", _mark_boom)
    create_job("job-thread-mm")

    # Must not raise — a daemon thread has no caller to propagate to.
    background("job-thread-mm", arg)

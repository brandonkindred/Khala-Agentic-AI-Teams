"""Tests for the Temporal-vs-thread dispatch branch on the three async job routes.

With ``TEMPORAL_ADDRESS`` unset ``is_temporal_enabled()`` is False, so the thread
path is the default (the integration ``test_api.py`` covers it end-to-end). These
tests cover the Temporal branch (patched enabled) and the dispatch-failure
handling for ``/plan/nutrition``, ``/plan/nutrition/{id}/regenerate`` and
``/plan/meals`` — without a running Temporal server.
"""

from __future__ import annotations

import builtins

import pytest
from fastapi.testclient import TestClient

from nutrition_meal_planning_team import pipeline
from nutrition_meal_planning_team.api import main as api_main
from nutrition_meal_planning_team.models import MealPlanRequest, NutritionPlanRequest
from nutrition_meal_planning_team.shared.job_store import JOB_STATUS_FAILED


@pytest.fixture
def client():
    with TestClient(api_main.app) as c:
        yield c


def _no_thread(*_a, **_k):  # pragma: no cover - asserts the thread path is skipped
    raise AssertionError("thread path must not run when Temporal is enabled")


def test_plan_nutrition_dispatches_to_temporal(client, monkeypatch, sample_nutrition_plan_body):
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)
    captured: dict = {}
    monkeypatch.setattr(
        "nutrition_meal_planning_team.temporal.start_workflow.start_nutrition_plan_workflow",
        lambda job_id, request: captured.update(job_id=job_id, request=request),
    )
    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    resp = client.post("/plan/nutrition", json=sample_nutrition_plan_body)

    assert resp.status_code == 200, resp.text
    assert captured["job_id"] == resp.json()["job_id"]
    assert captured["request"]["client_id"] == "client-1"


def test_plan_regenerate_dispatches_to_temporal(client, monkeypatch):
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)
    captured: dict = {}
    monkeypatch.setattr(
        "nutrition_meal_planning_team.temporal.start_workflow.start_regenerate_workflow",
        lambda job_id, client_id: captured.update(job_id=job_id, client_id=client_id),
    )
    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    resp = client.post("/plan/nutrition/client-9/regenerate")

    assert resp.status_code == 200, resp.text
    assert captured["job_id"] == resp.json()["job_id"]
    assert captured["client_id"] == "client-9"


def test_plan_meals_dispatches_to_temporal(client, monkeypatch, sample_meal_plan_body):
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)
    captured: dict = {}
    monkeypatch.setattr(
        "nutrition_meal_planning_team.temporal.start_workflow.start_meal_plan_workflow",
        lambda job_id, request: captured.update(job_id=job_id, request=request),
    )
    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    resp = client.post("/plan/meals", json=sample_meal_plan_body)

    assert resp.status_code == 200, resp.text
    assert captured["job_id"] == resp.json()["job_id"]
    assert captured["request"]["period_days"] == 7


def test_plan_meals_marks_job_failed_when_dispatch_raises(
    client, monkeypatch, fake_job_client, sample_meal_plan_body
):
    """A dispatch failure (e.g. the Temporal worker client never connected) must
    leave the freshly-created job in a terminal FAILED state, not orphaned in
    PENDING, and surface a 500."""
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    def _boom(job_id, request):
        raise RuntimeError("worker client not available")

    monkeypatch.setattr(
        "nutrition_meal_planning_team.temporal.start_workflow.start_meal_plan_workflow", _boom
    )

    resp = client.post("/plan/meals", json=sample_meal_plan_body)

    assert resp.status_code == 500
    assert "Failed to start meal plan run" in resp.json().get("detail", "")
    jobs = fake_job_client.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["status"] == JOB_STATUS_FAILED
    assert "Dispatch failed" in (jobs[0].get("error") or "")


def test_plan_nutrition_marks_job_failed_when_dispatch_raises(
    client, monkeypatch, fake_job_client, sample_nutrition_plan_body
):
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    def _boom(job_id, request):
        raise RuntimeError("worker client not available")

    monkeypatch.setattr(
        "nutrition_meal_planning_team.temporal.start_workflow.start_nutrition_plan_workflow", _boom
    )

    resp = client.post("/plan/nutrition", json=sample_nutrition_plan_body)

    assert resp.status_code == 500
    assert "Failed to start nutrition plan run" in resp.json().get("detail", "")
    jobs = fake_job_client.list_jobs()
    assert len(jobs) == 1 and jobs[0]["status"] == JOB_STATUS_FAILED
    assert "Dispatch failed" in (jobs[0].get("error") or "")


def test_plan_regenerate_marks_job_failed_when_dispatch_raises(
    client, monkeypatch, fake_job_client
):
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    def _boom(job_id, client_id):
        raise RuntimeError("worker client not available")

    monkeypatch.setattr(
        "nutrition_meal_planning_team.temporal.start_workflow.start_regenerate_workflow", _boom
    )

    resp = client.post("/plan/nutrition/client-9/regenerate")

    assert resp.status_code == 500
    assert "Failed to start nutrition regenerate run" in resp.json().get("detail", "")
    jobs = fake_job_client.list_jobs()
    assert len(jobs) == 1 and jobs[0]["status"] == JOB_STATUS_FAILED
    assert "Dispatch failed" in (jobs[0].get("error") or "")


def test_dispatch_helpers_use_thread_when_disabled(monkeypatch):
    """Direct unit check of the three helpers' thread fallback + targets."""
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: False)
    started: dict = {"calls": []}

    class _FakeThread:
        def __init__(self, *, target, args, daemon):
            started["calls"].append({"target": target, "args": args, "daemon": daemon})

        def start(self):
            started["started"] = True

    monkeypatch.setattr(api_main.threading, "Thread", _FakeThread)

    nutrition_body = NutritionPlanRequest(client_id="c")
    meal_body = MealPlanRequest(client_id="c")
    assert api_main._dispatch_nutrition_plan_run("j1", nutrition_body) is None
    assert api_main._dispatch_regenerate_run("j2", "c") is None
    assert api_main._dispatch_meal_plan_run("j3", meal_body) is None

    assert started["started"] is True
    calls = started["calls"]
    assert [c["target"] for c in calls] == [
        pipeline.run_nutrition_plan_background,
        pipeline.run_regenerate_background,
        pipeline.run_meal_plan_background,
    ]
    assert all(c["daemon"] is True for c in calls)
    # The thread branch runs in-process, so it receives the original Pydantic
    # model/client_id directly — no model_dump()/revalidate round trip (only the
    # Temporal branch, which crosses a process boundary, needs serialization).
    assert calls[0]["args"] == ("j1", nutrition_body)
    assert calls[1]["args"] == ("j2", "c")
    assert calls[2]["args"] == ("j3", meal_body)


def test_temporal_enabled_false_when_shared_temporal_missing(monkeypatch):
    """The dispatch guard treats a missing ``shared_temporal`` as disabled."""
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "shared_temporal":
            raise ImportError("no shared_temporal")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    assert api_main._temporal_enabled() is False

"""Tests for the ``/sales/pipeline/run`` Temporal-vs-thread dispatch branch.

With ``TEMPORAL_ADDRESS`` unset ``is_temporal_enabled()`` is False, so the
existing ``test_api_main.py`` cases already cover the thread path end-to-end.
These tests cover the Temporal branch (patched enabled) and the dispatch
failure path, without needing a running Temporal server.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sales_team.api import main as api_main

client = TestClient(api_main.app)

_PAYLOAD = {
    "product_name": "ProductX",
    "value_proposition": "Save 20% on outbound time",
    "icp": {"industry": ["SaaS"]},
    "entry_stage": "prospecting",
    "max_prospects": 5,
    "existing_prospects": [],
    "company_context": "ctx",
    "case_study_snippets": [],
}


@pytest.fixture
def bound_client(monkeypatch, fake_job_client):
    monkeypatch.setattr(api_main, "_job_manager", fake_job_client)
    return client


def test_run_dispatches_to_temporal_when_enabled(monkeypatch, bound_client, fake_job_client):
    # The dispatch helper imports both names lazily from their live modules
    # (``from shared_temporal import ...`` / ``from sales_team.temporal.
    # start_workflow import ...``). Patch via string paths so the patch
    # targets whatever module object sys.modules currently holds.
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    captured: dict = {}
    monkeypatch.setattr(
        "sales_team.temporal.start_workflow.start_sales_workflow",
        lambda job_id, request: captured.update(job_id=job_id, request=request),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - asserts the thread path is skipped
        raise AssertionError("thread path must not run when Temporal is enabled")

    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    response = bound_client.post("/sales/pipeline/run", json=_PAYLOAD)

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert captured["job_id"] == job_id
    assert captured["request"]["product_name"] == _PAYLOAD["product_name"]


def test_run_marks_job_failed_when_dispatch_raises(monkeypatch, bound_client, fake_job_client):
    """A dispatch failure (e.g. Temporal worker client never connected) must
    leave the job in a terminal FAILED state, not orphaned in PENDING."""
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    def _boom(job_id, request):
        raise RuntimeError("worker client not available")

    monkeypatch.setattr("sales_team.temporal.start_workflow.start_sales_workflow", _boom)

    response = bound_client.post("/sales/pipeline/run", json=_PAYLOAD)

    assert response.status_code == 500
    jobs = fake_job_client.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "failed"
    assert "Dispatch failed" in (jobs[0].get("error") or "")


def test_dispatch_helper_returns_thread_label_when_disabled(monkeypatch):
    """Direct unit check of the helper's thread fallback and its label."""
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: False)

    started: dict = {}

    class _FakeThread:
        def __init__(self, *, target, args, daemon):
            started["target"] = target
            started["args"] = args
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(api_main.threading, "Thread", _FakeThread)

    request = api_main.SalesPipelineRequest(**_PAYLOAD)

    label = api_main._dispatch_pipeline_job("job-thread", request)

    assert label == "thread"
    assert started["started"] is True
    assert started["daemon"] is True
    assert started["target"] is api_main._run_pipeline_job
    assert started["args"] == ("job-thread", request)

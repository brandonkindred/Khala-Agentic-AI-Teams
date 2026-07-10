"""Tests for the durable deep-research job endpoints + Temporal/thread dispatch.

Mirrors ``test_temporal_dispatch.py`` for the ``/sales/prospect/deep-research/run``
+ ``/status`` endpoints, without needing a running Temporal server.
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
    "target_prospects": 10,
    "max_per_company": 2,
    "company_context": "ctx",
}


@pytest.fixture
def bound_client(monkeypatch, fake_job_client):
    monkeypatch.setattr(api_main, "_job_manager", fake_job_client)
    return client


def test_run_dispatches_to_temporal_when_enabled(monkeypatch, bound_client, fake_job_client):
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)
    captured: dict = {}
    monkeypatch.setattr(
        "sales_team.temporal.start_workflow.start_deep_research_workflow",
        lambda job_id, request: captured.update(job_id=job_id, request=request),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - thread path must be skipped
        raise AssertionError("thread path must not run when Temporal is enabled")

    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    response = bound_client.post("/sales/prospect/deep-research/run", json=_PAYLOAD)

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert captured["job_id"] == job_id
    assert captured["request"]["target_prospects"] == 10


def test_run_marks_job_failed_when_dispatch_raises(monkeypatch, bound_client, fake_job_client):
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    def _boom(job_id, request):
        raise RuntimeError("worker client not available")

    monkeypatch.setattr("sales_team.temporal.start_workflow.start_deep_research_workflow", _boom)

    response = bound_client.post("/sales/prospect/deep-research/run", json=_PAYLOAD)

    assert response.status_code == 500
    jobs = fake_job_client.list_jobs()
    assert len(jobs) == 1 and jobs[0]["status"] == "failed"
    assert "Dispatch failed" in (jobs[0].get("error") or "")


def test_dispatch_falls_through_to_thread_when_shared_temporal_missing(monkeypatch):
    """If ``shared_temporal`` can't be imported at all, dispatch degrades to the
    thread path rather than erroring."""
    import sys

    monkeypatch.setitem(
        sys.modules, "shared_temporal", None
    )  # `from shared_temporal ...` → ImportError
    started: dict = {}

    class _FakeThread:
        def __init__(self, *, target, args, daemon):
            started["started"] = True

        def start(self):
            pass

    monkeypatch.setattr(api_main.threading, "Thread", _FakeThread)
    request = api_main.DeepResearchRequest(**_PAYLOAD)

    assert api_main._dispatch_deep_research_job("dr-x", request) == "thread"
    assert started["started"] is True


def test_dispatch_helper_returns_thread_label_when_disabled(monkeypatch):
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: False)
    started: dict = {}

    class _FakeThread:
        def __init__(self, *, target, args, daemon):
            started.update(target=target, args=args, daemon=daemon)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(api_main.threading, "Thread", _FakeThread)
    request = api_main.DeepResearchRequest(**_PAYLOAD)

    label = api_main._dispatch_deep_research_job("dr-thread", request)

    assert label == "thread"
    assert started["started"] is True and started["daemon"] is True
    assert started["target"] is api_main._run_deep_research_job
    assert started["args"] == ("dr-thread", request)


def test_status_returns_result_when_completed(bound_client, fake_job_client):
    from sales_team.models import DeepResearchResult

    result = DeepResearchResult(
        list_id="plst_1", product_name="ProductX", total_prospects=3, companies_represented=2
    )
    fake_job_client.create_job(
        "dr-done",
        status="completed",
        current_stage="completed",
        progress=100,
        product_name="ProductX",
        result=result.model_dump(),
        last_updated_at="now",
    )
    response = bound_client.get("/sales/prospect/deep-research/status/dr-done")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["result"]["list_id"] == "plst_1"
    assert body["result"]["total_prospects"] == 3


def test_status_result_none_when_result_malformed(bound_client, fake_job_client):
    """A malformed persisted result must not 500 the status poll — it degrades
    to ``result: null`` while still reporting status."""
    fake_job_client.create_job(
        "dr-bad",
        status="completed",
        current_stage="completed",
        progress=100,
        product_name="ProductX",
        result={"total_prospects": "not-an-int"},  # invalid DeepResearchResult
        last_updated_at="now",
    )
    response = bound_client.get("/sales/prospect/deep-research/status/dr-bad")
    assert response.status_code == 200
    assert response.json()["result"] is None


def test_status_404_when_missing(bound_client, fake_job_client):
    response = bound_client.get("/sales/prospect/deep-research/status/nope")
    assert response.status_code == 404

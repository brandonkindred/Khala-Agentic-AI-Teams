"""Tests for the ``/market-research/run`` Temporal-vs-thread dispatch branch.

With ``TEMPORAL_ADDRESS`` unset ``is_temporal_enabled()`` is False, so the
existing ``test_api.py`` cases already cover the thread path end-to-end. These
tests cover the Temporal branch (patched enabled) and the ImportError
fallback, without needing a running Temporal server.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from market_research_team.api import main as api_main
from market_research_team.api.main import app
from market_research_team.models import HumanReview, RunMarketResearchRequest
from market_research_team.pipeline import build_mission, run_market_research_background

client = TestClient(app)

_PAYLOAD = {
    "product_concept": "Interview analysis assistant",
    "target_users": "startup founders",
    "business_goal": "validate demand faster",
    "topology": "unified",
    "human_approved": True,
}


def test_run_dispatches_to_temporal_when_enabled(monkeypatch):
    # The dispatch helper imports both names lazily from their live modules
    # (``from shared_temporal import ...`` / ``from market_research_team.
    # temporal.start_workflow import ...``). Patch via string paths so the
    # patch targets whatever module object sys.modules currently holds — the
    # bootstrap test's importlib purge can otherwise swap it out from under a
    # reference bound at import time.
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    captured: dict = {}
    monkeypatch.setattr(
        "market_research_team.temporal.start_workflow.start_market_research_workflow",
        lambda job_id, request: captured.update(job_id=job_id, request=request),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - asserts the thread path is skipped
        raise AssertionError("thread path must not run when Temporal is enabled")

    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    response = client.post("/market-research/run", json=_PAYLOAD)

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert captured["job_id"] == job_id
    assert captured["request"]["product_concept"] == _PAYLOAD["product_concept"]


def test_run_marks_job_failed_when_dispatch_raises(monkeypatch, fake_job_client):
    """A dispatch failure (e.g. Temporal worker client never connected) must
    leave the job in a terminal FAILED state, not orphaned in PENDING."""
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    def _boom(job_id, request):
        raise RuntimeError("worker client not available")

    monkeypatch.setattr(
        "market_research_team.temporal.start_workflow.start_market_research_workflow", _boom
    )

    response = client.post("/market-research/run", json=_PAYLOAD)

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

    payload = RunMarketResearchRequest(**_PAYLOAD)

    label = api_main._dispatch_market_research_run("job-thread", payload)

    assert label == "thread"
    assert started["started"] is True
    assert started["daemon"] is True
    assert started["target"] is run_market_research_background
    # Mission/human_review are built lazily inside the thread branch (not
    # eagerly in the endpoint), derived from the payload.
    job_id, mission, human_review = started["args"]
    assert job_id == "job-thread"
    assert mission == build_mission(payload)
    assert human_review == HumanReview(approved=True, feedback="")

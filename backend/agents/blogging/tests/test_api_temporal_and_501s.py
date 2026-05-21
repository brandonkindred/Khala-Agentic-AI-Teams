"""Tests for API paths involving Temporal-mode startup and 501 fallback branches.

For Temporal: monkeypatch ``is_temporal_enabled`` to True and
``start_full_pipeline_workflow`` to a no-op so the route returns the
"(Temporal)" message.

For 501 fallbacks: temporarily set the api/main module-level helpers to
None so the route returns 501.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

_blogging_root = Path(__file__).resolve().parent.parent
if str(_blogging_root) not in sys.path:
    sys.path.insert(0, str(_blogging_root))

_spec = importlib.util.spec_from_file_location(
    "blogging_api_main_unit",
    _blogging_root / "api" / "main.py",
)
_api_main = sys.modules.get("blogging_api_main_unit")
if _api_main is None:
    _api_main = importlib.util.module_from_spec(_spec)
    sys.modules["blogging_api_main_unit"] = _api_main
    _spec.loader.exec_module(_api_main)
    for _cls_name in (
        "SelectTitleRequest",
        "TitleRatingItem",
        "RateTitlesRequest",
        "StoryResponseRequest",
        "BlogAnswersRequest",
        "DraftFeedbackRequest",
    ):
        _cls = getattr(_api_main, _cls_name, None)
        if _cls is not None:
            _cls.model_rebuild(_types_namespace={**_api_main.__dict__})
app = _api_main.app


@pytest.fixture
def patched_client(monkeypatch, fake_job_client) -> Any:
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    for name in (
        "create_blog_job",
        "delete_blog_job",
        "get_blog_job",
        "list_blog_jobs",
        "update_blog_job",
        "start_blog_job",
        "complete_blog_job",
        "fail_blog_job",
        "approve_blog_job",
        "unapprove_blog_job",
        "submit_title_selection",
        "submit_title_ratings",
        "submit_story_user_message",
        "skip_current_story_gap",
        "submit_blog_answers",
        "submit_draft_feedback",
        "is_waiting_for_draft_feedback",
    ):
        helper = getattr(bjs, name, None)
        if helper is not None:
            monkeypatch.setattr(_api_main, name, helper)
    return fake_job_client


@pytest.fixture
def client(patched_client) -> TestClient:
    return TestClient(app)


def _create_job(**fields: Any) -> str:
    from shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, fields.pop("brief", "brief"))
    if fields:
        bjs.update_blog_job(job_id, **fields)
    return job_id


# ---------------------------------------------------------------------------
# Temporal-enabled paths
# ---------------------------------------------------------------------------


def test_full_pipeline_async_with_temporal(client: TestClient, monkeypatch) -> None:
    """When is_temporal_enabled() returns True, route delegates to Temporal."""
    from blogging.temporal import client as tc_mod
    from blogging.temporal import start_workflow as sw_mod

    monkeypatch.setattr(tc_mod, "is_temporal_enabled", lambda: True)
    called: dict = {}

    def fake_start(job_id, request_dict):
        called["job_id"] = job_id
        called["payload"] = request_dict

    monkeypatch.setattr(sw_mod, "start_full_pipeline_workflow", fake_start)

    r = client.post("/full-pipeline-async", json={"brief": "x"})
    assert r.status_code == 200
    assert "(Temporal)" in r.json()["message"]
    assert called["job_id"]


def test_resume_with_temporal(client: TestClient, monkeypatch) -> None:
    from shared import blog_job_store as bjs

    from blogging.temporal import client as tc_mod
    from blogging.temporal import start_workflow as sw_mod

    monkeypatch.setattr(tc_mod, "is_temporal_enabled", lambda: True)
    called: dict = {}
    monkeypatch.setattr(
        sw_mod,
        "start_full_pipeline_workflow",
        lambda jid, payload: called.__setitem__("called", True),
    )

    job_id = _create_job()
    bjs.update_blog_job(job_id, status="interrupted", request_payload={"brief": "x"})

    r = client.post(f"/job/{job_id}/resume")
    assert r.status_code == 200
    assert "(Temporal)" in r.json()["message"]


def test_restart_with_temporal(client: TestClient, monkeypatch, fake_job_client) -> None:
    from shared import blog_job_store as bjs

    from blogging.temporal import client as tc_mod
    from blogging.temporal import start_workflow as sw_mod

    monkeypatch.setattr(tc_mod, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(sw_mod, "start_full_pipeline_workflow", lambda *a, **kw: None)

    # Patch the alternate bjs module path for reset_blog_job
    try:
        from blogging.shared import blog_job_store as bjs_alt

        monkeypatch.setattr(bjs_alt, "_client", lambda *a, **kw: fake_job_client)
    except ImportError:
        pass

    job_id = _create_job()
    bjs.update_blog_job(job_id, status="completed", request_payload={"brief": "x"})
    r = client.post(f"/job/{job_id}/restart")
    assert r.status_code == 200
    assert "(Temporal)" in r.json()["message"]


# ---------------------------------------------------------------------------
# 501 paths when helpers are None
# ---------------------------------------------------------------------------


def test_full_pipeline_async_501_when_create_blog_job_none(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "create_blog_job", None)
    r = client.post("/full-pipeline-async", json={"brief": "x"})
    assert r.status_code == 501


def test_get_job_status_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "get_blog_job", None)
    r = client.get("/job/anything")
    assert r.status_code == 501


def test_list_jobs_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "list_blog_jobs", None)
    r = client.get("/jobs")
    assert r.status_code == 501


def test_cancel_job_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "get_blog_job", None)
    r = client.post("/job/x/cancel")
    assert r.status_code == 501


def test_delete_job_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "delete_blog_job", None)
    r = client.delete("/job/x")
    assert r.status_code == 501


def test_resume_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "get_blog_job", None)
    r = client.post("/job/x/resume")
    assert r.status_code == 501


def test_restart_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "get_blog_job", None)
    r = client.post("/job/x/restart")
    assert r.status_code == 501


def test_approve_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "approve_blog_job", None)
    r = client.post("/job/x/approve")
    assert r.status_code == 501


def test_unapprove_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "unapprove_blog_job", None)
    r = client.post("/job/x/unapprove")
    assert r.status_code == 501


def test_select_title_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "submit_title_selection", None)
    r = client.post("/job/x/select-title", json={"title": "x"})
    assert r.status_code == 501


def test_rate_titles_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "submit_title_ratings", None)
    r = client.post("/job/x/rate-titles", json={"ratings": [{"title": "x", "rating": "like"}]})
    assert r.status_code == 501


def test_story_response_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "submit_story_user_message", None)
    r = client.post("/job/x/story-response", json={"message": "x"})
    assert r.status_code == 501


def test_skip_story_gap_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "skip_current_story_gap", None)
    r = client.post("/job/x/skip-story-gap")
    assert r.status_code == 501


def test_submit_answers_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "submit_blog_answers", None)
    r = client.post("/job/x/answers", json={"answers": []})
    assert r.status_code == 501


def test_draft_feedback_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "submit_draft_feedback", None)
    r = client.post("/job/x/draft-feedback", json={"feedback": "x", "approved": True})
    assert r.status_code == 501


def test_list_artifacts_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "get_blog_job", None)
    r = client.get("/job/x/artifacts")
    assert r.status_code == 501


def test_get_artifact_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "read_artifact", None)
    r = client.get("/job/x/artifacts/final.md")
    assert r.status_code == 501


def test_medium_stats_async_501_when_create_blog_job_none(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "create_blog_job", None)
    r = client.post("/medium-stats-async", json={})
    assert r.status_code == 501


def test_stream_501(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(_api_main, "get_blog_job", None)
    r = client.get("/job/x/stream")
    assert r.status_code == 501


def test_resume_404_via_validate_job_for_action(client: TestClient, monkeypatch) -> None:
    """validate_job_for_action raises ValueError 'not found' → 404."""
    job_id = _create_job()
    # Empty job dict triggers ValueError("Job ... not found") in validate_job_for_action

    def fake_get(jid):
        return None  # simulate missing

    monkeypatch.setattr(_api_main, "get_blog_job", fake_get)
    r = client.post(f"/job/{job_id}/resume")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# SSE event_generator — patch subscribe to deliver an event then a terminal
# ---------------------------------------------------------------------------


def test_stream_terminal_via_subscriber(client: TestClient, monkeypatch) -> None:
    """Cover the event_generator main path by delivering events."""
    from collections import deque

    import shared.job_event_bus as bus

    job_id = _create_job()  # status=pending so terminal short-circuit doesn't fire

    class _FakeSub:
        def __init__(self):
            self.events = deque(
                [
                    {"type": "update", "progress": 50},
                    {"type": "complete", "status": "completed"},
                ]
            )

            class _Notify:
                def wait(self, timeout=None):
                    pass

                def clear(self):
                    pass

                def set(self):
                    pass

            self.notify = _Notify()

        def touch(self):
            pass

    fake_sub = _FakeSub()
    monkeypatch.setattr(bus, "subscribe", lambda jid: fake_sub)
    monkeypatch.setattr(bus, "unsubscribe", lambda jid, sub: None)

    with client.stream("GET", f"/job/{job_id}/stream") as r:
        assert r.status_code == 200
        chunks = list(r.iter_text())
    text = "".join(chunks)
    # Snapshot + complete events
    assert "snapshot" in text
    assert "complete" in text
    assert "done" in text

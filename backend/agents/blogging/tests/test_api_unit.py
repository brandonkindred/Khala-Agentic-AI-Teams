"""Unit tests for the blogging FastAPI app.

These tests bypass the real job service and the heavy ``run_pipeline``
implementation by patching the relevant seams in ``api/main.py``. The whole
suite runs against an in-memory ``FakeJobServiceClient``.
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
_api_main = importlib.util.module_from_spec(_spec)
sys.modules["blogging_api_main_unit"] = _api_main
_spec.loader.exec_module(_api_main)
# api/main only rebuilds the response classes by default; the request DTOs
# defined in the second half of the file need an explicit rebuild after we
# import the module under a synthetic name.
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
    """Replace the global blog_job_store client and rebind all helpers on api/main."""
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    # Refresh references inside api/main to the freshly-patched helpers.
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


def _create(client: TestClient, **fields: Any) -> str:
    """Create a job via the underlying store using a known job_id."""
    from shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, fields.pop("brief", "brief"))
    if fields:
        bjs.update_blog_job(job_id, **fields)
    return job_id


# ---------------------------------------------------------------------------
# Basic endpoints
# ---------------------------------------------------------------------------


def test_health_endpoint(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "brand_spec_configured" in body
    assert isinstance(body["brand_spec_configured"], bool)


def test_format_audience_variants() -> None:
    """Internal helper covers None / str / AudienceDetails."""
    api_main = _api_main
    assert api_main._format_audience(None) == ""
    assert api_main._format_audience("  hello ") == "hello"
    aud = api_main.AudienceDetails(
        skill_level="beginner",
        profession="dev",
        hobbies=["coding", "biking"],
        other="loves coffee",
    )
    text = api_main._format_audience(aud)
    assert "skill level: beginner" in text
    assert "profession: dev" in text
    assert "interests: coding, biking" in text
    assert "loves coffee" in text
    # empty AudienceDetails returns empty string
    empty = api_main.AudienceDetails()
    assert api_main._format_audience(empty) == ""


# ---------------------------------------------------------------------------
# Job status / list / delete / cancel
# ---------------------------------------------------------------------------


def test_get_job_status_404(client: TestClient) -> None:
    r = client.get("/job/does-not-exist")
    assert r.status_code == 404


def test_get_job_status_200(client: TestClient) -> None:
    job_id = _create(client, brief="hello world")
    r = client.get(f"/job/{job_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id
    assert body["status"] == "pending"


def test_list_jobs_filters(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    a = _create(client)
    b = _create(client)
    bjs.start_blog_job(a)
    bjs.complete_blog_job(b)
    r = client.get("/jobs")
    assert r.status_code == 200
    items = r.json()
    ids = {item["job_id"] for item in items}
    assert {a, b}.issubset(ids)

    r = client.get("/jobs", params={"running_only": True})
    assert r.status_code == 200
    items = r.json()
    ids = {item["job_id"] for item in items}
    assert a in ids
    assert b not in ids


def test_cancel_job_lifecycle(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    bjs.start_blog_job(job_id)
    r = client.post(f"/job/{job_id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    # Second cancel returns 400 because status is no longer pending/running
    r = client.post(f"/job/{job_id}/cancel")
    assert r.status_code == 400


def test_cancel_job_404(client: TestClient) -> None:
    r = client.post("/job/missing/cancel")
    assert r.status_code == 404


def test_delete_job_lifecycle(client: TestClient) -> None:
    job_id = _create(client)
    r = client.delete(f"/job/{job_id}")
    assert r.status_code == 200
    # Now it should be gone
    r = client.delete(f"/job/{job_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Approve / unapprove
# ---------------------------------------------------------------------------


def test_approve_400_when_not_terminal(client: TestClient) -> None:
    job_id = _create(client)
    r = client.post(f"/job/{job_id}/approve")
    assert r.status_code == 400


def test_approve_404(client: TestClient) -> None:
    r = client.post("/job/missing/approve")
    assert r.status_code == 404


def test_approve_unapprove_happy_path(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    bjs.complete_blog_job(job_id)
    r = client.post(f"/job/{job_id}/approve")
    assert r.status_code == 200
    assert r.json()["approved_at"]

    r = client.post(f"/job/{job_id}/unapprove")
    assert r.status_code == 200
    assert r.json()["approved_at"] is None


def test_unapprove_404(client: TestClient) -> None:
    r = client.post("/job/missing/unapprove")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Title selection / rate-titles
# ---------------------------------------------------------------------------


def test_select_title_404(client: TestClient) -> None:
    r = client.post("/job/missing/select-title", json={"title": "x"})
    assert r.status_code == 404


def test_select_title_not_waiting(client: TestClient) -> None:
    job_id = _create(client)
    r = client.post(f"/job/{job_id}/select-title", json={"title": "x"})
    assert r.status_code == 400


def test_select_title_empty_title(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    bjs.update_blog_job(job_id, waiting_for_title_selection=True)
    r = client.post(f"/job/{job_id}/select-title", json={"title": "  "})
    assert r.status_code == 422


def test_select_title_ok(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    bjs.update_blog_job(job_id, waiting_for_title_selection=True)
    r = client.post(f"/job/{job_id}/select-title", json={"title": "Chosen"})
    assert r.status_code == 200
    body = r.json()
    assert body["selected_title"] == "Chosen"
    assert body["waiting_for_title_selection"] is False


def test_rate_titles_paths(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    # 404 when missing
    r = client.post(
        "/job/missing/rate-titles", json={"ratings": [{"title": "x", "rating": "like"}]}
    )
    assert r.status_code == 404
    # 400 when not waiting
    r = client.post(
        f"/job/{job_id}/rate-titles", json={"ratings": [{"title": "x", "rating": "like"}]}
    )
    assert r.status_code == 400

    bjs.update_blog_job(job_id, waiting_for_title_selection=True)

    # 422 empty ratings
    r = client.post(f"/job/{job_id}/rate-titles", json={"ratings": []})
    assert r.status_code == 422
    # 422 bad rating value
    r = client.post(
        f"/job/{job_id}/rate-titles",
        json={"ratings": [{"title": "x", "rating": "meh"}]},
    )
    assert r.status_code == 422
    # 200 happy path with love → selected_title set, no longer waiting
    r = client.post(
        f"/job/{job_id}/rate-titles",
        json={"ratings": [{"title": "Y", "rating": "love"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["selected_title"] == "Y"


# ---------------------------------------------------------------------------
# Story / skip-story / answers / draft-feedback
# ---------------------------------------------------------------------------


def test_story_response_paths(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    r = client.post("/job/missing/story-response", json={"message": "hi"})
    assert r.status_code == 404
    r = client.post(f"/job/{job_id}/story-response", json={"message": "hi"})
    assert r.status_code == 400

    bjs.update_blog_job(job_id, waiting_for_story_input=True)
    r = client.post(f"/job/{job_id}/story-response", json={"message": "  "})
    assert r.status_code == 422

    r = client.post(f"/job/{job_id}/story-response", json={"message": "context"})
    assert r.status_code == 200


def test_skip_story_gap_paths(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    r = client.post("/job/missing/skip-story-gap")
    assert r.status_code == 404

    bjs.update_blog_job(job_id, waiting_for_story_input=True, current_story_gap_index=0)
    r = client.post(f"/job/{job_id}/skip-story-gap")
    assert r.status_code == 200
    body = r.json()
    assert body["current_story_gap_index"] == 1


def test_submit_answers_paths(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    r = client.post("/job/missing/answers", json={"answers": []})
    assert r.status_code == 404
    r = client.post(f"/job/{job_id}/answers", json={"answers": []})
    assert r.status_code == 400

    bjs.update_blog_job(job_id, waiting_for_answers=True)
    r = client.post(f"/job/{job_id}/answers", json={"answers": [{"id": "q", "value": "a"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["waiting_for_answers"] is False


def test_draft_feedback_paths(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    r = client.post("/job/missing/draft-feedback", json={"feedback": "x", "approved": True})
    assert r.status_code == 404
    r = client.post(f"/job/{job_id}/draft-feedback", json={"feedback": "x", "approved": True})
    assert r.status_code == 400

    bjs.update_blog_job(job_id, waiting_for_draft_feedback=True, draft_for_review="d")
    r = client.post(f"/job/{job_id}/draft-feedback", json={"feedback": "ok", "approved": True})
    assert r.status_code == 200
    body = r.json()
    assert body["waiting_for_draft_feedback"] is False


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def test_artifact_list_404_for_missing_job(client: TestClient) -> None:
    r = client.get("/job/missing/artifacts")
    assert r.status_code == 404


def test_artifact_list_404_when_no_work_dir(client: TestClient) -> None:
    job_id = _create(client)
    r = client.get(f"/job/{job_id}/artifacts")
    assert r.status_code == 404


def test_artifact_list_200_and_get_artifact(client: TestClient, tmp_path: Path) -> None:
    from shared import blog_job_store as bjs

    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "final.md").write_text("# Final\n")
    (workdir / "compliance_report.json").write_text('{"pass": true}')
    job_id = _create(client)
    bjs.update_blog_job(job_id, work_dir=str(workdir))

    r = client.get(f"/job/{job_id}/artifacts")
    assert r.status_code == 200
    names = [a["name"] for a in r.json()["artifacts"]]
    assert "final.md" in names
    assert "compliance_report.json" in names

    # Read markdown artifact
    r = client.get(f"/job/{job_id}/artifacts/final.md")
    assert r.status_code == 200
    assert "Final" in r.json()["content"]

    # Read json artifact
    r = client.get(f"/job/{job_id}/artifacts/compliance_report.json")
    assert r.status_code == 200
    assert r.json()["content"] == {"pass": True}

    # Download as attachment for markdown
    r = client.get(f"/job/{job_id}/artifacts/final.md", params={"download": True})
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "").lower()
    assert b"Final" in r.content

    # Download as attachment for json (object branch)
    r = client.get(f"/job/{job_id}/artifacts/compliance_report.json", params={"download": True})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"

    # 404 for unknown artifact name
    r = client.get(f"/job/{job_id}/artifacts/unknown.md")
    assert r.status_code == 404


def test_artifact_get_404_missing_job(client: TestClient) -> None:
    r = client.get("/job/missing/artifacts/final.md")
    assert r.status_code == 404


def test_artifact_get_404_no_work_dir(client: TestClient) -> None:
    job_id = _create(client)
    r = client.get(f"/job/{job_id}/artifacts/final.md")
    assert r.status_code == 404


def test_artifact_get_404_when_file_missing(client: TestClient, tmp_path: Path) -> None:
    from shared import blog_job_store as bjs

    workdir = tmp_path / "work"
    workdir.mkdir()
    job_id = _create(client)
    bjs.update_blog_job(job_id, work_dir=str(workdir))
    # final.md is a known artifact name but not present on disk → 404
    r = client.get(f"/job/{job_id}/artifacts/final.md")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# SSE stream — quick check (terminal job branch)
# ---------------------------------------------------------------------------


def test_stream_terminal_job_completes_quickly(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    bjs.complete_blog_job(job_id)
    # The stream returns immediately for terminal jobs
    with client.stream("GET", f"/job/{job_id}/stream") as r:
        assert r.status_code == 200
        chunks = list(r.iter_text())
    text = "".join(chunks)
    assert '"type":"snapshot"' in text.replace(" ", "")
    assert "done" in text


def test_stream_missing_job_404(client: TestClient) -> None:
    r = client.get("/job/missing/stream")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Resume / restart
# ---------------------------------------------------------------------------


def test_resume_job_404(client: TestClient) -> None:
    r = client.post("/job/missing/resume")
    assert r.status_code == 404


def test_resume_job_400_when_not_resumable(client: TestClient) -> None:
    """A completed job can't be resumed."""
    from shared import blog_job_store as bjs

    job_id = _create(client)
    bjs.complete_blog_job(job_id)
    r = client.post(f"/job/{job_id}/resume")
    assert r.status_code == 400


def test_resume_job_400_when_no_payload(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    bjs.update_blog_job(job_id, status="interrupted")
    r = client.post(f"/job/{job_id}/resume")
    assert r.status_code == 400
    assert "payload" in r.json()["detail"].lower()


def test_restart_job_404(client: TestClient) -> None:
    r = client.post("/job/missing/restart")
    assert r.status_code == 404


def test_restart_job_400_when_no_payload(client: TestClient) -> None:
    from shared import blog_job_store as bjs

    job_id = _create(client)
    bjs.complete_blog_job(job_id)
    r = client.post(f"/job/{job_id}/restart")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Start full pipeline async / Medium stats — heavy work patched out
# ---------------------------------------------------------------------------


def test_start_full_pipeline_async_creates_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /full-pipeline-async creates a job and starts a background thread, which we no-op."""
    started: list[Any] = []

    class _NoOpThread:
        def __init__(self, target=None, args=(), daemon=False, **kw):
            self._target = target
            self._args = args
            started.append((target, args))

        def start(self):
            pass

    # Replace the threading module reference inside the API module.
    monkeypatch.setattr(_api_main.threading, "Thread", _NoOpThread)

    body = {
        "brief": "How to ship faster",
        "title_concept": "engineering",
        "audience": "developers",
        "tone_or_purpose": "informative",
        "max_results": 5,
    }
    r = client.post("/full-pipeline-async", json=body)
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    assert started


def test_medium_stats_sync_when_integration_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Medium stats sync 503s when the integration helper says it's not ready."""
    monkeypatch.setattr(
        _api_main,
        "medium_stats_integration_eligible",
        lambda: (False, "Integration disabled"),
    )
    r = client.post("/medium-stats", json={})
    assert r.status_code == 503
    assert "disabled" in r.json()["detail"].lower()


def test_medium_stats_async_when_integration_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _api_main,
        "medium_stats_integration_eligible",
        lambda: (False, "no creds"),
    )
    r = client.post("/medium-stats-async", json={})
    assert r.status_code == 503


def test_medium_stats_async_starts_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (True, ""))

    class _NoOpThread:
        def __init__(self, target=None, args=(), daemon=False, **kw):
            self._target = target
            self._args = args

        def start(self):
            pass

    monkeypatch.setattr(_api_main.threading, "Thread", _NoOpThread)
    monkeypatch.setenv("BLOGGING_MEDIUM_STATS_ROOT", str(tmp_path / "ms"))

    r = client.post("/medium-stats-async", json={})
    assert r.status_code == 200
    body = r.json()
    assert "job_id" in body


# ---------------------------------------------------------------------------
# Story bank endpoints (delegate to shared.story_bank)
# ---------------------------------------------------------------------------


def test_story_bank_endpoints(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """list/get/delete/search delegate to story_bank module functions."""
    import shared.story_bank as sb

    monkeypatch.setattr(sb, "list_stories", lambda limit=50, offset=0: [{"id": "s1"}])
    monkeypatch.setattr(
        sb, "get_story", lambda sid: {"id": sid, "narrative": "x"} if sid == "s1" else None
    )
    monkeypatch.setattr(sb, "delete_story", lambda sid: sid == "s1")
    monkeypatch.setattr(sb, "find_relevant_stories", lambda kw, limit=5: [{"id": "s1", "kw": kw}])

    r = client.get("/stories")
    assert r.status_code == 200
    assert r.json() == [{"id": "s1"}]

    r = client.get("/stories/s1")
    assert r.status_code == 200
    assert r.json()["id"] == "s1"

    r = client.get("/stories/missing")
    assert r.status_code == 404

    r = client.delete("/stories/s1")
    assert r.status_code == 200
    r = client.delete("/stories/missing")
    assert r.status_code == 404

    r = client.get("/stories/search/foo,bar")
    assert r.status_code == 200
    assert r.json()[0]["kw"] == ["foo", "bar"]


# ---------------------------------------------------------------------------
# QuietAccessFilter
# ---------------------------------------------------------------------------


def test_quiet_access_filter() -> None:
    import logging

    flt = _api_main._QuietAccessFilter()

    # Warning passes
    rec = logging.LogRecord("uvicorn.access", logging.WARNING, "x", 0, "anything", None, None)
    assert flt.filter(rec) is True

    # 200 on /health is suppressed
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "x", 0, 'GET /health HTTP/1.1" 200', None, None
    )
    assert flt.filter(rec) is False

    # 500 to /jobs still logs
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "x", 0, 'GET /jobs HTTP/1.1" 500', None, None
    )
    assert flt.filter(rec) is True

    # Random INFO not matching patterns still passes
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "x", 0, 'POST /full-pipeline HTTP/1.1" 200', None, None
    )
    assert flt.filter(rec) is True

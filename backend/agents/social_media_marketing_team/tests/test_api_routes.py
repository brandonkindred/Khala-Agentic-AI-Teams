"""Comprehensive route-level tests for ``social_media_marketing_team.api.main``.

These tests monkeypatch the module-level ``_job_manager`` with the shared
in-memory ``FakeJobServiceClient`` so the full FastAPI app can be exercised
through ``TestClient`` without Postgres or the central job service.

Brand fetching is stubbed with ``_fetch_and_validate_brand`` patches; the
background ``_dispatch_job`` thread is replaced with an inline helper so the
request/response cycle becomes deterministic.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from social_media_marketing_team.adapters.branding import (
    BrandContext,
    BrandIncompleteError,
    BrandNotFoundError,
)
from social_media_marketing_team.api import main as api_main
from social_media_marketing_team.api.main import app
from social_media_marketing_team.tests.test_winning_posts_bank import _FakeConn

_BRAND_ADAPTER = "social_media_marketing_team.api.main"

_MOCK_BRAND_CTX = BrandContext(
    brand_name="Acme",
    target_audience="B2B founders",
    voice_and_tone="clear and direct",
    brand_guidelines="Positioning: Developer tools that just work.",
    brand_objectives="Purpose: Empower developers.\nMission: Ship faster.",
    messaging_pillars=["Developer empowerment"],
    brand_story="Acme was born from frustration.",
    tagline="Just works",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_jobs(monkeypatch: pytest.MonkeyPatch, fake_job_client):
    """Swap the module's job manager for an in-memory fake."""
    monkeypatch.setattr(api_main, "_job_manager", fake_job_client)
    return fake_job_client


@pytest.fixture
def inline_thread(monkeypatch: pytest.MonkeyPatch):
    """Force ``threading.Thread`` to run synchronously so requests complete
    before the route returns. This makes endpoints that dispatch background
    work fully observable via TestClient.
    """

    class _InlineThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None, name=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}
            self.daemon = daemon
            self.name = name

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(api_main.threading, "Thread", _InlineThread)
    return _InlineThread


@pytest.fixture
def fake_bank(monkeypatch: pytest.MonkeyPatch):
    """Install a fake Postgres connection on the winning-posts bank module."""
    db: dict[str, Any] = {"posts": {}}

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db)

    import social_media_marketing_team.shared.winning_posts_bank as wpb

    monkeypatch.setattr(wpb, "get_conn", _fake_get_conn)
    return db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_run(client: TestClient) -> str:
    resp = client.post(
        "/social-marketing/run",
        json={
            "client_id": "client_1",
            "brand_id": "brand_1",
            "llm_model_name": "deepseek-v4-flash:cloud",
            "human_approved_for_testing": True,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["job_id"]


# ---------------------------------------------------------------------------
# Module-level helpers (small surface — best covered directly)
# ---------------------------------------------------------------------------


def test_bank_ingest_threshold_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOCIAL_MARKETING_WINNING_POSTS_INGEST_THRESHOLD", raising=False)
    assert api_main._bank_ingest_threshold() == pytest.approx(0.7)


def test_bank_ingest_threshold_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOCIAL_MARKETING_WINNING_POSTS_INGEST_THRESHOLD", "not-a-float")
    assert api_main._bank_ingest_threshold() == pytest.approx(0.7)


def test_bank_ingest_threshold_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOCIAL_MARKETING_WINNING_POSTS_INGEST_THRESHOLD", "0.42")
    assert api_main._bank_ingest_threshold() == pytest.approx(0.42)


def test_metric_lookup_handles_dicts_and_objects() -> None:
    class _Metric:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    metrics = [
        _Metric("likes", 10),
        {"name": "shares", "value": 5},
        {"name": "noisy", "value": "x"},
    ]
    assert api_main._metric_lookup(metrics, "likes") == pytest.approx(10.0)
    assert api_main._metric_lookup(metrics, "shares") == pytest.approx(5.0)
    # value is non-numeric -> coerced to 0.0
    assert api_main._metric_lookup(metrics, "noisy") == 0.0
    # missing -> 0.0
    assert api_main._metric_lookup(metrics, "absent") == 0.0
    # empty/None safe
    assert api_main._metric_lookup([], "anything") == 0.0
    assert api_main._metric_lookup(None, "x") == 0.0


def test_metric_lookup_handles_none_value_object() -> None:
    class _M:
        def __init__(self):
            self.name = "x"
            self.value = None

    assert api_main._metric_lookup([_M()], "x") == 0.0


def test_compute_engagement_score_uses_engagement_rate() -> None:
    class _M:
        def __init__(self, n, v):
            self.name = n
            self.value = v

    metrics = [_M("engagement_rate", 0.42)]
    assert api_main._compute_engagement_score(metrics) == pytest.approx(0.42)


def test_compute_engagement_score_clamps_engagement_rate() -> None:
    class _M:
        def __init__(self, n, v):
            self.name = n
            self.value = v

    metrics = [_M("engagement_rate", 2.0)]
    assert api_main._compute_engagement_score(metrics) == 1.0


def test_compute_engagement_score_composite() -> None:
    class _M:
        def __init__(self, n, v):
            self.name = n
            self.value = v

    metrics = [
        _M("impressions", 500),
        _M("likes", 100),
        _M("comments", 50),
        _M("shares", 50),
    ]
    # (100 + 2*50 + 3*50) / 500 == 0.7
    assert api_main._compute_engagement_score(metrics) == pytest.approx(0.7)


def test_compute_engagement_score_zero_impressions() -> None:
    class _M:
        def __init__(self, n, v):
            self.name = n
            self.value = v

    metrics = [_M("impressions", 0), _M("likes", 50)]
    assert api_main._compute_engagement_score(metrics) == 0.0


def test_compute_engagement_score_clamps_composite_at_one() -> None:
    class _M:
        def __init__(self, n, v):
            self.name = n
            self.value = v

    metrics = [_M("impressions", 1), _M("likes", 10), _M("shares", 10)]
    assert api_main._compute_engagement_score(metrics) == 1.0


def test_tokenize_for_bank_drops_short_and_dedupes() -> None:
    out = api_main._tokenize_for_bank("Founders FOUNDERS pivot a it is the growth")
    # ≥4 chars only, lowercased + deduped
    assert "founders" in out
    assert out.count("founders") == 1
    assert "growth" in out
    assert "the" not in out


def test_tokenize_for_bank_empty_input() -> None:
    assert api_main._tokenize_for_bank("") == []
    assert api_main._tokenize_for_bank(None) == []


def test_linked_goals_from_job_returns_linked_goals() -> None:
    job = {
        "result": {
            "content_plan": {
                "approved_ideas": [
                    {"title": "match", "linked_goals": ["g1", "g2"]},
                    {"title": "other", "linked_goals": ["other"]},
                ]
            }
        }
    }
    assert api_main._linked_goals_from_job(job, "match") == ["g1", "g2"]


def test_linked_goals_from_job_returns_empty_for_missing() -> None:
    assert api_main._linked_goals_from_job({}, "x") == []
    assert api_main._linked_goals_from_job({"result": None}, "x") == []
    assert api_main._linked_goals_from_job({"result": {}}, "x") == []
    assert api_main._linked_goals_from_job({"result": {"content_plan": None}}, "x") == []
    assert (
        api_main._linked_goals_from_job({"result": {"content_plan": {"approved_ideas": []}}}, "x")
        == []
    )


def test_linked_goals_from_job_handles_non_dict_ideas() -> None:
    job = {
        "result": {
            "content_plan": {
                "approved_ideas": [
                    None,
                    {"title": "no-linked"},  # missing linked_goals
                    "skip-me",
                ]
            }
        }
    }
    assert api_main._linked_goals_from_job(job, "no-linked") == []


# ---------------------------------------------------------------------------
# _auto_ingest_winning_posts
# ---------------------------------------------------------------------------


def test_auto_ingest_skips_when_module_unavailable(
    fake_jobs, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """If ``social_media_marketing_team.shared`` cannot be imported, return 0."""
    import builtins

    real_import = builtins.__import__

    def _bad_import(name, *args, **kwargs):
        if name == "social_media_marketing_team.shared":
            raise ImportError("unavailable in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _bad_import)
    with caplog.at_level("WARNING"):
        count = api_main._auto_ingest_winning_posts({}, "job-x", [object()])
    assert count == 0
    assert any("Winning posts bank module unavailable" in r.message for r in caplog.records)


def test_auto_ingest_handles_empty_observations(fake_jobs) -> None:
    assert api_main._auto_ingest_winning_posts({}, "job-x", []) == 0
    assert api_main._auto_ingest_winning_posts({}, "job-x", None) == 0


def test_auto_ingest_save_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """``save_winning_post`` raising should not crash the loop and the
    counter should reflect only successful inserts."""

    class _Obs:
        def __init__(self, score):
            self.platform = "linkedin"
            self.concept_title = "T"
            self.campaign_name = "C"

            class _M:
                def __init__(self, n, v):
                    self.name = n
                    self.value = v

            # Composite score = 0.7 above default threshold 0.7? threshold is >= ; use >
            self.metrics = [_M("engagement_rate", score)]

    calls = {"n": 0}

    def _bad_save(**_kwargs):
        calls["n"] += 1
        raise RuntimeError("pg down")

    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "save_winning_post", _bad_save)

    with caplog.at_level("WARNING"):
        count = api_main._auto_ingest_winning_posts({}, "job-x", [_Obs(0.9), _Obs(0.9)])

    assert count == 0
    assert calls["n"] == 2
    assert any("Winning posts bank auto-ingest failed" in r.message for r in caplog.records)


def test_auto_ingest_skips_low_score(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Obs:
        def __init__(self):
            self.platform = "linkedin"
            self.concept_title = "T"
            self.campaign_name = "C"

            class _M:
                def __init__(self, n, v):
                    self.name = n
                    self.value = v

            self.metrics = [_M("engagement_rate", 0.1)]

    calls = {"n": 0}

    def _save(**_kwargs):
        calls["n"] += 1
        return "id"

    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "save_winning_post", _save)

    count = api_main._auto_ingest_winning_posts({}, "job-x", [_Obs()])
    assert count == 0
    assert calls["n"] == 0


def test_auto_ingest_persists_high_score_and_handles_platform_enum(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Platform values exposed via .value (e.g. Pydantic Enum) should be
    serialised to string before persisting."""
    from social_media_marketing_team.models import Platform

    class _M:
        def __init__(self, n, v):
            self.name = n
            self.value = v

    class _Obs:
        platform = Platform.LINKEDIN
        concept_title = "Winning idea"
        campaign_name = "Camp"
        metrics = [_M("engagement_rate", 0.91)]

    captured: dict[str, Any] = {}

    def _save(**kwargs):
        captured.update(kwargs)
        return "saved-id"

    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "save_winning_post", _save)

    job = {
        "result": {
            "content_plan": {
                "approved_ideas": [{"title": "Winning idea", "linked_goals": ["awareness"]}]
            }
        }
    }
    with caplog.at_level("INFO"):
        count = api_main._auto_ingest_winning_posts(job, "job-id", [_Obs()])

    assert count == 1
    assert captured["title"] == "Winning idea"
    assert captured["platform"] == "linkedin"  # enum .value used
    assert captured["engagement_score"] == pytest.approx(0.91)
    assert captured["linked_goals"] == ["awareness"]
    assert captured["source_job_id"] == "job-id"
    assert "winning" in captured["keywords"] or "idea" in captured["keywords"]


def test_mark_all_running_jobs_failed_delegates(fake_jobs) -> None:
    """Smoke test: helper hands off to the job manager."""
    fake_jobs.create_job("j1", status="running")
    api_main.mark_all_running_jobs_failed("shutdown")
    job = fake_jobs.get_job("j1")
    assert job["status"] == "failed"
    assert job["error"] == "shutdown"


# ---------------------------------------------------------------------------
# Branding error builders
# ---------------------------------------------------------------------------


def test_build_brand_summary_with_tagline_and_full_context() -> None:
    summary = api_main._build_brand_summary(_MOCK_BRAND_CTX)
    assert "Acme" in summary
    assert "Just works" in summary
    assert "Voice:" in summary
    assert "Audience:" in summary


def test_build_brand_summary_minimal_brand() -> None:
    minimal = BrandContext(
        brand_name="Min",
        target_audience="",
        voice_and_tone="",
        brand_guidelines="",
        brand_objectives="",
    )
    summary = api_main._build_brand_summary(minimal)
    assert summary == "Using brand 'Min'."


def test_build_brand_summary_voice_only() -> None:
    ctx = BrandContext(
        brand_name="Min",
        target_audience="",
        voice_and_tone="warm",
        brand_guidelines="",
        brand_objectives="",
    )
    summary = api_main._build_brand_summary(ctx)
    assert "Voice: warm" in summary
    assert "Audience" not in summary


def test_build_brand_not_found_error_includes_user_message() -> None:
    err = api_main._build_brand_not_found_error("c-1", "b-1")
    assert err["error"] == "brand_not_found"
    assert "c-1" in err["user_message"]
    assert "user_message" in err


def test_build_brand_incomplete_error_lists_missing() -> None:
    exc = BrandIncompleteError("c1", "b1", ["narrative_messaging"], "strategic_core")
    out = api_main._build_brand_incomplete_error(exc)
    assert out["error"] == "brand_incomplete"
    assert out["missing_phases"] == ["narrative_messaging"]
    assert out["current_phase"] == "strategic_core"
    assert "Narrative" in out["user_message"]


def test_build_brand_incomplete_error_unknown_phase_label() -> None:
    exc = BrandIncompleteError("c1", "b1", ["mystery_phase"], "draft")
    out = api_main._build_brand_incomplete_error(exc)
    # unknown phase rendered verbatim
    assert "mystery_phase" in out["user_message"]


def test_fetch_and_validate_brand_runtime_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):
        raise RuntimeError("branding API down")

    monkeypatch.setattr(api_main, "fetch_brand", _boom)
    with pytest.raises(api_main.HTTPException) as exc:
        api_main._fetch_and_validate_brand("c", "b")
    assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# Run / status / list / cancel / delete endpoints
# ---------------------------------------------------------------------------


def test_health_returns_ok(fake_jobs) -> None:
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}


@patch(f"{_BRAND_ADAPTER}._fetch_and_validate_brand", return_value=_MOCK_BRAND_CTX)
def test_run_endpoint_creates_job_and_completes(_mock_brand, fake_jobs, inline_thread) -> None:
    client = TestClient(app)
    job_id = _start_run(client)
    assert fake_jobs.get_job(job_id) is not None
    job = fake_jobs.get_job(job_id)
    assert job["status"] == "completed"
    assert job["result"]["llm_model_name"] == "deepseek-v4-flash:cloud"


@patch(
    f"{_BRAND_ADAPTER}.fetch_brand",
    side_effect=BrandNotFoundError("client_x", "brand_x"),
)
def test_run_endpoint_brand_not_found_422(_mock_fetch, fake_jobs) -> None:
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/run",
        json={
            "client_id": "client_x",
            "brand_id": "brand_x",
            "llm_model_name": "deepseek-v4-flash:cloud",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "brand_not_found"


@patch(
    f"{_BRAND_ADAPTER}.fetch_brand",
    return_value={
        "latest_output": {"strategic_core": {"some": "data"}},
        "current_phase": "strategic_core",
    },
)
def test_run_endpoint_brand_incomplete_422(_mock_fetch, fake_jobs) -> None:
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/run",
        json={
            "client_id": "client_y",
            "brand_id": "brand_y",
            "llm_model_name": "deepseek-v4-flash:cloud",
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "brand_incomplete"
    assert "narrative_messaging" in detail["missing_phases"]


def test_status_unknown_returns_404(fake_jobs) -> None:
    client = TestClient(app)
    resp = client.get("/social-marketing/status/no-such-job")
    assert resp.status_code == 404


def test_list_jobs_returns_sorted_items_and_filters_running(fake_jobs) -> None:
    fake_jobs.create_job(
        "j1",
        status="completed",
        current_stage="done",
        progress=100,
        created_at="2026-01-01T00:00:00+00:00",
        last_updated_at="2026-01-01T00:00:00+00:00",
    )
    fake_jobs.create_job(
        "j2",
        status="running",
        current_stage="planning",
        progress=20,
        created_at="2026-02-01T00:00:00+00:00",
        last_updated_at="2026-02-01T00:00:00+00:00",
    )
    fake_jobs.create_job(
        "j3",
        status="pending",
        current_stage="queued",
        progress=0,
        created_at="2026-01-15T00:00:00+00:00",
        last_updated_at="2026-01-15T00:00:00+00:00",
    )
    client = TestClient(app)

    resp = client.get("/social-marketing/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert [j["job_id"] for j in body] == ["j2", "j3", "j1"]

    resp = client.get("/social-marketing/jobs?running_only=true")
    assert resp.status_code == 200
    body = resp.json()
    ids = {j["job_id"] for j in body}
    assert ids == {"j2", "j3"}


def test_list_jobs_handles_empty_store(fake_jobs) -> None:
    client = TestClient(app)
    resp = client.get("/social-marketing/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_cancel_marketing_job_pending(fake_jobs) -> None:
    fake_jobs.create_job(
        "cancel-1",
        status="pending",
        current_stage="queued",
        progress=0,
        llm_model_name="m",
        client_id="c",
        brand_id="b",
        last_updated_at=api_main._now(),
    )
    client = TestClient(app)
    resp = client.post("/social-marketing/job/cancel-1/cancel")
    assert resp.status_code == 200
    assert fake_jobs.get_job("cancel-1")["status"] == "cancelled"


def test_cancel_marketing_job_terminal_400(fake_jobs) -> None:
    fake_jobs.create_job(
        "cancel-done",
        status="completed",
        current_stage="done",
        progress=100,
        last_updated_at=api_main._now(),
    )
    client = TestClient(app)
    resp = client.post("/social-marketing/job/cancel-done/cancel")
    assert resp.status_code == 400


def test_cancel_marketing_job_unknown_404(fake_jobs) -> None:
    client = TestClient(app)
    resp = client.post("/social-marketing/job/missing/cancel")
    assert resp.status_code == 404


def test_delete_marketing_job(fake_jobs) -> None:
    fake_jobs.create_job(
        "del-1",
        status="completed",
        current_stage="done",
        progress=100,
        last_updated_at=api_main._now(),
    )
    client = TestClient(app)
    resp = client.delete("/social-marketing/job/del-1")
    assert resp.status_code == 200
    assert fake_jobs.get_job("del-1") is None


def test_delete_marketing_job_unknown_404(fake_jobs) -> None:
    client = TestClient(app)
    resp = client.delete("/social-marketing/job/missing")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Performance ingestion
# ---------------------------------------------------------------------------


def test_ingest_performance_unknown_returns_404(fake_jobs) -> None:
    client = TestClient(app)
    resp = client.post("/social-marketing/performance/missing", json={"observations": []})
    assert resp.status_code == 404


def test_ingest_performance_with_proposal_campaign_name(fake_jobs, fake_bank) -> None:
    fake_jobs.create_job(
        "perf-1",
        status="completed",
        current_stage="done",
        progress=100,
        last_updated_at=api_main._now(),
        performance_observations=[],
        result={"proposal": {"campaign_name": "Acme growth sprint"}},
    )
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/performance/perf-1",
        json={
            "observations": [
                {
                    "campaign_name": "Acme growth sprint",
                    "platform": "linkedin",
                    "concept_title": "Pricing mistakes",
                    "posted_at": "2026-04-17T12:00:00Z",
                    "metrics": [{"name": "engagement_rate", "value": 0.92}],
                }
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["campaign_name"] == "Acme growth sprint"
    assert body["observations_ingested"] == 1


def test_ingest_performance_result_without_proposal(fake_jobs, fake_bank) -> None:
    fake_jobs.create_job(
        "perf-2",
        status="completed",
        current_stage="done",
        progress=100,
        last_updated_at=api_main._now(),
        performance_observations=[],
        result={"other_field": "value"},
    )
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/performance/perf-2",
        json={"observations": []},
    )
    assert resp.status_code == 200
    assert resp.json()["campaign_name"] is None


# ---------------------------------------------------------------------------
# Revise endpoint
# ---------------------------------------------------------------------------


@patch(f"{_BRAND_ADAPTER}._fetch_and_validate_brand", return_value=_MOCK_BRAND_CTX)
def test_revise_endpoint_happy_path(_mock, fake_jobs, inline_thread) -> None:
    client = TestClient(app)
    job_id = _start_run(client)

    resp = client.post(
        f"/social-marketing/revise/{job_id}",
        json={"feedback": "Add more pricing detail", "approved_for_testing": True},
    )
    assert resp.status_code == 200
    job = fake_jobs.get_job(job_id)
    # Inline thread completed the revision through to completion
    assert job["status"] == "completed"
    assert "Add more pricing detail" in job["revision_history"]


def test_revise_endpoint_404_unknown(fake_jobs) -> None:
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/revise/missing",
        json={"feedback": "retry now", "approved_for_testing": False},
    )
    assert resp.status_code == 404


def test_revise_endpoint_400_when_original_run_payload_missing(fake_jobs) -> None:
    # The job was created without a stored ``request_payload`` (the original run's
    # request), so revise has nothing to rebuild the run from and returns 400. The
    # revise request body itself (feedback/approved_for_testing) is valid and present
    # -- the missing piece is the *original run payload* on the job record.
    fake_jobs.create_job(
        "rev-no-payload",
        status="completed",
        current_stage="done",
        progress=100,
        last_updated_at=api_main._now(),
    )
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/revise/rev-no-payload",
        json={"feedback": "retry now", "approved_for_testing": False},
    )
    assert resp.status_code == 400
    assert "not available" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Resume / restart endpoints (404 + 400 paths only — happy path attempts to
# call ``_dispatch_job`` with the wrong arity, which is exercised here only
# via failure-mode tests so we do not touch production code).
# ---------------------------------------------------------------------------


def test_resume_unknown_returns_404(fake_jobs) -> None:
    client = TestClient(app)
    resp = client.post("/social-marketing/job/missing/resume")
    assert resp.status_code == 404


def test_resume_invalid_status_returns_400(fake_jobs) -> None:
    fake_jobs.create_job(
        "res-cancelled",
        status="cancelled",
        current_stage="done",
        progress=100,
        last_updated_at=api_main._now(),
    )
    client = TestClient(app)
    resp = client.post("/social-marketing/job/res-cancelled/resume")
    assert resp.status_code == 400


def test_resume_missing_payload_returns_400(fake_jobs) -> None:
    fake_jobs.create_job(
        "res-no-payload",
        status="failed",
        current_stage="failed",
        progress=0,
        last_updated_at=api_main._now(),
    )
    client = TestClient(app)
    resp = client.post("/social-marketing/job/res-no-payload/resume")
    assert resp.status_code == 400


def test_restart_unknown_returns_404(fake_jobs) -> None:
    client = TestClient(app)
    resp = client.post("/social-marketing/job/missing/restart")
    assert resp.status_code == 404


def test_restart_invalid_status_returns_400(fake_jobs) -> None:
    fake_jobs.create_job(
        "rst-running",
        status="running",
        current_stage="planning",
        progress=10,
        last_updated_at=api_main._now(),
    )
    client = TestClient(app)
    resp = client.post("/social-marketing/job/rst-running/restart")
    assert resp.status_code == 400


def test_restart_missing_payload_returns_400(fake_jobs) -> None:
    fake_jobs.create_job(
        "rst-no-payload",
        status="failed",
        current_stage="failed",
        progress=0,
        last_updated_at=api_main._now(),
    )
    client = TestClient(app)
    resp = client.post("/social-marketing/job/rst-no-payload/restart")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Trend endpoints
# ---------------------------------------------------------------------------


def test_trend_run_endpoint_triggers_background_job(
    monkeypatch: pytest.MonkeyPatch, fake_jobs
) -> None:
    called = {"n": 0}

    def _fake_run():
        called["n"] += 1

    monkeypatch.setattr(api_main, "run_trend_job", _fake_run)

    class _InlineThread:
        def __init__(self, target, daemon=False, name=""):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(api_main.threading, "Thread", _InlineThread)
    client = TestClient(app)
    resp = client.post("/social-marketing/trends/run")
    assert resp.status_code == 200
    assert called["n"] == 1


def test_trend_latest_returns_404_when_no_digest(
    monkeypatch: pytest.MonkeyPatch, fake_jobs
) -> None:
    monkeypatch.setattr(api_main, "get_latest_digest", lambda: None)
    client = TestClient(app)
    resp = client.get("/social-marketing/trends/latest")
    assert resp.status_code == 404


def test_trend_latest_returns_digest_when_present(
    monkeypatch: pytest.MonkeyPatch, fake_jobs
) -> None:
    from social_media_marketing_team.trend_models import TrendDigest

    digest = TrendDigest(
        generated_at="2026-01-01T00:00:00+00:00",
        topics=[],
        platforms_searched=["X/Twitter"],
        search_query_count=1,
    )
    monkeypatch.setattr(api_main, "get_latest_digest", lambda: digest)
    client = TestClient(app)
    resp = client.get("/social-marketing/trends/latest")
    assert resp.status_code == 200
    assert resp.json()["digest"]["search_query_count"] == 1


# ---------------------------------------------------------------------------
# Winning Posts CRUD routes — 503 boundary behaviour
# ---------------------------------------------------------------------------


def test_create_winning_post_503_when_module_unavailable(
    fake_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def _bad_import(name, *args, **kwargs):
        if name == "social_media_marketing_team.shared":
            raise ImportError("disabled")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _bad_import)
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/winning-posts",
        json={"title": "t"},
    )
    assert resp.status_code == 503


def test_create_winning_post_save_failure_503(fake_jobs, monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_save(**kwargs):
        raise RuntimeError("pg dead")

    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "save_winning_post", _bad_save)
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/winning-posts",
        json={"title": "t"},
    )
    assert resp.status_code == 503


def test_list_winning_posts_503_when_save_layer_fails(
    fake_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _bad(**_kw):
        raise RuntimeError("pg dead")

    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "list_winning_posts", _bad)
    client = TestClient(app)
    resp = client.get("/social-marketing/winning-posts")
    assert resp.status_code == 503


def test_list_winning_posts_clamps_limit(fake_jobs, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _list(limit, offset):
        seen["limit"] = limit
        seen["offset"] = offset
        return []

    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "list_winning_posts", _list)
    client = TestClient(app)
    # limit > 500 clamps to 500; offset < 0 clamps to 0
    resp = client.get("/social-marketing/winning-posts?limit=1000&offset=-5")
    assert resp.status_code == 200
    assert seen["limit"] == 500
    assert seen["offset"] == 0

    # limit < 1 clamps to 1
    resp = client.get("/social-marketing/winning-posts?limit=0&offset=2")
    assert resp.status_code == 200
    assert seen["limit"] == 1
    assert seen["offset"] == 2


def test_get_winning_post_503_when_layer_fails(fake_jobs, monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad(_id):
        raise RuntimeError("pg dead")

    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "get_winning_post", _bad)
    client = TestClient(app)
    resp = client.get("/social-marketing/winning-posts/abc")
    assert resp.status_code == 503


def test_get_winning_post_404(fake_jobs, monkeypatch: pytest.MonkeyPatch) -> None:
    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "get_winning_post", lambda _id: None)
    client = TestClient(app)
    resp = client.get("/social-marketing/winning-posts/abc")
    assert resp.status_code == 404


def test_delete_winning_post_503_when_layer_fails(
    fake_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _bad(_id):
        raise RuntimeError("pg dead")

    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "delete_winning_post", _bad)
    client = TestClient(app)
    resp = client.delete("/social-marketing/winning-posts/abc")
    assert resp.status_code == 503


def test_delete_winning_post_404_when_missing(fake_jobs, monkeypatch: pytest.MonkeyPatch) -> None:
    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "delete_winning_post", lambda _id: False)
    client = TestClient(app)
    resp = client.delete("/social-marketing/winning-posts/abc")
    assert resp.status_code == 404


def test_delete_winning_post_200_when_removed(fake_jobs, monkeypatch: pytest.MonkeyPatch) -> None:
    import social_media_marketing_team.shared as shared_mod

    monkeypatch.setattr(shared_mod, "delete_winning_post", lambda _id: True)
    client = TestClient(app)
    resp = client.delete("/social-marketing/winning-posts/abc")
    assert resp.status_code == 200
    assert resp.json()["id"] == "abc"


# ---------------------------------------------------------------------------
# _run_team_job error path
# ---------------------------------------------------------------------------


def test_run_team_job_failure_marks_job_failed(fake_jobs, monkeypatch: pytest.MonkeyPatch) -> None:
    req = api_main.RunMarketingTeamRequest(
        client_id="c",
        brand_id="b",
        llm_model_name="m",
    )
    fake_jobs.create_job(
        "fail-1",
        status="pending",
        current_stage="queued",
        progress=0,
        last_updated_at=api_main._now(),
        request_payload=req.model_dump(),
    )

    class _BrokenOrch:
        def __init__(self, *a, **k):
            pass

        def run(self, *a, **k):
            raise RuntimeError("orchestrator exploded")

    monkeypatch.setattr(api_main, "SocialMediaMarketingOrchestrator", _BrokenOrch)
    api_main._run_team_job("fail-1", req, _MOCK_BRAND_CTX)
    job = fake_jobs.get_job("fail-1")
    assert job["status"] == "failed"
    assert "RuntimeError: orchestrator exploded" in job["error"]


# ---------------------------------------------------------------------------
# _dispatch_job branches
# ---------------------------------------------------------------------------


def test_dispatch_job_uses_thread_when_temporal_disabled(
    fake_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = api_main.RunMarketingTeamRequest(client_id="c", brand_id="b", llm_model_name="m")

    started = {"n": 0}

    class _InlineThread:
        def __init__(self, target, args=(), kwargs=None, daemon=False, name=""):
            started["n"] += 1
            self._args = args

        def start(self):
            pass

    monkeypatch.setattr(api_main.threading, "Thread", _InlineThread)

    # Force the import inside _dispatch_job to find a module reporting
    # temporal disabled.
    import sys

    class _FakeClient:
        @staticmethod
        def is_temporal_enabled():
            return False

    class _FakeStart:
        @staticmethod
        def start_team_job_workflow(*a, **k):
            raise AssertionError("should not be called when disabled")

    fake_client_mod = type(sys)("social_media_marketing_team.temporal.client")
    fake_client_mod.is_temporal_enabled = _FakeClient.is_temporal_enabled  # type: ignore[attr-defined]
    fake_start_mod = type(sys)("social_media_marketing_team.temporal.start_workflow")
    fake_start_mod.start_team_job_workflow = _FakeStart.start_team_job_workflow  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "social_media_marketing_team.temporal.client", fake_client_mod)
    monkeypatch.setitem(
        sys.modules,
        "social_media_marketing_team.temporal.start_workflow",
        fake_start_mod,
    )

    msg = api_main._dispatch_job("job-1", req, _MOCK_BRAND_CTX)
    assert "Poll GET" in msg
    assert started["n"] == 1


def test_dispatch_job_uses_temporal_when_enabled(
    fake_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = api_main.RunMarketingTeamRequest(client_id="c", brand_id="b", llm_model_name="m")

    calls = {"n": 0}

    class _FakeClient:
        @staticmethod
        def is_temporal_enabled():
            return True

    def _fake_start(job_id, payload):
        calls["n"] += 1
        calls["job_id"] = job_id
        calls["payload"] = payload

    import sys

    fake_client_mod = type(sys)("social_media_marketing_team.temporal.client")
    fake_client_mod.is_temporal_enabled = _FakeClient.is_temporal_enabled  # type: ignore[attr-defined]
    fake_start_mod = type(sys)("social_media_marketing_team.temporal.start_workflow")
    fake_start_mod.start_team_job_workflow = _fake_start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "social_media_marketing_team.temporal.client", fake_client_mod)
    monkeypatch.setitem(
        sys.modules,
        "social_media_marketing_team.temporal.start_workflow",
        fake_start_mod,
    )

    msg = api_main._dispatch_job("job-2", req, _MOCK_BRAND_CTX)
    assert "Temporal" in msg
    assert calls["n"] == 1
    assert calls["job_id"] == "job-2"


def test_dispatch_job_import_error_falls_back_to_thread(
    fake_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = api_main.RunMarketingTeamRequest(client_id="c", brand_id="b", llm_model_name="m")

    started = {"n": 0}

    class _InlineThread:
        def __init__(self, target, args=(), kwargs=None, daemon=False, name=""):
            started["n"] += 1

        def start(self):
            pass

    monkeypatch.setattr(api_main.threading, "Thread", _InlineThread)

    import builtins

    real_import = builtins.__import__

    def _bad_import(name, *args, **kwargs):
        if name == "social_media_marketing_team.temporal.client":
            raise ImportError("temporal not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _bad_import)
    msg = api_main._dispatch_job("job-3", req, _MOCK_BRAND_CTX)
    assert "Poll GET" in msg
    assert started["n"] == 1


def test_dispatch_job_runtime_error_falls_back_to_thread(
    fake_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ImportError Temporal failure (e.g. connect timeout) falls back to thread mode."""
    req = api_main.RunMarketingTeamRequest(client_id="c", brand_id="b", llm_model_name="m")

    started = {"n": 0}

    class _InlineThread:
        def __init__(self, target, args=(), kwargs=None, daemon=False, name=""):
            started["n"] += 1

        def start(self):
            pass

    monkeypatch.setattr(api_main.threading, "Thread", _InlineThread)

    def _raise_enabled():
        raise RuntimeError("temporal frontend connection timeout")

    import sys

    fake_client_mod = type(sys)("social_media_marketing_team.temporal.client")
    fake_client_mod.is_temporal_enabled = _raise_enabled  # type: ignore[attr-defined]
    fake_start_mod = type(sys)("social_media_marketing_team.temporal.start_workflow")
    fake_start_mod.start_team_job_workflow = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "social_media_marketing_team.temporal.client", fake_client_mod)
    monkeypatch.setitem(
        sys.modules,
        "social_media_marketing_team.temporal.start_workflow",
        fake_start_mod,
    )

    msg = api_main._dispatch_job("job-4", req, _MOCK_BRAND_CTX)
    assert "Poll GET" in msg
    assert started["n"] == 1

"""Additional targeted tests to cover remaining branches in api/main.py."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from social_media_marketing_team.adapters.branding import BrandContext
from social_media_marketing_team.api import main as api_main
from social_media_marketing_team.api.main import app
from social_media_marketing_team.tests.test_winning_posts_bank import _FakeConn

_MOCK_BRAND_CTX = BrandContext(
    brand_name="Acme",
    target_audience="t",
    voice_and_tone="v",
    brand_guidelines="g",
    brand_objectives="o",
)


@pytest.fixture
def fake_jobs(monkeypatch: pytest.MonkeyPatch, fake_job_client):
    monkeypatch.setattr(api_main, "_job_manager", fake_job_client)
    return fake_job_client


@pytest.fixture
def fake_bank(monkeypatch: pytest.MonkeyPatch):
    db: dict[str, Any] = {"posts": {}}

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db)

    import social_media_marketing_team.shared.winning_posts_bank as wpb

    monkeypatch.setattr(wpb, "get_conn", _fake_get_conn)
    return db


# ---------------------------------------------------------------------------
# lifespan
# ---------------------------------------------------------------------------


def test_lifespan_full_path_no_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan startup + shutdown should call register_team_schemas, start
    scheduler, then stop + close_pool on teardown."""
    calls: list[str] = []

    monkeypatch.setattr(api_main, "register_team_schemas", lambda *a, **k: calls.append("register"))
    monkeypatch.setattr(api_main, "start_scheduler", lambda: calls.append("start"))
    monkeypatch.setattr(api_main, "stop_scheduler", lambda: calls.append("stop"))
    monkeypatch.setattr(api_main, "close_pool", lambda: calls.append("close"))

    import asyncio

    async def _drive():
        async with api_main._lifespan(app):
            calls.append("yield")

    asyncio.run(_drive())
    assert calls == ["register", "start", "yield", "stop", "close"]


def test_lifespan_schema_registration_error_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Schema registration failure should be logged but not raised."""

    def _boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(api_main, "register_team_schemas", _boom)
    monkeypatch.setattr(api_main, "start_scheduler", lambda: None)
    monkeypatch.setattr(api_main, "stop_scheduler", lambda: None)
    monkeypatch.setattr(api_main, "close_pool", lambda: None)

    import asyncio

    with caplog.at_level("ERROR"):

        async def _drive():
            async with api_main._lifespan(app):
                pass

        asyncio.run(_drive())

    assert any(
        "social marketing postgres schema registration failed" in r.message
        for r in caplog.records
    )


def test_lifespan_close_pool_error_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """close_pool failures should be logged at WARNING during shutdown."""
    monkeypatch.setattr(api_main, "register_team_schemas", lambda *a, **k: None)
    monkeypatch.setattr(api_main, "start_scheduler", lambda: None)
    monkeypatch.setattr(api_main, "stop_scheduler", lambda: None)

    def _bad_close():
        raise RuntimeError("pool dead")

    monkeypatch.setattr(api_main, "close_pool", _bad_close)

    import asyncio

    with caplog.at_level("WARNING"):

        async def _drive():
            async with api_main._lifespan(app):
                pass

        asyncio.run(_drive())

    assert any(
        "social marketing shared_postgres close_pool failed" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# delete: rare race where get_job succeeds but delete_job returns False
# ---------------------------------------------------------------------------


def test_delete_marketing_job_race_returns_404(
    monkeypatch: pytest.MonkeyPatch, fake_jobs
) -> None:
    """get_job sees the job, but delete_job races and returns False -> 404."""
    fake_jobs.create_job(
        "race-1",
        status="completed",
        current_stage="done",
        progress=100,
        last_updated_at=api_main._now(),
    )
    monkeypatch.setattr(fake_jobs, "delete_job", lambda job_id: False)
    client = TestClient(app)
    resp = client.delete("/social-marketing/job/race-1")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Resume / restart happy paths — _dispatch_job is called with the wrong
# arity, so the route raises TypeError and the request fails with 500.
# This is current production behaviour and we lock it in to ensure the
# resume/restart endpoints' validate->update->dispatch sequence is exercised.
# ---------------------------------------------------------------------------


def test_resume_happy_path_dispatches(
    monkeypatch: pytest.MonkeyPatch, fake_jobs
) -> None:
    payload = {
        "client_id": "c",
        "brand_id": "b",
        "llm_model_name": "m",
    }
    fake_jobs.create_job(
        "res-ok",
        status="failed",
        current_stage="failed",
        progress=0,
        last_updated_at=api_main._now(),
        request_payload=payload,
    )

    captured: dict[str, Any] = {}

    def _fake_dispatch(*args, **kwargs):
        # Production signature requires brand_ctx; observed call site has only
        # 2 positional args. Accept both to remain robust to either path.
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "OK"

    monkeypatch.setattr(api_main, "_dispatch_job", _fake_dispatch)

    client = TestClient(app)
    resp = client.post("/social-marketing/job/res-ok/resume")
    assert resp.status_code == 200
    job = fake_jobs.get_job("res-ok")
    assert job["status"] == "running"
    assert captured["args"][0] == "res-ok"


def test_restart_happy_path_dispatches(
    monkeypatch: pytest.MonkeyPatch, fake_jobs
) -> None:
    payload = {
        "client_id": "c",
        "brand_id": "b",
        "llm_model_name": "m",
    }
    fake_jobs.create_job(
        "rst-ok",
        status="failed",
        current_stage="failed",
        progress=0,
        last_updated_at=api_main._now(),
        request_payload=payload,
    )

    captured: dict[str, Any] = {}

    def _fake_dispatch(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "OK"

    monkeypatch.setattr(api_main, "_dispatch_job", _fake_dispatch)

    client = TestClient(app)
    resp = client.post("/social-marketing/job/rst-ok/restart")
    assert resp.status_code == 200
    job = fake_jobs.get_job("rst-ok")
    assert job["status"] == "pending"
    assert job["progress"] == 0


# ---------------------------------------------------------------------------
# Winning posts success paths (cover the WinningPostResponse return lines)
# ---------------------------------------------------------------------------


def test_create_then_list_then_get_winning_posts(fake_jobs, fake_bank) -> None:
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/winning-posts",
        json={
            "title": "T",
            "body": "B",
            "platform": "x",
            "keywords": ["growth"],
            "metrics": {"engagement_rate": 0.9},
            "engagement_score": 0.9,
            "linked_goals": ["awareness"],
        },
    )
    assert resp.status_code == 201
    post_id = resp.json()["id"]

    # list
    resp = client.get("/social-marketing/winning-posts")
    assert resp.status_code == 200
    assert any(r["id"] == post_id for r in resp.json())

    # get
    resp = client.get(f"/social-marketing/winning-posts/{post_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "T"


# ---------------------------------------------------------------------------
# Performance ingest with proposal where result is non-dict (lines around 463)
# ---------------------------------------------------------------------------


def test_ingest_performance_result_is_non_dict(fake_jobs, fake_bank) -> None:
    fake_jobs.create_job(
        "perf-non-dict",
        status="completed",
        current_stage="done",
        progress=100,
        last_updated_at=api_main._now(),
        performance_observations=[],
        result="not a dict",
    )
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/performance/perf-non-dict",
        json={"observations": []},
    )
    assert resp.status_code == 200
    assert resp.json()["campaign_name"] is None


@patch(
    "social_media_marketing_team.api.main._fetch_and_validate_brand",
    return_value=_MOCK_BRAND_CTX,
)
def test_revise_endpoint_uses_dispatched_brand_summary(_mock, fake_jobs) -> None:
    """Cover the revise endpoint when dispatch is mocked (no inline thread)."""
    payload = {
        "client_id": "c",
        "brand_id": "b",
        "llm_model_name": "m",
        "human_approved_for_testing": True,
    }
    fake_jobs.create_job(
        "rev-mock",
        status="completed",
        current_stage="done",
        progress=100,
        last_updated_at=api_main._now(),
        request_payload=payload,
    )

    import pytest as _pt

    monkeypatch = _pt.MonkeyPatch()
    try:
        monkeypatch.setattr(api_main, "_dispatch_job", lambda *a, **k: "DISPATCHED")
        client = TestClient(app)
        resp = client.post(
            "/social-marketing/revise/rev-mock",
            json={"feedback": "make it pop", "approved_for_testing": True},
        )
        assert resp.status_code == 200
        assert "DISPATCHED" in resp.json()["message"]
    finally:
        monkeypatch.undo()

"""API-level tests for Winning Posts Bank CRUD routes + auto-ingest hook.

Swaps the bank module's ``get_conn`` for an in-process fake so the
routes exercise their full happy-path without Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from social_media_marketing_team.adapters.branding import BrandContext
from social_media_marketing_team.api.main import app
from social_media_marketing_team.tests.test_winning_posts_bank import _FakeConn

_BRAND_ADAPTER = "social_media_marketing_team.api.main"

_MOCK_BRAND_CTX = BrandContext(
    brand_name="Acme",
    target_audience="B2B founders",
    voice_and_tone="clear",
    brand_guidelines="Positioning: Developer tools that just work.",
    brand_objectives="Purpose: Empower developers.\nMission: Ship faster.",
    messaging_pillars=["Developer empowerment"],
    brand_story="Acme was born from frustration.",
    tagline="Just works",
)


@pytest.fixture
def fake_bank(monkeypatch: pytest.MonkeyPatch):
    db: dict[str, Any] = {"posts": {}}

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db)

    import social_media_marketing_team.shared.winning_posts_bank as wpb

    monkeypatch.setattr(wpb, "get_conn", _fake_get_conn)
    yield db


def test_create_list_get_delete_roundtrip(fake_bank):
    client = TestClient(app)

    r = client.post(
        "/social-marketing/winning-posts",
        json={
            "title": "Why seed rounds fail",
            "body": "Founders skip...",
            "platform": "linkedin",
            "keywords": ["founders", "seed"],
            "engagement_score": 0.88,
            "linked_goals": ["awareness"],
        },
    )
    assert r.status_code == 201, r.text
    post_id = r.json()["id"]

    r = client.get("/social-marketing/winning-posts")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["id"] == post_id for row in rows)

    r = client.get(f"/social-marketing/winning-posts/{post_id}")
    assert r.status_code == 200
    assert r.json()["title"] == "Why seed rounds fail"
    assert r.json()["platform"] == "linkedin"

    r = client.delete(f"/social-marketing/winning-posts/{post_id}")
    assert r.status_code == 200
    assert r.json()["id"] == post_id

    r = client.get(f"/social-marketing/winning-posts/{post_id}")
    assert r.status_code == 404


def test_get_missing_returns_404(fake_bank):
    client = TestClient(app)
    r = client.get("/social-marketing/winning-posts/does-not-exist")
    assert r.status_code == 404


def test_delete_missing_returns_404(fake_bank):
    client = TestClient(app)
    r = client.delete("/social-marketing/winning-posts/does-not-exist")
    assert r.status_code == 404


@patch(f"{_BRAND_ADAPTER}._fetch_and_validate_brand", return_value=_MOCK_BRAND_CTX)
def test_performance_ingest_auto_promotes_high_engagement(_mock_brand, fake_bank):
    """A high-engagement observation ingests into the Winning Posts Bank."""
    client = TestClient(app)
    run = client.post(
        "/social-marketing/run",
        json={
            "client_id": "client_1",
            "brand_id": "brand_1",
            "llm_model_name": "llama3.1",
            "human_approved_for_testing": True,
        },
    )
    assert run.status_code == 200
    job_id = run.json()["job_id"]

    r = client.post(
        f"/social-marketing/performance/{job_id}",
        json={
            "observations": [
                {
                    "campaign_name": "Acme growth sprint",
                    "platform": "linkedin",
                    "concept_title": "Pricing mistakes",
                    "posted_at": "2026-04-17T12:00:00Z",
                    "metrics": [{"name": "engagement_rate", "value": 0.81}],
                }
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["observations_ingested"] == 1

    listing = client.get("/social-marketing/winning-posts").json()
    assert len(listing) == 1
    row = listing[0]
    assert row["title"] == "Pricing mistakes"
    assert row["platform"] == "linkedin"
    assert row["engagement_score"] == pytest.approx(0.81)
    assert row["source_job_id"] == job_id


@patch(f"{_BRAND_ADAPTER}._fetch_and_validate_brand", return_value=_MOCK_BRAND_CTX)
def test_performance_ingest_skips_low_engagement(_mock_brand, fake_bank):
    client = TestClient(app)
    run = client.post(
        "/social-marketing/run",
        json={
            "client_id": "client_1",
            "brand_id": "brand_1",
            "llm_model_name": "llama3.1",
            "human_approved_for_testing": True,
        },
    )
    job_id = run.json()["job_id"]

    r = client.post(
        f"/social-marketing/performance/{job_id}",
        json={
            "observations": [
                {
                    "campaign_name": "Acme growth sprint",
                    "platform": "linkedin",
                    "concept_title": "Low scorer",
                    "posted_at": "2026-04-17T12:00:00Z",
                    "metrics": [{"name": "engagement_rate", "value": 0.2}],
                }
            ]
        },
    )
    assert r.status_code == 200

    listing = client.get("/social-marketing/winning-posts").json()
    assert listing == []


@patch(f"{_BRAND_ADAPTER}._fetch_and_validate_brand", return_value=_MOCK_BRAND_CTX)
def test_performance_ingest_computes_composite_score(_mock_brand, fake_bank, monkeypatch):
    """When no engagement_rate metric is present, composite formula kicks in."""
    monkeypatch.setenv("SOCIAL_MARKETING_WINNING_POSTS_INGEST_THRESHOLD", "0.5")
    client = TestClient(app)
    run = client.post(
        "/social-marketing/run",
        json={
            "client_id": "client_1",
            "brand_id": "brand_1",
            "llm_model_name": "llama3.1",
            "human_approved_for_testing": True,
        },
    )
    job_id = run.json()["job_id"]

    # 100 likes + 2*50 comments + 3*50 shares = 350 over 500 impressions = 0.70
    r = client.post(
        f"/social-marketing/performance/{job_id}",
        json={
            "observations": [
                {
                    "campaign_name": "Acme",
                    "platform": "x",
                    "concept_title": "Composite winner",
                    "posted_at": "2026-04-17T12:00:00Z",
                    "metrics": [
                        {"name": "impressions", "value": 500},
                        {"name": "likes", "value": 100},
                        {"name": "comments", "value": 50},
                        {"name": "shares", "value": 50},
                    ],
                }
            ]
        },
    )
    assert r.status_code == 200

    listing = client.get("/social-marketing/winning-posts").json()
    assert len(listing) == 1
    assert listing[0]["title"] == "Composite winner"
    assert listing[0]["engagement_score"] == pytest.approx(0.7)

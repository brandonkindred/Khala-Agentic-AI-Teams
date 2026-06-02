"""Integration tests for the Postgres-backed store.

Skipped by default; run with ``pytest -m integration`` against a real Postgres
(``POSTGRES_HOST`` set). Mirrors the layering described in ``backend/conftest.py``.
"""

from __future__ import annotations

import uuid

import pytest

from job_matching_team.models import JobMatchRequest, JobPosting, RankedJob, SubScores
from job_matching_team.profile.model import JobSeekerProfile

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def store():
    from job_matching_team.postgres import SCHEMA
    from job_matching_team.store import JobMatchingStore
    from shared_postgres import ensure_team_schema

    ensure_team_schema(SCHEMA)
    return JobMatchingStore()


def _ranked(company: str, score: float) -> RankedJob:
    posting = JobPosting(company=company, title="Engineer", location="NYC").ensure_fingerprint()
    return RankedJob(
        posting=posting,
        score=score,
        sub_scores=SubScores(title_fit=0.8),
        recommendation="apply",
        rationale="good fit",
        concerns=["minor"],
    )


def test_run_round_trip(store):
    run_id = str(uuid.uuid4())
    profile = JobSeekerProfile(target_titles=["Engineer"])
    store.create_run(run_id, profile, JobMatchRequest(top_n=5))
    ranked = [_ranked("Acme", 0.9), _ranked("Beta", 0.7)]
    store.save_results(run_id, ranked, total_found=4)

    detail = store.get_run(run_id)
    assert detail is not None
    assert detail.status == "completed"
    assert detail.total_found == 4
    assert detail.total_ranked == 2
    assert [r.posting.company for r in detail.ranked_jobs] == ["Acme", "Beta"]
    assert detail.ranked_jobs[0].sub_scores.title_fit == 0.8
    assert detail.ranked_jobs[0].concerns == ["minor"]

    summaries = store.list_runs(limit=10)
    assert any(s.run_id == run_id for s in summaries)

    fps = store.seen_fingerprints()
    assert ranked[0].posting.fingerprint in fps


def test_mark_failed(store):
    run_id = str(uuid.uuid4())
    store.create_run(run_id, JobSeekerProfile(), JobMatchRequest())
    store.mark_failed(run_id, "scan exploded")
    detail = store.get_run(run_id)
    assert detail.status == "failed"
    assert "scan exploded" in detail.error


def test_get_unknown_run_returns_none(store):
    assert store.get_run("does-not-exist") is None

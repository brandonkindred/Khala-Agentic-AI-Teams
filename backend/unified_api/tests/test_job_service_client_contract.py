"""Unit tests for the JobServiceClient + FakeJobServiceClient contracts.

Pure unit tests — they do not need a real job service.  They exist to
lock in the behaviours called out by review on PR #360:

* The real client resolves ``JOB_SERVICE_URL`` lazily so a placeholder set
  before module-level construction does not get pinned.
* The fake's stale-job sweep mirrors the real service's exclusion of all
  waiting states (``waiting_for_answers``, ``waiting_for_title_selection``,
  ``waiting_for_story_input``, ``waiting_for_draft_feedback``), not only
  the caller-supplied ``waiting_field``.
* The fake's ``update_job`` is a no-op on a missing job (no auto-create),
  matching the bare UPDATE in ``backend/job_service/db.py:197``.
* The fake's ``heartbeat`` raises ``httpx.HTTPStatusError`` (404) on a
  missing job, matching ``backend/job_service/main.py:184``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from job_service_client import JobServiceClient
from job_service_client_fake import FakeJobServiceClient

# ---------------------------------------------------------------------------
# Lazy URL resolution
# ---------------------------------------------------------------------------


def test_default_base_url_is_resolved_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client built without an explicit base_url should pick up later env changes."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://placeholder.example/")
    client = JobServiceClient(team="x")
    assert client._base_url == "http://placeholder.example"

    monkeypatch.setenv("JOB_SERVICE_URL", "http://real.example:8085/")
    assert client._base_url == "http://real.example:8085"


def test_explicit_base_url_is_sticky(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client built with an explicit base_url should ignore later env changes."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://env.example/")
    client = JobServiceClient(team="x", base_url="http://explicit.example/")
    monkeypatch.setenv("JOB_SERVICE_URL", "http://other.example/")
    assert client._base_url == "http://explicit.example"


def test_construction_raises_when_no_url_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_SERVICE_URL", raising=False)
    with pytest.raises(RuntimeError, match="JOB_SERVICE_URL is not set"):
        JobServiceClient(team="x")


# ---------------------------------------------------------------------------
# Fake stale-job sweep — mirrors production exclusions
# ---------------------------------------------------------------------------


@pytest.fixture
def stale_jobs_setup(fake_job_client: FakeJobServiceClient) -> FakeJobServiceClient:
    """Seed the fake with one job per waiting state, all with stale heartbeats."""
    stale = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
    seed = {
        "answers": "waiting_for_answers",
        "title": "waiting_for_title_selection",
        "story": "waiting_for_story_input",
        "draft": "waiting_for_draft_feedback",
        "running": None,  # not waiting; should be marked failed
    }
    for job_id, waiting_field in seed.items():
        fields: dict = {}
        if waiting_field is not None:
            fields[waiting_field] = True
        fake_job_client.create_job(job_id, status="running", **fields)
        fake_job_client.update_job(job_id, heartbeat=False, last_heartbeat_at=stale)
    return fake_job_client


def test_fake_stale_sweep_excludes_all_waiting_fields(
    stale_jobs_setup: FakeJobServiceClient,
) -> None:
    """Stale-failure must skip every paused-for-user state, regardless of the
    caller-supplied ``waiting_field`` (matches the real Postgres SQL in
    ``backend/job_service/db.py:404-413``)."""
    failed = stale_jobs_setup.mark_stale_active_jobs_failed(
        stale_after_seconds=60.0,
        reason="stale",
        waiting_field="waiting_for_answers",  # explicit; production also adds the rest
    )
    # Only the unpaused 'running' job should be marked failed.
    assert failed == ["running"]

    # All four waiting-state jobs remain in 'running' status.
    for job_id in ("answers", "title", "story", "draft"):
        job = stale_jobs_setup.get_job(job_id)
        assert job is not None
        assert job["status"] == "running", f"{job_id} should still be running"


# ---------------------------------------------------------------------------
# Fake missing-job behaviour — mirrors production endpoints
# ---------------------------------------------------------------------------


def test_fake_update_job_is_noop_on_missing(fake_job_client: FakeJobServiceClient) -> None:
    """``update_job`` on a missing id must not auto-create a row.

    Production runs a bare ``UPDATE … WHERE team=$1 AND job_id=$2`` — no row
    matched ⇒ silent no-op (see ``backend/job_service/db.py:197``).
    """
    fake_job_client.update_job("missing", status="running", repo_path="/x")
    assert fake_job_client.get_job("missing") is None


def test_fake_heartbeat_raises_on_missing(fake_job_client: FakeJobServiceClient) -> None:
    """``heartbeat`` on a missing id must raise ``httpx.HTTPStatusError`` (404).

    Production raises 404 (``backend/job_service/main.py:184``); the real
    ``JobServiceClient.heartbeat`` surfaces that as ``HTTPStatusError`` via
    ``raise_for_status``.  The fake does the same so unit tests can pin the
    same exception type.
    """
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        fake_job_client.heartbeat("missing")
    assert exc_info.value.response.status_code == 404


def test_fake_heartbeat_succeeds_for_existing_job(
    fake_job_client: FakeJobServiceClient,
) -> None:
    fake_job_client.create_job("present", status="running")
    before = fake_job_client.get_job("present")["last_heartbeat_at"]
    # Heartbeat must advance ``last_heartbeat_at`` and not raise.
    fake_job_client.heartbeat("present")
    after = fake_job_client.get_job("present")["last_heartbeat_at"]
    assert after >= before

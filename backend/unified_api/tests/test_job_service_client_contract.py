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

import job_service_client as jsc
from job_service_client import JobServiceClient, get_job_service_client
from job_service_client_fake import FakeJobServiceClient


class _RecordingHttp:
    """Stand-in for httpx.Client that records requests and never hits the network."""

    def __init__(self) -> None:
        self.calls: list = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    def close(self) -> None:  # pragma: no cover - trivial
        pass

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
# Connection pooling + per-team client caching
# ---------------------------------------------------------------------------


def test_get_http_pools_a_single_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """One pooled httpx.Client is reused across requests; close() rebuilds it."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://x.example")
    client = JobServiceClient(team="t")
    h1 = client._get_http()
    h2 = client._get_http()
    assert h1 is h2
    assert isinstance(h1, httpx.Client)
    client.close()
    assert client._http is None
    h3 = client._get_http()
    assert h3 is not h1  # rebuilt lazily after close
    client.close()


def test_request_reuses_pooled_client_and_forwards_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """_request must reuse the pooled client and pass timeout per-request."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://x.example")
    client = JobServiceClient(team="t")
    fake = _RecordingHttp()
    client._http = fake  # inject the pooled client
    client._request("GET", "http://x.example/jobs/t/1", timeout=12.5)
    client._request("GET", "http://x.example/jobs/t/2")  # default timeout
    assert len(fake.calls) == 2
    assert fake.calls[0][2]["timeout"] == 12.5
    assert fake.calls[1][2]["timeout"] == 30.0
    assert client._http is fake  # never rebuilt — same pooled connection


def test_get_job_service_client_caches_per_team(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory returns one shared client per (team, base_url)."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://x.example")
    jsc._shared_clients.clear()
    try:
        a1 = get_job_service_client("teamA")
        a2 = get_job_service_client("teamA")
        b = get_job_service_client("teamB")
        assert a1 is a2  # same team -> same cached client
        assert a1 is not b  # different team -> different client
        assert a1.team == "teamA"
        # An explicit base_url is keyed separately so it never aliases the env one.
        explicit = get_job_service_client("teamA", base_url="http://explicit.example")
        assert explicit is not a1
    finally:
        jsc._shared_clients.clear()


def test_get_job_service_client_requires_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_SERVICE_URL", "http://x.example")
    with pytest.raises(AssertionError):
        get_job_service_client("")


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


# ---------------------------------------------------------------------------
# Activity stamping — mirrors job_service/db.py central last_activity_at
# ---------------------------------------------------------------------------


def test_fake_create_job_stamps_activity_baseline(fake_job_client: FakeJobServiceClient) -> None:
    """Creation is activity: every new job carries a last_activity_at baseline so a
    job that hangs while still pending is detectable by the stall warning."""
    fake_job_client.create_job("j", status="pending")
    assert fake_job_client.get_job("j")["last_activity_at"] is not None


def test_fake_update_job_stamps_activity(fake_job_client: FakeJobServiceClient) -> None:
    """Every real update stamps last_activity_at centrally (production stamps in
    ``db.update_job``); an explicit None from a caller is replaced — the field must
    never go invalid once the job exists."""
    fake_job_client.create_job("j", status="running")
    fake_job_client.update_job("j", last_activity_at=None, status_text="x")
    stamped = fake_job_client.get_job("j")["last_activity_at"]
    assert stamped is not None

    # An explicit real value provided by the caller wins.
    fake_job_client.update_job("j", last_activity_at="2020-01-01T00:00:00+00:00")
    assert fake_job_client.get_job("j")["last_activity_at"] == "2020-01-01T00:00:00+00:00"


def test_fake_heartbeat_never_stamps_activity(fake_job_client: FakeJobServiceClient) -> None:
    """The liveness heartbeat keeps ticking even when the orchestrator thread is hung,
    so it must NOT count as activity — that is the entire signal the stall warning
    reads (production heartbeat touches only last_heartbeat_at/updated_at)."""
    fake_job_client.create_job("j", status="running")
    fake_job_client.update_job("j", last_activity_at="2020-01-01T00:00:00+00:00")
    fake_job_client.heartbeat("j")
    assert fake_job_client.get_job("j")["last_activity_at"] == "2020-01-01T00:00:00+00:00"


def test_fake_append_event_and_atomic_update_stamp_activity(
    fake_job_client: FakeJobServiceClient,
) -> None:
    """Events and atomic patches are real updates (production stamps in
    ``db.append_event`` / ``db.apply_patch``)."""
    fake_job_client.create_job("j", status="running")
    fake_job_client.update_job("j", last_activity_at="2020-01-01T00:00:00+00:00")

    fake_job_client.append_event("j", action="merge", outcome="ok")
    after_event = fake_job_client.get_job("j")["last_activity_at"]
    assert after_event != "2020-01-01T00:00:00+00:00"

    fake_job_client.update_job("j", last_activity_at="2020-01-01T00:00:00+00:00")
    fake_job_client.atomic_update("j", merge_fields={"phase": "coding"})
    after_patch = fake_job_client.get_job("j")["last_activity_at"]
    assert after_patch != "2020-01-01T00:00:00+00:00"


def test_fake_bulk_terminal_markers_clear_current_activity(
    fake_job_client: FakeJobServiceClient,
) -> None:
    """Bulk failure/interrupt markers run when no orchestrator finally-clear can —
    they must wipe current_activity so a dead job never serves a frozen sub-bar
    (production merges ``current_activity: None`` in ``db.mark_*``)."""
    fake_job_client.create_job("j1", status="running", current_activity={"step": "reviewing", "fraction": 0.4})
    fake_job_client.create_job("j2", status="running", current_activity={"step": "reviewing", "fraction": 0.7})

    fake_job_client.mark_all_active_jobs_interrupted("shutdown")
    assert fake_job_client.get_job("j1")["current_activity"] is None

    # Reactivate j2 with a fresh activity entry so the stale sweep's clear is
    # actually exercised (the interrupt marker above already cleared it once).
    fake_job_client.update_job("j2", status="running", current_activity={"step": "reviewing", "fraction": 0.7})
    fake_job_client.mark_stale_active_jobs_failed(stale_after_seconds=-1, reason="stale")
    j2 = fake_job_client.get_job("j2")
    assert j2["status"] == "failed"
    assert j2["current_activity"] is None

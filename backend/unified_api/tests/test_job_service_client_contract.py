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


@pytest.fixture(autouse=True)
def _isolate_pooled_client():
    """Reset the ``shared_http`` connection pool around every test.

    ``JobServiceClient._request`` reuses a process-wide client cached per timeout
    bucket (``shared_http.get_pooled_client``). The MockTransport tests below patch
    ``httpx.Client`` to inject a transport, which only takes effect when the pooled
    client is (re)built — so a client cached by an earlier test would otherwise
    shadow the patch (and a MockTransport client built here would leak into later
    tests). Clearing the pool before and after each test keeps them independent.
    """
    from shared_http import close_pool

    close_pool()
    yield
    close_pool()


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
    """With no explicit base_url and no JOB_SERVICE_URL env, construction raises RuntimeError."""
    monkeypatch.delenv("JOB_SERVICE_URL", raising=False)
    with pytest.raises(RuntimeError, match="JOB_SERVICE_URL is not set"):
        JobServiceClient(team="x")


# ---------------------------------------------------------------------------
# Real HTTP client wrappers — exercised against an httpx MockTransport so the
# new cancel endpoint and the bulk shutdown endpoint are covered without a live
# job service. These pin the wrapper -> URL contract (and confirm the methods
# the wrapper relies on actually exist on the HTTP client).
# ---------------------------------------------------------------------------


def _route_through_mock_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Make the client's internal ``httpx.Client(...)`` use a MockTransport.

    ``JobServiceClient._request`` constructs ``httpx.Client(timeout=...)`` per
    call, so patching the module-level ``httpx.Client`` to inject the transport
    intercepts every request without a live server.
    """
    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def test_real_client_cancel_active_job_posts_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real client POSTs to /jobs/{team}/{job_id}/cancel and returns the parsed `cancelled`."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"cancelled": True})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    assert client.cancel_active_job("j1") is True
    assert captured["method"] == "POST"
    assert captured["url"] == "http://js.example/jobs/t/j1/cancel"


def test_real_client_cancel_active_job_false_when_not_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal/missing job yields ``cancelled: false`` server-side -> False."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    _route_through_mock_transport(monkeypatch, lambda _req: httpx.Response(200, json={"cancelled": False}))
    client = JobServiceClient(team="t")
    assert client.cancel_active_job("j1") is False


def test_real_client_retries_on_remote_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale pooled keep-alive connection raises ``RemoteProtocolError`` ("server
    disconnected without sending a response"). ``_request`` must treat it as transient
    and retry on a fresh attempt rather than surfacing a 500."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"job": {"job_id": "j1", "status": "running"}})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    assert client.get_job("j1") == {"job_id": "j1", "status": "running"}
    assert calls["n"] == 2  # one failure + one successful retry


def test_real_client_reraises_remote_protocol_error_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every attempt hits the disconnect, the error is re-raised after the
    retry budget (``max_retries`` defaults to 3 -> 4 total attempts)."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    with pytest.raises(httpx.RemoteProtocolError):
        client.get_job("j1")
    assert calls["n"] == 4  # max_retries (3) + 1 initial attempt


def test_post_not_retried_on_remote_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``RemoteProtocolError`` on a non-idempotent POST (the request may already
    have reached the server) must NOT be retried — replaying it could duplicate
    the operation. The error propagates after a single attempt."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.method == "POST"
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    with pytest.raises(httpx.RemoteProtocolError):
        client.create_job("j1")
    assert calls["n"] == 1  # non-idempotent POST is not retried


def test_post_retried_on_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ConnectError`` means the connection was never established, so the request
    provably never reached the server — safe to retry even for a non-idempotent
    POST."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    client.create_job("j1")  # succeeds after one retry on a fresh connection
    assert calls["n"] == 2


def test_real_client_retries_on_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pooled keep-alive socket reset by the server/proxy raises ``ReadError``
    ("[Errno 104] Connection reset by peer") while reading the response. Like the
    sibling ``RemoteProtocolError``, ``_request`` must treat it as transient and
    retry an idempotent GET on a fresh attempt rather than surfacing a 500."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("[Errno 104] Connection reset by peer")
        return httpx.Response(200, json={"job": {"job_id": "j1", "status": "running"}})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    assert client.get_job("j1") == {"job_id": "j1", "status": "running"}
    assert calls["n"] == 2  # one failure + one successful retry


def test_real_client_reraises_read_error_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every attempt hits the connection reset, the ``ReadError`` is re-raised
    after the retry budget (``max_retries`` defaults to 3 -> 4 total attempts)."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadError("[Errno 104] Connection reset by peer")

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    with pytest.raises(httpx.ReadError):
        client.get_job("j1")
    assert calls["n"] == 4  # max_retries (3) + 1 initial attempt


def test_post_not_retried_on_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ReadError`` on a non-idempotent POST (the request may already have reached
    the server before the socket reset) must NOT be retried — replaying it could
    duplicate the operation. The error propagates after a single attempt."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.method == "POST"
        raise httpx.ReadError("[Errno 104] Connection reset by peer")

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    with pytest.raises(httpx.ReadError):
        client.create_job("j1")
    assert calls["n"] == 1  # non-idempotent POST is not retried


def test_real_client_retries_on_write_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``WriteError`` is the write-side analog of ``ReadError`` (a stale keep-alive
    socket reset while sending the request). It is retried for idempotent methods
    just like ``ReadError`` — here an idempotent GET succeeds on the retry."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.WriteError("[Errno 104] Connection reset by peer")
        return httpx.Response(200, json={"job": {"job_id": "j1", "status": "running"}})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    assert client.get_job("j1") == {"job_id": "j1", "status": "running"}
    assert calls["n"] == 2  # one failure + one successful retry


def test_real_client_mark_all_active_jobs_failed_hits_bulk_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shutdown hook routes through the atomic server-side bulk endpoint
    (``/mark-all-running-failed``), not a client-side list+update loop."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"failed_job_ids": ["a", "b"]})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    assert client.mark_all_active_jobs_failed("shutdown") == ["a", "b"]
    assert captured["method"] == "POST"
    assert captured["url"] == "http://js.example/jobs/t/mark-all-running-failed"


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
# Atomic cancel — mirrors job_service/db.py cancel_active_job conditional UPDATE
# ---------------------------------------------------------------------------


def test_fake_cancel_active_job_cancels_pending_and_running(
    fake_job_client: FakeJobServiceClient,
) -> None:
    """A pending or running job is cancellable; the fake mirrors the production
    conditional UPDATE (``status IN ('pending','running')``)."""
    fake_job_client.create_job("p", status="pending")
    fake_job_client.create_job("r", status="running")
    assert fake_job_client.cancel_active_job("p") is True
    assert fake_job_client.cancel_active_job("r") is True
    assert fake_job_client.get_job("p")["status"] == "cancelled"
    assert fake_job_client.get_job("r")["status"] == "cancelled"


def test_fake_cancel_active_job_noop_on_terminal(fake_job_client: FakeJobServiceClient) -> None:
    """A job that has reached a terminal status must NOT be overwritten — the status
    guard lives in the same UPDATE that writes, closing the check-then-act race."""
    for terminal in ("completed", "failed", "cancelled", "interrupted"):
        fake_job_client.create_job(terminal, status=terminal)
        assert fake_job_client.cancel_active_job(terminal) is False
        assert fake_job_client.get_job(terminal)["status"] == terminal


def test_fake_cancel_active_job_noop_on_missing(fake_job_client: FakeJobServiceClient) -> None:
    """Cancelling a job that does not exist returns False (no auto-create)."""
    assert fake_job_client.cancel_active_job("nope") is False
    assert fake_job_client.get_job("nope") is None


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

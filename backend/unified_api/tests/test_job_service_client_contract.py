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

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from job_service_client import JobServiceClient
from job_service_client_fake import FakeJobServiceClient


@pytest.fixture(autouse=True)
def _isolate_pooled_client():
    """Reset the ``shared.http`` connection pool around every test.

    ``JobServiceClient._request`` reuses a process-wide client cached per timeout
    bucket (``shared.http.get_pooled_client``). The MockTransport tests below patch
    ``httpx.Client`` to inject a transport, which only takes effect when the pooled
    client is (re)built — so a client cached by an earlier test would otherwise
    shadow the patch (and a MockTransport client built here would leak into later
    tests). Clearing the pool before and after each test keeps them independent.
    """
    from shared.http import close_pool

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


def test_real_client_update_job_if_not_cancelled_posts_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real client POSTs to /jobs/{team}/{job_id}/update-if-not-cancelled with
    the same {heartbeat, fields} body as update_job, and returns the parsed `updated`."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"updated": True})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    assert client.update_job_if_not_cancelled("j1", status="running") is True
    assert captured["method"] == "POST"
    assert captured["url"] == "http://js.example/jobs/t/j1/update-if-not-cancelled"
    assert captured["json"] == {"heartbeat": True, "fields": {"status": "running"}}


def test_real_client_update_job_if_not_cancelled_false_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled job yields ``updated: false`` server-side -> False."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    _route_through_mock_transport(monkeypatch, lambda _req: httpx.Response(200, json={"updated": False}))
    client = JobServiceClient(team="t")
    assert client.update_job_if_not_cancelled("j1", status="running") is False


def test_real_client_update_job_if_not_cancelled_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing job yields ``updated: null`` server-side -> None — distinct from
    the cancelled case (False) so the caller can tell a broken precondition
    (missing row) apart from a legitimate cancellation."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    _route_through_mock_transport(monkeypatch, lambda _req: httpx.Response(200, json={"updated": None}))
    client = JobServiceClient(team="t")
    assert client.update_job_if_not_cancelled("j1", status="running") is None


def test_real_client_update_job_if_not_cancelled_rejects_cancelled_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This primitive is not a cancellation mechanism (unlike cancel_active_job, it
    doesn't exclude other terminal statuses) — writing status='cancelled' through
    it is a caller precondition violation, rejected before any HTTP call is made.
    Enforced with an explicit raise, never an assert (stripped under python -O)."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    made_request = {"called": False}
    _route_through_mock_transport(
        monkeypatch, lambda _req: made_request.update(called=True) or httpx.Response(200, json={})
    )
    client = JobServiceClient(team="t")
    with pytest.raises(ValueError, match="must not be used to cancel"):
        client.update_job_if_not_cancelled("j1", status="cancelled")
    assert made_request["called"] is False


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


def test_post_retried_on_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ConnectTimeout`` is a TCP handshake that never completed — the request
    never reached the server, same as ``ConnectError``. It must be retried for
    any method (including non-idempotent POST) so brief job-service startup
    races do not surface as unhandled ASGI 500s."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, json={})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    client.create_job("j1")
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


# ---------------------------------------------------------------------------
# delete_job — retry-after-commit must not report a false "not found"
# ---------------------------------------------------------------------------


def test_real_client_delete_job_true_when_deleted_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """No transient error at all: the server reports ``deleted: true`` and
    ``delete_job`` returns it as-is on the first attempt."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"deleted": True})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    assert client.delete_job("j1") is True
    assert calls["n"] == 1


def test_real_client_delete_job_false_when_not_found_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine not-found delete (no transient error, so no retry ambiguity)
    still reports ``deleted: false`` — no behavior change for this case."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"deleted": False})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    assert client.delete_job("j1") is False
    assert calls["n"] == 1


def test_real_client_delete_job_true_when_retry_follows_lost_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this guards against: attempt 1's DELETE reaches the server and
    commits the row deletion, but the response is lost to a ``ReadError``
    (stale keep-alive reset). ``_request`` retries the idempotent DELETE;
    attempt 2 correctly finds no row and answers ``deleted: false``. Because
    an earlier attempt may have already reached the server, ``delete_job``
    must report this as success, not a spurious not-found."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("[Errno 104] Connection reset by peer")
        return httpx.Response(200, json={"deleted": False})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    assert client.delete_job("j1") is True
    assert calls["n"] == 2  # one lost-response failure + one successful retry


def test_real_client_delete_job_false_when_connect_error_then_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ConnectError`` proves the first attempt never reached the server at
    all, so a subsequent ``deleted: false`` is unambiguous and must NOT be
    upgraded to success — this pins that the fix doesn't overreach beyond the
    ``_RETRY_IDEMPOTENT_ONLY_ERRORS`` class."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"deleted": False})

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    assert client.delete_job("j1") is False
    assert calls["n"] == 2  # one connect failure + one successful retry


def test_real_client_delete_job_reraises_when_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every attempt hits the connection reset, ``delete_job`` still lets
    ``ReadError`` propagate after the retry budget — the ambiguity tracker
    never masks a final, real failure."""
    monkeypatch.setenv("JOB_SERVICE_URL", "http://js.example/")
    monkeypatch.setattr("job_service_client.time.sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadError("[Errno 104] Connection reset by peer")

    _route_through_mock_transport(monkeypatch, handler)
    client = JobServiceClient(team="t")
    with pytest.raises(httpx.ReadError):
        client.delete_job("j1")
    assert calls["n"] == 4  # max_retries (3) + 1 initial attempt


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
# Atomic conditional update — mirrors job_service/db.py
# update_job_if_not_cancelled's conditional UPDATE, closing the TOCTOU race a
# separate is_job_cancelled check + update_job write would leave open.
# ---------------------------------------------------------------------------


def test_fake_update_job_if_not_cancelled_updates_active_job(
    fake_job_client: FakeJobServiceClient,
) -> None:
    fake_job_client.create_job("j1", status="pending")
    assert fake_job_client.update_job_if_not_cancelled("j1", status="running") is True
    assert fake_job_client.get_job("j1")["status"] == "running"


def test_fake_update_job_if_not_cancelled_noop_on_cancelled(
    fake_job_client: FakeJobServiceClient,
) -> None:
    fake_job_client.create_job("j1", status="cancelled")
    assert fake_job_client.update_job_if_not_cancelled("j1", status="running") is False
    assert fake_job_client.get_job("j1")["status"] == "cancelled"


def test_fake_update_job_if_not_cancelled_noop_on_missing(
    fake_job_client: FakeJobServiceClient,
) -> None:
    """None, not False — distinct from the cancelled case so a caller can tell
    a broken precondition (missing row) apart from a legitimate cancellation."""
    assert fake_job_client.update_job_if_not_cancelled("nope", status="running") is None
    assert fake_job_client.get_job("nope") is None


def test_fake_update_job_if_not_cancelled_does_not_block_on_other_terminal_statuses(
    fake_job_client: FakeJobServiceClient,
) -> None:
    """Unlike ``cancel_active_job`` (which only allows pending/running), this
    primitive guards ONLY on 'cancelled' — matching ``is_job_cancelled``'s
    existing narrower check. A completed/failed job can still be written."""
    for terminal in ("completed", "failed", "interrupted"):
        fake_job_client.create_job(terminal, status=terminal)
        assert fake_job_client.update_job_if_not_cancelled(terminal, note="x") is True
        assert fake_job_client.get_job(terminal)["note"] == "x"


def test_fake_update_job_if_not_cancelled_rejects_cancelled_status(
    fake_job_client: FakeJobServiceClient,
) -> None:
    """This primitive is not a cancellation mechanism — writing status='cancelled'
    through it would silently overwrite a completed/failed job (unlike
    cancel_active_job's narrower pending/running-only guard), so it's rejected."""
    fake_job_client.create_job("j1", status="completed")
    with pytest.raises(ValueError, match="must not be used to cancel"):
        fake_job_client.update_job_if_not_cancelled("j1", status="cancelled")
    assert fake_job_client.get_job("j1")["status"] == "completed"


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

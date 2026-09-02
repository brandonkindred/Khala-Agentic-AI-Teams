"""Tests for the generic job-service proxy routes on the unified API.

Covers ``/api/jobs/...`` (list/delete/cancel/interrupt/resume/restart/mark-all)
and the ``/api/se/metrics`` DORA-metrics alias. Each handler opens a fresh
``httpx.AsyncClient`` per request rather than caching one (unlike
``unified_api.team_proxy``), so the upstream call is faked by monkeypatching
``httpx.AsyncClient`` itself to route through an ``httpx.MockTransport``.
"""

from __future__ import annotations

import inspect
import logging
import sys
from functools import partial
from pathlib import Path

import httpx
import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi.testclient import TestClient

from unified_api import main
from unified_api.main import app

client = TestClient(app)


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Route every ``httpx.AsyncClient(...)`` constructed by main.py through a mock transport.

    Preconditions: ``handler`` is a callable accepting an ``httpx.Request`` and
    returning an ``httpx.Response`` (the ``httpx.MockTransport`` handler contract).
    Postconditions: for the duration of the test, ``unified_api.main.httpx.AsyncClient``
    ignores whatever ``base_url``/``timeout`` kwargs the caller passes and always talks
    to ``handler`` instead of the network; monkeypatch reverts this automatically.
    """
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "unified_api.main.httpx.AsyncClient",
        partial(httpx.AsyncClient, transport=transport),
    )


def test_list_team_jobs_forwards_to_job_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/jobs/{team} forwards to the job service and returns its JSON body."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"jobs": [{"job_id": "abc"}]})

    _patch_async_client(monkeypatch, handler)
    resp = client.get("/api/jobs/blogging")
    assert resp.status_code == 200
    assert resp.json() == {"jobs": [{"job_id": "abc"}]}
    assert "/jobs/blogging" in seen["url"]
    assert "statuses" not in seen["url"]


def test_list_team_jobs_running_only_adds_status_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/jobs/{team}?running_only=true appends the pending/running status filters."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"jobs": []})

    _patch_async_client(monkeypatch, handler)
    resp = client.get("/api/jobs/blogging", params={"running_only": "true"})
    assert resp.status_code == 200
    assert "statuses=pending" in seen["url"]
    assert "statuses=running" in seen["url"]


def test_list_team_jobs_redacts_encrypted_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The persisted resume credential never leaves this API, even as ciphertext.

    Teams store ``github_token_encrypted`` on the job row so a worker can resume
    after its orchestrator thread dies; every legitimate reader loads it from the
    job SERVICE directly, never from this proxy's response. Forwarding it here
    would spray the ciphertext across every job reader, their caches and any
    intermediary's access logs for no consumer's benefit.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {"job_id": "abc", "status": "running", "github_token_encrypted": "gAAAAAsecret"},
                    {"job_id": "def", "status": "completed"},
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)
    resp = client.get("/api/jobs/software-engineering")
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    # Removed outright, not blanked: a placeholder could be mistaken for a value.
    assert "github_token_encrypted" not in jobs[0]
    assert "gAAAAAsecret" not in resp.text
    # Every other field is still forwarded verbatim, and a job without the
    # field is untouched.
    assert jobs[0] == {"job_id": "abc", "status": "running"}
    assert jobs[1] == {"job_id": "def", "status": "completed"}


@pytest.mark.parametrize(
    "path",
    ["cancel", "interrupt", "resume", "restart"],
)
def test_single_job_routes_redact_encrypted_github_token(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """The single-job mutation proxies echo a whole job record too, so they get the
    same redaction as the list route -- redacting only the list would leave four
    equivalent leaks open."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"job": {"job_id": "abc", "github_token_encrypted": "gAAAAAsecret"}},
        )

    _patch_async_client(monkeypatch, handler)
    resp = client.post(f"/api/jobs/software-engineering/abc/{path}")
    assert resp.status_code == 200
    assert resp.json() == {"job": {"job_id": "abc"}}
    assert "gAAAAAsecret" not in resp.text


def test_redact_job_secrets_redacts_and_warns_on_unrecognized_wrapper_key(caplog) -> None:
    """An unrecognized wrapper must be REDACTED, and must not pass in silence.

    Redaction is keyed on the credential FIELD NAMES, not on the envelope, so a
    job service that renamed its wrapper cannot silently resume serving
    credentials -- the field is stripped wherever it sits. The warning is
    retained on top of that as a contract-drift tripwire, not as the control.
    """
    payload = {"records": [{"job_id": "abc", "github_token_encrypted": "gAAAAAsecret"}]}
    with caplog.at_level(logging.WARNING, logger="unified_api"):
        out = main._redact_job_secrets(payload)
    assert out == {"records": [{"job_id": "abc"}]}
    # Not mutated in place: the caller's own object still carries what it had.
    assert payload["records"][0]["github_token_encrypted"] == "gAAAAAsecret"
    assert any("neither 'jobs' nor 'job'" in r.getMessage() for r in caplog.records)
    # The tripwire names the KEYS it saw, never a credential value.
    assert "gAAAAAsecret" not in caplog.text


def test_redact_job_secrets_redacts_and_warns_when_jobs_is_not_a_list(caplog) -> None:
    payload = {"jobs": {"job_id": "abc", "github_token_encrypted": "gAAAAAsecret"}}
    with caplog.at_level(logging.WARNING, logger="unified_api"):
        out = main._redact_job_secrets(payload)
    assert out == {"jobs": {"job_id": "abc"}}
    assert any("not a list" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "payload,expected",
    [
        # A job nested one level deeper than either known envelope.
        (
            {"page": {"items": [{"job_id": "a", "github_token_encrypted": "s"}]}},
            {"page": {"items": [{"job_id": "a"}]}},
        ),
        # A bare list body (no envelope at all).
        ([{"job_id": "a", "github_token_encrypted": "s"}], [{"job_id": "a"}]),
        # A bare job object.
        ({"job_id": "a", "github_token_encrypted": "s"}, {"job_id": "a"}),
        # The credential inside a known envelope but under a sub-object.
        (
            {"job": {"job_id": "a", "meta": {"github_token_encrypted": "s"}}},
            {"job": {"job_id": "a", "meta": {}}},
        ),
        # Non-container bodies are returned untouched rather than crashing.
        ("plain", "plain"),
        (None, None),
        (7, 7),
    ],
    ids=["nested", "bare-list", "bare-job", "sub-object", "str", "none", "int"],
)
def test_redact_job_secrets_is_shape_agnostic(payload, expected) -> None:
    """The redaction must depend on the KEY it strips, not on where the job sits.

    The previous shape-specific version fixed the two known envelopes and
    forwarded everything else untouched, so any job-service envelope change was
    a silent credential leak. Every case here would leak under that version.
    """
    assert main._redact_job_secrets(payload) == expected


def test_redact_job_secrets_stays_quiet_on_recognized_shapes(caplog) -> None:
    """The tripwire must not cry wolf on the two shapes it DOES handle, or the
    warning becomes noise no operator reads."""
    with caplog.at_level(logging.WARNING, logger="unified_api"):
        main._redact_job_secrets({"jobs": [{"job_id": "a"}]})
        main._redact_job_secrets({"job": {"job_id": "a"}})
    assert not [r for r in caplog.records if "_redact_job_secrets" in r.getMessage()]


def test_every_job_returning_proxy_route_redacts() -> None:
    """Completeness guard for the redaction added alongside these proxies.

    A future job-returning proxy added without ``_redact_job_secrets`` is a
    silent credential leak, and no per-route test would catch its absence. So
    assert it structurally: every ``/api/jobs`` route whose upstream returns a
    job record must mention the redactor in its own source.

    ``delete_job`` and ``mark_all_interrupted`` are deliberately exempt --
    their job-service counterparts return ``{"deleted": bool}`` and
    ``{"interrupted_job_ids": [...]}``, neither of which carries a job record.
    """
    exempt = {"delete_job", "mark_all_interrupted"}
    unredacted: list[str] = []
    for route in main.app.routes:
        endpoint = getattr(route, "endpoint", None)
        path = getattr(route, "path", "")
        if endpoint is None or not path.startswith("/api/jobs"):
            continue
        if endpoint.__name__ in exempt:
            continue
        if "_redact_job_secrets" not in inspect.getsource(endpoint):
            unredacted.append(f"{path} ({endpoint.__name__})")
    assert unredacted == [], f"job-returning proxy routes missing redaction: {unredacted}"


def test_delete_job_forwards_delete_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELETE /api/jobs/{team}/{job_id} issues a DELETE against the job service."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"deleted": True})

    _patch_async_client(monkeypatch, handler)
    resp = client.delete("/api/jobs/blogging/job-1")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    assert seen["method"] == "DELETE"
    assert seen["url"].endswith("/jobs/blogging/job-1")


@pytest.mark.parametrize(
    ("path_suffix", "expected_status", "expected_error"),
    [
        ("cancel", "cancelled", "Cancelled by user"),
        ("interrupt", "interrupted", "Marked interrupted by user"),
    ],
)
def test_job_status_transition_endpoints_patch_with_expected_fields(
    monkeypatch: pytest.MonkeyPatch, path_suffix: str, expected_status: str, expected_error: str
) -> None:
    """cancel/interrupt PATCH the job service with the matching status + error fields."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    _patch_async_client(monkeypatch, handler)
    resp = client.post(f"/api/jobs/blogging/job-1/{path_suffix}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert seen["method"] == "PATCH"
    body = seen["body"].decode()
    assert expected_status in body
    assert expected_error in body


def test_resume_job_resets_status_to_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """resume PATCHes the job back to 'running' with a cleared error and heartbeat=True."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    _patch_async_client(monkeypatch, handler)
    resp = client.post("/api/jobs/blogging/job-1/resume")
    assert resp.status_code == 200
    body = seen["body"].decode()
    assert '"status":"running"' in body
    assert '"heartbeat":true' in body


def test_restart_job_resets_status_to_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """restart PATCHes the job back to 'pending' with a cleared error and heartbeat=True."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    _patch_async_client(monkeypatch, handler)
    resp = client.post("/api/jobs/blogging/job-1/restart")
    assert resp.status_code == 200
    body = seen["body"].decode()
    assert '"status":"pending"' in body


def test_mark_all_interrupted_posts_bulk_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """mark-all-interrupted POSTs the bulk-interrupt action for the team."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"marked": 3})

    _patch_async_client(monkeypatch, handler)
    resp = client.post("/api/jobs/blogging/mark-all-interrupted")
    assert resp.status_code == 200
    assert resp.json() == {"marked": 3}
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/jobs/blogging/mark-all-running-interrupted")


# ---------------------------------------------------------------------------
# GET /api/se/metrics — DORA metrics alias
# ---------------------------------------------------------------------------


def test_se_metrics_alias_returns_503_when_service_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/se/metrics returns 503 when SOFTWARE_ENGINEERING_SERVICE_URL is unset."""
    monkeypatch.delenv("SOFTWARE_ENGINEERING_SERVICE_URL", raising=False)
    resp = client.get("/api/se/metrics")
    assert resp.status_code == 503


def test_se_metrics_alias_forwards_to_dora_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/se/metrics proxies to the SE service's /dora route and returns its body."""
    monkeypatch.setenv("SOFTWARE_ENGINEERING_SERVICE_URL", "http://se-service:8000")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"deploy_frequency": 1.5})

    _patch_async_client(monkeypatch, handler)
    resp = client.get("/api/se/metrics", params={"window_days": 7})
    assert resp.status_code == 200
    assert resp.json() == {"deploy_frequency": 1.5}
    assert seen["url"].startswith("http://se-service:8000/dora")
    assert "window_days=7" in seen["url"]


def test_se_metrics_alias_returns_502_on_upstream_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/se/metrics returns 502 when the SE service itself responds with an error."""
    monkeypatch.setenv("SOFTWARE_ENGINEERING_SERVICE_URL", "http://se-service:8000")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    _patch_async_client(monkeypatch, handler)
    resp = client.get("/api/se/metrics")
    assert resp.status_code == 502


def test_se_metrics_alias_returns_503_on_unreachable_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/se/metrics returns 503 when the SE service is unreachable."""
    monkeypatch.setenv("SOFTWARE_ENGINEERING_SERVICE_URL", "http://se-service:8000")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_async_client(monkeypatch, handler)
    resp = client.get("/api/se/metrics")
    assert resp.status_code == 503


def test_se_metrics_alias_resets_non_positive_timeout_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/se/metrics falls back to the 15s default when the timeout knob is <= 0."""
    monkeypatch.setenv("SOFTWARE_ENGINEERING_SERVICE_URL", "http://se-service:8000")
    monkeypatch.setenv("SE_METRICS_ALIAS_TIMEOUT", "-5")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"deploy_frequency": 1.5})

    _patch_async_client(monkeypatch, handler)
    resp = client.get("/api/se/metrics")
    assert resp.status_code == 200
    assert seen["timeout"] == {"connect": 15.0, "read": 15.0, "write": 15.0, "pool": 15.0}


def test_se_metrics_alias_returns_502_on_non_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/se/metrics returns 502 when the upstream 200s with a non-JSON body."""
    monkeypatch.setenv("SOFTWARE_ENGINEERING_SERVICE_URL", "http://se-service:8000")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>", headers={"content-type": "text/html"})

    _patch_async_client(monkeypatch, handler)
    resp = client.get("/api/se/metrics")
    assert resp.status_code == 502

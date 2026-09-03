"""Tests for the address-unresolved-comments proxy route:

    POST /api/integrations/github/pulls/{pr_number}/address-comments

Mirrors the review-pr proxy tests: no real network — the coding-team forward is
served by a fake httpx.AsyncClient, and the GitHub config/PAT is patched.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import httpx

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi.testclient import TestClient  # noqa: E402

from unified_api.main import app  # noqa: E402

client = TestClient(app, follow_redirects=False)

_M = "unified_api.routes.integrations"
# The flock acquire/release choreography now lives in the shared
# held_checkout_lock helper (shared/concurrency/checkout_lock.py), which
# imports flock_lock directly from shared.concurrency.flock_lock -- patching
# it on unified_api.routes.integrations (which no longer references it for
# these two routes) would have no effect on the lock actually used.
_LOCK_M = "shared.concurrency.checkout_lock"
_URL = "/api/integrations/github/pulls/7/address-comments"

_GH_CFG = {"enabled": True, "owner": "acme", "repo": "widget", "default_label": "", "repo_path": ""}


class _FakeResp:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("not JSON")
        return self._json


_NOT_RUNNING = _FakeResp(200, {"running_job_id": None})


class _FakeAsyncClient:
    """Fake httpx.AsyncClient. GET (the admission pre-check) defaults to "no job
    running" so tests that only configure POST behavior are unaffected by the
    extra pre-check call; pass `get_result`/`get_exc` to simulate the pre-check
    itself failing or reporting a running job."""

    def __init__(self, *, result=None, exc=None, get_result=None, get_exc=None):
        self._result = result
        self._exc = exc
        self._get_result = get_result if get_result is not None else _NOT_RUNNING
        self._get_exc = get_exc
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, params))
        if self._get_exc is not None:
            raise self._get_exc
        return self._get_result

    async def post(self, url, json=None):
        self.calls.append((url, json))
        if self._exc is not None:
            raise self._exc
        return self._result


_OK = {
    "job_id": "j1",
    "pr_number": 7,
    "pr_url": "https://github.com/acme/widget/pull/7",
    "unresolved_comment_count": 3,
    "status": "pending",
    "message": "started",
}


@patch(f"{_M}.get_github_config_meta", return_value={**_GH_CFG, "enabled": False})
def test_400_when_disabled(mock_cfg):
    assert client.post(_URL, json={}).status_code == 400


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_400_when_pat_missing(mock_cfg, mock_cred):
    assert client.post(_URL, json={}).status_code == 400


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_503_when_service_url_unset(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.delenv("CODING_TEAM_SERVICE_URL", raising=False)
    assert client.post(_URL, json={}).status_code == 503


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget/pr-7")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_success_proxies_with_token_and_path(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103/")
    fake = _FakeAsyncClient(result=_FakeResp(200, _OK))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_URL, json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "j1"
    assert body["unresolved_comment_count"] == 3
    # Proxied to the coding-team endpoint with the PR-number-scoped path.
    # calls[0] is the admission pre-check GET, calls[1] is the actual POST.
    url, sent = fake.calls[1]
    assert url == "http://coding:8103/pulls/7/address-comments"
    assert sent["pr_number"] == 7
    assert sent["github_token"] == "ghp"
    assert sent["repo_path"] == "/tmp/acme_widget/pr-7"
    # The checkout is materialized (cloned/fetched) before the job is forwarded.
    mock_clone.assert_called_once_with(
        "/tmp/acme_widget/pr-7", "acme", "widget", "ghp", platform_owned=True, acquire_lock=False
    )
    # Platform-owned (no repo_path override) → the coding team is told it may
    # reclaim the per-PR checkout once every comment is handled successfully.
    assert sent["cleanup_checkout_on_success"] is True


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/srv/pinned-checkout")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(
    f"{_M}.get_github_config_meta",
    return_value={**_GH_CFG, "repo_path": "/srv/pinned-checkout"},
)
def test_operator_pinned_checkout_is_never_cleaned_up(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    """An operator-pinned repo_path override must never be flagged for cleanup —
    it isn't per-PR-namespaced and the operator manages its lifecycle."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103/")
    fake = _FakeAsyncClient(result=_FakeResp(200, _OK))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_URL, json={})
    assert resp.status_code == 200
    _url, sent = fake.calls[1]
    assert sent["cleanup_checkout_on_success"] is False
    mock_clone.assert_called_once_with(
        "/srv/pinned-checkout", "acme", "widget", "ghp", platform_owned=False, acquire_lock=False
    )


@patch(f"{_M}._ensure_repo_clone", return_value="git clone failed: boom")
@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_502_when_clone_fails(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient()
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_URL, json={})
    assert resp.status_code == 502
    # The raw clone error is logged, not forwarded verbatim to the external
    # caller — a curated message is returned instead (matching the same
    # handler's other 502 responses' convention).
    assert resp.json()["detail"] == "Failed to prepare the repository clone for this pull request."


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_504_on_timeout(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ReadTimeout("slow"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.post(_URL, json={}).status_code == 504


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_502_on_unreachable(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ConnectError("refused"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.post(_URL, json={}).status_code == 502


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_wraps_upstream_5xx_error_in_generic_message(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    """A 5xx from the coding-team service is deliberately NOT propagated
    verbatim (it could carry an internal stack trace) — `_forward_to_coding_team`
    wraps it in a generic, client-safe message instead."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(502, {"detail": "github api error"}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_URL, json={})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Failed to start addressing the PR comments."


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_502_on_malformed_success_body(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(200, {"unexpected": "shape"}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.post(_URL, json={}).status_code == 502


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_409_and_no_checkout_touched_when_already_running(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    """The admission pre-check must run BEFORE the checkout is cloned/fetched:
    when a job is already running for this PR, the route must reject with 409
    and never call _ensure_repo_clone — otherwise a concurrent git fetch could
    race the running job's own git operations on the same shared checkout."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(
        result=_FakeResp(200, _OK),
        get_result=_FakeResp(200, {"running_job_id": "existing-job"}),
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_URL, json={})
    assert resp.status_code == 409
    assert "existing-job" in resp.json()["detail"]
    mock_clone.assert_not_called()
    # Only the admission pre-check GET happened — never the address-comments POST.
    assert len(fake.calls) == 1
    url, params = fake.calls[0]
    assert url == "http://coding:8103/pulls/7/address-comments/running"
    assert params == {"owner": "acme", "repo": "widget", "repo_path": "/tmp/x"}


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_admission_check_timeout_never_touches_checkout(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(get_exc=httpx.ReadTimeout("slow"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_URL, json={})
    assert resp.status_code == 504
    mock_clone.assert_not_called()


_lock_events: list[str] = []


class _SpyLock:
    """Stand-in for shared.concurrency.flock_lock: records enter/exit order (in
    the module-level ``_lock_events`` list — tests must ``.clear()`` it, not
    reassign it, since each instance appends to that same shared record) and
    can be made to fail acquisition (recording ``"lock_fail"`` in that case, so
    a test can prove the lock was ATTEMPTED and not silently skipped). Uses
    ``"lock_enter"``/``"lock_exit"``
    tokens (rather than bare ``"enter"``/``"exit"``) so a test can interleave
    them with other instrumented steps (HTTP calls, the clone) in the SAME
    list and assert the full ordering, not just that the lock was taken and
    released exactly once."""

    def __init__(self, path, *, fail: bool = False):
        self._fail = fail

    def __enter__(self):
        if self._fail:
            # Record the ATTEMPT before raising: without this, a failing
            # acquisition leaves no trace at all, and a test asserting only
            # "the request still succeeded" would pass just as happily against
            # a route that skipped the lock entirely.
            _lock_events.append("lock_fail")
            raise OSError("lock busy")
        _lock_events.append("lock_enter")
        return self

    def __exit__(self, *exc_info):
        _lock_events.append("lock_exit")
        return False


@patch(f"{_LOCK_M}.flock_lock", lambda p: _SpyLock(p))
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget/pr-7")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_checkout_lock_held_around_the_whole_flow(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    """The admission pre-check, clone/fetch, and forward POST all run under one
    lock on the checkout — closing the window where two simultaneous requests
    could each observe "nothing running" and then both mutate the checkout.

    Asserting just ["lock_enter", "lock_exit"] would only prove the lock was
    taken and released exactly once — an implementation that released the
    lock before the network calls and reacquired it afterward would still
    pass that check while violating the actual invariant. Instrumenting the
    admission GET, the clone, and the forward POST into the SAME ordered
    event list proves they all happen strictly BETWEEN the lock's enter and
    exit, not just that enter/exit happened somewhere in the test.
    """
    _lock_events.clear()
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103/")

    class _EventedAsyncClient(_FakeAsyncClient):
        async def get(self, url, params=None):
            _lock_events.append("http_get")
            return await super().get(url, params=params)

        async def post(self, url, json=None):
            _lock_events.append("http_post")
            return await super().post(url, json=json)

    def _clone_side_effect(*_args, **_kwargs):
        _lock_events.append("clone")
        return None

    mock_clone.side_effect = _clone_side_effect
    fake = _EventedAsyncClient(result=_FakeResp(200, _OK))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_URL, json={})
    assert resp.status_code == 200
    assert _lock_events == ["lock_enter", "http_get", "clone", "http_post", "lock_exit"]
    mock_clone.assert_called_once_with(
        "/tmp/acme_widget/pr-7", "acme", "widget", "ghp", platform_owned=True, acquire_lock=False
    )


@patch(f"{_LOCK_M}.flock_lock", lambda p: _SpyLock(p, fail=True))
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget/pr-7")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_503_when_platform_owned_lock_acquisition_fails(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    """Platform-owned checkouts rely on the clone lock for correctness; a
    failure to acquire it is a hard failure and must surface as 503 (a local
    workspace/serialization problem, not an upstream gateway error), and
    never proceed to an unguarded clone."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103/")
    resp = client.post(_URL, json={})
    assert resp.status_code == 503
    assert "clone lock" in resp.json()["detail"]
    mock_clone.assert_not_called()


@patch(f"{_LOCK_M}.flock_lock", lambda p: _SpyLock(p, fail=True))
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}._resolve_repo_path", return_value="/srv/pinned-checkout")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(
    f"{_M}.get_github_config_meta",
    return_value={**_GH_CFG, "repo_path": "/srv/pinned-checkout"},
)
def test_operator_pinned_lock_failure_degrades_gracefully(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
    """An operator-pinned path may live under a parent this service cannot
    write; failing to acquire the (best-effort) serialization lock there must
    not fail an otherwise-valid request.

    Asserting only "the request succeeded" would pass equally for a route that
    skipped locking on pinned paths altogether, so the lock_fail event is what
    actually pins "attempted, then degraded gracefully".
    """
    _lock_events.clear()
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103/")
    fake = _FakeAsyncClient(result=_FakeResp(200, _OK))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_URL, json={})
    assert resp.status_code == 200
    assert resp.json() == {**_OK, "created_at": None}
    # Attempted (and failed) exactly once; no lock_enter/lock_exit pair, since
    # acquisition never succeeded.
    assert _lock_events == ["lock_fail"]
    mock_clone.assert_called_once_with(
        "/srv/pinned-checkout", "acme", "widget", "ghp", platform_owned=False, acquire_lock=False
    )

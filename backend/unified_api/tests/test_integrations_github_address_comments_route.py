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
    mock_clone.assert_called_once_with("/tmp/acme_widget/pr-7", "acme", "widget", "ghp", platform_owned=True)
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
        "/srv/pinned-checkout", "acme", "widget", "ghp", platform_owned=False
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
    assert resp.json()["detail"] == "git clone failed: boom"


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
def test_propagates_upstream_error(mock_cfg, mock_cred, mock_path, mock_clone, monkeypatch):
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
    assert params == {"owner": "acme", "repo": "widget"}


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

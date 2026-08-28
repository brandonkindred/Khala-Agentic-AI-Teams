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


class _FakeAsyncClient:
    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

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


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_success_proxies_with_token_and_path(mock_cfg, mock_cred, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103/")
    fake = _FakeAsyncClient(result=_FakeResp(200, _OK))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_URL, json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "j1"
    assert body["unresolved_comment_count"] == 3
    # Proxied to the coding-team endpoint with the PR-number-scoped path.
    url, sent = fake.calls[0]
    assert url == "http://coding:8103/pulls/7/address-comments"
    assert sent["pr_number"] == 7
    assert sent["github_token"] == "ghp"
    assert sent["repo_path"] == "/tmp/acme_widget"


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_504_on_timeout(mock_cfg, mock_cred, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ReadTimeout("slow"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.post(_URL, json={}).status_code == 504


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_502_on_unreachable(mock_cfg, mock_cred, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ConnectError("refused"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.post(_URL, json={}).status_code == 502


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_propagates_upstream_error(mock_cfg, mock_cred, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(502, {"detail": "github api error"}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_URL, json={})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Failed to start addressing the PR comments."


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_502_on_malformed_success_body(mock_cfg, mock_cred, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(200, {"unexpected": "shape"}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.post(_URL, json={}).status_code == 502

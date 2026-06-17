"""Tests for the GitHub issue-to-PR route: POST /api/integrations/github/run-issue.

These focus on the failure modes that previously surfaced in the UI as an opaque
"0 Unknown Error": an unhandled ``subprocess``/``httpx`` exception becomes a 500
emitted outside the CORS middleware, so the browser drops the response. The route
must instead translate every failure into an ``HTTPException`` with a clear detail.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from unified_api.main import app  # noqa: E402
from unified_api.routes.integrations import (  # noqa: E402
    _ensure_repo_clone,
    _git_auth_env,
    _remote_matches,
    _resolve_repo_path,
)

client = TestClient(app, follow_redirects=False)

_M = "unified_api.routes.integrations"
_RUN_ISSUE = "/api/integrations/github/run-issue"

_GH_CFG = {"enabled": True, "owner": "acme", "repo": "widget", "repo_path": ""}


# ---------------------------------------------------------------------------
# Test doubles for the async httpx client used to reach the coding team service
# ---------------------------------------------------------------------------


class _FakeResp:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, status_code, json_data=None, text="", json_raises=False):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("response body is not JSON")
        return self._json


class _FakeAsyncClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""

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


def _ok_resp():
    return _FakeResp(
        200,
        {
            "job_id": "job-123",
            "issue_number": 7,
            "issue_url": "https://github.com/acme/widget/issues/7",
            "status": "pending",
            "message": "started",
        },
    )


# ---------------------------------------------------------------------------
# Configuration / precondition failures (4xx, no clone, no network)
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_github_config")
def test_run_issue_returns_400_when_integration_disabled(mock_cfg):
    mock_cfg.return_value = {**_GH_CFG, "enabled": False}
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@patch(f"{_M}.get_credential")
@patch(f"{_M}.get_github_config")
def test_run_issue_returns_400_when_pat_missing(mock_cfg, mock_cred):
    mock_cfg.return_value = dict(_GH_CFG)
    mock_cred.return_value = ""
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 400
    assert "PAT" in resp.json()["detail"]


@patch(f"{_M}.get_credential")
@patch(f"{_M}.get_github_config")
def test_run_issue_returns_400_when_owner_repo_missing(mock_cfg, mock_cred):
    mock_cfg.return_value = {**_GH_CFG, "owner": "", "repo": ""}
    mock_cred.return_value = "ghp_token"
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 400
    assert "owner/repo" in resp.json()["detail"]


@patch(f"{_M}.get_credential")
@patch(f"{_M}.get_github_config")
def test_run_issue_returns_503_when_service_url_unset(mock_cfg, mock_cred, monkeypatch):
    mock_cfg.return_value = dict(_GH_CFG)
    mock_cred.return_value = "ghp_token"
    monkeypatch.delenv("CODING_TEAM_SERVICE_URL", raising=False)
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone")
@patch(f"{_M}.get_credential")
@patch(f"{_M}.get_github_config")
def test_run_issue_returns_502_on_clone_failure(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    """A clone failure (e.g. missing git binary) must be a clean 502, not a 500."""
    mock_cfg.return_value = dict(_GH_CFG)
    mock_cred.return_value = "ghp_token"
    mock_clone.return_value = "git executable not found on the server; install git in the API image."
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 502
    assert "git executable not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Downstream (coding team service) transport failures — the "0 Unknown Error" class
# ---------------------------------------------------------------------------


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.get_credential", return_value="ghp_token")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_run_issue_returns_502_when_service_unreachable(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ConnectError("Connection refused"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 502
    assert "Could not reach coding team service" in resp.json()["detail"]


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.get_credential", return_value="ghp_token")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_run_issue_returns_504_on_service_timeout(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ReadTimeout("timed out"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 504
    assert "timed out" in resp.json()["detail"]


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.get_credential", return_value="ghp_token")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_run_issue_propagates_upstream_error_detail(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(409, {"detail": "issue blocked by sub-issues"}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "issue blocked by sub-issues"


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.get_credential", return_value="ghp_token")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_run_issue_upstream_error_with_non_json_body(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(500, text="internal boom", json_raises=True))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 500
    assert "internal boom" in resp.json()["detail"]


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.get_credential", return_value="ghp_token")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_run_issue_502_on_malformed_success_body(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    """A 200 from the service that is missing required fields is a 502, not a 500."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(200, {"unexpected": "shape"}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 502
    assert "unexpected response" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.get_credential", return_value="ghp_token")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_run_issue_success_returns_job(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103/")
    fake = _FakeAsyncClient(result=_ok_resp())
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "job-123"
    assert body["issue_number"] == 7
    assert body["status"] == "pending"
    # Trailing slash on the base URL must not produce a double slash.
    assert fake.calls[0][0] == "http://coding:8103/run-from-github"
    assert fake.calls[0][1]["issue_number"] == 7
    assert "github_token" in fake.calls[0][1]


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.get_credential", return_value="ghp_token")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_run_issue_forwards_base_branch(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_ok_resp())
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7, "base_branch": "develop"})
    assert resp.status_code == 200
    assert fake.calls[0][1]["base_branch"] == "develop"


# ---------------------------------------------------------------------------
# _ensure_repo_clone: never raises subprocess errors (unit tests)
# ---------------------------------------------------------------------------


def _encoded_credential(token: str) -> str:
    """Base64 basic-auth form of a fake token, built at runtime so a
    credential-shaped literal never appears in source — secret scanners
    (GitGuardian etc.) flag the pattern regardless of how fake the values are."""
    import base64

    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def test_git_auth_env_uses_basic_scheme_with_x_access_token():
    """GitHub's git smart-HTTP endpoint rejects `Bearer` with 401 `invalid
    credentials` even for a valid token (only the REST API accepts Bearer).
    Inside a container that 401 surfaces as "could not read Username ...
    terminal prompts disabled". Only Basic with the x-access-token username
    works across all GitHub token types."""
    env = _git_auth_env("secret-tok")
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {_encoded_credential('secret-tok')}"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "PATH" in env


def test_ensure_repo_clone_scrubs_encoded_token_form(tmp_path):
    """The Basic credential is a second representation of the secret; if git
    ever echoes the header value to stderr it must be scrubbed too."""
    repo = tmp_path / "checkout"
    encoded = _encoded_credential("tok-secret")
    failed = subprocess.CompletedProcess(
        args=["git", "clone"], returncode=128, stdout="", stderr=f"fatal: header Basic {encoded} rejected"
    )
    with patch(f"{_M}.subprocess.run", return_value=failed):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok-secret")
    assert err is not None
    assert encoded not in err
    assert "***" in err


def test_ensure_repo_clone_handles_missing_git_binary(tmp_path):
    repo = tmp_path / "checkout"
    with patch(f"{_M}.subprocess.run", side_effect=FileNotFoundError("git")):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is not None
    assert "git executable not found" in err


def test_ensure_repo_clone_handles_timeout(tmp_path):
    repo = tmp_path / "checkout"
    exc = subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=300)
    with patch(f"{_M}.subprocess.run", side_effect=exc):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is not None
    assert "timed out" in err


def test_ensure_repo_clone_scrubs_token_on_clone_failure(tmp_path):
    repo = tmp_path / "checkout"
    failed = subprocess.CompletedProcess(
        args=["git", "clone"], returncode=128, stdout="", stderr="fatal: bad creds tok-secret"
    )
    with patch(f"{_M}.subprocess.run", return_value=failed):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok-secret")
    assert err is not None
    assert "git clone failed" in err
    assert "tok-secret" not in err
    assert "***" in err


def test_ensure_repo_clone_clone_success(tmp_path):
    repo = tmp_path / "checkout"
    ok = subprocess.CompletedProcess(args=["git", "clone"], returncode=0, stdout="", stderr="")
    with patch(f"{_M}.subprocess.run", return_value=ok):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is None


def test_ensure_repo_clone_rejects_mismatched_existing_remote(tmp_path):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    url_check = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout="https://github.com/other/project\n", stderr=""
    )
    with patch(f"{_M}.subprocess.run", return_value=url_check):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is not None
    assert "does not match" in err


def test_ensure_repo_clone_fetch_success_on_existing_checkout(tmp_path):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    url_check = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout="https://github.com/acme/widget\n", stderr=""
    )
    fetch_ok = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    with patch(f"{_M}.subprocess.run", side_effect=[url_check, fetch_ok]):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is None


def test_ensure_repo_clone_fetch_failure_scrubs_token(tmp_path):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    url_check = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout="https://github.com/acme/widget\n", stderr=""
    )
    fetch_fail = subprocess.CompletedProcess(
        args=["git"], returncode=1, stdout="", stderr="fatal: auth tok-secret failed"
    )
    with patch(f"{_M}.subprocess.run", side_effect=[url_check, fetch_fail]):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok-secret")
    assert err is not None
    assert "git fetch failed" in err
    assert "tok-secret" not in err


def test_ensure_repo_clone_mkdir_failure_reports_workspace_error(tmp_path):
    # repo_path's parent already exists as a *file*, so mkdir raises OSError.
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    repo = blocker / "checkout"
    err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is not None
    assert "could not prepare workspace dir" in err


def test_ensure_repo_clone_lock_open_failure_reports_lock_error(tmp_path):
    repo = tmp_path / "checkout"
    # The only open() in _ensure_repo_clone is the lock file; failing it must
    # surface as a lock error, NOT the git-missing message.
    with patch("builtins.open", side_effect=PermissionError("denied")):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is not None
    assert "could not acquire clone lock" in err
    assert "git executable not found" not in err


def test_ensure_repo_clone_flock_failure_reports_lock_error(tmp_path):
    repo = tmp_path / "checkout"
    with patch(f"{_M}.fcntl.flock", side_effect=OSError("locked")):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is not None
    assert "could not acquire clone lock" in err


# ---------------------------------------------------------------------------
# _resolve_repo_path: per-issue checkout isolation
# ---------------------------------------------------------------------------


def test_resolve_repo_path_namespaces_per_issue_under_agent_cache(monkeypatch):
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    path = _resolve_repo_path(dict(_GH_CFG), issue_number=42)
    assert path == "/cache/github_workspaces/acme/widget/issue-42"


def test_resolve_repo_path_distinct_issues_map_to_distinct_paths(monkeypatch):
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    cfg = dict(_GH_CFG)
    assert _resolve_repo_path(cfg, issue_number=1) != _resolve_repo_path(cfg, issue_number=2)


def test_resolve_repo_path_uses_se_workspace_dir_with_issue(monkeypatch):
    monkeypatch.setenv("SE_WORKSPACE_DIR", "/work")
    path = _resolve_repo_path(dict(_GH_CFG), issue_number=9)
    assert path == "/work/acme_widget/issue-9"


def test_resolve_repo_path_without_issue_is_repo_level(monkeypatch):
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    # The PR-review path passes no issue number and must keep the repo-level path.
    assert _resolve_repo_path(dict(_GH_CFG)) == "/cache/github_workspaces/acme/widget"


def test_resolve_repo_path_without_issue_uses_se_workspace_dir(monkeypatch):
    monkeypatch.setenv("SE_WORKSPACE_DIR", "/work")
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    # No issue number + workspace-root env → repo-level path under that root.
    assert _resolve_repo_path(dict(_GH_CFG)) == "/work/acme_widget"


def test_resolve_repo_path_operator_override_returned_verbatim():
    cfg = {"enabled": True, "owner": "acme", "repo": "widget", "repo_path": "/srv/checkout"}
    # An operator-pinned checkout is never per-issue-namespaced.
    assert _resolve_repo_path(cfg, issue_number=42) == "/srv/checkout"


# ---------------------------------------------------------------------------
# run_github_issue: forwards a per-issue checkout + ephemeral cleanup flag
# ---------------------------------------------------------------------------


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.get_credential", return_value="ghp_token")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_run_issue_forwards_per_issue_checkout_and_cleanup_flag(mock_cfg, mock_cred, mock_clone, monkeypatch):
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_ok_resp())
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 200
    payload = fake.calls[0][1]
    assert payload["repo_path"] == "/cache/github_workspaces/acme/widget/issue-7"
    assert payload["cleanup_checkout_on_success"] is True
    # The clone target must be the per-issue folder, not the repo-level path.
    assert mock_clone.call_args[0][0] == "/cache/github_workspaces/acme/widget/issue-7"


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.get_credential", return_value="ghp_token")
@patch(
    f"{_M}.get_github_config",
    return_value={"enabled": True, "owner": "acme", "repo": "widget", "repo_path": "/srv/checkout"},
)
def test_run_issue_operator_override_disables_cleanup(mock_cfg, mock_cred, mock_clone, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_ok_resp())
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 200
    payload = fake.calls[0][1]
    assert payload["repo_path"] == "/srv/checkout"
    assert payload["cleanup_checkout_on_success"] is False


# ---------------------------------------------------------------------------
# _remote_matches: exact owner/repo comparison (no substring false positives)
# ---------------------------------------------------------------------------


def test_remote_matches_exact_https():
    assert _remote_matches("https://github.com/acme/widget.git", "acme", "widget") is True
    assert _remote_matches("https://github.com/acme/widget", "acme", "widget") is True


def test_remote_matches_is_case_insensitive():
    assert _remote_matches("https://github.com/ACME/Widget.git", "acme", "widget") is True


def test_remote_matches_accepts_scp_form():
    assert _remote_matches("git@github.com:acme/widget.git", "acme", "widget") is True


def test_remote_matches_rejects_repo_prefix_substring():
    # 'acme/widget' is a substring of 'acme/widget-extra' but must NOT match.
    assert _remote_matches("https://github.com/acme/widget-extra.git", "acme", "widget") is False


def test_remote_matches_rejects_owner_suffix_substring():
    # 'acme/widget' is a suffix of 'notacme/widget' but must NOT match.
    assert _remote_matches("https://github.com/notacme/widget.git", "acme", "widget") is False


def test_remote_matches_rejects_short_url():
    assert _remote_matches("widget", "acme", "widget") is False


def test_ensure_repo_clone_rejects_substring_remote(tmp_path):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    url_check = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout="https://github.com/acme/widget-extra.git\n", stderr=""
    )
    with patch(f"{_M}.subprocess.run", return_value=url_check):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is not None
    assert "does not match" in err


def test_ensure_repo_clone_accepts_scp_form_remote(tmp_path):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    url_check = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout="git@github.com:acme/widget.git\n", stderr=""
    )
    fetch_ok = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    with patch(f"{_M}.subprocess.run", side_effect=[url_check, fetch_ok]):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is None


# ---------------------------------------------------------------------------
# _resolve_repo_path: owner/repo path-traversal sanitization
# ---------------------------------------------------------------------------


def test_resolve_repo_path_rejects_owner_traversal(monkeypatch):
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    cfg = {"enabled": True, "owner": "../../etc", "repo": "widget", "repo_path": ""}
    with pytest.raises(HTTPException) as exc:
        _resolve_repo_path(cfg, issue_number=1)
    assert exc.value.status_code == 400


def test_resolve_repo_path_rejects_repo_separator(monkeypatch):
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    cfg = {"enabled": True, "owner": "acme", "repo": "a/b", "repo_path": ""}
    with pytest.raises(HTTPException) as exc:
        _resolve_repo_path(cfg, issue_number=1)
    assert exc.value.status_code == 400

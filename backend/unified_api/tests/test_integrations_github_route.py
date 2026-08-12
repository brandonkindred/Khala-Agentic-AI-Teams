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

# Full config shape returned by get_github_config() (includes the status booleans the
# GET response builder reads), used by the /github config-status tests below.
_GH_STATUS_CFG = {
    "enabled": True,
    "owner": "acme",
    "repo": "widget",
    "default_label": "ai-ready",
    "repo_path": "",
    "token_configured": False,
}

_GITHUB_CFG_ENDPOINT = "/api/integrations/github"


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
    """Async-context-manager stand-in for httpx.AsyncClient.

    ``calls`` records every ``post`` as a ``(url, json_payload)`` tuple; use
    ``last_payload()`` to read the JSON body of the most recent request rather
    than indexing the tuple structure directly. ``get`` handles the
    ``_assert_pat_can_reach_repo`` reachability probe (any ``/repos/...`` URL),
    answering with ``repo_access_status`` (200 by default, i.e. "reachable") so
    routes that don't exercise that gate are unaffected; the probe's own calls
    are recorded separately in ``repo_checks``, not ``calls``.
    """

    def __init__(self, *, result=None, exc=None, repo_access_status=200):
        self._result = result
        self._exc = exc
        self._repo_access_status = repo_access_status
        self.calls = []
        self.repo_checks = []

    def last_payload(self):
        """Return the JSON payload of the most recent post (the tuple's [1])."""
        return self.calls[-1][1]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, params=None, headers=None):
        self.repo_checks.append(url)
        return _FakeResp(self._repo_access_status, json_data={"full_name": "acme/widget"})

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


@patch(f"{_M}.get_github_config_meta")
def test_run_issue_returns_400_when_integration_disabled(mock_cfg):
    mock_cfg.return_value = {**_GH_CFG, "enabled": False}
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("", True))
@patch(f"{_M}.get_github_config_meta")
def test_run_issue_returns_400_when_pat_missing(mock_cfg, mock_status):
    # Store reachable, token genuinely absent → 400 "not configured" (a single read
    # reports value + reachability, so no separate probe and no env dependency).
    mock_cfg.return_value = dict(_GH_CFG)
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 400
    assert "PAT" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("", False))
@patch(f"{_M}.get_github_config_meta")
def test_run_issue_returns_503_when_credential_store_unreachable(mock_cfg, mock_status):
    # An empty PAT while the credential store is unreachable (the SAME read reports it)
    # is a transient outage (503), not a missing-credential operator error (400).
    mock_cfg.return_value = dict(_GH_CFG)
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 503
    assert "credential store" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta")
def test_run_issue_returns_400_when_owner_repo_missing(mock_cfg, mock_status):
    mock_cfg.return_value = {**_GH_CFG, "owner": "", "repo": ""}
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 400
    assert "owner/repo" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta")
def test_run_issue_returns_503_when_service_url_unset(mock_cfg, mock_status, monkeypatch):
    mock_cfg.return_value = dict(_GH_CFG)
    monkeypatch.delenv("CODING_TEAM_SERVICE_URL", raising=False)
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone")
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta")
def test_run_issue_returns_502_on_clone_failure(mock_cfg, mock_status, mock_clone, mock_path, monkeypatch):
    """A clone failure (e.g. missing git binary) must be a clean 502, not a 500."""
    mock_cfg.return_value = dict(_GH_CFG)
    mock_clone.return_value = "git executable not found on the server; install git in the API image."
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 502
    assert "git executable not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /github config status: credential_store_unreachable flag
#
# The panel derives reachability from get_github_config()'s store_reachable (the same
# single read that backs token_configured) — no separate probe — so these patch
# get_github_config directly.
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_github_config")
def test_get_github_flags_store_unreachable(mock_cfg):
    # store_reachable=False → the panel learns the store is down and warns.
    mock_cfg.return_value = {**_GH_STATUS_CFG, "store_reachable": False}
    resp = client.get(_GITHUB_CFG_ENDPOINT)
    assert resp.status_code == 200
    assert resp.json()["credential_store_unreachable"] is True


@patch(f"{_M}.get_github_config")
def test_get_github_store_reachable_flag_false(mock_cfg):
    # store_reachable=True → not flagged.
    mock_cfg.return_value = {**_GH_STATUS_CFG, "token_configured": True, "store_reachable": True}
    resp = client.get(_GITHUB_CFG_ENDPOINT)
    assert resp.status_code == 200
    assert resp.json()["credential_store_unreachable"] is False


@patch(f"{_M}.get_github_config")
def test_get_github_not_flagged_when_postgres_unconfigured(mock_cfg):
    # Postgres unset → get_credential_status returns reachable=True ("absent", not an
    # outage), so the unreachable flag stays False.
    mock_cfg.return_value = {**_GH_STATUS_CFG, "store_reachable": True}
    resp = client.get(_GITHUB_CFG_ENDPOINT)
    assert resp.status_code == 200
    assert resp.json()["credential_store_unreachable"] is False


@patch(
    f"{_M}.get_github_config_meta",
    return_value={"enabled": True, "owner": "acme", "repo": "widget", "default_label": "lbl"},
)
@patch(f"{_M}.get_github_config", side_effect=RuntimeError("store stalled"))
def test_get_github_degrades_to_unreachable_on_build_failure(mock_cfg, mock_meta):
    # A synchronous error building the config response is caught and reported as a
    # degraded unreachable response rather than 500-ing — AND the JSON-only settings
    # (owner/repo, no DB needed) are PRESERVED rather than blanked (fix: "store down" is
    # not "nothing configured").
    resp = client.get(_GITHUB_CFG_ENDPOINT)
    assert resp.status_code == 200
    body = resp.json()
    assert body["credential_store_unreachable"] is True
    assert (body["owner"], body["repo"]) == ("acme", "widget")
    assert body["token_configured"] is False


@patch(
    f"{_M}.get_github_config_meta",
    return_value={"enabled": True, "owner": "acme", "repo": "widget", "default_label": "lbl"},
)
@patch(f"{_M}.get_github_config")
def test_get_github_degrades_on_probe_timeout(mock_cfg, mock_meta, monkeypatch):
    # Exercises the wait_for TIMEOUT path (not just the synchronous-error path): _build
    # blocks past the budget, so the request returns degraded-unreachable (with settings
    # preserved) instead of hanging. Patch the shared budget tiny so we can prove the
    # timeout fired by elapsed time.
    import time

    from shared.postgres import client as pg_client

    monkeypatch.setattr(pg_client, "default_probe_budget", lambda: 0.2)
    mock_cfg.side_effect = lambda: time.sleep(1.5) or {**dict(_GH_STATUS_CFG), "store_reachable": True}
    t0 = time.monotonic()
    resp = client.get(_GITHUB_CFG_ENDPOINT)
    elapsed = time.monotonic() - t0
    body = resp.json()
    assert resp.status_code == 200
    assert body["credential_store_unreachable"] is True
    assert (body["owner"], body["repo"]) == ("acme", "widget")  # settings preserved
    # Returned well before the 1.5s sleep → the timeout branch fired (not the full block).
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# Downstream (coding team service) transport failures — the "0 Unknown Error" class
# ---------------------------------------------------------------------------


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_run_issue_returns_502_when_service_unreachable(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ConnectError("Connection refused"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 502
    assert "Could not reach coding team service" in resp.json()["detail"]


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_run_issue_returns_504_on_service_timeout(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ReadTimeout("timed out"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 504
    assert "timed out" in resp.json()["detail"]


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_run_issue_propagates_upstream_error_detail(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(409, {"detail": "issue blocked by sub-issues"}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 409
    # 4xx upstream detail is client-actionable, so it is passed through (bounded).
    assert resp.json()["detail"] == "issue blocked by sub-issues"


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_run_issue_upstream_error_with_non_json_body(mock_cfg, mock_cred, mock_clone, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(500, text="internal boom", json_raises=True))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to start the coding job."


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
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
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
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
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
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
    """_ensure_repo_clone reports a workspace error (not a git error) when the
    parent dir can't be created because the parent path is a file."""
    # repo_path's parent already exists as a *file*, so mkdir raises OSError.
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    repo = blocker / "checkout"
    err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is not None
    assert "could not prepare workspace dir" in err


def test_ensure_repo_clone_lock_open_failure_reports_lock_error(tmp_path):
    """A failure opening the clone lock surfaces as a lock error, not the
    git-executable-missing message."""
    repo = tmp_path / "checkout"
    # The only open() in _ensure_repo_clone is the lock file; failing *just* that
    # open (not every open) must surface as a lock error, NOT the git-missing
    # message. A targeted side effect avoids disturbing any other open().
    real_open = open

    def _open_boom(path, *a, **k):
        if str(path).endswith(".clone.lock"):
            raise PermissionError("denied")
        return real_open(path, *a, **k)

    with patch("builtins.open", _open_boom):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is not None
    assert "could not acquire clone lock" in err
    assert "git executable not found" not in err


def test_ensure_repo_clone_flock_failure_reports_lock_error(tmp_path):
    """A flock failure surfaces as a lock error rather than escaping."""
    repo = tmp_path / "checkout"
    with patch(f"{_M}.fcntl.flock", side_effect=OSError("locked")):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is not None
    assert "could not acquire clone lock" in err


# ---------------------------------------------------------------------------
# _resolve_repo_path: per-issue checkout isolation
# ---------------------------------------------------------------------------


def test_resolve_repo_path_namespaces_per_issue_under_agent_cache(monkeypatch):
    """AGENT_CACHE-derived path is namespaced per issue under github_workspaces."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    path = _resolve_repo_path(dict(_GH_CFG), "acme", "widget", issue_number=42)
    assert path == "/cache/github_workspaces/acme/widget/issue-42"


def test_resolve_repo_path_default_agent_cache_fallback(monkeypatch):
    """Unset AGENT_CACHE + no workspace root → the relative '.agent_cache' default,
    resolved to an absolute path under the github_workspaces layout. Asserts on the
    absolute-ness and the trailing layout rather than the exact cwd, so it's
    deterministic regardless of the working directory or test runner."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    path = _resolve_repo_path(dict(_GH_CFG), "acme", "widget", issue_number=42)
    assert Path(path).is_absolute()
    assert path.endswith("/.agent_cache/github_workspaces/acme/widget/issue-42")


def test_resolve_repo_path_distinct_issues_map_to_distinct_paths(monkeypatch):
    """Two distinct issue numbers resolve to two distinct checkout paths."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    cfg = dict(_GH_CFG)
    assert _resolve_repo_path(cfg, "acme", "widget", issue_number=1) != _resolve_repo_path(
        cfg, "acme", "widget", issue_number=2
    )


def test_resolve_repo_path_uses_se_workspace_dir_with_issue(monkeypatch):
    """SE_WORKSPACE_DIR (highest priority) yields <root>/<owner>_<repo>/issue-N."""
    monkeypatch.setenv("SE_WORKSPACE_DIR", "/work")
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    path = _resolve_repo_path(dict(_GH_CFG), "acme", "widget", issue_number=9)
    assert path == "/work/acme_widget/issue-9"


def test_resolve_repo_path_uses_workspace_root_with_issue(monkeypatch):
    """With SE_WORKSPACE_DIR unset, WORKSPACE_ROOT yields <root>/<owner>_<repo>/issue-N."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", "/ws")
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    path = _resolve_repo_path(dict(_GH_CFG), "acme", "widget", issue_number=5)
    assert path == "/ws/acme_widget/issue-5"


def test_resolve_repo_path_without_issue_is_repo_level(monkeypatch):
    """No issue number (PR-review path) keeps the repo-level path, no issue-N segment."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    assert _resolve_repo_path(dict(_GH_CFG), "acme", "widget") == "/cache/github_workspaces/acme/widget"


def test_resolve_repo_path_without_issue_uses_se_workspace_dir(monkeypatch):
    """No issue number + workspace-root env → repo-level path under that root."""
    monkeypatch.setenv("SE_WORKSPACE_DIR", "/work")
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    assert _resolve_repo_path(dict(_GH_CFG), "acme", "widget") == "/work/acme_widget"


def test_resolve_repo_path_operator_override_returned_verbatim():
    """An operator-pinned repo_path is returned verbatim, never per-issue-namespaced."""
    cfg = {"enabled": True, "owner": "acme", "repo": "widget", "repo_path": "/srv/checkout"}
    assert _resolve_repo_path(cfg, "acme", "widget", issue_number=42) == "/srv/checkout"


# ---------------------------------------------------------------------------
# run_github_issue: forwards a per-issue checkout + ephemeral cleanup flag
# ---------------------------------------------------------------------------


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_run_issue_forwards_per_issue_checkout_and_cleanup_flag(mock_cfg, mock_cred, mock_clone, monkeypatch):
    """An auto-derived run clones the per-issue folder and forwards repo_path +
    cleanup_checkout_on_success=True to the coding team."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_ok_resp())
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 200
    payload = fake.last_payload()
    assert payload["repo_path"] == "/cache/github_workspaces/acme/widget/issue-7"
    assert payload["cleanup_checkout_on_success"] is True
    # The clone target must be the per-issue folder, not the repo-level path, and
    # an auto-derived checkout is platform-owned (so it takes the sibling lock).
    mock_clone.assert_called_once_with(
        "/cache/github_workspaces/acme/widget/issue-7",
        "acme",
        "widget",
        "ghp_token",
        platform_owned=True,
    )


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_run_issue_targets_body_supplied_repo(mock_cfg, mock_cred, mock_clone, monkeypatch):
    """A body-supplied owner/repo runs the issue in THAT repository — the configured
    default is only a fallback; the PAT's own authorization is the access list."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_ok_resp())
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7, "owner": "other", "repo": "thing"})
    assert resp.status_code == 200
    payload = fake.last_payload()
    assert payload["owner"] == "other"
    assert payload["repo"] == "thing"
    assert payload["repo_path"] == "/cache/github_workspaces/other/thing/issue-7"
    mock_clone.assert_called_once_with(
        "/cache/github_workspaces/other/thing/issue-7",
        "other",
        "thing",
        "ghp_token",
        platform_owned=True,
    )


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_run_issue_owner_without_repo_is_400(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    resp = client.post(_RUN_ISSUE, json={"issue_number": 7, "owner": "other"})
    assert resp.status_code == 400
    assert "together" in resp.json()["detail"]


@patch(f"{_M}._ensure_repo_clone", return_value=None)
@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(
    f"{_M}.get_github_config_meta",
    return_value={"enabled": True, "owner": "acme", "repo": "widget", "repo_path": "/srv/checkout"},
)
def test_run_issue_operator_override_disables_cleanup(mock_cfg, mock_cred, mock_clone, monkeypatch):
    """An operator-pinned repo_path is forwarded verbatim with cleanup disabled, and
    the clone still runs once against the override path (the run-issue route always
    ensures the checkout; only the auto-cleanup is suppressed for operator paths)."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_ok_resp())
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_RUN_ISSUE, json={"issue_number": 7})
    assert resp.status_code == 200
    payload = fake.last_payload()
    assert payload["repo_path"] == "/srv/checkout"
    assert payload["cleanup_checkout_on_success"] is False
    # Cloning is not skipped for an override — it runs once against the pinned
    # path, and platform_owned=False so it does NOT require a sibling lock (the
    # operator's parent dir may be read-only).
    mock_clone.assert_called_once_with("/srv/checkout", "acme", "widget", "ghp_token", platform_owned=False)


# ---------------------------------------------------------------------------
# _remote_matches: exact owner/repo comparison (no substring false positives)
# ---------------------------------------------------------------------------


def test_remote_matches_exact_https():
    """An exact https remote (with or without .git) matches owner/repo."""
    assert _remote_matches("https://github.com/acme/widget.git", "acme", "widget") is True
    assert _remote_matches("https://github.com/acme/widget", "acme", "widget") is True


def test_remote_matches_is_case_insensitive():
    """owner/repo comparison is case-insensitive (GitHub treats them that way)."""
    assert _remote_matches("https://github.com/ACME/Widget.git", "acme", "widget") is True


def test_remote_matches_accepts_scp_form():
    """The git@host:owner/repo scp form matches the same owner/repo."""
    assert _remote_matches("git@github.com:acme/widget.git", "acme", "widget") is True


def test_remote_matches_rejects_repo_prefix_substring():
    """'acme/widget' is a substring of 'acme/widget-extra' but must NOT match."""
    assert _remote_matches("https://github.com/acme/widget-extra.git", "acme", "widget") is False


def test_remote_matches_rejects_owner_suffix_substring():
    """'acme/widget' is a suffix of 'notacme/widget' but must NOT match."""
    assert _remote_matches("https://github.com/notacme/widget.git", "acme", "widget") is False


def test_remote_matches_rejects_short_url():
    """A URL with fewer than two path segments never matches."""
    assert _remote_matches("widget", "acme", "widget") is False


def test_ensure_repo_clone_rejects_substring_remote(tmp_path):
    """An existing checkout whose remote only substring-matches owner/repo is
    rejected with a 'does not match' error rather than reused."""
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
    """An existing checkout whose remote is the scp form of the same owner/repo is
    accepted and fetched (no 'does not match' error)."""
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    url_check = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout="git@github.com:acme/widget.git\n", stderr=""
    )
    fetch_ok = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    with patch(f"{_M}.subprocess.run", side_effect=[url_check, fetch_ok]):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")
    assert err is None


def test_ensure_repo_clone_operator_path_skips_sibling_lock(tmp_path):
    """An operator-pinned checkout (platform_owned=False) fetches without creating a
    sibling lock in the parent, so it still works when the parent is not writable by
    the service (only the checkout itself need be)."""
    repo = tmp_path / "srv" / "repo"
    (repo / ".git").mkdir(parents=True)
    url_check = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout="https://github.com/acme/widget.git\n", stderr=""
    )
    fetch_ok = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    # If a sibling lock were attempted, open() would still succeed here (tmp is
    # writable); assert instead that no lock file is created, proving the lock path
    # is skipped entirely for operator-pinned checkouts.
    with patch(f"{_M}.subprocess.run", side_effect=[url_check, fetch_ok]):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok", platform_owned=False)
    assert err is None
    assert not (tmp_path / "srv" / ".repo.clone.lock").exists()


def test_ensure_repo_clone_platform_owned_creates_sibling_lock(tmp_path):
    """A platform-owned checkout takes the sibling lock (it is created beside the
    checkout) so concurrent clone/fetch is serialized."""
    repo = tmp_path / "issue-7"
    (repo / ".git").mkdir(parents=True)
    url_check = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout="https://github.com/acme/widget.git\n", stderr=""
    )
    fetch_ok = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    with patch(f"{_M}.subprocess.run", side_effect=[url_check, fetch_ok]):
        err = _ensure_repo_clone(str(repo), "acme", "widget", "tok")  # platform_owned=True default
    assert err is None
    assert (tmp_path / ".issue-7.clone.lock").exists()


# ---------------------------------------------------------------------------
# _resolve_repo_path: owner/repo path-traversal sanitization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -42])
def test_resolve_repo_path_rejects_nonpositive_issue_number(monkeypatch, bad):
    """A non-positive issue number (which would build a degenerate issue-0/issue--1
    segment) is rejected with HTTP 400."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    with pytest.raises(HTTPException) as exc:
        _resolve_repo_path(dict(_GH_CFG), "acme", "widget", issue_number=bad)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "field,value",
    [
        # owner and repo are BOTH path components, so both must reject traversal /
        # separator / backslash / null-byte / dot / surrounding-whitespace injection.
        ("owner", "../../etc"),
        ("owner", "a/b"),
        ("owner", "a\\b"),
        ("owner", "a\x00b"),
        ("owner", ".."),
        ("owner", " acme"),
        ("repo", "../../etc"),
        ("repo", "a/b"),
        ("repo", "a\\b"),
        ("repo", "a\x00b"),
        ("repo", ".."),
        ("repo", "widget "),
    ],
)
def test_resolve_repo_path_rejects_unsafe_owner_or_repo(monkeypatch, field, value):
    """Both owner and repo are validated against path-injection characters; an unsafe
    value in either yields HTTP 400 before any path is built."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    # repo_path="" means "no operator override", so the function proceeds to
    # owner/repo path derivation (and thus the sanitization checks under test)
    # rather than returning an override verbatim.
    cfg = {"enabled": True, "owner": "", "repo": "", "repo_path": ""}
    owner = "acme" if field != "owner" else value
    repo = "widget" if field != "repo" else value
    with pytest.raises(HTTPException) as exc:
        _resolve_repo_path(cfg, owner, repo, issue_number=1)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("missing", ["owner", "repo"])
def test_resolve_repo_path_rejects_missing_owner_or_repo(monkeypatch, missing):
    """A blank owner/repo yields HTTP 400 (enforcing the documented precondition)
    rather than building a degenerate path."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    cfg = {"enabled": True, "owner": "acme", "repo": "widget", "repo_path": ""}
    owner = "" if missing == "owner" else "acme"
    repo = "" if missing == "repo" else "widget"
    with pytest.raises(HTTPException) as exc:
        _resolve_repo_path(cfg, owner, repo, issue_number=1)
    assert exc.value.status_code == 400


def test_resolve_repo_path_override_not_applied_to_other_repo(monkeypatch):
    """The operator's pinned repo_path applies only to the configured default repo; a
    run against a *different* PAT-accessible repo derives its own per-issue path so the
    pinned checkout (whose remote wouldn't match) is never reused for it."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    cfg = {"enabled": True, "owner": "acme", "repo": "widget", "repo_path": "/srv/checkout"}
    assert _resolve_repo_path(cfg, "other", "thing", issue_number=3) == "/cache/github_workspaces/other/thing/issue-3"


# ---------------------------------------------------------------------------
# POST /github/events: webhook receiver for the "@khala review" PR-comment trigger
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
import hmac  # noqa: E402

_EVENTS = "/api/integrations/github/events"


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_github_events_ping_returns_ok():
    """A ping event with a valid signature short-circuits to {"ok": true}."""
    body = b'{"zen": "Keep it simple."}'
    with patch("unified_api.integrations_store.get_github_webhook_secret_status", return_value=("whsec", True)):
        resp = client.post(
            _EVENTS,
            content=body,
            headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": _sign("whsec", body)},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_github_events_rejects_bad_signature():
    body = b'{"action":"created"}'
    with patch("unified_api.integrations_store.get_github_webhook_secret_status", return_value=("whsec", True)):
        resp = client.post(
            _EVENTS,
            content=body,
            headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": _sign("wrong", body)},
        )
    assert resp.status_code == 401
    assert "signature" in resp.json()["detail"].lower()


def test_github_events_refuses_review_events_without_secret():
    """With no secret configured (store reachable, genuinely unset), a review-triggering
    event is refused with 403 and never dispatched — an unsigned request must not be able
    to forge a collaborator comment and spend review budget."""
    body = b'{"action":"created"}'
    with (
        patch("unified_api.integrations_store.get_github_webhook_secret_status", return_value=(None, True)),
        patch("unified_api.github_events_handler.dispatch_github_event") as disp,
    ):
        resp = client.post(_EVENTS, content=body, headers={"X-GitHub-Event": "issue_comment"})
    assert resp.status_code == 403
    assert "secret" in resp.json()["detail"].lower()
    disp.assert_not_called()


def test_github_events_ping_allowed_without_secret():
    """``ping`` is exempt from the no-secret refusal so an operator can verify webhook
    delivery during setup, before a signing secret is configured. It never dispatches
    review work, so allowing it is safe."""
    body = b'{"zen": "Keep it simple."}'
    with (
        patch("unified_api.integrations_store.get_github_webhook_secret_status", return_value=(None, True)),
        patch("unified_api.github_events_handler.dispatch_github_event") as disp,
    ):
        resp = client.post(_EVENTS, content=body, headers={"X-GitHub-Event": "ping"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    disp.assert_not_called()


def test_github_events_fails_closed_when_secret_store_unreachable():
    """No secret found AND the store is unreachable (vs. genuinely unconfigured) must
    reject with 503, never silently skip verification — an unreachable store could be
    hiding a real stored secret, and treating that the same as "not configured" would
    let a forged, unsigned payload through for the duration of the outage."""
    body = b'{"action":"created"}'
    with (
        patch("unified_api.integrations_store.get_github_webhook_secret_status", return_value=(None, False)),
        patch("unified_api.github_events_handler.dispatch_github_event") as disp,
    ):
        resp = client.post(_EVENTS, content=body, headers={"X-GitHub-Event": "issue_comment"})
    assert resp.status_code == 503
    assert "credential store" in resp.json()["detail"].lower()
    disp.assert_not_called()


def test_github_events_dispatches_valid_signed_comment():
    body = b'{"action":"created","issue":{"number":42}}'
    with (
        patch("unified_api.integrations_store.get_github_webhook_secret_status", return_value=("whsec", True)),
        patch("unified_api.github_events_handler.dispatch_github_event") as disp,
    ):
        resp = client.post(
            _EVENTS,
            content=body,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "delivery-123",
                "X-Hub-Signature-256": _sign("whsec", body),
            },
        )
    assert resp.status_code == 200
    disp.assert_called_once()
    assert disp.call_args[0][0] == "issue_comment"
    assert disp.call_args[0][1] == {"action": "created", "issue": {"number": 42}}
    assert disp.call_args[0][2] == "delivery-123"


def test_github_events_returns_400_on_invalid_json():
    body = b"not json"
    with (
        patch("unified_api.integrations_store.get_github_webhook_secret_status", return_value=("whsec", True)),
        patch("unified_api.github_events_handler.dispatch_github_event") as disp,
    ):
        resp = client.post(
            _EVENTS,
            content=body,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-Hub-Signature-256": _sign("whsec", body),
            },
        )
    assert resp.status_code == 400
    assert "Invalid JSON" in resp.json()["detail"]
    disp.assert_not_called()


def test_github_events_returns_400_on_non_object_body():
    """Valid JSON that is not an object (e.g. `[]`) is rejected with 400 rather than
    letting dispatch's payload.get(...) raise AttributeError → an unhandled 500."""
    body = b"[]"
    with (
        patch("unified_api.integrations_store.get_github_webhook_secret_status", return_value=("whsec", True)),
        patch("unified_api.github_events_handler.dispatch_github_event") as disp,
    ):
        resp = client.post(
            _EVENTS,
            content=body,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-Hub-Signature-256": _sign("whsec", body),
            },
        )
    assert resp.status_code == 400
    assert "JSON object" in resp.json()["detail"]
    disp.assert_not_called()


def test_github_events_non_ascii_signature_returns_401_not_500():
    """A crafted non-ASCII X-Hub-Signature-256 must be a clean 401, not a 500 from a
    TypeError inside hmac.compare_digest. The header is sent as raw bytes with a byte
    >= 0x80 (Starlette decodes it latin-1 server-side, reproducing the attack)."""
    body = b'{"action":"created"}'
    with patch("unified_api.integrations_store.get_github_webhook_secret_status", return_value=("whsec", True)):
        resp = client.post(
            _EVENTS,
            content=body,
            headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": b"sha256=deadbeef\xff"},
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /github config status: webhook_secret_configured flag
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_github_config")
def test_get_github_reports_webhook_secret_configured(mock_cfg):
    mock_cfg.return_value = {**_GH_STATUS_CFG, "store_reachable": True, "webhook_secret_configured": True}
    resp = client.get(_GITHUB_CFG_ENDPOINT)
    assert resp.status_code == 200
    assert resp.json()["webhook_secret_configured"] is True


@patch(f"{_M}.get_github_config")
def test_get_github_webhook_secret_unconfigured_defaults_false(mock_cfg):
    mock_cfg.return_value = {**_GH_STATUS_CFG, "store_reachable": True}
    resp = client.get(_GITHUB_CFG_ENDPOINT)
    assert resp.status_code == 200
    assert resp.json()["webhook_secret_configured"] is False


# ---------------------------------------------------------------------------
# POST /github/review-pr: thin wrapper over the shared _start_pr_review helper
# ---------------------------------------------------------------------------

_REVIEW_PR = "/api/integrations/github/review-pr"


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_review_pr_success_forwards_pr_number(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(
        result=_FakeResp(
            200,
            {
                "job_id": "rev-1",
                "pr_number": 7,
                "pr_url": "https://github.com/acme/widget/pull/7",
                "status": "pending",
                "message": "started",
            },
        )
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_PR, json={"pr_number": 7})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "rev-1"
    assert fake.calls[0][0] == "http://coding:8103/review-pr"
    assert fake.calls[0][1]["pr_number"] == 7


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_review_pr_forwards_base_branch(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(
        result=_FakeResp(
            200,
            {"job_id": "rev-2", "pr_number": 7, "pr_url": "u", "status": "pending", "message": ""},
        )
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_PR, json={"pr_number": 7, "base_branch": "develop"})
    assert resp.status_code == 200
    assert fake.calls[0][1]["base_branch"] == "develop"


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_review_pr_forwards_owner_repo_from_body(mock_cfg, mock_cred, monkeypatch):
    """POST /github/review-pr accepts owner/repo in the body and forwards them to the coding
    team service, overriding the configured default (acme/widget)."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(
        result=_FakeResp(
            200,
            {"job_id": "rev-3", "pr_number": 7, "pr_url": "u", "status": "pending", "message": ""},
        )
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_PR, json={"pr_number": 7, "owner": "other", "repo": "thing"})
    assert resp.status_code == 200
    forwarded = fake.calls[0][1]
    assert forwarded["owner"] == "other"
    assert forwarded["repo"] == "thing"


# ---------------------------------------------------------------------------
# _redact_url_userinfo: never leak embedded credentials in checkout-mismatch errors
# ---------------------------------------------------------------------------

from unified_api.routes.integrations import _redact_url_userinfo  # noqa: E402


def test_redact_url_userinfo_strips_embedded_credentials():
    # https URLs with user:pass@ userinfo — the credential must be gone.
    assert _redact_url_userinfo("https://user:ghp_secret@github.com/acme/widget.git") == (
        "https://github.com/acme/widget.git"
    )
    assert _redact_url_userinfo("https://tokenonly@github.com/acme/widget") == "https://github.com/acme/widget"
    # A port is preserved; a credential-free URL is unchanged.
    assert _redact_url_userinfo("https://x:y@git.example.com:8443/a/b") == "https://git.example.com:8443/a/b"
    assert _redact_url_userinfo("https://github.com/acme/widget.git") == "https://github.com/acme/widget.git"
    # An scp-like ssh remote (no URL authority) drops anything before the last '@'.
    assert _redact_url_userinfo("git@github.com:acme/widget.git") == "github.com:acme/widget.git"
    # A malformed/out-of-range port must NOT make the helper raise (urlparse defers port
    # validation to attribute access) — it degrades to "<redacted>" instead of leaking or crashing.
    assert _redact_url_userinfo("https://user:tok@github.com:99999999/acme/widget.git") == "<redacted>"
    assert _redact_url_userinfo("https://user:tok@github.com:notaport/acme/widget.git") == "<redacted>"


# ---------------------------------------------------------------------------
# POST /github/reviews/{job_id}/issues (create issues from pre-existing findings)
# ---------------------------------------------------------------------------

_REVIEW_ISSUES = "/api/integrations/github/reviews/rev-9/issues"


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_forwards_and_injects_token(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(
        result=_FakeResp(
            200,
            {
                "job_id": "rev-9",
                "created": [
                    {
                        "proposal_id": "p0",
                        "issue_number": 3,
                        "issue_url": "https://github.com/acme/widget/issues/3",
                        "title": "[high] bug",
                    }
                ],
                "proposals": [{"id": "p0", "issue_url": "https://github.com/acme/widget/issues/3"}],
            },
        )
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "acme", "repo": "widget"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"][0]["issue_number"] == 3
    # The coding-team endpoint is targeted with the job id; the token is injected
    # server-side (never sent by the browser), and the caller's owner/repo are
    # forwarded so the review's repository can be validated downstream.
    assert fake.calls[0][0] == "http://coding:8103/reviews/rev-9/issues"
    assert fake.calls[0][1]["proposal_ids"] == ["p0"]
    assert fake.calls[0][1]["github_token"] == "ghp_token"
    assert fake.calls[0][1]["owner"] == "acme"
    assert fake.calls[0][1]["repo"] == "widget"


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_preserves_upstream_409(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(409, {"detail": "review rev-9 belongs to acme/other, not acme/widget"}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "acme", "repo": "widget"})
    assert resp.status_code == 409
    assert "belongs to" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_400_on_blank_owner(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    # A blank owner/repo can never resolve a repository; refuse before any call.
    resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "", "repo": "widget"})
    assert resp.status_code == 400


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_400_on_malformed_repo(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    # A repo with a path separator is rejected by the owner/repo validator.
    resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "acme", "repo": "a/b"})
    assert resp.status_code == 400


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_preserves_upstream_404(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(404, {"detail": "no review found for job rev-9"}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "acme", "repo": "widget"})
    assert resp.status_code == 404
    assert "no review found" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_masks_upstream_5xx(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(500, text="stacktrace"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "acme", "repo": "widget"})
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to create issues."


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_503_when_service_url_unset(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.delenv("CODING_TEAM_SERVICE_URL", raising=False)
    resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "acme", "repo": "widget"})
    assert resp.status_code == 503


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_504_on_timeout(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ReadTimeout("timed out"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "acme", "repo": "widget"})
    assert resp.status_code == 504


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_502_on_connect_error(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ConnectError("connection refused"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "acme", "repo": "widget"})
    assert resp.status_code == 502


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_502_on_malformed_body(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(200, json_data={"unexpected": True}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "acme", "repo": "widget"})
    assert resp.status_code == 502


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_create_review_issues_404_when_pat_cannot_reach_repo(mock_cfg, mock_cred, monkeypatch):
    """This route mutates Khala's own store (a review's persisted proposals) rather than
    being implicitly gated by a GitHub call, so — mirroring GET /github/reviews — it must
    verify the PAT can reach owner/repo before ever contacting the coding-team service.
    A repo the PAT can't reach (GitHub 404 on the access probe) yields 404 and never
    forwards the request, so a caller cannot use a job_id to file issues into (or read
    proposal detail for) a repository outside the PAT's own access boundary."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(200, json_data={}), repo_access_status=404)
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW_ISSUES, json={"proposal_ids": ["p0"], "owner": "secret", "repo": "repo"})
    assert resp.status_code == 404
    assert fake.repo_checks  # the PAT access probe ran
    assert fake.calls == []  # the coding-team forward was skipped


# ---------------------------------------------------------------------------
# GET /github/reviews/{job_id}/transcript
# ---------------------------------------------------------------------------
#
# ``_FakeAsyncClient.get`` above is single-purpose (the ``_assert_pat_can_reach_repo``
# probe only), so this route's own GET forward to the coding-team service is
# tested by patching ``_forward_to_coding_team``/``_assert_pat_can_reach_repo``
# directly rather than at the httpx layer.

from unittest.mock import AsyncMock  # noqa: E402

_REVIEW_TRANSCRIPT = "/api/integrations/github/reviews/rev-9/transcript"


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_get_review_transcript_forwards_with_owner_repo(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    entries = [
        {
            "stage": "chunk_review",
            "target": "a.py",
            "model": "m",
            "prompt": "p1",
            "response": "r1",
            "started_at": "2024-01-01T00:00:00+00:00",
            "duration_ms": 10,
        }
    ]
    forward = AsyncMock(return_value={"job_id": "rev-9", "entries": entries})
    with (
        patch(f"{_M}._assert_pat_can_reach_repo", new=AsyncMock(return_value=None)) as gate,
        patch(f"{_M}._forward_to_coding_team", new=forward),
    ):
        resp = client.get(_REVIEW_TRANSCRIPT, params={"owner": "acme", "repo": "widget"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "rev-9"
    assert body["entries"][0]["stage"] == "chunk_review"
    gate.assert_awaited_once()
    forward.assert_awaited_once()
    _url, path = forward.await_args.args[:2]
    assert path == "reviews/rev-9/transcript"
    assert forward.await_args.kwargs["params"] == {"owner": "acme", "repo": "widget"}


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_get_review_transcript_404_when_pat_cannot_reach_repo(mock_cfg, mock_cred, monkeypatch):
    """Mirrors GET /github/reviews: a repo the PAT can't reach must refuse before
    ever contacting the coding-team service, so a job_id cannot be used to read
    transcript content for a repository outside the PAT's access boundary."""
    from fastapi import HTTPException

    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    forward = AsyncMock()
    with (
        patch(
            f"{_M}._assert_pat_can_reach_repo",
            new=AsyncMock(side_effect=HTTPException(status_code=404, detail="not found")),
        ),
        patch(f"{_M}._forward_to_coding_team", new=forward),
    ):
        resp = client.get(_REVIEW_TRANSCRIPT, params={"owner": "secret", "repo": "repo"})
    assert resp.status_code == 404
    forward.assert_not_awaited()


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_get_review_transcript_503_when_service_url_unset(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.delenv("CODING_TEAM_SERVICE_URL", raising=False)
    with patch(f"{_M}._assert_pat_can_reach_repo", new=AsyncMock(return_value=None)) as gate:
        resp = client.get(_REVIEW_TRANSCRIPT, params={"owner": "acme", "repo": "widget"})
    assert resp.status_code == 503
    # The route checks CODING_TEAM_SERVICE_URL (_require_coding_team_url) before
    # the PAT-reachability gate, same order as GET /github/reviews, so a missing
    # service URL must fail before the gate is ever reached.
    gate.assert_not_awaited()


def test_get_review_transcript_422_without_owner_repo():
    # owner/repo are required query params — a caller must supply them, not
    # rely on the configured default (the transcript is fetched by job_id, and
    # the PAT-reachability gate needs a concrete repo to check against).
    resp = client.get("/api/integrations/github/reviews/rev-9/transcript")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# _start_pr_review(token=...): webhook path reuses a pre-resolved PAT (no 2nd read)
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

from unified_api.routes.integrations import _start_pr_review  # noqa: E402


def test_start_pr_review_with_token_skips_credential_read(monkeypatch):
    """When a token is passed, the PAT is NOT re-read via resolve_credential_with_env_fallback;
    only the JSON-only settings are validated, and the passed token is forwarded."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(
        result=_FakeResp(
            200,
            {"job_id": "rev-9", "pr_number": 7, "pr_url": "u", "status": "pending", "message": ""},
        )
    )
    with (
        patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG)),
        patch(f"{_M}.resolve_credential_with_env_fallback", side_effect=AssertionError("must not read PAT")) as cred,
        patch(f"{_M}.httpx.AsyncClient", return_value=fake),
    ):
        result = asyncio.run(_start_pr_review(7, None, token="ghp_pre"))
    assert result.job_id == "rev-9"
    assert fake.calls[0][1]["github_token"] == "ghp_pre"
    cred.assert_not_called()


def test_start_pr_review_with_token_400_when_disabled():
    from fastapi import HTTPException as _HTTPExc

    with (
        patch(f"{_M}.get_github_config_meta", return_value={**_GH_CFG, "enabled": False}),
        pytest.raises(_HTTPExc) as exc,
    ):
        asyncio.run(_start_pr_review(7, None, token="ghp_pre"))
    assert exc.value.status_code == 400
    assert "not enabled" in exc.value.detail


def test_start_pr_review_with_token_400_when_owner_repo_missing():
    from fastapi import HTTPException as _HTTPExc

    with (
        patch(f"{_M}.get_github_config_meta", return_value={**_GH_CFG, "owner": "", "repo": ""}),
        pytest.raises(_HTTPExc) as exc,
    ):
        asyncio.run(_start_pr_review(7, None, token="ghp_pre"))
    assert exc.value.status_code == 400
    assert "owner/repo" in exc.value.detail


def test_start_pr_review_targets_caller_supplied_repo(monkeypatch):
    """A caller-supplied owner/repo (the UI's picked repo or the webhook's commented
    repo) is the review target — the configured default is ignored, because the PAT's
    own authorization configuration is the access list, not Khala settings."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(
        result=_FakeResp(
            200,
            {"job_id": "rev-9", "pr_number": 7, "pr_url": "u", "status": "pending", "message": ""},
        )
    )
    with (
        # Config points at a different default repo; the explicit target must win.
        patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG)),
        patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_pre", True)),
        patch(f"{_M}.httpx.AsyncClient", return_value=fake),
    ):
        result = asyncio.run(_start_pr_review(7, None, token="ghp_pre", owner="other", repo="thing"))
    assert result.job_id == "rev-9"
    payload = fake.last_payload()
    assert payload["owner"] == "other"
    assert payload["repo"] == "thing"


def test_start_pr_review_falls_back_to_configured_default_repo(monkeypatch):
    """With no caller-supplied target, the legacy configured default owner/repo is used."""
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(
        result=_FakeResp(
            200,
            {"job_id": "rev-9", "pr_number": 7, "pr_url": "u", "status": "pending", "message": ""},
        )
    )
    with (
        patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG)),
        patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_pre", True)),
        patch(f"{_M}.httpx.AsyncClient", return_value=fake),
    ):
        result = asyncio.run(_start_pr_review(7, None, token="ghp_pre"))
    assert result.job_id == "rev-9"
    payload = fake.last_payload()
    assert payload["owner"] == "acme"
    assert payload["repo"] == "widget"

"""Tests for the PAT-scoped repository listing: GET /api/integrations/github/repos.

Repository access is defined by the personal access token itself — the route mirrors
GitHub's ``GET /user/repos`` for the stored token, so whatever repositories the token's
authorization configuration grants are exactly what the pickers see. No owner/repo is
configured in Khala. These tests pin that behaviour: the full accessible set is
returned across pages, malformed items are dropped, and the shared error mapping and
prerequisite checks (enabled + PAT, but NOT owner/repo) apply.
"""

import sys
from pathlib import Path
from unittest.mock import patch

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi.testclient import TestClient  # noqa: E402

from unified_api.main import app  # noqa: E402
from unified_api.routes import integrations  # noqa: E402

client = TestClient(app, follow_redirects=False)

_M = "unified_api.routes.integrations"
_REPOS = "/api/integrations/github/repos"

# No owner/repo configured on purpose: the repos listing must not require one.
_GH_CFG = {"enabled": True, "owner": "", "repo": "", "default_label": "", "repo_path": ""}


class _FakeResp:
    """Minimal stand-in for an httpx.Response from GET /user/repos."""

    def __init__(self, status_code=200, json_data=None, next_url=None, json_raises=False):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        self._json_raises = json_raises
        self.links = {"next": {"url": next_url, "rel": "next"}} if next_url else {}

    def json(self):
        if self._json_raises:
            # Mirrors httpx.Response.json() on a non-JSON 200 body (json.JSONDecodeError
            # is a ValueError subclass).
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json


class _FakeClient:
    """Async-context-manager client returning queued responses, recording calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # list of (url, params)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, headers=None, params=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


def _repo(owner, name, **overrides):
    raw = {
        "name": name,
        "full_name": f"{owner}/{name}",
        "owner": {"login": owner},
        "private": False,
        "archived": False,
        "html_url": f"https://github.com/{owner}/{name}",
        "description": f"{name} repo",
        "default_branch": "main",
        "open_issues_count": 3,
        "pushed_at": "2026-07-01T00:00:00Z",
    }
    raw.update(overrides)
    return raw


# ---------------------------------------------------------------------------
# Configuration / precondition failures
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_github_config_meta")
def test_repos_returns_400_when_integration_disabled(mock_cfg):
    mock_cfg.return_value = {**_GH_CFG, "enabled": False}
    resp = client.get(_REPOS)
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_returns_400_when_pat_missing(mock_cfg, mock_cred):
    resp = client.get(_REPOS)
    assert resp.status_code == 400
    assert "PAT" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("", False))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_returns_503_when_credential_store_unreachable(mock_cfg, mock_cred):
    resp = client.get(_REPOS)
    assert resp.status_code == 503
    assert "credential store" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_does_not_require_configured_owner_repo(mock_cfg, mock_cred):
    """The listing works with NO configured owner/repo — access comes from the PAT."""
    fake = _FakeClient([_FakeResp(200, [_repo("acme", "widget")])])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REPOS)
    assert resp.status_code == 200
    assert [r["full_name"] for r in resp.json()] == ["acme/widget"]


# ---------------------------------------------------------------------------
# GitHub HTTP error mapping (shared _collect_github_pages contract)
# ---------------------------------------------------------------------------


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_returns_401_on_bad_token(mock_cfg, mock_cred):
    fake = _FakeClient([_FakeResp(401)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REPOS)
    assert resp.status_code == 401
    assert "invalid or expired" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_returns_502_on_other_github_error(mock_cfg, mock_cred):
    fake = _FakeClient([_FakeResp(500)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REPOS)
    assert resp.status_code == 502
    assert "GitHub API returned 500" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_returns_502_on_non_json_200(mock_cfg, mock_cred):
    """A 200 whose body isn't JSON (e.g. an HTML error page from a proxy) maps to a 502,
    not an unhandled 500 from resp.json() raising."""
    fake = _FakeClient([_FakeResp(200, json_raises=True)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REPOS)
    assert resp.status_code == 502
    assert "non-JSON" in resp.json()["detail"]


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_returns_502_on_non_list_200(mock_cfg, mock_cred):
    """A 200 whose JSON body is an object rather than an array is malformed for a list
    endpoint and maps to a 502 instead of iterating the dict's keys."""
    fake = _FakeClient([_FakeResp(200, {"message": "unexpected"})])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REPOS)
    assert resp.status_code == 502
    assert "unexpected response shape" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_maps_fields_and_requests_pushed_order(mock_cfg, mock_cred):
    page = [
        _repo("acme", "widget", private=True, open_issues_count=7),
        _repo("other", "thing", archived=True),
    ]
    fake = _FakeClient([_FakeResp(200, page)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REPOS)
    assert resp.status_code == 200
    body = resp.json()
    assert [r["full_name"] for r in body] == ["acme/widget", "other/thing"]
    first = body[0]
    assert first["owner"] == "acme"
    assert first["name"] == "widget"
    assert first["private"] is True
    assert first["open_issues_count"] == 7
    assert first["default_branch"] == "main"
    assert body[1]["archived"] is True
    # One request to /user/repos with the max page size and pushed ordering.
    assert len(fake.calls) == 1
    url, params = fake.calls[0]
    assert url.endswith("/user/repos")
    assert params["per_page"] == integrations._GITHUB_REPOS_PER_PAGE
    assert params["sort"] == "pushed"


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_follows_link_pagination(mock_cfg, mock_cred):
    page1 = [_repo("acme", "widget")]
    page2 = [_repo("acme", "gadget")]
    fake = _FakeClient(
        [
            _FakeResp(200, page1, next_url="https://api.github.com/user/repos?page=2"),
            _FakeResp(200, page2),
        ]
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REPOS)
    assert resp.status_code == 200
    assert [r["full_name"] for r in resp.json()] == ["acme/widget", "acme/gadget"]
    assert len(fake.calls) == 2
    # The second request follows the Link URL verbatim (no params re-sent).
    assert fake.calls[1] == ("https://api.github.com/user/repos?page=2", None)


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_drops_malformed_items_and_defaults_fields(mock_cfg, mock_cred):
    page = [
        "not-a-dict",
        {"full_name": "x/y"},  # no name → dropped
        {"name": "bare"},  # minimal: everything else defaults
        _repo("acme", "widget", open_issues_count=True),  # bool masquerading as int → 0
    ]
    fake = _FakeClient([_FakeResp(200, page)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REPOS)
    assert resp.status_code == 200
    body = resp.json()
    assert [r["name"] for r in body] == ["bare", "widget"]
    bare = body[0]
    assert bare["owner"] == ""
    assert bare["full_name"] == "/bare"
    assert bare["open_issues_count"] == 0
    assert body[1]["open_issues_count"] == 0  # bool is not a count


@patch(f"{_M}.resolve_credential_with_env_fallback", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_repos_page_cap_returns_partial_and_warns(mock_cfg, mock_cred, caplog):
    """Hitting the page cap returns what was gathered instead of failing."""
    pages = [
        _FakeResp(200, [_repo("acme", f"repo-{i}")], next_url=f"https://api.github.com/user/repos?page={i + 2}")
        for i in range(integrations._GITHUB_MAX_REPO_PAGES)
    ]
    fake = _FakeClient(pages)
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake), caplog.at_level("WARNING"):
        resp = client.get(_REPOS)
    assert resp.status_code == 200
    assert len(resp.json()) == integrations._GITHUB_MAX_REPO_PAGES
    assert len(fake.calls) == integrations._GITHUB_MAX_REPO_PAGES
    assert any("page cap" in r.message for r in caplog.records)

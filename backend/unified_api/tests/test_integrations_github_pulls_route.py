"""Tests for the code-review panel routes:

- GET /api/integrations/github/pulls  (list open pull requests)
- POST /api/integrations/github/review-pr  (proxy to the coding-team review flow)

No real network: the GitHub list uses a fake async client, and the review-pr
proxy patches httpx.AsyncClient. The review-pr endpoint must NOT clone the repo.
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

client = TestClient(app, follow_redirects=False)

_M = "unified_api.routes.integrations"
_PULLS = "/api/integrations/github/pulls"
_REVIEW = "/api/integrations/github/review-pr"

_GH_CFG = {"enabled": True, "owner": "acme", "repo": "widget", "default_label": "", "repo_path": ""}


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakePullsResp:
    def __init__(self, status_code=200, json_data=None, next_url=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        self.links = {"next": {"url": next_url, "rel": "next"}} if next_url else {}

    def json(self):
        return self._json


class _FakePullsClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, headers=None, params=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


class _FakeResp:
    def __init__(self, status_code, json_data=None, text="", json_raises=False):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
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


def _pull(number, **overrides):
    payload = {
        "number": number,
        "title": f"PR {number}",
        "body": "body text",
        "user": {"login": "octocat"},
        "html_url": f"https://github.com/acme/widget/pull/{number}",
        "head": {"ref": f"feature-{number}"},
        "base": {"ref": "main"},
        "draft": False,
        "labels": [{"name": "needs-review"}],
        "updated_at": "2026-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# GET /github/pulls — precondition failures
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_github_config")
def test_pulls_400_when_disabled(mock_cfg):
    mock_cfg.return_value = {**_GH_CFG, "enabled": False}
    assert client.get(_PULLS).status_code == 400


@patch(f"{_M}.get_credential", return_value="")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_pulls_400_when_pat_missing(mock_cfg, mock_cred):
    resp = client.get(_PULLS)
    assert resp.status_code == 400
    assert "PAT" in resp.json()["detail"]


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value={**_GH_CFG, "owner": "", "repo": ""})
def test_pulls_400_when_owner_repo_missing(mock_cfg, mock_cred):
    resp = client.get(_PULLS)
    assert resp.status_code == 400
    assert "owner/repo" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /github/pulls — HTTP error mapping
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_pulls_401(mock_cfg, mock_cred):
    fake = _FakePullsClient([_FakePullsResp(401)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.get(_PULLS).status_code == 401


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_pulls_404(mock_cfg, mock_cred):
    fake = _FakePullsClient([_FakePullsResp(404)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.get(_PULLS).status_code == 404


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_pulls_502(mock_cfg, mock_cred):
    fake = _FakePullsClient([_FakePullsResp(500)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.get(_PULLS).status_code == 502


# ---------------------------------------------------------------------------
# GET /github/pulls — success + pagination + field mapping
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_pulls_field_mapping(mock_cfg, mock_cred):
    fake = _FakePullsClient([_FakePullsResp(200, [_pull(7, draft=True)])])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_PULLS)
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["number"] == 7
    assert item["author"] == "octocat"
    assert item["head"] == "feature-7"
    assert item["base"] == "main"
    assert item["draft"] is True
    assert item["labels"] == ["needs-review"]


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_pulls_pagination(mock_cfg, mock_cred):
    fake = _FakePullsClient(
        [
            _FakePullsResp(200, [_pull(1)], next_url="https://api.github.com/repos/acme/widget/pulls?page=2"),
            _FakePullsResp(200, [_pull(2)]),
        ]
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_PULLS)
    assert [p["number"] for p in resp.json()] == [1, 2]
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# POST /github/review-pr — validation
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_github_config", return_value={**_GH_CFG, "enabled": False})
def test_review_400_when_disabled(mock_cfg):
    assert client.post(_REVIEW, json={"pr_number": 7}).status_code == 400


@patch(f"{_M}.get_credential", return_value="")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_review_400_when_pat_missing(mock_cfg, mock_cred):
    assert client.post(_REVIEW, json={"pr_number": 7}).status_code == 400


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_review_503_when_service_url_unset(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.delenv("CODING_TEAM_SERVICE_URL", raising=False)
    assert client.post(_REVIEW, json={"pr_number": 7}).status_code == 503


# ---------------------------------------------------------------------------
# POST /github/review-pr — proxy behaviour (no clone)
# ---------------------------------------------------------------------------


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/acme_widget")
@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_review_success_does_not_clone(mock_cfg, mock_cred, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103/")
    ok = _FakeResp(
        200,
        {"job_id": "j1", "pr_number": 7, "pr_url": "https://github.com/acme/widget/pull/7", "status": "pending", "message": "started"},
    )
    fake = _FakeAsyncClient(result=ok)
    with patch(f"{_M}._ensure_repo_clone") as mock_clone, patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW, json={"pr_number": 7})
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "j1"
    # The review flow is API-only: the repo must never be cloned.
    mock_clone.assert_not_called()
    # Proxied to the coding team's /review-pr with the resolved repo_path + token.
    url, body = fake.calls[0]
    assert url == "http://coding:8103/review-pr"
    assert body["pr_number"] == 7
    assert body["github_token"] == "ghp"
    assert body["repo_path"] == "/tmp/acme_widget"


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_review_504_on_timeout(mock_cfg, mock_cred, mock_path, monkeypatch):
    import httpx

    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ReadTimeout("timed out"))
    with patch(f"{_M}._ensure_repo_clone"), patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.post(_REVIEW, json={"pr_number": 7}).status_code == 504


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_review_502_on_unreachable(mock_cfg, mock_cred, mock_path, monkeypatch):
    import httpx

    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(exc=httpx.ConnectError("refused"))
    with patch(f"{_M}._ensure_repo_clone"), patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.post(_REVIEW, json={"pr_number": 7}).status_code == 502


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_review_propagates_upstream_error(mock_cfg, mock_cred, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(502, {"detail": "github api error"}))
    with patch(f"{_M}._ensure_repo_clone"), patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.post(_REVIEW, json={"pr_number": 7})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "github api error"


@patch(f"{_M}._resolve_repo_path", return_value="/tmp/x")
@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_review_502_on_malformed_success_body(mock_cfg, mock_cred, mock_path, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeAsyncClient(result=_FakeResp(200, {"unexpected": "shape"}))
    with patch(f"{_M}._ensure_repo_clone"), patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.post(_REVIEW, json={"pr_number": 7}).status_code == 502


# ---------------------------------------------------------------------------
# GET /github/reviews — proxy to the coding-team review history
# ---------------------------------------------------------------------------

_REVIEWS = "/api/integrations/github/reviews"


class _FakeReviewsClient:
    """Async-client double whose only verb is GET (the reviews proxy reads)."""

    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, params))
        if self._exc is not None:
            raise self._exc
        return self._result


@patch(f"{_M}.get_github_config", return_value={**_GH_CFG, "enabled": False})
def test_reviews_400_when_disabled(mock_cfg):
    assert client.get(_REVIEWS).status_code == 400


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_reviews_503_when_service_url_unset(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.delenv("CODING_TEAM_SERVICE_URL", raising=False)
    assert client.get(_REVIEWS).status_code == 503


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_reviews_success_injects_owner_repo(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103/")
    rows = [
        {
            "job_id": "j1",
            "pr_number": 7,
            "pr_url": "u",
            "status": "completed",
            "status_text": "done",
            "review_summary": {"total_issues": 1, "inline_comments": 1, "comment_findings": 0, "event": "COMMENT"},
            "error": None,
            "created_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:01:00Z",
        }
    ]
    fake = _FakeReviewsClient(result=_FakeResp(200, json_data=rows))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REVIEWS, params={"pr_number": 7})
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["job_id"] == "j1"
    assert data[0]["review_summary"]["event"] == "COMMENT"
    # owner/repo are injected from config; pr_number + default limit are forwarded.
    url, params = fake.calls[0]
    assert url == "http://coding:8103/reviews"
    assert params["owner"] == "acme"
    assert params["repo"] == "widget"
    assert params["pr_number"] == 7
    assert params["limit"] == 500


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_reviews_omits_pr_number_when_absent(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeReviewsClient(result=_FakeResp(200, json_data=[]))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REVIEWS)
    assert resp.status_code == 200
    _url, params = fake.calls[0]
    assert "pr_number" not in params


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_reviews_forwards_limit(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeReviewsClient(result=_FakeResp(200, json_data=[]))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REVIEWS, params={"limit": 10})
    assert resp.status_code == 200
    _url, params = fake.calls[0]
    assert params["limit"] == 10


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_reviews_rejects_out_of_range_limit(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    # Validated by FastAPI before any upstream call.
    assert client.get(_REVIEWS, params={"limit": 0}).status_code == 422
    assert client.get(_REVIEWS, params={"limit": 5000}).status_code == 422


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_reviews_504_on_timeout(mock_cfg, mock_cred, monkeypatch):
    import httpx

    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeReviewsClient(exc=httpx.ReadTimeout("slow"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.get(_REVIEWS).status_code == 504


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_reviews_502_on_connect_error(mock_cfg, mock_cred, monkeypatch):
    import httpx

    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeReviewsClient(exc=httpx.ConnectError("nope"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.get(_REVIEWS).status_code == 502


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_reviews_propagates_upstream_error(mock_cfg, mock_cred, monkeypatch):
    # Upstream detail is sanitized: the status code is preserved but the client
    # gets a generic message (the real detail is only logged server-side).
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeReviewsClient(result=_FakeResp(500, json_data={"detail": "boom"}))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REVIEWS)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to retrieve review history."


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_reviews_propagates_upstream_error_with_plain_text_body(mock_cfg, mock_cred, monkeypatch):
    # A non-JSON (plain text) error body is also sanitized to the generic message.
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeReviewsClient(result=_FakeResp(500, json_raises=True, text="internal boom"))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_REVIEWS)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to retrieve review history."


@patch(f"{_M}.get_credential", return_value="ghp")
@patch(f"{_M}.get_github_config", return_value=dict(_GH_CFG))
def test_reviews_502_on_malformed_success_body(mock_cfg, mock_cred, monkeypatch):
    monkeypatch.setenv("CODING_TEAM_SERVICE_URL", "http://coding:8103")
    fake = _FakeReviewsClient(result=_FakeResp(200, json_data=None, json_raises=True))
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        assert client.get(_REVIEWS).status_code == 502

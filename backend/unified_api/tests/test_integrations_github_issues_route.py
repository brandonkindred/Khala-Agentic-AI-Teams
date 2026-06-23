"""Tests for the GitHub issue-listing route: GET /api/integrations/github/issues.

The coding-team page loads the repository's open issues into a picker panel. The
route previously requested a single page of ``per_page=30`` and never followed
GitHub's ``Link``-header pagination, so a repo with more than 30 open issues had
its list silently truncated. These tests pin the corrected behaviour: every open
issue is returned across all result pages, pull requests are excluded, and a
safety page-cap is enforced.
"""

import logging
import re
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
_ISSUES = "/api/integrations/github/issues"

_GH_CFG = {"enabled": True, "owner": "acme", "repo": "widget", "default_label": ""}


# ---------------------------------------------------------------------------
# Test doubles for the async httpx client used to reach the GitHub REST API
# ---------------------------------------------------------------------------


class _FakeIssuesResp:
    """Minimal stand-in for an httpx.Response from GET /repos/.../issues."""

    def __init__(self, status_code=200, json_data=None, next_url=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        # Mirror httpx.Response.links: {"next": {"url": ..., "rel": "next"}}.
        self.links = {"next": {"url": next_url, "rel": "next"}} if next_url else {}

    def json(self):
        return self._json


class _FakeDepResp:
    """Stand-in for an httpx.Response from GET /issues/{n}/dependencies/blocked_by.

    ``next_url`` populates the ``links`` mapping (mirroring httpx.Response.links) so the
    dependency fetch's ``Link``-header pagination can be exercised.
    """

    def __init__(self, status_code=200, json_data=None, raise_exc=None, next_url=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        self._raise_exc = raise_exc
        self.links = {"next": {"url": next_url, "rel": "next"}} if next_url else {}

    def json(self):
        return self._json


_BLOCKED_BY_RE = re.compile(r"/issues/(\d+)/dependencies/blocked_by")


class _FakeIssuesClient:
    """Async-context-manager client returning queued responses, recording calls.

    Issues-list pages are served FIFO from ``responses``; ``blocked_by`` dependency
    requests are routed by issue number to ``dependency_responses`` (default: an empty
    200), so dependency enrichment never consumes an issues-list response. A mapping
    value may be a single ``_FakeDepResp`` or a list of them (one per dependency page),
    served in order across successive blocked_by requests for that issue.
    """

    def __init__(self, responses, dependency_responses=None):
        self._responses = list(responses)
        # Normalize each dependency entry to a FIFO list of pages.
        self._dependency_pages = {
            number: list(value) if isinstance(value, list) else [value]
            for number, value in (dependency_responses or {}).items()
        }
        self.calls = []  # list of (url, params) for issues-list requests
        self.dep_calls = []  # list of issue numbers whose dependencies were fetched
        self.dep_params = []  # list of params dicts passed to each blocked_by request

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, headers=None, params=None):
        match = _BLOCKED_BY_RE.search(url)
        if match:
            number = int(match.group(1))
            self.dep_calls.append(number)
            self.dep_params.append(params)
            pages = self._dependency_pages.get(number)
            resp = pages.pop(0) if pages else _FakeDepResp(200, [])
            if resp._raise_exc is not None:
                raise resp._raise_exc
            return resp
        self.calls.append((url, params))
        return self._responses.pop(0)


def _dep(number, *, state="open", title=None):
    """A dependency object as GitHub returns it on the blocked_by endpoint."""
    return {
        "number": number,
        "state": state,
        "title": title if title is not None else f"Dependency {number}",
        "html_url": f"https://github.com/acme/widget/issues/{number}",
    }


def _issue(number, *, title=None, body="body text", labels=None, html_url=None):
    return {
        "number": number,
        "title": title if title is not None else f"Issue {number}",
        "body": body,
        "labels": [{"name": name} for name in (labels or [])],
        "html_url": html_url or f"https://github.com/acme/widget/issues/{number}",
    }


def _pr(number):
    """An item as GitHub returns it for a pull request on the issues endpoint."""
    item = _issue(number, title=f"PR {number}")
    item["pull_request"] = {"url": f"https://api.github.com/repos/acme/widget/pulls/{number}"}
    return item


# ---------------------------------------------------------------------------
# Configuration / precondition failures
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_github_config_meta")
def test_issues_returns_400_when_integration_disabled(mock_cfg):
    mock_cfg.return_value = {**_GH_CFG, "enabled": False}
    resp = client.get(_ISSUES)
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@patch(f"{_M}.get_credential_status", return_value=("", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issues_returns_400_when_pat_missing(mock_cfg, mock_cred):
    # Store reachable, token genuinely absent → 400 (not 503).
    resp = client.get(_ISSUES)
    assert resp.status_code == 400
    assert "PAT" in resp.json()["detail"]


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value={**_GH_CFG, "owner": "", "repo": ""})
def test_issues_returns_400_when_owner_repo_missing(mock_cfg, mock_cred):
    resp = client.get(_ISSUES)
    assert resp.status_code == 400
    assert "owner/repo" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GitHub HTTP error mapping
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issues_returns_401_on_bad_token(mock_cfg, mock_cred):
    fake = _FakeIssuesClient([_FakeIssuesResp(401)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 401
    assert "invalid or expired" in resp.json()["detail"]


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issues_returns_404_on_missing_repo(mock_cfg, mock_cred):
    fake = _FakeIssuesClient([_FakeIssuesResp(404)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issues_returns_502_on_other_github_error(mock_cfg, mock_cred):
    fake = _FakeIssuesClient([_FakeIssuesResp(500)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 502
    assert "GitHub API returned 500" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Success paths: single page, pull-request filtering, label passthrough
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issues_single_page_returns_all_and_excludes_prs(mock_cfg, mock_cred):
    page = [_issue(1, labels=["bug", "ready"]), _pr(2), _issue(3)]
    fake = _FakeIssuesClient([_FakeIssuesResp(200, page)])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    body = resp.json()
    assert [item["number"] for item in body] == [1, 3]  # PR #2 excluded
    assert body[0]["labels"] == ["bug", "ready"]
    # Only one page was needed (no Link header), so only one request was made.
    assert len(fake.calls) == 1
    # The first request asks GitHub for the max page size and open state.
    first_params = fake.calls[0][1]
    assert first_params["state"] == "open"
    assert first_params["per_page"] == integrations._GITHUB_ISSUES_PER_PAGE


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issues_body_preview_truncated_to_200_chars(mock_cfg, mock_cred):
    long_body = "x" * 500
    fake = _FakeIssuesClient([_FakeIssuesResp(200, [_issue(1, body=long_body)])])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    assert len(resp.json()[0]["body_preview"]) == 200


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issues_handles_null_body_and_label_objects(mock_cfg, mock_cred):
    raw = {"number": 9, "title": None, "body": None, "labels": [{"no_name": "x"}, {"name": "ok"}], "html_url": None}
    fake = _FakeIssuesClient([_FakeIssuesResp(200, [raw])])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["title"] == ""
    assert item["body_preview"] == ""
    assert item["html_url"] == ""
    assert item["labels"] == ["ok"]  # malformed label without a name is dropped


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value={**_GH_CFG, "default_label": "ready"})
def test_issues_uses_default_label_when_no_query(mock_cfg, mock_cred):
    fake = _FakeIssuesClient([_FakeIssuesResp(200, [_issue(1)])])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    assert fake.calls[0][1]["labels"] == "ready"


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value={**_GH_CFG, "default_label": "ready"})
def test_issues_query_label_overrides_default(mock_cfg, mock_cred):
    fake = _FakeIssuesClient([_FakeIssuesResp(200, [_issue(1)])])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES, params={"label": "blocked"})
    assert resp.status_code == 200
    assert fake.calls[0][1]["labels"] == "blocked"


# ---------------------------------------------------------------------------
# Pagination: the core fix — every page is fetched, not just the first
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issues_follows_link_header_across_pages(mock_cfg, mock_cred):
    page2_url = "https://api.github.com/repositories/1/issues?page=2&per_page=100&state=open"
    responses = [
        _FakeIssuesResp(200, [_issue(1), _issue(2)], next_url=page2_url),
        _FakeIssuesResp(200, [_issue(3), _pr(4), _issue(5)], next_url=None),
    ]
    fake = _FakeIssuesClient(responses)
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    # All four issues from both pages are present; the PR (#4) is excluded.
    assert [item["number"] for item in resp.json()] == [1, 2, 3, 5]
    # Two requests were made: the base URL, then the verbatim "next" URL.
    assert len(fake.calls) == 2
    assert fake.calls[1][0] == page2_url
    # The first page carries query params; the next-page URL must not re-append them.
    assert fake.calls[0][1] is not None
    assert fake.calls[1][1] is None


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issues_stops_at_page_cap_and_warns(mock_cfg, mock_cred, monkeypatch, caplog):
    # Force a tiny cap and hand back pages that always advertise a "next" link, so
    # only the cap can stop the loop.
    monkeypatch.setattr(integrations, "_GITHUB_MAX_ISSUE_PAGES", 2)
    responses = [
        _FakeIssuesResp(200, [_issue(1)], next_url="https://api.github.com/x?page=2"),
        _FakeIssuesResp(200, [_issue(2)], next_url="https://api.github.com/x?page=3"),
    ]
    fake = _FakeIssuesClient(responses)
    with (
        caplog.at_level(logging.WARNING, logger="unified_api.routes.integrations"),
        patch(f"{_M}.httpx.AsyncClient", return_value=fake),
    ):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    assert [item["number"] for item in resp.json()] == [1, 2]
    # Exactly two pages were fetched — the loop honoured the cap rather than chasing
    # the still-present "next" link into page 3.
    assert len(fake.calls) == 2
    assert any("page cap" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Dependency enrichment: blocked_by relationships drive the picker indicator
# ---------------------------------------------------------------------------


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issue_with_open_dependency_is_blocked(mock_cfg, mock_cred):
    fake = _FakeIssuesClient(
        [_FakeIssuesResp(200, [_issue(1)])],
        dependency_responses={1: _FakeDepResp(200, [_dep(3, state="open"), _dep(5, state="closed")])},
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["blocked"] is True
    assert item["open_dependencies"] == [3]
    assert {d["number"]: d["state"] for d in item["dependencies"]} == {3: "open", 5: "closed"}
    assert fake.dep_calls == [1]


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issue_with_all_closed_dependencies_not_blocked(mock_cfg, mock_cred):
    fake = _FakeIssuesClient(
        [_FakeIssuesResp(200, [_issue(1)])],
        dependency_responses={1: _FakeDepResp(200, [_dep(3, state="closed"), _dep(5, state="closed")])},
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["blocked"] is False
    assert item["open_dependencies"] == []
    assert [d["number"] for d in item["dependencies"]] == [3, 5]


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_issue_with_no_dependencies(mock_cfg, mock_cred):
    # No queued dependency response → the fake returns an empty 200.
    fake = _FakeIssuesClient([_FakeIssuesResp(200, [_issue(1)])])
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["blocked"] is False
    assert item["dependencies"] == []
    assert item["open_dependencies"] == []


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_dependency_endpoint_404_treated_as_no_deps(mock_cfg, mock_cred):
    # 404 = issue dependencies feature not enabled for the repo.
    fake = _FakeIssuesClient(
        [_FakeIssuesResp(200, [_issue(1)])],
        dependency_responses={1: _FakeDepResp(404)},
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["blocked"] is False
    assert item["dependencies"] == []


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_one_dependency_fetch_failure_does_not_fail_list(mock_cfg, mock_cred):
    # Issue #1's dependency lookup raises; issue #2's succeeds. The whole list must
    # still return 200 with #1 degraded to empty deps and #2 enriched.
    fake = _FakeIssuesClient(
        [_FakeIssuesResp(200, [_issue(1), _issue(2)])],
        dependency_responses={
            1: _FakeDepResp(raise_exc=RuntimeError("boom")),
            2: _FakeDepResp(200, [_dep(9, state="open")]),
        },
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    by_number = {item["number"]: item for item in resp.json()}
    assert by_number[1]["blocked"] is False
    assert by_number[1]["dependencies"] == []
    assert by_number[2]["blocked"] is True
    assert by_number[2]["open_dependencies"] == [9]


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_dependency_concurrency_knob_respected(mock_cfg, mock_cred, monkeypatch):
    # A small concurrency bound must not drop any dependency fetch.
    monkeypatch.setattr(integrations, "_GITHUB_DEPENDENCY_CONCURRENCY", 2)
    issues = [_issue(n) for n in (1, 2, 3, 4, 5)]
    fake = _FakeIssuesClient(
        [_FakeIssuesResp(200, issues)],
        dependency_responses={n: _FakeDepResp(200, [_dep(100 + n, state="open")]) for n in (1, 2, 3, 4, 5)},
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    assert sorted(fake.dep_calls) == [1, 2, 3, 4, 5]
    assert all(item["blocked"] for item in resp.json())
    # The first page of each dependency fetch asks GitHub for the max page size.
    assert all(p and p.get("per_page") == integrations._GITHUB_DEPENDENCY_PER_PAGE for p in fake.dep_params)


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_dependency_fetch_follows_link_header_across_pages(mock_cfg, mock_cred):
    # A blocked_by list spanning two pages: the open blocker is on page 2, so dropping
    # page 2 would wrongly mark the issue runnable. Both pages must be fetched.
    page2 = "https://api.github.com/repos/acme/widget/issues/1/dependencies/blocked_by?page=2"
    fake = _FakeIssuesClient(
        [_FakeIssuesResp(200, [_issue(1)])],
        dependency_responses={
            1: [
                _FakeDepResp(200, [_dep(3, state="closed")], next_url=page2),
                _FakeDepResp(200, [_dep(5, state="open")]),
            ]
        },
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["blocked"] is True
    assert item["open_dependencies"] == [5]
    assert {d["number"] for d in item["dependencies"]} == {3, 5}
    # Two blocked_by requests were made for issue #1; the second used the Link URL verbatim.
    assert fake.dep_calls == [1, 1]
    assert fake.dep_params == [{"per_page": integrations._GITHUB_DEPENDENCY_PER_PAGE}, None]


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_dependency_fetch_stops_at_page_cap(mock_cfg, mock_cred, monkeypatch):
    # Pages that always advertise a "next" link must be bounded by the page cap.
    monkeypatch.setattr(integrations, "_GITHUB_MAX_DEPENDENCY_PAGES", 2)
    nxt = "https://api.github.com/repos/acme/widget/issues/1/dependencies/blocked_by?page=n"
    fake = _FakeIssuesClient(
        [_FakeIssuesResp(200, [_issue(1)])],
        dependency_responses={
            1: [
                _FakeDepResp(200, [_dep(3, state="closed")], next_url=nxt),
                _FakeDepResp(200, [_dep(5, state="closed")], next_url=nxt),
                _FakeDepResp(200, [_dep(7, state="open")], next_url=nxt),
            ]
        },
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    # Only the first two pages were fetched; page 3 (the open blocker) was never reached.
    assert fake.dep_calls == [1, 1]
    item = resp.json()[0]
    assert {d["number"] for d in item["dependencies"]} == {3, 5}
    # An incomplete (cap-truncated) fetch is NOT forced to blocked: ``blocked`` stays
    # derived from the observed dependencies (both closed here) so the picker never shows
    # a block with an empty open-dependency list.
    assert item["blocked"] is False


@patch(f"{_M}.get_credential_status", return_value=("ghp_token", True))
@patch(f"{_M}.get_github_config_meta", return_value=dict(_GH_CFG))
def test_dependency_partial_pages_kept_on_mid_pagination_error(mock_cfg, mock_cred):
    # A transport error on page 2 keeps page 1's dependencies rather than discarding them.
    nxt = "https://api.github.com/repos/acme/widget/issues/1/dependencies/blocked_by?page=2"
    fake = _FakeIssuesClient(
        [_FakeIssuesResp(200, [_issue(1)])],
        dependency_responses={
            1: [
                _FakeDepResp(200, [_dep(3, state="open")], next_url=nxt),
                _FakeDepResp(raise_exc=RuntimeError("boom")),
            ]
        },
    )
    with patch(f"{_M}.httpx.AsyncClient", return_value=fake):
        resp = client.get(_ISSUES)
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["blocked"] is True
    assert item["open_dependencies"] == [3]


def test_parse_dependency_concurrency_falls_back_on_garbage():
    assert integrations._parse_dependency_concurrency("not-a-number") == 8
    assert integrations._parse_dependency_concurrency("0") == 8
    assert integrations._parse_dependency_concurrency("-3") == 8
    assert integrations._parse_dependency_concurrency("") == 8
    assert integrations._parse_dependency_concurrency(None) == 8
    assert integrations._parse_dependency_concurrency("  4 ") == 4

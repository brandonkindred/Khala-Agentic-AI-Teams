"""
Tests for the GitHub-issue-driven coding-team flow.

Covers the github_source module (client / resolver / mapper) and the
POST /run-from-github endpoint in api/main.py. No real network — every test
either uses httpx.MockTransport for the low-level client or monkey-patches
the GitHubClient and helper functions on the api module.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

from software_engineering_team.github_source import (
    GitHubAPIError,
    GitHubClient,
    GitHubRepoReader,
    Issue,
    NotAnIssueError,
    PullRequest,
    Repo,
    SubIssue,
    is_ready,
    issue_to_plan_input,
    pick_ready_issue,
    scrub_token_from_text,
)
from software_engineering_team.github_source.client import (
    MAX_ISSUES_TRAVERSED,
    _is_safe_ref,
)
from software_engineering_team.github_source.client_http import (
    SECONDARY_RATE_LIMIT_MAX_RETRIES,
    _parse_next_link,
)
from software_engineering_team.models import CodingTeamPlanInput
from software_engineering_team.tests.conftest import _expected_basic_header

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubClient:
    """Build a GitHubClient whose underlying httpx.Client uses a mock transport."""
    transport = httpx.MockTransport(handler)
    client = GitHubClient(token="t", sleep=lambda _s: None)
    client._client.close()  # type: ignore[attr-defined]
    client._client = httpx.Client(transport=transport, timeout=client._timeout)  # type: ignore[attr-defined]
    return client


def _issue_payload(number: int, **overrides: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": overrides.get("title", f"Issue {number}"),
        "body": overrides.get("body"),
        "state": overrides.get("state", "open"),
        "html_url": overrides.get("html_url", f"https://example/issues/{number}"),
        "labels": overrides.get("labels", []),
        **({"pull_request": {}} if overrides.get("is_pr") else {}),
    }


def _sub_payload(number: int, state: str = "open") -> dict[str, Any]:
    return {"number": number, "state": state, "title": f"Sub {number}"}


# ---------------------------------------------------------------------------
# Client: pagination & PR filtering
# ---------------------------------------------------------------------------


class TestClientListOpenIssues:
    def test_paginates_via_link_header(self) -> None:
        page1 = [_issue_payload(1), _issue_payload(2)]
        page2 = [_issue_payload(3)]

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.params.get("page") == "2":
                return httpx.Response(200, json=page2)
            return httpx.Response(
                200,
                json=page1,
                headers={
                    "Link": '<https://api.github.com/x?page=2>; rel="next"',
                },
            )

        client = _client_with(handler)
        numbers = [i.number for i in client.list_open_issues("o", "r")]
        assert numbers == [1, 2, 3]

    def test_filters_out_pull_requests(self) -> None:
        payload = [
            _issue_payload(1),
            _issue_payload(2, is_pr=True),
            _issue_payload(3),
        ]

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = _client_with(handler)
        numbers = [i.number for i in client.list_open_issues("o", "r")]
        assert numbers == [1, 3]

    def test_passes_label_filter(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.url.params.get("labels") or "")
            return httpx.Response(200, json=[])

        client = _client_with(handler)
        list(client.list_open_issues("o", "r", label="ready"))
        assert seen == ["ready"]

    def test_pull_requests_count_toward_max_issues_traversed(self) -> None:
        # Regression (Codex-flagged): a repository dominated by open pull requests
        # (which the /issues endpoint also returns, filtered out client-side) must
        # not be able to bypass MAX_ISSUES_TRAVERSED -- if skipped PRs didn't count
        # toward the cap, a caller reading only the first few yielded issues via
        # itertools.islice would still pay for paginating through every PR page.
        payload = [_issue_payload(n, is_pr=True) for n in range(1, MAX_ISSUES_TRAVERSED + 2)] + [
            _issue_payload(MAX_ISSUES_TRAVERSED + 100)
        ]

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = _client_with(handler)
        numbers = [i.number for i in client.list_open_issues("o", "r")]
        # The single real issue sits past the cap (buried among MAX_ISSUES_TRAVERSED+1
        # pull requests examined first), so traversal stops before ever reaching it.
        assert numbers == []


class TestClientSubIssues:
    def test_404_returns_empty_list(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        client = _client_with(handler)
        assert client.list_sub_issues("o", "r", 7) == []

    def test_paginates(self) -> None:
        page1 = [_sub_payload(10), _sub_payload(11)]
        page2 = [_sub_payload(12)]

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.params.get("page") == "2":
                return httpx.Response(200, json=page2)
            return httpx.Response(
                200,
                json=page1,
                headers={"Link": '<https://api.github.com/x?page=2>; rel="next"'},
            )

        client = _client_with(handler)
        subs = client.list_sub_issues("o", "r", 1)
        assert [s.number for s in subs] == [10, 11, 12]

    def test_non_404_error_raises_even_with_not_found_ok(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        client = _client_with(handler)
        with pytest.raises(GitHubAPIError) as exc_info:
            client.list_sub_issues("o", "r", 7)
        assert exc_info.value.status == 500


class TestClientGetIssue:
    def test_rejects_pr(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_issue_payload(5, is_pr=True))

        client = _client_with(handler)
        with pytest.raises(NotAnIssueError) as exc_info:
            client.get_issue("o", "r", 5)
        # NotAnIssueError remains a GitHubAPIError so existing handlers catch it.
        assert isinstance(exc_info.value, GitHubAPIError)
        assert exc_info.value.number == 5

    def test_coerces_null_body(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_issue_payload(5, body=None))

        client = _client_with(handler)
        assert client.get_issue("o", "r", 5).body == ""


class TestClientUpdatePullRequest:
    def test_patches_pr_body(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["method"] = req.method
            seen["url"] = str(req.url)
            seen["body"] = json.loads(req.content.decode())
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "html_url": "https://example/pr/7",
                    "head": {"ref": "feature"},
                    "base": {"ref": "main"},
                },
            )

        client = _client_with(handler)
        pr = client.update_pull_request(owner="o", repo="r", number=7, body="new body")
        assert seen["method"] == "PATCH"
        assert seen["url"].endswith("/repos/o/r/pulls/7")
        assert seen["body"] == {"body": "new body"}
        assert pr.number == 7
        assert pr.html_url == "https://example/pr/7"


class TestClientGetRepo:
    def test_returns_repo_metadata(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert str(req.url).endswith("/repos/o/r")
            return httpx.Response(200, json={"default_branch": "develop"})

        client = _client_with(handler)
        repo = client.get_repo("o", "r")
        assert repo.default_branch == "develop"

    def test_raises_on_error(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "not found"})

        client = _client_with(handler)
        with pytest.raises(GitHubAPIError):
            client.get_repo("o", "r")


class TestClientFindExistingPr:
    def test_returns_first_open_pr_for_head(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["params"] = dict(req.url.params)
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 9,
                        "html_url": "https://example/pr/9",
                        "head": {"ref": "feature"},
                        "base": {"ref": "main"},
                    }
                ],
            )

        client = _client_with(handler)
        pr = client.find_existing_pr("o", "r", "feature")
        assert seen["params"] == {"state": "open", "head": "o:feature"}
        assert pr is not None
        assert pr.number == 9
        assert pr.head == "feature"

    def test_returns_none_when_no_match(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client = _client_with(handler)
        assert client.find_existing_pr("o", "r", "feature") is None


class TestClientCreatePullRequest:
    def test_posts_and_returns_pr(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["method"] = req.method
            seen["url"] = str(req.url)
            seen["body"] = json.loads(req.content.decode())
            return httpx.Response(
                201,
                json={
                    "number": 11,
                    "html_url": "https://example/pr/11",
                    "head": {"ref": "feature"},
                    "base": {"ref": "main"},
                },
            )

        client = _client_with(handler)
        pr = client.create_pull_request(
            owner="o",
            repo="r",
            title="Add thing",
            head="feature",
            base="main",
            body="does the thing",
            draft=False,
        )
        assert seen["method"] == "POST"
        assert seen["url"].endswith("/repos/o/r/pulls")
        assert seen["body"] == {
            "title": "Add thing",
            "head": "feature",
            "base": "main",
            "body": "does the thing",
            "draft": False,
        }
        assert pr.number == 11
        assert pr.head == "feature"
        assert pr.base == "main"

    def test_defaults_draft_true(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content.decode())
            return httpx.Response(
                201,
                json={
                    "number": 12,
                    "html_url": "https://example/pr/12",
                    "head": {"ref": "feature"},
                    "base": {"ref": "main"},
                },
            )

        client = _client_with(handler)
        client.create_pull_request(
            owner="o", repo="r", title="t", head="feature", base="main", body="b"
        )
        assert seen["body"]["draft"] is True

    def test_raises_on_error(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"message": "no commits between branches"})

        client = _client_with(handler)
        with pytest.raises(GitHubAPIError):
            client.create_pull_request(
                owner="o", repo="r", title="t", head="feature", base="main", body="b"
            )


class TestClientLifecycle:
    def test_close_closes_underlying_httpx_client(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, json={}))
        client.close()
        assert client._client.is_closed  # type: ignore[attr-defined]

    def test_context_manager_closes_on_exit(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, json={}))
        with client as c:
            assert c is client
            assert not client._client.is_closed  # type: ignore[attr-defined]
        assert client._client.is_closed  # type: ignore[attr-defined]


class TestClientTransportErrorRetry:
    def test_retries_on_transport_error_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json={"default_branch": "main"})

        client = _client_with(handler)
        repo = client.get_repo("o", "r")
        assert repo.default_branch == "main"
        assert calls["n"] == 2

    def test_exhausts_retries_on_persistent_transport_error_raises(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("connection refused")

        client = _client_with(handler)
        with pytest.raises(GitHubAPIError) as exc_info:
            client.get_repo("o", "r")
        assert exc_info.value.status == 0
        assert "transport error" in str(exc_info.value)
        # max_retries default = 3
        assert calls["n"] == 3


class TestClientRetries:
    def test_retries_on_502_then_raises(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(502, json={"message": "bad gateway"})

        client = _client_with(handler)
        with pytest.raises(GitHubAPIError) as exc_info:
            client.get_repo("o", "r")
        # Exhausting retries on a 5xx must surface the last response's actual
        # status/body, not the generic 0/"exceeded retries" placeholder.
        assert exc_info.value.status == 502
        assert "bad gateway" in exc_info.value.body
        # max_retries default = 3
        assert calls["n"] == 3

    def test_rate_limit_sleeps_and_retries(self) -> None:
        slept: list[float] = []

        def handler(_req: httpx.Request) -> httpx.Response:
            if not slept:  # first call: rate-limited
                return httpx.Response(
                    403,
                    json={"message": "rate limited"},
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "0",
                    },
                )
            return httpx.Response(200, json={"default_branch": "main"})

        transport = httpx.MockTransport(handler)
        client = GitHubClient(token="t", sleep=lambda s: slept.append(s))
        client._client.close()  # type: ignore[attr-defined]
        client._client = httpx.Client(transport=transport, timeout=10)  # type: ignore[attr-defined]

        repo = client.get_repo("o", "r")
        assert repo.default_branch == "main"
        assert len(slept) == 1

    def test_rate_limit_retried_even_when_not_first_attempt(self) -> None:
        # The primary rate-limit branch must retry regardless of which attempt
        # index it's hit on, not just attempt 0. Force a 502 retry first
        # (bumping the attempt index), then a rate-limited 403, then success.
        slept: list[float] = []
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(502, json={"message": "bad gateway"})
            if calls["n"] == 2:
                return httpx.Response(
                    403,
                    json={"message": "rate limited"},
                    headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"},
                )
            return httpx.Response(200, json={"default_branch": "main"})

        transport = httpx.MockTransport(handler)
        client = GitHubClient(token="t", sleep=lambda s: slept.append(s))
        client._client.close()  # type: ignore[attr-defined]
        client._client = httpx.Client(transport=transport, timeout=10)  # type: ignore[attr-defined]

        repo = client.get_repo("o", "r")
        assert repo.default_branch == "main"
        assert calls["n"] == 3
        # One sleep for the 502 retry, one for the 403 rate-limit retry.
        assert len(slept) == 2

    def test_rate_limit_missing_reset_header_defaults_wait_to_one_second(self) -> None:
        slept: list[float] = []

        def handler(_req: httpx.Request) -> httpx.Response:
            if not slept:
                return httpx.Response(
                    403,
                    json={"message": "rate limited"},
                    headers={"X-RateLimit-Remaining": "0"},
                )
            return httpx.Response(200, json={"default_branch": "main"})

        transport = httpx.MockTransport(handler)
        client = GitHubClient(token="t", sleep=lambda s: slept.append(s))
        client._client.close()  # type: ignore[attr-defined]
        client._client = httpx.Client(transport=transport, timeout=10)  # type: ignore[attr-defined]

        repo = client.get_repo("o", "r")
        assert repo.default_branch == "main"
        assert slept == [1.0]

    def test_rate_limit_non_numeric_reset_header_defaults_wait_to_one_second(self) -> None:
        slept: list[float] = []

        def handler(_req: httpx.Request) -> httpx.Response:
            if not slept:
                return httpx.Response(
                    403,
                    json={"message": "rate limited"},
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "not-a-number",
                    },
                )
            return httpx.Response(200, json={"default_branch": "main"})

        transport = httpx.MockTransport(handler)
        client = GitHubClient(token="t", sleep=lambda s: slept.append(s))
        client._client.close()  # type: ignore[attr-defined]
        client._client = httpx.Client(transport=transport, timeout=10)  # type: ignore[attr-defined]

        repo = client.get_repo("o", "r")
        assert repo.default_branch == "main"
        assert slept == [1.0]

    def test_403_without_rate_limit_headers_raises_immediately(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(403, json={"message": "forbidden"})

        client = _client_with(handler)
        with pytest.raises(GitHubAPIError) as exc_info:
            client.get_repo("o", "r")
        assert exc_info.value.status == 403
        assert calls["n"] == 1


class TestClientSecondaryRateLimitRetry:
    def test_429_retries_then_succeeds(self) -> None:
        slept: list[float] = []
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] <= 2:
                return httpx.Response(
                    429,
                    json={"message": "You have exceeded a secondary rate limit"},
                    headers={"Retry-After": "2"},
                )
            return httpx.Response(200, json={"default_branch": "main"})

        transport = httpx.MockTransport(handler)
        client = GitHubClient(token="t", sleep=lambda s: slept.append(s))
        client._client.close()  # type: ignore[attr-defined]
        client._client = httpx.Client(transport=transport, timeout=10)  # type: ignore[attr-defined]

        repo = client.get_repo("o", "r")
        assert repo.default_branch == "main"
        assert calls["n"] == 3
        assert len(slept) == 2
        assert all(s >= 2.0 for s in slept)

    def test_429_honors_retry_after_header_value(self) -> None:
        slept: list[float] = []
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    429,
                    json={"message": "rate limited"},
                    headers={"Retry-After": "30"},
                )
            return httpx.Response(200, json={"default_branch": "main"})

        transport = httpx.MockTransport(handler)
        client = GitHubClient(token="t", sleep=lambda s: slept.append(s))
        client._client.close()  # type: ignore[attr-defined]
        client._client = httpx.Client(transport=transport, timeout=10)  # type: ignore[attr-defined]

        repo = client.get_repo("o", "r")
        assert repo.default_branch == "main"
        assert len(slept) == 1
        assert slept[0] >= 30.0

    def test_429_exhausts_retries_and_raises_hard_failure(self) -> None:
        slept: list[float] = []
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                429,
                json={"message": "You have exceeded a secondary rate limit"},
                headers={"Retry-After": "1"},
            )

        transport = httpx.MockTransport(handler)
        client = GitHubClient(token="t", sleep=lambda s: slept.append(s))
        client._client.close()  # type: ignore[attr-defined]
        client._client = httpx.Client(transport=transport, timeout=10)  # type: ignore[attr-defined]

        with pytest.raises(GitHubAPIError) as exc_info:
            client.get_repo("o", "r")
        assert exc_info.value.status == 429
        # 1 initial request + SECONDARY_RATE_LIMIT_MAX_RETRIES retries.
        assert calls["n"] == 1 + SECONDARY_RATE_LIMIT_MAX_RETRIES
        assert len(slept) == SECONDARY_RATE_LIMIT_MAX_RETRIES

    def test_429_missing_retry_after_falls_back_to_backoff(self) -> None:
        slept: list[float] = []
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, json={"message": "rate limited"})
            return httpx.Response(200, json={"default_branch": "main"})

        transport = httpx.MockTransport(handler)
        client = GitHubClient(token="t", sleep=lambda s: slept.append(s))
        client._client.close()  # type: ignore[attr-defined]
        client._client = httpx.Client(transport=transport, timeout=10)  # type: ignore[attr-defined]

        repo = client.get_repo("o", "r")
        assert repo.default_branch == "main"
        assert calls["n"] == 2
        assert len(slept) == 1
        assert slept[0] > 0.0


class TestGitHubHttpHeaders:
    def test_headers_sent_on_request(self) -> None:
        seen: dict[str, str] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen.update(req.headers)
            return httpx.Response(200, json={"default_branch": "main"})

        client = _client_with(handler)
        client.get_repo("o", "r")
        assert seen["authorization"] == "Bearer t"
        assert seen["accept"] == "application/vnd.github+json"
        assert seen["x-github-api-version"] == "2022-11-28"
        assert seen["user-agent"] == "khala-coding-team"


class TestAbsoluteUrl:
    def test_passes_through_already_absolute(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, json={}))
        assert client._absolute_url("https://example.com/x") == "https://example.com/x"  # type: ignore[attr-defined]

    def test_prepends_base_when_leading_slash_present(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, json={}))
        assert client._absolute_url("/repos/o/r") == f"{client._base_url}/repos/o/r"  # type: ignore[attr-defined]

    def test_prepends_leading_slash_when_missing(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, json={}))
        assert client._absolute_url("repos/o/r") == f"{client._base_url}/repos/o/r"  # type: ignore[attr-defined]


class TestClientIssueCommentMarker:
    def test_add_issue_comment_appends_khala_marker(self) -> None:
        """Every Khala-posted conversation comment carries the invisible marker so the
        '@khala review' webhook can recognize (and never re-trigger on) Khala's own
        output — author identity can't do this, since Khala posts with the operator's
        PAT and the PAT owner may be exactly the person triggering reviews."""
        from software_engineering_team.github_source.client import KHALA_COMMENT_MARKER

        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content.decode())
            return httpx.Response(201, json={"id": 1})

        _client_with(handler).add_issue_comment("acme", "widget", 42, "Code review failed: boom")
        assert seen["body"]["body"] == f"Code review failed: boom\n\n{KHALA_COMMENT_MARKER}"

    def test_add_issue_comment_does_not_duplicate_marker(self) -> None:
        from software_engineering_team.github_source.client import KHALA_COMMENT_MARKER

        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content.decode())
            return httpx.Response(201, json={"id": 1})

        body = f"already marked\n\n{KHALA_COMMENT_MARKER}"
        _client_with(handler).add_issue_comment("acme", "widget", 42, body)
        assert seen["body"]["body"] == body
        assert seen["body"]["body"].count(KHALA_COMMENT_MARKER) == 1


class TestClientCreateIssue:
    def test_create_issue_posts_and_returns_issue(self) -> None:
        from software_engineering_team.github_source.client import KHALA_COMMENT_MARKER

        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["method"] = req.method
            seen["url"] = str(req.url)
            seen["body"] = json.loads(req.content.decode())
            return httpx.Response(
                201,
                json={
                    "number": 99,
                    "title": "t",
                    "body": "b",
                    "state": "open",
                    "html_url": "https://example/issues/99",
                    "labels": [],
                },
            )

        issue = _client_with(handler).create_issue(
            "acme", "widget", title="Fix bug", body="details", labels=["bug"]
        )
        assert seen["method"] == "POST"
        assert seen["url"].endswith("/repos/acme/widget/issues")
        assert seen["body"]["title"] == "Fix bug"
        # The marker is appended (provenance), and labels are forwarded.
        assert seen["body"]["body"] == f"details\n\n{KHALA_COMMENT_MARKER}"
        assert seen["body"]["labels"] == ["bug"]
        assert issue.number == 99
        assert issue.html_url == "https://example/issues/99"

    def test_create_issue_omits_labels_when_none(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content.decode())
            return httpx.Response(
                201,
                json={"number": 1, "title": "t", "body": "b", "state": "open", "html_url": "u"},
            )

        _client_with(handler).create_issue("acme", "widget", title="t", body="b")
        assert "labels" not in seen["body"]

    def test_create_issue_raises_on_error(self) -> None:
        from software_engineering_team.github_source import GitHubAPIError

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "no scope"})

        with pytest.raises(GitHubAPIError):
            _client_with(handler).create_issue("acme", "widget", title="t", body="b")


class TestClientCommentReaction:
    def test_posts_eyes_reaction_to_comment(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["method"] = req.method
            seen["path"] = req.url.path
            seen["body"] = json.loads(req.content.decode())
            return httpx.Response(201, json={"id": 1, "content": "eyes"})

        client = _client_with(handler)
        client.create_comment_reaction("acme", "widget", 555)
        assert seen["method"] == "POST"
        assert seen["path"] == "/repos/acme/widget/issues/comments/555/reactions"
        assert seen["body"] == {"content": "eyes"}

    def test_accepts_already_exists_200(self) -> None:
        # GitHub returns 200 (not 201) when the reaction already exists; both are OK.
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 1, "content": "eyes"})

        result = _client_with(handler).create_comment_reaction(
            "acme", "widget", 555, content="eyes"
        )
        # No exception on a 200 (vs. the 201 the happy-path test exercises), and the
        # documented contract is a bare None return either way.
        assert result is None

    def test_raises_on_error(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        with pytest.raises(GitHubAPIError):
            _client_with(handler).create_comment_reaction("acme", "widget", 555)


class TestCreateIssueReaction:
    def test_posts_plus_one_reaction_to_pr(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["method"] = req.method
            seen["path"] = req.url.path
            seen["body"] = json.loads(req.content.decode())
            return httpx.Response(201, json={"id": 1, "content": "+1"})

        client = _client_with(handler)
        client.create_issue_reaction("acme", "widget", 555)
        assert seen["method"] == "POST"
        assert seen["path"] == "/repos/acme/widget/issues/555/reactions"
        assert seen["body"] == {"content": "+1"}

    def test_accepts_already_exists_200(self) -> None:
        # GitHub returns 200 (not 201) when the reaction already exists; both are OK.
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": 1, "content": "+1"})

        result = _client_with(handler).create_issue_reaction("acme", "widget", 555, content="+1")
        assert result is None

    def test_raises_on_error(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        with pytest.raises(GitHubAPIError):
            _client_with(handler).create_issue_reaction("acme", "widget", 555)


class TestScrubTokenFromText:
    def test_redacts_user_at_url(self) -> None:
        # Build the credentialed URL at runtime so the literal `user:pwd@host`
        # pattern never appears contiguously in source — secret scanners
        # (GitGuardian etc.) flag that pattern regardless of how fake the
        # values look.
        scheme = "https://"
        user = "u" + "ser"
        pwd = "fa" + "ke"
        msg = f"fatal: unable to push to {scheme}{user}:{pwd}@example.com/repo.git"

        out = scrub_token_from_text(msg)
        assert pwd not in out
        assert user not in out
        assert "https://***@example.com/repo.git" in out

    def test_idempotent_on_clean_text(self) -> None:
        assert scrub_token_from_text("nothing sensitive here") == "nothing sensitive here"

    def test_handles_empty(self) -> None:
        assert scrub_token_from_text("") == ""


class TestIsSafeRef:
    def test_accepts_normal_branch_names(self) -> None:
        assert _is_safe_ref("main")
        assert _is_safe_ref("feature/foo-bar")
        assert _is_safe_ref("release_2.1.0")

    def test_rejects_leading_dash(self) -> None:
        assert not _is_safe_ref("-evil")

    def test_rejects_shell_metacharacters(self) -> None:
        assert not _is_safe_ref("foo;rm -rf /")
        assert not _is_safe_ref("foo bar")
        assert not _is_safe_ref("$(echo)")

    def test_rejects_empty(self) -> None:
        assert not _is_safe_ref("")


class TestParseNextLink:
    def test_extracts_next(self) -> None:
        header = (
            '<https://api.github.com/r?page=2>; rel="next", '
            '<https://api.github.com/r?page=5>; rel="last"'
        )
        assert _parse_next_link(header) == "https://api.github.com/r?page=2"

    def test_no_next(self) -> None:
        assert _parse_next_link(None) is None
        assert _parse_next_link('<x>; rel="last"') is None

    def test_ignores_unrelated_rel_before_next(self) -> None:
        header = (
            '<https://x/a>; rel="prev", <https://x/b?page=3>; rel="next", <https://x/c>; rel="last"'
        )
        assert _parse_next_link(header) == "https://x/b?page=3"


class TestGitHubAPIError:
    def test_constructs_with_status_and_body(self) -> None:
        err = GitHubAPIError(404, "not found")
        assert err.status == 404
        assert err.body == "not found"
        assert str(err) == "GitHub API 404: not found"

    def test_defaults_body_to_empty_string(self) -> None:
        err = GitHubAPIError(500)
        assert err.body == ""
        assert str(err) == "GitHub API 500: "


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class _FakeClient:
    """Just the surface dependency_resolver / endpoint code touches."""

    def __init__(
        self,
        issues: Optional[list[Issue]] = None,
        sub_map: Optional[dict[int, list[SubIssue]]] = None,
        repo: Optional[Repo] = None,
        existing_pr: Optional[PullRequest] = None,
    ) -> None:
        self._issues = issues or []
        self._sub_map = sub_map or {}
        self._repo = repo or Repo(default_branch="main")
        self._existing_pr = existing_pr
        self.created_pulls: list[dict[str, Any]] = []
        self.updated_pulls: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.fail_comments = False
        self.fail_get_repo = False
        self.get_repo_calls = 0

    def list_open_issues(self, _o: str, _r: str, label: Optional[str] = None):
        for i in self._issues:
            if label and label not in i.labels:
                continue
            yield i

    def get_issue(self, _o: str, _r: str, n: int) -> Issue:
        for i in self._issues:
            if i.number == n:
                return i
        raise GitHubAPIError(404, f"missing #{n}")

    def list_sub_issues(self, _o: str, _r: str, n: int) -> list[SubIssue]:
        return list(self._sub_map.get(n, []))

    def get_repo(self, _o: str, _r: str) -> Repo:
        self.get_repo_calls += 1
        if self.fail_get_repo:
            raise GitHubAPIError(500, "boom")
        return self._repo

    def add_issue_comment(self, _o: str, _r: str, n: int, body: str) -> None:
        if self.fail_comments:
            raise GitHubAPIError(403, "no scope")
        self.comments.append((n, body))

    def find_existing_pr(self, _o: str, _r: str, _h: str):
        return self._existing_pr

    # Context-manager protocol so the production code's `with` blocks work.
    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def create_pull_request(self, **kwargs: Any) -> PullRequest:
        self.created_pulls.append(kwargs)
        return PullRequest(
            number=42,
            html_url="https://example/pr/42",
            head=kwargs["head"],
            base=kwargs["base"],
        )

    def update_pull_request(self, *, owner: str, repo: str, number: int, body: str) -> PullRequest:
        self.updated_pulls.append({"number": number, "body": body})
        existing = self._existing_pr
        return PullRequest(
            number=number,
            html_url=existing.html_url if existing else "https://example/pr/updated",
            head=existing.head if existing else "head",
            base=existing.base if existing else "base",
        )


def _issue(num: int, title: str = "T", body: str = "B", labels: tuple[str, ...] = ()) -> Issue:
    return Issue(
        number=num,
        title=title,
        body=body,
        state="open",
        html_url=f"https://example/issues/{num}",
        labels=labels,
    )


class TestIsReady:
    def test_no_subs_is_ready(self) -> None:
        c = _FakeClient(sub_map={})
        r = is_ready(c, "o", "r", _issue(1))
        assert r.ready is True
        assert r.blocking == ()

    def test_all_closed_is_ready(self) -> None:
        c = _FakeClient(sub_map={1: [SubIssue(2, "closed", "x"), SubIssue(3, "closed", "y")]})
        r = is_ready(c, "o", "r", _issue(1))
        assert r.ready is True

    def test_any_open_blocks(self) -> None:
        c = _FakeClient(sub_map={1: [SubIssue(2, "closed", "x"), SubIssue(3, "open", "y")]})
        r = is_ready(c, "o", "r", _issue(1))
        assert r.ready is False
        assert r.blocking == (3,)


class TestPickReady:
    def test_skips_blocked_returns_first_ready(self) -> None:
        c = _FakeClient(
            issues=[_issue(1), _issue(2), _issue(3)],
            sub_map={
                1: [SubIssue(99, "open", "x")],
                2: [],
                3: [],
            },
        )
        picked = pick_ready_issue(c, "o", "r")
        assert picked is not None
        issue, ready = picked
        assert issue.number == 2
        assert ready.ready is True

    def test_returns_none_when_all_blocked(self) -> None:
        c = _FakeClient(
            issues=[_issue(1), _issue(2)],
            sub_map={
                1: [SubIssue(10, "open", "x")],
                2: [SubIssue(11, "open", "y")],
            },
        )
        assert pick_ready_issue(c, "o", "r") is None

    def test_label_filter_passes_through(self) -> None:
        c = _FakeClient(
            issues=[
                _issue(1, labels=("other",)),
                _issue(2, labels=("ready",)),
            ],
            sub_map={1: [], 2: []},
        )
        picked = pick_ready_issue(c, "o", "r", label="ready")
        assert picked is not None
        assert picked[0].number == 2


# ---------------------------------------------------------------------------
# issue_to_plan_input
# ---------------------------------------------------------------------------


class TestIssueToPlanInput:
    def test_maps_basic_fields(self) -> None:
        plan = issue_to_plan_input(
            _issue(7, title="Add login", body="Sign in with email."),
            "/tmp/repo",
            sub_issues=[],
            owner="o",
            repo="r",
        )
        assert isinstance(plan, CodingTeamPlanInput)
        assert plan.requirements_title == "Add login"
        assert plan.requirements_description == "Sign in with email."
        assert plan.repo_path == "/tmp/repo"
        gh = plan.project_overview["github_issue"]
        assert gh["owner"] == "o"
        assert gh["repo"] == "r"
        assert gh["number"] == 7
        assert plan.completed_work_summary is None

    def test_summarizes_closed_sub_issues(self) -> None:
        plan = issue_to_plan_input(
            _issue(7),
            "/tmp/repo",
            sub_issues=[
                SubIssue(8, "closed", "Schema"),
                SubIssue(9, "closed", "Migrations"),
            ],
            owner="o",
            repo="r",
        )
        assert plan.completed_work_summary is not None
        assert "#8 Schema" in plan.completed_work_summary
        assert "#9 Migrations" in plan.completed_work_summary
        # Closed sub-issues are completed-work evidence, not ordinary repo context.
        assert plan.existing_code_summary is None

    def test_skips_open_sub_issues_in_summary(self) -> None:
        plan = issue_to_plan_input(
            _issue(7),
            "/tmp/repo",
            sub_issues=[SubIssue(8, "open", "WIP")],
            owner="o",
            repo="r",
        )
        assert plan.completed_work_summary is None


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


def _stub_heavy_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Pre-load lightweight stand-ins for the LLM/agent modules that api.main
    transitively imports. This keeps the endpoint tests insulated from the
    full agent stack (strands, llm_service, software_engineering_team, ...).
    """
    import sys
    import types

    # Replace coding_team.orchestrator with a stub exposing only the symbol
    # api.main imports. Tests monkey-patch the function on api.main itself.
    # monkeypatch.setitem (not a bare sys.modules[...] = stub assignment) so
    # this entry is automatically reverted at the end of THIS test — otherwise
    # the stub outlives the test and can leak into an unrelated test (in this
    # file, or another sharing the same xdist worker process) that needs the
    # real orchestrator module.
    if "software_engineering_team.coding_team_orchestrator" not in sys.modules or not hasattr(
        sys.modules["software_engineering_team.coding_team_orchestrator"], "_stubbed"
    ):
        stub = types.ModuleType("software_engineering_team.coding_team_orchestrator")
        stub._stubbed = True  # type: ignore[attr-defined]

        def _noop(*_a: Any, **_kw: Any) -> None:
            return None

        stub.run_coding_team_orchestrator = _noop  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "software_engineering_team.coding_team_orchestrator", stub)

    # git_utils now lives in the neutral, stdlib-only shared.git package, so the
    # real module imports cheaply. Importing it (instead of injecting a fake into
    # sys.modules) avoids leaking a stub that poisons other test files under xdist.
    import shared.git.git_utils  # noqa: F401

    gu_mod = sys.modules["shared.git.git_utils"]
    if not hasattr(gu_mod, "git_identity_env"):
        # Functional stand-in mirroring the real helper: api.main imports it
        # for the recovered-WIP merge, which needs a complete commit identity
        # in identity-free environments.
        def _stub_git_identity_env():
            env = dict(os.environ)
            for key in (
                "GIT_AUTHOR_NAME",
                "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL",
            ):
                if not (env.get(key) or "").strip():
                    env[key] = "stub@example.com" if key.endswith("EMAIL") else "Stub"
            return env

        monkeypatch.setattr(gu_mod, "git_identity_env", _stub_git_identity_env)
    if not hasattr(gu_mod, "commit_working_tree"):
        # Functional stand-in: api.main imports commit_working_tree for dirty-tree
        # recovery, and TestPrepareIssueBranch exercises that path against real
        # repos — a (True, "...") no-op stub would leave the tree dirty and make
        # those tests order-dependent on whether the real module loaded first.
        def _stub_commit_working_tree(repo_path, message):
            import subprocess as sp

            sp.run(["git", "-C", str(repo_path), "add", "-A"], capture_output=True)
            r = sp.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "-c",
                    "user.name=Stub",
                    "-c",
                    "user.email=stub@example.com",
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    message,
                ],
                capture_output=True,
                text=True,
            )
            ok = r.returncode == 0 or "nothing to commit" in (r.stdout + r.stderr)
            return ok, (r.stdout + r.stderr).strip()

        monkeypatch.setattr(gu_mod, "commit_working_tree", _stub_commit_working_tree)


@pytest.fixture
def patched_app(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """
    Wire the coding_team API with:
      * a FakeJobServiceClient backing job_store
      * GitHubClient replaced with a stub (per test, via the returned setter)
      * start_coding_team_workflow captured for route-level dispatch assertions
      * git helpers that succeed by default
      * orchestrator no-op that records a merged task
    """
    _stub_heavy_modules(monkeypatch)

    from job_service_client_fake import FakeJobServiceClient

    fake_jobs = FakeJobServiceClient(team="coding_team")

    from software_engineering_team import job_store as job_store_mod

    monkeypatch.setattr(job_store_mod, "_client", lambda *a, **kw: fake_jobs)

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    from software_engineering_team.api import coding_team_main as api_main

    holder: dict[str, Any] = {"client": _FakeClient()}

    def _make_client(**_kw: Any) -> _FakeClient:
        return holder["client"]

    monkeypatch.setattr(api_main, "GitHubClient", _make_client)

    import software_engineering_team.api.routes.github as gh_routes

    started: list[dict[str, Any]] = []

    def _capture_start(job_id: str, repo_path: str, plan_input: dict[str, Any], github=None) -> None:
        """Record Temporal workflow starts for route-level assertions.

        Preconditions:
            - The route passed the job id, repo path, plan input, and optional
              GitHub metadata it would send to Temporal.
        Postconditions:
            - ``started`` contains one additional capture dictionary preserving
              the call arguments.
        """
        started.append(
            {
                "job_id": job_id,
                "repo_path": repo_path,
                "plan_input": plan_input,
                "github": github,
            }
        )

    monkeypatch.setattr(gh_routes, "start_coding_team_workflow", _capture_start, raising=False)

    # Git helpers: success by default.
    monkeypatch.setattr(api_main, "_prepare_issue_branch", lambda *a, **kw: (True, None, []))
    monkeypatch.setattr(api_main, "_fast_forward", lambda *a, **kw: (True, None))
    monkeypatch.setattr(api_main, "_push_branch", lambda *a, **kw: (True, None))

    # Orchestrator no-op: mark a merged task on the job.
    def _fake_orchestrator(job_id: str, _repo_path, _plan, **kw):
        update_fn = kw["update_job_fn"]
        update_fn(
            status="completed",
            phase="completed",
            task_graph_snapshot=[
                {
                    "id": "t1",
                    "status": "merged",
                    "feature_branch": "feature/t1",
                    "merged_at": "2026-05-10T00:00:00Z",
                }
            ],
        )

    monkeypatch.setattr(api_main, "run_coding_team_orchestrator", _fake_orchestrator)

    from fastapi.testclient import TestClient

    return {
        "client": TestClient(api_main.app),
        "api": api_main,
        "repo_path": str(repo_path),
        "set_github": lambda fc: holder.__setitem__("client", fc),
        "github": lambda: holder["client"],
        "jobs": fake_jobs,
        "started_workflows": started,
    }


def _body(issue_number: int = 1, **overrides: Any) -> dict[str, Any]:
    return {
        "owner": "o",
        "repo": "r",
        "repo_path": overrides.pop("repo_path"),
        "issue_number": issue_number,
        **overrides,
    }


def _post_run_from_github_then_run_legacy_hooks(patched_app, json: dict[str, Any]):
    """Post to the route, then explicitly drive the *legacy* hook path.

    This is NOT route/Temporal coverage. ``POST /run-from-github`` starts
    ``CodingTeamWorkflow`` and never calls ``_run_with_github_hooks``; this
    helper posts (asserting Temporal start succeeds) and then synchronously
    invokes ``_run_with_github_hooks`` so legacy hook-focused unit tests can
    still exercise comments, busy-checkout, publish-window, and cleanup
    behavior that has not yet been moved into Temporal activities.

    Preconditions:
        - ``patched_app`` is the endpoint fixture from this module, with a captured
          workflow start for successful ``/run-from-github`` responses.
        - ``json`` is a valid ``RunFromGitHubRequest`` payload for the fake app.
    Postconditions:
        - Returns the route response unchanged.
        - For 200 responses, the route has started the workflow and this helper
          has synchronously driven ``_run_with_github_hooks`` for legacy-hook
          assertions only.
    """
    resp = patched_app["client"].post("/run-from-github", json=json)
    if resp.status_code != 200:
        return resp

    started = patched_app["started_workflows"][-1]
    api = patched_app["api"]
    request = api.RunFromGitHubRequest(**json)
    issue = patched_app["github"]().get_issue(request.owner, request.repo, resp.json()["issue_number"])
    plan = CodingTeamPlanInput.model_validate(started["plan_input"])
    token = json.get("github_token") or os.environ["GITHUB_TOKEN"]
    api._run_with_github_hooks(started["job_id"], request, plan, issue, token)
    return resp


# Backward-compatible alias used by legacy-hook unit tests below.
_post_run_from_github_and_run_hooks = _post_run_from_github_then_run_legacy_hooks


class TestEndpointHappyPath:
    def test_run_from_github_starts_coding_team_workflow(self, patched_app, monkeypatch) -> None:
        import software_engineering_team.api.routes.github as gh_routes

        started: dict[str, Any] = {}

        def _capture(job_id: str, repo_path: str, plan_input: dict[str, Any], github=None) -> None:
            """Capture the workflow start from this focused route test.

            Preconditions:
                - The route supplies the same arguments it would pass to the
                  Temporal start helper in production.
            Postconditions:
                - ``started`` contains the captured arguments keyed by name.
            """
            started["job_id"] = job_id
            started["repo_path"] = repo_path
            started["plan_input"] = plan_input
            started["github"] = github

        monkeypatch.setattr(gh_routes, "start_coding_team_workflow", _capture)

        gh = _FakeClient(
            issues=[_issue(1, title="Add feature")],
            sub_map={1: []},
            repo=Repo(default_branch="trunk"),
        )
        patched_app["set_github"](gh)

        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(
                1,
                repo_path=patched_app["repo_path"],
                remote="upstream",
                cleanup_checkout_on_success=True,
            ),
        )

        assert resp.status_code == 200, resp.text
        assert started["repo_path"] == patched_app["repo_path"]
        assert started["plan_input"]["requirements_title"] == "Add feature"
        assert started["github"] == {
            "owner": "o",
            "repo": "r",
            "issue_number": 1,
            "issue_title": "Add feature",
            "remote": "upstream",
            "base": "trunk",
            "integration_branch": "khala/issue-1",
            "cleanup_checkout_on_success": True,
        }
        assert "token" not in started["github"]
        assert gh.get_repo_calls == 1

    def test_run_from_github_skips_get_repo_when_base_branch_supplied(
        self, patched_app, monkeypatch
    ) -> None:
        """When the caller supplies base_branch, do not call get_repo for default_branch."""
        import software_engineering_team.api.routes.github as gh_routes

        started: dict[str, Any] = {}

        def _capture(job_id: str, repo_path: str, plan_input: dict[str, Any], github=None) -> None:
            started["github"] = github

        monkeypatch.setattr(gh_routes, "start_coding_team_workflow", _capture)

        gh = _FakeClient(
            issues=[_issue(1, title="Add feature")],
            sub_map={1: []},
            repo=Repo(default_branch="trunk"),
        )
        patched_app["set_github"](gh)

        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(
                1,
                repo_path=patched_app["repo_path"],
                base_branch="release",
            ),
        )

        assert resp.status_code == 200, resp.text
        assert started["github"]["base"] == "release"
        assert gh.get_repo_calls == 0

    def test_run_from_github_marks_job_failed_and_503_when_temporal_dispatch_raises(
        self, patched_app, monkeypatch
    ) -> None:
        """When Temporal dispatch fails, the route must mark the job failed and return 503."""
        import software_engineering_team.api.routes.github as gh_routes

        def _raise(*a, **k):
            raise RuntimeError("temporal down")

        monkeypatch.setattr(gh_routes, "start_coding_team_workflow", _raise)

        gh = _FakeClient(
            issues=[_issue(1, title="Add feature")],
            sub_map={1: []},
            repo=Repo(default_branch="trunk"),
        )
        patched_app["set_github"](gh)

        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )

        assert resp.status_code == 503
        jobs = patched_app["jobs"].list_jobs()
        assert len(jobs) == 1
        job = patched_app["jobs"].get_job(jobs[0]["job_id"])
        assert job is not None
        assert job["status"] == "failed"
        assert "Temporal dispatch failed" in job["error"]

    def test_picks_ready_issue_and_opens_pr(self, patched_app) -> None:
        gh = _FakeClient(
            issues=[_issue(11, title="Add feature")],
            sub_map={11: []},
        )
        patched_app["set_github"](gh)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            {
                "owner": "o",
                "repo": "r",
                "repo_path": patched_app["repo_path"],
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["issue_number"] == 11
        # Two comments: start + draft-PR-opened
        assert len(gh.comments) == 2
        assert "started job" in gh.comments[0][1]
        assert "Draft PR opened" in gh.comments[1][1]
        # PR was created
        assert len(gh.created_pulls) == 1
        assert gh.created_pulls[0]["draft"] is True
        assert gh.created_pulls[0]["head"] == "khala/issue-11"
        assert gh.created_pulls[0]["base"] == "main"
        # A clean run auto-closes the issue on merge.
        assert "Closes #11" in gh.created_pulls[0]["body"]
        # Job persisted with PR url
        job = patched_app["jobs"].get_job(data["job_id"])
        assert job["github_pr_url"] == "https://example/pr/42"
        assert job["integration_branch"] == "khala/issue-11"

    def test_persists_request_token_encrypted_for_resume(self, patched_app, monkeypatch) -> None:
        """The per-request PAT is persisted ENCRYPTED on the job record so a later resume (after the
        orchestrator thread dies) can re-drive the GitHub publish flow without a GITHUB_TOKEN env —
        and the raw record (echoed by the generic GET /api/jobs/{team}) never holds a usable PAT."""
        from cryptography.fernet import Fernet

        from software_engineering_team import token_crypto

        monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())
        gh = _FakeClient(issues=[_issue(11, title="Add feature")], sub_map={11: []})
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json={
                "owner": "o",
                "repo": "r",
                "repo_path": patched_app["repo_path"],
                "github_token": "request-pat",
            },
        )
        assert resp.status_code == 200, resp.text
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        # Only opaque ciphertext is stored; the plaintext PAT appears nowhere in the record.
        assert "github_token" not in job
        assert job["github_token_encrypted"] != "request-pat"
        assert token_crypto.decrypt_token(job["github_token_encrypted"]) == "request-pat"
        assert "request-pat" not in json.dumps(job)

    def test_no_encryption_key_skips_token_persistence(self, patched_app, monkeypatch) -> None:
        """With no key configured, the token is simply not persisted (resume falls back to env)."""
        from software_engineering_team import token_crypto

        monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
        monkeypatch.setattr(token_crypto, "_load_key", lambda: None)
        gh = _FakeClient(issues=[_issue(11, title="Add feature")], sub_map={11: []})
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json={"owner": "o", "repo": "r", "repo_path": patched_app["repo_path"]},
        )
        assert resp.status_code == 200, resp.text
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert "github_token_encrypted" not in job
        assert "github_token" not in job

    def test_specific_issue_number(self, patched_app) -> None:
        gh = _FakeClient(issues=[_issue(7)], sub_map={7: []})
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(7, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        assert resp.json()["issue_number"] == 7

    def test_partial_failure_publishes_pr_and_flags_failed_tasks(
        self, patched_app, monkeypatch
    ) -> None:
        """Some tasks merged, one FAILED: still publish the PR, but flag the gap and report
        a partial terminal status rather than a clean completion."""
        api = patched_app["api"]

        def _partial_orchestrator(job_id, _repo_path, _plan, **kw):
            kw["update_job_fn"](
                status="completed_with_failures",
                phase="completed",
                task_graph_snapshot=[
                    {
                        "id": "t1",
                        "status": "merged",
                        "feature_branch": "feature/t1",
                        "merged_at": "2026-05-10T00:00:00Z",
                    },
                    {"id": "t2", "title": "Broken task", "status": "failed"},
                ],
            )

        monkeypatch.setattr(api, "run_coding_team_orchestrator", _partial_orchestrator)
        gh = _FakeClient(issues=[_issue(11, title="Add feature")], sub_map={11: []})
        patched_app["set_github"](gh)

        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            {"owner": "o", "repo": "r", "repo_path": patched_app["repo_path"]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # PR is still created for the merged work, with the failed task flagged in the body.
        assert len(gh.created_pulls) == 1
        assert "Broken task" in gh.created_pulls[0]["body"]
        assert "t2" in gh.created_pulls[0]["body"]
        # A partial result must NOT auto-close the issue — use a non-closing reference.
        assert "Refs #11" in gh.created_pulls[0]["body"]
        assert "Closes #" not in gh.created_pulls[0]["body"]
        # A warning comment about the incomplete tasks was posted.
        assert any("did not complete" in body for _n, body in gh.comments)
        # The job is reported as a partial success, not a clean completion.
        job = patched_app["jobs"].get_job(data["job_id"])
        assert job["status"] == "completed_with_failures"


class TestEndpointFailures:
    def test_no_token_returns_400(self, patched_app, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            {"owner": "o", "repo": "r", "repo_path": patched_app["repo_path"]},
        )
        assert resp.status_code == 400
        assert "GITHUB_TOKEN" in resp.json()["detail"]

    def test_bad_repo_path_returns_400(self, patched_app) -> None:
        resp = patched_app["client"].post(
            "/run-from-github",
            json={"owner": "o", "repo": "r", "repo_path": "/nope/does-not-exist"},
        )
        assert resp.status_code == 400
        assert "repo_path" in resp.json()["detail"]

    def test_no_ready_issue_returns_404(self, patched_app) -> None:
        gh = _FakeClient(
            issues=[_issue(1)],
            sub_map={1: [SubIssue(2, "open", "blocker")]},
        )
        patched_app["set_github"](gh)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            {"owner": "o", "repo": "r", "repo_path": patched_app["repo_path"]},
        )
        assert resp.status_code == 404
        assert gh.created_pulls == []
        assert gh.comments == []

    def test_specific_issue_blocked_returns_409(self, patched_app) -> None:
        gh = _FakeClient(
            issues=[_issue(1)],
            sub_map={1: [SubIssue(2, "open", "blocker")]},
        )
        patched_app["set_github"](gh)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 409
        assert "blocked by sub-issues [2]" in resp.json()["detail"]

    def test_get_repo_failure(self, patched_app) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        gh.fail_get_repo = True
        patched_app["set_github"](gh)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 502
        assert patched_app["started_workflows"] == []
        assert gh.created_pulls == []
        assert gh.comments == []

    def test_orchestrator_raises(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)

        def _boom(*_a, **_kw) -> None:
            raise RuntimeError("orchestrator exploded")

        monkeypatch.setattr(patched_app["api"], "run_coding_team_orchestrator", _boom)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "orchestrator exploded" in job["error"]
        assert gh.created_pulls == []
        # Two comments expected: "started" + failure.
        bodies = [b for _, b in gh.comments]
        assert any("started job" in b for b in bodies)
        assert any("orchestrator exploded" in b for b in bodies)

    def test_no_merged_tasks_marks_failed(self, patched_app, monkeypatch) -> None:
        """Orchestrator returns successfully but with no merged task."""
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)

        def _no_merge(job_id: str, _rp, _plan, **kw):
            kw["update_job_fn"](
                status="completed",
                phase="completed",
                task_graph_snapshot=[{"id": "t1", "status": "to_do", "feature_branch": None}],
            )

        monkeypatch.setattr(patched_app["api"], "run_coding_team_orchestrator", _no_merge)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "no merged tasks" in job["error"]
        assert gh.created_pulls == []
        assert any("produced no merged tasks" in b for _, b in gh.comments)

    def test_only_resolved_without_changes_merge_is_not_publishable(
        self, patched_app, monkeypatch
    ) -> None:
        """A completed_with_failures job whose only MERGED task landed no diff
        (resolved_without_changes), alongside a failed task, has nothing real to publish: the hook
        must report 'no merged tasks' and open NO PR, not push an empty branch / no-op PR."""
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)

        def _no_real_merge(job_id: str, _rp, _plan, **kw):
            kw["update_job_fn"](
                status="completed_with_failures",
                phase="completed",
                task_graph_snapshot=[
                    {"id": "t1", "status": "merged", "resolved_without_changes": True},
                    {"id": "t2", "status": "failed"},
                ],
            )

        monkeypatch.setattr(patched_app["api"], "run_coding_team_orchestrator", _no_real_merge)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "no merged tasks" in job["error"]
        assert gh.created_pulls == []  # no no-op PR for synthetic no-diff merges
        assert any("produced no merged tasks" in b for _, b in gh.comments)

    def test_already_complete_recommends_closure_no_pr(self, patched_app, monkeypatch) -> None:
        """When the orchestrator reports the work already done, the hook comments a closure
        recommendation and opens NO pull request."""
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)

        def _already_done(job_id: str, _rp, _plan, **kw):
            kw["update_job_fn"](
                status="already_complete",
                phase="completed",
                already_complete=True,
                completion_evidence="Sub-issues #12 and #13 already merged.",
                task_graph_snapshot=[],
            )

        monkeypatch.setattr(patched_app["api"], "run_coding_team_orchestrator", _already_done)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "already_complete"
        assert gh.created_pulls == []  # no no-op PR
        bodies = [b for _, b in gh.comments]
        assert any("already complete" in b.lower() and "Recommend closing #1" in b for b in bodies)
        assert any("#12 and #13" in b for b in bodies)

    def test_fast_forward_failure_sets_status_failed(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        monkeypatch.setattr(patched_app["api"], "_fast_forward", lambda *a, **kw: (False, "ff err"))
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        # Regression: previously left status="completed" with an error field.
        assert job["status"] == "failed"
        assert "fast-forward failed" in job["error"]

    def test_push_failure_sets_status_failed(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        monkeypatch.setattr(patched_app["api"], "_push_branch", lambda *a, **kw: (False, "auth"))
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"

    def test_pr_lookup_failure_sets_status_failed(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})

        def _raise_lookup(*_a, **_kw):
            raise GitHubAPIError(500, "lookup boom")

        gh.find_existing_pr = _raise_lookup  # type: ignore[assignment]
        patched_app["set_github"](gh)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "find_existing_pr" in job["error"]

    def test_pr_creation_failure_sets_status_failed(self, patched_app) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})

        def _raise_create(**_kw):
            raise GitHubAPIError(422, "validation")

        gh.create_pull_request = _raise_create  # type: ignore[assignment]
        patched_app["set_github"](gh)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "create_pull_request" in job["error"]

    def test_pr_number_pointing_at_pr_returns_400(self, patched_app) -> None:
        """Operator passed a PR number, not an issue number → 400, not 502."""
        gh = _FakeClient()

        def _raise(_o, _r, _n):
            raise NotAnIssueError(7)

        gh.get_issue = _raise  # type: ignore[assignment]
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(7, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 400
        assert "pull request" in resp.json()["detail"]

    def test_branch_prep_failure(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        monkeypatch.setattr(
            patched_app["api"],
            "_prepare_issue_branch",
            lambda *a, **kw: (False, "no remote", []),
        )
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "branch prep failed" in job["error"]
        assert gh.created_pulls == []


class TestEndpointReuse:
    def test_reuses_existing_pr(self, patched_app) -> None:
        gh = _FakeClient(
            issues=[_issue(1)],
            sub_map={1: []},
            existing_pr=PullRequest(
                number=99,
                html_url="https://example/pr/99",
                head="khala/issue-1",
                base="main",
            ),
        )
        patched_app["set_github"](gh)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        # No new PR created, but job records the existing PR url.
        assert gh.created_pulls == []
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["github_pr_url"] == "https://example/pr/99"
        assert any("Reusing existing draft PR" in c[1] for c in gh.comments)
        # The reused PR's body is refreshed to reflect this (clean) run.
        assert len(gh.updated_pulls) == 1
        assert "Closes #1" in gh.updated_pulls[0]["body"]

    def test_reused_pr_body_updated_on_partial_failure(self, patched_app, monkeypatch) -> None:
        """When a retry reuses an existing PR but some tasks failed, the PR body is rewritten so
        the PR itself surfaces the gap (the warning comment lands on the issue, not the PR)."""
        api = patched_app["api"]

        def _partial_orchestrator(job_id, _repo_path, _plan, **kw):
            kw["update_job_fn"](
                status="completed_with_failures",
                phase="completed",
                task_graph_snapshot=[
                    {
                        "id": "t1",
                        "status": "merged",
                        "feature_branch": "feature/t1",
                        "merged_at": "2026-05-10T00:00:00Z",
                    },
                    {"id": "t2", "title": "Broken task", "status": "failed"},
                ],
            )

        monkeypatch.setattr(api, "run_coding_team_orchestrator", _partial_orchestrator)
        gh = _FakeClient(
            issues=[_issue(11, title="Add feature")],
            sub_map={11: []},
            existing_pr=PullRequest(
                number=99,
                html_url="https://example/pr/99",
                head="khala/issue-11",
                base="main",
            ),
        )
        patched_app["set_github"](gh)

        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            {"owner": "o", "repo": "r", "repo_path": patched_app["repo_path"]},
        )
        assert resp.status_code == 200, resp.text

        # No new PR; the reused PR's body was patched to flag the failed task.
        assert gh.created_pulls == []
        assert len(gh.updated_pulls) == 1
        assert gh.updated_pulls[0]["number"] == 99
        assert "Broken task" in gh.updated_pulls[0]["body"]
        assert "t2" in gh.updated_pulls[0]["body"]
        assert "Refs #11" in gh.updated_pulls[0]["body"]

    def test_reused_pr_body_refreshed_on_clean_retry(self, patched_app, monkeypatch) -> None:
        """A later retry that completes every task must refresh the reused PR body so a stale
        partial-failure warning from an earlier run is cleared (and the issue auto-closes again)."""
        api = patched_app["api"]

        def _partial_orchestrator(job_id, _repo_path, _plan, **kw):
            kw["update_job_fn"](
                status="completed_with_failures",
                phase="completed",
                task_graph_snapshot=[
                    {
                        "id": "t1",
                        "status": "merged",
                        "feature_branch": "feature/t1",
                        "merged_at": "2026-05-10T00:00:00Z",
                    },
                    {"id": "t2", "title": "Broken task", "status": "failed"},
                ],
            )

        def _clean_orchestrator(job_id, _repo_path, _plan, **kw):
            kw["update_job_fn"](
                status="completed",
                phase="completed",
                task_graph_snapshot=[
                    {
                        "id": "t1",
                        "status": "merged",
                        "feature_branch": "feature/t1",
                        "merged_at": "2026-05-10T00:00:00Z",
                    }
                ],
            )

        gh = _FakeClient(
            issues=[_issue(1)],
            sub_map={1: []},
            existing_pr=PullRequest(
                number=99,
                html_url="https://example/pr/99",
                head="khala/issue-1",
                base="main",
            ),
        )
        patched_app["set_github"](gh)

        # First (failing) run: seed a stale partial-failure warning on the PR body.
        monkeypatch.setattr(api, "run_coding_team_orchestrator", _partial_orchestrator)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200, resp.text
        assert gh.created_pulls == []
        assert len(gh.updated_pulls) == 1
        assert "did not complete" in gh.updated_pulls[0]["body"]
        assert "Broken task" in gh.updated_pulls[0]["body"]

        # Retry with a clean (all-merged) orchestrator.
        monkeypatch.setattr(api, "run_coding_team_orchestrator", _clean_orchestrator)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200, resp.text
        assert gh.created_pulls == []
        assert len(gh.updated_pulls) == 2
        assert gh.updated_pulls[-1]["number"] == 99
        # Body reflects the clean retry: closing reference, stale failure warning cleared.
        assert "Closes #1" in gh.updated_pulls[-1]["body"]
        assert "did not complete" not in gh.updated_pulls[-1]["body"]


class TestEndpointDuplicateGuard:
    def test_rejects_concurrent_run_for_same_issue(self, patched_app) -> None:
        # Seed a running job tagged with the same issue.
        patched_app["jobs"].create_job(
            "running-job",
            status="running",
            github_context={
                "owner": "o",
                "repo": "r",
                "issue_number": 5,
                "issue_url": "x",
            },
        )
        gh = _FakeClient(issues=[_issue(5)], sub_map={5: []})
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(5, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"]

    def test_terminal_job_for_same_issue_does_not_block(self, patched_app) -> None:
        """A previously-failed job must not block a retry on the same issue."""
        patched_app["jobs"].create_job(
            "old-failed-job",
            status="failed",
            github_context={
                "owner": "o",
                "repo": "r",
                "issue_number": 5,
                "issue_url": "x",
            },
            error="prior push failed",
        )
        gh = _FakeClient(issues=[_issue(5)], sub_map={5: []})
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(5, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["job_id"] != "old-failed-job"


class TestTruncateTitle:
    def test_unicode_long_title_caps_at_256(self, patched_app) -> None:
        api = patched_app["api"]
        title = "✦" * 300
        out = api._truncate_title(title, 42)
        assert len(out) == 256
        assert out.endswith(" (closes #42)")

    def test_short_title_unchanged(self, patched_app) -> None:
        api = patched_app["api"]
        out = api._truncate_title("Add login", 7)
        assert out == "Add login (closes #7)"

    def test_strips_trailing_whitespace_in_head(self, patched_app) -> None:
        api = patched_app["api"]
        # Title that hits the boundary exactly with a trailing space.
        out = api._truncate_title("a " * 130, 7)  # 260 chars, trims to fit
        assert " (closes #7)" in out
        assert "  (closes" not in out  # no double-space at the boundary

    def test_empty_title_falls_back_to_issue_number(self, patched_app) -> None:
        api = patched_app["api"]
        # No leading-space-only PR title; we substitute a placeholder instead.
        assert api._truncate_title("", 42) == "Issue #42 (closes #42)"


class TestPrepareIssueBranch:
    """Exercise _prepare_issue_branch against a real on-disk git repo.

    These tests deliberately avoid the ``patched_app`` fixture because that
    fixture monkey-patches the git helpers to no-op stubs for the endpoint
    tests; we want the real implementations here.
    """

    @staticmethod
    def _git(repo: str, *args: str) -> None:
        import subprocess

        subprocess.run(
            ["git", "-C", repo, *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _init_repo(self, path) -> str:
        repo = str(path / "repo")
        import os

        os.makedirs(repo, exist_ok=True)
        self._git(repo, "init", "-q")
        # Disable commit signing in case the host environment forces it.
        self._git(repo, "config", "commit.gpgsign", "false")
        self._git(repo, "config", "tag.gpgsign", "false")
        self._git(repo, "config", "user.email", "test@example.com")
        self._git(repo, "config", "user.name", "test")
        # Force the branch to "main" regardless of the host's init.defaultBranch
        # (older git defaults to "master"; newer installs may already be "main",
        # in which case a plain "-b main" fails with "branch already exists").
        self._git(repo, "checkout", "-q", "-B", "main")
        with open(f"{repo}/README.md", "w") as fh:
            fh.write("seed\n")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-q", "--no-gpg-sign", "-m", "seed")
        # Self-alias as origin so fetch works without a real remote.
        self._git(repo, "remote", "add", "origin", repo)
        return repo

    @pytest.fixture
    def api(self, monkeypatch):
        """Import the api module fresh, without the patched_app fixture's stubs."""
        _stub_heavy_modules(monkeypatch)
        from software_engineering_team.api import coding_team_main as api_main

        return api_main

    def test_dirty_tree_recovered_to_rescue_branch(self, api, tmp_path) -> None:
        """Uncommitted unattributed changes are preserved on a rescue branch, then prep proceeds."""
        import re
        import subprocess

        repo = self._init_repo(tmp_path)
        with open(f"{repo}/README.md", "a") as fh:
            fh.write("dirty\n")

        ok, msg, notes = api._prepare_issue_branch(repo, "origin", "main", "khala/issue-9")
        assert ok is True, msg
        rescue_note = next((n for n in notes if "khala/rescue/" in n), None)
        assert rescue_note is not None, notes
        rescue_branch = re.search(r"`(khala/rescue/[^`]+)`", rescue_note).group(1)

        status = subprocess.run(
            ["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
        assert status == ""

        # The dirty change was actually committed onto the rescue branch, not dropped.
        rescued_contents = subprocess.run(
            ["git", "-C", repo, "show", f"{rescue_branch}:README.md"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "dirty" in rescued_contents

    def test_clean_tree_succeeds(self, api, tmp_path) -> None:
        repo = self._init_repo(tmp_path)
        self._git(repo, "fetch", "origin", "main")
        ok, msg, _notes = api._prepare_issue_branch(repo, "origin", "main", "khala/issue-9")
        assert ok is True, msg
        import subprocess

        head = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == "khala/issue-9"

    def test_unsafe_default_branch_rejected(self, api, tmp_path) -> None:
        repo = self._init_repo(tmp_path)
        ok, msg, _notes = api._prepare_issue_branch(repo, "origin", "--exec=evil", "khala/issue-9")
        assert ok is False
        assert "unsafe" in (msg or "")

    def test_unsafe_integration_branch_rejected(self, api, tmp_path) -> None:
        repo = self._init_repo(tmp_path)
        ok, msg, _notes = api._prepare_issue_branch(repo, "origin", "main", "-evil-name")
        assert ok is False
        assert "unsafe" in (msg or "")


class TestGitCredentialThreading:
    """The token must reach the network git ops (fetch/push) transiently.

    The unified API clones with a credential that is never persisted to
    ``.git/config``; the coding-team service runs later on the same shared
    checkout, so it must re-supply the token on every fetch/push or a default
    checkout has no auth — private repos (and the final push for public repos)
    would otherwise fail or hang until the git timeout after the job started.
    """

    @pytest.fixture
    def api(self, monkeypatch):
        """Import the api module fresh, without the patched_app fixture's stubs."""
        _stub_heavy_modules(monkeypatch)
        from software_engineering_team.api import coding_team_main as api_main

        return api_main

    def test_git_auth_env_injects_transient_basic_header(self, api) -> None:
        env = api._git_auth_env("secret-tok")
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        # GitHub's git smart-HTTP endpoint rejects `Bearer` (401) even for a
        # valid token — only Basic with the x-access-token username works.
        assert env["GIT_CONFIG_VALUE_0"] == _expected_basic_header("secret-tok")
        # Disable interactive prompts so a bad credential fails fast.
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        # Inherits the parent environment (PATH etc. survive).
        assert "PATH" in env

    def test_prepare_issue_branch_passes_auth_env_to_fetch(self, api, monkeypatch) -> None:
        calls = []

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            calls.append((args, env))
            return 0, ""

        monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (True, False, None))
        monkeypatch.setattr(api, "_git", fake_git)
        ok, msg, _notes = api._prepare_issue_branch(
            "/repo", "origin", "main", "khala/issue-1", "tok-123"
        )
        assert ok is True, msg
        # Both fetches (base branch + issue-branch continuation candidate)
        # must carry the auth env.
        fetches = [(args, env) for args, env in calls if args[0] == "fetch"]
        assert len(fetches) == 2
        for _args, env in fetches:
            assert env is not None
            assert env["GIT_CONFIG_VALUE_0"] == _expected_basic_header("tok-123")
        # Local-only git ops never carry the credential.
        assert all(env is None for args, env in calls if args[0] != "fetch")

    def test_prepare_issue_branch_without_token_uses_no_auth_env(self, api, monkeypatch) -> None:
        calls = []

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            calls.append((args, env))
            return 0, ""

        monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (True, False, None))
        monkeypatch.setattr(api, "_git", fake_git)
        ok, _msg, _notes = api._prepare_issue_branch("/repo", "origin", "main", "khala/issue-1")
        assert ok is True
        assert all(env is None for _, env in calls)

    def test_push_branch_passes_auth_env(self, api, monkeypatch) -> None:
        captured = {}

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            captured["args"] = args
            captured["env"] = env
            return 0, ""

        monkeypatch.setattr(api, "_git", fake_git)
        ok, msg = api._push_branch("/repo", "origin", "khala/issue-1", "tok-xyz")
        assert ok is True, msg
        assert captured["args"][0] == "push"
        assert captured["env"]["GIT_CONFIG_VALUE_0"] == _expected_basic_header("tok-xyz")

    def test_push_branch_without_token_uses_no_auth_env(self, api, monkeypatch) -> None:
        captured = {}

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            captured["env"] = env
            return 0, ""

        monkeypatch.setattr(api, "_git", fake_git)
        ok, _ = api._push_branch("/repo", "origin", "khala/issue-1")
        assert ok is True
        assert captured["env"] is None

    def test_push_branch_rejects_unsafe_branch_before_running_git(self, api, monkeypatch) -> None:
        called = {"ran": False}

        def fake_git(*a, **kw):
            called["ran"] = True
            return 0, ""

        monkeypatch.setattr(api, "_git", fake_git)
        ok, msg = api._push_branch("/repo", "origin", "-evil", "tok")
        assert ok is False
        assert "unsafe" in (msg or "")
        assert called["ran"] is False


class TestActiveIssueMarkerLifecycle:
    """The marker means "this checkout holds unpublished work for issue N":
    it is cleared only once the work is published (PR recorded). Every
    unpublished terminal path — orchestrator exception, no merged tasks,
    fast-forward/push/PR failure — must retain it so a retry continues from
    `development` instead of rescuing the finished work and starting over."""

    def _run(self, patched_app, monkeypatch, github_client, orchestrator=None):
        api = patched_app["api"]
        cleared: list[str] = []
        monkeypatch.setattr(api, "_clear_active_issue_if_matches", lambda p, _n: cleared.append(p))
        if orchestrator is not None:
            monkeypatch.setattr(api, "run_coding_team_orchestrator", orchestrator)
        patched_app["set_github"](github_client)
        resp = _post_run_from_github_and_run_hooks(
            patched_app, _body(3, repo_path=patched_app["repo_path"])
        )
        assert resp.status_code == 200
        return cleared

    def test_cleared_on_publish_success(self, patched_app, monkeypatch) -> None:
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client)
        assert cleared == [patched_app["repo_path"]]

    def test_retained_when_orchestrator_raises(self, patched_app, monkeypatch) -> None:
        def boom(*_a, **_kw):
            raise RuntimeError("orchestrator died")

        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client, orchestrator=boom)
        assert cleared == []

    def test_retained_when_no_merged_tasks(self, patched_app, monkeypatch) -> None:
        def no_merge(_job_id, _repo, _plan, **kw):
            kw["update_job_fn"](status="completed", task_graph_snapshot=[])

        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client, orchestrator=no_merge)
        assert cleared == []

    def test_cleared_on_already_complete(self, patched_app, monkeypatch) -> None:
        # An already-complete run is a clean no-op success: it must clear the marker like the normal
        # publish-success path, not leave it behind (a later retry would treat stale local state as
        # interrupted progress).
        def already_done(_job_id, _repo, _plan, **kw):
            kw["update_job_fn"](
                status="already_complete",
                already_complete=True,
                completion_evidence="already merged",
                task_graph_snapshot=[],
            )

        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client, orchestrator=already_done)
        assert cleared == [patched_app["repo_path"]]

    def test_already_complete_removes_ephemeral_checkout_when_flagged(
        self, patched_app, monkeypatch
    ) -> None:
        # The per-issue clone must also be removed on an already-complete run when
        # cleanup_checkout_on_success is set — otherwise the clone leaks just like on the normal
        # success path.
        api = patched_app["api"]
        removed: list[str] = []
        monkeypatch.setattr(api, "_clear_active_issue_if_matches", lambda p, _n: None)
        monkeypatch.setattr(api, "_cleanup_issue_checkout", lambda p: removed.append(p))

        def already_done(_job_id, _repo, _plan, **kw):
            kw["update_job_fn"](
                status="already_complete",
                already_complete=True,
                completion_evidence="already merged",
                task_graph_snapshot=[],
            )

        monkeypatch.setattr(api, "run_coding_team_orchestrator", already_done)
        patched_app["set_github"](_FakeClient(issues=[_issue(3)], sub_map={3: []}))
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(3, repo_path=patched_app["repo_path"], cleanup_checkout_on_success=True),
        )
        assert resp.status_code == 200
        assert removed == [patched_app["repo_path"]]

    def test_retained_when_push_fails(self, patched_app, monkeypatch) -> None:
        api = patched_app["api"]
        monkeypatch.setattr(api, "_push_branch", lambda *a, **kw: (False, "remote hung up"))
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client)
        assert cleared == []

    def test_retained_when_fast_forward_fails(self, patched_app, monkeypatch) -> None:
        api = patched_app["api"]
        monkeypatch.setattr(api, "_fast_forward", lambda *a, **kw: (False, "not possible"))
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client)
        assert cleared == []

    def test_retained_when_pr_creation_fails(self, patched_app, monkeypatch) -> None:
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})

        def _raise_create(**_kw):
            raise GitHubAPIError(422, "validation")

        client.create_pull_request = _raise_create  # type: ignore[assignment]
        cleared = self._run(patched_app, monkeypatch, client)
        assert cleared == []

    def test_prep_notes_posted_as_issue_comments(self, patched_app, monkeypatch) -> None:
        api = patched_app["api"]
        monkeypatch.setattr(api, "_clear_active_issue_if_matches", lambda p, _n: None)
        monkeypatch.setattr(
            api,
            "_prepare_issue_branch",
            lambda *a, **kw: (True, None, ["♻️ recovered", "▶️ continuing"]),
        )
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        patched_app["set_github"](client)
        resp = _post_run_from_github_and_run_hooks(
            patched_app, _body(3, repo_path=patched_app["repo_path"])
        )
        assert resp.status_code == 200
        bodies = [body for _n, body in client.comments]
        assert "♻️ recovered" in bodies
        assert "▶️ continuing" in bodies

    def test_prep_receives_issue_number(self, patched_app, monkeypatch) -> None:
        api = patched_app["api"]
        seen: dict = {}

        def fake_prep(*args, **kwargs):
            seen["issue_number"] = kwargs.get("issue_number")
            return True, None, []

        monkeypatch.setattr(api, "_clear_active_issue_if_matches", lambda p, _n: None)
        monkeypatch.setattr(api, "_prepare_issue_branch", fake_prep)
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        patched_app["set_github"](client)
        resp = _post_run_from_github_and_run_hooks(
            patched_app, _body(3, repo_path=patched_app["repo_path"])
        )
        assert resp.status_code == 200
        assert seen["issue_number"] == 3


class TestStatusResponseSurfacing:
    def test_status_returns_github_fields(self, patched_app) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        post = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(1, repo_path=patched_app["repo_path"]),
        )
        job_id = post.json()["job_id"]
        status = patched_app["client"].get(f"/status/{job_id}")
        body = status.json()
        assert body["github_context"]["issue_number"] == 1
        assert body["github_pr_url"] == "https://example/pr/42"

    def test_github_context_persists_cleanup_flag(self, patched_app) -> None:
        # The cleanup decision is persisted in github_context so a later resume
        # reproduces it; without this a resumed job would default to no-cleanup
        # and leak its ephemeral per-issue checkout.
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        post = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"], cleanup_checkout_on_success=True),
        )
        assert post.status_code == 200
        status = patched_app["client"].get(f"/status/{post.json()['job_id']}")
        assert status.json()["github_context"]["cleanup_checkout_on_success"] is True


class TestBusyCheckoutGuard:
    """Auto-recovery must never mutate a sibling job's live working tree:
    prep on a checkout with another non-terminal job fails fast (the old
    dirty-guard behavior for the live case), while crashed-job leftovers
    (no running sibling) still recover."""

    def test_running_sibling_on_same_checkout_fails_job(self, patched_app, monkeypatch) -> None:
        from software_engineering_team.job_store import create_job, update_job

        api = patched_app["api"]
        repo_path = patched_app["repo_path"]
        create_job(job_id="sibling-1", repo_path=repo_path, plan_input=None)
        update_job(
            "sibling-1",
            status="running",
            github_context={"owner": "o", "repo": "r", "issue_number": 99},
        )
        prep_calls: list = []
        monkeypatch.setattr(
            api, "_prepare_issue_branch", lambda *a, **kw: prep_calls.append(a) or (True, None, [])
        )
        monkeypatch.setattr(api, "_clear_active_issue_if_matches", lambda *a: None)
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        patched_app["set_github"](client)
        resp = _post_run_from_github_and_run_hooks(
            patched_app, _body(3, repo_path=repo_path)
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "busy" in (job["error"] or "").lower()
        assert prep_calls == []  # the sibling's tree was never touched

    def test_sibling_under_alias_spelling_still_blocks(self, patched_app, monkeypatch) -> None:
        """The guard compares canonical paths: a sibling registered under a
        different spelling of the same checkout (symlink, `/.` suffix) must
        still block — string equality would fail open exactly where the
        guard matters."""
        from software_engineering_team.job_store import create_job, update_job

        api = patched_app["api"]
        repo_path = patched_app["repo_path"]
        alias = os.path.join(os.path.dirname(repo_path), "repo-alias")
        os.symlink(repo_path, alias)
        create_job(job_id="sibling-3", repo_path=os.path.join(alias, "."), plan_input=None)
        update_job(
            "sibling-3",
            status="running",
            github_context={"owner": "o", "repo": "r", "issue_number": 99},
        )
        prep_calls: list = []
        monkeypatch.setattr(
            api, "_prepare_issue_branch", lambda *a, **kw: prep_calls.append(a) or (True, None, [])
        )
        monkeypatch.setattr(api, "_clear_active_issue_if_matches", lambda *a: None)
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        patched_app["set_github"](client)
        resp = _post_run_from_github_and_run_hooks(
            patched_app, _body(3, repo_path=repo_path)
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "busy" in (job["error"] or "").lower()
        assert prep_calls == []

    def test_terminal_sibling_does_not_block(self, patched_app, monkeypatch) -> None:
        from software_engineering_team.job_store import create_job, update_job

        repo_path = patched_app["repo_path"]
        create_job(job_id="sibling-2", repo_path=repo_path, plan_input=None)
        update_job("sibling-2", status="completed")
        api = patched_app["api"]
        monkeypatch.setattr(api, "_clear_active_issue_if_matches", lambda *a: None)
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        patched_app["set_github"](client)
        resp = _post_run_from_github_and_run_hooks(
            patched_app, _body(3, repo_path=repo_path)
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"


class TestPublishWindowLiveness:
    """A job must not report a terminal status while the hook is still
    mutating the checkout (fast-forward, push, PR creation, marker clear):
    the busy-checkout guard keys liveness off pending/running, so a terminal
    status during the publish window would let a sibling job reset the shared
    checkout mid-publish."""

    def test_job_stays_running_during_publish_window(self, patched_app, monkeypatch) -> None:
        from software_engineering_team.job_store import list_jobs as store_list_jobs

        api = patched_app["api"]
        repo_path = patched_app["repo_path"]
        seen: dict = {}

        def spy_push(repo, remote, branch, token=None):
            jobs = store_list_jobs()
            assert len(jobs) == 1
            seen["status_at_push"] = jobs[0]["status"]
            seen["phase_at_push"] = jobs[0].get("phase")
            return True, None

        monkeypatch.setattr(api, "_push_branch", spy_push)
        monkeypatch.setattr(api, "_clear_active_issue_if_matches", lambda *a: None)
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        patched_app["set_github"](client)
        resp = _post_run_from_github_and_run_hooks(
            patched_app, _body(3, repo_path=repo_path)
        )
        assert resp.status_code == 200
        # The orchestrator declared success before the push, but the job must
        # still be non-terminal (and visible to the busy-checkout guard)…
        assert seen["status_at_push"] == "running"
        assert seen["phase_at_push"] == "publishing"
        # …and only goes terminal once the hook is fully done.
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job.get("phase") == "completed"


class TestEphemeralCheckoutCleanup:
    """The per-issue clone is deleted only on a clean completion when the
    caller flagged the checkout as platform-owned and ephemeral."""

    def test_request_default_cleanup_flag_is_false(self) -> None:
        """RunFromGitHubRequest defaults cleanup_checkout_on_success to False."""
        from software_engineering_team.api.coding_team_main import RunFromGitHubRequest

        req = RunFromGitHubRequest(owner="o", repo="r", repo_path="/tmp/x")
        assert req.cleanup_checkout_on_success is False

    def test_cleanup_helper_removes_directory(self, patched_app, tmp_path, monkeypatch) -> None:
        """A real per-issue checkout under an ephemeral root is removed."""
        api = patched_app["api"]
        # tmp_path is an ephemeral workspace root for this test, so checkouts
        # under it are eligible for removal. Clear the other root vars so an
        # ambient env can't add unexpected ephemeral roots.
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("AGENT_CACHE", raising=False)
        target = tmp_path / "issue-7"  # the auto-derived per-issue shape
        target.mkdir()
        (target / ".git").mkdir()  # only real checkouts are removed
        (target / "file.txt").write_text("x", encoding="utf-8")
        api._cleanup_issue_checkout(str(target))
        assert not target.exists()

    def test_cleanup_refuses_repo_level_path_under_root(
        self, patched_app, tmp_path, monkeypatch
    ) -> None:
        """A repo-level checkout (no issue-N component) under a root is never removed."""
        # A repo-level checkout (no ``issue-N`` final component) that merely sits
        # under an ephemeral root must NOT be removed even with .git and the flag —
        # only per-issue clones are reclaimable (the PR-review path lives here).
        api = patched_app["api"]
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("AGENT_CACHE", raising=False)
        repo_level = tmp_path / "acme_widget"  # no issue-N segment
        repo_level.mkdir()
        (repo_level / ".git").mkdir()
        assert api._is_ephemeral_checkout_path(str(repo_level)) is False
        api._cleanup_issue_checkout(str(repo_level))
        assert repo_level.exists()

    def test_cleanup_helper_does_not_raise_on_missing_dir(self, patched_app, tmp_path) -> None:
        """A missing directory is refused by the guard and cleanup does not raise."""
        api = patched_app["api"]
        # A missing dir is refused by the safety guard (no .git); must not raise.
        api._cleanup_issue_checkout(str(tmp_path / "does-not-exist"))

    def test_cleanup_helper_refuses_non_checkout_path(
        self, patched_app, tmp_path, monkeypatch
    ) -> None:
        """A directory without a .git entry is not a checkout and is left untouched."""
        api = patched_app["api"]
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        # A directory under the root but without .git is not a checkout → untouched.
        target = tmp_path / "not-a-checkout"
        target.mkdir()
        (target / "file.txt").write_text("x", encoding="utf-8")
        api._cleanup_issue_checkout(str(target))
        assert target.exists()

    def test_cleanup_helper_refuses_path_outside_ephemeral_root(
        self, patched_app, tmp_path, monkeypatch
    ) -> None:
        """A real checkout outside every ephemeral root is never removed, even with the flag."""
        api = patched_app["api"]
        # Configure an ephemeral root that does NOT contain the target: even a
        # real git checkout with the cleanup flag must not be removed.
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ephemeral_root"))
        monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
        monkeypatch.setenv("AGENT_CACHE", str(tmp_path / "cache"))
        outside = tmp_path / "someones" / "real-repo"
        outside.mkdir(parents=True)
        (outside / ".git").mkdir()
        assert api._is_ephemeral_checkout_path(str(outside)) is False
        api._cleanup_issue_checkout(str(outside))
        assert outside.exists()

    def test_is_ephemeral_checkout_path_refuses_shallow_path(self, patched_app) -> None:
        """A filesystem root / shallow system path is never eligible for removal."""
        api = patched_app["api"]
        # A filesystem root / shallow system path must never be removed even if it exists.
        assert api._is_ephemeral_checkout_path("/") is False
        api._cleanup_issue_checkout("/")  # must not raise and must not attempt removal

    def test_is_ephemeral_checkout_path_handles_resolve_error(self, patched_app) -> None:
        """An unresolvable path (embedded null byte) is treated as unsafe, not removed."""
        api = patched_app["api"]
        # An embedded null byte makes Path.resolve() raise ValueError → treated as unsafe.
        assert api._is_ephemeral_checkout_path("bad\x00path") is False

    def test_cleanup_helper_swallows_rmtree_failure_and_keeps_lock(
        self, patched_app, tmp_path, monkeypatch
    ) -> None:
        """An rmtree failure is swallowed; the checkout and the lock are both retained."""
        api = patched_app["api"]
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        target = tmp_path / "issue-7"
        target.mkdir()
        (target / ".git").mkdir()
        lock = tmp_path / ".issue-7.clone.lock"
        lock.write_text("", encoding="utf-8")

        def _boom(*_a, **_kw):
            raise OSError("permission denied")

        monkeypatch.setattr(api.shutil, "rmtree", _boom)
        # Must not raise; and because rmtree failed, the checkout is still present
        # and the lock file is retained so it keeps guarding it.
        api._cleanup_issue_checkout(str(target))
        assert target.exists()
        assert lock.exists()

    def test_cleanup_retains_lock_file_for_reuse(self, patched_app, tmp_path, monkeypatch) -> None:
        """Cleanup removes the checkout but retains the clone lock for reuse."""
        # The clone lock is held around rmtree and deliberately NOT unlinked: it is
        # the stable per-issue lock both clone and cleanup share, so unlinking a
        # flock'd file can't orphan its inode and let two runs hold "the" lock.
        api = patched_app["api"]
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        target = tmp_path / "issue-7"
        target.mkdir()
        (target / ".git").mkdir()
        lock = tmp_path / ".issue-7.clone.lock"
        api._cleanup_issue_checkout(str(target))
        assert not target.exists()  # checkout removed
        assert lock.exists()  # lock retained for reuse

    def test_cleanup_skipped_when_lock_cannot_be_opened(
        self, patched_app, tmp_path, monkeypatch
    ) -> None:
        """If the clone lock cannot be opened, cleanup skips deletion and does not raise."""
        # If the clone lock can't be opened, cleanup must skip (not delete
        # unsynchronised) and never raise.
        api = patched_app["api"]
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        target = tmp_path / "issue-7"
        target.mkdir()
        (target / ".git").mkdir()

        real_open = open

        def _open_boom(path, *a, **k):
            if str(path).endswith(".clone.lock"):
                raise OSError("cannot open lock")
            return real_open(path, *a, **k)

        monkeypatch.setattr("builtins.open", _open_boom)
        api._cleanup_issue_checkout(str(target))  # must not raise
        assert target.exists()  # not deleted without the lock

    def test_cleanup_skipped_when_flock_fails(self, patched_app, tmp_path, monkeypatch) -> None:
        """If flock fails (e.g. ENOLCK), cleanup skips deletion and never raises."""
        api = patched_app["api"]
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        target = tmp_path / "issue-7"
        target.mkdir()
        (target / ".git").mkdir()

        def _flock_boom(fd, op):
            raise OSError("ENOLCK")

        monkeypatch.setattr(api.fcntl, "flock", _flock_boom)
        api._cleanup_issue_checkout(str(target))  # must not raise
        assert target.exists()  # not deleted because the lock was never held

    def test_cleanup_deletes_resolved_path_not_raw_symlinked_string(
        self, patched_app, tmp_path, monkeypatch
    ) -> None:
        """Cleanup deletes the resolved canonical path, not the raw symlinked request string."""
        # TOCTOU hardening: the deletion must target the resolved, symlink-collapsed
        # path the safety check validated, not the raw request string. Here the raw
        # path reaches the checkout through a symlinked parent; cleanup must delete
        # the canonical location and pass that resolved path to rmtree.
        api = patched_app["api"]
        real = tmp_path / "real"
        (real / "issue-7" / ".git").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setenv("WORKSPACE_ROOT", str(real))
        monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
        monkeypatch.setenv("AGENT_CACHE", str(tmp_path / "cache"))

        captured: dict = {}
        real_rmtree = api.shutil.rmtree

        def _capture(path, *a, **k):
            captured["path"] = path
            return real_rmtree(path, *a, **k)

        monkeypatch.setattr(api.shutil, "rmtree", _capture)
        api._cleanup_issue_checkout(str(link / "issue-7"))  # raw path via the symlink
        assert captured["path"] == (real / "issue-7").resolve()
        assert not (real / "issue-7").exists()

    def test_cleanup_refuses_symlinked_checkout_root(
        self, patched_app, tmp_path, monkeypatch
    ) -> None:
        """If the checkout root itself is a symlink to a sibling issue-N checkout
        (a job swapping its own dir), cleanup refuses and must not follow the link
        to delete the sibling."""
        api = patched_app["api"]
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("AGENT_CACHE", raising=False)
        sibling = tmp_path / "issue-8"
        (sibling / ".git").mkdir(parents=True)
        link = tmp_path / "issue-7"
        link.symlink_to(sibling, target_is_directory=True)  # issue-7 → issue-8
        assert api._is_ephemeral_checkout_path(str(link)) is False
        api._cleanup_issue_checkout(str(link))
        assert sibling.exists()  # the sibling checkout is untouched

    def test_cleanup_symlink_swap_during_lock_spares_other_checkout(
        self, patched_app, tmp_path, monkeypatch
    ) -> None:
        """A symlink swapped between the initial resolve and lock acquisition can't
        redirect the delete: cleanup removes the originally-resolved checkout and
        leaves the swapped-in checkout untouched."""
        api = patched_app["api"]
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("AGENT_CACHE", raising=False)
        a = tmp_path / "real_a" / "issue-7"
        (a / ".git").mkdir(parents=True)
        b = tmp_path / "real_b" / "issue-7"
        (b / ".git").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "real_a", target_is_directory=True)  # initially → A

        state = {"swapped": False}
        orig_flock = api.fcntl.flock

        def _swap_then_lock(fd, op):
            # Swap the symlink to B's parent exactly once, at lock acquisition,
            # i.e. between the initial resolve (which captured A) and the delete.
            if not state["swapped"]:
                link.unlink()
                link.symlink_to(tmp_path / "real_b", target_is_directory=True)
                state["swapped"] = True
            return orig_flock(fd, op)

        monkeypatch.setattr(api.fcntl, "flock", _swap_then_lock)
        api._cleanup_issue_checkout(str(link / "issue-7"))  # resolves to A first
        assert not a.exists()  # the originally-resolved checkout is removed
        assert b.exists()  # the swapped-in checkout is spared

    def test_clean_success_with_flag_deletes_checkout(self, patched_app, monkeypatch) -> None:
        """On clean completion with the flag set, the per-issue checkout is deleted."""
        # Per-issue checkout (``issue-N``) directly under an ephemeral root, so it
        # is eligible for cleanup; clear the other root vars so the env can't add
        # extra roots.
        root = Path(patched_app["repo_path"])
        checkout = root / "issue-11"
        (checkout / ".git").mkdir(parents=True)  # a real checkout
        monkeypatch.setenv("WORKSPACE_ROOT", str(root))
        monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("AGENT_CACHE", raising=False)
        gh = _FakeClient(issues=[_issue(11)], sub_map={11: []})
        patched_app["set_github"](gh)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(11, repo_path=str(checkout), cleanup_checkout_on_success=True),
        )
        assert resp.status_code == 200, resp.text
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert not checkout.is_dir()

    def test_clean_success_without_flag_keeps_checkout(self, patched_app) -> None:
        """Without the flag, an operator-managed checkout is preserved on clean completion."""
        repo_path = patched_app["repo_path"]
        gh = _FakeClient(issues=[_issue(11)], sub_map={11: []})
        patched_app["set_github"](gh)
        # No flag → default False → operator-managed checkout is preserved.
        resp = patched_app["client"].post("/run-from-github", json=_body(11, repo_path=repo_path))
        assert resp.status_code == 200, resp.text
        assert os.path.isdir(repo_path)

    def test_partial_failure_keeps_checkout_even_with_flag(self, patched_app, monkeypatch) -> None:
        """On partial failure the checkout is kept even with the cleanup flag set."""
        api = patched_app["api"]
        # Per-issue checkout under an ephemeral root (same eligible setup as the
        # clean-success test) so the cleanup decision turns solely on job status:
        # were partial failure NOT to keep it, it WOULD be deleted here. Without
        # this the safety guard refuses deletion regardless of status and the test
        # would pass for the wrong reason.
        root = Path(patched_app["repo_path"])
        checkout = root / "issue-11"
        (checkout / ".git").mkdir(parents=True)  # a real checkout
        monkeypatch.setenv("WORKSPACE_ROOT", str(root))
        monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("AGENT_CACHE", raising=False)

        def _partial_orchestrator(job_id, _repo_path, _plan, **kw):
            kw["update_job_fn"](
                status="running",
                phase="coding",
                task_graph_snapshot=[
                    {"id": "t1", "status": "merged", "feature_branch": "feature/t1"},
                    {"id": "t2", "status": "failed", "title": "Broken task"},
                ],
            )

        monkeypatch.setattr(api, "run_coding_team_orchestrator", _partial_orchestrator)
        gh = _FakeClient(issues=[_issue(11)], sub_map={11: []})
        patched_app["set_github"](gh)
        resp = _post_run_from_github_and_run_hooks(
            patched_app,
            _body(11, repo_path=str(checkout), cleanup_checkout_on_success=True),
        )
        assert resp.status_code == 200, resp.text
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed_with_failures"
        # Ephemeral + cleanup flag set, yet kept → the partial-failure status
        # overrode cleanup (a retry can seed from the preserved checkout).
        assert checkout.is_dir()


# ---------------------------------------------------------------------------
# get_file_contents / get_repository_tree + GitHubRepoReader
# ---------------------------------------------------------------------------


class TestFileContentsAndTree:
    """Verify GitHubClient content/tree helpers handle files, directories, and errors."""

    def test_get_file_contents_decodes_base64(self) -> None:
        """A base64-encoded file response is decoded and returned as text."""
        body = "class Model:\n    pass\n"
        encoded = base64.b64encode(body.encode()).decode()

        def handler(req: httpx.Request) -> httpx.Response:
            assert "/contents/pkg/models.py" in req.url.path
            assert req.url.params.get("ref") == "sha1"
            return httpx.Response(
                200, json={"type": "file", "encoding": "base64", "content": encoded}
            )

        assert _client_with(handler).get_file_contents("o", "r", "pkg/models.py", "sha1") == body

    def test_get_file_contents_404_returns_none(self) -> None:
        """A missing file (404) is reported as None rather than raising."""

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        assert _client_with(handler).get_file_contents("o", "r", "missing.py", "sha1") is None

    def test_get_file_contents_directory_returns_none(self) -> None:
        """A directory path (JSON array response) is reported as None, not a file body."""

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"type": "file", "name": "a.py"}])

        # A directory listing is a JSON array (not a file dict) -> None.
        assert _client_with(handler).get_file_contents("o", "r", "pkg", "sha1") is None

    def test_get_file_contents_non_404_error_raises(self) -> None:
        """A non-404 error response (e.g. 403) propagates as GitHubAPIError."""

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Forbidden"})

        with pytest.raises(GitHubAPIError):
            _client_with(handler).get_file_contents("o", "r", "a.py", "sha1")

    def test_get_repository_tree_returns_blob_paths(self) -> None:
        """Only blob (file) entries are returned; tree (directory) entries are excluded."""

        def handler(req: httpx.Request) -> httpx.Response:
            assert "/git/trees/sha1" in req.url.path
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {"type": "blob", "path": "pkg/a.py"},
                        {"type": "tree", "path": "pkg"},
                        {"type": "blob", "path": "pkg/b.py"},
                    ],
                },
            )

        paths = _client_with(handler).get_repository_tree("o", "r", "sha1")
        assert paths == ["pkg/a.py", "pkg/b.py"]  # trees (directories) excluded

    def test_get_repository_tree_truncated_returns_partial(self) -> None:
        """A truncated tree response still returns the partial blob listing it received."""

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"truncated": True, "tree": [{"type": "blob", "path": "a.py"}]}
            )

        assert _client_with(handler).get_repository_tree("o", "r", "sha1") == ["a.py"]


class TestGitHubRepoReader:
    """Verify GitHubRepoReader's caching, failure handling, and concurrency safety."""

    def test_reads_and_caches_and_lists(self) -> None:
        """list_files and read_file each hit the API once, then serve from cache."""
        calls = {"contents": 0, "tree": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            if "/git/trees/" in req.url.path:
                calls["tree"] += 1
                return httpx.Response(
                    200, json={"tree": [{"type": "blob", "path": "pkg/models.py"}]}
                )
            calls["contents"] += 1
            enc = base64.b64encode(b"class M: ...").decode()
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": enc})

        reader = GitHubRepoReader(_client_with(handler), "o", "r", "sha1")
        assert reader.list_files() == ["pkg/models.py"]
        assert reader.list_files() == ["pkg/models.py"]  # cached, no 2nd tree call
        assert calls["tree"] == 1
        assert reader.read_file("pkg/models.py") == "class M: ..."
        assert reader.read_file("pkg/models.py") == "class M: ..."  # cached
        assert calls["contents"] == 1

    def test_read_failure_and_cap_return_none(self) -> None:
        """A blank path never fetches; a failed fetch and the fetch cap both yield None."""

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        reader = GitHubRepoReader(_client_with(handler), "o", "r", "sha1", max_fetches=1)
        assert reader.read_file("") is None  # blank never fetches
        assert reader.read_file("a.py") is None  # 404 -> None (1 fetch used)
        assert reader.read_file("b.py") is None  # cap reached -> None

    def test_tree_error_is_failsafe(self) -> None:
        """A tree-fetch error is swallowed, yielding an empty file listing instead of raising."""

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        reader = GitHubRepoReader(_client_with(handler), "o", "r", "sha1")
        assert reader.list_files() == []

    def test_list_files_is_capped(self) -> None:
        """list_files truncates to max_listed instead of returning the full unbounded tree."""

        def handler(_req: httpx.Request) -> httpx.Response:
            blobs = [{"type": "blob", "path": f"f{i}.py"} for i in range(10)]
            return httpx.Response(200, json={"tree": blobs})

        reader = GitHubRepoReader(_client_with(handler), "o", "r", "sha1", max_listed=3)
        assert len(reader.list_files()) == 3  # capped, unlike the pre-fix unbounded listing

    def test_read_file_single_flight_under_concurrency(self) -> None:
        """Concurrent same-path read_file calls fetch exactly once (single-flight).

        Single-flight means only the leader thread ever reaches the handler —
        the other 7 block on the reader's own condvar, never issuing a GET — so
        this must NOT barrier-synchronize multiple handler invocations (there is
        only ever one). A short sleep in the handler simply widens the window in
        which the other 7 threads arrive and queue up on the in-flight guard
        instead of each opening their own connection.
        """
        calls = {"contents": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["contents"] += 1
            time.sleep(0.05)  # widen the race window so waiters queue up
            enc = base64.b64encode(b"class M: ...").decode()
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": enc})

        reader = GitHubRepoReader(_client_with(handler), "o", "r", "sha1")
        results: list[Optional[str]] = [None] * 8

        def _worker(i: int) -> None:
            results[i] = reader.read_file("pkg/models.py")

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert calls["contents"] == 1  # single-flight: only the leader fetched
        assert all(r == "class M: ..." for r in results)  # every waiter got the result
        assert reader._fetches == 1  # the fetch cap was charged exactly once

    def test_list_files_single_flight_under_concurrency(self) -> None:
        """Concurrent list_files calls before the tree resolves fetch exactly once.

        Guards against the double-checked-locking race where several threads all
        pass the initial 'is self._tree None' check before any of them sets it,
        each issuing its own tree GET. Only the leader ever reaches the handler
        (the rest wait on the reader's condvar), so — as in the read_file test
        above — this does not barrier-synchronize multiple handler invocations.
        """
        calls = {"tree": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["tree"] += 1
            time.sleep(0.05)  # widen the race window so waiters queue up
            return httpx.Response(200, json={"tree": [{"type": "blob", "path": "a.py"}]})

        reader = GitHubRepoReader(_client_with(handler), "o", "r", "sha1")
        results: list[Optional[list[str]]] = [None] * 8

        def _worker(i: int) -> None:
            results[i] = reader.list_files()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert calls["tree"] == 1  # single-flight: only the leader fetched the tree
        assert all(r == ["a.py"] for r in results)  # every waiter got the same listing

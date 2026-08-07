"""Tests for the PR-review flow.

Covers the new GitHub client methods (via httpx.MockTransport), and the
POST /review-pr endpoint + background hook in api/main.py (with a fake
GitHubClient and a stubbed CodeReviewAgent — no network, no LLM).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

import httpx
import pytest

from software_engineering_team.github_source import (
    MAX_REVIEW_COMMENTS_TRAVERSED,
    GitHubAPIError,
    GitHubClient,
    Issue,
    PullRequestDetail,
    PullRequestFile,
    ReviewComment,
)

from .test_coding_team_github_source import _stub_heavy_modules

# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubClient:
    """Create a GitHubClient whose HTTP calls are served by an httpx.MockTransport handler."""
    transport = httpx.MockTransport(handler)
    client = GitHubClient(token="t", sleep=lambda _s: None)
    client._client.close()  # type: ignore[attr-defined]
    client._client = httpx.Client(transport=transport, timeout=client._timeout)  # type: ignore[attr-defined]
    return client


def _pr_payload(number: int = 7, **overrides: Any) -> dict[str, Any]:
    """Return a dict mirroring GitHub's pull-request API payload, with optional field overrides."""
    payload = {
        "number": number,
        "html_url": f"https://example/pull/{number}",
        "title": "Add feature",
        "body": "PR body",
        "draft": False,
        "state": "open",
        "updated_at": "2026-01-01T00:00:00Z",
        "user": {"login": "octocat"},
        "head": {"ref": "feature", "sha": "abc123"},
        "base": {"ref": "main"},
        "labels": [{"name": "needs-review"}],
    }
    payload.update(overrides)
    return payload


def _pr_detail(
    *,
    number: int,
    html_url: str,
    head_sha: str = "sha1",
    author: str = "alice",
    body: str = "body",
    head: str = "feature",
    base: str = "main",
    title: str = "Add feature",
    draft: bool = False,
    state: str = "open",
    updated_at: str = "2026-01-01T00:00:00Z",
    labels: tuple[str, ...] = (),
) -> PullRequestDetail:
    """Build a `PullRequestDetail` test object with stable defaults."""
    return PullRequestDetail(
        number=number,
        html_url=html_url,
        head=head,
        base=base,
        head_sha=head_sha,
        title=title,
        body=body,
        draft=draft,
        author=author,
        state=state,
        updated_at=updated_at,
        labels=labels,
    )


# ---------------------------------------------------------------------------
# Client: list_open_pull_requests
# ---------------------------------------------------------------------------


class TestListOpenPullRequests:
    def test_paginates_via_link_header(self) -> None:
        page1 = "https://api.github.com/repos/o/r/pulls"
        page2 = "https://api.github.com/repos/o/r/pulls?page=2"
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(str(req.url))
            if "page=2" in str(req.url):
                return httpx.Response(200, json=[_pr_payload(2)])
            return httpx.Response(
                200, json=[_pr_payload(1)], headers={"Link": f'<{page2}>; rel="next"'}
            )

        client = _client_with(handler)
        prs = list(client.list_open_pull_requests("o", "r"))
        assert [p.number for p in prs] == [1, 2]
        assert page1 in calls[0]

    def test_empty(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, json=[]))
        assert list(client.list_open_pull_requests("o", "r")) == []


# ---------------------------------------------------------------------------
# Client: get_pull_request
# ---------------------------------------------------------------------------


class TestGetPullRequest:
    def test_parses_detail(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, json=_pr_payload(7)))
        pr = client.get_pull_request("o", "r", 7)
        assert isinstance(pr, PullRequestDetail)
        assert pr.head_sha == "abc123"
        assert pr.author == "octocat"
        assert pr.base == "main"
        assert pr.labels == ("needs-review",)
        assert pr.draft is False

    def test_error_raises(self) -> None:
        client = _client_with(lambda _req: httpx.Response(404, text="missing"))
        with pytest.raises(GitHubAPIError):
            client.get_pull_request("o", "r", 7)


# ---------------------------------------------------------------------------
# Client: get_pull_request_files
# ---------------------------------------------------------------------------


class TestGetPullRequestFiles:
    def test_pagination_binary_and_rename(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if "page=2" in str(req.url):
                return httpx.Response(
                    200,
                    json=[
                        {"filename": "img.png", "status": "added", "additions": 0, "deletions": 0},
                        {
                            "filename": "new_name.py",
                            "previous_filename": "old_name.py",
                            "status": "renamed",
                            "patch": "@@ -1 +1 @@\n-x\n+y",
                            "additions": 1,
                            "deletions": 1,
                        },
                    ],
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "a.py",
                        "status": "modified",
                        "patch": "@@ -1 +1 @@\n+a",
                        "additions": 1,
                        "deletions": 0,
                    }
                ],
                headers={
                    "Link": '<https://api.github.com/repos/o/r/pulls/7/files?page=2>; rel="next"'
                },
            )

        client = _client_with(handler)
        files = client.get_pull_request_files("o", "r", 7)
        assert [f.filename for f in files] == ["a.py", "img.png", "new_name.py"]
        assert files[1].patch == ""  # binary file: no patch
        assert files[2].previous_filename == "old_name.py"


# ---------------------------------------------------------------------------
# Client: list_review_comments / list_issue_comments
# ---------------------------------------------------------------------------


class TestListReviewComments:
    def test_pagination_and_file_level(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if "page=2" in str(req.url):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 2,
                            "path": "b.py",
                            "body": "file-level note",
                            "html_url": "https://example/comment/2",
                        }
                    ],
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "path": "a.py",
                        "line": 3,
                        "body": "line note",
                        "html_url": "https://example/comment/1",
                    }
                ],
                headers={
                    "Link": '<https://api.github.com/repos/o/r/pulls/7/comments?page=2>; rel="next"'
                },
            )

        client = _client_with(handler)
        comments = client.list_review_comments("o", "r", 7)
        assert [c.id for c in comments] == [1, 2]
        assert comments[0].line == 3
        assert comments[1].line is None  # file-level: no "line" key in the payload

    def test_error_raises(self) -> None:
        client = _client_with(lambda _req: httpx.Response(404, text="missing"))
        with pytest.raises(GitHubAPIError):
            client.list_review_comments("o", "r", 7)

    def test_caps_traversal_at_max_review_comments_traversed(self) -> None:
        overflow = [
            {
                "id": i,
                "path": "a.py",
                "line": 1,
                "body": "x",
                "html_url": f"https://example/comment/{i}",
            }
            for i in range(MAX_REVIEW_COMMENTS_TRAVERSED + 50)
        ]
        client = _client_with(lambda _req: httpx.Response(200, json=overflow))
        comments = client.list_review_comments("o", "r", 7)
        assert len(comments) == MAX_REVIEW_COMMENTS_TRAVERSED


class TestListIssueComments:
    def test_pagination(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if "page=2" in str(req.url):
                return httpx.Response(
                    200,
                    json=[
                        {"id": 20, "body": "second", "html_url": "https://example/issue-comment/20"}
                    ],
                )
            return httpx.Response(
                200,
                json=[{"id": 10, "body": "first", "html_url": "https://example/issue-comment/10"}],
                headers={
                    "Link": '<https://api.github.com/repos/o/r/issues/7/comments?page=2>; rel="next"'
                },
            )

        client = _client_with(handler)
        comments = client.list_issue_comments("o", "r", 7)
        assert [c.id for c in comments] == [10, 20]
        assert comments[0].body == "first"

    def test_error_raises(self) -> None:
        client = _client_with(lambda _req: httpx.Response(403, text="nope"))
        with pytest.raises(GitHubAPIError):
            client.list_issue_comments("o", "r", 7)

    def test_caps_traversal_at_max_review_comments_traversed(self) -> None:
        overflow = [
            {"id": i, "body": "x", "html_url": f"https://example/issue-comment/{i}"}
            for i in range(MAX_REVIEW_COMMENTS_TRAVERSED + 50)
        ]
        client = _client_with(lambda _req: httpx.Response(200, json=overflow))
        comments = client.list_issue_comments("o", "r", 7)
        assert len(comments) == MAX_REVIEW_COMMENTS_TRAVERSED


# ---------------------------------------------------------------------------
# Client: get_resolved_review_thread_comment_ids (GraphQL)
# ---------------------------------------------------------------------------


def _review_threads_response(
    *, has_next_page: bool = False, end_cursor: Optional[str] = None, nodes: Any = ()
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                        "nodes": list(nodes),
                    }
                }
            }
        }
    }


class TestGetResolvedReviewThreadCommentIds:
    def test_posts_graphql_and_parses_resolved_ids(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            captured["body"] = json.loads(req.content)
            return httpx.Response(
                200,
                json=_review_threads_response(
                    nodes=[
                        {"isResolved": True, "comments": {"nodes": [{"databaseId": 1}]}},
                        {"isResolved": False, "comments": {"nodes": [{"databaseId": 2}]}},
                    ]
                ),
            )

        client = _client_with(handler)
        resolved = client.get_resolved_review_thread_comment_ids("o", "r", 7)
        assert resolved == {1}
        assert captured["url"].endswith("/graphql")
        assert captured["body"]["variables"] == {
            "owner": "o",
            "repo": "r",
            "number": 7,
            "after": None,
        }

    def test_paginates_via_page_info(self) -> None:
        calls: list[Optional[str]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            after = json.loads(req.content)["variables"]["after"]
            calls.append(after)
            if after is None:
                return httpx.Response(
                    200,
                    json=_review_threads_response(
                        has_next_page=True,
                        end_cursor="cursor1",
                        nodes=[{"isResolved": True, "comments": {"nodes": [{"databaseId": 1}]}}],
                    ),
                )
            return httpx.Response(
                200,
                json=_review_threads_response(
                    nodes=[{"isResolved": True, "comments": {"nodes": [{"databaseId": 2}]}}]
                ),
            )

        client = _client_with(handler)
        resolved = client.get_resolved_review_thread_comment_ids("o", "r", 7)
        assert resolved == {1, 2}
        assert calls == [None, "cursor1"]

    def test_degrades_to_empty_set_on_graphql_errors(self) -> None:
        client = _client_with(
            lambda _req: httpx.Response(200, json={"errors": [{"message": "nope"}]})
        )
        assert client.get_resolved_review_thread_comment_ids("o", "r", 7) == set()

    def test_degrades_to_empty_set_on_http_error(self) -> None:
        client = _client_with(lambda _req: httpx.Response(500, text="boom"))
        assert client.get_resolved_review_thread_comment_ids("o", "r", 7) == set()

    def test_degrades_to_empty_set_on_malformed_json(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, text="not json"))
        assert client.get_resolved_review_thread_comment_ids("o", "r", 7) == set()


# ---------------------------------------------------------------------------
# Client: create_pull_request_review
# ---------------------------------------------------------------------------


class TestCreatePullRequestReview:
    def test_posts_expected_body(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            captured["body"] = json.loads(req.content)
            return httpx.Response(200, json={"id": 1, "html_url": "https://example/review/1"})

        client = _client_with(handler)
        out = client.create_pull_request_review(
            owner="o",
            repo="r",
            number=7,
            commit_id="abc123",
            body="overall",
            event="REQUEST_CHANGES",
            comments=[{"path": "a.py", "line": 3, "side": "RIGHT", "body": "fix"}],
        )
        assert out["id"] == 1
        assert captured["url"].endswith("/pulls/7/reviews")
        assert captured["body"]["commit_id"] == "abc123"
        assert captured["body"]["event"] == "REQUEST_CHANGES"
        assert captured["body"]["comments"][0]["line"] == 3

    def test_omits_comments_when_empty(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content)
            return httpx.Response(200, json={"id": 2})

        client = _client_with(handler)
        client.create_pull_request_review(
            owner="o", repo="r", number=7, commit_id="s", body="b", event="COMMENT", comments=[]
        )
        assert "comments" not in captured["body"]

    def test_422_raises(self) -> None:
        client = _client_with(lambda _req: httpx.Response(422, text="bad line"))
        with pytest.raises(GitHubAPIError):
            client.create_pull_request_review(
                owner="o", repo="r", number=7, commit_id="s", body="b"
            )


class TestCreateReviewComment:
    """Tests for GitHubClient.create_review_comment covering line-anchored and
    file-level posting, error handling, and precondition validation."""

    def test_line_comment_posts_line_and_side(self) -> None:
        """A line-anchored comment sends line + side and no subject_type."""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            captured["body"] = json.loads(req.content)
            return httpx.Response(201, json={"id": 9, "html_url": "https://example/comment/9"})

        client = _client_with(handler)
        out = client.create_review_comment(
            owner="o", repo="r", number=7, commit_id="abc", path="a.py", body="fix", line=3
        )
        assert out["id"] == 9
        assert captured["url"].endswith("/pulls/7/comments")
        assert captured["body"]["commit_id"] == "abc"
        assert captured["body"]["path"] == "a.py"
        assert captured["body"]["line"] == 3
        assert captured["body"]["side"] == "RIGHT"
        assert "subject_type" not in captured["body"]

    def test_file_comment_posts_subject_type_no_line(self) -> None:
        """A file-level comment sends subject_type="file" and omits line/side."""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content)
            return httpx.Response(201, json={"id": 10})

        client = _client_with(handler)
        client.create_review_comment(
            owner="o",
            repo="r",
            number=7,
            commit_id="abc",
            path="a.py",
            body="fix",
            subject_type="file",
        )
        assert captured["body"]["subject_type"] == "file"
        assert "line" not in captured["body"]
        assert "side" not in captured["body"]

    def test_422_raises(self) -> None:
        """A non-2xx response surfaces as GitHubAPIError for the caller to handle."""
        client = _client_with(lambda _req: httpx.Response(422, text="bad file comment"))
        with pytest.raises(GitHubAPIError):
            client.create_review_comment(
                owner="o",
                repo="r",
                number=7,
                commit_id="s",
                path="a.py",
                body="b",
                subject_type="file",
            )

    def test_requires_exactly_one_anchor(self) -> None:
        """Neither or both of line/subject_type violates the precondition → ValueError."""
        # Never reaches the network: the precondition is enforced first.
        client = _client_with(lambda _req: httpx.Response(500, text="should not be hit"))
        with pytest.raises(ValueError):  # both supplied
            client.create_review_comment(
                owner="o",
                repo="r",
                number=7,
                commit_id="s",
                path="a.py",
                body="b",
                line=3,
                subject_type="file",
            )
        with pytest.raises(ValueError):  # neither supplied
            client.create_review_comment(
                owner="o", repo="r", number=7, commit_id="s", path="a.py", body="b"
            )

    def test_rejects_invalid_preconditions(self) -> None:
        """Non-positive line, invalid side, empty path/body, or non-file subject_type
        each raise ValueError."""
        client = _client_with(lambda _req: httpx.Response(500, text="should not be hit"))
        with pytest.raises(ValueError):  # line < 1
            client.create_review_comment(
                owner="o", repo="r", number=7, commit_id="s", path="a.py", body="b", line=0
            )
        with pytest.raises(ValueError):  # invalid side for a line comment
            client.create_review_comment(
                owner="o",
                repo="r",
                number=7,
                commit_id="s",
                path="a.py",
                body="b",
                line=3,
                side="MIDDLE",
            )
        with pytest.raises(ValueError):  # non-"file" subject_type
            client.create_review_comment(
                owner="o",
                repo="r",
                number=7,
                commit_id="s",
                path="a.py",
                body="b",
                subject_type="line",
            )
        with pytest.raises(ValueError):  # empty path
            client.create_review_comment(
                owner="o", repo="r", number=7, commit_id="s", path="", body="b", line=3
            )
        with pytest.raises(ValueError):  # empty body
            client.create_review_comment(
                owner="o", repo="r", number=7, commit_id="s", path="a.py", body="", line=3
            )


class TestAuthenticatedLogin:
    def test_returns_login(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, json={"login": "khala-bot"}))
        assert client.get_authenticated_login() == "khala-bot"

    def test_empty_when_missing(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, json={}))
        assert client.get_authenticated_login() == ""


# ---------------------------------------------------------------------------
# Endpoint + hook
# ---------------------------------------------------------------------------


class _FakeOutput:
    """Stand-in for the code-review agent's output: issues, summary, spec-compliance notes, and suggested commit message."""

    def __init__(self, issues: list[Any], summary: str = "S", spec: str = "SC") -> None:
        self.issues = issues
        self.summary = summary
        self.spec_compliance_notes = spec
        self.suggested_commit_message = ""


class _FakeReviewIssue:
    """Duck-typed stand-in for a CodeReviewIssue, with the attributes the PR-review
    flow reads (severity, category, file_path, line, description, suggestion,
    pre_existing)."""

    def __init__(
        self,
        severity: str,
        line: Optional[int],
        file_path: str = "a.py",
        description: str = "desc",
        pre_existing: bool = False,
    ) -> None:
        self.severity = severity
        self.category = "logic"
        self.file_path = file_path
        self.line = line
        self.description = description
        self.suggestion = "fix"
        self.pre_existing = pre_existing


class _FakeReviewClient:
    """Fake GitHubClient surface the review endpoint + hook touch.

    Configurable failure knobs (all default to "never fail"):
        - ``fail_get_pr``: ``get_pull_request`` raises a 404 ``GitHubAPIError``.
        - ``fail_get_pr_after_first_call``: ``get_pull_request`` succeeds on its
          FIRST call (the admission-time pre-check in the route handler) but
          raises a 404 on every subsequent call (the fetch inside
          ``_run_pr_review_body``/``_fetch_pr_metadata``) — lets a test fail the
          body's own PR fetch independently of admission.
        - ``fail_get_pr_files``: ``get_pull_request_files`` raises a 502
          ``GitHubAPIError``.
        - ``review_fail_times``: the first N ``create_pull_request_review`` calls
          raise a 422, exercising the submit-degradation retry ladder.
        - ``bad_lines``: any review whose comments include one of these line
          numbers 422s, modelling a stray off-diff line and driving bisection.
        - ``review_comment_fail_paths``: ``create_review_comment`` 422s for these
          paths, exercising the file-level → standalone fallback.
        - ``review_exc``: a non-API exception raised on every review submit (to
          test the broad outer error handler).
        - ``comment_fail_times``: the first N ``add_issue_comment`` calls raise a
          403, exercising the per-finding comment failure path.
        - ``reaction_fail``: ``create_issue_reaction`` raises a 403, exercising the
          best-effort reaction path that must never fail the job.
    Captured side effects: ``reviews`` (each submitted review's kwargs),
    ``review_comments`` (each ``create_review_comment`` kwargs — the dedicated
    review-comments endpoint that carries file-level comments), ``comments``
    (each posted standalone ``(issue_number, body)``), and ``reactions`` (each
    ``create_issue_reaction`` call as ``(issue_number, content)``).

    Models the real GitHub constraint that the Reviews API's embedded ``comments``
    array does not accept ``subject_type``: any such entry 422s the whole review,
    so file-level comments must travel via ``create_review_comment``.
    """

    def __init__(self) -> None:
        self.files: list[PullRequestFile] = [
            PullRequestFile(
                filename="a.py",
                status="modified",
                patch="@@ -1,2 +1,3 @@\n ctx\n+added\n more",
                additions=1,
                deletions=0,
                previous_filename=None,
            )
        ]
        self.login = "khala-bot"
        self.author = "alice"
        self.reviews: list[dict[str, Any]] = []  # every attempt (success or 422)
        self.submitted_reviews: list[dict[str, Any]] = []  # successful submits only
        self.review_comments: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.reactions: list[tuple[int, str]] = []
        self.fail_get_pr = False
        self.fail_get_pr_after_first_call = False
        self.fail_get_pr_files = False
        self.get_pull_request_calls = 0
        self.review_fail_times = 0  # number of leading create_review calls that fail
        self.review_fail_status = 422  # status raised by review_fail_times / bad_lines
        self.bad_lines: set[int] = set()  # lines whose review 422s (drives bisection)
        self.review_comment_fail_paths: set[str] = set()  # paths that 422 as file comments
        self.review_comment_fail_status = 422  # status raised by review_comment_fail_paths
        self.review_exc: Optional[Exception] = None  # non-API error to raise on submit
        self.comment_fail_times = 0  # number of leading add_issue_comment calls that 422
        self._comment_calls = 0
        self.reaction_fail = False  # create_issue_reaction raises a 403 when True
        self.created_issues: list[dict[str, Any]] = []  # each create_issue call's kwargs
        self.create_issue_fail = False  # create_issue raises a 403 when True
        self.open_issues: list[Any] = []  # Issue-like objects returned by list_open_issues
        self.list_open_issues_exc: Optional[Exception] = None
        self.list_open_issues_calls = 0
        self.existing_review_comments: list[Any] = []  # ReviewComment-shaped stand-ins
        self.existing_issue_comments: list[Any] = []  # IssueComment-shaped stand-ins
        self.existing_resolved_ids: set[int] = set()
        # When True, list_review_comments raises — exercises the fail-open path in
        # _fetch_existing_comments (a lookup failure must not fail the review).
        self.fetch_existing_comments_fail = False
        self.list_review_comments_calls = 0  # counts calls, to assert the fetch is skipped

    def __enter__(self) -> "_FakeReviewClient":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def list_review_comments(self, _o: str, _r: str, _n: int) -> list[Any]:
        self.list_review_comments_calls += 1
        if self.fetch_existing_comments_fail:
            raise GitHubAPIError(500, "existing comments unavailable")
        return list(self.existing_review_comments)

    def list_issue_comments(self, _o: str, _r: str, _n: int) -> list[Any]:
        return list(self.existing_issue_comments)

    def get_resolved_review_thread_comment_ids(self, _o: str, _r: str, _n: int) -> set[int]:
        return set(self.existing_resolved_ids)

    def get_pull_request(self, _o: str, _r: str, n: int) -> PullRequestDetail:
        self.get_pull_request_calls += 1
        if self.fail_get_pr:
            raise GitHubAPIError(404, "missing PR")
        if self.fail_get_pr_after_first_call and self.get_pull_request_calls > 1:
            raise GitHubAPIError(404, "missing PR")
        return _pr_detail(
            number=n,
            html_url=f"https://example/pull/{n}",
            author=self.author,
        )

    def get_pull_request_files(self, _o: str, _r: str, _n: int) -> list[PullRequestFile]:
        if self.fail_get_pr_files:
            raise GitHubAPIError(502, "files unavailable")
        return list(self.files)

    def get_authenticated_login(self) -> str:
        return self.login

    def add_issue_comment(self, _o: str, _r: str, n: int, body: str) -> None:
        self._comment_calls += 1
        if self._comment_calls <= self.comment_fail_times:
            raise GitHubAPIError(403, "rate limited")
        self.comments.append((n, body))

    def create_pull_request_review(self, **kwargs: Any) -> dict[str, Any]:
        if self.review_exc is not None:
            raise self.review_exc
        comments = kwargs.get("comments") or []
        # Real GitHub constraint: the reviews array rejects subject_type.
        if any("subject_type" in c for c in comments):
            self.reviews.append(kwargs)
            raise GitHubAPIError(422, "subject_type not allowed in review comments")
        if self.bad_lines and any(c.get("line") in self.bad_lines for c in comments):
            self.reviews.append(kwargs)
            raise GitHubAPIError(self.review_fail_status, "off-diff line rejected")
        if len(self.reviews) < self.review_fail_times:
            self.reviews.append(kwargs)
            raise GitHubAPIError(self.review_fail_status, "simulated review failure")
        self.reviews.append(kwargs)
        self.submitted_reviews.append(kwargs)
        return {"id": 1, "html_url": "https://example/review/1"}

    def create_review_comment(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("path") in self.review_comment_fail_paths:
            raise GitHubAPIError(self.review_comment_fail_status, "file comment failed")
        self.review_comments.append(kwargs)
        return {"id": 2, "html_url": "https://example/comment/2"}

    def create_issue_reaction(self, _o: str, _r: str, n: int, content: str = "+1") -> None:
        if self.reaction_fail:
            raise GitHubAPIError(403, "rate limited")
        self.reactions.append((n, content))

    def create_issue(self, _o: str, _r: str, *, title: str, body: str, labels: Any = None) -> Any:
        """Record a created issue and return an object exposing number/html_url.

        ``create_issue_fail`` (set on the instance) raises a 403 to exercise the
        create-issues error path.
        """
        if self.create_issue_fail:
            raise GitHubAPIError(403, "no issue-write scope")
        self.created_issues.append({"title": title, "body": body, "labels": labels})
        number = len(self.created_issues)
        return type(
            "_Issue",
            (),
            {"number": number, "html_url": f"https://example/issues/{number}"},
        )()

    def list_open_issues(self, _o: str, _r: str, label: Optional[str] = None) -> Any:
        """Duplicate-detection's read of existing open issues.

        ``list_open_issues_exc``, when set, is raised instead — exercising the
        review flow's fail-open degrade-and-continue path.
        """
        self.list_open_issues_calls += 1
        if self.list_open_issues_exc is not None:
            raise self.list_open_issues_exc
        return iter(self.open_issues)


@pytest.fixture
def review_app(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Build a wired-up PR-review test environment.

    Returns a dictionary used by the integration tests:
    - ``client``: FastAPI ``TestClient``
    - ``api``: the monkeypatched ``coding_team_main`` module
    - ``repo_path``: temporary directory path used as ``repo_path``
    - ``github``: dict holding the ``_FakeReviewClient`` under ``client``
    - ``jobs``: fake job service client recording job-store calls
    """
    _stub_heavy_modules(monkeypatch)

    from job_service_client_fake import FakeJobServiceClient

    fake_jobs = FakeJobServiceClient(team="coding_team")
    from software_engineering_team import job_store as job_store_mod

    monkeypatch.setattr(job_store_mod, "_client", lambda *a, **kw: fake_jobs)
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.delenv("PR_REVIEW_EVENT", raising=False)

    from software_engineering_team.api import coding_team_main as api_main

    holder: dict[str, Any] = {"client": _FakeReviewClient()}
    monkeypatch.setattr(api_main, "GitHubClient", lambda **_kw: holder["client"])
    monkeypatch.setattr(
        api_main,
        "_start_pr_review_thread",
        lambda *a, **kw: api_main._run_pr_review(*a, **kw),
    )

    # Install a fake engine provider so no LLM stack loads. The PR-review path
    # calls provider.run_pr_code_review(...) via coding_team.engine_provider; the
    # monkeypatched module global auto-reverts after the test.
    holder["agent_output"] = _FakeOutput(
        issues=[_FakeReviewIssue("high", line=2), _FakeReviewIssue("low", line=999)]
    )

    class _FakeProvider:
        def run_pr_code_review(self, **_kw: Any) -> Any:
            out = holder["agent_output"]
            if isinstance(out, Exception):
                raise out
            return out

    monkeypatch.setattr("software_engineering_team.engine_provider._provider", _FakeProvider())

    from fastapi.testclient import TestClient

    return {
        "client": TestClient(api_main.app),
        "api": api_main,
        "repo_path": str(tmp_path),
        "github": holder,
        "jobs": fake_jobs,
    }


def _review_body(**overrides: Any) -> dict[str, Any]:
    """Return a default PR-review request body, merged with caller overrides.

    Defaults:
    - ``owner``: ``"o"``
    - ``repo``: ``"r"``
    - ``repo_path``: current working directory
    - ``pr_number``: ``7``
    """
    body = {
        "owner": "o",
        "repo": "r",
        "repo_path": overrides.pop("repo_path", str(Path.cwd())),
        "pr_number": 7,
    }
    body.update(overrides)
    return body


class TestReviewEndpoint:
    """Integration tests for the POST /review-pr endpoint.

    Exercises end-to-end review submission, severity accounting,
    token scrubbing, and failure handling via the review_app fixture.
    """

    def test_happy_path_posts_review(self, review_app) -> None:
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        data = resp.json()
        assert data["pr_number"] == 7
        # The response carries a server-clock start time so the UI computes a live
        # duration on one clock (this start + the completion from job status).
        assert data["created_at"]
        gh = review_app["github"]["client"]
        # One review submitted, REQUEST_CHANGES (high severity, author != reviewer).
        assert len(gh.reviews) == 1
        review = gh.reviews[0]
        assert review["event"] == "REQUEST_CHANGES"
        # The in-diff line (2) is a line-anchored comment carried in the review.
        line_comments = [c for c in review["comments"] if "line" in c]
        assert len(line_comments) == 1 and line_comments[0]["line"] == 2
        assert len(review["comments"]) == 1
        # The out-of-diff line (999) on the same changed file is a file-level
        # comment posted on the dedicated endpoint (subject_type="file"),
        # not in the review's comments array.
        assert len(gh.review_comments) == 1
        assert gh.review_comments[0]["path"] == "a.py"
        assert gh.review_comments[0]["subject_type"] == "file"
        assert "line" not in gh.review_comments[0]
        # The body is summary-only — no finding is batched into it.
        assert "General findings" not in review["body"]
        # No loose conversation comments: every finding rode on the review/endpoint.
        assert gh.comments == []
        # Job completed with the PR url + review summary.
        job = review_app["jobs"].get_job(data["job_id"])
        assert job["status"] == "completed"
        assert job["github_pr_url"] == "https://example/pull/7"
        assert job["review_summary"]["inline_comments"] == 1
        assert job["review_summary"]["file_comments"] == 1
        assert job["review_summary"]["comment_findings"] == 0
        # The two PR findings (one high inline, one low file-level) are broken down
        # by severity for the Code Review page's per-review metrics. Only non-zero
        # levels are emitted, so the zero levels are absent from the map.
        assert job["review_summary"]["severity_counts"] == {"high": 1, "low": 1}
        # Invariant: with all findings at recognized severities, the per-severity
        # counts sum to total_issues.
        assert (
            sum(job["review_summary"]["severity_counts"].values())
            == job["review_summary"]["total_issues"]
        )

    def test_review_summary_counts_findings_by_severity(self, review_app) -> None:
        # Findings across several recognized severities, plus a pre-existing bug:
        # severity matching is case-insensitive ("HIGH" folds into "high"), the
        # pre-existing bug (excluded from the PR review) does not inflate the counts,
        # and only non-zero levels are emitted. With every PR finding at a recognized
        # severity the invariant holds: the counts sum to total_issues.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("critical", line=2, description="crit one"),
                _FakeReviewIssue("high", line=2, description="high one"),
                _FakeReviewIssue("HIGH", line=2, description="high two, cased"),
                _FakeReviewIssue("info", line=2, description="info one"),
                _FakeReviewIssue(
                    "high",
                    line=4,
                    file_path="unchanged.py",
                    description="pre-existing bug",
                    pre_existing=True,
                ),
            ],
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        # Four PR findings counted (the pre-existing bug is excluded); zero levels
        # (medium, low) are absent from the compact map.
        assert job["review_summary"]["total_issues"] == 4
        assert job["review_summary"]["severity_counts"] == {"critical": 1, "high": 2, "info": 1}
        # Invariant: all findings are at recognized severities, so the counts sum to
        # total_issues.
        assert (
            sum(job["review_summary"]["severity_counts"].values())
            == job["review_summary"]["total_issues"]
        )
        # The pre-existing bug is excluded from the counts because it's routed to a
        # proposal, not silently dropped: it must show up there and never as any kind
        # of PR comment.
        proposals = job["review_summary"]["pending_issue_proposals"]
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal["description"] == "pre-existing bug"
        assert proposal["file_path"] == "unchanged.py"
        gh = review_app["github"]["client"]
        all_comment_bodies = (
            [c["body"] for rev in gh.submitted_reviews for c in rev.get("comments", [])]
            + [c.get("body", "") for c in gh.review_comments]
            + [body for _n, body in gh.comments]
        )
        assert not any("pre-existing bug" in body for body in all_comment_bodies)

    def test_review_summary_excludes_unknown_and_blank_severities(self, review_app) -> None:
        # A finding whose severity is unrecognized ("bogus") or blank ("") is counted
        # in total_issues but excluded from the severity breakdown (there is no chip
        # for it). This is the one documented exception to the sum==total_issues
        # invariant: the sum falls short by the number of such findings.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=2, description="recognized"),
                _FakeReviewIssue("bogus", line=2, description="unknown severity"),
                _FakeReviewIssue("", line=2, description="blank severity"),
            ],
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        # All three are PR findings, but only the recognized one is bucketed.
        assert job["review_summary"]["total_issues"] == 3
        assert job["review_summary"]["severity_counts"] == {"high": 1}
        # Sum is short by the two unrecognized/blank findings.
        assert (
            sum(job["review_summary"]["severity_counts"].values())
            == job["review_summary"]["total_issues"] - 2
        )

    def test_review_body_and_inline_comments_are_token_scrubbed(self, review_app) -> None:
        # LLM output (summary + inline finding text) can echo a credential from the
        # reviewed code; it must be scrubbed before the review is submitted, just
        # like the standalone comments.
        secret_url = "https://x:ghp_SECRETTOKEN@github.com/o/r.git"
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[_FakeReviewIssue("high", line=2, description=f"leak {secret_url} here")],
            summary=f"overall {secret_url}",
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        assert len(gh.reviews) == 1
        review = gh.reviews[0]
        assert len(review["comments"]) == 1
        assert "ghp_SECRETTOKEN" not in review["body"]
        assert "https://***@" in review["body"]
        assert "ghp_SECRETTOKEN" not in review["comments"][0]["body"]

    def test_file_level_and_standalone_comments_are_token_scrubbed(self, review_app) -> None:
        # The line-anchored/review-body path is covered above; this covers the
        # other two comment paths this PR touches -- a file-level comment (an
        # off-diff line on a changed file) and a standalone comment (a file
        # absent from the diff entirely) -- so a leaked credential can't slip
        # through either one unscrubbed.
        secret_url = "https://x:ghp_SECRETTOKEN@github.com/o/r.git"
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("low", line=999, description=f"leak {secret_url} here"),
                _FakeReviewIssue(
                    "low", line=4, file_path="not_in_diff.py", description=f"leak {secret_url} too"
                ),
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        assert len(gh.review_comments) == 1
        assert len(gh.comments) == 1
        assert "ghp_SECRETTOKEN" not in gh.review_comments[0]["body"]
        assert "https://***@" in gh.review_comments[0]["body"]
        assert "ghp_SECRETTOKEN" not in gh.comments[0][1]
        assert "https://***@" in gh.comments[0][1]

    def test_multiple_off_diff_findings_each_get_own_file_comment(self, review_app) -> None:
        # The core contract: every finding produces its OWN comment and no comment
        # lists more than one finding (never batched). Both findings cite off-diff
        # lines on the changed file `a.py`, so each becomes its own file-level
        # review comment attached to the single review (no loose comments).
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=999, description="first finding"),
                _FakeReviewIssue("low", line=1000, description="second finding"),
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        # Two file-level comments posted on the dedicated endpoint; the summary
        # review carries no inline comments; no loose conversation comments.
        review_comments = gh.review_comments
        assert all(c.get("subject_type") == "file" for c in review_comments)
        assert len(review_comments) == 2
        assert gh.comments == []
        bodies = [c["body"] for c in review_comments]
        # Each comment carries exactly one finding (the two never share a comment).
        assert sum("first finding" in b for b in bodies) == 1
        assert sum("second finding" in b for b in bodies) == 1
        for b in bodies:
            assert not ("first finding" in b and "second finding" in b)
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["review_summary"]["file_comments"] == 2
        assert job["review_summary"]["comment_findings"] == 0

    def test_off_diff_finding_without_tag_becomes_standalone_comment(self, review_app) -> None:
        # A finding whose file is not in the PR diff, and which the reviewer did NOT
        # tag pre_existing, is still an in-scope PR finding (round 2): forcing every
        # off-diff-file finding into proposals would silently drop a real,
        # PR-blocking finding like "this PR references module X but never added
        # it". Since it cannot be anchored to any diff location, it is posted as
        # its own standalone conversation comment naming its own file -- never
        # dropped, and never misattributed to an unrelated changed file.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("low", line=4, file_path="not_in_diff.py", description="orphan")
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        # Never a file-level comment (would misattribute it to the changed a.py).
        assert gh.review_comments == []
        for review in gh.reviews:
            for c in review.get("comments", []):
                assert "orphan" not in c.get("body", "")
        # Posted as its own standalone comment naming its own file.
        assert len(gh.comments) == 1
        assert gh.comments[0][0] == 7
        assert "orphan" in gh.comments[0][1]
        assert "`not_in_diff.py`" in gh.comments[0][1]
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["file_comments"] == 0
        assert job["review_summary"]["comment_findings"] == 1
        # Never routed to a proposal -- only an explicit pre_existing=True tag does that.
        assert job["review_summary"]["pending_issue_proposals"] == []

    def test_reviewer_none_output_fails_job(self, review_app) -> None:
        # A provider that returns None WITHOUT raising must fail the job, never
        # leave it wedged in "running". It degrades to a quiet outage: the raw
        # "reviewer returned no output" detail stays in the store, and only a
        # neutral, non-blocking note is posted to the PR.
        review_app["github"]["agent_output"] = None
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # The raw internal detail is preserved in the job store for operators...
        assert "reviewer returned no output" in (job.get("error") or "")
        gh = review_app["github"]["client"]
        # ...but is NEVER posted on the PR...
        assert not any("reviewer returned no output" in body for _n, body in gh.comments)
        # ...and EXACTLY ONE neutral, non-blocking note is posted (never multiple).
        outage_notes = [
            body
            for _n, body in gh.comments
            if "could not complete and did not post findings" in body
        ]
        assert len(outage_notes) == 1
        # No pull request review is created on the outage path.
        assert gh.reviews == []

    def test_reviewer_exception_does_not_post_raw_error(self, review_app) -> None:
        # A reviewer that RAISES must not leak the exception text onto the PR:
        # the job is failed with the detail recorded in the store, but the PR only
        # gets the neutral outage note — never "code review failed:" or the raw
        # message.
        review_app["github"]["agent_output"] = RuntimeError("secret internal detail")
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # The detail is preserved in the job store for diagnosis...
        assert "secret internal detail" in (job.get("error") or "")
        gh = review_app["github"]["client"]
        # ...but never leaks onto the PR.
        assert not any("secret internal detail" in body for _n, body in gh.comments)
        assert not any("code review failed" in body for _n, body in gh.comments)
        # Exactly one neutral outage note, never multiple.
        outage_notes = [
            body
            for _n, body in gh.comments
            if "could not complete and did not post findings" in body
        ]
        assert len(outage_notes) == 1
        # No pull request review or file-level comment is created on the outage
        # path -- the exception happens before any finding-posting is attempted.
        assert gh.reviews == []
        assert gh.review_comments == []

    def test_reviewer_bare_timeout_error_records_type_name_not_empty_string(
        self, review_app
    ) -> None:
        """Bare TimeoutError() has empty str(); job error must name the type.

        A durable-review client-side wait that times out with no attached detail
        would otherwise record ``code review failed: `` (useless for triage). The
        except block must fall back to the exception type name, keep that detail
        in the job store, and still post exactly one neutral outage note — never
        leak ``code review failed`` onto the PR.
        """
        review_app["github"]["agent_output"] = TimeoutError()
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        error = job.get("error") or ""
        assert "code review failed:" in error
        assert "TimeoutError" in error
        assert "no error message" in error
        gh = review_app["github"]["client"]
        # Never leaks onto the PR either.
        assert not any("code review failed" in body for _n, body in gh.comments)
        outage_notes = [
            body
            for _n, body in gh.comments
            if "could not complete and did not post findings" in body
        ]
        assert len(outage_notes) == 1
        assert gh.reviews == []

    def test_outage_notice_suppressed_when_disabled(self, review_app, monkeypatch) -> None:
        """With PR_REVIEW_POST_OUTAGE_NOTICE off, outages post nothing on the PR.

        The job must still be marked failed with the error detail preserved in
        the store, and neither comments nor a pull request review may be created.
        """
        monkeypatch.setenv("PR_REVIEW_POST_OUTAGE_NOTICE", "false")
        review_app["github"]["agent_output"] = RuntimeError("llm down")
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # Detail is still preserved in the store even though nothing is posted.
        assert "llm down" in (job.get("error") or "")
        assert review_app["github"]["client"].comments == []
        # And no pull request review is created either.
        assert review_app["github"]["client"].reviews == []

    def test_run_pr_review_survives_body_exception(self, review_app, monkeypatch) -> None:
        """Regression: an exception that escapes ``_run_pr_review_body`` (e.g. its
        own last-resort finalize failing on a store outage) must never propagate
        out of ``_run_pr_review`` — the daemon-thread hook must not die and the
        job must still be marked failed, honoring the "never raises" contract."""
        import software_engineering_team.api.pr_review as prm

        def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("body blew up past its own handler")

        monkeypatch.setattr(prm, "_run_pr_review_body", _boom)
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        # The outer guard caught the escape and finalized the job — no wedge.
        assert job["status"] == "failed"
        # And it preserved the cause in the job store for diagnosis.
        assert "body blew up past its own handler" in (job.get("error") or "")

    def test_run_pr_review_survives_setup_exception(
        self, review_app, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Setup failures before the body (e.g. RUNNING ``update_job``) must also
        honor the "never raises" contract via the widened outer guard, and any
        token-bearing exception text must be scrubbed from the finalized error
        and from warning/error logs."""
        import software_engineering_team.api.coding_team_main as main_mod
        from software_engineering_team.models import JobStatus

        secret_url = "https://x:ghp_LEAKEDTOKEN@github.com/o/r.git"
        real_update_job = main_mod.update_job

        def _update_job(job_id: str, **kw: Any) -> None:
            if kw.get("status") == JobStatus.RUNNING.value:
                raise RuntimeError(f"job service unreachable during RUNNING update: {secret_url}")
            return real_update_job(job_id, **kw)

        monkeypatch.setattr(main_mod, "update_job", _update_job)

        with caplog.at_level("ERROR"):
            resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "job service unreachable" in (job.get("error") or "")
        assert "ghp_LEAKEDTOKEN" not in (job.get("error") or "")
        assert not any("ghp_LEAKEDTOKEN" in r.getMessage() for r in caplog.records)
        assert not any(r.exc_text and "ghp_LEAKEDTOKEN" in r.exc_text for r in caplog.records)

    def test_body_failure_scrubs_token_from_log_and_outage(
        self, review_app, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression for the token-leak bug: when the ``_run_pr_review_body``
        try block raises with a GitHub token embedded in the exception text
        (e.g. leaked into git stderr), the scrubbed form — not the raw
        exception — must be what's logged and what reaches
        ``_record_review_outage``, matching the "best-effort, token-scrubbed"
        contract the surrounding comment promises for the whole except block."""
        import software_engineering_team.api.pr_review as prm

        secret_url = "https://x:ghp_LEAKEDTOKEN@github.com/o/r.git"

        def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError(f"clone failed: {secret_url}")

        monkeypatch.setattr(prm, "_finalize_review_outcome", _boom)
        with caplog.at_level("ERROR"):
            resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # Require exactly one hook-failed log, and scrub every caplog record
        # (message + attached traceback text).
        matches = [r for r in caplog.records if r.getMessage().startswith("PR review hook failed")]
        assert len(matches) == 1
        for r in caplog.records:
            assert "ghp_LEAKEDTOKEN" not in r.getMessage()
            assert "ghp_LEAKEDTOKEN" not in (r.exc_text or "")
        assert "ghp_LEAKEDTOKEN" not in (job.get("error") or "")
        gh = review_app["github"]["client"]
        assert not any("ghp_LEAKEDTOKEN" in body for _n, body in gh.comments)

    def test_missing_token_returns_400(self, review_app, monkeypatch) -> None:
        """POST /review-pr with github_token=None must return 400 because a GitHub token is required."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        resp = review_app["client"].post(
            "/review-pr", json={**_review_body(), "github_token": None}
        )
        assert resp.status_code == 400

    def test_pr_not_found_returns_502(self, review_app) -> None:
        """POST /review-pr must return 502 when GitHub cannot resolve the pull request."""
        review_app["github"]["client"].fail_get_pr = True
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 502

    def test_pr_fetch_failure_inside_body_marks_job_failed(self, review_app) -> None:
        """The admission-time get_pull_request (route handler) is a SEPARATE call
        from the one inside _run_pr_review_body/_fetch_pr_metadata — admission can
        succeed while the body's own fetch still fails, and that failure must still
        propagate through _fetch_pr_metadata to the outer handler and fail the job,
        exactly as the prior serial call would have."""
        gh = review_app["github"]["client"]
        gh.fail_get_pr_after_first_call = True
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "missing PR" in (job.get("error") or "")

    def test_pr_files_fetch_failure_marks_job_failed(self, review_app) -> None:
        """get_pull_request_files failing (with get_pull_request succeeding) must
        still fail the job via the outer handler, unchanged from the prior serial
        behavior, now that the two calls run concurrently in _fetch_pr_metadata."""
        gh = review_app["github"]["client"]
        gh.fail_get_pr_files = True
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "files unavailable" in (job.get("error") or "")

    def test_duplicate_review_for_same_pr_returns_409(self, review_app, monkeypatch) -> None:
        """A second review while one is already running for the same PR is rejected 409 —
        the cross-worker duplicate-review guard (also covers the manual UI trigger)."""
        api = review_app["api"]
        active = {
            "job_id": "existing-job",
            "status": "running",
            "github_context": {"owner": "o", "repo": "r", "pr_number": 7},
        }
        monkeypatch.setattr(api, "list_jobs", lambda **kw: [active])
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"]
        assert "existing-job" in resp.json()["detail"]

    def test_running_job_for_different_pr_does_not_block(self, review_app, monkeypatch) -> None:
        """An active review for a DIFFERENT PR (or an issue run) must not block this PR."""
        api = review_app["api"]
        other = {
            "job_id": "other-job",
            "status": "running",
            "github_context": {"owner": "o", "repo": "r", "pr_number": 999},
        }
        issue_run = {
            "job_id": "issue-job",
            "status": "running",
            "github_context": {"owner": "o", "repo": "r", "issue_number": 7},
        }
        monkeypatch.setattr(api, "list_jobs", lambda **kw: [other, issue_run])
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200

    def test_stale_heartbeat_review_does_not_block_and_marks_zombie_failed(
        self, review_app, monkeypatch
    ) -> None:
        """A crash-orphaned review job (heartbeat far past the staleness cutoff) must not
        block new reviews of the PR with 409 forever, and is best-effort marked failed."""
        from datetime import datetime, timedelta, timezone

        api = review_app["api"]
        stale_stamp = (
            datetime.now(timezone.utc) - timedelta(seconds=api._REVIEW_GUARD_HEARTBEAT_STALE_S + 60)
        ).isoformat()
        zombie = {
            "job_id": "zombie-job",
            "status": "running",
            "last_heartbeat_at": stale_stamp,
            "github_context": {"owner": "o", "repo": "r", "pr_number": 7},
        }
        monkeypatch.setattr(api, "list_jobs", lambda **kw: [zombie])
        marked: list[tuple[str, dict]] = []
        real_update = api.update_job
        monkeypatch.setattr(
            api,
            "update_job",
            lambda job_id, **kw: (marked.append((job_id, kw)), real_update(job_id, **kw))[-1],
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200  # unblocked — the zombie no longer counts as running
        zombie_updates = [kw for jid, kw in marked if jid == "zombie-job"]
        assert any(kw.get("status") == "failed" for kw in zombie_updates)

    def test_fresh_heartbeat_review_still_blocks(self, review_app, monkeypatch) -> None:
        """A review whose worker heartbeated recently is live and must keep blocking."""
        from datetime import datetime, timezone

        api = review_app["api"]
        live = {
            "job_id": "live-job",
            "status": "running",
            "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "github_context": {"owner": "o", "repo": "r", "pr_number": 7},
        }
        monkeypatch.setattr(api, "list_jobs", lambda **kw: [live])
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 409

    def test_far_future_heartbeat_treated_as_stale_not_live(self, review_app, monkeypatch) -> None:
        """A stamp beyond the clock-skew tolerance in the future is implausible (bad
        clock or corrupt data) — it must NOT count as live, or a dead job would block
        reviews until that future time passes."""
        from datetime import datetime, timedelta, timezone

        api = review_app["api"]
        far_future = (
            datetime.now(timezone.utc)
            + timedelta(seconds=api._HEARTBEAT_CLOCK_SKEW_TOLERANCE_S + 3600)
        ).isoformat()
        bad = {
            "job_id": "future-job",
            "status": "running",
            "last_heartbeat_at": far_future,
            "github_context": {"owner": "o", "repo": "r", "pr_number": 7},
        }
        monkeypatch.setattr(api, "list_jobs", lambda **kw: [bad])
        monkeypatch.setattr(api, "update_review", lambda *a, **kw: None)
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200  # unblocked

    def test_slightly_future_heartbeat_within_skew_is_live(self, review_app, monkeypatch) -> None:
        """NTP drift up to the tolerance must still count as live (keeps blocking)."""
        from datetime import datetime, timedelta, timezone

        api = review_app["api"]
        slight_future = (
            datetime.now(timezone.utc)
            + timedelta(seconds=api._HEARTBEAT_CLOCK_SKEW_TOLERANCE_S - 2)
        ).isoformat()
        live = {
            "job_id": "skewed-live-job",
            "status": "running",
            "last_heartbeat_at": slight_future,
            "github_context": {"owner": "o", "repo": "r", "pr_number": 7},
        }
        monkeypatch.setattr(api, "list_jobs", lambda **kw: [live])
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 409

    def test_missing_heartbeat_stamp_treated_as_live(self, review_app, monkeypatch) -> None:
        """No last_heartbeat_at → treated as live (fail toward blocking duplicates, not
        starting them) — the job service stamps it on every create/update, so a missing
        stamp means an unfamiliar store, not a dead worker."""
        api = review_app["api"]
        unstamped = {
            "job_id": "unstamped-job",
            "status": "running",
            "github_context": {"owner": "o", "repo": "r", "pr_number": 7},
        }
        monkeypatch.setattr(api, "list_jobs", lambda **kw: [unstamped])
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 409

    def test_pr_review_admission_serializes_within_process(self, review_app) -> None:
        """Two concurrent admissions for the same PR must not overlap — the lock makes
        the duplicate scan + job creation atomic within the process."""
        import threading as _threading
        import time as _time

        api = review_app["api"]
        order: list[str] = []
        entered = _threading.Event()
        release = _threading.Event()
        contending = _threading.Event()

        def first() -> None:
            with api._pr_review_admission("o", "r", 7):
                entered.set()
                release.wait(5)
                order.append("first-exit")

        def second() -> None:
            # Signal that we are about to contend for the lock BEFORE blocking on
            # it, so the main thread can wait for genuine contention rather than
            # guessing with a sleep alone.
            contending.set()
            with api._pr_review_admission("o", "r", 7):
                order.append("second-enter")

        t1 = _threading.Thread(target=first)
        t1.start()
        assert entered.wait(5)
        t2 = _threading.Thread(target=second)
        t2.start()
        # The second thread is running and about to block on the (held) lock; the
        # brief window then lets it (wrongly) acquire if mutual exclusion is broken.
        assert contending.wait(5)
        # Contending thread has signaled it is about to take the lock; poll briefly
        # to ensure it remains blocked while first still holds it (no single fixed
        # sleep that can flake under load).
        deadline = _time.monotonic() + 0.1
        while _time.monotonic() < deadline:
            assert "second-enter" not in order
            _time.sleep(0)
        release.set()
        t1.join(5)
        t2.join(5)
        assert order == ["first-exit", "second-enter"]

    def test_pr_review_admission_takes_pg_advisory_lock_when_postgres_enabled(
        self, review_app, monkeypatch
    ) -> None:
        """With Postgres configured, admission additionally takes a transaction-scoped
        advisory lock keyed on the casefolded owner/repo#pr — the cross-worker half of
        the mutual exclusion."""
        import contextlib as _contextlib

        api = review_app["api"]
        conn = MagicMock()

        @_contextlib.contextmanager
        def _fake_conn():
            yield conn

        import shared.postgres

        monkeypatch.setattr(shared.postgres, "is_postgres_enabled", lambda: True)
        monkeypatch.setattr(shared.postgres, "get_conn", _fake_conn)
        with api._pr_review_admission("Org", "Repo", 7):
            pass
        conn.execute.assert_called_once_with(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            ("coding_team_review_pr", "org/repo#7"),
        )

    def test_pr_review_admission_degrades_when_postgres_unavailable(
        self, review_app, monkeypatch
    ) -> None:
        """A failing advisory-lock acquisition degrades to the process-local lock alone
        (logged) — admission must never raise or block reviews on a Postgres outage."""
        import shared.postgres

        api = review_app["api"]
        monkeypatch.setattr(shared.postgres, "is_postgres_enabled", lambda: True)
        monkeypatch.setattr(
            shared.postgres, "get_conn", MagicMock(side_effect=RuntimeError("pg down"))
        )
        with api._pr_review_admission("o", "r", 7):
            pass  # must not raise

    def test_review_run_wraps_body_in_liveness_heartbeat(self, review_app, monkeypatch) -> None:
        """_run_pr_review must hold a continuous heartbeat for the job while the review
        runs (a single review LLM call can outlast the staleness cutoff), stopping it on
        exit — asserted via the context-manager protocol on a recording stand-in."""
        import shared.concurrency

        api = review_app["api"]
        seen: dict[str, Any] = {}

        class _RecordingHB:
            def __init__(self, beat, interval, **kw):
                seen["interval"] = interval
                seen["kwargs"] = kw
                seen["beat"] = beat

            def __enter__(self):
                seen["entered"] = True
                return self

            def __exit__(self, *exc):
                seen["exited"] = True
                return False

        monkeypatch.setattr(shared.concurrency, "BackgroundHeartbeat", _RecordingHB)
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        assert seen["entered"] and seen["exited"]
        assert seen["interval"] == api._REVIEW_HEARTBEAT_INTERVAL_S
        # The beat touches the job's liveness stamp via the job service.
        seen["beat"]()
        job_id = resp.json()["job_id"]
        assert review_app["jobs"].get_job(job_id) is not None

    def test_heartbeat_job_touches_job_service(self, review_app) -> None:
        from software_engineering_team import job_store as job_store_mod

        fake_jobs = review_app["jobs"]
        review_app["client"].post("/review-pr", json=_review_body())
        # Direct contract: heartbeat_job delegates to the job service's heartbeat.
        jobs = fake_jobs.list_jobs()
        assert jobs, "a review job should exist"
        job_id = jobs[0]["job_id"]
        job_store_mod.heartbeat_job(job_id)  # must not raise
        # And it actually touched the liveness stamp (not a silent no-op).
        assert fake_jobs.get_job(job_id).get("last_heartbeat_at") is not None

    def test_agent_failure_marks_job_failed(
        self, review_app, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Agent exceptions fail the job, post a neutral outage note, and scrub secrets."""
        secret_url = "https://x:ghp_LEAKEDTOKEN@github.com/o/r.git"
        review_app["github"]["agent_output"] = RuntimeError(f"llm down: {secret_url}")
        with caplog.at_level("WARNING"):
            resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # A failed job must not keep claiming mid-review progress: the failure
        # handler resets the percentage status_text and the activity entry.
        assert job["status_text"] is None
        assert job["current_activity"] is None
        # The core behavioral change: a neutral outage note is posted, and the raw
        # exception text is never surfaced on the PR.
        gh = review_app["github"]["client"]
        assert any("could not complete and did not post findings" in b for _n, b in gh.comments)
        all_bodies = (
            [b for _n, b in gh.comments]
            + [r.get("body", "") for r in gh.reviews]
            + [c.get("body", "") for c in gh.review_comments]
        )
        assert not any("llm down" in b for b in all_bodies)
        assert not any("RuntimeError" in b for b in all_bodies)
        assert not any("ghp_LEAKEDTOKEN" in b for b in all_bodies)
        assert "ghp_LEAKEDTOKEN" not in (job.get("error") or "")
        # Formatted log messages must also be scrubbed (traceback may still carry
        # the raw exception via logger.exception / exc_info — out of scope here).
        assert not any("ghp_LEAKEDTOKEN" in r.getMessage() for r in caplog.records)
        assert not any(secret_url in r.getMessage() for r in caplog.records)

    def test_no_changed_files_completes(self, review_app) -> None:
        review_app["github"]["client"].files = []
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert review_app["github"]["client"].reviews == []
        assert any(
            "no changed files" in c[1].lower() for c in review_app["github"]["client"].comments
        )

    def test_review_422_retries_then_succeeds(self, review_app) -> None:
        gh = review_app["github"]["client"]
        # Explicit file_path so this stays an off-diff *line in a changed file*
        # (-> file-level review comment) even if the fixture default ever changes.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=2, description="in-diff finding"),
                _FakeReviewIssue("low", line=999, file_path="a.py", description="off-diff line"),
            ]
        )
        gh.review_fail_times = 1  # first submit 422s, retry as COMMENT succeeds
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert len(gh.reviews) == 2
        assert gh.reviews[-1]["event"] == "COMMENT"
        # The retry kept the line-anchored comment (in-diff inline), so nothing
        # degraded to a loose conversation comment.
        assert gh.reviews[-1]["comments"] == gh.reviews[0]["comments"]
        assert len(gh.reviews[-1]["comments"]) == 1
        # The off-diff finding rode on the dedicated file-comment endpoint.
        assert len(gh.review_comments) == 1
        assert gh.review_comments[0]["subject_type"] == "file"
        assert gh.comments == []

    def test_forced_degradation_still_posts_line_comment_via_bisection(self, review_app) -> None:
        # When the full-batch review 422s on both the event and the COMMENT retry,
        # the summary is posted on its own and the line-anchored comments are
        # re-submitted (here, via the bisection path) so they stay inline — no
        # standalone conversation comments.
        gh = review_app["github"]["client"]
        # Explicit file_path so this stays an off-diff *line in a changed file*
        # (-> file-level review comment) even if the fixture default ever changes.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=2, description="in-diff finding"),
                _FakeReviewIssue("low", line=999, file_path="a.py", description="off-diff line"),
            ]
        )
        gh.review_fail_times = 2  # both full-batch attempts 422; bisection then succeeds
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        # No standalone comments — the line comment is re-posted inline.
        assert gh.comments == []
        # The in-diff line (2) survives as a line-anchored comment in a posted review.
        posted_line_comments = [
            c for rev in gh.submitted_reviews for c in rev.get("comments", []) if "line" in c
        ]
        assert any(c["line"] == 2 for c in posted_line_comments)
        # The off-diff finding rode on the dedicated file-comment endpoint.
        assert any(c.get("subject_type") == "file" for c in gh.review_comments)
        assert job["review_summary"]["inline_comments"] == 1
        assert job["review_summary"]["comment_findings"] == 0

    def test_bad_line_is_bisected_out_keeping_other_lines_inline(self, review_app) -> None:
        # A single line rejected by GitHub (bad_lines) must not collapse the whole
        # review: the good lines stay inline and only the bad one is demoted to a
        # file-level comment. (Diff valid lines for a.py are {1, 2, 3}; line 3 is
        # in-diff but still rejected by the fake client.)
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=2, description="good line"),
                _FakeReviewIssue("high", line=3, description="bad line"),
            ]
        )
        gh = review_app["github"]["client"]
        gh.bad_lines = {3}  # line 3 is rejected inline by GitHub
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        # The good line (2) is posted inline; the bad line (3) never lands in a
        # successfully-submitted review.
        posted_line_comments = [
            c for rev in gh.submitted_reviews for c in rev.get("comments", []) if "line" in c
        ]
        assert any(c["line"] == 2 for c in posted_line_comments)
        assert all(c["line"] != 3 for c in posted_line_comments)
        # The bad line (3) is demoted to a file-level comment on its file.
        demoted = [c for c in gh.review_comments if "bad line" in c.get("body", "")]
        assert len(demoted) == 1 and demoted[0]["subject_type"] == "file"
        assert gh.comments == []
        assert job["review_summary"]["inline_comments"] == 1
        assert job["review_summary"]["file_comments"] == 1
        assert job["review_summary"]["comment_findings"] == 0

    def test_multiple_bad_lines_are_bisected_out(self, review_app) -> None:
        # Bisection must isolate more than one line rejected by GitHub (bad_lines):
        # with two bad lines among three in-diff findings, only the good line stays
        # inline and both bad lines are demoted to file-level comments (none lost
        # to standalone).
        # (Diff valid lines for a.py are {1, 2, 3}, so all three are line-anchored.)
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=1, description="bad alpha"),
                _FakeReviewIssue("high", line=2, description="good beta"),
                _FakeReviewIssue("high", line=3, description="bad gamma"),
            ]
        )
        gh = review_app["github"]["client"]
        gh.bad_lines = {1, 3}  # two lines rejected inline by GitHub (bad_lines)
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        # Only the good line (2) is posted inline; neither bad line is.
        posted_line_comments = [
            c for rev in gh.submitted_reviews for c in rev.get("comments", []) if "line" in c
        ]
        assert any(c["line"] == 2 for c in posted_line_comments)
        assert all(c["line"] not in {1, 3} for c in posted_line_comments)
        # Both bad lines are demoted to file-level comments; nothing went standalone.
        demoted_bodies = [c.get("body", "") for c in gh.review_comments]
        assert any("bad alpha" in b for b in demoted_bodies)
        assert any("bad gamma" in b for b in demoted_bodies)
        assert gh.comments == []
        assert job["review_summary"]["inline_comments"] == 1
        assert job["review_summary"]["file_comments"] == 2
        assert job["review_summary"]["comment_findings"] == 0

    def test_off_diff_finding_without_tag_becomes_standalone_not_inline(self, review_app) -> None:
        # A finding whose file is not in the diff, without a pre_existing tag,
        # cannot be anchored to any diff location -- it must never be posted
        # line-anchored or file-level, but (round 2) it must still be posted, as
        # its own standalone comment, rather than dropped to a proposal. The
        # in-diff finding is unaffected.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=2, description="inline"),
                _FakeReviewIssue("low", line=4, file_path="missing.py", description="leftover"),
            ]
        )
        gh = review_app["github"]["client"]
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        # The review carries only the line-anchored inline for "inline"; the
        # out-of-diff finding is never line- or file-level anchored.
        assert len(gh.reviews) == 1
        line_comments = [c for c in gh.reviews[0]["comments"] if c.get("side") == "RIGHT"]
        file_comments = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert len(line_comments) == 1  # the in-diff finding
        assert file_comments == []  # the out-of-diff finding is never file-anchored
        # Instead it is posted as its own standalone conversation comment,
        # naming its own file rather than the unrelated in-diff file.
        assert len(gh.comments) == 1
        assert "leftover" in gh.comments[0][1]
        assert "missing.py" in gh.comments[0][1]
        assert job["review_summary"]["comment_findings"] == 1
        assert job["review_summary"]["pending_issue_proposals"] == []

    def test_multiple_off_diff_findings_each_get_own_standalone_comment(self, review_app) -> None:
        # All three findings name files absent from the diff, with no pre_existing
        # tag -- none can be anchored to any diff location, so each becomes its own
        # standalone conversation comment: never dropped, never merged, and never
        # misattributed to an unrelated changed file.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=1, file_path="gone.py", description="leftover one"),
                _FakeReviewIssue("high", line=2, file_path="gone.py", description="leftover two"),
                _FakeReviewIssue("low", line=3, file_path="gone.py", description="leftover three"),
            ]
        )
        gh = review_app["github"]["client"]
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        # None became a file-level comment (would misattribute them to a.py).
        assert gh.review_comments == []
        for review in gh.reviews:
            for c in review.get("comments", []):
                assert "leftover" not in c.get("body", "")
        # Each got its own standalone comment; none were merged or dropped.
        assert len(gh.comments) == 3
        bodies = [body for _n, body in gh.comments]
        assert sum("leftover one" in b for b in bodies) == 1
        assert sum("leftover two" in b for b in bodies) == 1
        assert sum("leftover three" in b for b in bodies) == 1
        for b in bodies:
            assert b.count("leftover") == 1  # no comment carries more than one finding
            assert "gone.py" in b  # each standalone comment still names its own file
        assert job["review_summary"]["comment_findings"] == 3
        assert job["review_summary"]["file_comments"] == 0
        assert job["review_summary"]["pending_issue_proposals"] == []

    def test_non_api_error_marks_job_failed_not_stuck(self, review_app) -> None:
        # A non-GitHubAPIError during submit must be caught by the broad outer
        # handler so the job transitions to failed instead of wedging in 'running'.
        review_app["github"]["client"].review_exc = RuntimeError("kaboom")
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # The submit-error path also degrades to the neutral outage note — never
        # the raw exception text.
        gh = review_app["github"]["client"]
        assert any("could not complete and did not post findings" in b for _n, b in gh.comments)
        assert not any("kaboom" in b for _n, b in gh.comments)

    def test_file_comment_422_falls_back_to_standalone(self, review_app) -> None:
        # Last-resort path: a file-level finding whose dedicated-endpoint post is
        # rejected (422) falls through to a standalone conversation comment so it
        # is not silently lost. The job still completes (the standalone succeeded).
        gh = review_app["github"]["client"]
        gh.review_comment_fail_paths = {"a.py"}  # every file-level post 422s
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("low", line=999, file_path="a.py", description="dropped finding")
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        # No file-level comment landed; the finding fell through to add_issue_comment.
        assert gh.review_comments == []
        assert len(gh.comments) == 1, (
            f"Expected exactly one standalone fallback comment, got gh.comments={gh.comments}"
        )
        _n, body = gh.comments[0]
        assert "dropped finding" in body, (
            f"Fallback comment missing finding text: gh.comments={gh.comments}"
        )
        assert "could not complete and did not post findings" not in body
        assert job["review_summary"]["comment_findings"] == 1

    def test_standalone_fallback_failure_marks_job_failed(self, review_app) -> None:
        # When even the standalone last-resort comment cannot be posted, the
        # "one comment per finding" contract is broken: the job is marked failed
        # and a (best-effort) notice is posted on the PR.
        gh = review_app["github"]["client"]
        gh.review_comment_fail_paths = {"a.py"}  # file-level post 422s → standalone
        gh.comment_fail_times = 1  # ...and the first standalone comment also fails
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[_FakeReviewIssue("low", line=999, file_path="a.py", description="lost finding")]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert job["review_summary"]["comments_failed"] == 1
        # The "incomplete" notice actually reached the PR, not just the job store.
        assert any(
            "1 of 1 finding comment(s) could not be posted" in body for _n, body in gh.comments
        )

    def test_summary_only_review_failure_does_not_fail_job(self, review_app) -> None:
        # With a file-level finding but no line-anchored diff comments, the review
        # body is summary-only; its failure must NOT fail the job because the
        # file-level comment still posts on the dedicated endpoint.
        gh = review_app["github"]["client"]
        gh.review_fail_times = 1  # the lone summary-only review attempt 422s
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue(
                    "low", line=999, file_path="a.py", description="off-hunk line in changed file"
                )
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        # The summary never posted, but the in-diff file-level finding did — and nothing
        # fell through to a standalone conversation comment.
        assert gh.submitted_reviews == []
        assert len(gh.review_comments) == 1
        assert gh.review_comments[0]["subject_type"] == "file"
        assert gh.comments == []
        assert job["review_summary"]["file_comments"] == 1
        assert job["review_summary"]["comment_findings"] == 0

    def test_zero_finding_review_summary_failure_marks_job_failed(self, review_app) -> None:
        # A zero-finding review's only output is the summary body. If that cannot
        # be posted, nothing reached GitHub — the job must surface as failed, not
        # report a hollow success (there are no findings to carry the review via
        # another path).
        gh = review_app["github"]["client"]
        gh.review_fail_times = 1  # the lone summary-only review attempt fails
        review_app["github"]["agent_output"] = _FakeOutput(issues=[])
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert gh.submitted_reviews == []
        assert gh.review_comments == []
        # The review never reached GitHub, so no clean-review reaction either.
        assert gh.reactions == []
        # This no-output path degrades to the neutral outage note (no raw error).
        assert any("could not complete and did not post findings" in b for _n, b in gh.comments)
        assert not any("simulated review failure" in b for _n, b in gh.comments)

    def test_zero_finding_review_summary_success_completes(self, review_app) -> None:
        # The clean-review baseline: zero findings, summary posts fine → completed.
        review_app["github"]["agent_output"] = _FakeOutput(issues=[])
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert len(gh.submitted_reviews) == 1  # the summary-only review
        assert gh.review_comments == []
        # A clean review carries an empty severity map (only non-zero levels are emitted).
        assert job["review_summary"]["severity_counts"] == {}
        # A clean review gets a celebratory +1 reaction directly on the PR.
        assert gh.reactions == [(7, "+1")]

    def test_review_with_findings_does_not_react(self, review_app) -> None:
        # The +1 reaction is reserved for a truly clean review — a review that
        # found (and posted) findings must not also get the "all good" reaction.
        # Set agent_output explicitly (rather than relying on the fixture's
        # default) so this test stays correct even if that default ever changes.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[_FakeReviewIssue("high", line=2, description="a finding")]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert gh.reactions == []
        assert gh.comments == []
        assert job["review_summary"]["comment_findings"] == 0
        # The finding was actually posted (as a line-anchored inline comment,
        # since line=2 on a.py is in-diff) -- not silently dropped, which would
        # also produce no reaction/comments and pass the assertions above.
        line_anchored = [
            c
            for rev in gh.reviews
            for c in rev.get("comments", [])
            if c.get("path") == "a.py" and c.get("line") == 2
        ]
        assert len(line_anchored) == 1
        assert "a finding" in line_anchored[0]["body"]

    def test_clean_review_reaction_failure_still_completes(self, review_app) -> None:
        # The +1 reaction is a best-effort courtesy: a failure adding it (rate
        # limit, missing scope, transport error) must not turn an otherwise
        # successful clean review into a failed job.
        review_app["github"]["agent_output"] = _FakeOutput(issues=[])
        gh = review_app["github"]["client"]
        gh.reaction_fail = True
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert len(gh.submitted_reviews) == 1
        assert gh.reactions == []

    def test_non_422_review_error_marks_job_failed_not_degraded(self, review_app) -> None:
        """A non-422 review failure (e.g. 403) must fail the job, not degrade."""
        gh = review_app["github"]["client"]
        gh.review_fail_times = 1
        gh.review_fail_status = 403
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # The error propagated before any degradation: no review was posted and no
        # finding was quietly re-routed to a file-level comment.
        assert gh.submitted_reviews == []
        assert gh.review_comments == []
        # The failure surfaces on the PR as the neutral outage note only — never
        # the raw 403 / exception text (graceful degradation).
        assert any(
            "could not complete and did not post findings" in body for _n, body in gh.comments
        )
        assert not any("403" in body for _n, body in gh.comments)
        assert not any("rate limited" in body for _n, body in gh.comments)
        assert not any("code review failed" in body for _n, body in gh.comments)

    def test_non_422_file_comment_error_marks_job_failed_not_degraded(self, review_app) -> None:
        """A non-422 file-comment failure must fail the job, not fall through."""
        gh = review_app["github"]["client"]
        gh.review_comment_fail_paths = {"a.py"}
        gh.review_comment_fail_status = 403
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue(
                    "low", line=999, file_path="a.py", description="off-hunk line in changed file"
                )
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # The finding was NOT silently posted as a standalone conversation comment;
        # only the failure notice (which never carries the finding text) is allowed.
        assert not any("off-hunk line in changed file" in body for _n, body in gh.comments)

    def test_all_changed_files_are_reviewed_without_cap(self, review_app) -> None:
        gh = review_app["github"]["client"]
        # Many changed files: every reviewable one must be reviewed — there is
        # no per-PR file cap. The coordinator chunks large input rather than
        # dropping files.
        gh.files = [
            PullRequestFile(
                f"mod_{i}.py", "modified", f"@@ -1,2 +1,3 @@\n c\n+x{i}\n d", 1, 0, None
            )
            for i in range(120)
        ]
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["files_reviewed"] == 120
        assert "files_skipped" not in job["review_summary"]
        # No partial-coverage disclosure is appended when nothing was skipped.
        assert "were not inspected" not in gh.reviews[-1]["body"]


class TestReviewEndpointExistingComments:
    """The review endpoint recognizes findings already on the PR (see
    coding_team.github_source.existing_comments): a match against an already
    RESOLVED comment is dropped, a match against a still-open one is kept and
    cross-referenced, and a lookup failure degrades gracefully rather than
    failing the review. Default fixture output is two findings on a.py:
    line=2 "high"/"desc" (in-diff) and line=999 "low"/"desc" (file-level).
    """

    def test_drops_finding_matching_resolved_existing_comment(self, review_app) -> None:
        gh = review_app["github"]["client"]
        gh.existing_review_comments = [
            ReviewComment(
                id=1, path="a.py", line=2, body="desc", html_url="https://example/comment/1"
            )
        ]
        gh.existing_resolved_ids = {1}

        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["addressed_issues_dropped"] == 1
        # Only the still-unmatched line=999 finding remains: no line-anchored
        # comment survives, and it posts as the sole file-level comment.
        assert job["review_summary"]["total_issues"] == 1
        assert gh.reviews[0]["comments"] == []
        assert gh.reviews[0]["event"] == "COMMENT"  # the dropped finding was the only blocking one
        assert len(gh.review_comments) == 1

    def test_keeps_and_references_finding_matching_unresolved_existing_comment(
        self, review_app
    ) -> None:
        gh = review_app["github"]["client"]
        gh.existing_review_comments = [
            ReviewComment(
                id=1, path="a.py", line=2, body="desc", html_url="https://example/comment/1"
            )
        ]
        # Not in existing_resolved_ids => still open.

        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["addressed_issues_dropped"] == 0
        assert job["review_summary"]["total_issues"] == 2
        line_comments = [c for c in gh.reviews[0]["comments"] if "line" in c]
        assert len(line_comments) == 1
        assert "https://example/comment/1" in line_comments[0]["body"]

    def test_existing_comment_fetch_failure_degrades_gracefully(self, review_app) -> None:
        gh = review_app["github"]["client"]
        gh.fetch_existing_comments_fail = True

        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["addressed_issues_dropped"] == 0
        assert job["review_summary"]["total_issues"] == 2

    def test_clean_review_skips_existing_comment_fetch(self, review_app) -> None:
        gh = review_app["github"]["client"]
        review_app["github"]["agent_output"] = _FakeOutput(issues=[])

        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["total_issues"] == 0
        assert job["review_summary"]["addressed_issues_dropped"] == 0
        # No findings to de-duplicate: the existing-comment fetch must not run.
        assert gh.list_review_comments_calls == 0


class TestReviewPersistence:
    """The review flow records a code_review_runs row on start and keeps it in
    lockstep with job state. These tests capture the store calls (the real store
    is a best-effort no-op without Postgres) and exercise GET /reviews."""

    def _capture(self, review_app, monkeypatch) -> tuple[list, list]:
        api_main = review_app["api"]
        starts: list = []
        updates: list = []
        monkeypatch.setattr(
            api_main, "record_review_start", lambda *a, **kw: starts.append((a, kw))
        )
        monkeypatch.setattr(api_main, "update_review", lambda *a, **kw: updates.append((a, kw)))
        return starts, updates

    def test_happy_path_persists_start_and_completion(self, review_app, monkeypatch) -> None:
        starts, updates = self._capture(review_app, monkeypatch)
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        # Started exactly once with the PR identity (job_id, owner, repo, pr_number, ...).
        assert len(starts) == 1
        start_args = starts[0][0]
        assert start_args[:4] == (job_id, "o", "r", 7)
        # Status transitions persisted: running then completed (completed flagged).
        statuses = [kw.get("status") for (_a, kw) in updates]
        assert "running" in statuses
        assert "completed" in statuses
        completed = [kw for (_a, kw) in updates if kw.get("status") == "completed"][0]
        assert completed["completed"] is True
        assert completed["review_summary"]["inline_comments"] == 1

    def test_failure_persists_failed_status(self, review_app, monkeypatch) -> None:
        _starts, updates = self._capture(review_app, monkeypatch)
        review_app["github"]["agent_output"] = RuntimeError("llm down")
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        statuses = [kw.get("status") for (_a, kw) in updates]
        assert "failed" in statuses

    def test_get_reviews_lists_persisted_runs(self, review_app, monkeypatch) -> None:
        from datetime import datetime, timezone

        api_main = review_app["api"]
        rows = [
            {
                "job_id": "j1",
                "owner": "o",
                "repo": "r",
                "pr_number": 7,
                "pr_url": "https://x/pull/7",
                "status": "completed",
                "status_text": "done",
                "review_summary": {
                    "total_issues": 1,
                    "inline_comments": 1,
                    "comment_findings": 0,
                    "event": "COMMENT",
                },
                "error": None,
                "author": "alice",
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "completed_at": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            }
        ]
        captured: dict = {}

        def _fake_list(owner, repo, pr_number=None, *, limit=500):
            captured["args"] = (owner, repo, pr_number, limit)
            return rows

        monkeypatch.setattr(api_main, "list_reviews", _fake_list)
        resp = review_app["client"].get(
            "/reviews", params={"owner": "o", "repo": "r", "pr_number": 7}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["job_id"] == "j1"
        assert data[0]["review_summary"]["event"] == "COMMENT"
        assert captured["args"] == ("o", "r", 7, 500)

    def test_get_reviews_rejects_out_of_range_limit(self, review_app) -> None:
        # limit is validated at the API layer (1..2000); out-of-range -> 422.
        assert (
            review_app["client"]
            .get("/reviews", params={"owner": "o", "repo": "r", "limit": 0})
            .status_code
            == 422
        )
        assert (
            review_app["client"]
            .get("/reviews", params={"owner": "o", "repo": "r", "limit": 3000})
            .status_code
            == 422
        )


# ---------------------------------------------------------------------------
# BUG CONDITION REGRESSION TESTS
#
# Regression guards for _partition_review_issues routing: off-diff findings
# without an explicit pre_existing tag are posted as standalone conversation
# comments naming their own file_path — never mis-anchored onto an unrelated
# changed file, and never forced into pending_issue_proposals by file
# membership alone. Validates Requirements 2.1–2.5.
# ---------------------------------------------------------------------------


class TestBugConditionExploration:
    """Exploration tests written BEFORE the fix as part of the bugfix workflow (Task 1).

    Originally encoded round-1 "route every off-diff-file finding to a proposal"
    behavior; updated for round 2 (see the section comment above) to encode
    the correct behavior instead: an off-diff-file finding WITHOUT an explicit
    pre_existing tag becomes its own standalone conversation comment -- never a
    proposal, and never a comment mis-anchored to an unrelated changed file.

    Why keep these alongside TestReviewEndpoint?
    - TestReviewEndpoint tests are the canonical integration suite.  They cover the
      same scenarios.
    - TestBugConditionExploration tests preserve the exact counterexample
      documentation from the original bugfix workflow, so future readers can see
      precisely what each historical bug looked like and what condition each test
      was designed to catch.
    - test_422_dropped_comments_reanchored_not_standalone is untouched by the
      round-2 change (it concerns only in-diff findings dropped by a failed 422
      review submission, not off-diff-file routing) and still guards that
      regression exactly as before.
    - If a regression reintroduces mis-anchored file-level posting, or silently
      drops an untagged off-diff finding to a proposal, both suites will catch
      it; the exploration tests' error messages include "BUG CONFIRMED" to make
      the failure immediately recognisable.

    Counterexamples captured on unfixed code (Task 1 documentation):
      - test_leftover_finding_becomes_standalone_comment_not_misanchored:
          (interim-fix bug) review_comments carried a subject_type="file" entry
          anchored to "a.py" (the PR's only changed file), not the finding's own
          "src/config.py" -- misattributing the finding to unrelated code.
          (round-1 bug) the finding was silently routed to pending_issue_proposals
          instead of being posted at all.

      - test_empty_file_path_finding_becomes_standalone_not_misanchored:
          same two bugs, for a finding with no file_path at all.

      - test_422_dropped_comments_reanchored_not_standalone:
          gh.comments contains bodies from inline_comment_to_timeline_body(c)
          review_summary["comment_findings"] = 2  (both inline findings dropped)

      - test_multiple_leftovers_each_get_their_own_standalone_comment:
          same two bugs, for two findings sharing no changed file.
    """

    def test_leftover_finding_becomes_standalone_comment_not_misanchored(self, review_app) -> None:
        """A finding whose file_path is NOT in the PR diff, and which the reviewer did
        NOT tag pre_existing, is an in-scope PR finding (round 2): since it cannot be
        anchored to any diff location, it must be posted as its own standalone
        conversation comment naming its own file. It must NEVER be re-anchored as a
        file-level review comment against an unrelated changed file (the interim
        "anchor to first file" bug), and it must NEVER be silently dropped to a
        proposal either (the round-1 regression Codex flagged).

        On code with EITHER now-fixed bug this test FAILS:
          - review_comments carries a subject_type="file" entry mis-anchored to
            "a.py" (the PR's only changed file, not the finding's own file), OR
          - the finding was silently routed to pending_issue_proposals instead of
            being posted as a standalone comment.
        """
        # The PR diff only touches "a.py" (set up by _FakeReviewClient.files).
        # The finding names "src/config.py" which is absent from the diff.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue(
                    "low",
                    line=4,
                    file_path="src/config.py",
                    description="Security issue in config",
                )
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200

        gh = review_app["github"]["client"]

        # Never mis-anchored to an unrelated changed file.
        file_level = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert file_level == [], (
            f"BUG CONFIRMED — out-of-diff finding mis-anchored as a file-level "
            f"comment: review_comments = {gh.review_comments}"
        )

        # Posted as its own standalone comment naming its own file, never dropped.
        assert len(gh.comments) == 1, (
            f"BUG CONFIRMED — finding not posted as a standalone comment: gh.comments = {gh.comments}"
        )
        assert "Security issue in config" in gh.comments[0][1]
        assert "`src/config.py`" in gh.comments[0][1]

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["review_summary"]["comment_findings"] == 1
        # Never routed to a proposal -- only an explicit pre_existing=True tag does that.
        assert job["review_summary"]["pending_issue_proposals"] == []

    def test_empty_file_path_finding_becomes_standalone_not_misanchored(self, review_app) -> None:
        """A finding with an empty or None file_path cannot resolve into the diff at
        all, but (round 2) is still an in-scope PR finding absent an explicit
        pre_existing tag: it must become its own standalone comment, never anchored
        to the first changed file in the diff as a file-level comment, and never
        silently dropped to a proposal.

        On code with EITHER now-fixed bug this test FAILS:
          - review_comments carries a subject_type="file" entry for the finding, OR
          - the finding was silently routed to pending_issue_proposals instead of
            being posted as a standalone comment.
        """
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue(
                    "low",
                    line=None,
                    file_path="",  # empty file_path — no anchor at all
                    description="No file path finding",
                )
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200

        gh = review_app["github"]["client"]

        # Never posted as a file-level comment.
        file_level = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert file_level == [], (
            f"BUG CONFIRMED — empty-file_path finding mis-anchored as a file-level "
            f"comment: review_comments = {gh.review_comments}"
        )

        # Posted as a standalone comment (no file location prefix, since file_path
        # is empty), never dropped to a proposal.
        assert len(gh.comments) == 1, (
            f"BUG CONFIRMED — finding not posted as a standalone comment: gh.comments = {gh.comments}"
        )
        assert "No file path finding" in gh.comments[0][1]
        assert not gh.comments[0][1].startswith("`")

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["review_summary"]["comment_findings"] == 1
        assert job["review_summary"]["pending_issue_proposals"] == []

    def test_multiple_leftovers_each_get_their_own_standalone_comment(self, review_app) -> None:
        """Multiple findings whose files are not in the PR diff, without a
        pre_existing tag, must each become their OWN standalone comment -- never
        mis-anchored to the same first changed file, never merged into one comment,
        and never silently dropped to a proposal.

        On code with EITHER now-fixed bug this test FAILS:
          - review_comments carries subject_type="file" entries for the findings, OR
          - the findings were silently routed to pending_issue_proposals instead of
            being posted as standalone comments.
        """
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=1, file_path="gone1.py", description="issue one"),
                _FakeReviewIssue("low", line=2, file_path="gone2.py", description="issue two"),
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200

        gh = review_app["github"]["client"]

        # Neither finding may appear as a file-level comment.
        file_level = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert file_level == [], (
            f"BUG CONFIRMED — out-of-diff findings mis-anchored as file-level "
            f"comments: review_comments = {gh.review_comments}"
        )

        # Each finding gets its own standalone comment; neither is dropped or merged.
        assert len(gh.comments) == 2, (
            f"BUG CONFIRMED — {len(gh.comments)} standalone comment(s) posted for leftover "
            f"findings (expected 2): gh.comments = {gh.comments}"
        )
        bodies = [body for _n, body in gh.comments]
        assert sum("issue one" in b for b in bodies) == 1
        assert sum("issue two" in b for b in bodies) == 1
        for b in bodies:
            assert not ("issue one" in b and "issue two" in b)

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["review_summary"]["comment_findings"] == 2
        assert job["review_summary"]["pending_issue_proposals"] == []

    def test_422_dropped_comments_reanchored_not_standalone(self, review_app) -> None:
        """When the full-batch review 422s on both the event and COMMENT attempts,
        the line-anchored comments MUST NOT be reposted via add_issue_comment as
        standalone top-level comments.  They must be re-submitted inline (via the
        summary-only + bisection path) so every finding keeps a review anchor.

        On UNFIXED code this test FAILED:
          - gh.comments had entries for the dropped inline findings
          - review_summary["comment_findings"] == 2  (not 0)
        """
        gh = review_app["github"]["client"]
        # Trigger both full-batch attempts to 422, forcing the summary-only +
        # bisection path where the inline comments are re-posted, not dropped.
        gh.review_fail_times = 2

        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200

        # EXPECTED: no standalone comments for the findings.
        assert gh.comments == [], (
            f"BUG CONFIRMED — findings reposted as standalone comments: gh.comments = {gh.comments}"
        )

        # The in-diff line survives as a line-anchored comment in a posted review.
        posted_line_comments = [
            c for rev in gh.submitted_reviews for c in rev.get("comments", []) if "line" in c
        ]
        assert any(c["line"] == 2 for c in posted_line_comments), (
            f"Expected the in-diff line to be re-posted inline, got reviews={gh.submitted_reviews}"
        )

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["review_summary"]["comment_findings"] == 0, (
            f"BUG CONFIRMED — comment_findings = {job['review_summary']['comment_findings']}, expected 0"
        )
        assert job["status"] == "completed"


# ---------------------------------------------------------------------------
# PRESERVATION PROPERTY TESTS  (Task 2 — MUST PASS on UNFIXED code)
#
# These tests verify that CORRECTLY-ROUTED findings are unaffected by the
# upcoming fix.  They encode baseline behavior observed on the unfixed code
# and must continue to pass both before and after the fix is applied.
#
# The diff for "a.py" in _FakeReviewClient is:
#   "@@ -1,2 +1,3 @@\n ctx\n+added\n more"
# parse_valid_lines produces valid new-file line numbers: {1, 2, 3}
#   line 1 — context ("ctx")
#   line 2 — added  ("+added")
#   line 3 — context ("more")
#
# Observation methodology (task 2 requirement):
#   - PBT A: line IN {1,2,3}, file="a.py" → line-anchored comment
#     side="RIGHT", path="a.py", line=N.  Verified on unfixed code: PASSES.
#   - PBT B: line NOT in {1,2,3}, file="a.py" → file-level comment
#     subject_type="file", path="a.py", no "line" key.  Verified: PASSES.
#   - PBT C: severity combinations drive correct event.  Verified: PASSES.
#   - PBT D: mixed file_path lists — routing of in-diff paths unchanged.
#     Verified: PASSES.
#
# Hypothesis is not available in this environment; each property is covered
# by ≥10 parameterised concrete representative cases.
#
# Validates: Requirements 3.1, 3.2, 3.3, 3.4
# ---------------------------------------------------------------------------


class TestPreservationProperties:
    """Property-based preservation tests verifying that correctly-routed findings
    are UNCHANGED by the bugfix.  All tests MUST PASS on the UNFIXED code.

    The four properties verified:

    PBT A  — Line-anchored routing preserved (Req 3.1)
             Findings whose file="a.py" and line ∈ {1,2,3} (the diff hunk)
             must produce a review comment with side="RIGHT", correct path and
             line.

    PBT B  — File-level routing preserved (Req 3.2)
             Findings whose file="a.py" but line ∉ {1,2,3} (or line is None)
             must produce a review comment with subject_type="file" and correct
             path; "line" must not appear in that comment.

    PBT C  — Review event selection preserved (Req 3.3, 3.4)
             choose_event / _submit_review must produce REQUEST_CHANGES when
             any finding is critical/high and reviewer≠author, COMMENT otherwise.

    PBT D  — Mixed-path routing invariant (Req 3.1, 3.2)
             When a list of findings includes both in-diff and out-of-diff paths,
             the routing of the in-diff findings is byte-for-byte identical to
             what it would be with only in-diff findings present.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _post_review(review_app, issues):
        """Post /review-pr with the given issues and return (resp, gh, job)."""
        review_app["github"]["agent_output"] = _FakeOutput(issues=issues)
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        return resp, gh, job

    # ------------------------------------------------------------------
    # PBT A — Line-anchored routing preserved (Req 3.1)
    # Property: for any finding with file_path in valid_by_path AND
    #           line ∈ valid diff lines, the submitted review must contain
    #           exactly one comment with side="RIGHT", path == file_path,
    #           and line == the finding's line.
    # ------------------------------------------------------------------

    # Concrete representatives: (severity, line)
    # Diff hunk lines for a.py: {1, 2, 3}
    _PBT_A_CASES = [
        # (severity, line, description)
        ("high", 1, "context line 1 high severity"),
        ("high", 2, "added line 2 high severity"),
        ("high", 3, "context line 3 high severity"),
        ("low", 1, "context line 1 low severity"),
        ("low", 2, "added line 2 low severity"),
        ("low", 3, "context line 3 low severity"),
        ("critical", 1, "critical line 1"),
        ("critical", 2, "critical line 2"),
        ("critical", 3, "critical line 3"),
        ("medium", 1, "medium line 1"),
        ("medium", 2, "medium line 2"),
        ("medium", 3, "medium line 3"),
        ("info", 1, "info line 1"),
        ("info", 2, "info line 2"),
        ("info", 3, "info line 3"),
    ]

    @pytest.mark.parametrize("severity,line,description", _PBT_A_CASES)
    def test_pbt_a_line_anchored_routing_preserved(
        self, review_app, severity: str, line: int, description: str
    ) -> None:
        """PBT A — Line-anchored routing preserved.

        Property: a finding with file_path="a.py" and line ∈ {1,2,3} (valid
        diff hunk) must produce a review comment with:
          - side="RIGHT"
          - path=="a.py"
          - line==<finding line>
        This is the Req 3.1 invariant: correctly-routed findings are unchanged.
        Validates: Requirements 3.1
        """
        _resp, gh, _job = self._post_review(
            review_app,
            [_FakeReviewIssue(severity, line=line, file_path="a.py", description=description)],
        )

        assert len(gh.reviews) >= 1, "Expected at least one review submission"
        # Collect all comments across all review attempts (the last successful one
        # may be the one with comments if the first 422'd).
        all_comments = []
        for rev in gh.reviews:
            all_comments.extend(rev.get("comments", []))

        line_anchored = [
            c
            for c in all_comments
            if c.get("side") == "RIGHT" and c.get("path") == "a.py" and c.get("line") == line
        ]
        assert len(line_anchored) == 1, (
            f"PRESERVATION BROKEN — expected exactly 1 line-anchored comment "
            f"(side=RIGHT, path=a.py, line={line}) for severity={severity}, "
            f"but found {len(line_anchored)}. all_comments={all_comments}"
        )
        # The body must contain the description text (not empty/swapped).
        assert description in line_anchored[0]["body"], (
            f"PRESERVATION BROKEN — comment body does not contain description. "
            f"body={line_anchored[0]['body']!r}, expected to contain {description!r}"
        )
        # In-diff line-anchored findings must NOT also be posted as file-level
        # comments -- check both the dedicated file-comment endpoint and any
        # file-level entry embedded in a submitted review's own comments array.
        duplicate_file_level = [
            c
            for c in gh.review_comments
            if c.get("subject_type") == "file" and c.get("path") == "a.py"
        ] + [
            c
            for rev in gh.reviews
            for c in rev.get("comments", [])
            if c.get("subject_type") == "file" and c.get("path") == "a.py"
        ]
        assert len(duplicate_file_level) == 0, (
            f"PRESERVATION BROKEN — in-diff line-anchored finding also produced a "
            f"file-level comment: {duplicate_file_level}"
        )

    # ------------------------------------------------------------------
    # PBT B — File-level routing preserved (Req 3.2)
    # Property: for any finding with file_path in valid_by_path AND
    #           line NOT in valid diff lines (or line is None), the submitted
    #           review must contain exactly one comment with
    #           subject_type="file", path=="a.py", no "line" key.
    # ------------------------------------------------------------------

    # Concrete representatives: (severity, line_or_none, description)
    # Diff hunk lines for a.py: {1, 2, 3}; off-diff means anything else
    _PBT_B_CASES = [
        # (severity, line, description)
        ("high", None, "no line high severity"),
        ("high", 4, "line 4 off-diff high"),
        ("high", 10, "line 10 off-diff high"),
        ("high", 100, "line 100 off-diff high"),
        ("high", 999, "line 999 off-diff high"),
        ("low", None, "no line low severity"),
        ("low", 4, "line 4 off-diff low"),
        ("low", 50, "line 50 off-diff low"),
        ("low", 500, "line 500 off-diff low"),
        ("critical", None, "no line critical severity"),
        ("critical", 7, "line 7 off-diff critical"),
        ("medium", None, "no line medium severity"),
        ("medium", 20, "line 20 off-diff medium"),
        ("info", None, "no line info severity"),
        ("info", 999, "line 999 off-diff info"),
    ]

    @pytest.mark.parametrize("severity,line,description", _PBT_B_CASES)
    def test_pbt_b_file_level_routing_preserved(
        self, review_app, severity: str, line, description: str
    ) -> None:
        """PBT B — File-level routing preserved.

        Property: a finding with file_path="a.py" and line NOT in {1,2,3}
        (or line=None) must produce a review comment with:
          - subject_type=="file"
          - path=="a.py"
          - no "line" key in the comment dict
        This is the Req 3.2 invariant: correctly-routed file-level comments
        are unchanged.
        Validates: Requirements 3.2
        """
        _resp, gh, _job = self._post_review(
            review_app,
            [_FakeReviewIssue(severity, line=line, file_path="a.py", description=description)],
        )

        assert len(gh.reviews) >= 1, "Expected at least one review submission"
        # File-level comments are posted on the dedicated endpoint, not in the
        # review's comments array.
        file_level = [
            c
            for c in gh.review_comments
            if c.get("subject_type") == "file" and c.get("path") == "a.py"
        ]
        assert len(file_level) == 1, (
            f"PRESERVATION BROKEN — expected exactly 1 file-level comment "
            f"(subject_type=file, path=a.py) for severity={severity}, line={line}, "
            f"but found {len(file_level)}. review_comments={gh.review_comments}"
        )
        assert "line" not in file_level[0], (
            f"PRESERVATION BROKEN — file-level comment must not have 'line' key. "
            f"comment={file_level[0]}"
        )
        assert description in file_level[0]["body"], (
            f"PRESERVATION BROKEN — comment body does not contain description. "
            f"body={file_level[0]['body']!r}, expected to contain {description!r}"
        )
        # The same finding must not ALSO show up as a line-anchored comment —
        # file-level routing must be exclusive, not additive.
        all_comments = []
        for rev in gh.reviews:
            all_comments.extend(rev.get("comments", []))
        if line is None:
            line_anchored = [c for c in all_comments if c.get("path") == "a.py" and "line" in c]
        else:
            line_anchored = [
                c for c in all_comments if c.get("path") == "a.py" and c.get("line") == line
            ]
        assert len(line_anchored) == 0, (
            f"PRESERVATION BROKEN — off-diff finding should not also produce a "
            f"line-anchored comment, but found {line_anchored}."
        )

    # ------------------------------------------------------------------
    # PBT C — Review event selection preserved (Req 3.3, 3.4)
    # Property: choose_event / _submit_review produce REQUEST_CHANGES when
    #           any finding is critical/high AND reviewer≠author, COMMENT
    #           otherwise.
    # ------------------------------------------------------------------

    # (reviewer_is_author, severities, expected_event, description)
    _PBT_C_CASES = [
        # reviewer == author → always COMMENT regardless of severity
        (True, ["critical"], "COMMENT", "self-review critical"),
        (True, ["high"], "COMMENT", "self-review high"),
        (True, ["critical", "high"], "COMMENT", "self-review critical+high"),
        (True, ["low"], "COMMENT", "self-review low only"),
        (True, ["medium"], "COMMENT", "self-review medium only"),
        (True, ["info"], "COMMENT", "self-review info only"),
        # reviewer ≠ author, blocking severity → REQUEST_CHANGES
        (False, ["critical"], "REQUEST_CHANGES", "critical → REQUEST_CHANGES"),
        (False, ["high"], "REQUEST_CHANGES", "high → REQUEST_CHANGES"),
        (False, ["critical", "low"], "REQUEST_CHANGES", "critical+low → REQUEST_CHANGES"),
        (False, ["high", "medium"], "REQUEST_CHANGES", "high+medium → REQUEST_CHANGES"),
        (False, ["critical", "high"], "REQUEST_CHANGES", "critical+high → REQUEST_CHANGES"),
        # reviewer ≠ author, no blocking severity → COMMENT
        (False, ["low"], "COMMENT", "low only → COMMENT"),
        (False, ["medium"], "COMMENT", "medium only → COMMENT"),
        (False, ["info"], "COMMENT", "info only → COMMENT"),
        (False, ["low", "medium"], "COMMENT", "low+medium → COMMENT"),
        (False, ["low", "info"], "COMMENT", "low+info → COMMENT"),
    ]

    @pytest.mark.parametrize(
        "reviewer_is_author,severities,expected_event,description", _PBT_C_CASES
    )
    def test_pbt_c_event_selection_preserved(
        self,
        review_app,
        reviewer_is_author: bool,
        severities: list,
        expected_event: str,
        description: str,
    ) -> None:
        """PBT C — Review event selection preserved.

        Property: the review event submitted to GitHub is REQUEST_CHANGES iff
        (a) any finding has severity critical/high AND (b) reviewer≠author.
        Otherwise it is COMMENT.
        This is the Req 3.3/3.4 invariant: event selection logic unchanged.
        Validates: Requirements 3.3, 3.4
        """
        gh = review_app["github"]["client"]
        # Configure whether reviewer == author. The _FakeReviewClient defaults
        # author="alice" and login="khala-bot" (different).  Set login to "alice"
        # to simulate a self-review.
        if reviewer_is_author:
            gh.login = "alice"  # reviewer == author
        else:
            gh.login = "khala-bot"  # reviewer != author (default)

        issues = [
            _FakeReviewIssue(sev, line=2, file_path="a.py", description=f"{description} sev={sev}")
            for sev in severities
        ]
        _resp, gh2, _job = self._post_review(review_app, issues)

        # The event is the one used in the LAST SUCCESSFUL review attempt.
        assert len(gh2.reviews) >= 1, "Expected at least one review submission"
        submitted_event = gh2.reviews[-1]["event"]
        assert submitted_event == expected_event, (
            f"PRESERVATION BROKEN — event selection incorrect for {description}. "
            f"Expected {expected_event!r}, got {submitted_event!r}. "
            f"All submitted events: {[r['event'] for r in gh2.reviews]}"
        )

    # ------------------------------------------------------------------
    # PBT D — Mixed-path routing invariant (Req 3.1, 3.2)
    # Property: when a list of findings includes both in-diff and out-of-diff
    #           file_path values, the routing of the in-diff findings is
    #           byte-for-byte identical to routing them alone.
    # ------------------------------------------------------------------

    # Each case is a list of (severity, line, file_path, description) tuples.
    # The "in-diff" finding is always file_path="a.py"; the others are out-of-diff.
    _PBT_D_CASES = [
        # description, in_diff_spec, out_of_diff_specs
        (
            "one in-diff line-anchored + one out-of-diff",
            ("high", 2, "a.py", "in-diff line-anchored"),
            [("low", 4, "gone.py", "out-of-diff")],
        ),
        (
            "one in-diff file-level + one out-of-diff",
            ("low", 999, "a.py", "in-diff file-level"),
            [("high", 1, "absent.py", "out-of-diff")],
        ),
        (
            "in-diff line-anchored + two out-of-diff",
            ("medium", 3, "a.py", "in-diff line 3"),
            [
                ("low", 2, "missing1.py", "out-of-diff 1"),
                ("high", 1, "missing2.py", "out-of-diff 2"),
            ],
        ),
        (
            "in-diff file-level + empty-path finding",
            ("low", 50, "a.py", "in-diff file-level"),
            [("info", None, "", "empty file_path")],
        ),
        (
            "in-diff file-level + none-path finding",
            ("low", 100, "a.py", "in-diff file-level 100"),
            [("info", None, None, "None file_path")],
        ),
        (
            "critical in-diff line 1 + out-of-diff",
            ("critical", 1, "a.py", "critical line 1"),
            [("low", 9, "other.py", "out-of-diff low")],
        ),
        (
            "high in-diff line 2 + two out-of-diff",
            ("high", 2, "a.py", "high line 2"),
            [
                ("medium", 5, "x.py", "out x"),
                ("low", 3, "y.py", "out y"),
            ],
        ),
        (
            "low in-diff file-level + three out-of-diff",
            ("low", 200, "a.py", "low 200"),
            [
                ("high", 1, "f1.py", "f1 high"),
                ("high", 2, "f2.py", "f2 high"),
                ("low", 3, "f3.py", "f3 low"),
            ],
        ),
        (
            "info in-diff line 3 + out-of-diff",
            ("info", 3, "a.py", "info line 3"),
            [("critical", 5, "crit.py", "crit out-of-diff")],
        ),
        (
            "medium in-diff file-level + empty and absent",
            ("medium", 77, "a.py", "medium 77"),
            [
                ("low", None, "", "empty path"),
                ("high", 1, "not_there.py", "absent path"),
            ],
        ),
    ]

    @pytest.mark.parametrize("description,in_diff_spec,out_of_diff_specs", _PBT_D_CASES)
    def test_pbt_d_mixed_path_in_diff_routing_unchanged(
        self,
        review_app,
        description: str,
        in_diff_spec: tuple,
        out_of_diff_specs: list,
    ) -> None:
        """PBT D — Mixed-path routing invariant.

        Property: when findings include both in-diff (file_path in valid_by_path)
        and out-of-diff paths, the routing of the in-diff finding is byte-for-byte
        identical to routing it in isolation, and no extra comment is created on
        the in-diff path (guarding against an out-of-diff finding being
        re-anchored onto it).

        Concretely:
          1. Submit ONLY the in-diff finding → capture its review comment shape.
          2. Submit the in-diff finding TOGETHER with out-of-diff findings → capture
             the same in-diff finding's review comment shape.
          3. Assert the two shapes are identical (path, line/subject_type, side).

        This is the core Req 3.1/3.2 preservation invariant: adding out-of-diff
        findings to the list must not perturb the routing of in-diff ones.
        Validates: Requirements 3.1, 3.2
        """
        in_sev, in_line, in_path, in_desc = in_diff_spec

        # --- Step 1: route the in-diff finding alone ---
        solo_issues = [
            _FakeReviewIssue(in_sev, line=in_line, file_path=in_path, description=in_desc)
        ]
        _r1, gh1, _j1 = self._post_review(review_app, solo_issues)

        # A finding is routed to either the review (line-anchored) or the dedicated
        # file-comment endpoint (file-level); aggregate both so we can locate its
        # comment regardless of which endpoint was used.
        all_comments_solo: list = []
        for rev in gh1.reviews:
            all_comments_solo.extend(rev.get("comments", []))
        all_comments_solo.extend(gh1.review_comments)

        # Find the in-diff finding's comment in the solo run.
        in_diff_solo = [c for c in all_comments_solo if c.get("path") == in_path]
        assert len(in_diff_solo) == 1, (
            f"[{description}] Solo run: expected exactly 1 comment for in-diff finding "
            f"(path={in_path!r}), got {len(in_diff_solo)}. comments={all_comments_solo}"
        )
        solo_shape = {
            "path": in_diff_solo[0].get("path"),
            "side": in_diff_solo[0].get("side"),
            "line": in_diff_solo[0].get("line"),
            "subject_type": in_diff_solo[0].get("subject_type"),
        }

        # --- Step 2: route the in-diff finding TOGETHER with out-of-diff findings ---
        # Recreate a fresh _FakeReviewClient for the second run (review_app reuses
        # the same fixture; reset the client).
        review_app["github"]["client"] = _FakeReviewClient()

        out_issues = [
            _FakeReviewIssue(
                sev,
                line=ln,
                file_path=fp,
                description=desc,
            )
            for sev, ln, fp, desc in out_of_diff_specs
        ]
        mixed_issues = solo_issues + out_issues
        _r2, gh2, _j2 = self._post_review(review_app, mixed_issues)

        all_comments_mixed: list = []
        for rev in gh2.reviews:
            all_comments_mixed.extend(rev.get("comments", []))
        all_comments_mixed.extend(gh2.review_comments)

        # Find the in-diff finding's comment in the mixed run.
        in_diff_mixed = [
            c
            for c in all_comments_mixed
            if c.get("path") == in_path
            and (
                # line-anchored: same line
                (in_line is not None and c.get("line") == in_line)
                # file-level: subject_type=file (no line key)
                or (c.get("subject_type") == "file" and "line" not in c)
            )
        ]
        assert len(in_diff_mixed) == 1, (
            f"[{description}] Mixed run: expected exactly 1 comment for in-diff finding "
            f"(path={in_path!r}, line={in_line}), got {len(in_diff_mixed)}. "
            f"all_comments={all_comments_mixed}"
        )
        # No extra comment on the in-diff path either -- guards against an
        # out-of-diff finding being re-anchored onto in_path under a different
        # line/shape that the filter above wouldn't otherwise catch.
        mixed_on_path = [c for c in all_comments_mixed if c.get("path") == in_path]
        solo_on_path = [c for c in all_comments_solo if c.get("path") == in_path]
        assert len(mixed_on_path) == len(solo_on_path), (
            f"[{description}] Mixed run created extra comments on in-diff path {in_path!r}: "
            f"solo={solo_on_path}, mixed={mixed_on_path}"
        )
        # Each out-of-diff finding (none tagged pre_existing here) must appear
        # in at least one standalone conversation comment that names its file
        # when one is set, and never leak into the review/file-level comments
        # checked above.
        for issue in out_issues:
            path = issue.file_path or ""
            assert any(
                issue.description in body and (not path or path in body)
                for _n, body in gh2.comments
            ), (
                f"[{description}] out-of-diff finding {issue.description!r} "
                f"(file {path!r}) missing from standalone comments: {gh2.comments}"
            )
            assert not any(issue.description in c.get("body", "") for c in all_comments_mixed), (
                f"[{description}] out-of-diff finding {issue.description!r} leaked into "
                f"review/file-level comments: {all_comments_mixed}"
            )
        mixed_shape = {
            "path": in_diff_mixed[0].get("path"),
            "side": in_diff_mixed[0].get("side"),
            "line": in_diff_mixed[0].get("line"),
            "subject_type": in_diff_mixed[0].get("subject_type"),
        }

        # --- Step 3: assert shapes are identical ---
        assert solo_shape == mixed_shape, (
            f"PRESERVATION BROKEN [{description}] — routing of in-diff finding differs "
            f"when mixed with out-of-diff findings.\n"
            f"  Solo shape:  {solo_shape}\n"
            f"  Mixed shape: {mixed_shape}"
        )


# ---------------------------------------------------------------------------
# Task 3.5 — Integration tests for fixed _run_pr_review
# ---------------------------------------------------------------------------


class TestFixedRunPrReview:
    """Integration tests verifying _run_pr_review's handling of out-of-diff
    findings: never mis-anchored to an unrelated changed file, never dropped,
    and routed to a proposal ONLY when the reviewer explicitly tagged the
    finding pre_existing=True (round 2; see the section comment above
    TestBugConditionExploration for the full history).

    Validates: Requirements 2.1, 2.2, 2.4, 2.5
    """

    def test_off_diff_finding_without_tag_never_misanchored(self, review_app) -> None:
        """Full _run_pr_review with a finding naming a file outside the PR's diff,
        and no pre_existing tag: it is never posted as a file-level comment
        mis-anchored to an unrelated changed file, and it is never silently
        dropped -- it is posted as its own standalone conversation comment.

        A finding whose file_path is NOT in the PR diff (valid_by_path only
        contains "a.py") cannot be anchored to any diff location, so it must
        surface as a standalone comment naming its own file, not as a proposal
        and not mis-anchored onto "a.py".

        Validates: Requirements 2.1, 2.2, 2.5
        """
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue(
                    "low",
                    line=4,
                    file_path="not_in_diff.py",
                    description="leftover finding",
                )
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200

        gh = review_app["github"]["client"]

        # The finding must NOT be posted as a file-level comment — "a.py" is an
        # unrelated file the finding never named.
        file_level = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert file_level == [], (
            f"Expected 0 file-level review comments for the out-of-diff finding, "
            f"got {len(file_level)}. review_comments = {gh.review_comments}"
        )
        # Nor mis-anchored as a line comment on the unrelated changed file.
        assert not any(
            "leftover finding" in c.get("body", "")
            for review in gh.reviews
            for c in review.get("comments", [])
        ), f"Out-of-diff finding was mis-anchored as a line comment: {gh.reviews}"

        # Instead it is posted as its own standalone conversation comment.
        assert len(gh.comments) == 1, (
            f"Expected the finding to be posted as a standalone comment, got gh.comments = {gh.comments}"
        )
        assert "leftover finding" in gh.comments[0][1]
        assert "`not_in_diff.py`" in gh.comments[0][1]

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["comment_findings"] == 1
        assert job["review_summary"]["file_comments"] == 0
        assert job["review_summary"]["pending_issue_proposals"] == []

    def test_off_diff_file_finding_posted_standalone_alongside_others(self, review_app) -> None:
        """Mix of on-diff, off-diff-line, and off-diff-file findings: each is
        routed to the right shape and none is dropped.

        Three findings are submitted:
          1. On-diff (file="a.py", line=2)  → line-anchored inline comment.
          2. Off-diff-line (file="a.py", line=999) → file-level inline comment
             (the file itself is in the diff, only the cited line is not).
          3. Off-diff-file (file="not_in_diff.py", line=1), no pre_existing tag →
             cannot be anchored to any diff location, so it is posted as its own
             standalone conversation comment.

        Validates: Requirements 2.1, 2.2, 2.4, 2.5
        """
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=2, file_path="a.py", description="on-diff finding"),
                _FakeReviewIssue(
                    "low", line=999, file_path="a.py", description="off-diff-line finding"
                ),
                _FakeReviewIssue(
                    "medium",
                    line=1,
                    file_path="not_in_diff.py",
                    description="off-diff-file finding",
                ),
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200

        gh = review_app["github"]["client"]

        assert len(gh.reviews) == 1
        line_comments = [c for c in gh.reviews[0].get("comments", []) if c.get("side") == "RIGHT"]
        file_comments = [c for c in gh.review_comments if c.get("subject_type") == "file"]

        # on-diff finding → line-anchored
        assert len(line_comments) == 1, (
            f"Expected 1 line-anchored comment (on-diff), got {len(line_comments)}. "
            f"review comments = {gh.reviews[0].get('comments', [])}"
        )
        assert line_comments[0]["line"] == 2

        # off-diff-line (same file, off-diff line) → file-level; off-diff-file
        # is never file-anchored, so exactly one file-level comment, not two.
        assert len(file_comments) == 1, (
            f"Expected 1 file-level comment (off-diff-line only; off-diff-file must "
            f"never be file-anchored), got {len(file_comments)}. review_comments = {gh.review_comments}"
        )
        assert "off-diff-line finding" in file_comments[0]["body"]
        assert not any("off-diff-file finding" in c.get("body", "") for c in gh.review_comments)

        # off-diff-file finding → its own standalone conversation comment.
        assert len(gh.comments) == 1, (
            f"Expected the off-diff-file finding to be posted standalone, got gh.comments = {gh.comments}"
        )
        assert "off-diff-file finding" in gh.comments[0][1]

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["comment_findings"] == 1
        assert job["review_summary"]["pending_issue_proposals"] == []

    def test_off_diff_file_pre_existing_true_routes_to_proposal(self, review_app) -> None:
        """Off-diff-file findings tagged pre_existing=True (and not proven
        is_within_diff) route to a pending issue proposal and are never posted.
        """
        review_app["github"]["client"] = _FakeReviewClient()
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue(
                    "high",
                    line=3,
                    file_path="outside_the_diff.py",
                    description="out-of-scope bug (tagged)",
                    pre_existing=True,
                )
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        assert gh.comments == [], f"tagged pre_existing: standalone comment posted: {gh.comments}"
        assert gh.review_comments == [], (
            f"tagged pre_existing: file-level comment posted: {gh.review_comments}"
        )
        for review in gh.reviews:
            for c in review.get("comments", []):
                assert "out-of-scope bug" not in c.get("body", ""), (
                    f"tagged pre_existing: posted inline: {c}"
                )
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        proposals = job["review_summary"]["pending_issue_proposals"]
        assert len(proposals) == 1
        assert proposals[0]["description"] == "out-of-scope bug (tagged)"

    def test_off_diff_file_pre_existing_false_posts_standalone(self, review_app) -> None:
        """Off-diff-file findings left at default pre_existing=False are in-scope
        PR findings and are posted as their own standalone conversation comment.
        """
        review_app["github"]["client"] = _FakeReviewClient()
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue(
                    "high",
                    line=3,
                    file_path="outside_the_diff.py",
                    description="out-of-scope bug (untagged)",
                )
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        assert gh.review_comments == [], (
            f"untagged: file-level comment posted: {gh.review_comments}"
        )
        assert len(gh.comments) == 1, f"untagged: expected a standalone comment, got {gh.comments}"
        assert "out-of-scope bug (untagged)" in gh.comments[0][1]
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["pending_issue_proposals"] == []


# ---------------------------------------------------------------------------
# Whole-file review path (_fetch_head_files + files-mode dispatch)
# ---------------------------------------------------------------------------


class TestWholeFileReview:
    def test_fetch_head_files_returns_whole_files_and_skips_binary(self, review_app) -> None:
        from software_engineering_team.api.pr_review import _fetch_head_files

        files = [
            PullRequestFile("a.py", "modified", "@@ -1 +1 @@\n+x", 1, 0, None),
            PullRequestFile("gone.py", "removed", "@@ -1 +0 @@\n-x", 0, 1, None),
            PullRequestFile("img.png", "added", "", 0, 0, None),  # binary: no patch
        ]

        def _contents(o, r, path, ref):
            assert ref == "sha1"
            return "WHOLE\n" if path == "a.py" else None

        out = _fetch_head_files(_file_contents_client(_contents), "o", "r", files, "sha1")
        assert out == {"a.py": "WHOLE\n"}  # removed + binary skipped

    def test_fetch_head_files_degrades_on_client_without_method(self, review_app) -> None:
        from software_engineering_team.api.pr_review import _fetch_head_files

        files = [PullRequestFile("a.py", "modified", "@@ -1 +1 @@\n+x", 1, 0, None)]
        # A client missing get_file_contents must degrade to {} (hunk fallback),
        # not raise.
        assert _fetch_head_files(object(), "o", "r", files, "sha1") == {}

    def test_fetch_head_files_scrubs_token_from_fetch_warning(
        self, review_app, caplog: pytest.LogCaptureFixture
    ) -> None:
        from software_engineering_team.api.pr_review import _fetch_head_files

        secret_url = "https://x:ghp_LEAKEDTOKEN@github.com/o/r.git"
        files = [PullRequestFile("a.py", "modified", "@@ -1 +1 @@\n+x", 1, 0, None)]

        class _FakeGitHubClient:
            def get_file_contents(self, o, r, path, ref):
                raise RuntimeError(f"fetch failed: {secret_url}")

        with caplog.at_level("WARNING"):
            assert _fetch_head_files(_FakeGitHubClient(), "o", "r", files, "sha1") == {}

        assert not any("ghp_LEAKEDTOKEN" in r.getMessage() for r in caplog.records)
        assert not any(secret_url in r.getMessage() for r in caplog.records)
        assert not any(r.exc_text and "ghp_LEAKEDTOKEN" in r.exc_text for r in caplog.records)
        [record] = [r for r in caplog.records if "could not fetch head content" in r.getMessage()]
        assert "ghp_LEAKEDTOKEN" not in record.getMessage()
        assert "https://***@" in record.getMessage()

    def test_fetch_head_files_concurrent_fetches_do_not_corrupt_results(self, review_app) -> None:
        """_fetch_head_files fans per-file GETs out across a thread pool; each
        worker's (filename, content) pair must land under its own key, never a
        sibling's, even when several fetches are in flight at once.
        """
        import threading

        from software_engineering_team.api.pr_review import (
            _HEAD_FETCH_PARALLELISM,
            _fetch_head_files,
        )

        num_files = 16
        parties = min(_HEAD_FETCH_PARALLELISM, num_files)
        assert parties > 1, "test requires the parallel fetch path"
        barrier = threading.Barrier(parties)
        files = [
            PullRequestFile(f"f{i}.py", "modified", f"@@ -1 +1 @@\n+x{i}", 1, 0, None)
            for i in range(num_files)
        ]

        def _contents(o, r, path, ref):
            barrier.wait(timeout=5)
            return f"WHOLE-{path}\n"

        try:
            out = _fetch_head_files(_file_contents_client(_contents), "o", "r", files, "sha1")
        except threading.BrokenBarrierError:
            pytest.fail(
                f"fetches did not run concurrently: barrier timed out waiting for {parties} parties"
            )
        assert out == {f"f{i}.py": f"WHOLE-f{i}.py\n" for i in range(num_files)}

    def test_endpoint_uses_whole_files_and_passes_reader(self, review_app, monkeypatch) -> None:
        from software_engineering_team.github_source import GitHubRepoReader

        gh = review_app["github"]["client"]
        gh.get_file_contents = lambda o, r, path, ref: "def a():\n    return 1\n"
        gh.get_repository_tree = lambda o, r, ref, recursive=True: ["a.py"]

        captured: dict[str, Any] = {}

        class _CapProvider:
            def run_pr_code_review(self, **kw: Any) -> Any:
                captured.update(kw)
                return _FakeOutput(issues=[])

        monkeypatch.setattr("software_engineering_team.engine_provider._provider", _CapProvider())

        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        # Surface-first: head-backed change surface is the primary pre-numbered
        # code input; whole-file files= is skipped when the surface is nonempty.
        assert "files" not in captured
        assert captured["pre_numbered"] is True
        assert "### a.py ###" in captured["code"]
        assert isinstance(captured["repo_reader"], GitHubRepoReader)

    def test_endpoint_falls_back_to_hunks_when_no_head_files(self, review_app, monkeypatch) -> None:
        gh = review_app["github"]["client"]
        # Head fetch yields nothing -> hunk fallback (pre_numbered code blob).
        gh.get_file_contents = lambda o, r, path, ref: None

        captured: dict[str, Any] = {}

        class _CapProvider:
            def run_pr_code_review(self, **kw: Any) -> Any:
                captured.update(kw)
                return _FakeOutput(issues=[])

        monkeypatch.setattr("software_engineering_team.engine_provider._provider", _CapProvider())

        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        assert captured.get("files") is None
        assert captured["pre_numbered"] is True
        assert captured["code"]  # the hunk-rendered blob
        # Hunk mode now carries the same "tag pre-existing findings" focus note as
        # whole-file mode (previously it passed pr.body verbatim, with no tagging
        # instruction at all) -- see _hunk_review_focus.
        from software_engineering_team.api.pr_review import REVIEW_FOCUS_NOTE_PREFIX

        assert REVIEW_FOCUS_NOTE_PREFIX in captured["task_requirements"]
        # Content assertions beyond the shared prefix: both whole-file and
        # hunk-mode notes start with REVIEW_FOCUS_NOTE_PREFIX, so that check
        # alone wouldn't catch _hunk_review_focus regressing into an alias of
        # _whole_file_focus. Assert on the hunk-specific wording too.
        assert "pre_existing" in captured["task_requirements"]
        assert "diff hunks" in captured["task_requirements"]

    def test_whole_file_mode_appends_focus_note(self, review_app, monkeypatch) -> None:
        gh = review_app["github"]["client"]  # default: single reviewable file a.py
        gh.get_file_contents = lambda o, r, path, ref: "def a():\n    return 1\n"
        gh.get_repository_tree = lambda o, r, ref, recursive=True: ["a.py"]

        captured: dict[str, Any] = {}

        class _CapProvider:
            def run_pr_code_review(self, **kw: Any) -> Any:
                captured.update(kw)
                return _FakeOutput(issues=[])

        monkeypatch.setattr("software_engineering_team.engine_provider._provider", _CapProvider())
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        # Surface-first primary attempt uses the hunk-framed focus note
        # (_hunk_review_focus), not the whole-file files= note.
        from software_engineering_team.api.pr_review import REVIEW_FOCUS_NOTE_PREFIX

        assert REVIEW_FOCUS_NOTE_PREFIX in captured["task_requirements"]
        assert "diff hunks" in captured["task_requirements"]
        assert "complete file contents" not in captured["task_requirements"]

    def test_partial_head_fetch_reviews_fetched_subset_whole_and_missing_subset_via_hunks(
        self, review_app, monkeypatch
    ) -> None:
        gh = review_app["github"]["client"]
        # Two reviewable files; only one fetches whole content.
        gh.files = [
            PullRequestFile("a.py", "modified", "@@ -1,2 +1,3 @@\n ctx\n+added\n more", 1, 0, None),
            PullRequestFile("b.py", "modified", "@@ -1,1 +1,2 @@\n x\n+y", 1, 0, None),
        ]
        gh.get_file_contents = lambda o, r, path, ref: "whole a\n" if path == "a.py" else None
        gh.get_repository_tree = lambda o, r, ref, recursive=True: []

        calls: list[dict[str, Any]] = []

        class _CapProvider:
            def run_pr_code_review(self, **kw: Any) -> Any:
                calls.append(dict(kw))
                return _FakeOutput(issues=[])

        monkeypatch.setattr("software_engineering_team.engine_provider._provider", _CapProvider())
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        # Partial head fetch: surface-primary for fetched a.py + hunk code for
        # missing b.py. Both attempts are pre-numbered code= (no files=).
        assert len(calls) == 2
        assert all("files" not in c for c in calls)
        assert all(c["pre_numbered"] is True for c in calls)
        surface_call = next(c for c in calls if "### a.py ###" in c.get("code", ""))
        hunk_call = next(c for c in calls if "### b.py ###" in c.get("code", ""))
        assert "### b.py ###" not in surface_call["code"]
        assert "### a.py ###" not in hunk_call["code"]

        from software_engineering_team.api.pr_review import (
            REVIEW_FOCUS_NOTE_PREFIX,
        )

        assert REVIEW_FOCUS_NOTE_PREFIX in surface_call["task_requirements"]
        assert "diff hunks" in surface_call["task_requirements"]
        assert REVIEW_FOCUS_NOTE_PREFIX in hunk_call["task_requirements"]
        assert "pre_existing" in hunk_call["task_requirements"]
        assert "diff hunks" in hunk_call["task_requirements"]

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["files_reviewed"] == 2  # 1 surface + 1 hunk

    def test_partial_head_fetch_posts_findings_from_both_whole_file_and_hunk_subsets(
        self, review_app, monkeypatch
    ) -> None:
        gh = review_app["github"]["client"]
        gh.files = [
            PullRequestFile("a.py", "modified", "@@ -1,2 +1,3 @@\n ctx\n+added\n more", 1, 0, None),
            PullRequestFile("b.py", "modified", "@@ -1,1 +1,2 @@\n x\n+y", 1, 0, None),
        ]
        gh.get_file_contents = lambda o, r, path, ref: "whole a\n" if path == "a.py" else None
        gh.get_repository_tree = lambda o, r, ref, recursive=True: []

        class _SplitProvider:
            def run_pr_code_review(self, **kw: Any) -> Any:
                code = kw.get("code") or ""
                if "### a.py ###" in code:
                    return _FakeOutput(
                        issues=[
                            _FakeReviewIssue(
                                "high",
                                line=2,
                                file_path="a.py",
                                description="whole-file finding",
                            )
                        ],
                        summary="",  # blank: must not blank out the merged narrative
                        spec="",
                    )
                return _FakeOutput(
                    issues=[
                        _FakeReviewIssue(
                            "high", line=2, file_path="b.py", description="hunk finding"
                        )
                    ],
                    summary="Hunk summary text",
                    spec="",
                )

        monkeypatch.setattr("software_engineering_team.engine_provider._provider", _SplitProvider())
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"

        # Both findings actually made it onto the PR -- neither subset silently
        # vanished.
        posted_bodies = (
            [c["body"] for r in gh.reviews for c in (r.get("comments") or [])]
            + [c["body"] for c in gh.review_comments]
            + [b for _n, b in gh.comments]
        )
        assert any("whole-file finding" in b for b in posted_bodies)
        assert any("hunk finding" in b for b in posted_bodies)
        assert job["review_summary"]["total_issues"] == 2

        # The merged narrative drops the blank whole-file summary and keeps
        # the hunk-fallback summary -- proves _MergedReviewerOutput ran.
        assert len(gh.reviews) >= 1, "Expected at least one review submission"
        assert "Hunk summary text" in gh.reviews[-1]["body"]

    def test_endpoint_noop_when_nothing_whole_file_reviewable(
        self, review_app, monkeypatch
    ) -> None:
        gh = review_app["github"]["client"]
        # Only a removed file and a binary (no-patch) file -- nothing is
        # reviewable, so the gate must fire BEFORE any head-file fetch or
        # reviewer call, exactly as the old `if not code` gate did.
        gh.files = [
            PullRequestFile("gone.py", "removed", "@@ -1 +0 @@\n-x", 0, 1, None),
            PullRequestFile("img.png", "added", "", 0, 0, None),
        ]
        fetch_calls = 0

        def _get_file_contents(o, r, path, ref):
            nonlocal fetch_calls
            fetch_calls += 1
            return "should never be reached"

        gh.get_file_contents = _get_file_contents

        provider_calls = 0

        class _CountingProvider:
            def run_pr_code_review(self, **kw: Any) -> Any:
                nonlocal provider_calls
                provider_calls += 1
                return _FakeOutput(issues=[])

        monkeypatch.setattr(
            "software_engineering_team.engine_provider._provider",
            _CountingProvider(),
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["status_text"] == "No reviewable file content"
        assert any("no reviewable file content" in b.lower() for _n, b in gh.comments)
        assert fetch_calls == 0  # gate fires before any head-file fetch
        assert provider_calls == 0  # ...and before any reviewer call

    def test_endpoint_noop_when_reviewable_but_pure_removal_hunk_and_fetch_fails(
        self, review_app
    ) -> None:
        gh = review_app["github"]["client"]
        # "modified" + non-empty patch => _is_whole_file_reviewable is True, but
        # the patch is pure removal (no +/context lines), so render_annotated_hunks
        # -> "" and _build_review_code skips it. Default gh has no
        # get_file_contents, so the whole-file fetch also fails -> must still
        # land on the exact same noop as before this change.
        gh.files = [
            PullRequestFile("a.py", "modified", "@@ -1,2 +1,0 @@\n-a\n-b", 0, 2, None),
        ]
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["status_text"] == "No reviewable file content"


# ---------------------------------------------------------------------------
# Parallelized independent GitHub reads on the review fetch path
# ---------------------------------------------------------------------------


class TestParallelReviewReads:
    """_fetch_pr_metadata and _fetch_existing_comments fan independent GitHub
    reads out across a bounded thread pool (the same idiom _fetch_head_files
    proves). These tests confirm the fetches actually run concurrently and that
    results/error-propagation are unchanged from the prior serial calls."""

    def test_fetch_pr_metadata_concurrent_fetches_do_not_corrupt_results(self, review_app) -> None:
        import threading

        from software_engineering_team.api.pr_review import _fetch_pr_metadata

        barrier = threading.Barrier(3)

        client = _pr_metadata_client(on_each=lambda: barrier.wait(timeout=5))

        try:
            pr, files, reviewer_login = _fetch_pr_metadata(client, "o", "r", 7)
        except threading.BrokenBarrierError:
            pytest.fail("fetches did not run concurrently: barrier timed out waiting for 3 parties")
        assert pr.number == 7
        assert [f.filename for f in files] == ["a.py"]
        assert reviewer_login == "khala-bot"
        assert barrier.n_waiting == 0  # confirms the fetches actually ran concurrently

    def test_fetch_pr_metadata_get_pull_request_failure_propagates(self, review_app) -> None:
        from software_engineering_team.api.pr_review import _fetch_pr_metadata

        with pytest.raises(GitHubAPIError, match="missing PR"):
            _fetch_pr_metadata(
                _pr_metadata_client(fail_pr=GitHubAPIError(404, "missing PR"), files=[]),
                "o",
                "r",
                7,
            )

    def test_fetch_pr_metadata_get_pull_request_files_failure_propagates(self, review_app) -> None:
        from software_engineering_team.api.pr_review import _fetch_pr_metadata

        with pytest.raises(GitHubAPIError, match="files unavailable"):
            _fetch_pr_metadata(
                _pr_metadata_client(fail_files=GitHubAPIError(502, "files unavailable")),
                "o",
                "r",
                7,
            )

    def test_fetch_pr_metadata_awaits_all_futures_when_first_fails(
        self, review_app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When get_pull_request fails, siblings that also fail must still have
        ``result()`` called — a generator unpack stops early and leaves secondary
        exceptions unretrieved on those futures."""
        from concurrent.futures import ThreadPoolExecutor as _RealTPE

        from software_engineering_team.api import pr_review
        from software_engineering_team.api.pr_review import _fetch_pr_metadata

        result_calls = {"n": 0}
        futures_seen: list[Any] = []

        class _CountingExecutor(_RealTPE):
            def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
                fut = super().submit(fn, *args, **kwargs)
                futures_seen.append(fut)
                real_result = fut.result

                def _counting_result(*a: Any, **kw: Any) -> Any:
                    result_calls["n"] += 1
                    return real_result(*a, **kw)

                fut.result = _counting_result  # type: ignore[method-assign]
                return fut

        monkeypatch.setattr(pr_review, "ThreadPoolExecutor", _CountingExecutor)

        with pytest.raises(GitHubAPIError, match="missing PR"):
            _fetch_pr_metadata(
                _pr_metadata_client(
                    fail_pr=GitHubAPIError(404, "missing PR"),
                    fail_files=GitHubAPIError(502, "files unavailable"),
                ),
                "o",
                "r",
                7,
            )
        assert len(futures_seen) == 3
        assert result_calls["n"] == 3

    def test_fetch_pr_metadata_get_authenticated_login_failure_degrades(self, review_app) -> None:
        """A get_authenticated_login failure must degrade to "" without blocking
        or failing the (independent) pr/files fetches, unlike get_pull_request and
        get_pull_request_files, which are not best-effort."""
        from software_engineering_team.api.pr_review import _fetch_pr_metadata

        pr, files, reviewer_login = _fetch_pr_metadata(
            _pr_metadata_client(fail_login=GitHubAPIError(403, "no scope")),
            "o",
            "r",
            7,
        )
        assert pr.number == 7
        assert [f.filename for f in files] == ["a.py"]
        assert reviewer_login == ""

    def test_fetch_pr_metadata_get_authenticated_login_non_api_error_degrades(
        self, review_app
    ) -> None:
        """A non-GitHubAPIError failure (e.g. a bug, an unexpected error) from
        get_authenticated_login must ALSO degrade to "" rather than propagate —
        the docstring promises this lookup "must never fail the review", not
        just for GitHubAPIError."""
        from software_engineering_team.api.pr_review import _fetch_pr_metadata

        pr, files, reviewer_login = _fetch_pr_metadata(
            _pr_metadata_client(fail_login=RuntimeError("unexpected")),
            "o",
            "r",
            7,
        )
        assert pr.number == 7
        assert [f.filename for f in files] == ["a.py"]
        assert reviewer_login == ""

    def test_fetch_existing_comments_concurrent_fetches_do_not_corrupt_results(
        self, review_app
    ) -> None:
        import threading

        from software_engineering_team.api.pr_review import _fetch_existing_comments

        barrier = threading.Barrier(3)
        review_comment = ReviewComment(
            id=1, path="a.py", line=2, body="desc", html_url="https://example/comment/1"
        )

        class _FakeGitHubClient:
            def list_review_comments(self, o, r, n):
                barrier.wait(timeout=5)
                return [review_comment]

            def get_resolved_review_thread_comment_ids(self, o, r, n):
                barrier.wait(timeout=5)
                return {1}

            def list_issue_comments(self, o, r, n):
                barrier.wait(timeout=5)
                return []

        try:
            out = _fetch_existing_comments(_FakeGitHubClient(), "o", "r", 7)
        except threading.BrokenBarrierError:
            pytest.fail("fetches did not run concurrently: barrier timed out waiting for 3 parties")
        assert barrier.n_waiting == 0  # confirms the fetches actually ran concurrently
        assert len(out) == 1
        assert out[0].path == "a.py" and out[0].line == 2
        assert out[0].resolved is True  # id 1 is in resolved_ids

    @pytest.mark.parametrize(
        "failing_method",
        ["list_review_comments", "get_resolved_review_thread_comment_ids", "list_issue_comments"],
    )
    def test_fetch_existing_comments_any_failure_degrades_whole_result(
        self, review_app, failing_method
    ) -> None:
        """Any of the three calls failing must degrade the WHOLE result to [] —
        the same all-or-nothing semantics the prior serial version had — not a
        partial result from the two calls that succeeded."""
        from software_engineering_team.api.pr_review import _fetch_existing_comments

        class _FakeGitHubClient:
            def list_review_comments(self, o, r, n):
                if failing_method == "list_review_comments":
                    raise GitHubAPIError(500, "boom")
                return []

            def get_resolved_review_thread_comment_ids(self, o, r, n):
                if failing_method == "get_resolved_review_thread_comment_ids":
                    raise GitHubAPIError(500, "boom")
                return set()

            def list_issue_comments(self, o, r, n):
                if failing_method == "list_issue_comments":
                    raise GitHubAPIError(500, "boom")
                return []

        assert _fetch_existing_comments(_FakeGitHubClient(), "o", "r", 7) == []

    def test_fetch_existing_comments_awaits_all_futures_when_first_fails(
        self, review_app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the first concurrent comment fetch fails, siblings that also fail
        must still have ``result()`` called. The call still degrades to []
        (best-effort)."""
        from concurrent.futures import ThreadPoolExecutor as _RealTPE

        from software_engineering_team.api import pr_review
        from software_engineering_team.api.pr_review import _fetch_existing_comments

        result_calls = {"n": 0}
        futures_seen: list[Any] = []

        class _CountingExecutor(_RealTPE):
            def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
                fut = super().submit(fn, *args, **kwargs)
                futures_seen.append(fut)
                real_result = fut.result

                def _counting_result(*a: Any, **kw: Any) -> Any:
                    result_calls["n"] += 1
                    return real_result(*a, **kw)

                fut.result = _counting_result  # type: ignore[method-assign]
                return fut

        monkeypatch.setattr(pr_review, "ThreadPoolExecutor", _CountingExecutor)

        class _FakeGitHubClient:
            def list_review_comments(self, o, r, n):
                raise GitHubAPIError(500, "reviews boom")

            def get_resolved_review_thread_comment_ids(self, o, r, n):
                raise GitHubAPIError(500, "resolved boom")

            def list_issue_comments(self, o, r, n):
                raise GitHubAPIError(500, "issues boom")

        assert _fetch_existing_comments(_FakeGitHubClient(), "o", "r", 7) == []
        assert len(futures_seen) == 3
        assert result_calls["n"] == 3

    @pytest.mark.parametrize(
        "failing_method",
        ["list_review_comments", "get_resolved_review_thread_comment_ids", "list_issue_comments"],
    )
    def test_fetch_existing_comments_non_api_error_degrades_whole_result(
        self, review_app, failing_method
    ) -> None:
        """A non-GitHubAPIError failure (e.g. a bug, an unexpected error) from any
        of the three calls must ALSO degrade the whole result to [] — the
        docstring promises "Any failure ... degrades the WHOLE result", not
        just a GitHubAPIError."""
        from software_engineering_team.api.pr_review import _fetch_existing_comments

        class _FakeGitHubClient:
            def list_review_comments(self, o, r, n):
                if failing_method == "list_review_comments":
                    raise RuntimeError("unexpected")
                return []

            def get_resolved_review_thread_comment_ids(self, o, r, n):
                if failing_method == "get_resolved_review_thread_comment_ids":
                    raise RuntimeError("unexpected")
                return set()

            def list_issue_comments(self, o, r, n):
                if failing_method == "list_issue_comments":
                    raise RuntimeError("unexpected")
                return []

        assert _fetch_existing_comments(_FakeGitHubClient(), "o", "r", 7) == []


class TestSafeCommentUnit:
    """_safe_comment's docstring promises "Never raises"; this must hold for any
    failure from add_issue_comment, not just GitHubAPIError."""

    def test_returns_false_on_github_api_error(self, review_app) -> None:
        from software_engineering_team.api.pr_review import _safe_comment

        class _FakeGitHubClient:
            def add_issue_comment(self, o, r, n, body):
                raise GitHubAPIError(403, "rate limited")

        assert _safe_comment(_FakeGitHubClient(), "o", "r", 7, "body") is False

    def test_returns_false_on_non_api_error(self, review_app) -> None:
        """A non-GitHubAPIError failure (e.g. a bug, an unexpected error) must
        ALSO degrade to False rather than propagate."""
        from software_engineering_team.api.pr_review import _safe_comment

        class _FakeGitHubClient:
            def add_issue_comment(self, o, r, n, body):
                raise RuntimeError("unexpected")

        assert _safe_comment(_FakeGitHubClient(), "o", "r", 7, "body") is False

    def test_returns_true_on_success(self, review_app) -> None:
        from software_engineering_team.api.pr_review import _safe_comment

        posted: list[tuple[int, str]] = []

        class _FakeGitHubClient:
            def add_issue_comment(self, o, r, n, body):
                posted.append((n, body))

        assert _safe_comment(_FakeGitHubClient(), "o", "r", 7, "body") is True
        assert posted == [(7, "body")]

    def test_scrubs_token_from_failure_warning(
        self, review_app, caplog: pytest.LogCaptureFixture
    ) -> None:
        from software_engineering_team.api.pr_review import _safe_comment

        secret_url = "https://x:ghp_LEAKEDTOKEN@github.com/o/r.git"

        class _FakeGitHubClient:
            def add_issue_comment(self, o, r, n, body):
                raise RuntimeError(f"comment failed: {secret_url}")

        with caplog.at_level("WARNING"):
            assert _safe_comment(_FakeGitHubClient(), "o", "r", 7, "body") is False

        assert not any("ghp_LEAKEDTOKEN" in r.getMessage() for r in caplog.records)
        assert not any(secret_url in r.getMessage() for r in caplog.records)
        assert not any(r.exc_text and "ghp_LEAKEDTOKEN" in r.exc_text for r in caplog.records)
        for r in caplog.records:
            if r.exc_info and r.exc_info[1] is not None:
                assert "ghp_LEAKEDTOKEN" not in str(r.exc_info[1])
                assert secret_url not in str(r.exc_info[1])
        [record] = [r for r in caplog.records if "Failed to comment on issue" in r.getMessage()]
        assert "ghp_LEAKEDTOKEN" not in record.getMessage()
        assert "***" in record.getMessage()
        assert "github.com/o/r.git" in record.getMessage()


# ---------------------------------------------------------------------------
# Pre-existing findings -> issue proposals -> GitHub issues
# ---------------------------------------------------------------------------


def _run_review_with(review_app, issues: list[Any]) -> dict[str, Any]:
    """Run a PR review whose agent returns ``issues`` and return the completed job."""
    review_app["github"]["agent_output"] = _FakeOutput(issues=issues)
    resp = review_app["client"].post("/review-pr", json=_review_body())
    assert resp.status_code == 200
    return review_app["jobs"].get_job(resp.json()["job_id"])


class TestPreExistingFindings:
    def test_preexisting_finding_is_not_commented_but_stored_as_proposal(self, review_app) -> None:
        """A pre_existing-tagged finding drives no PR comment/event and is stored as
        a proposal instead; the real PR finding still posts and drives REQUEST_CHANGES."""
        gh = review_app["github"]["client"]
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue("high", line=2, description="real PR bug"),
                _FakeReviewIssue(
                    "critical",
                    line=2,
                    file_path="legacy.py",
                    description="old latent bug",
                    pre_existing=True,
                ),
            ],
        )
        summary = job["review_summary"]
        # Only the PR finding is posted; it drives the summary counts + event.
        assert summary["total_issues"] == 1
        assert summary["inline_comments"] == 1
        assert gh.submitted_reviews and gh.submitted_reviews[0]["event"] == "REQUEST_CHANGES"
        # The pre-existing finding never became a comment on the PR.
        for review in gh.reviews:
            for c in review.get("comments", []):
                assert "old latent bug" not in c.get("body", "")
        assert all("old latent bug" not in b for _n, b in gh.comments)
        assert all("old latent bug" not in rc.get("body", "") for rc in gh.review_comments)
        # Nor did it leak into the submitted review's own top-level body.
        assert all(
            "old latent bug" not in review.get("body", "") for review in gh.submitted_reviews
        )
        # It is stored as a proposal instead.
        proposals = summary["pending_issue_proposals"]
        assert len(proposals) == 1
        p = proposals[0]
        assert p["id"] == "p0"
        assert p["severity"] == "critical"
        assert p["description"] == "old latent bug"
        assert p["issue_url"] is None

    def test_only_preexisting_findings_reads_as_clean_pr(self, review_app) -> None:
        """When every finding is pre-existing, the PR itself reads as clean: a COMMENT
        event and a +1 reaction, while the finding still surfaces as a proposal."""
        gh = review_app["github"]["client"]
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high", line=2, file_path="legacy.py", description="latent", pre_existing=True
                )
            ],
        )
        summary = job["review_summary"]
        # The PR's own change is clean: no PR findings, a COMMENT (not
        # REQUEST_CHANGES) event, and a +1 reaction.
        assert summary["total_issues"] == 0
        assert gh.submitted_reviews and gh.submitted_reviews[0]["event"] == "COMMENT"
        assert gh.reactions and gh.reactions[0][1] == "+1"
        # The suppressed narrative never leaked the pre-existing finding into
        # the submitted review's own top-level body either.
        assert all("latent" not in review.get("body", "") for review in gh.submitted_reviews)
        # But the pre-existing bug is still surfaced as a proposal.
        assert len(summary["pending_issue_proposals"]) == 1

    def test_status_text_mentions_preexisting_count(self, review_app) -> None:
        """The job's status text reports how many pre-existing bug proposals were
        found. Two distinct (non-similar) descriptions stay separate proposals."""
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue("high", line=2),
                _FakeReviewIssue(
                    "low",
                    line=2,
                    file_path="legacy.py",
                    description="latent bug alpha",
                    pre_existing=True,
                ),
                _FakeReviewIssue(
                    "low",
                    line=2,
                    file_path="legacy.py",
                    description="unrelated null check",
                    pre_existing=True,
                ),
            ],
        )
        assert "2 pre-existing bugs to review" in job["status_text"]

    def test_status_text_counts_combined_proposal_once(self, review_app) -> None:
        """Similar pre-existing findings collapse into one proposal, and the status
        text counts the combined proposal, not the raw finding count."""
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "low",
                    line=2,
                    file_path="legacy.py",
                    description="bare import `os`",
                    pre_existing=True,
                ),
                _FakeReviewIssue(
                    "low",
                    line=2,
                    file_path="legacy.py",
                    description="bare import `sys`",
                    pre_existing=True,
                ),
            ],
        )
        assert "1 pre-existing bug to review" in job["status_text"]
        assert len(job["review_summary"]["pending_issue_proposals"]) == 1

    def test_preexisting_tag_on_context_line_is_not_overridden(self, review_app) -> None:
        """A pre_existing-tagged finding on a diff CONTEXT line (shown for anchoring
        but not actually added by the PR) must stay a proposal — only a finding on a
        line the PR actually ADDED can override a mistagged pre_existing=true back to
        a PR finding. The default patch's line 1 is context (` ctx`), line 2 is added
        (`+added`)."""
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high",
                    line=1,
                    file_path="a.py",
                    description="old bug on context line",
                    pre_existing=True,
                ),
            ],
        )
        summary = job["review_summary"]
        assert summary["total_issues"] == 0
        assert len(summary["pending_issue_proposals"]) == 1
        assert summary["pending_issue_proposals"][0]["description"] == "old bug on context line"

    def test_preexisting_tag_on_added_line_is_overridden(self, review_app) -> None:
        """A pre_existing-tagged finding on a line the PR actually ADDED cannot
        legitimately be pre-existing/unchanged code — it must be overridden back to a
        PR finding rather than silently skipping review."""
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high", line=2, file_path="a.py", description="mistagged bug", pre_existing=True
                ),
            ],
        )
        summary = job["review_summary"]
        assert summary["total_issues"] == 1
        assert summary["pending_issue_proposals"] == []

    def test_create_issues_files_selected_proposal(self, review_app) -> None:
        """Only the requested proposal is filed; unselected proposals stay unfiled."""
        gh = review_app["github"]["client"]
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high", line=2, file_path="legacy.py", description="latent A", pre_existing=True
                ),
                _FakeReviewIssue(
                    "low", line=3, file_path="legacy.py", description="latent B", pre_existing=True
                ),
            ],
        )
        job_id = job["job_id"]
        resp = review_app["client"].post(f"/reviews/{job_id}/issues", json={"proposal_ids": ["p0"]})
        assert resp.status_code == 200
        data = resp.json()
        # Exactly the selected proposal was filed.
        assert len(gh.created_issues) == 1
        assert "latent A" in gh.created_issues[0]["body"]
        assert data["created"][0]["proposal_id"] == "p0"
        assert data["created"][0]["issue_url"].startswith("https://example/issues/")
        # The returned + persisted proposal now carries the issue url; p1 is untouched.
        by_id = {p["id"]: p for p in data["proposals"]}
        assert by_id["p0"]["issue_url"] is not None
        assert by_id["p1"]["issue_url"] is None
        stored = review_app["jobs"].get_job(job_id)["review_summary"]["pending_issue_proposals"]
        assert {p["id"]: bool(p["issue_url"]) for p in stored} == {"p0": True, "p1": False}

    def test_create_issues_scrubs_title_in_response(self, review_app) -> None:
        """The returned ``created[].title`` must match what was actually filed on
        GitHub (scrubbed), never the raw finding text — a leaked token in the
        response would defeat the whole point of scrubbing it before the API call."""
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high",
                    line=2,
                    file_path="legacy.py",
                    description="leaked https://user:secrettoken@github.com/o/r.git in stderr",
                    pre_existing=True,
                )
            ],
        )
        resp = review_app["client"].post(
            f"/reviews/{job['job_id']}/issues", json={"proposal_ids": ["p0"]}
        )
        assert resp.status_code == 200
        title = resp.json()["created"][0]["title"]
        assert "secrettoken" not in title
        assert "https://***@" in title
        # The scrub must happen before the GitHub API call itself, not only in
        # the HTTP response -- check what was actually sent to the fake client.
        gh = review_app["github"]["client"]
        assert "secrettoken" not in gh.created_issues[0]["title"]
        assert "https://***@" in gh.created_issues[0]["title"]

    def test_create_issues_is_idempotent(self, review_app) -> None:
        """Filing the same proposal twice opens exactly one GitHub issue."""
        gh = review_app["github"]["client"]
        job = _run_review_with(
            review_app,
            [_FakeReviewIssue("high", line=2, file_path="legacy.py", pre_existing=True)],
        )
        job_id = job["job_id"]
        review_app["client"].post(f"/reviews/{job_id}/issues", json={"proposal_ids": ["p0"]})
        # Second call for the same proposal opens no new issue.
        resp = review_app["client"].post(f"/reviews/{job_id}/issues", json={"proposal_ids": ["p0"]})
        assert resp.status_code == 200
        assert resp.json()["created"] == []
        assert len(gh.created_issues) == 1

    def test_create_issues_ignores_unknown_proposal_id(self, review_app) -> None:
        """A proposal id that doesn't exist on the review is silently ignored."""
        gh = review_app["github"]["client"]
        job = _run_review_with(
            review_app,
            [_FakeReviewIssue("high", line=2, file_path="legacy.py", pre_existing=True)],
        )
        resp = review_app["client"].post(
            f"/reviews/{job['job_id']}/issues", json={"proposal_ids": ["nope"]}
        )
        assert resp.status_code == 200
        assert resp.json()["created"] == []
        assert gh.created_issues == []

    def test_create_issues_unknown_job_returns_404(self, review_app) -> None:
        """Filing issues for a job id that names no review returns 404."""
        resp = review_app["client"].post(
            "/reviews/does-not-exist/issues", json={"proposal_ids": ["p0"]}
        )
        assert resp.status_code == 404

    def test_create_issues_missing_token_returns_400(
        self, review_app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Filing issues with no GitHub token configured returns 400."""
        job = _run_review_with(
            review_app,
            [_FakeReviewIssue("high", line=2, file_path="legacy.py", pre_existing=True)],
        )
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        resp = review_app["client"].post(
            f"/reviews/{job['job_id']}/issues", json={"proposal_ids": ["p0"]}
        )
        assert resp.status_code == 400

    def test_create_issues_github_error_returns_502(self, review_app) -> None:
        """A GitHub API failure while filing an issue surfaces as 502."""
        gh = review_app["github"]["client"]
        gh.create_issue_fail = True
        job = _run_review_with(
            review_app,
            [_FakeReviewIssue("high", line=2, file_path="legacy.py", pre_existing=True)],
        )
        resp = review_app["client"].post(
            f"/reviews/{job['job_id']}/issues", json={"proposal_ids": ["p0"]}
        )
        assert resp.status_code == 502

    def test_create_issues_matching_owner_repo_succeeds(self, review_app) -> None:
        """A request whose owner/repo matches the reviewed repository (case-
        insensitively) succeeds."""
        gh = review_app["github"]["client"]
        job = _run_review_with(
            review_app,
            [_FakeReviewIssue("high", line=2, file_path="legacy.py", pre_existing=True)],
        )
        # The review ran against o/r (see _review_body); a matching request files.
        resp = review_app["client"].post(
            f"/reviews/{job['job_id']}/issues",
            json={"proposal_ids": ["p0"], "owner": "O", "repo": "R"},
        )
        assert resp.status_code == 200
        assert len(gh.created_issues) == 1

    def test_create_issues_wrong_owner_repo_returns_409(self, review_app) -> None:
        """A request whose owner/repo doesn't match the reviewed repository returns
        409 and opens no issue."""
        gh = review_app["github"]["client"]
        job = _run_review_with(
            review_app,
            [_FakeReviewIssue("high", line=2, file_path="legacy.py", pre_existing=True)],
        )
        resp = review_app["client"].post(
            f"/reviews/{job['job_id']}/issues",
            json={"proposal_ids": ["p0"], "owner": "o", "repo": "other"},
        )
        assert resp.status_code == 409
        # No issue was opened for the mismatched repository.
        assert gh.created_issues == []


class TestDuplicateProposalDetection:
    """A pre-existing finding matched to an already-open GitHub issue is already
    tracked, so it is dropped entirely rather than offered to the user."""

    def test_matching_open_issue_is_dropped_from_proposals(self, review_app) -> None:
        gh = review_app["github"]["client"]
        gh.open_issues = [
            Issue(
                number=42,
                title="off-by-one error in loop bound",
                body="",
                state="open",
                html_url="https://example/issues/42",
                labels=(),
            )
        ]
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high",
                    line=2,
                    file_path="legacy.py",
                    description="off-by-one error in loop bound",
                    pre_existing=True,
                )
            ],
        )
        assert job["review_summary"]["pending_issue_proposals"] == []

    def test_unrelated_open_issue_does_not_mark_proposal_matched(self, review_app) -> None:
        gh = review_app["github"]["client"]
        gh.open_issues = [
            Issue(
                number=42,
                title="unrelated feature request",
                body="nothing to do with this",
                state="open",
                html_url="https://example/issues/42",
                labels=(),
            )
        ]
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high",
                    line=2,
                    file_path="legacy.py",
                    description="off-by-one error in loop bound",
                    pre_existing=True,
                )
            ],
        )
        proposal = job["review_summary"]["pending_issue_proposals"][0]
        assert proposal["matched_existing"] is False
        assert proposal["issue_url"] is None

    def test_duplicate_check_fetches_open_issues_once_per_review_not_per_finding(
        self, review_app
    ) -> None:
        gh = review_app["github"]["client"]
        _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high", line=2, file_path="legacy.py", description="latent A", pre_existing=True
                ),
                _FakeReviewIssue(
                    "low", line=3, file_path="legacy.py", description="latent B", pre_existing=True
                ),
            ],
        )
        assert gh.list_open_issues_calls == 1

    def test_duplicate_check_skipped_when_no_preexisting_findings(self, review_app) -> None:
        gh = review_app["github"]["client"]
        _run_review_with(review_app, [_FakeReviewIssue("high", line=2)])
        assert gh.list_open_issues_calls == 0

    def test_duplicate_check_fails_open_on_github_api_error(self, review_app) -> None:
        """A GitHub failure listing open issues degrades to "no duplicates found"
        rather than failing the review — the proposal still surfaces, unmatched."""
        gh = review_app["github"]["client"]
        gh.list_open_issues_exc = GitHubAPIError(500, "boom")
        job = _run_review_with(
            review_app,
            [_FakeReviewIssue("high", line=2, file_path="legacy.py", pre_existing=True)],
        )
        assert job["status"] == "completed"
        proposal = job["review_summary"]["pending_issue_proposals"][0]
        assert proposal["matched_existing"] is False
        assert proposal["issue_url"] is None

    def test_duplicate_check_fails_open_on_unexpected_exception(self, review_app) -> None:
        """Same fail-open guarantee for a non-API exception (the broad except branch)."""
        gh = review_app["github"]["client"]
        gh.list_open_issues_exc = RuntimeError("boom")
        job = _run_review_with(
            review_app,
            [_FakeReviewIssue("high", line=2, file_path="legacy.py", pre_existing=True)],
        )
        assert job["status"] == "completed"
        proposal = job["review_summary"]["pending_issue_proposals"][0]
        assert proposal["matched_existing"] is False
        assert proposal["issue_url"] is None

    def test_duplicate_check_fails_open_when_annotation_itself_raises(
        self, review_app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: listing open issues can succeed while annotate_duplicate_proposals
        itself raises (e.g. a bug in the matching logic) -- this must degrade to "no
        duplicates found" exactly like a listing failure, not fail the whole review."""
        from software_engineering_team.api import pr_review

        def _raise(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(pr_review, "annotate_duplicate_proposals", _raise)
        job = _run_review_with(
            review_app,
            [_FakeReviewIssue("high", line=2, file_path="legacy.py", pre_existing=True)],
        )
        assert job["status"] == "completed"
        proposal = job["review_summary"]["pending_issue_proposals"][0]
        assert proposal["matched_existing"] is False
        assert proposal["issue_url"] is None

    def test_create_review_issues_never_refiles_a_matched_proposal(self, review_app) -> None:
        """A matched finding is dropped before it ever becomes a filable candidate,
        so requesting its (nonexistent) proposal id via the create-issues endpoint
        is a no-op rather than filing a new, duplicate GitHub issue."""
        gh = review_app["github"]["client"]
        gh.open_issues = [
            Issue(
                number=42,
                title="off-by-one error in loop bound",
                body="",
                state="open",
                html_url="https://example/issues/42",
                labels=(),
            )
        ]
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high",
                    line=2,
                    file_path="legacy.py",
                    description="off-by-one error in loop bound",
                    pre_existing=True,
                )
            ],
        )
        assert job["review_summary"]["pending_issue_proposals"] == []
        resp = review_app["client"].post(
            f"/reviews/{job['job_id']}/issues", json={"proposal_ids": ["p0"]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == []
        assert gh.created_issues == []
        assert data["proposals"] == []

    def test_duplicate_check_only_considers_open_issues_up_to_the_cap(
        self, review_app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A matching issue placed past the configured cap is never considered —
        the fetch is bounded rather than traversing every open issue."""
        monkeypatch.setenv("PR_REVIEW_DUPLICATE_MAX_OPEN_ISSUES", "2")
        gh = review_app["github"]["client"]
        gh.open_issues = [
            Issue(
                number=n,
                title="unrelated issue",
                body="",
                state="open",
                html_url=f"https://example/issues/{n}",
                labels=(),
            )
            for n in range(1, 3)
        ] + [
            Issue(
                number=99,
                title="off-by-one error in loop bound",
                body="",
                state="open",
                html_url="https://example/issues/99",
                labels=(),
            )
        ]
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high",
                    line=2,
                    file_path="legacy.py",
                    description="off-by-one error in loop bound",
                    pre_existing=True,
                )
            ],
        )
        proposal = job["review_summary"]["pending_issue_proposals"][0]
        # The matching issue (#99) sits past the cap of 2, so it was never fetched.
        assert proposal["matched_existing"] is False
        assert proposal["issue_url"] is None

    def test_duplicate_check_env_override_widens_the_cap(
        self, review_app, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raising the cap via env var lets a later-listed matching issue be found,
        so the finding is dropped as already-tracked instead of offered."""
        monkeypatch.setenv("PR_REVIEW_DUPLICATE_MAX_OPEN_ISSUES", "3")
        gh = review_app["github"]["client"]
        gh.open_issues = [
            Issue(
                number=n,
                title="unrelated issue",
                body="",
                state="open",
                html_url=f"https://example/issues/{n}",
                labels=(),
            )
            for n in range(1, 3)
        ] + [
            Issue(
                number=99,
                title="off-by-one error in loop bound",
                body="",
                state="open",
                html_url="https://example/issues/99",
                labels=(),
            )
        ]
        job = _run_review_with(
            review_app,
            [
                _FakeReviewIssue(
                    "high",
                    line=2,
                    file_path="legacy.py",
                    description="off-by-one error in loop bound",
                    pre_existing=True,
                )
            ],
        )
        assert job["review_summary"]["pending_issue_proposals"] == []


class TestDetectDuplicateProposalsUnit:
    """Direct unit tests for _detect_duplicate_proposals, independent of the full
    review harness (extracted from _run_pr_review_body for exactly this reason)."""

    def _proposal(self, pid: str, description: str = "d") -> dict:
        """Return a minimal duplicate-proposal dict for unit tests.

        Carries the fields ``_detect_duplicate_proposals`` expects, with a
        customizable description so tests can control matching behavior.
        """
        return {
            "id": pid,
            "severity": "high",
            "category": "logic",
            "file_path": "",
            "line": None,
            "description": description,
            "suggestion": "",
            "locations": [],
            "issue_number": None,
            "issue_url": None,
        }

    def test_empty_proposals_never_calls_the_client(self) -> None:
        """An empty proposals list must not touch GitHub at all."""
        from software_engineering_team.api import pr_review

        calls: list[str] = []

        class _Client:
            def list_open_issues(self, _o, _r):
                calls.append("list_open_issues")
                return iter(())

        result = pr_review._detect_duplicate_proposals([], _Client(), "o", "r", 1)
        assert result == []
        assert calls == [], "empty proposals must not call list_open_issues"

    def test_matches_against_a_fetched_open_issue(self) -> None:
        """A proposal whose description matches an open issue title is annotated."""
        from software_engineering_team.api import pr_review

        client = _open_issues_client(
            [
                Issue(
                    number=42,
                    title="off-by-one error in loop bound",
                    body="",
                    state="open",
                    html_url="https://example/issues/42",
                    labels=(),
                )
            ]
        )
        proposal = _assert_exactly_one(
            pr_review._detect_duplicate_proposals(
                [self._proposal("p0", "off-by-one error in loop bound")], client, "o", "r", 1
            ),
            label="proposals",
        )
        assert proposal["matched_existing"] is True
        assert proposal["issue_url"] == "https://example/issues/42"

    def test_list_open_issues_failure_degrades_to_unmatched(self) -> None:
        """A list_open_issues GitHubAPIError must fail open (unmatched, no raise)."""
        from software_engineering_team.api import pr_review

        client = _open_issues_client(error=GitHubAPIError(500, "boom"))
        proposal = _assert_exactly_one(
            pr_review._detect_duplicate_proposals([self._proposal("p0")], client, "o", "r", 1),
            label="proposals",
        )
        assert proposal["matched_existing"] is False
        assert proposal["issue_url"] is None

    def test_annotation_failure_falls_back_to_unmatched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If annotate_duplicate_proposals itself raises, proposals stay unmatched."""
        from software_engineering_team.api import pr_review

        def _raise(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(pr_review, "annotate_duplicate_proposals", _raise)
        proposal = _assert_exactly_one(
            pr_review._detect_duplicate_proposals(
                [self._proposal("p0")], _open_issues_client(), "o", "r", 1
            ),
            label="proposals",
        )
        assert proposal["matched_existing"] is False
        assert proposal["issue_url"] is None


def _mode_pr(head_sha: str = "sha1") -> PullRequestDetail:
    """Return a minimal PullRequestDetail for review-mode / posting unit tests.

    Defaults produce an open, non-draft PR against main so tests can focus on
    mode-selection and comment-posting logic without repeating boilerplate.
    ``head_sha`` is overridable when a test needs a specific commit ref.
    """
    return _pr_detail(number=7, html_url="https://example/pull/7", head_sha=head_sha, body="")


class TestDecideReviewModeUnit:
    """Direct unit tests for _decide_review_mode, extracted from
    _run_pr_review_body for exactly this reason (its own no-op exits, and each
    of the whole-file/partial/hunk-fallback branches, independent of the full
    review harness)."""

    def test_no_files_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from software_engineering_team.api import pr_review

        noop_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            pr_review,
            "_complete_review_noop",
            lambda *a, **kw: noop_calls.append(kw),
        )

        result = pr_review._decide_review_mode(object(), "job1", "o", "r", 7, _mode_pr(), [])
        assert result is None
        assert noop_calls == [
            {
                "comment": "Code review: no changed files to review.",
                "status_text": "No changed files to review",
            }
        ]

    def test_no_reviewable_files_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from software_engineering_team.api import pr_review

        noop_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            pr_review,
            "_complete_review_noop",
            lambda *a, **kw: noop_calls.append(kw),
        )

        def _must_not_parse(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError(
                "parse_valid_lines must not run on the no-reviewable noop path"
            )

        monkeypatch.setattr(pr_review, "parse_valid_lines", _must_not_parse)

        files = [
            PullRequestFile("gone.py", "removed", "@@ -1 +0 @@\n-x", 0, 1, None),
            PullRequestFile("img.png", "added", "", 0, 0, None),  # binary: no patch
        ]
        result = pr_review._decide_review_mode(object(), "job1", "o", "r", 7, _mode_pr(), files)
        assert result is None
        assert noop_calls == [
            {
                "comment": "Code review: no reviewable file content.",
                "status_text": "No reviewable file content",
            }
        ]

    def test_all_files_fetch_whole_skips_hunk_rendering(self) -> None:
        from software_engineering_team.api import pr_review

        files = [
            PullRequestFile("a.py", "modified", "@@ -1 +1 @@\n+x", 1, 0, None),
            PullRequestFile("b.py", "modified", "@@ -1 +1 @@\n+y", 1, 0, None),
        ]

        def _contents(o, r, path, ref):
            assert ref == "sha1"
            return f"WHOLE {path}\n"

        result = pr_review._decide_review_mode(
            _file_contents_client(_contents), "job1", "o", "r", 7, _mode_pr(), files
        )
        assert result is not None
        assert result.code == ""
        assert result.files_reviewed == 2
        assert set(result.head_files) == {"a.py", "b.py"}
        assert set(result.valid_by_path["a.py"]) == {1}
        assert set(result.changed_by_path["a.py"]) == {1}
        from software_engineering_team.code_review_agent.change_surface import ChangeSurface

        assert isinstance(result.change_surface, ChangeSurface)

    def test_decide_review_mode_attaches_head_backed_change_surface(self) -> None:
        from software_engineering_team.api import pr_review

        content = "def f():\n    return 1\n"
        patch = "@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n"
        files = [PullRequestFile("mod.py", "modified", patch, 1, 1, None)]

        result = pr_review._decide_review_mode(
            _file_contents_client(lambda o, r, path, ref: content),
            "job1",
            "o",
            "r",
            7,
            _mode_pr(),
            files,
        )
        assert result is not None
        assert not result.change_surface.is_empty
        assert "mod.py" in result.change_surface.blocks
        assert result.code == ""  # whole-file fetch still primary for dispatch this leaf

    def test_partial_fetch_falls_back_to_hunks_for_the_missing_subset(self) -> None:
        from software_engineering_team.api import pr_review

        files = [
            PullRequestFile("a.py", "modified", "@@ -1,2 +1,3 @@\n ctx\n+added\n more", 1, 0, None),
            PullRequestFile("b.py", "modified", "@@ -1,1 +1,2 @@\n x\n+y", 1, 0, None),
        ]

        result = pr_review._decide_review_mode(
            _file_contents_client(lambda o, r, path, ref: "whole a\n" if path == "a.py" else None),
            "job1",
            "o",
            "r",
            7,
            _mode_pr(),
            files,
        )
        assert result is not None
        assert set(result.head_files) == {"a.py"}
        assert "b.py" in result.code  # missing file's hunk was rendered
        assert "a.py" not in result.code  # fetched file's hunk was NOT rendered
        assert result.files_reviewed == 2  # 1 whole + 1 hunk
        assert "b.py" not in result.change_surface.blocks

    def test_total_fetch_failure_renders_every_files_hunks(self) -> None:
        from software_engineering_team.api import pr_review

        files = [PullRequestFile("a.py", "modified", "@@ -1 +1 @@\n+x", 1, 0, None)]

        result = pr_review._decide_review_mode(
            _file_contents_client(lambda o, r, path, ref: None),
            "job1",
            "o",
            "r",
            7,
            _mode_pr(),
            files,
        )
        assert result is not None
        assert result.head_files == {}
        assert result.code
        assert result.files_reviewed == 1
        assert result.change_surface.is_empty

    def test_total_fetch_failure_with_blank_hunk_render_is_a_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Belt-and-suspenders branch: reviewable is non-empty but every
        reviewable file's diff hunk happens to render blank (e.g. a
        deletion-only patch)."""
        from software_engineering_team.api import pr_review

        noop_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            pr_review,
            "_complete_review_noop",
            lambda *a, **kw: noop_calls.append(kw),
        )
        # A patch that only removes lines: has_patch is True (reviewable) but
        # render_annotated_hunks emits nothing for it.
        files = [PullRequestFile("a.py", "modified", "@@ -1,2 +1,0 @@\n-x\n-y", 0, 2, None)]

        result = pr_review._decide_review_mode(
            _file_contents_client(lambda o, r, path, ref: None),
            "job1",
            "o",
            "r",
            7,
            _mode_pr(),
            files,
        )
        assert result is None
        assert noop_calls == [
            {
                "comment": "Code review: no reviewable file content.",
                "status_text": "No reviewable file content",
            }
        ]


class TestPartitionReviewIssuesUnit:
    """Direct unit tests for _partition_review_issues, extracted from
    _run_pr_review_body for exactly this reason."""

    def test_pre_existing_tag_kept_when_not_within_diff(self) -> None:
        from software_engineering_team.api import pr_review

        issue = _FakeReviewIssue("medium", line=99, file_path="a.py", pre_existing=True)
        output = _FakeOutput(issues=[issue])
        valid_by_path = {"a.py": [1, 2, 99]}
        changed_by_path = {"a.py": [1]}  # line 99 was NOT added by this PR

        class _FakeGitHubClient:
            def list_review_comments(self, o, r, n):
                return []

            def list_issue_comments(self, o, r, n):
                return []

            def get_resolved_review_thread_comment_ids(self, o, r, n):
                return set()

            def list_open_issues(self, o, r):
                return iter(())

        result = pr_review._partition_review_issues(
            output, _FakeGitHubClient(), "o", "r", 7, valid_by_path, changed_by_path
        )
        assert result.pr_issues == []
        assert result.preexisting_issues == [issue]
        assert len(result.proposals) == 1

    def test_pre_existing_tag_overridden_when_within_diff(self) -> None:
        from software_engineering_team.api import pr_review

        issue = _FakeReviewIssue("medium", line=1, file_path="a.py", pre_existing=True)
        output = _FakeOutput(issues=[issue])
        valid_by_path = {"a.py": [1]}
        changed_by_path = {"a.py": [1]}  # line 1 WAS added by this PR

        class _FakeGitHubClient:
            def list_review_comments(self, o, r, n):
                return []

            def list_issue_comments(self, o, r, n):
                return []

            def get_resolved_review_thread_comment_ids(self, o, r, n):
                return set()

        result = pr_review._partition_review_issues(
            output, _FakeGitHubClient(), "o", "r", 7, valid_by_path, changed_by_path
        )
        assert result.pr_issues == [issue]
        assert result.preexisting_issues == []
        assert result.proposals == []

    def test_existing_comments_fetch_skipped_when_no_pr_issues(self) -> None:
        from software_engineering_team.api import pr_review

        output = _FakeOutput(issues=[])

        class _FakeGitHubClient:
            def list_review_comments(self, o, r, n):
                raise AssertionError("should not be called when pr_issues is empty")

        result = pr_review._partition_review_issues(output, _FakeGitHubClient(), "o", "r", 7, {}, {})
        assert result.pr_issues == []
        assert result.addressed_issues == []

    def test_finding_matching_resolved_comment_is_dropped_as_addressed(self) -> None:
        from software_engineering_team.api import pr_review
        from software_engineering_team.github_source import ReviewComment

        issue = _FakeReviewIssue("high", line=2, file_path="a.py", description="dup finding")
        output = _FakeOutput(issues=[issue])
        valid_by_path = {"a.py": [1, 2]}
        changed_by_path = {"a.py": [1, 2]}
        existing = ReviewComment(
            id=1, path="a.py", line=2, body="dup finding", html_url="https://example/comment/1"
        )

        class _FakeGitHubClient:
            def list_review_comments(self, o, r, n):
                return [existing]

            def list_issue_comments(self, o, r, n):
                return []

            def get_resolved_review_thread_comment_ids(self, o, r, n):
                return {1}  # resolved

        result = pr_review._partition_review_issues(
            output, _FakeGitHubClient(), "o", "r", 7, valid_by_path, changed_by_path
        )
        assert result.pr_issues == []
        assert result.addressed_issues == [issue]

    def test_out_of_diff_finding_without_tag_becomes_standalone(self) -> None:
        from software_engineering_team.api import pr_review

        # file_path not present in valid_by_path, and no pre_existing tag (round
        # 2): the finding stays a PR finding -- routing to preexisting_issues is
        # driven ONLY by the reviewer's own tag, never by diff/file membership
        # alone (forcing this to preexisting would silently drop a real, in-scope
        # finding like "this PR references module X but never added it"). Since
        # it cannot resolve to any path in the diff, map_issues_to_comments
        # returns it as a leftover, rendered into standalone_comments.
        issue = _FakeReviewIssue("high", line=1, file_path="missing.py")
        output = _FakeOutput(issues=[issue])
        valid_by_path = {"a.py": [1, 2]}
        changed_by_path = {"a.py": [1, 2]}

        class _FakeGitHubClient:
            def list_review_comments(self, o, r, n):
                return []

            def list_issue_comments(self, o, r, n):
                return []

            def get_resolved_review_thread_comment_ids(self, o, r, n):
                return set()

            def list_open_issues(self, o, r):
                return iter(())

        result = pr_review._partition_review_issues(
            output, _FakeGitHubClient(), "o", "r", 7, valid_by_path, changed_by_path
        )
        assert result.pr_issues == [issue]
        assert result.preexisting_issues == []
        assert result.proposals == []
        assert result.line_comments == []
        assert result.file_comments == []
        assert len(result.standalone_comments) == 1
        assert "missing.py" in result.standalone_comments[0]


def _review_issue_partition(**overrides: Any):
    """Build a ``ReviewIssuePartition`` with empty defaults for unit tests.

    Callers override only the fields under test (e.g. ``pr_issues``,
    ``line_comments``) so partition construction stays DRY across posting and
    finalize unit suites.
    """
    from software_engineering_team.api.pr_review import ReviewIssuePartition

    base = dict(
        pr_issues=[],
        preexisting_issues=[],
        proposals=[],
        addressed_issues=[],
        line_comments=[],
        file_comments=[],
        standalone_comments=[],
    )
    base.update(overrides)
    return ReviewIssuePartition(**base)


class TestPostReviewCommentsUnit:
    """Direct unit tests for _post_review_comments, extracted from
    _run_pr_review_body for exactly this reason."""

    def _partition(self, **overrides: Any):
        return _review_issue_partition(**overrides)

    def test_happy_path_counts(self) -> None:
        from software_engineering_team.api import pr_review

        client = _FakeReviewClient()
        line_comment = {"path": "a.py", "line": 2, "body": "b", "side": "RIGHT"}
        partition = self._partition(pr_issues=[object()], line_comments=[line_comment])
        output = _FakeOutput(issues=[])

        result = pr_review._post_review_comments(
            client, "o", "r", 7, _mode_pr(), "khala-bot", output, partition
        )
        assert result.inline_count == 1
        assert result.file_comment_count == 0
        assert result.comments_failed == 0
        assert len(client.submitted_reviews) == 1

    def test_github_api_error_tolerated_when_only_file_comments_remain(self) -> None:
        from software_engineering_team.api import pr_review

        client = _FakeReviewClient()
        client.review_exc = GitHubAPIError(500, "summary submit failed")
        file_comment = {"path": "a.py", "line": 2, "body": "b", "subject_type": "file"}
        partition = self._partition(pr_issues=[object()], file_comments=[file_comment])
        output = _FakeOutput(issues=[])

        result = pr_review._post_review_comments(
            client, "o", "r", 7, _mode_pr(), "khala-bot", output, partition
        )
        assert result.file_comment_count == 1
        assert result.inline_count == 0
        assert result.comments_failed == 0
        from software_engineering_team.api import pr_review

        client = _FakeReviewClient()
        client.review_exc = GitHubAPIError(500, "boom")
        line_comment = {"path": "a.py", "line": 2, "body": "b", "side": "RIGHT"}
        partition = self._partition(pr_issues=[object()], line_comments=[line_comment])
        output = _FakeOutput(issues=[])

        with pytest.raises(GitHubAPIError):
            pr_review._post_review_comments(
                client, "o", "r", 7, _mode_pr(), "khala-bot", output, partition
            )

    def test_github_api_error_reraised_when_nothing_else_to_post(self) -> None:
        from software_engineering_team.api import pr_review

        client = _FakeReviewClient()
        client.review_exc = GitHubAPIError(500, "boom")
        partition = self._partition()
        output = _FakeOutput(issues=[])

        with pytest.raises(GitHubAPIError):
            pr_review._post_review_comments(
                client, "o", "r", 7, _mode_pr(), "khala-bot", output, partition
            )

    def test_standalone_comment_failure_is_counted(self) -> None:
        from software_engineering_team.api import pr_review

        client = _FakeReviewClient()
        client.review_comment_fail_paths = {"a.py"}  # forces demotion to standalone
        client.comment_fail_times = 1  # the one standalone attempt fails
        file_comment = {"path": "a.py", "line": 2, "body": "b", "subject_type": "file"}
        partition = self._partition(pr_issues=[object()], file_comments=[file_comment])
        output = _FakeOutput(issues=[])

        result = pr_review._post_review_comments(
            client, "o", "r", 7, _mode_pr(), "khala-bot", output, partition
        )
        assert result.comment_findings == 1
        assert result.comments_failed == 1

    def test_github_api_error_tolerated_when_only_standalone_comments_remain(self) -> None:
        # No line- or file-level comments, but a standalone comment (an
        # off-diff-file finding, per _partition_review_issues) is still to post:
        # the summary submission's failure must be tolerated, same as when a
        # file-level comment alone remains.
        from software_engineering_team.api import pr_review

        client = _FakeReviewClient()
        client.review_exc = GitHubAPIError(500, "summary submit failed")
        partition = self._partition(
            pr_issues=[object()],
            standalone_comments=["`missing.py` — **[HIGH] logic** — orphan finding"],
        )
        output = _FakeOutput(issues=[])

        result = pr_review._post_review_comments(
            client, "o", "r", 7, _mode_pr(), "khala-bot", output, partition
        )
        assert result.inline_count == 0
        assert result.file_comment_count == 0
        assert result.comment_findings == 1
        assert result.comments_failed == 0
        assert client.comments == [(7, "`missing.py` — **[HIGH] logic** — orphan finding")]

    def test_partition_standalone_comments_merge_with_422_demoted_standalone(self) -> None:
        # standalone_bodies combines TWO sources: findings GitHub itself rejected
        # as file-level comments (422-demoted), and partition.standalone_comments
        # (findings whose file was never in the diff at all). Both must post and
        # both must count toward comment_findings.
        from software_engineering_team.api import pr_review

        client = _FakeReviewClient()
        client.review_comment_fail_paths = {"a.py"}  # forces demotion to standalone
        file_comment = {
            "path": "a.py",
            "line": 2,
            "body": "demoted finding",
            "subject_type": "file",
        }
        partition = self._partition(
            pr_issues=[object(), object()],
            file_comments=[file_comment],
            standalone_comments=["`missing.py` — off-diff finding"],
        )
        output = _FakeOutput(issues=[])

        result = pr_review._post_review_comments(
            client, "o", "r", 7, _mode_pr(), "khala-bot", output, partition
        )
        assert result.comment_findings == 2
        assert result.comments_failed == 0
        bodies = [body for _n, body in client.comments]
        assert any("demoted finding" in b for b in bodies)
        assert any("off-diff finding" in b for b in bodies)


class TestFinalizeReviewOutcomeUnit:
    """Direct unit tests for _finalize_review_outcome, extracted from
    _run_pr_review_body for exactly this reason."""

    def _partition(self, **overrides: Any):
        return _review_issue_partition(**overrides)

    def _posting(self, **overrides: Any):
        from software_engineering_team.api.pr_review import CommentPostingResult

        base = dict(
            event="COMMENT",
            inline_count=0,
            file_comment_count=0,
            comment_findings=0,
            comments_failed=0,
        )
        base.update(overrides)
        return CommentPostingResult(**base)

    def _capture_finalize(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        from software_engineering_team.api import pr_review

        calls: list[dict[str, Any]] = []

        def _fake_finalize(job_id, status, status_text=None, **kw):
            calls.append({"status": status, "status_text": status_text, **kw})

        monkeypatch.setattr(pr_review, "_finalize_review", _fake_finalize)
        return calls

    def test_comments_failed_marks_job_failed_and_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from software_engineering_team.api import pr_review
        from software_engineering_team.models import JobStatus

        calls = self._capture_finalize(monkeypatch)
        reacted: list[Any] = []
        monkeypatch.setattr(pr_review, "_react_to_pr", lambda *a, **kw: reacted.append(a))
        client = _FakeReviewClient()

        posting = self._posting(comments_failed=1, comment_findings=2)
        partition = self._partition(pr_issues=[_FakeReviewIssue("high", line=1)])

        pr_review._finalize_review_outcome(
            client, "job1", "o", "r", 7, _mode_pr(), 1, partition, posting
        )

        assert len(calls) == 1
        assert calls[0]["status"] == JobStatus.FAILED
        assert "1 of 2 finding comment(s)" in calls[0]["error"]
        assert reacted == []  # no further calls after the early return
        assert client.comments  # the "incomplete" notice was posted

    def test_clean_review_reacts_and_completes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from software_engineering_team.api import pr_review
        from software_engineering_team.models import JobStatus

        calls = self._capture_finalize(monkeypatch)
        reacted: list[Any] = []
        monkeypatch.setattr(pr_review, "_react_to_pr", lambda *a, **kw: reacted.append(a))
        client = _FakeReviewClient()

        posting = self._posting()
        partition = self._partition(pr_issues=[])

        pr_review._finalize_review_outcome(
            client, "job1", "o", "r", 7, _mode_pr(), 1, partition, posting
        )

        assert len(calls) == 1
        assert calls[0]["status"] == JobStatus.COMPLETED
        assert len(reacted) == 1
        assert "pre-existing" not in calls[0]["status_text"]
        assert "already-addressed" not in calls[0]["status_text"]

    def test_non_empty_pr_issues_does_not_react(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from software_engineering_team.api import pr_review
        from software_engineering_team.models import JobStatus

        calls = self._capture_finalize(monkeypatch)
        reacted: list[Any] = []
        monkeypatch.setattr(pr_review, "_react_to_pr", lambda *a, **kw: reacted.append(a))
        client = _FakeReviewClient()

        posting = self._posting()
        partition = self._partition(pr_issues=[_FakeReviewIssue("low", line=1)])

        pr_review._finalize_review_outcome(
            client, "job1", "o", "r", 7, _mode_pr(), 1, partition, posting
        )
        assert reacted == []
        assert len(calls) == 1
        assert calls[0]["status"] == JobStatus.COMPLETED
        assert calls[0].get("status_text")

    def test_status_text_includes_proposals_and_addressed_clauses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from software_engineering_team.api import pr_review

        calls = self._capture_finalize(monkeypatch)
        monkeypatch.setattr(pr_review, "_react_to_pr", lambda *a, **kw: None)
        client = _FakeReviewClient()

        posting = self._posting()
        partition = self._partition(
            pr_issues=[],
            proposals=[{"id": "p0"}],
            addressed_issues=[_FakeReviewIssue("low", line=1)],
        )

        pr_review._finalize_review_outcome(
            client, "job1", "o", "r", 7, _mode_pr(), 1, partition, posting
        )
        assert "1 pre-existing bug to review" in calls[0]["status_text"]
        assert "1 already-addressed finding skipped" in calls[0]["status_text"]

    def test_severity_bucketing_ignores_unrecognized_severity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from software_engineering_team.api import pr_review

        calls = self._capture_finalize(monkeypatch)
        monkeypatch.setattr(pr_review, "_react_to_pr", lambda *a, **kw: None)
        client = _FakeReviewClient()

        posting = self._posting()
        partition = self._partition(
            pr_issues=[
                _FakeReviewIssue("high", line=1),
                _FakeReviewIssue("weird-level", line=2),
                _FakeReviewIssue("", line=3),
            ]
        )

        pr_review._finalize_review_outcome(
            client, "job1", "o", "r", 7, _mode_pr(), 1, partition, posting
        )
        summary = calls[0]["review_summary"]
        assert summary["total_issues"] == 3
        assert summary["severity_counts"] == {"high": 1}


def _assert_exactly_one(items: list[Any], *, label: str = "items") -> Any:
    """Return the sole element of ``items``.

    Preconditions:
        - ``items`` is a sized collection.
    Postconditions:
        - Raises AssertionError unless ``len(items) == 1``; otherwise returns
          that single element. Prefer this over list-unpacking (``[x] = ...``)
          so a wrong count fails with a clear message instead of ValueError.
    """
    assert len(items) == 1, f"expected exactly 1 {label}, got {len(items)}: {items!r}"
    return items[0]


def _assert_no_aliased_dicts(merged: list[Any], *sources: list[Any]) -> None:
    """Assert every dict in ``merged`` is a fresh copy of its sources.

    Preconditions:
        - ``merged`` and each ``sources`` entry are lists of dict-like objects.
    Postconditions:
        - Raises AssertionError if any ``merged`` entry is identity-equal to any
          entry in any source list (centralized check for the
          ``_merge_filed_proposals`` "fresh dicts / no aliasing" contract).
    """
    for m in merged:
        for source in sources:
            for s in source:
                assert m is not s, f"merged entry aliases a source dict: {m!r}"


def _github_issue_client(
    *,
    on_create: Optional[Callable[..., Any]] = None,
    number: int = 1,
    html_url: str = "u1",
) -> Callable[..., Any]:
    """Build a GitHubClient stand-in for create_review_issues unit tests.

    Preconditions:
        - ``on_create``, when provided, matches
          ``(owner, repo, *, title, body, labels=None) -> issue-like``.
    Postconditions:
        - Returns a kwargs-factory suitable for
          ``monkeypatch.setattr(api_main, "GitHubClient", factory)``. The
          constructed client supports ``with`` and either delegates
          ``create_issue`` to ``on_create`` or returns a default issue with
          ``number`` / ``html_url``.
    """

    class _Client:
        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def create_issue(
            self, _o: str, _r: str, *, title: str, body: str, labels: Any = None
        ) -> Any:
            if on_create is not None:
                return on_create(_o, _r, title=title, body=body, labels=labels)
            return type("_I", (), {"number": number, "html_url": html_url})()

    return lambda **_k: _Client()


def _open_issues_client(
    issues: Any = (),
    *,
    error: Optional[BaseException] = None,
) -> Any:
    """Build a client stub whose ``list_open_issues`` yields ``issues`` or raises.

    Preconditions:
        - ``issues`` is an iterable of Issue-like objects when ``error`` is None.
    Postconditions:
        - Returns an instance with ``list_open_issues`` that either raises
          ``error`` or yields from ``issues``.
    """

    class _Client:
        def list_open_issues(self, _o: str, _r: str) -> Any:
            if error is not None:
                raise error
            yield from issues

    return _Client()


def _file_contents_client(get_contents: Callable[..., Any]) -> Any:
    """Build a client stub exposing only ``get_file_contents``.

    Preconditions:
        - ``get_contents`` matches ``(owner, repo, path, ref) -> Optional[str]``.
    Postconditions:
        - Returns an instance whose ``get_file_contents`` delegates to
          ``get_contents``. Consistent stand-in for the scattered ``_FakeGitHubClient`` /
          ``_Client`` stubs used by whole-file / decide-mode unit tests.
    """

    class _Client:
        def get_file_contents(self, o: str, r: str, path: str, ref: str) -> Any:
            return get_contents(o, r, path, ref)

    return _Client()


def _pr_metadata_client(
    *,
    pr: Any = None,
    files: Any = None,
    login: str = "khala-bot",
    fail_pr: Optional[BaseException] = None,
    fail_files: Optional[BaseException] = None,
    fail_login: Optional[BaseException] = None,
    on_each: Optional[Callable[[], None]] = None,
) -> Any:
    """Build a client stub for ``_fetch_pr_metadata`` unit tests.

    Preconditions:
        - At most one of ``pr`` / ``fail_pr`` applies for get_pull_request; same
          for files/login. ``on_each``, when set, runs before every method body
          (used by the concurrency barrier tests).
    Postconditions:
        - Returns an instance exposing ``get_pull_request``,
          ``get_pull_request_files``, and ``get_authenticated_login``.
    """
    default_files = [PullRequestFile("a.py", "modified", "@@ -1 +1 @@\n+x", 1, 0, None)]

    class _Client:
        def get_pull_request(self, o: str, r: str, n: int) -> Any:
            if on_each is not None:
                on_each()
            if fail_pr is not None:
                raise fail_pr
            if pr is not None:
                return pr if not callable(pr) else pr(n)
            return _pr_detail(number=n, html_url=f"https://example/pull/{n}")

        def get_pull_request_files(self, o: str, r: str, n: int) -> Any:
            if on_each is not None:
                on_each()
            if fail_files is not None:
                raise fail_files
            return list(files if files is not None else default_files)

        def get_authenticated_login(self) -> str:
            if on_each is not None:
                on_each()
            if fail_login is not None:
                raise fail_login
            return login

    return _Client()


class TestCreateReviewIssuesUnit:
    """Direct unit tests for create_review_issues / its context loader."""

    def test_review_store_fallback_when_job_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the in-memory job has aged out, the durable review row's proposals
        are used to file an issue instead."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        # No live job, but a durable review row carries the proposals.
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: None)
        row = {
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "pr_url": "https://example/pull/5",
            "status": "completed",
            "review_summary": {
                "pending_issue_proposals": [
                    {
                        "id": "p0",
                        "severity": "high",
                        "category": "logic",
                        "file_path": "a.py",
                        "line": 3,
                        "description": "d",
                        "suggestion": "s",
                        "issue_number": None,
                        "issue_url": None,
                    }
                ]
            },
        }
        monkeypatch.setattr(api_main, "get_review", lambda *_a, **_k: row)
        created_titles: list[str] = []

        def _create(_o, _r, *, title, body, labels=None):
            created_titles.append(title)
            return type("_I", (), {"number": 11, "html_url": "u11"})()

        monkeypatch.setattr(api_main, "GitHubClient", _github_issue_client(on_create=_create))
        monkeypatch.setattr(api_main, "update_job", lambda *_a, **_k: None)
        monkeypatch.setattr(api_main, "update_review", lambda *_a, **_k: None)

        out = pr_review_issues.create_review_issues("job1", ["p0"], token="t")
        assert created_titles and out["created"][0]["issue_number"] == 11
        assert out["proposals"][0]["issue_url"] == "u11"

    def test_raises_review_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither store knowing the job id raises ReviewNotFoundError before any
        GitHub client is constructed."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: None)
        monkeypatch.setattr(api_main, "get_review", lambda *_a, **_k: None)

        def _fail_client(**_k):
            raise AssertionError("client should not be constructed when review is not found")

        monkeypatch.setattr(api_main, "GitHubClient", _fail_client)
        with pytest.raises(pr_review_issues.ReviewNotFoundError):
            pr_review_issues.create_review_issues("missing", ["p0"], token="t")

    def test_partial_failure_persists_progress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When one proposal's GitHub call fails, the other's successful creation is
        still persisted rather than lost."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        job = {
            "github_context": {
                "owner": "o",
                "repo": "r",
                "pr_number": 9,
                "pr_url": "https://example/pull/9",
            },
            "status": "completed",
            "review_summary": {
                "pending_issue_proposals": [
                    {"id": "p0", "description": "a", "issue_url": None},
                    {"id": "p1", "description": "b", "issue_url": None},
                ]
            },
        }
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: job)
        persisted: dict[str, Any] = {}

        def _create(_o, _r, *, title, body, labels=None):
            # Fail specifically for p1 (identified by structured title from
            # description "b", not by grepping issue-body markdown — proposals
            # are filed concurrently, so call order is not guaranteed).
            if title == "[info] b":
                raise GitHubAPIError(403, "boom")
            return type("_I", (), {"number": 1, "html_url": "u1"})()

        monkeypatch.setattr(api_main, "GitHubClient", _github_issue_client(on_create=_create))
        monkeypatch.setattr(api_main, "update_review", lambda _j, **kw: persisted.update(kw))
        monkeypatch.setattr(api_main, "update_job", lambda *_a, **_k: None)

        with pytest.raises(GitHubAPIError):
            pr_review_issues.create_review_issues("job1", ["p0", "p1"], token="t")
        # The issue opened despite the other proposal failing was persisted
        # (p0 filed, p1 not) — one proposal's rejection never stops another's.
        saved = {
            p["id"]: bool(p["issue_url"])
            for p in persisted["review_summary"]["pending_issue_proposals"]
        }
        assert saved == {"p0": True, "p1": False}

    def test_multiple_failures_wrapped_in_composite_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression: when more than one proposal fails, the caller must see every
        failure, not just whichever happened to be first -- a plain re-raise of one
        error would misleadingly suggest only that one proposal had a problem. Every
        failure must also still be logged, regardless of which one ends up in the
        composite exception."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        job = {
            "github_context": {
                "owner": "o",
                "repo": "r",
                "pr_number": 9,
                "pr_url": "https://example/pull/9",
            },
            "status": "completed",
            "review_summary": {
                "pending_issue_proposals": [
                    {"id": "p0", "description": "a", "issue_url": None},
                    {"id": "p1", "description": "b", "issue_url": None},
                ]
            },
        }
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: job)

        def _create(_o, _r, *, title, body, labels=None):
            # Both proposals fail, each with a distinguishable error — identify
            # by structured title (from description), not body markdown.
            if title == "[info] a":
                raise GitHubAPIError(403, "boom-a")
            raise GitHubAPIError(500, "boom-b")

        monkeypatch.setattr(api_main, "GitHubClient", _github_issue_client(on_create=_create))
        monkeypatch.setattr(api_main, "update_review", lambda *_a, **_k: None)
        monkeypatch.setattr(api_main, "update_job", lambda *_a, **_k: None)

        with caplog.at_level("WARNING"):
            with pytest.raises(pr_review_issues.MultipleIssueCreationErrors) as exc_info:
                pr_review_issues.create_review_issues("job1", ["p0", "p1"], token="t")
        logged = caplog.text
        assert "p0" in logged and "boom-a" in logged
        assert "p1" in logged and "boom-b" in logged
        assert set(exc_info.value.failures) == {"p0", "p1"}
        message = str(exc_info.value)
        assert "p0" in message and "p1" in message
        assert "boom-a" in message and "boom-b" in message

    def test_malformed_proposals_field_yields_no_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-list pending_issue_proposals field degrades to no candidates and
        never constructs a GitHub client."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        job = {
            "github_context": {"owner": "o", "repo": "r", "pr_number": 1, "pr_url": "u"},
            "status": "completed",
            "review_summary": {"pending_issue_proposals": "not-a-list"},
        }
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: job)

        def _fail_client(**_k):
            raise AssertionError(
                "GitHubClient must not be constructed for malformed proposals"
            )

        monkeypatch.setattr(api_main, "GitHubClient", _fail_client)
        def _no_client(**_kw):
            raise AssertionError("GitHubClient must not be constructed for malformed proposals")

        monkeypatch.setattr(api_main, "GitHubClient", _no_client)
        out = pr_review_issues.create_review_issues("job1", ["p0"], token="t")
        assert out["created"] == []
        assert out["proposals"] == []

    def test_skips_already_filed_proposal_within_one_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A proposal already carrying an issue_url is skipped even when explicitly
        requested again alongside a genuinely unfiled one."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        job = {
            "github_context": {"owner": "o", "repo": "r", "pr_number": 1, "pr_url": "u"},
            "status": "completed",
            "review_summary": {
                "pending_issue_proposals": [
                    {"id": "p0", "description": "a", "issue_url": "already"},
                    {"id": "p1", "description": "b", "issue_url": None},
                ]
            },
        }
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: job)
        monkeypatch.setattr(api_main, "update_job", lambda *_a, **_k: None)
        monkeypatch.setattr(api_main, "update_review", lambda *_a, **_k: None)
        calls: list[str] = []

        def _create(_o, _r, *, title, body, labels=None):
            calls.append(title)
            return type("_I", (), {"number": 2, "html_url": "u2"})()

        monkeypatch.setattr(api_main, "GitHubClient", _github_issue_client(on_create=_create))
        # Both requested, but p0 is already filed -> only p1 opens a new issue.
        out = pr_review_issues.create_review_issues("job1", ["p0", "p1"], token="t")
        assert len(calls) == 1
        assert [c["proposal_id"] for c in out["created"]] == ["p1"]

    def test_duplicate_proposal_id_in_request_files_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed/direct request repeating the same proposal id (e.g. a doubled
        UI click landing as one request, or ["p0", "p0"]) must open exactly one
        GitHub issue for it — the concurrent filer has no other guard against two
        tasks for the SAME proposal both observing issue_url unset before either
        writes it, so the id list itself must be deduped first."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        job = {
            "github_context": {"owner": "o", "repo": "r", "pr_number": 1, "pr_url": "u"},
            "status": "completed",
            "review_summary": {
                "pending_issue_proposals": [{"id": "p0", "description": "a", "issue_url": None}]
            },
        }
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: job)
        monkeypatch.setattr(api_main, "update_job", lambda *_a, **_k: None)
        monkeypatch.setattr(api_main, "update_review", lambda *_a, **_k: None)
        calls: list[str] = []

        def _create(_o, _r, *, title, body, labels=None):
            calls.append(title)
            return type("_I", (), {"number": 4, "html_url": "u4"})()

        monkeypatch.setattr(api_main, "GitHubClient", _github_issue_client(on_create=_create))
        out = pr_review_issues.create_review_issues("job1", ["p0", "p0", "p0"], token="t")
        assert len(calls) == 1
        assert [c["proposal_id"] for c in out["created"]] == ["p0"]

    def test_persist_swallows_store_errors(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failure persisting the updated proposals never fails the request — the
        GitHub issue already exists regardless of whether the local record updates —
        but both store failures must still be logged at WARNING so the swallow is
        observable."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        job = {
            "github_context": {"owner": "o", "repo": "r", "pr_number": 1, "pr_url": "u"},
            "status": "completed",
            "review_summary": {
                "pending_issue_proposals": [{"id": "p0", "description": "a", "issue_url": None}]
            },
        }
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: job)

        def _boom(*_a, **_k):
            raise RuntimeError("store down")

        monkeypatch.setattr(api_main, "update_job", _boom)
        monkeypatch.setattr(api_main, "update_review", _boom)

        monkeypatch.setattr(api_main, "GitHubClient", _github_issue_client())
        # Both stores fail, but the issue was created, so the call still succeeds —
        # and both failures must appear in WARNING logs (with exc_info).
        with caplog.at_level("WARNING"):
            out = pr_review_issues.create_review_issues("job1", ["p0"], token="t")
        assert out["created"][0]["issue_url"] == "u1"
        logged = caplog.text
        assert "could not update job" in logged
        assert "could not update review row" in logged
        assert "store down" in logged

    def test_repo_mismatch_raises_before_any_issue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A mismatched expected owner/repo raises RepoMismatchError before the
        GitHub client is ever constructed."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        job = {
            "github_context": {"owner": "acme", "repo": "widget", "pr_number": 1, "pr_url": "u"},
            "status": "completed",
            "review_summary": {
                "pending_issue_proposals": [{"id": "p0", "description": "a", "issue_url": None}]
            },
        }
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: job)

        def _fail_client(**_k):  # a GitHub call would be a bug — the guard must fire first
            raise AssertionError("GitHubClient must not be constructed on a repo mismatch")

        monkeypatch.setattr(api_main, "GitHubClient", _fail_client)
        with pytest.raises(pr_review_issues.RepoMismatchError):
            pr_review_issues.create_review_issues(
                "job1", ["p0"], token="t", expected_owner="acme", expected_repo="other"
            )

    def test_repo_match_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Expected owner/repo are compared case-insensitively, as GitHub treats them."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        job = {
            "github_context": {"owner": "Acme", "repo": "Widget", "pr_number": 1, "pr_url": "u"},
            "status": "completed",
            "review_summary": {
                "pending_issue_proposals": [{"id": "p0", "description": "a", "issue_url": None}]
            },
        }
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: job)
        monkeypatch.setattr(api_main, "update_job", lambda *_a, **_k: None)
        monkeypatch.setattr(api_main, "update_review", lambda *_a, **_k: None)

        monkeypatch.setattr(api_main, "GitHubClient", _github_issue_client())
        # "acme/widget" matches the stored "Acme/Widget" (GitHub is case-insensitive).
        out = pr_review_issues.create_review_issues(
            "job1", ["p0"], token="t", expected_owner="acme", expected_repo="widget"
        )
        assert out["created"][0]["issue_url"] == "u1"

    def test_issue_creation_lock_takes_pg_advisory_lock_when_postgres_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With Postgres configured, issue filing additionally takes a transaction-scoped
        advisory lock keyed on the job id — the cross-worker half of the mutual
        exclusion, mirroring _pr_review_admission.

        Preconditions:
            - shared.postgres reports Postgres enabled and yields a recording
              connection (monkeypatched; no real database).
        Postconditions:
            - Entering the lock issues exactly one pg_advisory_xact_lock call
              keyed on ("coding_team_issue_creation", job_id).
        """
        import contextlib as _contextlib

        import shared.postgres
        from software_engineering_team.api import pr_review_issues

        conn = MagicMock()

        @_contextlib.contextmanager
        def _fake_conn():
            yield conn

        monkeypatch.setattr(shared.postgres, "is_postgres_enabled", lambda: True)
        monkeypatch.setattr(shared.postgres, "get_conn", _fake_conn)
        with pr_review_issues._issue_creation_lock("job-lock"):
            pass
        conn.execute.assert_called_once_with(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            ("coding_team_issue_creation", "job-lock"),
        )

    def test_issue_creation_lock_degrades_when_postgres_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing advisory-lock acquisition degrades to the process-local lock alone
        (logged) — issue filing must never raise or block on a Postgres outage.

        Preconditions:
            - shared.postgres reports Postgres enabled but get_conn raises
              (monkeypatched outage).
        Postconditions:
            - The context manager enters and exits without raising.
        """
        import shared.postgres
        from software_engineering_team.api import pr_review_issues

        monkeypatch.setattr(shared.postgres, "is_postgres_enabled", lambda: True)
        monkeypatch.setattr(
            shared.postgres, "get_conn", MagicMock(side_effect=RuntimeError("pg down"))
        )
        with pr_review_issues._issue_creation_lock("job-degrade"):
            pass  # must not raise

    def test_merge_filed_proposals_prefers_whichever_copy_already_filed(self) -> None:
        """A proposal only transitions unfiled -> filed: when the preferred copy is
        still unfiled but the other store's copy carries issue_url, the filed copy
        wins; an already-filed preferred copy and an id unknown to the other list
        pass through unchanged.

        Preconditions:
            - Both input lists carry proposal dicts with "id" keys.
        Postconditions:
            - The merge preserves the preferred list's order; freshness/no-aliasing
              is checked via ``_assert_no_aliased_dicts`` (single source of truth).
        """
        from software_engineering_team.api import pr_review_issues

        preferred = [
            {"id": "p0", "issue_url": None},
            {"id": "p1", "issue_url": "u-preferred"},
            {"id": "p2", "issue_url": None},
        ]
        other = [
            {"id": "p0", "issue_url": "u-other", "issue_number": 9},
            {"id": "p1", "issue_url": None},
        ]
        merged = pr_review_issues._merge_filed_proposals(preferred, other)
        assert [p["id"] for p in merged] == ["p0", "p1", "p2"]
        assert merged[0]["issue_url"] == "u-other"  # other side already filed -> wins
        assert merged[1]["issue_url"] == "u-preferred"  # filed preferred copy is kept
        assert merged[2]["issue_url"] is None  # unknown to other -> unchanged
        # Fresh dicts for every merged entry — never aliases of either input list.
        _assert_no_aliased_dicts(merged, preferred, other)

    def test_context_merges_row_proposals_when_job_and_row_both_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the live job AND the durable row both know the review, the loaded
        context's proposals are the merge of both copies, so a row that already
        filed an issue makes the job's stale unfiled copy filed.

        Preconditions:
            - api.main's get_job and get_review are monkeypatched to return a
              completed job and a durable row for the same review.
        Postconditions:
            - The loaded context's pending_issue_proposals carry the row's
              filed issue_url for the shared proposal id.
            - Those proposal dicts are fresh copies (not aliases of the job or
              row dicts), so later mutation cannot touch persisted review state.
        """
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        job = {
            "status": "completed",
            "github_context": {"owner": "o", "repo": "r", "pr_number": 5, "pr_url": "u"},
            "review_summary": {"pending_issue_proposals": [{"id": "p0", "issue_url": None}]},
        }
        row = {
            "review_summary": {
                "pending_issue_proposals": [{"id": "p0", "issue_url": "u0", "issue_number": 1}]
            }
        }
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: job)
        monkeypatch.setattr(api_main, "get_review", lambda *_a, **_k: row)
        ctx = pr_review_issues._load_review_issue_context("job1")
        assert ctx is not None
        assert ctx.summary["pending_issue_proposals"][0]["issue_url"] == "u0"
        # Fresh dicts — never aliases of either store's proposal list.
        _assert_no_aliased_dicts(
            ctx.summary["pending_issue_proposals"],
            job["review_summary"]["pending_issue_proposals"],
            row["review_summary"]["pending_issue_proposals"],
        )


def test_pr_review_issues_imports_cleanly_in_a_fresh_process() -> None:
    """pr_review_issues must be importable as the FIRST of the api trio.

    The module resolves the api hub lazily (see ``_api_main``) precisely so
    that importing it does not re-enter a partially initialized module chain
    (pr_review_issues -> main -> pr_review -> pr_review_issues). A subprocess
    is the only faithful check: within this test process the trio is already
    imported, so a regression would be invisible here.

    Preconditions:
        - ``sys.executable`` can import the team package (inherited
          environment; the working directory is the ``backend/agents`` root
          on ``sys.path`` via this test process's own import of the package).
    Postconditions:
        - A fresh interpreter that imports pr_review_issues before any other
          coding-team api module exits 0.
        - The child ``PYTHONPATH`` still contains any runner-provided entries
          that were present before ``backend_root`` was prepended.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    import software_engineering_team

    agents_root = Path(software_engineering_team.__file__).resolve().parent.parent
    backend_root = agents_root.parent
    # `shared.env` (imported transitively by pr_review_issues) now lives under
    # backend/shared/, one level above agents_root — mirrors the production
    # container's `ENV PYTHONPATH=/app:/app/agents` (backend/Dockerfile). Prepend
    # backend_root so CI/runner PYTHONPATH entries (extra package roots) are kept.
    env = os.environ.copy()
    sentinel = "/tmp/khala-pythonpath-sentinel-should-be-preserved"
    # Simulate a runner-provided PYTHONPATH entry that must survive into the child.
    prior = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (prior, sentinel) if p)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(backend_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, software_engineering_team.api.pr_review_issues as _\n"
            "print(os.environ.get('PYTHONPATH', ''))",
        ],
        cwd=str(agents_root),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    child_pp = proc.stdout.strip().split(os.pathsep)
    assert str(backend_root) in child_pp
    assert sentinel in child_pp, (
        "subprocess PYTHONPATH must preserve pre-existing entries; "
        f"got {proc.stdout.strip()!r}"
    )

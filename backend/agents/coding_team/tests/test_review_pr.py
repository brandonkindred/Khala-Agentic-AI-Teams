"""Tests for the PR-review flow.

Covers the new GitHub client methods (via httpx.MockTransport), and the
POST /review-pr endpoint + background hook in api/main.py (with a fake
GitHubClient and a stubbed CodeReviewAgent — no network, no LLM).
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from unittest.mock import MagicMock

import httpx
import pytest

from coding_team.github_source import (
    GitHubAPIError,
    GitHubClient,
    PullRequestDetail,
    PullRequestFile,
)

from .test_github_source import _stub_heavy_modules

# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubClient:
    transport = httpx.MockTransport(handler)
    client = GitHubClient(token="t", sleep=lambda _s: None)
    client._client.close()  # type: ignore[attr-defined]
    client._client = httpx.Client(transport=transport, timeout=client._timeout)  # type: ignore[attr-defined]
    return client


def _pr_payload(number: int = 7, **overrides: Any) -> dict[str, Any]:
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
# Client: create_pull_request_review
# ---------------------------------------------------------------------------


class TestCreatePullRequestReview:
    def test_posts_expected_body(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            import json as _json

            captured["url"] = str(req.url)
            captured["body"] = _json.loads(req.content)
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
            import json as _json

            captured["body"] = _json.loads(req.content)
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
            import json as _json

            captured["url"] = str(req.url)
            captured["body"] = _json.loads(req.content)
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
            import json as _json

            captured["body"] = _json.loads(req.content)
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
        """Non-positive line, bad side, and empty path/body each raise ValueError."""
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
    def __init__(self, issues: list[Any], summary: str = "S", spec: str = "SC") -> None:
        self.issues = issues
        self.summary = summary
        self.spec_compliance_notes = spec
        self.suggested_commit_message = ""


class _FakeReviewIssue:
    """Duck-typed stand-in for a CodeReviewIssue, with the attributes the PR-review
    flow reads (severity, category, file_path, line, description, suggestion)."""

    def __init__(
        self,
        severity: str,
        line: Optional[int],
        file_path: str = "a.py",
        description: str = "desc",
    ) -> None:
        self.severity = severity
        self.category = "logic"
        self.file_path = file_path
        self.line = line
        self.description = description
        self.suggestion = "fix"


class _FakeReviewClient:
    """Fake GitHubClient surface the review endpoint + hook touch.

    Configurable failure knobs (all default to "never fail"):
        - ``fail_get_pr``: ``get_pull_request`` raises a 404 ``GitHubAPIError``.
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
        self.review_fail_times = 0  # number of leading create_review calls that fail
        self.review_fail_status = 422  # status raised by review_fail_times / bad_lines
        self.bad_lines: set[int] = set()  # lines whose review 422s (drives bisection)
        self.review_comment_fail_paths: set[str] = set()  # paths that 422 as file comments
        self.review_comment_fail_status = 422  # status raised by review_comment_fail_paths
        self.review_exc: Optional[Exception] = None  # non-API error to raise on submit
        self.comment_fail_times = 0  # number of leading add_issue_comment calls that 422
        self._comment_calls = 0

    def __enter__(self) -> "_FakeReviewClient":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def get_pull_request(self, _o: str, _r: str, n: int) -> PullRequestDetail:
        if self.fail_get_pr:
            raise GitHubAPIError(404, "missing PR")
        return PullRequestDetail(
            number=n,
            html_url=f"https://example/pull/{n}",
            head="feature",
            base="main",
            head_sha="sha1",
            title="Add feature",
            body="body",
            draft=False,
            author=self.author,
            state="open",
            updated_at="2026-01-01T00:00:00Z",
            labels=(),
        )

    def get_pull_request_files(self, _o: str, _r: str, _n: int) -> list[PullRequestFile]:
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
        self.reactions.append((n, content))


@pytest.fixture
def review_app(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _stub_heavy_modules()

    from job_service_client_fake import FakeJobServiceClient

    fake_jobs = FakeJobServiceClient(team="coding_team")
    from coding_team import job_store as job_store_mod

    monkeypatch.setattr(job_store_mod, "_client", lambda *a, **kw: fake_jobs)
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.delenv("PR_REVIEW_EVENT", raising=False)

    from coding_team.api import main as api_main

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

    monkeypatch.setattr("coding_team.engine_provider._provider", _FakeProvider())

    from fastapi.testclient import TestClient

    return {
        "client": TestClient(api_main.app),
        "api": api_main,
        "repo_path": str(tmp_path),
        "github": holder,
        "jobs": fake_jobs,
    }


def _review_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "owner": "o",
        "repo": "r",
        "repo_path": overrides.pop("repo_path", "/tmp/x"),
        "pr_number": 7,
    }
    body.update(overrides)
    return body


class TestReviewEndpoint:
    def test_happy_path_posts_review(self, review_app) -> None:
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        data = resp.json()
        assert data["pr_number"] == 7
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
        review = review_app["github"]["client"].reviews[0]
        assert "ghp_SECRETTOKEN" not in review["body"]
        assert "https://***@" in review["body"]
        assert "ghp_SECRETTOKEN" not in review["comments"][0]["body"]

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

    def test_leftover_finding_anchored_as_file_level_review_comment(self, review_app) -> None:
        # A finding whose file is not in the PR diff is re-anchored as a file-level
        # inline review comment on the first changed file ("a.py"), rather than
        # posted as a standalone conversation comment.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("low", line=4, file_path="not_in_diff.py", description="orphan")
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        # The finding is re-anchored as a file-level comment on "a.py", posted via
        # the dedicated endpoint — no standalone conversation comments for findings.
        assert gh.comments == []
        file_comments = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert len(file_comments) >= 1
        assert file_comments[0]["path"] == "a.py"
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["file_comments"] >= 1
        assert job["review_summary"]["comment_findings"] == 0

    def test_reviewer_none_output_fails_job(self, review_app) -> None:
        # A provider that returns None WITHOUT raising must fail the job (and post
        # a PR notice), never leave it wedged in "running" with no terminal write.
        # The pre-decomposition body reached the same failed state by dereferencing
        # `output.issues` into the outer except; _run_reviewer must preserve it.
        review_app["github"]["agent_output"] = None
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # The reviewer watches the PR, so the failure is surfaced there too.
        gh = review_app["github"]["client"]
        assert any("reviewer returned no output" in body for _n, body in gh.comments)

    def test_missing_token_returns_400(self, review_app, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        resp = review_app["client"].post(
            "/review-pr", json={**_review_body(), "github_token": None}
        )
        assert resp.status_code == 400

    def test_pr_not_found_returns_502(self, review_app) -> None:
        review_app["github"]["client"].fail_get_pr = True
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 502

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
        reviews until that future time passes. Mirrors _answer_wait_heartbeat_fresh."""
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

        def first() -> None:
            with api._pr_review_admission("o", "r", 7):
                entered.set()
                release.wait(5)
                order.append("first-exit")

        def second() -> None:
            with api._pr_review_admission("o", "r", 7):
                order.append("second-enter")

        t1 = _threading.Thread(target=first)
        t1.start()
        assert entered.wait(5)
        t2 = _threading.Thread(target=second)
        t2.start()
        _time.sleep(0.1)  # give second a window to (wrongly) enter while first holds
        assert "second-enter" not in order
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

        import shared_postgres

        monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
        monkeypatch.setattr(shared_postgres, "get_conn", _fake_conn)
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
        import shared_postgres

        api = review_app["api"]
        monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
        monkeypatch.setattr(
            shared_postgres, "get_conn", MagicMock(side_effect=RuntimeError("pg down"))
        )
        with api._pr_review_admission("o", "r", 7):
            pass  # must not raise

    def test_review_run_wraps_body_in_liveness_heartbeat(self, review_app, monkeypatch) -> None:
        """_run_pr_review must hold a continuous heartbeat for the job while the review
        runs (a single review LLM call can outlast the staleness cutoff), stopping it on
        exit — asserted via the context-manager protocol on a recording stand-in."""
        import shared_concurrency

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

        monkeypatch.setattr(shared_concurrency, "BackgroundHeartbeat", _RecordingHB)
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        assert seen["entered"] and seen["exited"]
        assert seen["interval"] == api._REVIEW_HEARTBEAT_INTERVAL_S
        # The beat touches the job's liveness stamp via the job service.
        seen["beat"]()
        job_id = resp.json()["job_id"]
        assert review_app["jobs"].get_job(job_id) is not None

    def test_heartbeat_job_touches_job_service(self, review_app) -> None:
        from coding_team import job_store as job_store_mod

        fake_jobs = review_app["jobs"]
        review_app["client"].post("/review-pr", json=_review_body())
        # Direct contract: heartbeat_job delegates to the job service's heartbeat.
        jobs = fake_jobs.list_jobs()
        assert jobs, "a review job should exist"
        job_store_mod.heartbeat_job(jobs[0]["job_id"])  # must not raise

    def test_agent_failure_marks_job_failed(self, review_app) -> None:
        review_app["github"]["agent_output"] = RuntimeError("llm down")
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # A failed job must not keep claiming mid-review progress: the failure
        # handler resets the percentage status_text and the activity entry.
        assert job["status_text"] is None
        assert job["current_activity"] is None

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
        # A single off-diff line must not collapse the whole review: the good lines
        # stay inline and only the bad one is demoted to a file-level comment.
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
        # Bisection must isolate more than one off-diff line: with two bad lines
        # among three in-diff findings, only the good line stays inline and both
        # bad lines are demoted to file-level comments (none lost to standalone).
        # (Diff valid lines for a.py are {1, 2, 3}, so all three are line-anchored.)
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=1, description="bad alpha"),
                _FakeReviewIssue("high", line=2, description="good beta"),
                _FakeReviewIssue("high", line=3, description="bad gamma"),
            ]
        )
        gh = review_app["github"]["client"]
        gh.bad_lines = {1, 3}  # two off-diff lines rejected inline by GitHub
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

    def test_leftover_finding_posted_as_inline_not_standalone(self, review_app) -> None:
        # After the fix, a finding whose file is not in the diff is re-anchored as a
        # file-level inline review comment, not posted as a standalone conversation
        # comment. The job succeeds and no add_issue_comment is called for the finding.
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
        # The review carries the line-anchored inline for "inline"; the leftover
        # is a file-level comment (re-anchored to "a.py") on the dedicated endpoint.
        assert len(gh.reviews) == 1
        line_comments = [c for c in gh.reviews[0]["comments"] if c.get("side") == "RIGHT"]
        file_comments = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert len(line_comments) == 1  # the in-diff finding
        assert len(file_comments) == 1  # the re-anchored leftover
        assert file_comments[0]["path"] == "a.py"
        # No standalone comments — the leftover is a file-level review comment now.
        assert gh.comments == []
        assert job["review_summary"]["comment_findings"] == 0

    def test_multiple_leftover_findings_all_anchored_inline(self, review_app) -> None:
        # All three findings name files absent from the diff; after the fix they are
        # each re-anchored as file-level inline review comments on "a.py" (the first
        # changed file). No add_issue_comment calls; job succeeds.
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
        # All three leftovers became file-level comments on the dedicated endpoint.
        file_comments = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert len(file_comments) == 3
        bodies = [c["body"] for c in file_comments]
        assert any("leftover one" in b for b in bodies)
        assert any("leftover two" in b for b in bodies)
        assert any("leftover three" in b for b in bodies)
        # No standalone comments.
        assert gh.comments == []
        assert job["review_summary"]["comment_findings"] == 0
        assert job["review_summary"]["file_comments"] == 3

    def test_non_api_error_marks_job_failed_not_stuck(self, review_app) -> None:
        # A non-GitHubAPIError during submit must be caught by the broad outer
        # handler so the job transitions to failed instead of wedging in 'running'.
        review_app["github"]["client"].review_exc = RuntimeError("kaboom")
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"

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
        assert len(gh.comments) >= 1, (
            f"Expected at least one standalone fallback comment, got gh.comments={gh.comments}"
        )
        assert any("dropped finding" in body for _n, body in gh.comments), (
            f"Fallback comment missing finding text: gh.comments={gh.comments}"
        )
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

    def test_summary_only_review_failure_does_not_fail_job(self, review_app) -> None:
        # With no line-anchored findings, _submit_review posts only the summary
        # body. That courtesy review carries no findings, so its failure must NOT
        # fail the job — the file-level findings still post on the dedicated
        # endpoint. (Regression guard for the empty-line-comments path.)
        gh = review_app["github"]["client"]
        gh.review_fail_times = 1  # the lone summary-only review attempt 422s
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("low", line=999, file_path="a.py", description="off-diff only")
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        # The summary never posted, but the file-level finding did — and nothing
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
        # A clean review gets a celebratory +1 reaction directly on the PR.
        assert gh.reactions == [(7, "+1")]

    def test_review_with_findings_does_not_react(self, review_app) -> None:
        # The +1 reaction is reserved for a truly clean review — a review that
        # found (and posted) findings must not also get the "all good" reaction.
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert gh.reactions == []
        assert gh.comments == []
        assert job["review_summary"]["comment_findings"] == 0

    def test_non_422_review_error_marks_job_failed_not_degraded(self, review_app) -> None:
        # A non-422 review failure (e.g. 403 permission / rate-limit) is a real
        # error, not a bad diff line: it must propagate and mark the job failed
        # rather than be silently degraded to file-level/standalone comments and
        # reported as completed.
        gh = review_app["github"]["client"]
        gh.review_fail_times = 1
        gh.review_fail_status = 403
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # The error propagated before any degradation: no review was posted and no
        # finding was quietly re-routed to a file-level comment. (A failure notice
        # on the PR is expected and lives in gh.comments.)
        assert gh.submitted_reviews == []
        assert gh.review_comments == []

    def test_non_422_file_comment_error_marks_job_failed_not_degraded(self, review_app) -> None:
        # A non-422 failure from the dedicated file-comment endpoint is likewise a
        # real error and must propagate, not silently fall through to a standalone
        # conversation comment.
        gh = review_app["github"]["client"]
        gh.review_comment_fail_paths = {"a.py"}
        gh.review_comment_fail_status = 403
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("low", line=999, file_path="a.py", description="off-diff find")
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        # The finding was NOT silently posted as a standalone conversation comment;
        # only the failure notice (which never carries the finding text) is allowed.
        assert not any("off-diff find" in body for _n, body in gh.comments)

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
# These tests started as exploratory red tests and now serve as regression
# guards for the exact counterexamples that proved the original bug.
#
# Bug: findings whose file is NOT in the PR diff are posted via add_issue_comment
# (standalone top-level PR conversation comment) instead of being bundled as
# file-level inline review comments (subject_type="file") under the single review.
#
# Property 1 (design.md): For any code-review run that produces ≥1 finding, the
# fixed _run_pr_review SHALL NOT call add_issue_comment for any individual
# finding.  Every finding SHALL appear as a line-anchored OR file-level inline
# review comment under the single PR review submission.
#
# Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5
# ---------------------------------------------------------------------------


class TestBugConditionExploration:
    """Exploration tests written BEFORE the fix as part of the bugfix workflow (Task 1).

    These tests encode the CORRECT / EXPECTED behavior post-fix, but were originally
    written to FAIL against the unfixed code — that failure acted as proof the bug
    existed.  They now pass and serve as regression guards for the specific bug
    conditions identified during exploration (file-not-in-diff leftovers and 422-dropped
    comments reposted as standalone).

    Why keep these alongside TestReviewEndpoint?
    - TestReviewEndpoint tests are the canonical integration suite.  They cover the same
      scenarios but were updated (task 3.4) as part of the fix landing.
    - TestBugConditionExploration tests preserve the exact assertions written against the
      unfixed code, including the counterexample documentation, so future readers can see
      precisely what the bug looked like and what condition each test was designed to catch.
    - If a regression reintroduces standalone posting for any of these specific inputs,
      both suites will catch it; the exploration tests' error messages include
      "BUG CONFIRMED" to make the failure immediately recognisable.

    Counterexamples captured on unfixed code (Task 1 documentation):
      - test_leftover_finding_not_posted_as_standalone_comment:
          gh.comments = [(7, '`src/config.py` — **[LOW] logic** — Security issue ...')]
          review_summary["comment_findings"] = 1
          review["comments"] = []  (no subject_type="file" entry)

      - test_empty_file_path_finding_anchored_not_standalone:
          gh.comments = [(7, '**[LOW] logic** — No file path finding')]
          review_summary["comment_findings"] = 1

      - test_422_dropped_comments_reanchored_not_standalone:
          gh.comments contains bodies from inline_comment_to_timeline_body(c)
          review_summary["comment_findings"] = 2  (both inline findings dropped)

      - test_multiple_leftovers_no_standalone_comments:
          gh.comments = [(7, '`gone1.py` — ...'), (7, '`gone2.py` — ...')]
          review_summary["comment_findings"] = 2
    """

    def test_leftover_finding_not_posted_as_standalone_comment(self, review_app) -> None:
        """A finding whose file_path is NOT in the PR diff MUST NOT produce a standalone
        add_issue_comment call.  It must instead appear in the submitted review as a
        file-level inline comment (subject_type="file"), anchored to the first changed
        file in valid_by_path.

        On UNFIXED code this test FAILS:
          - gh.comments is non-empty (add_issue_comment was called for the finding)
          - review["comments"] is empty (no subject_type="file" entry exists)
          - review_summary["comment_findings"] == 1  (not 0)

        Counterexample:
          gh.comments = [(7, '`src/config.py` — **[LOW] logic** — Security issue ...')]
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

        # EXPECTED (correct) behavior: no standalone comment for the finding.
        # ACTUAL (buggy) behavior on unfixed code: gh.comments is non-empty.
        assert gh.comments == [], (
            f"BUG CONFIRMED — finding posted as standalone comment: gh.comments = {gh.comments}"
        )

        # The finding must be posted as a file-level comment on the dedicated endpoint.
        file_level = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert len(file_level) >= 1, (
            f"BUG CONFIRMED — no file-level review comment: review_comments = {gh.review_comments}"
        )

        # comment_findings must be 0: the finding was NOT posted as a standalone comment.
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["review_summary"]["comment_findings"] == 0, (
            f"BUG CONFIRMED — comment_findings = {job['review_summary']['comment_findings']}, expected 0"
        )

    def test_empty_file_path_finding_anchored_not_standalone(self, review_app) -> None:
        """A finding with an empty or None file_path MUST NOT produce a standalone
        add_issue_comment call.  It must be anchored to the first changed file in the
        diff as a file-level inline review comment.

        On UNFIXED code this test FAILS:
          - gh.comments is non-empty (add_issue_comment was called)
          - review_summary["comment_findings"] == 1  (not 0)

        Counterexample:
          gh.comments = [(7, '**[LOW] logic** — No file path finding')]
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

        # EXPECTED: no standalone comment for the finding.
        assert gh.comments == [], (
            f"BUG CONFIRMED — finding with empty file_path posted as standalone: gh.comments = {gh.comments}"
        )

        # The finding should be posted as a file-level comment on the dedicated endpoint.
        file_level = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert len(file_level) >= 1, (
            f"BUG CONFIRMED — no file-level comment for empty-path finding: review_comments = {gh.review_comments}"
        )

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["review_summary"]["comment_findings"] == 0, (
            f"BUG CONFIRMED — comment_findings = {job['review_summary']['comment_findings']}, expected 0"
        )

    def test_multiple_leftovers_no_standalone_comments(self, review_app) -> None:
        """Multiple findings whose files are not in the PR diff MUST each appear as a
        file-level inline review comment — zero standalone add_issue_comment calls.

        On UNFIXED code this test FAILS:
          - gh.comments has 2 entries (one per leftover finding)
          - review_summary["comment_findings"] == 2  (not 0)

        Counterexample:
          gh.comments = [(7, '`gone1.py` — **[HIGH] logic** — issue one'),
                         (7, '`gone2.py` — **[LOW] logic** — issue two')]
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

        # EXPECTED: no standalone comments for findings.
        assert gh.comments == [], (
            f"BUG CONFIRMED — {len(gh.comments)} standalone comment(s) posted for leftover findings: "
            f"gh.comments = {gh.comments}"
        )

        # Both findings must appear as file-level comments on the dedicated endpoint.
        file_level = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert len(file_level) == 2, (
            f"BUG CONFIRMED — expected 2 file-level comments, got {len(file_level)}: "
            f"review_comments = {gh.review_comments}"
        )

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["review_summary"]["comment_findings"] == 0, (
            f"BUG CONFIRMED — comment_findings = {job['review_summary']['comment_findings']}, expected 0"
        )

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
        identical to routing it in isolation.

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
        # file-comment endpoint (file-level); aggregate both to see its routing.
        # Line-anchored (review) comments come first so a line match wins over a
        # same-path file-level re-anchor of an unrelated finding.
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
                file_path=fp if fp is not None else "",
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
        assert len(in_diff_mixed) >= 1, (
            f"[{description}] Mixed run: no comment found for in-diff finding "
            f"(path={in_path!r}, line={in_line}). all_comments={all_comments_mixed}"
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
# Task 3.5 — Unit tests for anchor_to_first_file pure helper
# ---------------------------------------------------------------------------


class TestAnchorToFirstFileUnit:
    """Unit tests for the anchor_to_first_file pure helper."""

    def test_empty_valid_by_path_returns_none(self) -> None:
        """Empty valid_by_path → returns None (no file to anchor to)."""
        from coding_team.github_source import anchor_to_first_file

        finding = _FakeReviewIssue("low", line=4, file_path="missing.py", description="issue")
        assert anchor_to_first_file(finding, {}) is None

    def test_body_matches_format_comment_body(self) -> None:
        """The body field in the returned dict equals format_comment_body(finding)."""
        from coding_team.github_source import anchor_to_first_file, format_comment_body

        finding = _FakeReviewIssue(
            "high", line=5, file_path="src/config.py", description="Security issue"
        )
        valid_by_path = {"src/api.py": {1, 2, 3}}
        result = anchor_to_first_file(finding, valid_by_path)
        assert result is not None
        expected_body = format_comment_body(finding)
        assert result["body"] == expected_body

    def test_returns_first_key_of_valid_by_path(self) -> None:
        """The first key in insertion order is used as the anchor path (Python 3.7+)."""
        from coding_team.github_source import anchor_to_first_file

        finding = _FakeReviewIssue("low", line=1, file_path="not_here.py", description="x")
        valid_by_path = {"first_file.py": {1}, "second_file.py": {2}, "third_file.py": {3}}
        result = anchor_to_first_file(finding, valid_by_path)
        assert result is not None
        assert result["path"] == "first_file.py"

    def test_single_entry_valid_by_path(self) -> None:
        """Single-entry valid_by_path → anchors to that one file, no line key."""
        from coding_team.github_source import anchor_to_first_file

        finding = _FakeReviewIssue("high", line=10, file_path="gone.py", description="gone")
        valid_by_path = {"only_file.py": {5, 6, 7}}
        result = anchor_to_first_file(finding, valid_by_path)
        assert result is not None
        assert result["path"] == "only_file.py"
        assert result["subject_type"] == "file"
        assert result.get("line") is None  # no line key for file-level comments

    # ------------------------------------------------------------------
    # Task 3.5 — explicitly-named unit tests (required by spec)
    # ------------------------------------------------------------------

    def test_anchor_to_first_file_returns_file_level_comment(self) -> None:
        """Finding absent from diff → returned dict has path == first key of
        valid_by_path and subject_type == "file".

        Validates: Requirements 2.2, 2.5
        """
        from coding_team.github_source import anchor_to_first_file

        finding = _FakeReviewIssue("low", line=4, file_path="src/config.py", description="issue")
        valid_by_path = {"src/api.py": {1, 2, 3}, "src/utils.py": {5, 6}}
        result = anchor_to_first_file(finding, valid_by_path)
        assert result is not None
        assert result["path"] == "src/api.py"  # must be the FIRST key
        assert result["subject_type"] == "file"

    def test_anchor_to_first_file_empty_valid_by_path_returns_none(self) -> None:
        """Empty valid_by_path → anchor_to_first_file returns None.

        Validates: Requirements 2.2, 2.5
        """
        from coding_team.github_source import anchor_to_first_file

        finding = _FakeReviewIssue("low", line=4, file_path="missing.py", description="issue")
        assert anchor_to_first_file(finding, {}) is None

    def test_anchor_to_first_file_body_matches_format_comment_body(self) -> None:
        """The body field of the returned dict exactly equals format_comment_body(finding).

        Validates: Requirements 2.2, 2.5
        """
        from coding_team.github_source import anchor_to_first_file, format_comment_body

        finding = _FakeReviewIssue(
            "high", line=5, file_path="src/config.py", description="Security issue"
        )
        valid_by_path = {"src/api.py": {1, 2, 3}}
        result = anchor_to_first_file(finding, valid_by_path)
        assert result is not None
        assert result["body"] == format_comment_body(finding)


# ---------------------------------------------------------------------------
# Task 3.5 — Integration tests for fixed _run_pr_review
# ---------------------------------------------------------------------------


class TestFixedRunPrReview:
    """Integration tests verifying the fixed _run_pr_review behaviour for
    leftover findings and for the 'no standalone comments' invariant.

    Validates: Requirements 2.1, 2.2, 2.4, 2.5
    """

    def test_leftover_finding_anchored_as_file_level_inline(self, review_app) -> None:
        """Full _run_pr_review with a leftover finding: the review carries an entry
        with subject_type="file" and gh.add_issue_comment is NOT called for the
        finding.

        A finding whose file_path is NOT in the PR diff (valid_by_path only
        contains "a.py") must be re-anchored as a file-level inline review
        comment on the first changed file — it must never appear as a standalone
        top-level PR conversation comment.

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

        # gh.add_issue_comment must NOT be called for the finding.
        assert gh.comments == [], (
            f"add_issue_comment was called for the leftover finding: gh.comments = {gh.comments}"
        )

        # The finding must be posted as a file-level comment on the dedicated endpoint.
        file_level = [c for c in gh.review_comments if c.get("subject_type") == "file"]
        assert len(file_level) >= 1, (
            f"Expected at least 1 file-level review comment, got 0. "
            f"review_comments = {gh.review_comments}"
        )
        # The anchor path must be the first changed file ("a.py").
        assert file_level[0]["path"] == "a.py", (
            f"Expected anchor path 'a.py', got {file_level[0]['path']!r}"
        )

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["comment_findings"] == 0

    def test_no_standalone_comments_for_any_findings(self, review_app) -> None:
        """Mix of on-diff, off-diff-line, and off-diff-file findings → gh.comments == []
        after the full flow.

        Three findings are submitted:
          1. On-diff (file="a.py", line=2)  → line-anchored inline comment.
          2. Off-diff-line (file="a.py", line=999) → file-level inline comment.
          3. Off-diff-file (file="not_in_diff.py", line=1) → re-anchored file-level
             inline comment on "a.py".

        After the full _run_pr_review flow, gh.comments (add_issue_comment calls)
        must be empty — every finding rode on the single PR review submission.

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

        # Core assertion: no standalone comments for any finding.
        assert gh.comments == [], (
            f"Expected gh.comments == [] (no standalone comments), but got {gh.comments}"
        )

        # All three findings must be present: one inline on the review, two on the
        # dedicated file-comment endpoint.
        assert len(gh.reviews) >= 1
        line_comments = [c for c in gh.reviews[0].get("comments", []) if c.get("side") == "RIGHT"]
        file_comments = [c for c in gh.review_comments if c.get("subject_type") == "file"]

        # on-diff finding → line-anchored
        assert len(line_comments) == 1, (
            f"Expected 1 line-anchored comment (on-diff), got {len(line_comments)}. "
            f"review comments = {gh.reviews[0].get('comments', [])}"
        )
        assert line_comments[0]["line"] == 2

        # off-diff-line + off-diff-file → both file-level on the dedicated endpoint
        assert len(file_comments) == 2, (
            f"Expected 2 file-level comments (off-diff-line + off-diff-file), "
            f"got {len(file_comments)}. review_comments = {gh.review_comments}"
        )

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["comment_findings"] == 0


# ---------------------------------------------------------------------------
# Whole-file review path (_fetch_head_files + files-mode dispatch)
# ---------------------------------------------------------------------------


class TestWholeFileReview:
    def test_fetch_head_files_returns_whole_files_and_skips_binary(self, review_app) -> None:
        from coding_team.api.pr_review import _fetch_head_files

        files = [
            PullRequestFile("a.py", "modified", "@@ -1 +1 @@\n+x", 1, 0, None),
            PullRequestFile("gone.py", "removed", "@@ -1 +0 @@\n-x", 0, 1, None),
            PullRequestFile("img.png", "added", "", 0, 0, None),  # binary: no patch
        ]

        class _C:
            def get_file_contents(self, o, r, path, ref):
                assert ref == "sha1"
                return "WHOLE\n" if path == "a.py" else None

        out = _fetch_head_files(_C(), "o", "r", files, "sha1")
        assert out == {"a.py": "WHOLE\n"}  # removed + binary skipped

    def test_fetch_head_files_degrades_on_client_without_method(self, review_app) -> None:
        from coding_team.api.pr_review import _fetch_head_files

        files = [PullRequestFile("a.py", "modified", "@@ -1 +1 @@\n+x", 1, 0, None)]
        # A client missing get_file_contents must degrade to {} (hunk fallback),
        # not raise.
        assert _fetch_head_files(object(), "o", "r", files, "sha1") == {}

    def test_fetch_head_files_concurrent_fetches_do_not_corrupt_results(self, review_app) -> None:
        """_fetch_head_files fans per-file GETs out across a thread pool; each
        worker's (filename, content) pair must land under its own key, never a
        sibling's, even when several fetches are in flight at once.
        """
        import threading
        import time

        from coding_team.api.pr_review import _fetch_head_files

        num_files = 16
        files = [
            PullRequestFile(f"f{i}.py", "modified", f"@@ -1 +1 @@\n+x{i}", 1, 0, None)
            for i in range(num_files)
        ]
        seen_threads: set[int] = set()
        lock = threading.Lock()

        class _C:
            def get_file_contents(self, o, r, path, ref):
                with lock:
                    seen_threads.add(threading.get_ident())
                time.sleep(0.01)  # widen the race window so fetches overlap
                return f"WHOLE-{path}\n"

        out = _fetch_head_files(_C(), "o", "r", files, "sha1")
        assert out == {f"f{i}.py": f"WHOLE-f{i}.py\n" for i in range(num_files)}
        assert len(seen_threads) > 1  # confirms the fetches actually ran concurrently

    def test_endpoint_uses_whole_files_and_passes_reader(self, review_app, monkeypatch) -> None:
        from coding_team.github_source import GitHubRepoReader

        gh = review_app["github"]["client"]
        gh.get_file_contents = lambda o, r, path, ref: "def a():\n    return 1\n"
        gh.get_repository_tree = lambda o, r, ref, recursive=True: ["a.py"]

        captured: dict[str, Any] = {}

        class _CapProvider:
            def run_pr_code_review(self, **kw: Any) -> Any:
                captured.update(kw)
                return _FakeOutput(issues=[])

        monkeypatch.setattr("coding_team.engine_provider._provider", _CapProvider())

        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        # Whole-file mode: files mapping passed, pre_numbered off, reader supplied.
        assert captured["files"] == {"a.py": "def a():\n    return 1\n"}
        assert captured["pre_numbered"] is False
        assert isinstance(captured["repo_reader"], GitHubRepoReader)
        assert "code" not in captured or not captured.get("code")

    def test_endpoint_falls_back_to_hunks_when_no_head_files(self, review_app, monkeypatch) -> None:
        gh = review_app["github"]["client"]
        # Head fetch yields nothing -> hunk fallback (pre_numbered code blob).
        gh.get_file_contents = lambda o, r, path, ref: None

        captured: dict[str, Any] = {}

        class _CapProvider:
            def run_pr_code_review(self, **kw: Any) -> Any:
                captured.update(kw)
                return _FakeOutput(issues=[])

        monkeypatch.setattr("coding_team.engine_provider._provider", _CapProvider())

        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        assert captured.get("files") is None
        assert captured["pre_numbered"] is True
        assert captured["code"]  # the hunk-rendered blob

    def test_whole_file_mode_appends_focus_note(self, review_app, monkeypatch) -> None:
        gh = review_app["github"]["client"]  # default: single reviewable file a.py
        gh.get_file_contents = lambda o, r, path, ref: "def a():\n    return 1\n"
        gh.get_repository_tree = lambda o, r, ref, recursive=True: ["a.py"]

        captured: dict[str, Any] = {}

        class _CapProvider:
            def run_pr_code_review(self, **kw: Any) -> Any:
                captured.update(kw)
                return _FakeOutput(issues=[])

        monkeypatch.setattr("coding_team.engine_provider._provider", _CapProvider())
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        # Whole-file mode steers the reviewer to focus on the change, not unchanged code.
        from coding_team.api.pr_review import WHOLE_FILE_FOCUS_NOTE_PREFIX

        assert WHOLE_FILE_FOCUS_NOTE_PREFIX in captured["task_requirements"]

    def test_partial_head_fetch_falls_back_to_hunks(self, review_app, monkeypatch) -> None:
        gh = review_app["github"]["client"]
        # Two reviewable files; only one fetches whole content.
        gh.files = [
            PullRequestFile("a.py", "modified", "@@ -1,2 +1,3 @@\n ctx\n+added\n more", 1, 0, None),
            PullRequestFile("b.py", "modified", "@@ -1,1 +1,2 @@\n x\n+y", 1, 0, None),
        ]
        gh.get_file_contents = lambda o, r, path, ref: "whole a\n" if path == "a.py" else None
        gh.get_repository_tree = lambda o, r, ref, recursive=True: []

        captured: dict[str, Any] = {}

        class _CapProvider:
            def run_pr_code_review(self, **kw: Any) -> Any:
                captured.update(kw)
                return _FakeOutput(issues=[])

        monkeypatch.setattr("coding_team.engine_provider._provider", _CapProvider())
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        # Only 1 of 2 reviewable files fetched -> must NOT silently drop b.py.
        # Falls back to hunk mode (covers every changed file).
        assert captured.get("files") is None
        assert captured["pre_numbered"] is True
        assert captured["code"]

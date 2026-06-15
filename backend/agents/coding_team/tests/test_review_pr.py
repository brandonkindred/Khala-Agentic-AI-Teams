"""Tests for the PR-review flow.

Covers the new GitHub client methods (via httpx.MockTransport), and the
POST /review-pr endpoint + background hook in api/main.py (with a fake
GitHubClient and a stubbed CodeReviewAgent — no network, no LLM).
"""

from __future__ import annotations

import sys
import types
from typing import Any, Callable, Optional

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
        - ``review_exc``: a non-API exception raised on every review submit (to
          test the broad outer error handler).
        - ``comment_fail_times``: the first N ``add_issue_comment`` calls raise a
          403, exercising the per-finding comment failure path.
    Captured side effects: ``reviews`` (each submitted review's kwargs) and
    ``comments`` (each posted ``(issue_number, body)``).
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
        self.reviews: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.fail_get_pr = False
        self.review_fail_times = 0  # number of leading create_review calls that 422
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
        if len(self.reviews) < self.review_fail_times:
            self.reviews.append(kwargs)
            raise GitHubAPIError(422, "bad line")
        self.reviews.append(kwargs)
        return {"id": 1, "html_url": "https://example/review/1"}


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

    # Stub the lazily-imported reviewer so no LLM stack loads.
    holder["agent_output"] = _FakeOutput(
        issues=[_FakeReviewIssue("high", line=2), _FakeReviewIssue("low", line=999)]
    )

    class _FakeAgent:
        def run(self, _inp: Any, progress_callback: Any = None) -> Any:
            out = holder["agent_output"]
            if isinstance(out, Exception):
                raise out
            return out

    stub = types.ModuleType("software_engineering_team.code_review_agent")
    stub.CodeReviewAgent = _FakeAgent  # type: ignore[attr-defined]
    stub.CodeReviewInput = lambda **kw: kw  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "software_engineering_team.code_review_agent", stub)

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
        # The in-diff line (2) is an inline comment; the out-of-diff line (999) is not.
        assert review["comments"] == [c for c in review["comments"] if c["line"] == 2]
        assert len(review["comments"]) == 1
        # The body is summary-only — no finding is batched into it.
        assert "General findings" not in review["body"]
        # The out-of-diff finding (line 999) is posted as its own conversation
        # comment carrying the finding's formatted content (severity + file + desc).
        assert len(gh.comments) == 1
        assert gh.comments[0][0] == 7
        leftover_body = gh.comments[0][1]
        assert "desc" in leftover_body
        assert "[LOW]" in leftover_body  # severity label from format_comment_body
        assert "a.py" in leftover_body  # location prefix
        # Job completed with the PR url + review summary.
        job = review_app["jobs"].get_job(data["job_id"])
        assert job["status"] == "completed"
        assert job["github_pr_url"] == "https://example/pull/7"
        assert job["review_summary"]["inline_comments"] == 1
        assert job["review_summary"]["comment_findings"] == 1

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

    def test_multiple_unanchorable_findings_each_get_own_comment(self, review_app) -> None:
        # The core contract: every un-anchorable finding produces its OWN comment
        # and no comment lists more than one finding (never batched).
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=999, description="first leftover"),
                _FakeReviewIssue("low", line=1000, description="second leftover"),
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        gh = review_app["github"]["client"]
        # No inline comments (both lines are out of diff); two separate comments.
        assert len(gh.reviews) == 1
        assert gh.reviews[0]["comments"] == []
        assert len(gh.comments) == 2
        bodies = [body for _n, body in gh.comments]
        # Each comment carries exactly one finding (the two never share a comment).
        assert sum("first leftover" in b for b in bodies) == 1
        assert sum("second leftover" in b for b in bodies) == 1
        for b in bodies:
            assert not ("first leftover" in b and "second leftover" in b)
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["review_summary"]["comment_findings"] == 2

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
        # The retry kept the inline comment, so only the out-of-diff finding is a
        # standalone comment — the inline finding is not re-posted.
        assert len(gh.comments) == 1

    def test_dropped_inline_findings_reposted_as_comments(self, review_app) -> None:
        # When every attempt that carries inline comments 422s, the review
        # degrades to a body-only COMMENT and the dropped inline finding must be
        # re-posted as its own conversation comment so nothing is lost.
        gh = review_app["github"]["client"]
        gh.review_fail_times = 2  # both comment-carrying attempts 422; body-only wins
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert len(gh.reviews) == 3
        assert gh.reviews[-1]["event"] == "COMMENT"
        assert gh.reviews[-1]["comments"] == []
        # Two standalone comments: the out-of-diff finding + the dropped inline one.
        assert len(gh.comments) == 2
        # The dropped inline finding carries its `path:line` location.
        assert any("a.py:2" in body for _n, body in gh.comments)
        assert job["review_summary"]["inline_comments"] == 0
        assert job["review_summary"]["comment_findings"] == 2

    def test_multiple_dropped_inline_findings_each_reposted(self, review_app) -> None:
        # Several inline findings dropped by the body-only fallback must each be
        # reposted as their own comment (one per finding, never batched).
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=2, description="inline one"),
                _FakeReviewIssue("high", line=3, description="inline two"),
            ]
        )
        gh = review_app["github"]["client"]
        gh.review_fail_times = 2  # both comment-carrying attempts 422; body-only wins
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert gh.reviews[-1]["comments"] == []
        # Two distinct reposted comments, each anchored to its own `path:line`.
        assert len(gh.comments) == 2
        bodies = [body for _n, body in gh.comments]
        assert any("a.py:2" in b for b in bodies)
        assert any("a.py:3" in b for b in bodies)
        assert job["review_summary"]["inline_comments"] == 0
        assert job["review_summary"]["comment_findings"] == 2

    def test_failed_finding_comment_marks_job_failed(self, review_app) -> None:
        # A finding posted as its own comment no longer lives in the review body,
        # so a rejected comment would drop the finding. The job must report failure
        # (with a count) rather than claiming every finding was posted.
        gh = review_app["github"]["client"]
        gh.comment_fail_times = 1  # the out-of-diff finding's standalone comment 422s
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "could not be posted" in (job["error"] or "")
        assert job["review_summary"]["comments_failed"] == 1
        # The review itself was still submitted (inline comment for the in-diff line).
        assert len(gh.reviews) == 1
        # The author is notified on the PR that part of the review is missing
        # (the finding comment failed, but the follow-up notification succeeds).
        assert any("could not be posted" in body for _n, body in gh.comments)

    def test_partial_finding_comment_failures_counted(self, review_app) -> None:
        # With several standalone findings where only a subset fail to post, the
        # job is still failed and comments_failed reflects the exact failure count
        # while the successful comments are kept.
        review_app["github"]["agent_output"] = _FakeOutput(
            issues=[
                _FakeReviewIssue("high", line=999, description="leftover one"),
                _FakeReviewIssue("high", line=1000, description="leftover two"),
                _FakeReviewIssue("low", line=1001, description="leftover three"),
            ]
        )
        gh = review_app["github"]["client"]
        gh.comment_fail_times = 2  # first two finding comments 422; the third succeeds
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert job["review_summary"]["comment_findings"] == 3
        assert job["review_summary"]["comments_failed"] == 2
        # The surviving finding comment + the partial-failure notification posted.
        bodies = [body for _n, body in gh.comments]
        assert any("leftover three" in b for b in bodies)
        assert any("could not be posted" in b for b in bodies)

    def test_non_api_error_marks_job_failed_not_stuck(self, review_app) -> None:
        # A non-GitHubAPIError during submit must be caught by the broad outer
        # handler so the job transitions to failed instead of wedging in 'running'.
        review_app["github"]["client"].review_exc = RuntimeError("kaboom")
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"

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

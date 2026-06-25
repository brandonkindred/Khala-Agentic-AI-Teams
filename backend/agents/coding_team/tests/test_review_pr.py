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
        # The in-diff line (2) is a line-anchored comment; the out-of-diff line
        # (999) on the same changed file attaches to the review as a file-level
        # comment — both ride on the single review, neither in the body.
        line_comments = [c for c in review["comments"] if "line" in c]
        file_comments = [c for c in review["comments"] if c.get("subject_type") == "file"]
        assert len(line_comments) == 1 and line_comments[0]["line"] == 2
        assert len(file_comments) == 1 and file_comments[0]["path"] == "a.py"
        assert "line" not in file_comments[0]
        assert len(review["comments"]) == 2
        # The body is summary-only — no finding is batched into it.
        assert "General findings" not in review["body"]
        # No loose conversation comments: every finding rode on the review.
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
        # One review carrying two file-level comments; no loose conversation comments.
        assert len(gh.reviews) == 1
        review_comments = gh.reviews[0]["comments"]
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
        # The finding is re-anchored as a file-level inline comment on "a.py" —
        # no standalone conversation comments for findings.
        assert gh.comments == []
        review_comments = gh.reviews[0]["comments"]
        file_comments = [c for c in review_comments if c.get("subject_type") == "file"]
        assert len(file_comments) >= 1
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["file_comments"] >= 1
        assert job["review_summary"]["comment_findings"] == 0

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
        # The retry kept both review comments (the in-diff inline + the off-diff
        # file-level one), so nothing degraded to a loose conversation comment.
        assert gh.reviews[-1]["comments"] == gh.reviews[0]["comments"]
        assert len(gh.reviews[-1]["comments"]) == 2
        assert gh.comments == []

    def test_dropped_inline_findings_reanchored_as_file_level(self, review_app) -> None:
        # When every attempt that carries comments 422s, the review degrades to a
        # body-only COMMENT. The dropped findings are then re-anchored as file-level
        # inline review comments in a follow-up review — no standalone comments.
        gh = review_app["github"]["client"]
        gh.review_fail_times = 2  # both comment-carrying attempts 422; body-only wins
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        # At least 4 reviews: REQUEST_CHANGES (422) + COMMENT (422) + body-only +
        # re-anchor follow-up with file-level inline comments.
        assert len(gh.reviews) >= 4
        # No standalone comments — dropped findings are re-anchored inline.
        assert gh.comments == []
        # The last review (re-anchor) carries file-level inline comments for dropped findings.
        last_review = gh.reviews[-1]
        file_comments = [c for c in last_review["comments"] if c.get("subject_type") == "file"]
        assert len(file_comments) >= 1
        assert job["review_summary"]["comment_findings"] == 0

    def test_multiple_dropped_inline_findings_each_reanchored_as_file_level(self, review_app) -> None:
        # Several inline findings dropped by the body-only fallback are each
        # re-anchored as file-level inline review comments (never posted standalone).
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
        # No standalone comments — both dropped findings are re-anchored inline.
        assert gh.comments == []
        # The re-anchor follow-up review carries file-level inline comments for both.
        last_review = gh.reviews[-1]
        file_comments = [c for c in last_review["comments"] if c.get("subject_type") == "file"]
        assert len(file_comments) == 2
        bodies = [c["body"] for c in file_comments]
        assert any("inline one" in b for b in bodies)
        assert any("inline two" in b for b in bodies)
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
        # The review carries both: the line-anchored inline for "inline" and the
        # file-level inline for "leftover" (re-anchored to "a.py").
        assert len(gh.reviews) == 1
        review_comments = gh.reviews[0]["comments"]
        line_comments = [c for c in review_comments if c.get("side") == "RIGHT"]
        file_comments = [c for c in review_comments if c.get("subject_type") == "file"]
        assert len(line_comments) == 1  # the in-diff finding
        assert len(file_comments) == 1  # the re-anchored leftover
        # No standalone comments — the leftover is inline now.
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
        # All three leftovers became file-level inline comments in one review.
        assert len(gh.reviews) == 1
        review_comments = gh.reviews[0]["comments"]
        file_comments = [c for c in review_comments if c.get("subject_type") == "file"]
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

    def test_reanchor_follow_up_422_falls_back_to_standalone(self, review_app) -> None:
        # Last-resort path: body-only review succeeds but the re-anchor follow-up
        # also fails with a GitHubAPIError.  In that extreme case the dropped
        # findings are posted as standalone top-level comments so they are not
        # silently lost.
        #
        # Sequence:
        #   attempt 1 — REQUEST_CHANGES + comments  → 422  (review_fail_times=2)
        #   attempt 2 — COMMENT + comments          → 422
        #   attempt 3 — body-only COMMENT           → succeeds (comments dropped)
        #   re-anchor _submit_review call            → GitHubAPIError (patched)
        #   last resort → _safe_comment for each dropped finding (standalone)
        gh = review_app["github"]["client"]
        gh.review_fail_times = 2  # first two comment-carrying attempts 422; body-only wins

        # Patch _submit_review in api_main so the re-anchor follow-up call raises,
        # triggering the last-resort standalone path.
        api_main = review_app["api"]
        original_submit = api_main._submit_review
        call_count = {"n": 0}

        def _submit_failing_on_second_call(*args: Any, **kwargs: Any) -> list:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                # This is the re-anchor follow-up call — make it fail.
                raise GitHubAPIError(422, "re-anchor also failed")
            return original_submit(*args, **kwargs)

        import unittest.mock as _mock
        with _mock.patch.object(api_main, "_submit_review", side_effect=_submit_failing_on_second_call):
            # Use a HIGH finding so event=REQUEST_CHANGES, giving 3 distinct
            # _submit_review attempts (REQUEST_CHANGES+inline, COMMENT+inline,
            # body-only). review_fail_times=2 makes the first two fail, body-only
            # succeeds and returns the dropped comments for re-anchoring.
            review_app["github"]["agent_output"] = _FakeOutput(
                issues=[_FakeReviewIssue("high", line=2, description="dropped finding")]
            )
            resp = review_app["client"].post("/review-pr", json=_review_body())

        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"

        # The re-anchor attempt also failed, so the finding falls through to
        # add_issue_comment as the absolute last resort.
        assert len(gh.comments) >= 1, (
            f"Expected at least one standalone fallback comment, got gh.comments={gh.comments}"
        )
        # Confirm the fallback comment body contains the finding text.
        assert any("dropped finding" in body for _n, body in gh.comments), (
            f"Fallback comment missing finding text: gh.comments={gh.comments}"
        )

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
# BUG CONDITION EXPLORATION TESTS  (Task 1 — exploratory, EXPECTED TO FAIL on unfixed code)
#
# These tests encode the CORRECT / EXPECTED behavior after the fix.
# They intentionally FAIL against the unfixed code — the failure confirms the bug
# exists.  Do not modify the production code or these tests until Task 3.
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

        # The submitted review must carry a file-level inline comment for the finding.
        assert len(gh.reviews) >= 1
        review_comments = gh.reviews[-1].get("comments", [])
        file_level = [c for c in review_comments if c.get("subject_type") == "file"]
        assert len(file_level) >= 1, (
            f"BUG CONFIRMED — no file-level inline comment in review: review_comments = {review_comments}"
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

        # The finding should appear in the review as a file-level inline comment.
        review_comments = gh.reviews[-1].get("comments", []) if gh.reviews else []
        file_level = [c for c in review_comments if c.get("subject_type") == "file"]
        assert len(file_level) >= 1, (
            f"BUG CONFIRMED — no file-level inline comment for empty-path finding: review_comments = {review_comments}"
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

        # Both findings must appear as file-level inline comments in the review.
        review_comments = gh.reviews[-1].get("comments", []) if gh.reviews else []
        file_level = [c for c in review_comments if c.get("subject_type") == "file"]
        assert len(file_level) == 2, (
            f"BUG CONFIRMED — expected 2 file-level inline comments, got {len(file_level)}: "
            f"review_comments = {review_comments}"
        )

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["review_summary"]["comment_findings"] == 0, (
            f"BUG CONFIRMED — comment_findings = {job['review_summary']['comment_findings']}, expected 0"
        )

    def test_422_dropped_comments_reanchored_not_standalone(self, review_app) -> None:
        """When a review submission 422s and falls back to body-only, the dropped inline
        comments MUST NOT be reposted via add_issue_comment as standalone top-level
        comments.  They must be re-anchored as file-level inline comments in a follow-up
        review submission.

        On UNFIXED code this test FAILS:
          - gh.comments has entries for the dropped inline findings
          - review_summary["comment_findings"] == 2  (not 0)

        Counterexample:
          gh.comments = [(7, '`a.py:2` — **[HIGH] logic** — desc'),
                         (7, '`a.py` — **[LOW] logic** — desc')]
          (Both dropped findings reposted as standalone conversation comments.)
        """
        gh = review_app["github"]["client"]
        # Trigger both comment-carrying attempts to 422, forcing the body-only fallback
        # where the inline comments are "dropped" and currently reposted as standalone.
        gh.review_fail_times = 2

        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200

        # EXPECTED: no standalone comments for the dropped findings.
        # The dropped comments should be re-anchored as file-level inline comments
        # in an additional review submission.
        assert gh.comments == [], (
            f"BUG CONFIRMED — dropped inline findings reposted as standalone comments: "
            f"gh.comments = {gh.comments}"
        )

        # After the body-only review, a follow-up review must carry the re-anchored comments.
        # At minimum: ≥3 reviews total (REQUEST_CHANGES attempt, COMMENT attempt, body-only +
        # follow-up re-anchor attempt).
        assert len(gh.reviews) >= 3, (
            f"Expected ≥3 review submissions (retry + body-only + re-anchor), got {len(gh.reviews)}"
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
        ("high",     1, "context line 1 high severity"),
        ("high",     2, "added line 2 high severity"),
        ("high",     3, "context line 3 high severity"),
        ("low",      1, "context line 1 low severity"),
        ("low",      2, "added line 2 low severity"),
        ("low",      3, "context line 3 low severity"),
        ("critical", 1, "critical line 1"),
        ("critical", 2, "critical line 2"),
        ("critical", 3, "critical line 3"),
        ("medium",   1, "medium line 1"),
        ("medium",   2, "medium line 2"),
        ("medium",   3, "medium line 3"),
        ("info",     1, "info line 1"),
        ("info",     2, "info line 2"),
        ("info",     3, "info line 3"),
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
            c for c in all_comments
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
        ("high",     None, "no line high severity"),
        ("high",     4,    "line 4 off-diff high"),
        ("high",     10,   "line 10 off-diff high"),
        ("high",     100,  "line 100 off-diff high"),
        ("high",     999,  "line 999 off-diff high"),
        ("low",      None, "no line low severity"),
        ("low",      4,    "line 4 off-diff low"),
        ("low",      50,   "line 50 off-diff low"),
        ("low",      500,  "line 500 off-diff low"),
        ("critical", None, "no line critical severity"),
        ("critical", 7,    "line 7 off-diff critical"),
        ("medium",   None, "no line medium severity"),
        ("medium",   20,   "line 20 off-diff medium"),
        ("info",     None, "no line info severity"),
        ("info",     999,  "line 999 off-diff info"),
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
        all_comments = []
        for rev in gh.reviews:
            all_comments.extend(rev.get("comments", []))

        file_level = [
            c for c in all_comments
            if c.get("subject_type") == "file" and c.get("path") == "a.py"
        ]
        assert len(file_level) == 1, (
            f"PRESERVATION BROKEN — expected exactly 1 file-level comment "
            f"(subject_type=file, path=a.py) for severity={severity}, line={line}, "
            f"but found {len(file_level)}. all_comments={all_comments}"
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
        (True,  ["critical"],              "COMMENT",         "self-review critical"),
        (True,  ["high"],                  "COMMENT",         "self-review high"),
        (True,  ["critical", "high"],      "COMMENT",         "self-review critical+high"),
        (True,  ["low"],                   "COMMENT",         "self-review low only"),
        (True,  ["medium"],                "COMMENT",         "self-review medium only"),
        (True,  ["info"],                  "COMMENT",         "self-review info only"),
        # reviewer ≠ author, blocking severity → REQUEST_CHANGES
        (False, ["critical"],              "REQUEST_CHANGES", "critical → REQUEST_CHANGES"),
        (False, ["high"],                  "REQUEST_CHANGES", "high → REQUEST_CHANGES"),
        (False, ["critical", "low"],       "REQUEST_CHANGES", "critical+low → REQUEST_CHANGES"),
        (False, ["high", "medium"],        "REQUEST_CHANGES", "high+medium → REQUEST_CHANGES"),
        (False, ["critical", "high"],      "REQUEST_CHANGES", "critical+high → REQUEST_CHANGES"),
        # reviewer ≠ author, no blocking severity → COMMENT
        (False, ["low"],                   "COMMENT",         "low only → COMMENT"),
        (False, ["medium"],                "COMMENT",         "medium only → COMMENT"),
        (False, ["info"],                  "COMMENT",         "info only → COMMENT"),
        (False, ["low", "medium"],         "COMMENT",         "low+medium → COMMENT"),
        (False, ["low", "info"],           "COMMENT",         "low+info → COMMENT"),
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
            gh.login = "alice"   # reviewer == author
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
            ("high",   2,    "a.py", "in-diff line-anchored"),
            [("low",   4,    "gone.py",     "out-of-diff")],
        ),
        (
            "one in-diff file-level + one out-of-diff",
            ("low",    999,  "a.py", "in-diff file-level"),
            [("high",  1,    "absent.py",   "out-of-diff")],
        ),
        (
            "in-diff line-anchored + two out-of-diff",
            ("medium", 3,    "a.py", "in-diff line 3"),
            [
                ("low",    2, "missing1.py",  "out-of-diff 1"),
                ("high",   1, "missing2.py",  "out-of-diff 2"),
            ],
        ),
        (
            "in-diff file-level + empty-path finding",
            ("low",    50,   "a.py", "in-diff file-level"),
            [("info",  None, "",             "empty file_path")],
        ),
        (
            "in-diff file-level + none-path finding",
            ("low",    100,  "a.py", "in-diff file-level 100"),
            [("info",  None, None,           "None file_path")],
        ),
        (
            "critical in-diff line 1 + out-of-diff",
            ("critical", 1,  "a.py", "critical line 1"),
            [("low",    9, "other.py",      "out-of-diff low")],
        ),
        (
            "high in-diff line 2 + two out-of-diff",
            ("high",   2,    "a.py", "high line 2"),
            [
                ("medium", 5, "x.py",        "out x"),
                ("low",    3, "y.py",         "out y"),
            ],
        ),
        (
            "low in-diff file-level + three out-of-diff",
            ("low",    200,  "a.py", "low 200"),
            [
                ("high",   1, "f1.py",        "f1 high"),
                ("high",   2, "f2.py",        "f2 high"),
                ("low",    3, "f3.py",         "f3 low"),
            ],
        ),
        (
            "info in-diff line 3 + out-of-diff",
            ("info",   3,    "a.py", "info line 3"),
            [("critical", 5, "crit.py",     "crit out-of-diff")],
        ),
        (
            "medium in-diff file-level + empty and absent",
            ("medium", 77,   "a.py", "medium 77"),
            [
                ("low",  None, "",             "empty path"),
                ("high", 1,    "not_there.py", "absent path"),
            ],
        ),
    ]

    @pytest.mark.parametrize(
        "description,in_diff_spec,out_of_diff_specs", _PBT_D_CASES
    )
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
        solo_issues = [_FakeReviewIssue(in_sev, line=in_line, file_path=in_path, description=in_desc)]
        _r1, gh1, _j1 = self._post_review(review_app, solo_issues)

        all_comments_solo: list = []
        for rev in gh1.reviews:
            all_comments_solo.extend(rev.get("comments", []))

        # Find the in-diff finding's comment in the solo run.
        in_diff_solo = [
            c for c in all_comments_solo if c.get("path") == in_path
        ]
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

        # Find the in-diff finding's comment in the mixed run.
        in_diff_mixed = [
            c for c in all_comments_mixed if c.get("path") == in_path
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

    def test_returns_file_level_comment_dict(self) -> None:
        """Finding absent from diff → dict with correct path and subject_type."""
        from coding_team.github_source import anchor_to_first_file

        finding = _FakeReviewIssue("low", line=4, file_path="src/config.py", description="issue")
        valid_by_path = {"src/api.py": {1, 2, 3}, "src/utils.py": {5, 6}}
        result = anchor_to_first_file(finding, valid_by_path)
        assert result is not None
        assert result["path"] == "src/api.py"  # first key
        assert result["subject_type"] == "file"
        assert "body" in result

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
        assert result["path"] == "src/api.py"   # must be the FIRST key
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

        # The submitted review must carry a file-level inline comment.
        assert len(gh.reviews) >= 1
        review_comments = gh.reviews[-1].get("comments", [])
        file_level = [c for c in review_comments if c.get("subject_type") == "file"]
        assert len(file_level) >= 1, (
            f"Expected at least 1 file-level inline comment in the review, "
            f"got 0. review_comments = {review_comments}"
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
                _FakeReviewIssue("high",   line=2,   file_path="a.py",           description="on-diff finding"),
                _FakeReviewIssue("low",    line=999, file_path="a.py",           description="off-diff-line finding"),
                _FakeReviewIssue("medium", line=1,   file_path="not_in_diff.py", description="off-diff-file finding"),
            ]
        )
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200

        gh = review_app["github"]["client"]

        # Core assertion: no standalone comments for any finding.
        assert gh.comments == [], (
            f"Expected gh.comments == [] (no standalone comments), "
            f"but got {gh.comments}"
        )

        # All three findings must be present in the submitted review.
        assert len(gh.reviews) >= 1
        review_comments = gh.reviews[0].get("comments", [])

        line_comments = [c for c in review_comments if c.get("side") == "RIGHT"]
        file_comments = [c for c in review_comments if c.get("subject_type") == "file"]

        # on-diff finding → line-anchored
        assert len(line_comments) == 1, (
            f"Expected 1 line-anchored comment (on-diff), got {len(line_comments)}. "
            f"review_comments = {review_comments}"
        )
        assert line_comments[0]["line"] == 2

        # off-diff-line + off-diff-file → both file-level
        assert len(file_comments) == 2, (
            f"Expected 2 file-level comments (off-diff-line + off-diff-file), "
            f"got {len(file_comments)}. review_comments = {review_comments}"
        )

        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert job["review_summary"]["comment_findings"] == 0

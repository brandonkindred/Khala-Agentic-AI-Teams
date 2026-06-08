"""Tests for the PR-review flow.

Covers the new GitHub client methods (via httpx.MockTransport), and the
POST /review-pr endpoint + background hook in api/main.py (with a fake
GitHubClient and a stubbed CodeReviewAgent — no network, no LLM).
"""

from __future__ import annotations

import base64
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
                json=[{"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n+a", "additions": 1, "deletions": 0}],
                headers={"Link": '<https://api.github.com/repos/o/r/pulls/7/files?page=2>; rel="next"'},
            )

        client = _client_with(handler)
        files = client.get_pull_request_files("o", "r", 7)
        assert [f.filename for f in files] == ["a.py", "img.png", "new_name.py"]
        assert files[1].patch == ""  # binary file: no patch
        assert files[2].previous_filename == "old_name.py"


# ---------------------------------------------------------------------------
# Client: get_file_content
# ---------------------------------------------------------------------------


class TestGetFileContent:
    def test_base64_decode(self) -> None:
        encoded = base64.b64encode(b"hello\nworld").decode()
        client = _client_with(
            lambda _req: httpx.Response(200, json={"content": encoded, "encoding": "base64"})
        )
        assert client.get_file_content("o", "r", "a.py", "sha") == "hello\nworld"

    def test_non_base64_or_directory_returns_empty(self) -> None:
        client = _client_with(lambda _req: httpx.Response(200, json=[{"name": "a"}]))
        assert client.get_file_content("o", "r", "dir", "sha") == ""


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
    def __init__(self, severity: str, line: Optional[int], file_path: str = "a.py") -> None:
        self.severity = severity
        self.category = "logic"
        self.file_path = file_path
        self.line = line
        self.description = "desc"
        self.suggestion = "fix"


class _FakeReviewClient:
    """Fake GitHubClient surface the review endpoint + hook touch."""

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

    def get_file_content(self, _o: str, _r: str, _path: str, _ref: str) -> str:
        return "ctx\nadded\nmore\n"

    def get_authenticated_login(self) -> str:
        return self.login

    def add_issue_comment(self, _o: str, _r: str, n: int, body: str) -> None:
        self.comments.append((n, body))

    def create_pull_request_review(self, **kwargs: Any) -> dict[str, Any]:
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
        def run(self, _inp: Any) -> Any:
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
    body = {"owner": "o", "repo": "r", "repo_path": overrides.pop("repo_path", "/tmp/x"), "pr_number": 7}
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
        assert review["comments"] == [
            c for c in review["comments"] if c["line"] == 2
        ]
        assert len(review["comments"]) == 1
        # Job completed with the PR url + review summary.
        job = review_app["jobs"].get_job(data["job_id"])
        assert job["status"] == "completed"
        assert job["github_pr_url"] == "https://example/pull/7"
        assert job["review_summary"]["inline_comments"] == 1

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

    def test_no_changed_files_completes(self, review_app) -> None:
        review_app["github"]["client"].files = []
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert review_app["github"]["client"].reviews == []
        assert any("no changed files" in c[1].lower() for c in review_app["github"]["client"].comments)

    def test_review_422_retries_then_succeeds(self, review_app) -> None:
        gh = review_app["github"]["client"]
        gh.review_fail_times = 1  # first submit 422s, retry as COMMENT succeeds
        resp = review_app["client"].post("/review-pr", json=_review_body())
        assert resp.status_code == 200
        job = review_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "completed"
        assert len(gh.reviews) == 2
        assert gh.reviews[-1]["event"] == "COMMENT"

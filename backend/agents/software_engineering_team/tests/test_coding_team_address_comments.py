"""Tests for the "address & respond to unresolved PR comments" flow.

Covers:
  - The new GitHubClient methods (list_review_threads, reply_to_review_comment,
    resolve_review_thread) via httpx.MockTransport.
  - The address_comments orchestration (_unresolved_comments, _handle_comment,
    _run_address_comments) with a fake client and stubbed LLM/pipeline.
  - The POST /pulls/{pr_number}/address-comments route via TestClient.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import httpx
import pytest

from software_engineering_team.github_source import (
    GitHubAPIError,
    GitHubClient,
    PullRequestDetail,
    ReviewComment,
    ReviewThread,
    ReviewThreadsUnavailableError,
)

from .test_coding_team_github_source import _stub_heavy_modules

# ---------------------------------------------------------------------------
# Client helpers (mirrors test_coding_team_review_pr._client_with)
# ---------------------------------------------------------------------------


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubClient:
    transport = httpx.MockTransport(handler)
    client = GitHubClient(token="t", sleep=lambda _s: None)
    client._client.close()  # type: ignore[attr-defined]
    client._client = httpx.Client(transport=transport, timeout=client._timeout)  # type: ignore[attr-defined]
    return client


def _threads_response(
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


# ---------------------------------------------------------------------------
# Client: list_review_threads
# ---------------------------------------------------------------------------


class TestListReviewThreads:
    def test_parses_threads_with_ids_and_comment_ids(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_threads_response(
                    nodes=[
                        {
                            "id": "T_resolved",
                            "isResolved": True,
                            "comments": {"nodes": [{"databaseId": 1}]},
                        },
                        {
                            "id": "T_open",
                            "isResolved": False,
                            "comments": {"nodes": [{"databaseId": 2}, {"databaseId": 3}]},
                        },
                    ]
                ),
            )

        threads = _client_with(handler).list_review_threads("o", "r", 7)
        assert [t.id for t in threads] == ["T_resolved", "T_open"]
        assert threads[0].is_resolved is True
        assert threads[1].comment_ids == (2, 3)

    def test_paginates(self) -> None:
        calls: list[Optional[str]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            after = json.loads(req.content)["variables"]["after"]
            calls.append(after)
            if after is None:
                return httpx.Response(
                    200,
                    json=_threads_response(
                        has_next_page=True,
                        end_cursor="c1",
                        nodes=[{"id": "T1", "isResolved": False, "comments": {"nodes": []}}],
                    ),
                )
            return httpx.Response(
                200,
                json=_threads_response(
                    nodes=[{"id": "T2", "isResolved": True, "comments": {"nodes": []}}]
                ),
            )

        threads = _client_with(handler).list_review_threads("o", "r", 7)
        assert [t.id for t in threads] == ["T1", "T2"]
        assert calls == [None, "c1"]

    def test_fails_closed_on_graphql_error(self) -> None:
        # Unknown thread state must raise (fail closed), not degrade to empty — an
        # empty list would make the caller re-triage resolved discussions.
        client = _client_with(lambda _r: httpx.Response(200, json={"errors": [{"message": "no"}]}))
        with pytest.raises(ReviewThreadsUnavailableError):
            client.list_review_threads("o", "r", 7)

    def test_fails_closed_on_http_error(self) -> None:
        client = _client_with(lambda _r: httpx.Response(500, text="boom"))
        with pytest.raises(ReviewThreadsUnavailableError):
            client.list_review_threads("o", "r", 7)

    def test_fails_closed_on_incomplete_thread_comment_page(self) -> None:
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "T1",
                                    "isResolved": False,
                                    "comments": {
                                        "nodes": [{"databaseId": 1}],
                                        "pageInfo": {"hasNextPage": True},
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        client = _client_with(lambda _r: httpx.Response(200, json=payload))
        with pytest.raises(ReviewThreadsUnavailableError):
            client.list_review_threads("o", "r", 7)


# ---------------------------------------------------------------------------
# Client: reply_to_review_comment
# ---------------------------------------------------------------------------


class TestReplyToReviewComment:
    def test_posts_reply_to_replies_endpoint(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            captured["body"] = json.loads(req.content)
            return httpx.Response(201, json={"id": 9, "html_url": "https://example/c/9"})

        out = _client_with(handler).reply_to_review_comment(
            owner="o", repo="r", number=7, comment_id=42, body="done"
        )
        assert out["id"] == 9
        assert captured["url"].endswith("/pulls/7/comments/42/replies")
        assert captured["body"] == {"body": "done"}

    def test_empty_body_raises_value_error(self) -> None:
        client = _client_with(lambda _r: httpx.Response(201, json={}))
        with pytest.raises(ValueError):
            client.reply_to_review_comment(owner="o", repo="r", number=7, comment_id=1, body="")

    def test_non_2xx_raises(self) -> None:
        client = _client_with(lambda _r: httpx.Response(404, text="gone"))
        with pytest.raises(GitHubAPIError):
            client.reply_to_review_comment(owner="o", repo="r", number=7, comment_id=1, body="x")


# ---------------------------------------------------------------------------
# Client: resolve_review_thread
# ---------------------------------------------------------------------------


class TestResolveReviewThread:
    def test_returns_true_when_resolved(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content)
            return httpx.Response(
                200,
                json={
                    "data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}
                },
            )

        assert _client_with(handler).resolve_review_thread("T1") is True
        assert captured["body"]["variables"] == {"threadId": "T1"}

    def test_returns_false_on_graphql_error(self) -> None:
        client = _client_with(lambda _r: httpx.Response(200, json={"errors": [{"message": "no"}]}))
        assert client.resolve_review_thread("T1") is False

    def test_returns_false_on_http_error(self) -> None:
        client = _client_with(lambda _r: httpx.Response(500, text="boom"))
        assert client.resolve_review_thread("T1") is False


# ---------------------------------------------------------------------------
# Orchestration fake client + LLM/pipeline stubs
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal GitHubClient stand-in for the address-comments orchestration."""

    def __init__(self) -> None:
        self.review_comments: list[ReviewComment] = []
        self.threads: list[ReviewThread] = []
        self.file_contents = "def f():\n    return 1\n"
        self.replies: list[tuple[int, str]] = []
        self.resolved: list[str] = []
        self.resolve_result = True  # what resolve_review_thread returns
        self.labels_set: list[list[str]] = []
        self.pr = PullRequestDetail(
            number=7,
            html_url="https://example/pull/7",
            head="feature",
            base="main",
            head_sha="sha1",
            title="t",
            body="b",
            draft=False,
            author="alice",
            state="open",
            updated_at="",
            labels=("bug",),
        )

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def get_pull_request(self, _o: str, _r: str, _n: int) -> PullRequestDetail:
        return self.pr

    def list_review_comments(self, _o: str, _r: str, _n: int) -> list[ReviewComment]:
        return list(self.review_comments)

    def list_review_threads(self, _o: str, _r: str, _n: int) -> list[ReviewThread]:
        return list(self.threads)

    def get_file_contents(self, _o: str, _r: str, _p: str, _ref: str) -> Optional[str]:
        return self.file_contents

    def reply_to_review_comment(self, *, owner, repo, number, comment_id, body) -> dict[str, Any]:
        self.replies.append((comment_id, body))
        return {"id": comment_id, "html_url": "https://example/reply"}

    def resolve_review_thread(self, thread_id: str) -> bool:
        self.resolved.append(thread_id)
        return self.resolve_result

    def update_issue(self, _o: str, _r: str, _n: int, *, labels=None, body=None) -> Any:
        self.labels_set.append(list(labels or []))
        return None


def _comment(
    cid: int, body: str = "This has a bug", path: str = "a.py", line: int = 2
) -> ReviewComment:
    return ReviewComment(
        id=cid, path=path, line=line, body=body, html_url=f"https://example/c/{cid}"
    )


@pytest.fixture
def address_env(monkeypatch: pytest.MonkeyPatch):
    """Wire the address_comments module with a fake client and stubbed LLM/pipeline."""
    _stub_heavy_modules(monkeypatch)
    from software_engineering_team.api import address_comments as ac
    from software_engineering_team.api import coding_team_main as _main
    from software_engineering_team.api.coding_team_models import AddressCommentsRequest

    fake = _FakeClient()
    job_updates: list[dict[str, Any]] = []
    child_jobs: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []

    monkeypatch.setattr(_main, "GitHubClient", lambda **_kw: fake)
    monkeypatch.setattr(_main, "update_job", lambda job_id, **kw: job_updates.append(kw))
    monkeypatch.setattr(_main, "update_review", lambda job_id, **kw: None)
    monkeypatch.setattr(_main, "create_job", lambda **kw: child_jobs.append(kw))
    monkeypatch.setattr(_main, "encrypt_token", lambda token: "encrypted-token")

    def _execute(*args, **kwargs):
        executions.append({"args": args, "kwargs": kwargs})
        return {"status": "completed"}

    monkeypatch.setattr(_main, "execute_coding_team_workflow", _execute)

    return {
        "ac": ac,
        "main": _main,
        "fake": fake,
        "job_updates": job_updates,
        "child_jobs": child_jobs,
        "executions": executions,
        "request": AddressCommentsRequest(owner="o", repo="r", repo_path="/tmp/x", pr_number=7),
    }


def _stub_triage(monkeypatch, ac, *, raises_issue: bool, is_false_positive: bool) -> None:
    from software_engineering_team.api import coding_team_main as _main

    def _gen(prompt, *, schema, **kw):
        if schema is ac.CommentTriage:
            return ac.CommentTriage(
                raises_issue=raises_issue, is_false_positive=is_false_positive, issue_summary="s"
            )
        # IssueResolutionPlan
        return ac.IssueResolutionPlan(
            requirements=["r1", "r2"],
            candidate_solutions=[
                ac.SolutionCandidate(
                    summary="A",
                    requirement_fit=9,
                    computational_performance=8,
                    memory_usage=7,
                    code_complexity=6,
                ),
                ac.SolutionCandidate(
                    summary="B",
                    requirement_fit=5,
                    computational_performance=5,
                    memory_usage=5,
                    code_complexity=5,
                ),
            ],
            chosen_plan="do the thing",
        )

    monkeypatch.setattr(_main, "generate_structured", _gen)


# ---------------------------------------------------------------------------
# _unresolved_comments
# ---------------------------------------------------------------------------


class TestUnresolvedComments:
    def test_excludes_resolved_and_khala_comments(self, address_env) -> None:
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(1),
            _comment(2),
            _comment(3, body="ack <!-- khala-generated -->"),
        ]
        fake.threads = [
            ReviewThread(id="T1", is_resolved=True, comment_ids=(1,)),
            ReviewThread(id="T2", is_resolved=False, comment_ids=(2,)),
        ]
        # Exercise the PUBLIC entry point the route depends on.
        unresolved, by_comment = ac.unresolved_comments(fake, "o", "r", 7)
        assert [c.id for c in unresolved] == [2]
        assert by_comment[2].id == "T2"

    def test_fails_closed_when_thread_state_unavailable(self, address_env) -> None:
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [_comment(2)]

        def _boom(*_a, **_kw):
            raise ReviewThreadsUnavailableError("o", "r", 7, "graphql down")

        fake.list_review_threads = _boom  # type: ignore[assignment]
        with pytest.raises(ReviewThreadsUnavailableError):
            ac.unresolved_comments(fake, "o", "r", 7)

    def test_fails_closed_when_rest_comment_has_no_thread(self, address_env) -> None:
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [_comment(2)]
        fake.threads = []

        with pytest.raises(ReviewThreadsUnavailableError):
            ac.unresolved_comments(fake, "o", "r", 7)


# ---------------------------------------------------------------------------
# _handle_comment
# ---------------------------------------------------------------------------


class TestHandleComment:
    def test_real_issue_waits_for_publish_then_replies_and_resolves(
        self, address_env, monkeypatch
    ) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))

        outcome = ac._handle_comment(
            fake, "parent", req, _comment(2), thread, "feature", "main", fake.pr.html_url, "tok"
        )

        assert outcome.outcome == "resolved"
        assert address_env["child_jobs"][0]["job_id"] == "parent:comment:2"
        github = address_env["executions"][0]["kwargs"]["github"]
        assert github["publish_mode"] == "existing_pr"
        assert github["integration_branch"] == "feature"
        assert fake.replies and fake.replies[0][0] == 2
        assert fake.resolved == ["T2"]

    def test_false_positive_replies_and_resolves(self, address_env, monkeypatch) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=True)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))

        outcome = ac._handle_comment(
            fake, "parent", req, _comment(2), thread, "feature", "main", fake.pr.html_url, "tok"
        )

        assert outcome.outcome == "false_positive"
        # The false-positive path must actually reply (comment 11's concern).
        assert len(fake.replies) == 1
        assert fake.replies[0][0] == 2
        assert fake.resolved == ["T2"]

    def test_real_issue_does_not_reply_or_resolve_until_workflow_succeeds(
        self, address_env, monkeypatch
    ) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        monkeypatch.setattr(
            address_env["main"],
            "execute_coding_team_workflow",
            lambda *_a, **_kw: {"status": "failed"},
        )
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))

        outcome = ac._handle_comment(
            fake, "parent", req, _comment(2), thread, "feature", "main", fake.pr.html_url, "tok"
        )

        assert outcome.outcome == "failed"
        assert fake.replies == []
        assert fake.resolved == []

    def test_false_positive_reports_failed_when_resolve_fails(
        self, address_env, monkeypatch
    ) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=True)
        fake.resolve_result = False  # thread resolution fails
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))

        outcome = ac._handle_comment(
            fake, "parent", req, _comment(2), thread, "feature", "main", fake.pr.html_url, "tok"
        )

        # Must NOT falsely report a handled false positive when the thread stays open.
        assert outcome.outcome == "failed"

    def test_not_an_issue_is_skipped(self, address_env, monkeypatch) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)

        outcome = ac._handle_comment(
            fake, "parent", req, _comment(2), None, "feature", "main", fake.pr.html_url, "tok"
        )

        assert outcome.outcome == "not_an_issue"
        assert fake.replies == []
        assert fake.resolved == []

    def test_solution_candidates_ranked_best_first(self, address_env, monkeypatch) -> None:
        ac = address_env["ac"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        plan = ac._plan_resolution(_comment(2), "code")
        assert plan is not None
        # "A" (sum 30) must rank ahead of "B" (sum 20).
        assert plan.candidate_solutions[0].summary == "A"


# ---------------------------------------------------------------------------
# _run_address_comments (full background hook)
# ---------------------------------------------------------------------------


class TestRunAddressComments:
    def test_completes_and_marks_waiting_for_review(self, address_env, monkeypatch) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]

        ac._run_address_comments("job1", req, "tok")

        # The workflow completed, the thread resolved, and the PR is ready for review.
        assert fake.labels_set and ac.WAITING_FOR_REVIEW_LABEL in fake.labels_set[-1]
        assert "bug" in fake.labels_set[-1]
        final = [u for u in address_env["job_updates"] if u.get("status") == "completed"]
        assert final and final[-1]["review_summary"]["counts"]["resolved"] == 1

    def test_does_not_mark_waiting_for_review_when_a_comment_fails(
        self, address_env, monkeypatch
    ) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        # Real issue, but the plan reply fails → outcome "failed" → PR not labelled.
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)

        def _reply_boom(**_kw):
            raise GitHubAPIError(500, "reply down")

        fake.reply_to_review_comment = _reply_boom  # type: ignore[assignment]
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]

        ac._run_address_comments("job1", req, "tok")

        assert fake.labels_set == []  # not moved to waiting-for-review
        final = [u for u in address_env["job_updates"] if u.get("status") == "completed"]
        assert final and final[-1]["review_summary"]["counts"]["failed"] == 1

    def test_fails_closed_when_thread_state_unavailable(self, address_env, monkeypatch) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        fake.review_comments = [_comment(2)]

        def _boom(*_a, **_kw):
            raise ReviewThreadsUnavailableError("o", "r", 7, "graphql down")

        fake.list_review_threads = _boom  # type: ignore[assignment]
        ac._run_address_comments("job1", req, "tok")
        # The job fails (does not silently proceed on unknown thread state).
        assert [u for u in address_env["job_updates"] if u.get("status") == "failed"]
        assert fake.labels_set == []

    def test_no_unresolved_comments_still_completes(self, address_env, monkeypatch) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        fake.review_comments = []
        fake.threads = []

        ac._run_address_comments("job1", req, "tok")

        final = [u for u in address_env["job_updates"] if u.get("status") == "completed"]
        assert final and final[-1]["review_summary"]["total_comments"] == 0

    def test_github_failure_marks_job_failed(self, address_env, monkeypatch) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]

        def _boom(*_a, **_kw):
            raise GitHubAPIError(502, "down")

        monkeypatch.setattr(fake, "get_pull_request", _boom)
        ac._run_address_comments("job1", req, "tok")
        failed = [u for u in address_env["job_updates"] if u.get("status") == "failed"]
        assert failed


# ---------------------------------------------------------------------------
# Route: POST /pulls/{pr_number}/address-comments
# ---------------------------------------------------------------------------


class TestAddressCommentsRoute:
    @pytest.fixture
    def route_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        _stub_heavy_modules(monkeypatch)
        from job_service_client_fake import FakeJobServiceClient
        from software_engineering_team import job_store as job_store_mod

        fake_jobs = FakeJobServiceClient(team="coding_team")
        monkeypatch.setattr(job_store_mod, "_client", lambda *a, **kw: fake_jobs)
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

        from software_engineering_team.api import address_comments as ac
        from software_engineering_team.api import coding_team_main as _main

        fake = _FakeClient()
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]
        monkeypatch.setattr(_main, "GitHubClient", lambda **_kw: fake)
        monkeypatch.setattr(_main, "encrypt_token", lambda token: "ciphertext")
        started: list[tuple] = []
        # The route calls the PUBLIC entry point; patch that.
        monkeypatch.setattr(
            ac, "start_address_comments_thread", lambda *a, **kw: started.append(a)
        )

        from fastapi.testclient import TestClient

        return {
            "client": TestClient(_main.app),
            "fake": fake,
            "started": started,
            "repo_path": str(tmp_path),
            "jobs": fake_jobs,
        }

    def test_starts_job_and_reports_unresolved_count(self, route_env) -> None:
        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": route_env["repo_path"], "pr_number": 7},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pr_number"] == 7
        assert body["pr_url"] == "https://example/pull/7"
        assert body["unresolved_comment_count"] == 1
        assert route_env["started"]  # background hook launched
        assert route_env["jobs"].get_job(body["job_id"])["github_token_encrypted"] == "ciphertext"

    def test_path_pr_number_wins_over_body(self, route_env) -> None:
        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": route_env["repo_path"], "pr_number": 999},
        )
        assert resp.status_code == 200
        # The launched request carries the path's pr_number, not the body's.
        launched_request = route_env["started"][0][1]
        assert launched_request.pr_number == 7

    def test_github_error_returns_502(self, route_env, monkeypatch) -> None:
        def _boom(*_a, **_kw):
            raise GitHubAPIError(404, "missing")

        monkeypatch.setattr(route_env["fake"], "get_pull_request", _boom)
        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": route_env["repo_path"], "pr_number": 7},
        )
        assert resp.status_code == 502

    def test_rejects_closed_pr_with_400(self, route_env) -> None:
        fake = route_env["fake"]
        fake.pr = PullRequestDetail(
            number=7, html_url="https://example/pull/7", head="feature", base="main",
            head_sha="sha1", title="t", body="b", draft=False, author="alice",
            state="closed", updated_at="", labels=(),
        )
        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": route_env["repo_path"], "pr_number": 7},
        )
        assert resp.status_code == 400
        assert route_env["started"] == []  # no job launched for a closed PR

    def test_thread_state_unavailable_returns_502(self, route_env, monkeypatch) -> None:
        def _boom(*_a, **_kw):
            raise ReviewThreadsUnavailableError("o", "r", 7, "graphql down")

        monkeypatch.setattr(route_env["fake"], "list_review_threads", _boom)
        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": route_env["repo_path"], "pr_number": 7},
        )
        assert resp.status_code == 502
        assert route_env["started"] == []

    def test_thread_launch_failure_terminalizes_created_job(self, route_env, monkeypatch) -> None:
        from software_engineering_team.api import address_comments as ac

        monkeypatch.setattr(
            ac,
            "start_address_comments_thread",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
        )
        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={
                "owner": "o",
                "repo": "r",
                "repo_path": route_env["repo_path"],
                "pr_number": 7,
            },
        )

        assert resp.status_code == 500
        jobs = route_env["jobs"].list_jobs()
        assert jobs[-1]["status"] == "failed"

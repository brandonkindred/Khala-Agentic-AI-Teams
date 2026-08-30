"""Tests for the "address & respond to unresolved PR comments" flow.

Covers:
  - The new GitHubClient methods (list_review_threads, reply_to_review_comment,
    resolve_review_thread) via httpx.MockTransport.
  - The address_comments orchestration (_unresolved_comments, _handle_comment,
    _run_address_comments) with a fake client and stubbed LLM/pipeline.
  - The POST /pulls/{pr_number}/address-comments route via TestClient.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
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
from software_engineering_team.github_source.client import KHALA_COMMENT_MARKER

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

    def test_query_requests_comments_page_info(self) -> None:
        """A thread with >100 comments must be detectable as incomplete: the
        query itself has to request `comments`' `pageInfo { hasNextPage }`, or
        GitHub never returns it and the parser's fail-closed check (which reads
        exactly that field) can never fire — silently truncating the thread."""
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["query"] = json.loads(req.content)["query"]
            return httpx.Response(200, json=_threads_response(nodes=[]))

        _client_with(handler).list_review_threads("o", "r", 7)
        comments_block = captured["query"].split("comments(first: 100)", 1)[1]
        assert "pageInfo" in comments_block.split("nodes", 1)[0]
        assert "hasNextPage" in comments_block.split("nodes", 1)[0]

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
        # KHALA_COMMENT_MARKER is appended (matching add_issue_comment /
        # create_issue's provenance convention) so a later triage pass can
        # recognize and skip Khala's own reply, e.g. after a resolve failure.
        assert captured["body"] == {"body": f"done\n\n{KHALA_COMMENT_MARKER}"}

    def test_marker_not_duplicated_when_already_present(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content)
            return httpx.Response(201, json={"id": 9, "html_url": "https://example/c/9"})

        _client_with(handler).reply_to_review_comment(
            owner="o",
            repo="r",
            number=7,
            comment_id=42,
            body=f"done\n\n{KHALA_COMMENT_MARKER}",
        )
        assert captured["body"]["body"].count(KHALA_COMMENT_MARKER) == 1

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
        self.authenticated_login = "khala-bot"
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
            head_repo_full_name="o/r",
        )

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def get_pull_request(self, _o: str, _r: str, _n: int) -> PullRequestDetail:
        return self.pr

    def get_authenticated_login(self) -> str:
        return self.authenticated_login

    def list_review_comments(self, _o: str, _r: str, _n: int) -> list[ReviewComment]:
        return list(self.review_comments)

    def list_review_threads(self, _o: str, _r: str, _n: int) -> list[ReviewThread]:
        return list(self.threads)

    def get_file_contents(self, _o: str, _r: str, _p: str, _ref: str) -> Optional[str]:
        return self.file_contents

    def reply_to_review_comment(
        self, *, owner: str, repo: str, number: int, comment_id: int, body: str
    ) -> dict[str, Any]:
        self.replies.append((comment_id, body))
        # Mirror real GitHub state: a posted reply becomes a new comment on the
        # SAME thread, so a later live re-check (list_review_comments/
        # list_review_threads) sees it — matching how the real API behaves and
        # how the production freshness/re-list checks assume state evolves.
        new_id = max([c.id for c in self.review_comments] + [comment_id], default=0) + 1
        marked_body = body if KHALA_COMMENT_MARKER in body else f"{body}\n\n{KHALA_COMMENT_MARKER}"
        reply = ReviewComment(
            id=new_id,
            path="",
            line=None,
            body=marked_body,
            html_url="https://example/reply",
            author=self.authenticated_login,
        )
        self.review_comments = [*self.review_comments, reply]
        self.threads = [
            replace(t, comment_ids=(*t.comment_ids, new_id))
            if comment_id in t.comment_ids
            else t
            for t in self.threads
        ]
        return {"id": new_id, "html_url": "https://example/reply"}

    def resolve_review_thread(self, thread_id: str) -> bool:
        self.resolved.append(thread_id)
        if self.resolve_result:
            # Mirror real GitHub state: a successful resolve flips the live
            # thread's is_resolved flag, so a later live re-check sees it.
            self.threads = [
                replace(t, is_resolved=True) if t.id == thread_id else t for t in self.threads
            ]
        return self.resolve_result

    def update_issue(
        self,
        _o: str,
        _r: str,
        _n: int,
        *,
        labels: Optional[list[str]] = None,
        body: Optional[str] = None,
    ) -> None:
        self.labels_set.append(list(labels or []))
        return None


def _comment(
    cid: int, body: str = "This has a bug", path: str = "a.py", line: int = 2, author: str = ""
) -> ReviewComment:
    return ReviewComment(
        id=cid, path=path, line=line, body=body, html_url=f"https://example/c/{cid}", author=author
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
    # The real heartbeat_job hits the job service; _run_address_comments now
    # heartbeats continuously (see the P1 fix for the admission-guard race), so
    # every test that reaches it would otherwise pay a real network call.
    monkeypatch.setattr(_main, "heartbeat_job", lambda job_id: None)

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
            _comment(3, body="ack <!-- khala-generated -->", author="khala-bot"),
        ]
        fake.threads = [
            ReviewThread(id="T1", is_resolved=True, comment_ids=(1,)),
            ReviewThread(id="T2", is_resolved=False, comment_ids=(2,)),
        ]
        # Exercise the PUBLIC entry point the route depends on.
        unresolved, by_comment, retry_resolve, _history = ac.unresolved_comments(fake, "o", "r", 7)
        assert [c.id for c in unresolved] == [2]
        assert by_comment[2].id == "T2"
        assert retry_resolve == []

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

    def test_thread_with_root_and_replies_yields_one_comment(self, address_env) -> None:
        """A thread's root comment plus its replies all map to the same thread
        (GitHub's REST listing returns every message); only the LATEST message
        is returned so the conversation is triaged/handled once (using its
        most current concern), not once per message."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [_comment(2), _comment(3), _comment(4)]
        fake.threads = [
            ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3, 4)),
        ]
        unresolved, by_comment, retry_resolve, _history = ac.unresolved_comments(fake, "o", "r", 7)
        assert [c.id for c in unresolved] == [4]
        # The thread map still covers every message's id (callers only look up
        # ids drawn from `unresolved`, so the extra entries are harmless).
        assert {by_comment[2].id, by_comment[3].id, by_comment[4].id} == {"T2"}
        assert retry_resolve == []

    def test_multiple_distinct_threads_each_keep_their_latest(self, address_env) -> None:
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [_comment(2), _comment(3), _comment(5)]
        fake.threads = [
            ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3)),
            ReviewThread(id="T5", is_resolved=False, comment_ids=(5,)),
        ]
        unresolved, _by_comment, _retry_resolve, _history = ac.unresolved_comments(fake, "o", "r", 7)
        assert [c.id for c in unresolved] == [3, 5]

    def test_thread_with_marked_reply_excluded_even_via_unmarked_root(self, address_env) -> None:
        """A thread whose root comment carries no marker but whose LATER reply
        does (Khala already replied) must never surface via its unmarked root —
        the whole thread is excluded from `unresolved`, and since it is still
        unresolved (the resolve mutation failed previously), its id is reported
        for a resolve-only retry."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _comment(
                3, body="Addressed by the SE team. <!-- khala-generated -->", author="khala-bot"
            ),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]

        unresolved, _by_comment, retry_resolve, _history = ac.unresolved_comments(fake, "o", "r", 7)

        assert unresolved == []
        assert retry_resolve == [("T2", 3)]

    def test_reviewer_feedback_after_khala_reply_is_re_triaged_not_discarded(
        self, address_env
    ) -> None:
        """A reviewer who posts NEW feedback after Khala's generated reply
        (e.g. "this fix is incomplete") must have that feedback re-triaged,
        never silently discarded in favor of auto-resolving the thread just
        because it once carried a Khala reply."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _comment(
                3, body="Addressed by the SE team. <!-- khala-generated -->", author="khala-bot"
            ),
            _comment(4, body="this fix is incomplete, the null case is still broken"),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3, 4))]

        unresolved, _by_comment, retry_resolve, _history = ac.unresolved_comments(fake, "o", "r", 7)

        assert [c.id for c in unresolved] == [4]
        assert retry_resolve == []

    def test_thread_with_marked_reply_that_is_already_resolved_is_not_retried(
        self, address_env
    ) -> None:
        """An already-resolved thread with a Khala reply needs no retry — it's
        just an ordinary resolved thread, not a pending resolve-retry."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _comment(
                3, body="Addressed by the SE team. <!-- khala-generated -->", author="khala-bot"
            ),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=True, comment_ids=(2, 3))]

        unresolved, _by_comment, retry_resolve, _history = ac.unresolved_comments(fake, "o", "r", 7)

        assert unresolved == []
        assert retry_resolve == []


# ---------------------------------------------------------------------------
# _is_khala_authored
# ---------------------------------------------------------------------------


class TestIsKhalaAuthored:
    def test_marker_and_matching_author_is_trusted(self, address_env) -> None:
        ac = address_env["ac"]
        comment = _comment(1, body="fixed. <!-- khala-generated -->", author="khala-bot")
        assert ac._is_khala_authored(comment, "khala-bot") is True

    def test_author_comparison_is_case_insensitive(self, address_env) -> None:
        ac = address_env["ac"]
        comment = _comment(1, body="fixed. <!-- khala-generated -->", author="Khala-Bot")
        assert ac._is_khala_authored(comment, "khala-bot") is True

    def test_marker_without_matching_author_is_not_trusted(self, address_env) -> None:
        """The core exploit this guards against: any commenter can include the
        public marker string in their own comment body."""
        ac = address_env["ac"]
        comment = _comment(1, body="fixed. <!-- khala-generated -->", author="some-rando")
        assert ac._is_khala_authored(comment, "khala-bot") is False

    def test_matching_author_without_marker_is_not_trusted(self, address_env) -> None:
        ac = address_env["ac"]
        comment = _comment(1, body="just a normal reply", author="khala-bot")
        assert ac._is_khala_authored(comment, "khala-bot") is False

    def test_unresolved_authenticated_login_fails_closed(self, address_env) -> None:
        """When the authenticated login could not be resolved (""), nothing is
        ever trusted as Khala's own — the safe failure mode is a redundant
        re-triage, never trusting an unauthenticated marker."""
        ac = address_env["ac"]
        comment = _comment(1, body="fixed. <!-- khala-generated -->", author="khala-bot")
        assert ac._is_khala_authored(comment, "") is False


class TestUnresolvedCommentsMarkerAuthentication:
    def test_marker_from_a_different_author_is_not_treated_as_khalas(self, address_env) -> None:
        """Fresh evidence: a non-Khala comment containing the literal marker
        string must not suppress triage/implementation of its thread."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(2, body="please fix this. <!-- khala-generated -->", author="some-rando"),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]

        unresolved, _by_comment, retry_resolve, _history = ac.unresolved_comments(fake, "o", "r", 7)

        assert [c.id for c in unresolved] == [2]
        assert retry_resolve == []

    def test_login_resolution_failure_degrades_to_untrusted_not_re_triage_skip(
        self, address_env
    ) -> None:
        """A best-effort failure to resolve the authenticated login must not
        crash the run; it just means no marker is trusted this run."""
        ac, fake = address_env["ac"], address_env["fake"]

        def _boom() -> str:
            raise RuntimeError("network blip")

        fake.get_authenticated_login = _boom  # type: ignore[assignment]
        fake.review_comments = [
            _comment(2, body="Addressed. <!-- khala-generated -->", author="khala-bot"),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]

        unresolved, _by_comment, retry_resolve, _history = ac.unresolved_comments(fake, "o", "r", 7)

        assert [c.id for c in unresolved] == [2]
        assert retry_resolve == []


# ---------------------------------------------------------------------------
# _pr_head_remote
# ---------------------------------------------------------------------------


class TestPrHeadRemote:
    def test_same_repo_pr_resolves_to_origin(self, address_env) -> None:
        ac, fake = address_env["ac"], address_env["fake"]
        assert ac._pr_head_remote("o", "r", fake.pr) == "origin"

    def test_same_repo_pr_is_case_insensitive(self, address_env) -> None:
        ac, fake = address_env["ac"], address_env["fake"]
        fake.pr = replace(fake.pr, head_repo_full_name="O/R")
        assert ac._pr_head_remote("o", "r", fake.pr) == "origin"

    def test_fork_pr_resolves_to_fork_clone_url(self, address_env) -> None:
        ac, fake = address_env["ac"], address_env["fake"]
        fake.pr = replace(fake.pr, head_repo_full_name="contributor/r")
        assert ac._pr_head_remote("o", "r", fake.pr) == "https://github.com/contributor/r.git"

    def test_deleted_fork_resolves_to_none(self, address_env) -> None:
        ac, fake = address_env["ac"], address_env["fake"]
        fake.pr = replace(fake.pr, head_repo_full_name="")
        assert ac._pr_head_remote("o", "r", fake.pr) is None


# ---------------------------------------------------------------------------
# _handle_comment
# ---------------------------------------------------------------------------


class TestTriageComment:
    def test_llm_failure_raises_triage_unavailable_not_a_fabricated_verdict(
        self, address_env, monkeypatch
    ) -> None:
        """An LLM outage must surface as TriageUnavailableError, never a
        fabricated raises_issue=False verdict indistinguishable from a
        genuine "not an issue" analysis."""
        ac = address_env["ac"]

        def _boom(*_a, **_kw):
            raise RuntimeError("LLM outage")

        monkeypatch.setattr(ac._main, "generate_structured", _boom)

        with pytest.raises(ac.TriageUnavailableError):
            ac._triage_comment(_comment(2), "code", [_comment(2)])


class TestHandleComment:
    def test_triage_outage_records_failed_not_not_an_issue(self, address_env, monkeypatch) -> None:
        """A triage-LLM outage must be recorded as a FAILED comment outcome
        (work still owed, thread stays open), never as the same "not_an_issue"
        success a genuine non-issue verdict produces — a false success there
        could leave the underlying problem unaddressed while the PR is
        reported ready for review and its checkout reclaimed."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]

        def _boom(*_a, **_kw):
            raise RuntimeError("LLM outage")

        monkeypatch.setattr(ac._main, "generate_structured", _boom)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "failed"
        assert fake.replies == []
        assert fake.resolved == []

    def test_real_issue_waits_for_publish_then_replies_and_resolves(
        self, address_env, monkeypatch
    ) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "resolved"
        assert address_env["child_jobs"][0]["job_id"] == "parent:comment:2"
        github = address_env["executions"][0]["kwargs"]["github"]
        assert github["publish_mode"] == "existing_pr"
        assert github["integration_branch"] == "feature"
        assert fake.replies and fake.replies[0][0] == 2
        assert fake.resolved == ["T2"]

    def test_new_reviewer_feedback_during_workflow_prevents_resolution(
        self, address_env, monkeypatch
    ) -> None:
        """A reviewer's follow-up feedback posted while the implementation
        workflow was running — after `comment` was snapshotted as this run's
        representative comment, before this call would reply/resolve — must
        skip BOTH the reply and the resolve, leaving the thread exactly as
        found so the next run's latest-message check correctly sees the human
        feedback (not a just-posted Khala reply) as the thread's live latest
        message and re-triages it, instead of the reply itself masking that
        feedback behind the resolve-only retry path."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 6))
        # comment 6 simulates feedback that landed after comment 2 was
        # snapshotted — reflected here as the "live" state _reply_and_resolve
        # re-fetches before doing anything.
        fake.review_comments = [_comment(2), _comment(6, body="wait, this is still broken")]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "failed"
        assert fake.replies == []  # never replied — a reply would mask the feedback
        assert fake.resolved == []  # never resolved

    def test_in_place_edit_of_triaged_comment_prevents_resolution(
        self, address_env, monkeypatch
    ) -> None:
        """GitHub retains a comment's id across an edit, so a reviewer who edits
        the ALREADY-triaged comment in place (rather than posting a reply) must
        still be caught: an id-only freshness check would see no id greater
        than the snapshot and silently resolve over the edited feedback."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        # The live comment 2 now has a different body than what was triaged —
        # simulating an in-place edit while the implementation workflow ran.
        fake.review_comments = [_comment(2, body="actually this is a bigger problem than I thought")]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2, body="This has a bug"),  # the snapshot triage saw
            thread,
            [_comment(2, body="This has a bug")],  # the snapshot triage saw
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "failed"
        assert fake.replies == []
        assert fake.resolved == []  # never resolved

    def test_real_issue_on_fork_pr_pushes_to_fork_remote(self, address_env, monkeypatch) -> None:
        """A fork-opened PR's implementation workflow is dispatched with the fork's
        clone URL as the remote, not "origin" (the base repo)."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "https://github.com/contributor/r.git",
            "tok",
        )

        assert outcome.outcome == "resolved"
        github = address_env["executions"][0]["kwargs"]["github"]
        assert github["remote"] == "https://github.com/contributor/r.git"

    def test_real_issue_with_unresolvable_remote_fails_without_dispatch(
        self, address_env, monkeypatch
    ) -> None:
        """A deleted fork (no resolvable remote) fails the comment before dispatching
        any implementation workflow — never guesses "origin" for a fork PR."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            None,
            "tok",
        )

        assert outcome.outcome == "failed"
        assert "fork appears to have been deleted" in outcome.detail
        assert address_env["executions"] == []
        assert fake.replies == []
        assert fake.resolved == []

    def test_false_positive_replies_and_resolves(self, address_env, monkeypatch) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=True)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "false_positive"
        # The false-positive path must actually reply (comment 11's concern).
        assert len(fake.replies) == 1
        assert fake.replies[0][0] == 2
        assert fake.resolved == ["T2"]

    def test_reply_targets_thread_root_not_a_later_representative_comment(
        self, address_env, monkeypatch
    ) -> None:
        """When the representative comment is a reviewer follow-up (a reply, not
        the thread's root — as `_unresolved_comments` now surfaces for a thread
        with newer feedback), the reply must still target the thread's ROOT
        comment id: GitHub's create-reply endpoint requires the top-level
        comment id, so replying against a non-root id would be rejected or land
        outside the thread."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=True)
        # Root is comment 2; comment 4 (a later reply in the same thread) is the
        # representative comment being handled.
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3, 4))
        fake.review_comments = [_comment(2), _comment(3), _comment(4, body="this fix is incomplete")]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(4, body="this fix is incomplete"),
            thread,
            [_comment(4, body="this fix is incomplete")],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "false_positive"
        assert len(fake.replies) == 1
        assert fake.replies[0][0] == 2  # thread.comment_ids[0], not comment.id (4)
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
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
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
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        # Must NOT falsely report a handled false positive when the thread stays open.
        assert outcome.outcome == "failed"

    def test_not_an_issue_is_skipped(self, address_env, monkeypatch) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            None,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "not_an_issue"
        assert fake.replies == []
        assert fake.resolved == []

    def test_solution_candidates_ranked_best_first(self, address_env, monkeypatch) -> None:
        ac = address_env["ac"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        plan = ac._plan_resolution(_comment(2), "code", [_comment(2)])
        assert plan is not None
        # "A" (sum 30) must rank ahead of "B" (sum 20).
        assert plan.candidate_solutions[0].summary == "A"


# ---------------------------------------------------------------------------
# _run_address_comments (full background hook)
# ---------------------------------------------------------------------------


class TestRunAddressComments:
    def test_refreshes_pr_head_sha_before_each_comment(self, address_env, monkeypatch) -> None:
        """An earlier comment's real-issue workflow can push a new head commit;
        every subsequent comment's cited-code grounding must use the FRESH head
        SHA, not the single `pr.head_sha` captured before the loop, or a later
        verdict/plan would be grounded against stale code."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)
        fake.review_comments = [_comment(2), _comment(5)]
        fake.threads = [
            ReviewThread(id="T2", is_resolved=False, comment_ids=(2,)),
            ReviewThread(id="T5", is_resolved=False, comment_ids=(5,)),
        ]

        pr_shas = ["sha1", "sha2", "sha3"]
        call_count = {"n": 0}

        def _get_pr(_o: str, _r: str, _n: int) -> PullRequestDetail:
            idx = min(call_count["n"], len(pr_shas) - 1)
            call_count["n"] += 1
            return replace(fake.pr, head_sha=pr_shas[idx])

        monkeypatch.setattr(fake, "get_pull_request", _get_pr)

        refs_used: list[str] = []

        def _get_file_contents(_o: str, _r: str, _p: str, ref: str) -> str:
            refs_used.append(ref)
            return fake.file_contents

        monkeypatch.setattr(fake, "get_file_contents", _get_file_contents)

        ac._run_address_comments("job1", req, "tok")

        # First get_pull_request call (index 0 -> sha1) is the pre-loop fetch;
        # each comment then gets its own refresh (sha2, sha3) before triage.
        assert refs_used == ["sha2", "sha3"]

    def test_cleans_up_checkout_on_full_success_when_flagged(
        self, address_env, monkeypatch
    ) -> None:
        """cleanup_checkout_on_success=True reclaims the per-PR checkout once
        every comment is handled without failure — mirrors the issue-driven
        flow's cleanup, since the address-comments checkout otherwise leaks
        indefinitely on every successfully addressed PR."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        req = req.model_copy(update={"cleanup_checkout_on_success": True})
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]
        cleaned: list[str] = []
        monkeypatch.setattr(ac._main, "_cleanup_issue_checkout", cleaned.append)

        ac._run_address_comments("job1", req, "tok")

        assert cleaned == [req.repo_path]

    def test_does_not_clean_up_when_a_comment_fails(self, address_env, monkeypatch) -> None:
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        req = req.model_copy(update={"cleanup_checkout_on_success": True})
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        monkeypatch.setattr(
            address_env["main"],
            "execute_coding_team_workflow",
            lambda *_a, **_kw: {"status": "failed"},
        )
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]
        cleaned: list[str] = []
        monkeypatch.setattr(ac._main, "_cleanup_issue_checkout", cleaned.append)

        ac._run_address_comments("job1", req, "tok")

        assert cleaned == []

    def test_does_not_clean_up_when_flag_is_unset(self, address_env, monkeypatch) -> None:
        """Default (unset) cleanup_checkout_on_success never removes the checkout
        — matches an operator-managed repo_path override."""
        ac, req = address_env["ac"], address_env["request"]
        assert req.cleanup_checkout_on_success is False
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)
        cleaned: list[str] = []
        monkeypatch.setattr(ac._main, "_cleanup_issue_checkout", cleaned.append)

        ac._run_address_comments("job1", req, "tok")

        assert cleaned == []

    def test_retries_resolve_for_thread_with_existing_khala_reply(
        self, address_env, monkeypatch
    ) -> None:
        """A thread that already carries a Khala reply (but GitHub still
        reports it unresolved) gets ONLY a resolve retry — no triage, no
        implementation dispatch, no second reply."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _comment(
                3, body="Addressed by the SE team. <!-- khala-generated -->", author="khala-bot"
            ),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]

        ac._run_address_comments("job1", req, "tok")

        assert fake.resolved == ["T2"]
        assert fake.replies == []
        assert address_env["executions"] == []
        assert address_env["child_jobs"] == []
        final = [u for u in address_env["job_updates"] if u.get("status") == "completed"]
        # A resolve-only retry never produces a CommentOutcome (it has only a
        # thread_id, not the comment metadata CommentOutcome requires), so it's
        # intentionally invisible to counts/total_comments — see _build_summary's
        # docstring. The real work is still surfaced via the waiting-for-review
        # label instead (see test_marks_waiting_for_review_on_retry_only_success).
        assert final and final[-1]["review_summary"]["counts"] == {}

    def test_run_wraps_body_in_liveness_heartbeat(self, address_env, monkeypatch) -> None:
        """_run_address_comments must hold a continuous heartbeat for the job while
        it runs — a single comment's implementation can now block for hours (see
        execute_coding_team_workflow's reattach_on_timeout) — mirroring
        _run_pr_review's review_hb, asserted via a recording stand-in."""
        import shared.concurrency

        ac, req = address_env["ac"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)
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

        ac._run_address_comments("job1", req, "tok")

        assert seen["entered"] and seen["exited"]
        assert seen["interval"] == ac._main._REVIEW_HEARTBEAT_INTERVAL_S
        seen["beat"]()  # must not raise; touches the job's liveness stamp

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

    def test_marks_waiting_for_review_on_retry_only_success(self, address_env) -> None:
        """A run consisting SOLELY of successful resolve-only retries (no fresh
        unresolved comments to triage) still did real work — the resolve
        mutation that a previous run's reply left pending — and must be
        labelled waiting-for-review too, not just a run with fresh `outcomes`."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        # Latest message in the thread is already Khala's own reply, so this
        # routes entirely through retry_resolve_thread_ids; `unresolved`/
        # `outcomes` stay empty.
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _comment(
                3, body="Addressed by the SE team. <!-- khala-generated -->", author="khala-bot"
            ),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]

        ac._run_address_comments("job1", req, "tok")

        assert fake.resolved == ["T2"]
        assert fake.labels_set and ac.WAITING_FOR_REVIEW_LABEL in fake.labels_set[-1]

    def test_retry_resolve_revalidates_freshness_before_resolving(self, address_env) -> None:
        """A reviewer's follow-up posted in the window between the
        `_unresolved_comments` snapshot (which routed this thread to the
        resolve-only retry path because its LATEST message was Khala's own
        reply at snapshot time) and the retry loop actually resolving it must
        NOT be silently resolved over — the retry loop must re-check live
        state immediately before each resolve, the same way a fresh
        reply/resolve already does."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _comment(
                3, body="Addressed by the SE team. <!-- khala-generated -->", author="khala-bot"
            ),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3, 4))]

        # The snapshot (_unresolved_comments' own list_review_comments call) sees
        # only [2, 3] — comment 4 lands only on SUBSEQUENT calls, simulating a
        # reviewer posting a follow-up after the run's snapshot was taken but
        # before the retry-resolve loop re-checks the thread.
        calls = {"n": 0}
        snapshot_comments = list(fake.review_comments)
        live_comments = [*snapshot_comments, _comment(4, body="still broken")]

        def _list_comments(_o: str, _r: str, _n: int):
            calls["n"] += 1
            return list(snapshot_comments if calls["n"] == 1 else live_comments)

        fake.list_review_comments = _list_comments  # type: ignore[assignment]

        ac._run_address_comments("job1", req, "tok")

        assert fake.resolved == []  # never resolved over the newer feedback
        # A run that skipped a stale retry did NOT fully succeed this round —
        # never labelled ready while a reviewer's follow-up is still unaddressed.
        assert fake.labels_set == []

    def test_relists_unresolved_threads_before_declaring_success(
        self, address_env, monkeypatch
    ) -> None:
        """A reviewer can open a BRAND-NEW thread while an earlier comment's
        implementation workflow is still running — invisible to both
        `outcomes` and `retry_resolve_threads`, which only cover what THIS
        run's initial snapshot saw. The run must re-list live unresolved
        state before declaring itself fully successful, or it would label
        the PR ready (and could reclaim its checkout) over a thread that was
        never triaged."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]

        real_unresolved_comments = ac._unresolved_comments
        calls = {"n": 0}
        new_thread_comment = _comment(9, body="separate new concern")

        def _stub(client, owner, repo, pr_number):
            calls["n"] += 1
            result = real_unresolved_comments(client, owner, repo, pr_number)
            if calls["n"] == 1:
                return result
            # The final re-list (this run's second call) sees a brand-new
            # thread a reviewer opened while comment 2's workflow was running.
            unresolved, by_comment, retry, history = result
            by_comment = {**by_comment, 9: ReviewThread(id="T9", is_resolved=False, comment_ids=(9,))}
            history = {**history, 9: [new_thread_comment]}
            return [*unresolved, new_thread_comment], by_comment, retry, history

        monkeypatch.setattr(ac, "_unresolved_comments", _stub)

        ac._run_address_comments("job1", req, "tok")

        assert calls["n"] == 2  # the initial snapshot, then the final re-list
        assert fake.resolved == ["T2"]  # comment 2's own thread still resolved
        # But the run as a whole did NOT declare success over the new thread.
        assert fake.labels_set == []

    def test_relist_ignores_a_known_unchanged_not_an_issue_comment(
        self, address_env, monkeypatch
    ) -> None:
        """A comment triaged as `not_an_issue` is NEVER resolved (there's
        nothing to fix or reply to), so it legitimately reappears in the
        final re-list every time. That is expected, not "still owed" — it
        must not block the run from succeeding, or a PR containing any
        question/acknowledgement could never reach waiting-for-review and
        every future run would re-triage the same non-issue forever."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)
        fake.review_comments = [_comment(2, body="just a question")]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]

        ac._run_address_comments("job1", req, "tok")

        assert fake.resolved == []  # nothing to resolve for a non-issue
        assert fake.replies == []
        final = [u for u in address_env["job_updates"] if u.get("status") == "completed"]
        assert final and final[-1]["review_summary"]["counts"]["not_an_issue"] == 1
        # The run still succeeded: the unresolved not_an_issue comment is a
        # KNOWN, UNCHANGED one this run itself triaged, not new/edited feedback.
        assert fake.labels_set and ac.WAITING_FOR_REVIEW_LABEL in fake.labels_set[-1]

    def test_relist_still_blocks_when_a_not_an_issue_comment_is_edited(
        self, address_env, monkeypatch
    ) -> None:
        """A reviewer editing a comment this run already triaged as
        `not_an_issue` — while an EARLIER comment's workflow was still
        running — must still block success and get re-triaged: only an
        UNCHANGED known non-issue is exempted from the re-list check."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)
        fake.review_comments = [_comment(2, body="just a question")]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]

        real_unresolved_comments = ac._unresolved_comments
        calls = {"n": 0}

        def _stub(client, owner, repo, pr_number):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_unresolved_comments(client, owner, repo, pr_number)
            # The final re-list sees the SAME comment id, but its live body
            # has changed since this run triaged it.
            edited = _comment(2, body="actually, please also check the edge case")
            thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
            return [edited], {2: thread}, [], {2: [edited]}

        monkeypatch.setattr(ac, "_unresolved_comments", _stub)

        ac._run_address_comments("job1", req, "tok")

        assert fake.labels_set == []  # the edited feedback still blocks success

    def test_dispatch_time_freshness_check_blocks_stale_implementation(
        self, address_env, monkeypatch
    ) -> None:
        """A reviewer's follow-up posted while triage/planning (LLM round-trips)
        were running — after `comment` was snapshotted, before the
        implementation workflow would be dispatched — must prevent the
        workflow from ever being dispatched: `_reply_and_resolve`'s own
        freshness check alone runs too late, since by then the workflow would
        already have implemented and pushed a fix for the stale snapshot."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 6))
        # comment 6 simulates feedback that landed after comment 2 was
        # snapshotted as this run's representative comment.
        fake.review_comments = [_comment(2), _comment(6, body="use the other approach instead")]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "failed"
        assert "superseded" in outcome.detail
        assert address_env["child_jobs"] == []  # implementation never dispatched
        assert address_env["executions"] == []
        assert fake.replies == []
        assert fake.resolved == []

    def test_dispatch_time_freshness_check_blocks_manually_resolved_thread(
        self, address_env, monkeypatch
    ) -> None:
        """A reviewer can resolve a thread by hand (the GitHub UI) while
        triage/planning for it is still running — this supersedes the
        in-flight work just as decisively as new feedback would. Dispatching
        (or pushing) a fix for a concern the reviewer already closed is
        wasted work and must be blocked."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        # The LIVE thread is already resolved (a human resolved it after this
        # run's snapshot was taken); the snapshot itself still shows it open.
        thread = ReviewThread(id="T2", is_resolved=True, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            ReviewThread(id="T2", is_resolved=False, comment_ids=(2,)),  # the snapshot's view
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "failed"
        assert "superseded" in outcome.detail
        assert address_env["child_jobs"] == []  # implementation never dispatched
        assert fake.replies == []
        assert fake.resolved == []  # never re-resolved — already resolved live

    def test_dispatch_time_freshness_check_blocks_deleted_thread(
        self, address_env, monkeypatch
    ) -> None:
        """A reviewer (or the PR author) can delete the representative comment
        or its whole thread while triage/planning is still running — the live
        re-fetch then finds no matching thread at all. This must be treated
        exactly like an already-resolved thread (superseded, stop) rather than
        proceeding as if nothing changed, or the implementation workflow would
        be dispatched and push a fix for withdrawn feedback."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        # The snapshot's view of the thread; the LIVE fake state has NO thread
        # with this id at all (fake.threads left empty) — simulating deletion.
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "failed"
        assert "superseded" in outcome.detail
        assert address_env["child_jobs"] == []  # implementation never dispatched
        assert fake.replies == []
        assert fake.resolved == []

    def test_dispatch_time_freshness_check_blocks_edit_to_earlier_history_message(
        self, address_env, monkeypatch
    ) -> None:
        """Triage/planning consume the thread's FULL history, not just the
        representative (latest) comment. A reviewer editing an EARLIER
        message in that history — e.g. changing the requested approach in the
        root while leaving a later "still broken" reply untouched — carries
        no new comment id, so an id-only or representative-only body check
        would miss it. It must still block dispatch."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 4))
        root_snapshot = _comment(2, body="please fix the null check")
        latest = _comment(4, body="still broken")
        # The LIVE root comment's body has changed since triage ran — the
        # representative comment (4) itself is unchanged.
        fake.review_comments = [_comment(2, body="actually, use a different approach entirely"), latest]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            latest,
            thread,
            [root_snapshot, latest],  # the full history triage/planning actually saw
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "failed"
        assert "superseded" in outcome.detail
        assert address_env["child_jobs"] == []  # implementation never dispatched
        assert fake.replies == []
        assert fake.resolved == []

    def test_triage_and_plan_prompts_include_full_thread_history(
        self, address_env, monkeypatch
    ) -> None:
        """A short context-dependent follow-up ("still broken") is unintelligible
        in isolation — triage and planning must ground on the thread's full
        conversation (root concern + any earlier reply), not just the latest
        message, so the LLM can tell what "still" refers to."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 6))
        root = _comment(2, body="This null-check is missing entirely")
        follow_up = _comment(6, body="still broken")
        fake.review_comments = [root, follow_up]
        fake.threads = [thread]

        seen_prompts: list[str] = []

        def _gen(prompt, *, schema, **kw):
            seen_prompts.append(prompt)
            if schema is ac.CommentTriage:
                return ac.CommentTriage(raises_issue=False, is_false_positive=False, issue_summary="s")
            return ac.IssueResolutionPlan(chosen_plan="p")

        monkeypatch.setattr(ac._main, "generate_structured", _gen)

        ac._handle_comment(
            fake,
            "parent",
            req,
            follow_up,
            thread,
            [root, follow_up],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert len(seen_prompts) == 1  # triage only — raises_issue=False short-circuits planning
        assert "This null-check is missing entirely" in seen_prompts[0]
        assert "still broken" in seen_prompts[0]

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
        # A true no-op run (no comments, no retries) never did any work — must
        # NOT be labelled waiting-for-review just because "nothing failed".
        assert fake.labels_set == []

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
        monkeypatch.setattr(ac, "start_address_comments_thread", lambda *a, **kw: started.append(a))

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

    def test_409_when_sibling_job_running_on_same_checkout_different_pr(
        self, route_env
    ) -> None:
        """An operator-pinned repo_path is shared (unnamespaced) across every PR
        of that repo. A job already active for a DIFFERENT PR on the SAME
        checkout must block admission — the PR-scoped running-job check alone
        would miss this, since it only matches this exact PR number."""
        route_env["jobs"].create_job(
            "sibling-job",
            status="running",
            repo_path=route_env["repo_path"],
            github_context={"owner": "o", "repo": "r", "pr_number": 99},
        )

        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": route_env["repo_path"], "pr_number": 7},
        )

        assert resp.status_code == 409
        assert "sibling-job" in resp.json()["detail"]
        assert route_env["started"] == []  # never launched

    def test_checkout_admission_lock_wraps_sibling_scan_and_job_creation(
        self, route_env, monkeypatch
    ) -> None:
        """The sibling-checkout scan and the job it admits must run inside ONE
        checkout-keyed lock — not just the per-PR `_pr_review_admission` lock,
        which alone would let two DIFFERENT PRs sharing the same operator-pinned
        repo_path both pass the scan (neither job exists yet) before either
        creates its job, admitting a race onto the same checkout."""
        from software_engineering_team.api import coding_team_main as _main

        events: list[str] = []
        orig_create_job = _main.create_job

        @contextlib.contextmanager
        def _recording_admission(repo_path: str):
            assert repo_path == route_env["repo_path"]
            events.append("enter")
            yield
            events.append("exit")

        def _recording_create_job(**kw):
            events.append("create_job")
            return orig_create_job(**kw)

        monkeypatch.setattr(_main, "_checkout_admission", _recording_admission)
        monkeypatch.setattr(_main, "create_job", _recording_create_job)

        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": route_env["repo_path"], "pr_number": 7},
        )

        assert resp.status_code == 200
        # create_job must run strictly BETWEEN the lock's enter and exit.
        assert events == ["enter", "create_job", "exit"]

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
            number=7,
            html_url="https://example/pull/7",
            head="feature",
            base="main",
            head_sha="sha1",
            title="t",
            body="b",
            draft=False,
            author="alice",
            state="closed",
            updated_at="",
            labels=(),
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


# ---------------------------------------------------------------------------
# Route: GET /pulls/{pr_number}/address-comments/running
# ---------------------------------------------------------------------------


class TestAddressCommentsAdmissionRoute:
    """The lightweight pre-check a caller (the unified API) uses to avoid
    touching a PR's shared checkout when a job is already running for it."""

    # Reuses TestAddressCommentsRoute's fixture body directly (a fixture defined
    # on one test class isn't visible to another).
    route_env = TestAddressCommentsRoute.route_env

    def test_reports_none_when_nothing_running(self, route_env) -> None:
        resp = route_env["client"].get(
            "/pulls/7/address-comments/running", params={"owner": "o", "repo": "r"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"running_job_id": None}

    def test_reports_running_job_id(self, route_env, monkeypatch) -> None:
        from software_engineering_team.api import coding_team_main as _main

        monkeypatch.setattr(_main, "_running_review_for_pr", lambda *_a, **_kw: "job-abc")
        resp = route_env["client"].get(
            "/pulls/7/address-comments/running", params={"owner": "o", "repo": "r"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"running_job_id": "job-abc"}

    def test_is_read_only_and_creates_no_job(self, route_env) -> None:
        before = len(route_env["jobs"].list_jobs())
        route_env["client"].get(
            "/pulls/7/address-comments/running", params={"owner": "o", "repo": "r"}
        )
        assert len(route_env["jobs"].list_jobs()) == before

    def test_repo_path_catches_sibling_job_on_different_pr(self, route_env) -> None:
        """An operator-pinned repo_path is shared, unnamespaced, across every PR
        of that repo, so a job active for a DIFFERENT PR on the SAME checkout
        must be reported too — not just an exact PR-number match."""
        route_env["jobs"].create_job(
            "sibling-job",
            status="running",
            repo_path="/shared/checkout",
            github_context={"owner": "o", "repo": "r", "pr_number": 99},
        )
        resp = route_env["client"].get(
            "/pulls/7/address-comments/running",
            params={"owner": "o", "repo": "r", "repo_path": "/shared/checkout"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"running_job_id": "sibling-job"}

    def test_omitting_repo_path_skips_the_sibling_checkout_check(self, route_env) -> None:
        route_env["jobs"].create_job(
            "sibling-job",
            status="running",
            repo_path="/shared/checkout",
            github_context={"owner": "o", "repo": "r", "pr_number": 99},
        )
        resp = route_env["client"].get(
            "/pulls/7/address-comments/running", params={"owner": "o", "repo": "r"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"running_job_id": None}

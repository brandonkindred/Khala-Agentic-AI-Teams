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
import subprocess
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
                            "comments": {
                                "nodes": [{"databaseId": 1}],
                                "pageInfo": {"hasNextPage": False},
                            },
                        },
                        {
                            "id": "T_open",
                            "isResolved": False,
                            "comments": {
                                "nodes": [{"databaseId": 2}, {"databaseId": 3}],
                                "pageInfo": {"hasNextPage": False},
                            },
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
                        nodes=[
                            {
                                "id": "T1",
                                "isResolved": False,
                                "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                            }
                        ],
                    ),
                )
            return httpx.Response(
                200,
                json=_threads_response(
                    nodes=[
                        {
                            "id": "T2",
                            "isResolved": True,
                            "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                        }
                    ]
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
        self.labels_created: list[str] = []
        self.authenticated_login = "khala-bot"
        self.web_host = "github.com"
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

    def get_file_contents_detailed(
        self, _o: str, _r: str, _p: str, _ref: str
    ) -> tuple[Optional[str], bool]:
        return self.file_contents, False

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

    def create_label(self, _o: str, _r: str, name: str, **_kw: Any) -> None:
        self.labels_created.append(name)


def _comment(
    cid: int, body: str = "This has a bug", path: str = "a.py", line: int = 2, author: str = ""
) -> ReviewComment:
    return ReviewComment(
        id=cid, path=path, line=line, body=body, html_url=f"https://example/c/{cid}", author=author
    )


def _khala_reply(ac, fake, cid: int) -> ReviewComment:
    """A synthetic Khala-generated reply comment, built from the production
    module's own marker constant (`ac._KHALA_COMMENT_MARKER`) and `fake`'s
    own `authenticated_login` rather than hardcoding either literal here —
    so a change to the production marker or `_FakeClient`'s default bot
    identity can't silently stop these tests from exercising the
    retry-resolve path they're meant to cover."""
    return _comment(
        cid,
        body=f"Addressed by the SE team. {ac._KHALA_COMMENT_MARKER}",
        author=fake.authenticated_login,
    )


def _khala_reply_with_accounted_through(
    ac, fake, cid: int, accounted_through: int, body: str = "Addressed by the SE team."
) -> ReviewComment:
    """A synthetic Khala-generated reply that also carries the
    `_accounted_through_marker` boundary `_reply_and_resolve` now embeds in
    every reply it posts, built from the production module's own marker
    helpers (mirroring `_khala_reply`'s rationale) so a change to the marker
    format can't silently stop these tests from exercising the path they're
    meant to cover."""
    return _comment(
        cid,
        body=f"{body} {ac._accounted_through_marker(accounted_through)} {ac._KHALA_COMMENT_MARKER}",
        author=fake.authenticated_login,
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
    # Not cancelled by default — a test simulating cancellation flips this
    # (e.g. `job_state["status"] = "cancelled"`).
    job_state: dict[str, Any] = {"status": "running"}
    # Comment-scoped child job records, keyed by `_child_job_id_for_comment(id)`
    # (e.g. "address-comment:2") — a test simulating an already-published or
    # still-active prior dispatch populates this; absent (the default), a
    # lookup by a comment-scoped id returns None (not found), matching a real
    # job service for a comment never dispatched before.
    child_job_states: dict[str, dict[str, Any]] = {}

    monkeypatch.setattr(_main, "GitHubClient", lambda **_kw: fake)
    monkeypatch.setattr(_main, "update_job", lambda job_id, **kw: job_updates.append(kw))
    monkeypatch.setattr(_main, "update_review", lambda job_id, **kw: None)
    monkeypatch.setattr(_main, "create_job", lambda **kw: child_jobs.append(kw))
    monkeypatch.setattr(_main, "encrypt_token", lambda token: "encrypted-token")
    # The real heartbeat_job hits the job service; _run_address_comments now
    # heartbeats continuously (see the P1 fix for the admission-guard race), so
    # every test that reaches it would otherwise pay a real network call.
    monkeypatch.setattr(_main, "heartbeat_job", lambda job_id: None)
    # The real get_job also hits the job service — _job_cancelled checks it at
    # multiple points in the run now, so every test would otherwise pay a real
    # (and, against the deliberately-unreachable test JOB_SERVICE_URL, slow
    # and retried) network call for each check. `_previously_published_fix`
    # and `_dispatch_implementation`'s active-job guard also now call get_job,
    # but with a comment-scoped id (see `child_job_states` above) rather than
    # the parent run's own job_id `_job_cancelled` checks — route each id to
    # its own fake store rather than conflating the two.
    monkeypatch.setattr(
        _main,
        "get_job",
        lambda job_id: dict(child_job_states[job_id]) if job_id in child_job_states else (
            dict(job_state) if not job_id.startswith("address-comment:") else None
        ),
    )

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
        "job_state": job_state,
        "child_job_states": child_job_states,
        "request": AddressCommentsRequest(owner="o", repo="r", repo_path="/tmp/x", pr_number=7),
    }


def _stub_triage(monkeypatch, ac, *, raises_issue: bool, is_false_positive: bool) -> None:
    """Stub ``_main.generate_structured`` to return deterministic triage/plan output.

    ``raises_issue``/``is_false_positive`` control ONLY the ``CommentTriage``
    verdict (matching this codebase's real encoding: ``is_false_positive`` is
    meaningless unless ``raises_issue`` is also True — see ``CommentTriage``'s
    own docstring). Any other requested schema (i.e. ``IssueResolutionPlan``)
    always gets the same fixed three-candidate plan, regardless of these flags.
    """
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
                ac.SolutionCandidate(
                    summary="C",
                    requirement_fit=4,
                    computational_performance=4,
                    memory_usage=4,
                    code_complexity=4,
                ),
            ],
            chosen_candidate_index=0,  # "A" — the higher-scoring candidate
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
        unresolved, by_comment, retry_resolve, _history, _ambiguous = ac.unresolved_comments(fake, "o", "r", 7)
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
        unresolved, by_comment, retry_resolve, _history, _ambiguous = ac.unresolved_comments(fake, "o", "r", 7)
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
        unresolved, _by_comment, _retry_resolve, _history, _ambiguous = ac.unresolved_comments(fake, "o", "r", 7)
        # The relative order between independent threads is not part of the
        # contract — each unresolved thread contributes its latest comment,
        # but which thread's comment comes first is an implementation detail.
        assert {c.id for c in unresolved} == {3, 5}

    def test_thread_with_marked_reply_excluded_even_via_unmarked_root(
        self, address_env, monkeypatch
    ) -> None:
        """A thread whose root comment carries no marker but whose LATER reply
        does (Khala already replied) must never surface via its unmarked root —
        the whole thread is excluded from `unresolved`. With PERSISTED evidence
        that Khala's own resolve mutation for this reply is on record as having
        failed, its id is reported for a resolve-only retry."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _khala_reply(ac, fake, 3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]
        monkeypatch.setattr(
            address_env["main"],
            "has_recorded_resolve_failure",
            lambda owner, repo, pr_number, thread_id, reply_id: (thread_id, reply_id) == ("T2", 3),
        )

        unresolved, _by_comment, retry_resolve, _history, _ambiguous = ac.unresolved_comments(fake, "o", "r", 7)

        assert unresolved == []
        assert retry_resolve == [("T2", 3)]

    def test_khala_marker_reply_with_no_recorded_failure_is_ambiguous_and_skipped(
        self, address_env
    ) -> None:
        """P1 regression: an unresolved thread whose LATEST message is Khala's
        own reply is NOT automatically retried just because GitHub still
        reports it unresolved — that state is identical whether our resolve
        mutation genuinely failed or a reviewer manually clicked "Reopen
        conversation" with no new comment. Without persisted evidence that
        OUR resolve call actually ran and failed for this reply (the default
        in this test — no Postgres, so `has_recorded_resolve_failure`
        degrades to False), the thread must be treated as an ambiguous
        possible reviewer reopen: neither auto-resolved (excluded from
        `retry_resolve`) nor silently re-triaged (excluded from
        `unresolved`) — just left alone for a human to actually look at."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _khala_reply(ac, fake, 3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]

        unresolved, _by_comment, retry_resolve, _history, ambiguous = ac.unresolved_comments(fake, "o", "r", 7)

        assert unresolved == []
        assert retry_resolve == []
        # P1 regression: dropped from both `unresolved` and `retry_resolve`,
        # the thread must still surface somewhere a completion check can see
        # it as blocking — see `ambiguous_threads` in `_unresolved_comments`.
        assert ambiguous == [("T2", 3)]

    def test_recorded_failure_for_a_superseded_reply_does_not_authorize_retry(
        self, address_env, monkeypatch
    ) -> None:
        """Evidence recorded against an OLDER Khala reply must not authorize a
        retry for a DIFFERENT (newer) reply on the same thread — e.g. a
        reviewer reopened after reply #3, Khala replied again as #5, and only
        #3's earlier resolve failure is on record. `has_recorded_resolve_
        failure` is called with the CURRENT latest reply's id, so a store
        keyed on the old id correctly reports no evidence for the new one."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _khala_reply(ac, fake, 5),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 5))]
        monkeypatch.setattr(
            address_env["main"],
            "has_recorded_resolve_failure",
            lambda owner, repo, pr_number, thread_id, reply_id: (thread_id, reply_id) == ("T2", 3),
        )

        unresolved, _by_comment, retry_resolve, _history, _ambiguous = ac.unresolved_comments(fake, "o", "r", 7)

        assert unresolved == []
        assert retry_resolve == []

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
            _khala_reply(ac, fake, 3),
            _comment(4, body="this fix is incomplete, the null case is still broken"),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3, 4))]

        unresolved, _by_comment, retry_resolve, _history, _ambiguous = ac.unresolved_comments(fake, "o", "r", 7)

        assert [c.id for c in unresolved] == [4]
        assert retry_resolve == []

    def test_reviewer_feedback_unaccounted_for_by_reply_is_re_triaged_even_with_a_lower_id(
        self, address_env
    ) -> None:
        """Regression test for the P1 gap: a reviewer's follow-up (H, id 3)
        that was posted before Khala's reply (R, id 4) actually landed — but
        AFTER `R` was generated from a snapshot that only saw the root
        comment — must still be re-triaged next run, even though H's id is
        LOWER than R's and R is therefore GitHub's chronologically-latest
        message. Using message order alone (R is latest → "safe resolve-only
        retry") would silently bury H forever the moment the retry resolves
        the thread; the reply's own embedded accounted-through boundary (2 —
        the root comment's id, the only thing R was actually generated from)
        proves H (id 3 > 2) was never accounted for."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _comment(3, body="wait, this is still broken"),
            _khala_reply_with_accounted_through(ac, fake, 4, accounted_through=2),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3, 4))]

        unresolved, _by_comment, retry_resolve, history, _ambiguous = ac.unresolved_comments(fake, "o", "r", 7)

        assert [c.id for c in unresolved] == [3]
        assert retry_resolve == []
        # The full thread is still handed to triage as context, not just H
        # in isolation.
        assert [m.id for m in history[3]] == [2, 3, 4]

    def test_reply_accounted_for_every_earlier_message_is_still_a_clean_retry(
        self, address_env, monkeypatch
    ) -> None:
        """The ordinary, fine shape — every message in the thread predates
        (by id) the boundary Khala's reply was actually generated from —
        must still take the resolve-only retry path with the new
        accounted-through check in place, not be swept into re-triage just
        because the check now exists. Persisted evidence that Khala's own
        resolve for this reply is on record as having failed authorizes the
        retry (see the reviewer-reopen ambiguity check in
        TestUnresolvedComments above)."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _comment(3, body="also consider this"),
            _khala_reply_with_accounted_through(ac, fake, 4, accounted_through=3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3, 4))]
        monkeypatch.setattr(
            address_env["main"],
            "has_recorded_resolve_failure",
            lambda owner, repo, pr_number, thread_id, reply_id: (thread_id, reply_id) == ("T2", 4),
        )

        unresolved, _by_comment, retry_resolve, _history, _ambiguous = ac.unresolved_comments(fake, "o", "r", 7)

        assert unresolved == []
        assert retry_resolve == [("T2", 4)]

    def test_fails_closed_when_unresolved_thread_has_no_fetched_messages(
        self, address_env
    ) -> None:
        """An unresolved thread whose comment ids never showed up in the REST
        comment listing (e.g. truncated by list_review_comments' traversal
        cap) must fail closed rather than be silently skipped — dropping it
        would omit it from both triage and the caller's final re-list check,
        letting the run wrongly declare the PR waiting for review."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = []  # comment 9 never came back from the REST listing
        fake.threads = [ReviewThread(id="T9", is_resolved=False, comment_ids=(9,))]

        with pytest.raises(ReviewThreadsUnavailableError):
            ac.unresolved_comments(fake, "o", "r", 7)

    def test_fails_closed_when_unresolved_thread_is_only_partially_fetched(
        self, address_env
    ) -> None:
        """A thread with SOME (but not all) of its comment ids present in the
        REST listing is just as dangerous as one with zero: the early message
        inside the traversal cap can look like a complete `messages` list and
        get silently treated as the latest, grounding triage on stale
        history. GraphQL's `comment_ids` is the authoritative membership
        list — any id missing from the fetched messages must fail closed."""
        ac, fake = address_env["ac"], address_env["fake"]
        # Only comment 2 (the thread's root) came back from the REST listing;
        # comment 3 (a later reply) was truncated by the traversal cap.
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]

        with pytest.raises(ReviewThreadsUnavailableError):
            ac.unresolved_comments(fake, "o", "r", 7)

    def test_thread_with_marked_reply_that_is_already_resolved_is_not_retried(
        self, address_env
    ) -> None:
        """An already-resolved thread with a Khala reply needs no retry — it's
        just an ordinary resolved thread, not a pending resolve-retry."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _khala_reply(ac, fake, 3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=True, comment_ids=(2, 3))]

        unresolved, _by_comment, retry_resolve, _history, _ambiguous = ac.unresolved_comments(fake, "o", "r", 7)

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

        unresolved, _by_comment, retry_resolve, _history, _ambiguous = ac.unresolved_comments(fake, "o", "r", 7)

        assert [c.id for c in unresolved] == [2]
        assert retry_resolve == []

    def test_login_resolution_failure_fails_closed(self, address_env) -> None:
        """A failure to resolve the authenticated login must abort the run
        rather than degrade to treating every marker as untrusted: on a
        thread that DOES have a discoverable thread, an empty login would
        make Khala's own genuine prior reply look like fresh reviewer
        feedback, risking a duplicate implementation dispatch for an
        already-fixed comment."""
        ac, fake = address_env["ac"], address_env["fake"]

        def _boom() -> str:
            raise RuntimeError("network blip")

        fake.get_authenticated_login = _boom  # type: ignore[assignment]
        fake.review_comments = [
            _comment(2, body="Addressed. <!-- khala-generated -->", author="khala-bot"),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]

        with pytest.raises(ReviewThreadsUnavailableError):
            ac.unresolved_comments(fake, "o", "r", 7)

    def test_login_resolution_returning_empty_fails_closed(self, address_env) -> None:
        """get_authenticated_login()'s own contract degrades to "" on a
        best-effort failure rather than raising — that must still fail
        closed here, via the empty-string check."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.authenticated_login = ""
        fake.review_comments = [
            _comment(2, body="Addressed. <!-- khala-generated -->", author="khala-bot"),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]

        with pytest.raises(ReviewThreadsUnavailableError):
            ac.unresolved_comments(fake, "o", "r", 7)


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

    def test_fork_pr_uses_the_given_web_host(self, address_env) -> None:
        """A GitHub Enterprise Server deployment's fork remote must resolve
        against ITS OWN web host, not the hardcoded github.com Cloud host."""
        ac, fake = address_env["ac"], address_env["fake"]
        fake.pr = replace(fake.pr, head_repo_full_name="contributor/r")
        assert (
            ac._pr_head_remote("o", "r", fake.pr, web_host="ghes.example.com")
            == "https://ghes.example.com/contributor/r.git"
        )


# ---------------------------------------------------------------------------
# _bounded_cited_excerpt
# ---------------------------------------------------------------------------


class TestBoundedCitedExcerpt:
    def test_returns_unchanged_when_already_within_budget(self, address_env) -> None:
        ac = address_env["ac"]
        text = "line1\nline2\nline3\n"
        assert ac._bounded_cited_excerpt(text, 2) == text

    def test_falls_back_to_from_start_when_line_is_none(self, address_env) -> None:
        """A file-level comment has no specific line to center on — keep the
        prior, line-agnostic from-the-start behavior."""
        ac = address_env["ac"]
        text = "x" * (ac._MAX_CITED_CODE_CHARS + 500)
        assert ac._bounded_cited_excerpt(text, None) == text[: ac._MAX_CITED_CODE_CHARS]

    def test_centers_excerpt_on_cited_line_beyond_the_char_budget(self, address_env) -> None:
        """The cited line lies well past the first _MAX_CITED_CODE_CHARS
        characters — a from-the-start truncation would drop it entirely. The
        excerpt must actually include the cited line's own content."""
        ac = address_env["ac"]
        # Each line is 10 chars ("lineNNNNN\n"); the file is far larger than
        # the char budget, so a from-the-start truncation would never reach
        # a line deep in the file.
        total_lines = (ac._MAX_CITED_CODE_CHARS // 10) * 3
        lines = [f"line{i:05d}\n" for i in range(total_lines)]
        text = "".join(lines)
        cited_line_number = total_lines - 5  # near the end, 1-based
        cited_line_text = lines[cited_line_number - 1]

        excerpt = ac._bounded_cited_excerpt(text, cited_line_number)

        assert cited_line_text in excerpt
        assert len(excerpt) <= ac._MAX_CITED_CODE_CHARS

    def test_excerpt_never_exceeds_budget_for_line_near_file_start(self, address_env) -> None:
        ac = address_env["ac"]
        total_lines = (ac._MAX_CITED_CODE_CHARS // 10) * 3
        text = "".join(f"line{i:05d}\n" for i in range(total_lines))

        excerpt = ac._bounded_cited_excerpt(text, 1)

        assert "line00000\n" in excerpt
        assert len(excerpt) <= ac._MAX_CITED_CODE_CHARS

    def test_truncates_a_single_line_that_alone_exceeds_the_budget(self, address_env) -> None:
        """P2 regression: a minified/generated file can have a single line
        longer than _MAX_CITED_CODE_CHARS. The expand-outward loop's budget
        guard (`total < _MAX_CITED_CODE_CHARS`) starts already false in that
        case, so the loop body never runs and the whole oversized line was
        previously returned unbounded, defeating the cap entirely."""
        ac = address_env["ac"]
        huge_line = "x" * (ac._MAX_CITED_CODE_CHARS * 2)
        text = f"short line before\n{huge_line}\nshort line after\n"
        cited_line_number = 2

        excerpt = ac._bounded_cited_excerpt(text, cited_line_number)

        assert len(excerpt) <= ac._MAX_CITED_CODE_CHARS + len("...(truncated)")
        assert excerpt.startswith("x" * 10)


# ---------------------------------------------------------------------------
# _format_thread_history
# ---------------------------------------------------------------------------


def _msg(id_: int, body: str) -> ReviewComment:
    return ReviewComment(id=id_, path="a.py", line=1, body=body, html_url=f"https://x/{id_}")


class TestFormatThreadHistory:
    def test_renders_all_messages_when_within_budget(self, address_env) -> None:
        ac = address_env["ac"]
        history = [_msg(1, "first"), _msg(2, "second"), _msg(3, "third")]

        rendered = ac._format_thread_history(history)

        assert "first" in rendered
        assert "second" in rendered
        assert "third" in rendered

    def test_bounds_total_size_for_a_long_discussion(self, address_env) -> None:
        """P2 regression: up to 100 thread messages were concatenated with no
        aggregate size cap before going into an LLM prompt (unlike the cited-
        code excerpt, which IS capped) — a long discussion could produce a
        multi-megabyte prompt."""
        ac = address_env["ac"]
        history = [_msg(i, "x" * 5000) for i in range(100)]

        rendered = ac._format_thread_history(history)

        assert len(rendered) <= ac._MAX_THREAD_HISTORY_CHARS + 5000

    def test_preserves_the_latest_message_in_full(self, address_env) -> None:
        """The latest message is "the current concern" per both callers'
        prompt wording — it must survive truncation even when earlier
        messages are dropped to make room."""
        ac = address_env["ac"]
        history = [_msg(i, "x" * 5000) for i in range(100)]
        latest_body = "THIS IS THE CURRENT CONCERN"
        history.append(_msg(100, latest_body))

        rendered = ac._format_thread_history(history)

        assert latest_body in rendered


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


class TestReadCitedCode:
    def test_confirmed_missing_file_returns_sentinel_not_a_raise(
        self, address_env
    ) -> None:
        """A confirmed-404 cited file (the common, legitimate case of a
        review concern addressed by deleting/renaming the file) must be
        triage-able, not perma-failed: this returns a sentinel string
        instead of raising CitedCodeUnavailableError."""
        ac, fake = address_env["ac"], address_env["fake"]

        def _detailed(_o: str, _r: str, _p: str, _ref: str):
            return None, True  # confirmed 404

        fake.get_file_contents_detailed = _detailed  # type: ignore[assignment]

        result = ac._read_cited_code(fake, "o", "r", _comment(2), "sha1")

        assert "no longer exists" in result

    def test_directory_or_undecodable_still_raises(self, address_env) -> None:
        """A directory / non-file entry / undecodable payload is genuinely
        unreadable, NOT confirmed absent — this must still fail closed."""
        ac, fake = address_env["ac"], address_env["fake"]

        def _detailed(_o: str, _r: str, _p: str, _ref: str):
            return None, False  # not a confirmed 404

        fake.get_file_contents_detailed = _detailed  # type: ignore[assignment]

        with pytest.raises(ac.CitedCodeUnavailableError):
            ac._read_cited_code(fake, "o", "r", _comment(2), "sha1")

    def test_transport_error_still_raises(self, address_env) -> None:
        ac, fake = address_env["ac"], address_env["fake"]

        def _detailed(_o: str, _r: str, _p: str, _ref: str):
            raise RuntimeError("network blip")

        fake.get_file_contents_detailed = _detailed  # type: ignore[assignment]

        with pytest.raises(ac.CitedCodeUnavailableError):
            ac._read_cited_code(fake, "o", "r", _comment(2), "sha1")


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
        assert address_env["child_jobs"][0]["job_id"] == "address-comment:2"
        github = address_env["executions"][0]["kwargs"]["github"]
        assert github["publish_mode"] == "existing_pr"
        assert github["integration_branch"] == "feature"
        # The PR head SHA this run's triage/plan was grounded on (see the
        # pr_head_sha argument to _handle_comment above) must reach the child
        # workflow so branch prep can pin to it -- see
        # test_github_branch_prep_forwards_expected_head_sha
        # (test_coding_team_temporal_workflow.py) and
        # test_expected_head_sha_mismatch_blocks_prep_without_mutating_checkout
        # (test_coding_team_github_source.py) for the mismatch-detection path
        # this enables.
        assert github["expected_head_sha"] == "sha1"
        assert fake.replies and fake.replies[0][0] == 2
        assert fake.resolved == ["T2"]

    def test_pr_closed_during_dispatch_skips_reply_and_resolve(
        self, address_env, monkeypatch
    ) -> None:
        """The implementation workflow can block for a long time; if the PR
        is merged or closed while it's running, the fix has already been
        published by the time `_dispatch_implementation` returns (that
        can't be undone from here), but replying to and resolving the
        thread on an already-closed PR must still be skipped."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        calls = {"n": 0}
        real_pr = fake.pr

        def _get_pull_request(_o, _r, _n):
            calls["n"] += 1
            # The post-triage and post-planning freshness checks both see the
            # PR still open; the NEW post-dispatch check discovers it closed.
            if calls["n"] <= 2:
                return real_pr
            return replace(real_pr, state="closed")

        fake.get_pull_request = _get_pull_request  # type: ignore[assignment]

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
        assert "no longer open" in outcome.detail
        assert address_env["child_jobs"]  # the implementation job DID run
        assert fake.replies == []  # but no reply or resolve followed
        assert fake.resolved == []

    def test_existing_job_lookup_failure_fails_closed_without_dispatching(
        self, address_env, monkeypatch
    ) -> None:
        """P1 regression: `_dispatch_implementation`'s own "is there already an
        ACTIVE child job for this comment?" lookup (distinct from
        `_previously_published_fix`'s own lookup, which round 7 already
        hardened) must fail this comment closed — not silently treat a
        transient lookup failure as "no active job" and fall through to
        `create_job`'s upsert, which could double-dispatch on top of a child
        job that is genuinely still running."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        child_job_id = ac._child_job_id_for_comment(2)
        calls = {"n": 0}

        def _get_job(job_id):
            if job_id == child_job_id:
                calls["n"] += 1
                # 1st call: `_previously_published_fix`'s own lookup -- no
                # prior job (normal fresh-triage path). 2nd call:
                # `_dispatch_implementation`'s active-job guard -- fails.
                if calls["n"] == 1:
                    return None
                raise RuntimeError("job service unavailable")
            return dict(address_env["job_state"])

        monkeypatch.setattr(address_env["main"], "get_job", _get_job)

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
        assert "could not check for an existing job" in outcome.detail
        # Nothing was created or dispatched, so there is no leftover
        # unpublished work on the shared `development` branch to flag.
        assert outcome.left_unpublished_work is False
        assert address_env["child_jobs"] == []
        assert address_env["executions"] == []
        assert fake.replies == []
        assert fake.resolved == []

    def test_previously_published_fix_retries_reply_resolve_without_redispatching(
        self, address_env, monkeypatch
    ) -> None:
        """An earlier run's implementation can have already been published
        (child job completed) while that run's own reply/resolve step then
        failed — GitHub still reports the thread unresolved with the SAME
        original comment as its latest message, so this run must retry ONLY
        the reply/resolve step for the already-completed child job, never
        re-triage or dispatch a brand new implementation workflow on top of
        one that may already be on the PR branch."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        address_env["child_job_states"][ac._child_job_id_for_comment(2)] = {
            "status": "completed",
            "chosen_plan": "Add the missing null check.",
            "github_context": {"review_comment_id": 2},
        }
        # If triage or planning were reached, this would raise and fail the test.
        monkeypatch.setattr(
            ac,
            "_triage_comment",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not re-triage")),
        )

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
        assert "Add the missing null check." in outcome.detail
        assert address_env["child_jobs"] == []  # no new implementation dispatched
        assert fake.replies and fake.replies[0][0] == 2
        assert "address-comment:2" in fake.replies[0][1]
        assert fake.resolved == ["T2"]

    def test_previously_published_fix_with_closed_pr_skips_reply(
        self, address_env
    ) -> None:
        """A fix already published by an earlier run must not be replied to
        or resolved against a PR that has since closed or merged."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]
        fake.get_pull_request = lambda _o, _r, _n: replace(fake.pr, state="closed")  # type: ignore[assignment]

        address_env["child_job_states"][ac._child_job_id_for_comment(2)] = {
            "status": "completed",
            "chosen_plan": "Add the missing null check.",
            "github_context": {"review_comment_id": 2},
        }

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
        assert "no longer open" in outcome.detail
        assert fake.replies == []
        assert fake.resolved == []

    def test_active_prior_job_for_same_comment_blocks_redispatch(
        self, address_env, monkeypatch
    ) -> None:
        """The child job id is now comment-scoped (stable across runs), so an
        earlier run's job for this exact comment can still be ACTIVE (e.g. a
        stale/orphaned "running" row left behind by a crashed worker) when
        this run reaches dispatch — `_previously_published_fix` only skips
        re-dispatch for a `"completed"` job, so anything else falls through
        here. Dispatching anyway would call `create_job` with the SAME id and
        silently reset that row, risking corruption of a genuinely still-
        running implementation. This must be refused instead: no new job is
        created, and the thread is left unresolved for the next run rather
        than replied to or resolved."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]
        address_env["child_job_states"][ac._child_job_id_for_comment(2)] = {"status": "running"}

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
        assert outcome.left_unpublished_work is True
        assert address_env["child_jobs"] == []  # never called create_job
        assert fake.replies == []
        assert fake.resolved == []

    def test_terminal_non_completed_prior_job_allows_redispatch(
        self, address_env, monkeypatch
    ) -> None:
        """Unlike an ACTIVE prior job, one that already reached a TERMINAL
        but non-"completed" status (failed, completed_with_failures,
        cancelled) is safe to reset and retry — that is the ordinary case a
        comment resurfaces for re-dispatch at all (its previous attempt
        didn't succeed)."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]
        address_env["child_job_states"][ac._child_job_id_for_comment(2)] = {"status": "failed"}

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
        assert address_env["child_jobs"] and address_env["child_jobs"][0]["job_id"] == "address-comment:2"
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

    def test_false_positive_blocks_resolve_when_feedback_lands_after_the_reply(
        self, address_env, monkeypatch
    ) -> None:
        """A reviewer can post follow-up feedback in the window between the
        pre-reply freshness check and the resolve call — e.g. while
        `reply_to_review_comment` itself is in flight. The reply has already
        been posted by then (best-effort, not undoable), but resolving on
        top of that feedback must still be blocked, or the concern is
        silently dropped forever."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=True)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        real_reply = fake.reply_to_review_comment

        def _reply_then_inject_human_followup(**kw):
            result = real_reply(**kw)
            # Simulate a reviewer's follow-up landing in the window between
            # the reply being posted and the resolve call that follows it.
            human_followup = _comment(5, body="wait, this is still a problem")
            fake.review_comments = [*fake.review_comments, human_followup]
            fake.threads = [
                replace(t, comment_ids=(*t.comment_ids, 5)) if t.id == "T2" else t
                for t in fake.threads
            ]
            return result

        fake.reply_to_review_comment = _reply_then_inject_human_followup  # type: ignore[assignment]

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
        assert len(fake.replies) == 1  # the reply was already posted (best-effort)
        assert fake.resolved == []  # but never resolved over the new feedback

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

    def test_posted_reply_embeds_accounted_through_marker_for_highest_history_id(
        self, address_env, monkeypatch
    ) -> None:
        """Every reply this flow posts must carry `_accounted_through_marker`
        for the highest comment id in the thread history it was actually
        generated from, so a later run can tell a genuinely clean
        resolve-only retry apart from a reviewer follow-up that slipped in
        before the reply was generated but got assigned a lower id than the
        reply itself (see `_unresolved_comments`'s own regression test)."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=True)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 6))
        history = [_comment(2), _comment(6, body="also this")]
        fake.review_comments = list(history)
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(6, body="also this"),
            thread,
            history,
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "false_positive"
        assert len(fake.replies) == 1
        assert ac._parse_accounted_through(fake.replies[0][1]) == 6

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

    def test_reply_and_resolve_records_ledger_entry_when_resolve_fails(
        self, address_env, monkeypatch
    ) -> None:
        """When `resolve_review_thread` fails, `_reply_and_resolve` must record
        the failure in the resolve-attempt ledger, keyed by the reply comment's
        own id — this is the ONLY evidence a later run's `_unresolved_comments`
        will trust to authorize a resolve-only retry rather than treating the
        still-unresolved thread as an ambiguous reviewer reopen."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=True)
        fake.resolve_result = False
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]
        recorded: list[tuple] = []
        monkeypatch.setattr(
            address_env["main"],
            "record_resolve_failure",
            lambda owner, repo, pr_number, thread_id, reply_id: recorded.append(
                (owner, repo, pr_number, thread_id, reply_id)
            ),
        )
        cleared: list[tuple] = []
        monkeypatch.setattr(
            address_env["main"],
            "clear_resolve_attempt",
            lambda owner, repo, pr_number, thread_id: cleared.append(
                (owner, repo, pr_number, thread_id)
            ),
        )
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))

        ac._handle_comment(
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

        assert len(recorded) == 1
        assert recorded[0][:4] == (req.owner, req.repo, req.pr_number, "T2")
        # The reply landed (see `fake.replies`) before the resolve failed — the
        # recorded id is the newly-created reply comment's own id, the same id
        # a later run's `_unresolved_comments` would see as the thread's
        # LATEST message.
        assert fake.replies
        assert recorded[0][4] is not None
        assert cleared == []

    def test_reply_and_resolve_clears_ledger_entry_when_resolve_succeeds(
        self, address_env, monkeypatch
    ) -> None:
        """A successful resolve must clear any stale ledger entry for the
        thread — otherwise a LATER, genuine reviewer reopen of the same
        thread could be mistaken for leftover evidence of this now-resolved
        attempt."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=True)
        fake.resolve_result = True
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]
        recorded: list[tuple] = []
        cleared: list[tuple] = []
        monkeypatch.setattr(
            address_env["main"],
            "record_resolve_failure",
            lambda *a, **kw: recorded.append(a),
        )
        monkeypatch.setattr(
            address_env["main"],
            "clear_resolve_attempt",
            lambda *a, **kw: cleared.append(a),
        )
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))

        ac._handle_comment(
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

        assert cleared == [(req.owner, req.repo, req.pr_number, "T2")]
        assert recorded == []

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
        # "A" (sum 30) must rank ahead of "B" (sum 20) and "C" (sum 16) — and
        # no candidate must be dropped or an extra one injected along the way.
        assert [c.summary for c in plan.candidate_solutions] == ["A", "B", "C"]

    def test_plan_rejected_when_chosen_index_disagrees_with_top_score(
        self, address_env, monkeypatch
    ) -> None:
        """chosen_plan is free text the model writes independently of
        candidate_solutions — nothing in the schema enforces it actually
        describes chosen_candidate_index, let alone the top-scoring
        candidate. Since _dispatch_implementation acts on chosen_plan alone,
        a mismatch here is treated as a fail-closed planning failure (None)
        rather than dispatching a chosen_plan nothing here verified
        implements the best-scoring candidate."""
        ac = address_env["ac"]
        from software_engineering_team.api import coding_team_main as _main

        def _gen(prompt, *, schema, **kw):
            if schema is ac.CommentTriage:
                return ac.CommentTriage(raises_issue=True, is_false_positive=False, issue_summary="s")
            return ac.IssueResolutionPlan(
                requirements=["r1"],
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
                    ac.SolutionCandidate(
                        summary="C",
                        requirement_fit=4,
                        computational_performance=4,
                        memory_usage=4,
                        code_complexity=4,
                    ),
                ],
                chosen_candidate_index=1,  # names "B" even though "A" scores highest
                chosen_plan="do B's thing",
            )

        monkeypatch.setattr(_main, "generate_structured", _gen)

        plan = ac._plan_resolution(_comment(2), "code", [_comment(2)])

        assert plan is None

    def test_plan_rejected_when_candidate_count_is_not_three(
        self, address_env, monkeypatch
    ) -> None:
        """The "always implement the best-scoring of three candidates"
        invariant requires exactly three scored candidates to compare — a
        short (or long) candidate list can't be trusted to have surfaced the
        actual best option, so this fails closed rather than dispatching."""
        ac = address_env["ac"]
        from software_engineering_team.api import coding_team_main as _main

        def _gen(prompt, *, schema, **kw):
            if schema is ac.CommentTriage:
                return ac.CommentTriage(raises_issue=True, is_false_positive=False, issue_summary="s")
            return ac.IssueResolutionPlan(
                requirements=["r1"],
                candidate_solutions=[
                    ac.SolutionCandidate(
                        summary="A",
                        requirement_fit=9,
                        computational_performance=8,
                        memory_usage=7,
                        code_complexity=6,
                    ),
                ],
                chosen_candidate_index=0,
                chosen_plan="do A's thing",
            )

        monkeypatch.setattr(_main, "generate_structured", _gen)

        plan = ac._plan_resolution(_comment(2), "code", [_comment(2)])

        assert plan is None


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

        # A unique SHA per call, rather than a small fixed list: the run now
        # makes several get_pull_request calls beyond the one this test cares
        # about (the waiting-for-review label check, the head-freshness
        # re-checks around triage/planning), and pinning an exact call count
        # here would make the test fragile to those unrelated additions.
        call_count = {"n": 0}

        def _get_pr(_o: str, _r: str, _n: int) -> PullRequestDetail:
            call_count["n"] += 1
            return replace(fake.pr, head_sha=f"sha{call_count['n']}")

        monkeypatch.setattr(fake, "get_pull_request", _get_pr)

        refs_used: list[str] = []

        def _get_file_contents_detailed(_o: str, _r: str, _p: str, ref: str) -> tuple[str, bool]:
            refs_used.append(ref)
            return fake.file_contents, False

        monkeypatch.setattr(fake, "get_file_contents_detailed", _get_file_contents_detailed)

        ac._run_address_comments("job1", req, "tok")

        # Each comment's cited-code read used its own freshly-refreshed SHA —
        # never the original "sha1" captured on the PR fixture, and never the
        # same SHA reused across both comments.
        assert len(refs_used) == 2
        assert "sha1" not in refs_used
        assert refs_used[0] != refs_used[1]

    def test_skips_all_work_when_job_already_cancelled(self, address_env, monkeypatch) -> None:
        """An operator can cancel this job through the normal job APIs (e.g.
        POST /api/jobs/{team}/{job_id}/cancel) at any point. If already
        cancelled before this run does anything, no comment should be
        dispatched, no thread resolved, and the terminal status must not be
        overwritten with a completion status."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]
        address_env["job_state"]["status"] = "cancelled"

        ac._run_address_comments("job1", req, "tok")

        assert address_env["child_jobs"] == []  # no implementation ever dispatched
        assert fake.replies == []
        assert fake.resolved == []
        assert fake.labels_set == []
        # The cancelled status must not be overwritten with a completion status.
        assert address_env["job_updates"] == []

    def test_stops_mid_loop_when_job_is_cancelled_between_comments(
        self, address_env, monkeypatch
    ) -> None:
        """Cancellation arriving BETWEEN comments (not before the run starts)
        must also stop further dispatch — comment 5 must never be reached
        once cancellation is detected after comment 2 completes."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)
        fake.review_comments = [_comment(2), _comment(5)]
        fake.threads = [
            ReviewThread(id="T2", is_resolved=False, comment_ids=(2,)),
            ReviewThread(id="T5", is_resolved=False, comment_ids=(5,)),
        ]

        # Call 1: the pre-run check (not yet cancelled, so the run starts
        # normally). Call 2: the pre-retry-section check — not yet
        # cancelled. Call 3: the per-iteration check before comment 2 — not
        # yet cancelled, so comment 2 is handled normally. Call 4: the
        # per-iteration check before comment 5 — cancelled by now, so
        # comment 5 is never reached.
        calls = {"n": 0}

        def _get_job_sequenced(_job_id):
            calls["n"] += 1
            if calls["n"] >= 4:
                return {"status": "cancelled"}
            return {"status": "running"}

        monkeypatch.setattr(address_env["main"], "get_job", _get_job_sequenced)

        ac._run_address_comments("job1", req, "tok")

        # Comment 2 was handled (triaged as not_an_issue) — the run's own
        # initial "running" marker was written, but no TERMINAL status was:
        # the job is cancelled, so its status is left as-is rather than
        # overwritten with a completion status.
        assert not any(u.get("status") in ("completed", "completed_with_failures") for u in address_env["job_updates"])
        assert fake.labels_set == []

    def test_stops_processing_when_a_comment_leaves_unpublished_work(
        self, address_env, monkeypatch
    ) -> None:
        """When a comment's implementation is dispatched but the workflow
        doesn't complete cleanly, a child job/workflow may still have
        committed partial work to the shared `development` branch. Every
        comment of this PR shares the SAME `khala.active-issue` marker, so a
        LATER comment's branch preparation would otherwise treat that
        leftover state as same-work continuation and could publish it
        alongside its own fix. The loop must stop rather than risk that."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        fake.review_comments = [_comment(2), _comment(5)]
        fake.threads = [
            ReviewThread(id="T2", is_resolved=False, comment_ids=(2,)),
            ReviewThread(id="T5", is_resolved=False, comment_ids=(5,)),
        ]
        monkeypatch.setattr(
            address_env["main"],
            "execute_coding_team_workflow",
            lambda *a, **kw: {"status": "completed_with_failures"},
        )

        ac._run_address_comments("job1", req, "tok")

        final = [u for u in address_env["job_updates"] if u.get("status") == "completed_with_failures"]
        assert final
        # Only comment 2 was ever attempted; comment 5 was never reached.
        assert final[-1]["review_summary"]["counts"].get("failed") == 1
        assert final[-1]["review_summary"]["total_comments"] == 1
        assert fake.labels_set == []
        # Comment 2's own reply/resolve was skipped too — its thread stays
        # open for the next run rather than getting a reply that implies
        # completion.
        assert fake.replies == []
        assert fake.resolved == []

    def test_ambiguous_thread_blocks_run_from_reporting_full_success(
        self, address_env
    ) -> None:
        """P1 regression: a thread whose latest message is Khala's own reply,
        with no persisted resolve-failure evidence (an "ambiguous" possible
        reviewer reopen — see `test_khala_marker_reply_with_no_recorded_
        failure_is_ambiguous_and_skipped`), must keep blocking the run's
        completion check. Before the fix, such a thread appeared in neither
        `unresolved` nor `retry_resolve_threads`, so both the initial and
        final-re-list completion checks saw nothing left owed and the run
        reported full success (`status="completed"`) — silently burying a
        reviewer's reopened conversation."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _khala_reply(ac, fake, 3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]

        ac._run_address_comments("job1", req, "tok")

        # Never resolved (correctly) — but also never reported "completed":
        # the ambiguous thread is still genuinely unresolved on GitHub.
        assert fake.resolved == []
        final = [
            u
            for u in address_env["job_updates"]
            if u.get("status") in ("completed", "completed_with_failures")
        ]
        assert final
        assert final[-1]["status"] == "completed_with_failures"
        assert fake.labels_set == []

    def test_stops_processing_when_pr_closes_mid_run(self, address_env, monkeypatch) -> None:
        """If the PR is merged/closed by someone else while an earlier
        comment's workflow was still running, the loop must stop rather than
        keep dispatching workflows (which would push commits, reply,
        resolve, and label a PR that is no longer accepting any of that) —
        and the run must never be mistaken for a fully successful one."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)
        fake.review_comments = [_comment(2), _comment(5)]
        fake.threads = [
            ReviewThread(id="T2", is_resolved=False, comment_ids=(2,)),
            ReviewThread(id="T5", is_resolved=False, comment_ids=(5,)),
        ]

        calls = {"n": 0}
        real_pr = fake.pr

        def _get_pull_request(_o, _r, _n):
            calls["n"] += 1
            # The pre-loop fetch, _clear_waiting_for_review's own fetch,
            # comment 2's own per-iteration refresh, and comment 2's own
            # post-triage staleness re-check (inside _handle_comment) all
            # see it open, so comment 2 succeeds normally; comment 5's
            # per-iteration refresh discovers it was closed in the meantime.
            if calls["n"] <= 4:
                return real_pr
            return replace(real_pr, state="closed")

        fake.get_pull_request = _get_pull_request  # type: ignore[assignment]

        ac._run_address_comments("job1", req, "tok")

        # Terminal status must say "completed_with_failures", not "completed"
        # — a caller polling job status needs to see that another run is needed.
        final = [u for u in address_env["job_updates"] if u.get("status") == "completed_with_failures"]
        assert final
        # Only comment 2 was ever handled; comment 5 was never attempted.
        assert final[-1]["review_summary"]["counts"].get("not_an_issue") == 1
        assert final[-1]["review_summary"]["total_comments"] == 1
        # The run must not be mistaken for fully successful — no waiting-for-review label.
        assert fake.labels_set == []

    def test_does_not_mark_waiting_for_review_when_pr_closes_during_last_comment(
        self, address_env, monkeypatch
    ) -> None:
        """The per-iteration PR-state check (see
        test_stops_processing_when_pr_closes_mid_run) only catches a closure
        BETWEEN comments — it can't see the PR close WHILE the final (or, as
        here, only) comment's own `_handle_comment` call is still running,
        since there's no next iteration to observe it. A post-loop re-check
        must catch this too, or a run whose last comment happened to succeed
        would still get labelled waiting-for-review on an already-closed
        PR."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]

        calls = {"n": 0}
        real_pr = fake.pr

        def _get_pull_request(_o, _r, _n):
            calls["n"] += 1
            # Pre-loop fetch, _clear_waiting_for_review's fetch, the only
            # comment's own per-iteration refresh, and its post-triage
            # staleness re-check (inside _handle_comment) all see it open
            # — the PR closes only while that comment's _handle_comment is
            # "running" beyond that point, discovered by the post-loop
            # re-check (the 5th call).
            if calls["n"] <= 4:
                return real_pr
            return replace(real_pr, state="closed")

        fake.get_pull_request = _get_pull_request  # type: ignore[assignment]

        ac._run_address_comments("job1", req, "tok")

        # Terminal status must say "completed_with_failures", not "completed".
        final = [u for u in address_env["job_updates"] if u.get("status") == "completed_with_failures"]
        assert final
        # The comment itself was handled normally...
        assert final[-1]["review_summary"]["counts"].get("not_an_issue") == 1
        # ...but the run must not be mistaken for fully successful.
        assert fake.labels_set == []

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
            _khala_reply(ac, fake, 3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]
        monkeypatch.setattr(address_env["main"], "has_recorded_resolve_failure", lambda *a, **kw: True)

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

    def test_retry_resolve_failure_is_recorded_in_ledger(self, address_env, monkeypatch) -> None:
        """When the resolve-only retry's own `resolve_review_thread` call
        fails again, that failure must be (re-)recorded in the ledger — the
        SAME evidence a future run's `_unresolved_comments` needs to keep
        authorizing the retry, rather than the thread silently reverting to
        "ambiguous" the moment persisted evidence would otherwise expire or
        be cleared elsewhere."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _khala_reply(ac, fake, 3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]
        fake.resolve_result = False
        monkeypatch.setattr(address_env["main"], "has_recorded_resolve_failure", lambda *a, **kw: True)
        recorded: list[tuple] = []
        monkeypatch.setattr(
            address_env["main"],
            "record_resolve_failure",
            lambda *a, **kw: recorded.append(a),
        )

        ac._run_address_comments("job1", req, "tok")

        assert fake.resolved == ["T2"]  # the retry was attempted...
        assert recorded == [("o", "r", 7, "T2", 3)]  # ...and its failure recorded
        assert fake.labels_set == []  # never mislabelled ready when the retry itself failed

    def test_retry_resolve_blocks_on_edit_to_earlier_history_message(
        self, address_env, monkeypatch
    ) -> None:
        """A resolve-only retry's freshness check must catch a reviewer
        editing an EARLIER message in the thread (root comment 2), not just
        a new comment appearing — an id-only ">" comparison against the
        Khala reply's own id can never see this, since any message that
        predates the reply always has a lower id."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        original_root = _comment(2, body="please fix this")
        khala_reply = _khala_reply(ac, fake, 3)
        edited_root = _comment(2, body="actually, use a completely different approach")

        calls = {"n": 0}

        def _list_review_comments(_o, _r, _n):
            calls["n"] += 1
            # First call: _unresolved_comments' initial snapshot (unedited).
            # Every call after: the live re-check right before resolving
            # sees the reviewer's edit to the root comment.
            if calls["n"] == 1:
                return [original_root, khala_reply]
            return [edited_root, khala_reply]

        fake.list_review_comments = _list_review_comments  # type: ignore[assignment]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]
        monkeypatch.setattr(address_env["main"], "has_recorded_resolve_failure", lambda *a, **kw: True)

        ac._run_address_comments("job1", req, "tok")

        assert fake.resolved == []  # blocked — never resolved over the edit
        assert fake.labels_set == []  # not moved to waiting-for-review

    def test_run_wraps_body_in_liveness_heartbeat(self, address_env, monkeypatch) -> None:
        """_run_address_comments must hold a continuous heartbeat for the job while
        it runs — a single comment's implementation can now block for hours (see
        execute_coding_team_workflow's reattach_on_timeout) — mirroring
        _run_pr_review's review_hb, asserted via a recording stand-in."""
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

        monkeypatch.setattr(ac, "BackgroundHeartbeat", _RecordingHB)

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
        # The label is created (idempotently) before it is applied, so a repo
        # that has never defined "waiting for review" doesn't silently no-op.
        assert ac.WAITING_FOR_REVIEW_LABEL in fake.labels_created

    def test_mark_waiting_for_review_creates_label_before_applying_it(
        self, address_env
    ) -> None:
        """GitHub rejects (422) applying a label name the repo has never
        defined. _mark_waiting_for_review must create the label first
        (idempotently) so the very first run on a fresh repo doesn't
        silently fail to apply it."""
        ac, fake = address_env["ac"], address_env["fake"]

        ac._mark_waiting_for_review(fake, "o", "r", 7)

        assert fake.labels_created == [ac.WAITING_FOR_REVIEW_LABEL]
        assert fake.labels_set and ac.WAITING_FOR_REVIEW_LABEL in fake.labels_set[-1]

    def test_mark_waiting_for_review_still_applies_label_when_create_fails(
        self, address_env
    ) -> None:
        """Label creation is itself best-effort: a failure there (e.g. the
        label already exists under a client that doesn't recognize this as
        idempotent, or a transient error) must not prevent the apply attempt
        that follows — the label may already exist."""
        ac, fake = address_env["ac"], address_env["fake"]

        def _create_label(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("boom")

        fake.create_label = _create_label  # type: ignore[assignment]

        ac._mark_waiting_for_review(fake, "o", "r", 7)

        assert fake.labels_set and ac.WAITING_FOR_REVIEW_LABEL in fake.labels_set[-1]

    def test_clears_stale_waiting_for_review_label_when_new_work_starts(
        self, address_env, monkeypatch
    ) -> None:
        """A previous successful run can leave WAITING_FOR_REVIEW_LABEL on the
        PR. When THIS run finds genuinely new unresolved feedback, that stale
        label must be cleared up front rather than kept until this run's own
        (possibly failing, possibly long-running) outcome is known."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        fake.review_comments = [_comment(2)]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))]
        fake.pr = replace(fake.pr, labels=("bug", ac.WAITING_FOR_REVIEW_LABEL))

        ac._run_address_comments("job1", req, "tok")

        # First label write clears the stale label before work begins...
        assert ac.WAITING_FOR_REVIEW_LABEL not in fake.labels_set[0]
        assert "bug" in fake.labels_set[0]
        # ...and the final write re-adds it once this run's own work succeeds.
        assert ac.WAITING_FOR_REVIEW_LABEL in fake.labels_set[-1]

    def test_marks_waiting_for_review_on_retry_only_success(self, address_env, monkeypatch) -> None:
        """A run consisting SOLELY of successful resolve-only retries (no fresh
        unresolved comments to triage) still did real work — the resolve
        mutation that a previous run's reply left pending — and must be
        labelled waiting-for-review too, not just a run with fresh `outcomes`."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        # Latest message in the thread is already Khala's own reply, so this
        # routes entirely through retry_resolve_thread_ids; `unresolved`/
        # `outcomes` stay empty. Persisted evidence confirms our own resolve
        # for this reply is on record as having failed, authorizing the retry.
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _khala_reply(ac, fake, 3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]
        monkeypatch.setattr(address_env["main"], "has_recorded_resolve_failure", lambda *a, **kw: True)

        ac._run_address_comments("job1", req, "tok")

        assert fake.resolved == ["T2"]
        assert fake.labels_set and ac.WAITING_FOR_REVIEW_LABEL in fake.labels_set[-1]

    def test_retry_only_skips_resolve_when_pr_already_closed(self, address_env, monkeypatch) -> None:
        """The PR can already be closed by the time the resolve-only retry
        loop runs — this run's own pre-loop snapshot can be stale by then,
        since `_unresolved_comments` above can take a while. Resolving a
        thread (or, later, labelling the PR waiting-for-review) on a closed
        PR would be acting on state it no longer accepts. A retry-only run
        (no fresh `unresolved` comments) has no `_handle_comment` call to
        catch this via — the per-iteration/post-triage checks only exist
        inside that loop — so the resolve-only retries need their own
        check."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _khala_reply(ac, fake, 3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]
        monkeypatch.setattr(address_env["main"], "has_recorded_resolve_failure", lambda *a, **kw: True)

        calls = {"n": 0}
        real_pr = fake.pr

        def _get_pull_request(_o, _r, _n):
            calls["n"] += 1
            # Call 1: the pre-loop snapshot (still open). Call 2: the
            # resolve-only retries' own PR-state check, which discovers it
            # closed and must skip resolving rather than proceed.
            if calls["n"] <= 1:
                return real_pr
            return replace(real_pr, state="closed")

        fake.get_pull_request = _get_pull_request  # type: ignore[assignment]

        ac._run_address_comments("job1", req, "tok")

        assert fake.resolved == []  # never resolved on the closed PR
        assert fake.labels_set == []

    def test_retry_only_skips_label_when_pr_closes_after_resolving(
        self, address_env, monkeypatch
    ) -> None:
        """The PR can also close AFTER the resolve-only retries' own
        PR-state check runs (and the retry succeeds) but BEFORE the run is
        labelled waiting-for-review — the post-loop re-check must catch that
        gap too, or a closed PR would get mislabelled ready for review."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        fake.review_comments = [
            _comment(2, body="please fix this"),
            _khala_reply(ac, fake, 3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]
        monkeypatch.setattr(address_env["main"], "has_recorded_resolve_failure", lambda *a, **kw: True)

        calls = {"n": 0}
        real_pr = fake.pr

        def _get_pull_request(_o, _r, _n):
            calls["n"] += 1
            # Calls 1-2 (pre-loop snapshot, retry-loop's own state check) see
            # it open, so the retry proceeds and resolves. Call 3 (the
            # post-loop re-check) discovers it closed since.
            if calls["n"] <= 2:
                return real_pr
            return replace(real_pr, state="closed")

        fake.get_pull_request = _get_pull_request  # type: ignore[assignment]

        ac._run_address_comments("job1", req, "tok")

        assert fake.resolved == ["T2"]  # the retry itself succeeded
        assert fake.labels_set == []  # but never mislabelled on the now-closed PR

    def test_retry_resolve_revalidates_freshness_before_resolving(self, address_env, monkeypatch) -> None:
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
            _khala_reply(ac, fake, 3),
        ]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3, 4))]
        monkeypatch.setattr(address_env["main"], "has_recorded_resolve_failure", lambda *a, **kw: True)

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
            unresolved, by_comment, retry, history, ambiguous = result
            by_comment = {**by_comment, 9: ReviewThread(id="T9", is_resolved=False, comment_ids=(9,))}
            history = {**history, 9: [new_thread_comment]}
            return [*unresolved, new_thread_comment], by_comment, retry, history, ambiguous

        monkeypatch.setattr(ac, "_unresolved_comments", _stub)

        ac._run_address_comments("job1", req, "tok")

        assert calls["n"] >= 2  # at least the initial snapshot and a final re-list
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

    def test_relist_still_blocks_when_an_earlier_history_message_is_edited(
        self, address_env, monkeypatch
    ) -> None:
        """The not_an_issue verdict can be grounded on the thread's FULL
        history, not just its latest message. A reviewer editing an EARLIER
        message — while this run's triage was in flight — changes that
        context without touching the latest message's id or body at all, so
        comparing bodies alone would wrongly treat the thread as unchanged
        and let a stale verdict block a re-triage."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=False, is_false_positive=False)
        root = _comment(2, body="is this actually a problem?")
        follow_up = _comment(3, body="just a question")
        fake.review_comments = [root, follow_up]
        fake.threads = [ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))]

        real_unresolved_comments = ac._unresolved_comments
        calls = {"n": 0}

        def _stub(client, owner, repo, pr_number):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_unresolved_comments(client, owner, repo, pr_number)
            # The final re-list sees the SAME latest comment (id 3, unchanged
            # body), but the thread's root message was edited in the window
            # since triage ran.
            edited_root = _comment(2, body="wait, this needs a null check")
            thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 3))
            return [follow_up], {2: thread, 3: thread}, [], {3: [edited_root, follow_up]}, []

        monkeypatch.setattr(ac, "_unresolved_comments", _stub)

        ac._run_address_comments("job1", req, "tok")

        assert fake.labels_set == []  # the edited earlier message still blocks success

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
            return [edited], {2: thread}, [], {2: [edited]}, []

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

    def test_dispatch_time_freshness_check_blocks_unfetched_newer_comment(
        self, address_env, monkeypatch
    ) -> None:
        """The thread's live `comment_ids` can report a comment id newer than
        the triaged snapshot that the REST listing didn't return (e.g.
        list_review_comments' traversal cap truncated before reaching it).
        Its content is unverifiable — this must fail closed (block dispatch)
        rather than silently treat an unfetched comment as if it doesn't
        exist."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        # The live thread now has a newer comment (id 6) that never showed up
        # in fake.review_comments — simulating a REST-listing truncation.
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 6))
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

        assert outcome.outcome == "failed"
        assert address_env["child_jobs"] == []  # implementation never dispatched
        assert fake.replies == []
        assert fake.resolved == []

    def test_thread_freshness_check_fetches_comments_before_thread_membership(
        self, address_env
    ) -> None:
        """A reviewer can post a reply between the two live re-fetches this
        check makes. If thread membership were fetched FIRST and comment
        bodies SECOND, a reply landing in that gap would show up in the
        (later) comment listing but be absent from the (earlier) membership
        snapshot's `comment_ids` — the loop would never examine it and
        silently return False. Fetching comments first means any such gap
        reply is instead missing from the (earlier) comment listing while
        present in the (later) membership snapshot, which the existing
        unfetched-id check already fails closed on (True)."""
        ac, fake = address_env["ac"], address_env["fake"]
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        original_list_review_comments = fake.list_review_comments

        def racy_list_review_comments(o: str, r: str, n: int) -> list[ReviewComment]:
            result = original_list_review_comments(o, r, n)
            # A reviewer's reply lands right after comments were read but
            # before thread membership is re-fetched.
            fake.threads = [replace(thread, comment_ids=(2, 99))]
            return result

        fake.list_review_comments = racy_list_review_comments

        assert (
            ac._thread_has_new_reviewer_feedback(fake, "o", "r", 7, "T2", since_comment_id=2) is True
        )

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

    def test_dispatch_time_freshness_check_blocks_deleted_history_message(
        self, address_env, monkeypatch
    ) -> None:
        """A reviewer can delete an EARLIER message in the triaged history
        while leaving the thread and the representative comment intact — the
        live comment set then has no entry at all for that id. This must
        still block, same as an edit would: the plan/verdict was grounded on
        context the reviewer withdrew."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2, 4))
        root_snapshot = _comment(2, body="please fix the null check")
        latest = _comment(4, body="still broken")
        # The LIVE comment set no longer has comment 2 at all (deleted).
        fake.review_comments = [latest]
        fake.threads = [thread]

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            latest,
            thread,
            [root_snapshot, latest],
            "feature",
            "sha1",
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "failed"
        assert "superseded" in outcome.detail
        assert address_env["child_jobs"] == []
        assert fake.replies == []
        assert fake.resolved == []

    def test_stale_pr_head_after_triage_blocks_acting_on_the_verdict(
        self, address_env, monkeypatch
    ) -> None:
        """The PR author can push a new commit while triage's LLM call is in
        flight, after `pr_head_sha` was captured for this comment. Acting on
        a verdict grounded on the now-stale code — resolving a false positive,
        or dispatching a plan — must be skipped so the next run re-triages
        against the current head."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=True)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]
        # The LIVE PR head has moved past what was passed as pr_head_sha.
        fake.pr = replace(fake.pr, head_sha="sha2")

        outcome = ac._handle_comment(
            fake,
            "parent",
            req,
            _comment(2),
            thread,
            [_comment(2)],
            "feature",
            "sha1",  # the (now stale) SHA triage's cited_code was read at
            "main",
            fake.pr.html_url,
            "origin",
            "tok",
        )

        assert outcome.outcome == "failed"
        assert "sha1" in outcome.detail and "sha2" in outcome.detail
        assert fake.replies == []
        assert fake.resolved == []

    def test_pr_closed_after_triage_without_head_change_blocks_false_positive_resolve(
        self, address_env, monkeypatch
    ) -> None:
        """A PR can be merged (as-is) or closed without any new commit
        landing — the head SHA GitHub reports never changes. A staleness
        check that only compares head SHA would miss this entirely and let
        the false-positive path reply to and resolve a conversation on a
        PR no longer open. Live `state` must be checked too, not just
        `head_sha`."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=True)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]
        # Closed, but the SAME head_sha as what was passed in (sha1) — no new commit.
        fake.pr = replace(fake.pr, state="closed", head_sha="sha1")

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
        assert "no longer open" in outcome.detail
        assert fake.replies == []
        assert fake.resolved == []

    def test_stale_pr_head_after_planning_blocks_dispatch(self, address_env, monkeypatch) -> None:
        """A push can land during planning's own LLM round-trip too, not just
        during triage. The post-triage freshness check alone would miss this
        window and dispatch an implementation job grounded on stale code."""
        ac, fake, req = address_env["ac"], address_env["fake"], address_env["request"]
        _stub_triage(monkeypatch, ac, raises_issue=True, is_false_positive=False)
        thread = ReviewThread(id="T2", is_resolved=False, comment_ids=(2,))
        fake.review_comments = [_comment(2)]
        fake.threads = [thread]

        calls = {"n": 0}
        real_pr = fake.pr

        def _get_pull_request(_o: str, _r: str, _n: int):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_pr  # post-triage check: head unchanged
            return replace(real_pr, head_sha="sha2")  # post-planning check: head moved

        fake.get_pull_request = _get_pull_request  # type: ignore[assignment]

        dispatched = {"called": False}
        monkeypatch.setattr(
            ac,
            "_dispatch_implementation",
            lambda *a, **kw: dispatched.__setitem__("called", True) or "child-job",
        )

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
        assert "sha1" in outcome.detail and "sha2" in outcome.detail
        assert "planned" in outcome.detail or "planning" in outcome.detail
        assert dispatched["called"] is False
        assert fake.replies == []
        assert fake.resolved == []

    def test_triage_prompt_includes_full_thread_history(self, address_env, monkeypatch) -> None:
        """A short context-dependent follow-up ("still broken") is unintelligible
        in isolation — triage must ground on the thread's full conversation
        (root concern + any earlier reply), not just the latest message, so
        the LLM can tell what "still" refers to."""
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

    def test_planning_prompt_includes_full_thread_history(self, address_env, monkeypatch) -> None:
        """Same as triage: planning is a SEPARATE LLM round-trip, run only on
        the real-issue path, and it too must ground on the thread's full
        conversation — not just the comment it was handed — or a plan for a
        context-dependent follow-up would be built without the context that
        makes it intelligible."""
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
                return ac.CommentTriage(raises_issue=True, is_false_positive=False, issue_summary="s")
            return ac.IssueResolutionPlan(chosen_plan="p")

        monkeypatch.setattr(ac._main, "generate_structured", _gen)
        monkeypatch.setattr(ac, "_dispatch_implementation", lambda *a, **kw: "child-job")

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

        assert len(seen_prompts) == 2  # triage, then planning
        for prompt in seen_prompts:
            assert "This null-check is missing entirely" in prompt
            assert "still broken" in prompt

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
        # Terminal status must say "completed_with_failures", not "completed"
        # — a caller polling job status needs to see that work is still owed.
        final = [u for u in address_env["job_updates"] if u.get("status") == "completed_with_failures"]
        assert final and final[-1]["review_summary"]["counts"]["failed"] == 1
        assert not any(u.get("status") == "completed" for u in address_env["job_updates"])

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

        # The route now validates repo_path is an actual git checkout whose
        # origin remote matches owner/repo before admitting a job — give
        # tmp_path a real (if minimal) git repo with a matching origin so
        # every route test below that doesn't specifically exercise that
        # validation keeps simulating a real, usable checkout.
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/o/r.git"],
            check=True,
        )

        return {
            "client": TestClient(_main.app),
            "fake": fake,
            "started": started,
            "repo_path": str(tmp_path),
            "jobs": fake_jobs,
        }

    def test_starts_job_and_reports_unresolved_count(self, route_env) -> None:
        """The happy path: the route creates a job, persists the token
        ciphertext, launches the background hook, and reports the PR's
        unresolved-comment count in its response."""
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

    def test_400_when_repo_path_blank_and_comments_unresolved(self, route_env) -> None:
        """`repo_path` defaults to "" (documented as accepted "for parity with
        /review-pr", which genuinely never touches a checkout), but a real
        issue among the PR's unresolved comments needs a checkout to
        implement and push a fix. An empty path would otherwise canonicalize
        to the service's own working directory rather than failing loudly,
        admitting a job that reports "started" but cannot implement any real
        fix it triages. Must be rejected up front instead."""
        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": "", "pr_number": 7},
        )

        assert resp.status_code == 400
        assert "repo_path is required" in resp.json()["detail"]
        assert route_env["started"] == []  # never launched

    def test_400_when_repo_path_blank_even_with_nothing_unresolved_yet(self, route_env) -> None:
        """repo_path is required UNCONDITIONALLY, even when THIS admission-time
        snapshot has nothing unresolved: the background worker takes its own,
        LATER snapshot, and a reviewer can post genuinely new feedback in the
        gap between the two. A job admitted with no repo_path because nothing
        was unresolved yet would then discover a real issue it can't
        implement a fix for."""
        route_env["fake"].review_comments = []
        route_env["fake"].threads = []

        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": "", "pr_number": 7},
        )

        assert resp.status_code == 400
        assert "repo_path is required" in resp.json()["detail"]
        assert route_env["started"] == []

    def test_400_when_repo_path_origin_does_not_match_owner_repo(
        self, route_env, tmp_path
    ) -> None:
        """A repo_path that IS a git checkout but of a DIFFERENT repository
        must be rejected — without this, this PR's remediation plan could get
        committed and pushed to that unrelated repo's origin if the token
        happens to have access to it."""
        wrong_repo = tmp_path / "wrong-repo"
        subprocess.run(["git", "init", "-q", str(wrong_repo)], check=True)
        subprocess.run(
            ["git", "-C", str(wrong_repo), "remote", "add", "origin", "https://github.com/other/repo.git"],
            check=True,
        )

        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": str(wrong_repo), "pr_number": 7},
        )

        assert resp.status_code == 400
        assert "does not match" in resp.json()["detail"]
        assert route_env["started"] == []

    def test_400_when_repo_path_is_not_a_git_checkout(self, route_env, tmp_path) -> None:
        """A non-empty repo_path that names a real directory but isn't a git
        checkout (no `.git` entry) is exactly as unusable to a real-issue
        child as an empty one — must be rejected the same way, not admitted
        only to fail later during branch preparation."""
        not_a_checkout = tmp_path / "plain-folder"
        not_a_checkout.mkdir()

        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": str(not_a_checkout), "pr_number": 7},
        )

        assert resp.status_code == 400
        assert "git checkout" in resp.json()["detail"]
        assert route_env["started"] == []

    def test_400_when_repo_path_does_not_exist(self, route_env, tmp_path) -> None:
        """A non-empty repo_path naming a directory that doesn't exist at all
        must be rejected the same way as a non-git one."""
        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={
                "owner": "o",
                "repo": "r",
                "repo_path": str(tmp_path / "does-not-exist"),
                "pr_number": 7,
            },
        )

        assert resp.status_code == 400
        assert "git checkout" in resp.json()["detail"]
        assert route_env["started"] == []

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
        """A GitHub API failure while fetching the PR surfaces as 502, not
        an opaque 500 or a silently-launched job."""

        def _boom(*_a, **_kw):
            raise GitHubAPIError(404, "missing")

        monkeypatch.setattr(route_env["fake"], "get_pull_request", _boom)
        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": route_env["repo_path"], "pr_number": 7},
        )
        assert resp.status_code == 502

    def test_rejects_closed_pr_with_400(self, route_env) -> None:
        """Only OPEN PRs can be addressed — a closed PR is rejected with 400
        and no job is ever launched for it."""
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
        """Unknown/incomplete thread state (the GraphQL listing failing) must
        fail closed as 502, never silently launching a job over unverifiable
        resolved/unresolved state."""

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
        """When the background hook fails to launch after the job row was
        already created, that job must be marked failed rather than left
        stuck in a non-terminal state forever (creation and launch are not
        transactional)."""
        from software_engineering_team.api import address_comments as ac

        def _raise_thread_unavailable(*_a, **_kw):
            raise RuntimeError("thread unavailable")

        monkeypatch.setattr(ac, "start_address_comments_thread", _raise_thread_unavailable)
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

    def test_context_update_failure_terminalizes_created_job(self, route_env, monkeypatch) -> None:
        """P1 regression: a failure in the FIRST `update_job` call (persisting
        github_context/github_token_encrypted, which sits before
        record_review_start/thread-launch) must still terminalize the job —
        not just a failure in record_review_start or the thread launch. Before
        the fix, create_job/update_job sat outside the try/except, so a job
        row could be created with no github_context and never marked failed,
        orphaning it in a non-terminal state forever."""
        from software_engineering_team.api import coding_team_main as _main

        calls = {"n": 0}
        real_update_job = _main.update_job

        def _flaky_update_job(job_id, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("job service unavailable")
            return real_update_job(job_id, **kwargs)

        monkeypatch.setattr(_main, "update_job", _flaky_update_job)

        resp = route_env["client"].post(
            "/pulls/7/address-comments",
            json={"owner": "o", "repo": "r", "repo_path": route_env["repo_path"], "pr_number": 7},
        )

        assert resp.status_code == 500
        jobs = route_env["jobs"].list_jobs()
        assert jobs[-1]["status"] == "failed"
        assert route_env["started"] == []  # never reached the thread launch


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
        """No live job for this PR → `running_job_id: None`."""
        resp = route_env["client"].get(
            "/pulls/7/address-comments/running", params={"owner": "o", "repo": "r"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"running_job_id": None}

    def test_reports_running_job_id(self, route_env, monkeypatch) -> None:
        """A live job for this PR is reported by id, matching the POST
        route's own admission check (`_running_review_for_pr`)."""
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

    @pytest.mark.parametrize(
        "owner,repo,pr_number",
        [("", "r", 7), ("o", "", 7), ("o", "r", 0), ("o", "r", -1)],
        ids=["empty-owner", "empty-repo", "zero-pr", "negative-pr"],
    )
    def test_service_seam_rejects_degenerate_coordinates(self, owner, repo, pr_number) -> None:
        """A degenerate coordinate can never match a stored ``github_context``, so
        without enforcement it would silently return ``None`` -- which every caller
        reads as the load-bearing "no job is running for this PR" and acts on by
        touching that PR's shared checkout. Fail loudly instead, mirroring
        ``get_running_job_on_checkout``'s own precondition enforcement."""
        from software_engineering_team.api import coding_team_main as _main

        with pytest.raises(ValueError):
            _main.get_running_review_for_pr(owner, repo, pr_number)

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

"""Activity-level tests for github_publish_activity.

Covers the three paths the acceptance criteria name: a merged-work publish
(new PR and reused PR), an already-complete no-op, and a partial-failure
PR-annotation run, plus the wrapper's own request validation and each
fast-forward/push/GitHub-API failure path `_publish_merged_work` defines.
Everything `_finish_already_complete`/`_publish_merged_work` touch is reached
through the shared `coding_team_main` module-object alias, so this file
monkeypatches that surface directly rather than driving real git repos or a
real job service.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

import pytest

from software_engineering_team.github_source import GitHubAPIError, PullRequest
from software_engineering_team.tests.conftest import (
    _ensure_real_modules,
    _stub_orchestrator_only,
)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import ``coding_team_main`` fresh, with the real orchestration stack in place."""
    _ensure_real_modules()
    _stub_orchestrator_only(monkeypatch)
    from software_engineering_team.api import coding_team_main as api_main

    return api_main


class _FakeGitHubClient:
    """Records calls; returns scripted responses. Stands in for ``GitHubClient``.

    Supports the ``with GitHubClient(...) as client:`` context-manager usage
    the activity performs; entering/exiting does nothing but return ``self``.
    """

    def __init__(self, token: str) -> None:
        assert token, "activity must pass a non-empty token"
        self.token = token
        self.comments: list[tuple[str, str, int, str]] = []
        self.created_prs: list[dict[str, Any]] = []
        self.updated_prs: list[dict[str, Any]] = []
        self.existing_pr: Optional[PullRequest] = None
        self.create_pr_error: Optional[Exception] = None
        self.update_pr_error: Optional[Exception] = None
        self.find_pr_error: Optional[Exception] = None

    def __enter__(self) -> "_FakeGitHubClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def find_existing_pr(self, owner: str, repo: str, head: str) -> Optional[PullRequest]:
        if self.find_pr_error is not None:
            raise self.find_pr_error
        return self.existing_pr

    def create_pull_request(
        self,
        *,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str,
        draft: bool = True,
    ) -> PullRequest:
        if self.create_pr_error is not None:
            raise self.create_pr_error
        self.created_prs.append(
            {
                "owner": owner,
                "repo": repo,
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
            }
        )
        return PullRequest(number=101, html_url="https://example/pull/101", head=head, base=base)

    def update_pull_request(self, *, owner: str, repo: str, number: int, body: str) -> PullRequest:
        if self.update_pr_error is not None:
            raise self.update_pr_error
        self.updated_prs.append({"owner": owner, "repo": repo, "number": number, "body": body})
        return PullRequest(
            number=number, html_url=f"https://example/pull/{number}", head="h", base="b"
        )

    def add_issue_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self.comments.append((owner, repo, number, body))


class _FakeJobStore:
    """In-memory ``get_job``/``update_job`` recorder, seeded with one job.

    ``cleared_markers`` records every ``(repo_path, issue_number)`` pair the
    activity's active-issue-marker cleanup was invoked with, so tests can
    assert the marker was actually cleared rather than just trusting the
    docstring.
    """

    def __init__(self, job_id: str, **fields: Any) -> None:
        self.jobs: dict[str, dict[str, Any]] = {job_id: dict(fields)}
        self.cleared_markers: list[tuple[str, int]] = []

    def get_job(self, job_id: str, cache_dir: Any = None) -> Optional[dict[str, Any]]:
        job = self.jobs.get(job_id)
        return dict(job) if job is not None else None

    def update_job(
        self, job_id: str, cache_dir: Any = None, heartbeat: bool = True, **fields: Any
    ) -> None:
        self.jobs.setdefault(job_id, {}).update(fields)


def _install(
    monkeypatch: pytest.MonkeyPatch, api: Any, job_id: str, **job_fields: Any
) -> tuple[_FakeJobStore, Callable[[], _FakeGitHubClient]]:
    """Wire a fresh fake job store + GitHub client into ``coding_team_main``.

    The second return value is a getter, not a client instance: the activity
    constructs its ``GitHubClient`` only after being called, so callers use
    this to fetch whichever fake client that call produced.
    """
    from cryptography.fernet import Fernet

    from software_engineering_team import token_crypto

    if "github_token_encrypted" not in job_fields:
        monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())
        ct = token_crypto.encrypt_token("tok-123")
        assert ct is not None
        job_fields = {**job_fields, "github_token_encrypted": ct}
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    store = _FakeJobStore(job_id, **job_fields)
    client_holder: list[_FakeGitHubClient] = []

    def _make_client(token: str) -> _FakeGitHubClient:
        client = _FakeGitHubClient(token)
        client_holder.append(client)
        return client

    monkeypatch.setattr(api, "get_job", store.get_job)
    monkeypatch.setattr(api, "update_job", store.update_job)
    monkeypatch.setattr(api, "GitHubClient", _make_client)
    monkeypatch.setattr(api, "_fast_forward", lambda repo_path, branch, base: (True, None))
    monkeypatch.setattr(api, "_push_branch", lambda repo_path, remote, branch, token: (True, None))
    monkeypatch.setattr(
        api,
        "_clear_active_issue_if_matches",
        lambda repo_path, num: store.cleared_markers.append((repo_path, num)),
    )
    monkeypatch.setattr(api, "_cleanup_issue_checkout", lambda repo_path: None)

    # _publish_merged_work/_finish_already_complete/_record_failure all resolve
    # collaborators through this same `_main` module object, so patching it here
    # is visible to them without touching orchestration.py directly.
    def _get_client() -> _FakeGitHubClient:
        assert client_holder, "GitHubClient was never constructed"
        return client_holder[-1]

    return store, _get_client


BASE_REQUEST = {
    "job_id": "job-1",
    "owner": "acme",
    "repo": "widgets",
    "repo_path": "/repo",
    "issue_number": 9,
}

MERGED_WORK_FIELDS = {
    "base": "main",
    "integration_branch": "khala/issue-9",
    "issue_title": "Fix the widget",
}


def _activity():
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_publish_activity,
    )

    return github_publish_activity


@pytest.mark.parametrize(
    "request_overrides,expected_fields,seed_job",
    [
        ({"job_id": None}, ["job_id"], False),
        ({"owner": None, "repo": None}, ["owner", "repo"], True),
    ],
)
def test_publish_activity_missing_base_field_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
    api: Any,
    request_overrides: dict[str, Any],
    expected_fields: list[str],
    seed_job: bool,
) -> None:
    """A missing/falsy base-required field raises before any GitHub side effect,
    naming only the missing field(s) -- never the payload (which may carry a token)."""
    if seed_job:
        _install(monkeypatch, api, "job-1")
    request = {**BASE_REQUEST, **MERGED_WORK_FIELDS, **request_overrides}

    with pytest.raises(ValueError, match="missing") as exc_info:
        _activity()(request)

    msg = str(exc_info.value)
    for field in expected_fields:
        assert field in msg
    assert repr(request) not in msg
    assert "tok-123" not in msg


def test_publish_activity_rejects_plaintext_token_arg(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    _install(monkeypatch, api, "job-1")
    secret = "ghp_leaked"
    request = {**BASE_REQUEST, **MERGED_WORK_FIELDS, "token": secret}
    with pytest.raises(ValueError, match="token") as exc_info:
        _activity()(request)
    assert secret not in str(exc_info.value)


def test_publish_activity_unresolvable_token_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    _install(monkeypatch, api, "job-1", github_token_encrypted="")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(ValueError, match="token"):
        _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS})


def test_publish_activity_merged_work_missing_branch_fields_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """A job that is not already-complete additionally requires base/integration_branch/
    issue_title; the second-stage ValueError fires before any GitHub call and names only
    those fields."""
    _install(monkeypatch, api, "job-1", already_complete=False)
    request = {**BASE_REQUEST}  # no MERGED_WORK_FIELDS

    with pytest.raises(ValueError, match="missing") as exc_info:
        _activity()(request)

    msg = str(exc_info.value)
    for field in ("base", "integration_branch", "issue_title"):
        assert field in msg


def test_publish_activity_merged_work_creates_new_draft_pr(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """No existing PR: a new draft PR is created with a closing reference, the job is
    updated with the PR URL, a "Draft PR opened" comment is posted, the marker is
    cleared, and the job ends `completed`."""
    store, get_client = _install(
        monkeypatch, api, "job-1", already_complete=False, task_graph_snapshot=[]
    )

    out = _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS})

    client = get_client()
    assert client.token == "tok-123"
    assert len(client.created_prs) == 1
    pr = client.created_prs[0]
    assert pr["base"] == "main"
    assert pr["head"] == "khala/issue-9"
    assert pr["body"].startswith("Closes #9")
    assert client.updated_prs == []
    assert any("Draft PR opened" in c[3] for c in client.comments)
    assert store.jobs["job-1"]["github_pr_url"] == "https://example/pull/101"
    assert store.jobs["job-1"]["status"] == "completed"
    assert store.cleared_markers == [("/repo", 9)]
    assert out["status"] == "completed"


def test_publish_activity_merged_work_reuses_existing_draft_pr(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """An open PR already exists for the head branch: its body is refreshed via
    update_pull_request (no create call), and a "Reusing existing draft PR" comment
    is posted."""
    store, get_client = _install(
        monkeypatch, api, "job-1", already_complete=False, task_graph_snapshot=[]
    )
    # Seed the existing PR onto the client the activity will construct: patch
    # GitHubClient again with a factory that pre-populates existing_pr.
    existing = PullRequest(
        number=55, html_url="https://example/pull/55", head="khala/issue-9", base="main"
    )

    def _make_client_with_existing(token: str) -> _FakeGitHubClient:
        client = _FakeGitHubClient(token)
        client.existing_pr = existing
        get_client_holder.append(client)
        return client

    get_client_holder: list[_FakeGitHubClient] = []
    monkeypatch.setattr(api, "GitHubClient", _make_client_with_existing)

    out = _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS})

    client = get_client_holder[-1]
    assert client.token == "tok-123"
    assert client.created_prs == []
    assert len(client.updated_prs) == 1
    assert client.updated_prs[0]["number"] == 55
    assert any("Reusing existing draft PR" in c[3] for c in client.comments)
    assert store.jobs["job-1"]["github_pr_url"] == "https://example/pull/55"
    assert out["status"] == "completed"


def test_publish_activity_partial_failure_annotates_pr_and_marks_completed_with_failures(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """A job with one merged and one failed task: the PR body uses a non-closing
    `Refs` reference plus the failed-task list, an extra warning comment is posted,
    and the terminal status is `completed_with_failures` -- the AC's named
    partial-failure PR-annotation path."""
    task_graph_snapshot = [
        {"id": "t1", "title": "Add widget", "status": "merged"},
        {"id": "t2", "title": "Add gadget", "status": "failed"},
    ]
    store, get_client = _install(
        monkeypatch, api, "job-1", already_complete=False, task_graph_snapshot=task_graph_snapshot
    )

    out = _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS})

    client = get_client()
    assert client.token == "tok-123"
    pr = client.created_prs[0]
    assert pr["body"].startswith("Refs #9")
    assert "t2" in pr["body"]
    assert any("did not complete and were not merged" in c[3] for c in client.comments)
    assert store.jobs["job-1"]["status"] == "completed_with_failures"
    assert out["status"] == "completed_with_failures"


def test_publish_activity_update_pr_failure_is_nonfatal_and_still_completes(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """A reused PR's update_pull_request call raising GitHubAPIError is logged and
    non-fatal: the job still reaches a terminal success status."""
    store, _ = _install(monkeypatch, api, "job-1", already_complete=False, task_graph_snapshot=[])
    existing = PullRequest(
        number=55, html_url="https://example/pull/55", head="khala/issue-9", base="main"
    )
    holder: list[_FakeGitHubClient] = []

    def _make_client(token: str) -> _FakeGitHubClient:
        client = _FakeGitHubClient(token)
        client.existing_pr = existing
        client.update_pr_error = GitHubAPIError(500, "boom")
        holder.append(client)
        return client

    monkeypatch.setattr(api, "GitHubClient", _make_client)

    out = _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS})

    assert len(holder) == 1
    assert holder[-1].token == "tok-123"
    assert store.jobs["job-1"]["status"] == "completed"
    assert out["status"] == "completed"


def test_publish_activity_cleanup_runs_only_on_clean_completion_when_requested(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """`cleanup_checkout_on_success` triggers checkout cleanup on a clean run, but is
    skipped when a task failed (a partial result keeps the checkout for retry)."""
    store, _ = _install(monkeypatch, api, "job-1", already_complete=False, task_graph_snapshot=[])
    cleaned: list[str] = []
    monkeypatch.setattr(api, "_cleanup_issue_checkout", lambda repo_path: cleaned.append(repo_path))

    _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS, "cleanup_checkout_on_success": True})
    assert cleaned == ["/repo"]

    cleaned.clear()
    store2, _ = _install(
        monkeypatch,
        api,
        "job-2",
        already_complete=False,
        task_graph_snapshot=[{"id": "t1", "title": "x", "status": "failed"}],
    )
    monkeypatch.setattr(api, "_cleanup_issue_checkout", lambda repo_path: cleaned.append(repo_path))
    _activity()(
        {
            **BASE_REQUEST,
            "job_id": "job-2",
            **MERGED_WORK_FIELDS,
            "cleanup_checkout_on_success": True,
        }
    )
    assert cleaned == []


def test_publish_activity_fast_forward_failure_records_failure_and_stops(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """A fast-forward failure marks the job failed with a comment and never attempts
    push or PR creation."""
    store, get_client = _install(
        monkeypatch, api, "job-1", already_complete=False, task_graph_snapshot=[]
    )
    monkeypatch.setattr(api, "_fast_forward", lambda repo_path, branch, base: (False, "conflict"))
    pushed = []
    monkeypatch.setattr(
        api,
        "_push_branch",
        lambda repo_path, remote, branch, token: pushed.append(1) or (True, None),
    )

    _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS})

    client = get_client()
    assert client.token == "tok-123"
    assert pushed == []
    assert client.created_prs == []
    assert store.jobs["job-1"]["status"] == "failed"
    assert "fast-forward failed" in store.jobs["job-1"]["error"]
    assert any("failed" in c[3] for c in client.comments)


def test_publish_activity_push_failure_records_failure_and_stops(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """A git push failure marks the job failed and never attempts PR creation."""
    store, get_client = _install(
        monkeypatch, api, "job-1", already_complete=False, task_graph_snapshot=[]
    )
    monkeypatch.setattr(
        api, "_push_branch", lambda repo_path, remote, branch, token: (False, "denied")
    )

    _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS})

    client = get_client()
    assert client.token == "tok-123"
    assert client.created_prs == []
    assert store.jobs["job-1"]["status"] == "failed"
    assert "git push failed" in store.jobs["job-1"]["error"]


def test_publish_activity_create_pr_github_api_error_records_failure(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """create_pull_request raising GitHubAPIError marks the job failed."""
    store, _ = _install(monkeypatch, api, "job-1", already_complete=False, task_graph_snapshot=[])
    holder: list[_FakeGitHubClient] = []

    def _make_client(token: str) -> _FakeGitHubClient:
        client = _FakeGitHubClient(token)
        client.create_pr_error = GitHubAPIError(422, "no commits")
        holder.append(client)
        return client

    monkeypatch.setattr(api, "GitHubClient", _make_client)

    _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS})

    assert len(holder) == 1
    assert holder[-1].token == "tok-123"
    assert store.jobs["job-1"]["status"] == "failed"
    assert "create_pull_request" in store.jobs["job-1"]["error"]


def test_publish_activity_already_complete_posts_close_recommendation_and_marks_done(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """An already-complete job gets a close-recommendation comment (including the
    completion evidence) and no PR calls at all; the marker is cleared and the job
    ends `already_complete`. `base`/`integration_branch`/`issue_title` are not
    required for this path."""
    store, get_client = _install(
        monkeypatch,
        api,
        "job-1",
        already_complete=True,
        completion_evidence="tests already covered this",
    )

    out = _activity()({**BASE_REQUEST})  # no MERGED_WORK_FIELDS needed

    client = get_client()
    assert client.token == "tok-123"
    assert client.created_prs == []
    assert client.updated_prs == []
    assert len(client.comments) == 1
    body = client.comments[0][3]
    assert "tests already covered this" in body
    assert "Recommend closing #9" in body
    assert store.jobs["job-1"]["status"] == "already_complete"
    assert store.cleared_markers == [("/repo", 9)]
    assert out["status"] == "already_complete"


def test_publish_activity_returns_final_job_snapshot(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """The activity's return value is exactly the post-publish get_job(job_id) dict."""
    store, _ = _install(monkeypatch, api, "job-1", already_complete=False, task_graph_snapshot=[])

    out = _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS})

    assert out == store.jobs["job-1"]


def test_publish_activity_missing_job_returns_unknown_status(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """A job the job store has never heard of throughout the whole call (edge case,
    not expected in practice) still returns a well-formed dict rather than raising
    or returning None."""
    from cryptography.fernet import Fernet

    from software_engineering_team import token_crypto

    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    ct = token_crypto.encrypt_token("tok-123")
    assert ct is not None
    calls = 0

    def _get_job(job_id: str, cache_dir: Any = None) -> Optional[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"job_id": job_id, "github_token_encrypted": ct}
        return None

    monkeypatch.setattr(api, "get_job", _get_job)
    monkeypatch.setattr(
        api, "update_job", lambda job_id, cache_dir=None, heartbeat=True, **fields: None
    )
    monkeypatch.setattr(api, "GitHubClient", lambda token: _FakeGitHubClient(token))
    monkeypatch.setattr(api, "_fast_forward", lambda repo_path, branch, base: (True, None))
    monkeypatch.setattr(api, "_push_branch", lambda repo_path, remote, branch, token: (True, None))
    monkeypatch.setattr(api, "_clear_active_issue_if_matches", lambda repo_path, num: None)
    monkeypatch.setattr(api, "_cleanup_issue_checkout", lambda repo_path: None)

    out = _activity()({**BASE_REQUEST, **MERGED_WORK_FIELDS})

    assert out == {"job_id": "job-1", "status": "unknown"}


def test_publish_activity_registered_under_expected_temporal_name() -> None:
    """The activity must be registered as ``coding_team_github_publish``, matching
    the name workflow.execute_activity dispatch (and any future workflow wiring)
    will reference -- a decorator with a wrong or accidentally-dropped name would
    silently break that dispatch without this test catching it."""
    definition = _activity().__temporal_activity_definition
    assert definition.name == "coding_team_github_publish"

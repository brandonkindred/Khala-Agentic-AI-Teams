"""Activity-level tests for github_failure_notice_activity.

Covers the two paths the acceptance criteria name: the raw-failure path
(`_record_failure`) and the outage-notice path (`_record_review_outage`),
including the `PR_REVIEW_POST_OUTAGE_NOTICE` gate, plus the wrapper's own
request validation. Everything `_record_failure`/`_record_review_outage`
touch is reached through the shared `coding_team_main` module-object alias,
so this file monkeypatches that surface directly rather than driving a real
job service or review-history store.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

import pytest

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
    """Records posted comments. Stands in for ``GitHubClient``.

    Supports the ``with GitHubClient(...) as client:`` context-manager usage
    the activity performs; entering/exiting does nothing but return ``self``.
    """

    def __init__(self, token: str) -> None:
        assert token, "activity must pass a non-empty token"
        self.token = token
        self.comments: list[tuple[str, str, int, str]] = []

    def __enter__(self) -> "_FakeGitHubClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def add_issue_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self.comments.append((owner, repo, number, body))


class _FakeJobStore:
    """In-memory ``get_job``/``update_job`` recorder, seeded with one job."""

    def __init__(self, job_id: str, **fields: Any) -> None:
        self.jobs: dict[str, dict[str, Any]] = {job_id: dict(fields)}

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
    monkeypatch.setattr(api, "update_review", lambda job_id, **kw: None)
    monkeypatch.setattr(api, "GitHubClient", _make_client)

    # _record_failure/_record_review_outage both resolve their collaborators
    # through this same `_main` module object, so patching it here is visible
    # to them without touching orchestration.py directly.
    def _get_client() -> _FakeGitHubClient:
        assert client_holder, "GitHubClient was never constructed"
        return client_holder[-1]

    return store, _get_client


BASE_REQUEST = {
    "job_id": "job-1",
    "owner": "acme",
    "repo": "widgets",
    "number": 9,
    "message": "boom",
    "kind": "failure",
}


def _activity():
    from software_engineering_team.temporal.coding_team_github_activities import (
        github_failure_notice_activity,
    )

    return github_failure_notice_activity


@pytest.mark.parametrize(
    "request_overrides,expected_fields,seed_job",
    [
        ({"job_id": None}, ["job_id"], False),
        ({"owner": None, "repo": None}, ["owner", "repo"], True),
        ({"message": None}, ["message"], True),
        ({"number": None}, ["number"], True),
        ({"kind": None}, ["kind"], True),
    ],
)
def test_failure_notice_activity_missing_base_field_raises_value_error(
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
    request = {**BASE_REQUEST, **request_overrides}

    with pytest.raises(ValueError, match="missing") as exc_info:
        _activity()(request)

    msg = str(exc_info.value)
    for field in expected_fields:
        assert field in msg
    assert repr(request) not in msg
    assert "tok-123" not in msg


def test_failure_notice_activity_rejects_plaintext_token_arg(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    _install(monkeypatch, api, "job-1")
    secret = "ghp_leaked"
    request = {**BASE_REQUEST, "token": secret}
    with pytest.raises(ValueError, match="token") as exc_info:
        _activity()(request)
    assert secret not in str(exc_info.value)


def test_failure_notice_activity_unresolvable_token_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    _install(monkeypatch, api, "job-1", github_token_encrypted="")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(ValueError, match="token"):
        _activity()(BASE_REQUEST)


def test_failure_notice_activity_invalid_kind_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """An unrecognized `kind` value raises `ValueError` naming the bad value, before
    any GitHub/job-store call runs. The error must never include the request payload
    or the GitHub token, because activity exceptions are recorded in Temporal history."""
    request = {**BASE_REQUEST, "kind": "bogus"}
    store, _ = _install(monkeypatch, api, "job-1")

    with pytest.raises(ValueError, match="kind") as exc_info:
        _activity()(request)

    msg = str(exc_info.value)
    assert "bogus" in msg
    assert repr(request) not in msg
    assert "tok-123" not in msg
    assert "status" not in store.jobs["job-1"]


def test_failure_notice_activity_failure_path_marks_job_failed_and_posts_raw_error(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """`kind="failure"` marks the job failed with the scrubbed error, leaves `phase`
    untouched (the key behavioral difference from the outage path), and always posts
    exactly one comment containing the job id and the error text."""
    store, get_client = _install(monkeypatch, api, "job-1", phase="implementing")

    out = _activity()({**BASE_REQUEST, "kind": "failure"})

    client = get_client()
    assert client.token == "tok-123"
    assert store.jobs["job-1"]["status"] == "failed"
    assert "boom" in store.jobs["job-1"]["error"]
    assert store.jobs["job-1"]["phase"] == "implementing"
    assert len(client.comments) == 1
    body = client.comments[0][3]
    assert "job-1" in body
    assert "boom" in body
    assert out["status"] == "failed"


def test_failure_notice_activity_outage_path_gate_on_marks_completed_and_posts_neutral_notice(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """`kind="outage"` with the gate unset (default true) marks the job failed with
    `phase="completed"`, and posts exactly one comment with the fixed neutral outage
    text -- never the raw error."""
    monkeypatch.delenv("PR_REVIEW_POST_OUTAGE_NOTICE", raising=False)
    store, get_client = _install(monkeypatch, api, "job-1")

    out = _activity()({**BASE_REQUEST, "kind": "outage"})

    client = get_client()
    assert client.token == "tok-123"
    assert store.jobs["job-1"]["status"] == "failed"
    assert store.jobs["job-1"]["phase"] == "completed"
    assert "boom" in store.jobs["job-1"]["error"]
    assert len(client.comments) == 1
    body = client.comments[0][3]
    assert "could not complete" in body
    assert "boom" not in body
    assert out["status"] == "failed"


def test_failure_notice_activity_outage_path_gate_off_marks_completed_with_zero_comments(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """With `PR_REVIEW_POST_OUTAGE_NOTICE` disabled, the outage path still marks the
    job failed/completed but posts no comment at all -- the acceptance criterion's
    named gate test."""
    monkeypatch.setenv("PR_REVIEW_POST_OUTAGE_NOTICE", "false")
    store, get_client = _install(monkeypatch, api, "job-1")

    _activity()({**BASE_REQUEST, "kind": "outage"})

    client = get_client()
    assert client.token == "tok-123"
    assert store.jobs["job-1"]["status"] == "failed"
    assert store.jobs["job-1"]["phase"] == "completed"
    assert client.comments == []


def test_failure_notice_activity_returns_final_job_snapshot(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """The activity's return value is exactly the post-call get_job(job_id) dict."""
    store, _ = _install(monkeypatch, api, "job-1")

    out = _activity()({**BASE_REQUEST, "kind": "failure"})

    assert out == store.jobs["job-1"]


def test_failure_notice_activity_missing_job_returns_unknown_status(
    monkeypatch: pytest.MonkeyPatch, api: Any
) -> None:
    """A job the job store has never heard of throughout the whole call still returns
    a well-formed dict rather than raising or returning None."""
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
    monkeypatch.setattr(api, "update_review", lambda job_id, **kw: None)
    monkeypatch.setattr(api, "GitHubClient", lambda token: _FakeGitHubClient(token))

    out = _activity()({**BASE_REQUEST, "kind": "failure"})

    assert out == {"job_id": "job-1", "status": "unknown"}


def test_failure_notice_activity_registered_under_expected_temporal_name() -> None:
    """The activity must be registered as ``coding_team_github_failure_notice``,
    matching the name workflow.execute_activity dispatch (and any future workflow
    wiring) will reference -- a decorator with a wrong or accidentally-dropped name
    would silently break that dispatch without this test catching it."""
    definition = _activity().__temporal_activity_definition
    assert definition.name == "coding_team_github_failure_notice"

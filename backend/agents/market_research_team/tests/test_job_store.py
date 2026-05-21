"""Direct coverage for the team's ``job_store`` thin-wrapper module.

These tests bypass the autouse patch that re-binds ``_client`` to the fake
in order to exercise:

* the lazy-singleton ``_client()`` factory itself (the original function),
* ``list_jobs(statuses=...)`` pass-through, and
* ``mark_all_running_jobs_failed``'s success and failure-tolerant branches.
"""

from __future__ import annotations

from typing import Any

import pytest

from job_service_client_fake import FakeJobServiceClient
from market_research_team.shared import job_store as js


def test_client_factory_constructs_lazy_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original ``_client()`` factory should build a JobServiceClient
    on first call and reuse it thereafter."""

    # The autouse conftest fixture rebinds ``js._client`` to a lambda that
    # always returns the fake. Undo every patch on this test's monkeypatch
    # so the real factory runs under coverage, then re-establish only the
    # constructor stub we want.
    monkeypatch.undo()

    constructed: list[FakeJobServiceClient] = []

    def _ctor(team: str) -> FakeJobServiceClient:
        client = FakeJobServiceClient(team=team)
        constructed.append(client)
        return client

    monkeypatch.setattr(js, "_client_instance", None, raising=False)
    monkeypatch.setattr(js, "JobServiceClient", _ctor)

    first = js._client()
    second = js._client()

    assert first is second
    assert len(constructed) == 1
    assert constructed[0].team == "market_research_team"


def test_list_jobs_passes_statuses_to_client(
    monkeypatch: pytest.MonkeyPatch, fake_job_client: FakeJobServiceClient
) -> None:
    """``list_jobs`` must thread the ``statuses`` keyword through to the client."""

    received: dict[str, Any] = {}
    original = fake_job_client.list_jobs

    def _spy(*, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        received["statuses"] = statuses
        return original(statuses=statuses)

    monkeypatch.setattr(fake_job_client, "list_jobs", _spy)

    js.list_jobs(statuses=[js.JOB_STATUS_RUNNING])

    assert received["statuses"] == [js.JOB_STATUS_RUNNING]


def test_mark_all_running_jobs_failed_delegates_to_client(
    fake_job_client: FakeJobServiceClient,
) -> None:
    fake_job_client.create_job("active-1", status=js.JOB_STATUS_RUNNING)
    fake_job_client.create_job("active-2", status=js.JOB_STATUS_PENDING)

    js.mark_all_running_jobs_failed("shutdown")

    for job_id in ("active-1", "active-2"):
        job = fake_job_client.get_job(job_id)
        assert job is not None
        assert job["status"] == "failed"
        assert job["error"] == "shutdown"


def test_mark_all_running_jobs_failed_swallows_client_errors(
    monkeypatch: pytest.MonkeyPatch, fake_job_client: FakeJobServiceClient
) -> None:
    """The wrapper logs and absorbs exceptions from the underlying client."""

    def _boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("client down")

    monkeypatch.setattr(fake_job_client, "mark_all_active_jobs_failed", _boom)

    # Must not raise — coverage of the except branch.
    js.mark_all_running_jobs_failed("network outage")

"""Tests for the shared BaseJobStore methods (cancel / is_cancelled / list / shutdown).

Self-contained: a tiny subclass routes ``_client`` to the in-memory
FakeJobServiceClient, so this needs no real job service.
"""

from __future__ import annotations

from typing import Any

import pytest

from job_service_client import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    BaseJobStore,
)
from job_service_client_fake import FakeJobServiceClient


class _Store(BaseJobStore):
    team = "test_team"

    def __init__(self, client: FakeJobServiceClient) -> None:
        self._fake = client

    def _client(self) -> FakeJobServiceClient:  # type: ignore[override]
        return self._fake


@pytest.fixture
def store() -> _Store:
    return _Store(FakeJobServiceClient(team="test_team"))


def test_cancel_active_job_marks_cancelled(store: _Store) -> None:
    """Cancelling a running job sets its status to cancelled and returns True."""
    store.create_job("j1", status=JOB_STATUS_RUNNING)
    assert store.cancel_job("j1") is True
    assert store.is_job_cancelled("j1") is True


def test_cancel_pending_job_marks_cancelled(store: _Store) -> None:
    """A pending job is also a valid cancellable state (not just running)."""
    store.create_job("p1", status=JOB_STATUS_PENDING)
    assert store.cancel_job("p1") is True
    assert store.is_job_cancelled("p1") is True


def test_cancel_missing_job_returns_false(store: _Store) -> None:
    """Cancelling / querying a non-existent job returns False (no write)."""
    assert store.cancel_job("nope") is False
    assert store.is_job_cancelled("nope") is False


def test_cancel_and_is_cancelled_reject_empty_job_id(store: _Store) -> None:
    """An empty job_id violates the precondition: both methods raise ValueError
    (an explicit check, so it survives ``python -O``)."""
    with pytest.raises(ValueError):
        store.cancel_job("")
    with pytest.raises(ValueError):
        store.is_job_cancelled("")


def test_cancel_terminal_job_is_noop(store: _Store) -> None:
    """A job already in a terminal status is not overwritten: cancel returns False
    and the original status is preserved (the atomic status-guarded cancel)."""
    store.create_job("done", status=JOB_STATUS_COMPLETED)
    assert store.cancel_job("done") is False
    assert store.get_job("done")["status"] == JOB_STATUS_COMPLETED


def test_list_jobs_no_args_returns_all(store: _Store) -> None:
    """Default behaviour (no running_only, no statuses): returns every job
    regardless of status."""
    store.create_job("r", status=JOB_STATUS_RUNNING)
    store.create_job("p", status=JOB_STATUS_PENDING)
    store.create_job("d", status=JOB_STATUS_COMPLETED)
    assert {j["job_id"] for j in store.list_jobs()} == {"r", "p", "d"}


def test_list_jobs_running_only_and_explicit_statuses(store: _Store) -> None:
    """``running_only`` selects pending+running; an explicit ``statuses`` filters
    to exactly those, and takes precedence over ``running_only`` when both are set."""
    store.create_job("r", status=JOB_STATUS_RUNNING)
    store.create_job("p", status=JOB_STATUS_PENDING)
    store.create_job("d", status=JOB_STATUS_COMPLETED)

    running = {j["job_id"] for j in store.list_jobs(running_only=True)}
    assert running == {"r", "p"}

    explicit = {j["job_id"] for j in store.list_jobs(statuses=[JOB_STATUS_COMPLETED])}
    assert explicit == {"d"}

    # Explicit statuses take precedence over running_only.
    both = {j["job_id"] for j in store.list_jobs(running_only=True, statuses=[JOB_STATUS_COMPLETED])}
    assert both == {"d"}


def test_mark_all_running_jobs_failed_returns_ids(store: _Store) -> None:
    """The bulk shutdown sweep marks every *active* job failed — both pending and
    running (not only running) — and returns all affected ids."""
    store.create_job("a", status=JOB_STATUS_RUNNING)
    store.create_job("b", status=JOB_STATUS_PENDING)
    failed = store.mark_all_running_jobs_failed("shutdown")
    assert set(failed) == {"a", "b"}  # pending 'b' is included
    assert store.get_job("a")["status"] == "failed"
    assert store.get_job("b")["status"] == "failed"  # the pending job is failed too


def test_mark_all_running_jobs_failed_swallows_client_error(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """The shutdown hook must never raise: a client error is logged and swallowed,
    and the method returns []."""

    def _boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("client down")

    monkeypatch.setattr(store._fake, "mark_all_active_jobs_failed", _boom)
    assert store.mark_all_running_jobs_failed("x") == []

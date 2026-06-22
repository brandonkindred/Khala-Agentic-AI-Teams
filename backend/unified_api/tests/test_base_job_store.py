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
    store.create_job("j1", status=JOB_STATUS_RUNNING)
    assert store.cancel_job("j1") is True
    assert store.is_job_cancelled("j1") is True


def test_cancel_pending_job_marks_cancelled(store: _Store) -> None:
    # A pending job is also a valid cancellable state (not just running).
    store.create_job("p1", status=JOB_STATUS_PENDING)
    assert store.cancel_job("p1") is True
    assert store.is_job_cancelled("p1") is True


def test_cancel_missing_job_returns_false(store: _Store) -> None:
    assert store.cancel_job("nope") is False
    assert store.is_job_cancelled("nope") is False


def test_cancel_terminal_job_is_noop(store: _Store) -> None:
    store.create_job("done", status=JOB_STATUS_COMPLETED)
    assert store.cancel_job("done") is False
    assert store.get_job("done")["status"] == JOB_STATUS_COMPLETED


def test_list_jobs_running_only_and_explicit_statuses(store: _Store) -> None:
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
    store.create_job("a", status=JOB_STATUS_RUNNING)
    store.create_job("b", status=JOB_STATUS_PENDING)
    failed = store.mark_all_running_jobs_failed("shutdown")
    assert set(failed) == {"a", "b"}
    assert store.get_job("a")["status"] == "failed"


def test_mark_all_running_jobs_failed_swallows_client_error(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    def _boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("client down")

    monkeypatch.setattr(store._fake, "mark_all_active_jobs_failed", _boom)
    # Must not raise (shutdown hook) and returns [] on error.
    assert store.mark_all_running_jobs_failed("x") == []

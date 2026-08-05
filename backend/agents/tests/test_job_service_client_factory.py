"""Tests for the cached client factory and BaseJobStore.

These cover the shared job-store infrastructure that teams now delegate to:
``get_job_service_client`` (one client per team) and ``BaseJobStore`` (the
inherited CRUD wrappers), neither of which had direct coverage before.
"""

from __future__ import annotations

import pytest

import job_service_client as jsc
from job_service_client import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    BaseJobStore,
    get_job_service_client,
)
from job_service_client_fake import FakeJobServiceClient


@pytest.fixture(autouse=True)
def _clear_cache():
    jsc._clear_job_client_cache_for_testing()
    yield
    jsc._clear_job_client_cache_for_testing()


# --------------------------------------------------------------------------
# get_job_service_client
# --------------------------------------------------------------------------


def test_factory_returns_same_instance_per_team():
    a = get_job_service_client("team_a")
    b = get_job_service_client("team_a")
    assert a is b
    assert a.team == "team_a"


def test_factory_isolates_distinct_teams():
    assert get_job_service_client("team_a") is not get_job_service_client("team_b")


def test_factory_rejects_empty_team():
    with pytest.raises(ValueError, match="non-empty string"):
        get_job_service_client("")


def test_factory_rejects_none_team():
    with pytest.raises(ValueError, match="non-empty string"):
        get_job_service_client(None)  # type: ignore[arg-type]


def test_factory_rejects_non_string_team():
    with pytest.raises(ValueError, match="non-empty string"):
        get_job_service_client(123)  # type: ignore[arg-type]


def test_clear_cache_forces_fresh_instance():
    first = get_job_service_client("team_a")
    jsc._clear_job_client_cache_for_testing()
    assert get_job_service_client("team_a") is not first


# --------------------------------------------------------------------------
# BaseJobStore — inherited CRUD delegates to the team's cached client
# --------------------------------------------------------------------------


class _Store(BaseJobStore):
    team = "base_store_test"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    """A BaseJobStore subclass whose client is an in-memory fake.

    Seeds the factory cache so ``BaseJobStore._client`` resolves to the fake
    via the normal ``get_job_service_client`` path.
    """
    fake = FakeJobServiceClient(team="base_store_test")
    jsc._client_cache["base_store_test"] = fake
    return _Store()


def test_base_store_uses_cached_client(store: _Store):
    assert store._client() is jsc._client_cache["base_store_test"]


def test_base_store_without_team_raises_clear_error():
    """A subclass that forgets to set ``team`` gets an explicit NotImplementedError
    naming the class, not the factory's generic AssertionError."""

    class _NoTeam(BaseJobStore):
        pass

    with pytest.raises(NotImplementedError, match="_NoTeam.*team"):
        _NoTeam()._client()


def test_base_store_create_get_update_delete(store: _Store):
    store.create_job("j1", foo="bar")
    job = store.get_job("j1")
    assert job is not None
    assert job["status"] == JOB_STATUS_PENDING
    assert job["foo"] == "bar"

    store.update_job("j1", foo="baz")
    assert store.get_job("j1")["foo"] == "baz"

    assert store.delete_job("j1") is True
    assert store.get_job("j1") is None


def test_base_store_list_jobs_running_only(store: _Store):
    store.create_job("running", status=JOB_STATUS_RUNNING)
    store.create_job("done", status=JOB_STATUS_COMPLETED)
    all_jobs = {j["job_id"] for j in store.list_jobs()}
    assert all_jobs == {"running", "done"}
    running = {j["job_id"] for j in store.list_jobs(running_only=True)}
    assert running == {"running"}


def test_base_store_mark_helpers(store: _Store):
    store.create_job("j1")
    store.mark_job_running("j1")
    assert store.get_job("j1")["status"] == JOB_STATUS_RUNNING

    store.mark_job_completed("j1", result="ok")
    done = store.get_job("j1")
    assert done["status"] == JOB_STATUS_COMPLETED
    assert done["progress"] == 100
    assert done["result"] == "ok"

    store.create_job("j2")
    store.mark_job_failed("j2", "boom")
    failed = store.get_job("j2")
    assert failed["status"] == JOB_STATUS_FAILED
    assert failed["error"] == "boom"


def test_base_store_mark_all_running_failed(store: _Store):
    store.create_job("a", status=JOB_STATUS_RUNNING)
    store.create_job("b", status=JOB_STATUS_PENDING)
    failed = store.mark_all_running_jobs_failed("shutdown")
    assert set(failed) == {"a", "b"}


def test_base_store_reset_job(store: _Store):
    store.create_job("j1", status=JOB_STATUS_RUNNING, progress=42, error="x")
    store.reset_job("j1")
    job = store.get_job("j1")
    assert job["status"] == JOB_STATUS_PENDING
    assert job["progress"] == 0
    assert job["error"] is None


def test_base_store_reset_job_clears_pause_envelope(store: _Store):
    """A reset must also clear the HITL pause envelope, not just status/progress/error,
    so a reset paused job doesn't trip pending-pause re-entry on next orchestrator run."""
    store.create_job(
        "j1",
        status=JOB_STATUS_RUNNING,
        waiting_for_answers=True,
        pending_questions=["what next?"],
        resume_token="tok-123",
        pause_kind="clarification",
        pause_context={"foo": "bar"},
    )
    store.reset_job("j1")
    job = store.get_job("j1")
    assert job["waiting_for_answers"] is False
    assert job["pending_questions"] == []
    assert job["resume_token"] is None
    assert job["pause_kind"] is None
    assert job["pause_context"] is None

"""Regression-lock tests for coding_team.models.JobStatus and its hitl.py/job_store.py derivations."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from software_engineering_team.coding_team import hitl, job_store
from software_engineering_team.coding_team.models import JobStatus


class _FakeJobStoreClient:
    def __init__(self) -> None:
        self.jobs: Dict[str, Dict[str, Any]] = {}

    def create_job(self, job_id: str, status: str = "pending", **fields: Any) -> None:
        self.jobs[job_id] = {"job_id": job_id, "status": status, **fields}

    def update_job(self, job_id: str, heartbeat: bool = True, **fields: Any) -> None:
        self.jobs.setdefault(job_id, {"job_id": job_id}).update(fields)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.jobs.get(job_id)
        return dict(job) if job is not None else None


@pytest.mark.parametrize("member", list(JobStatus))
def test_job_status_round_trips_through_update_job_get_job(monkeypatch, member: JobStatus):
    """Every JobStatus member, written via update_job, reads back as its plain .value string."""
    fake = _FakeJobStoreClient()
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: fake)
    job_store.create_job("job_1", "some/repo")

    job_store.update_job("job_1", status=member.value)

    fetched = job_store.get_job("job_1")
    assert fetched["status"] == member.value
    assert type(fetched["status"]) is str  # not the enum member — a genuine plain str


def test_job_status_values_match_known_literal_set():
    """Regression-lock: the discovered set of 8 job-status literals must not silently grow/shrink."""
    assert {m.value for m in JobStatus} == {
        "pending",
        "running",
        "waiting_for_user",
        "completed",
        "completed_with_failures",
        "already_complete",
        "failed",
        "cancelled",
    }


def test_hitl_waiting_status_matches_job_status():
    assert hitl.WAITING_STATUS == JobStatus.WAITING_FOR_USER.value == "waiting_for_user"


def test_hitl_terminal_success_statuses_regression_lock():
    assert hitl.TERMINAL_SUCCESS_STATUSES == frozenset(
        {"completed", "completed_with_failures", "already_complete"}
    )


def test_hitl_terminal_statuses_regression_lock():
    # _TERMINAL_STATUSES is module-private by convention only; this suite already reaches into
    # underscore-prefixed internals elsewhere (job_store._client, orch_mod._render_context_file,
    # cache._entries) so a direct regression-lock assertion here matches established style, on
    # top of (not instead of) the behavioral coverage below via hitl.is_terminal().
    assert hitl._TERMINAL_STATUSES == frozenset(
        {"completed", "completed_with_failures", "already_complete", "failed", "cancelled"}
    )


def test_is_terminal_covers_every_terminal_and_non_terminal_job_status():
    for member in (
        JobStatus.COMPLETED,
        JobStatus.COMPLETED_WITH_FAILURES,
        JobStatus.ALREADY_COMPLETE,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    ):
        assert hitl.is_terminal({"status": member.value})
    for member in (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.WAITING_FOR_USER):
        assert not hitl.is_terminal({"status": member.value})


def test_job_store_non_terminal_statuses_regression_lock():
    assert job_store.NON_TERMINAL_STATUSES == ("pending", "running", "waiting_for_user")

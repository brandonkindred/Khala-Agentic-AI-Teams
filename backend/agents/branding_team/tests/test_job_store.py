"""Direct coverage for branding_team's guarded-transition helpers.

Exercises ``begin_job``/``mark_completed``/``mark_failed`` end-to-end against
the in-memory ``FakeJobServiceClient`` (no real job service or Postgres needed),
including the literal TOCTOU scenario ``update_job_if_not_cancelled`` closes: a
cancel that lands before a guarded transition must not be silently overwritten.
"""

from __future__ import annotations

import pytest

from branding_team.shared import job_store
from job_service_client_fake import FakeJobServiceClient


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeJobServiceClient:
    client = FakeJobServiceClient(team="branding_team")
    monkeypatch.setattr(job_store, "_client_instance", client)
    return client


def test_begin_job_marks_running(fake_client: FakeJobServiceClient) -> None:
    fake_client.create_job("job-1", status=job_store.JOB_STATUS_PENDING)

    assert job_store.begin_job("job-1") is True
    assert fake_client.get_job("job-1")["status"] == job_store.JOB_STATUS_RUNNING


def test_begin_job_noop_when_already_cancelled(fake_client: FakeJobServiceClient) -> None:
    fake_client.create_job("job-1", status=job_store.JOB_STATUS_CANCELLED)

    assert job_store.begin_job("job-1") is False
    assert fake_client.get_job("job-1")["status"] == job_store.JOB_STATUS_CANCELLED


def test_begin_job_closes_the_cancel_race(fake_client: FakeJobServiceClient) -> None:
    """A cancel lands first; begin_job must not resurrect the job as running."""
    fake_client.create_job("job-1", status=job_store.JOB_STATUS_PENDING)
    assert fake_client.cancel_active_job("job-1") is True

    assert job_store.begin_job("job-1") is False
    assert fake_client.get_job("job-1")["status"] == job_store.JOB_STATUS_CANCELLED


def test_mark_completed_writes_result(fake_client: FakeJobServiceClient) -> None:
    fake_client.create_job("job-1", status=job_store.JOB_STATUS_RUNNING)

    assert job_store.mark_completed("job-1", {"ok": True}) is True
    job = fake_client.get_job("job-1")
    assert job["status"] == job_store.JOB_STATUS_COMPLETED
    assert job["result"] == {"ok": True}


def test_mark_completed_noop_when_cancelled(fake_client: FakeJobServiceClient) -> None:
    fake_client.create_job("job-1", status=job_store.JOB_STATUS_CANCELLED)

    assert job_store.mark_completed("job-1", {"ok": True}) is False
    assert fake_client.get_job("job-1")["status"] == job_store.JOB_STATUS_CANCELLED


def test_mark_failed_writes_error(fake_client: FakeJobServiceClient) -> None:
    fake_client.create_job("job-1", status=job_store.JOB_STATUS_RUNNING)

    assert job_store.mark_failed("job-1", "boom") is True
    job = fake_client.get_job("job-1")
    assert job["status"] == job_store.JOB_STATUS_FAILED
    assert job["error"] == "boom"


def test_mark_failed_noop_when_cancelled(fake_client: FakeJobServiceClient) -> None:
    fake_client.create_job("job-1", status=job_store.JOB_STATUS_CANCELLED)

    assert job_store.mark_failed("job-1", "boom") is False
    assert fake_client.get_job("job-1")["status"] == job_store.JOB_STATUS_CANCELLED


def test_begin_job_raises_for_missing_job(fake_client: FakeJobServiceClient) -> None:
    """A missing job_id is a broken precondition, not a legitimate cancellation —
    it must not be silently mislabeled as 'already cancelled' (regression: the
    atomic primitive can't distinguish missing from cancelled by itself, since
    both match zero rows; _guarded_transition disambiguates with a supplementary
    read and raises instead)."""
    with pytest.raises(ValueError, match="does not exist"):
        job_store.begin_job("missing-job")


def test_mark_completed_raises_for_missing_job(fake_client: FakeJobServiceClient) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        job_store.mark_completed("missing-job", {"ok": True})


def test_mark_failed_raises_for_missing_job(fake_client: FakeJobServiceClient) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        job_store.mark_failed("missing-job", "boom")


def test_guarded_transition_rejects_writing_cancelled_status(
    fake_client: FakeJobServiceClient,
) -> None:
    """update_job_if_not_cancelled must not be usable to cancel a job (it would
    overwrite a completed/failed job too, unlike cancel_active_job's narrower
    guard) — enforced as a precondition, not silently allowed."""
    fake_client.create_job("job-1", status=job_store.JOB_STATUS_COMPLETED)

    with pytest.raises(AssertionError):
        job_store.update_job_if_not_cancelled("job-1", status=job_store.JOB_STATUS_CANCELLED)

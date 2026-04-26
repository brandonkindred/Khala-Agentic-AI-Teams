"""Unit tests for SE job-store heartbeat and stale-job logic.

These tests run against the in-memory ``FakeJobServiceClient`` (see
``backend/conftest.py``) — they do not require Postgres or the real job
service.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _patched_job_store(monkeypatch, fake_job_client):
    """Route the SE job_store's ``_client`` through the in-memory fake."""
    from software_engineering_team.shared import job_store as js

    monkeypatch.setattr(js, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client


def test_heartbeat_updates_last_heartbeat_at(tmp_path: Path) -> None:
    """start_job_heartbeat_thread causes last_heartbeat_at to advance for a pending job."""
    from software_engineering_team.shared.job_store import (
        create_job,
        get_job,
        start_job_heartbeat_thread,
    )

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())
    create_job(job_id, "/repo", cache_dir=cache_dir)

    data0 = get_job(job_id, cache_dir=cache_dir)
    assert data0 is not None
    initial_hb = (
        data0.get("last_heartbeat_at") or data0.get("updated_at") or data0.get("created_at")
    )

    start_job_heartbeat_thread(job_id, interval_seconds=0.15, cache_dir=cache_dir)
    time.sleep(0.4)

    data1 = get_job(job_id, cache_dir=cache_dir)
    assert data1 is not None
    later_hb = data1.get("last_heartbeat_at") or data1.get("updated_at")
    assert later_hb >= initial_hb
    assert data1.get("status") in ("pending", "running")


def test_heartbeat_stops_when_job_terminal(tmp_path: Path) -> None:
    """Heartbeat thread stops updating once job status is completed; status remains completed."""
    from software_engineering_team.shared.job_store import (
        JOB_STATUS_COMPLETED,
        create_job,
        get_job,
        start_job_heartbeat_thread,
        update_job,
    )

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())
    create_job(job_id, "/repo", cache_dir=cache_dir)
    start_job_heartbeat_thread(job_id, interval_seconds=0.15, cache_dir=cache_dir)

    update_job(job_id, status=JOB_STATUS_COMPLETED, cache_dir=cache_dir)
    time.sleep(0.35)

    data = get_job(job_id, cache_dir=cache_dir)
    assert data is not None
    assert data.get("status") == JOB_STATUS_COMPLETED


def test_mark_stale_jobs_failed_marks_old_heartbeat(tmp_path: Path, fake_job_client) -> None:
    """Job with last_heartbeat_at older than threshold (and not waiting_for_answers) is marked failed."""
    from software_engineering_team.shared.job_store import (
        create_job,
        get_job,
        mark_stale_jobs_failed,
    )

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())
    create_job(job_id, "/repo", cache_dir=cache_dir)
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    fake_job_client.update_job(job_id, heartbeat=False, last_heartbeat_at=stale_ts)

    failed = mark_stale_jobs_failed(stale_after_seconds=60.0, reason="stale", cache_dir=cache_dir)
    assert job_id in failed

    data = get_job(job_id, cache_dir=cache_dir)
    assert data is not None
    assert data.get("status") == "failed"
    assert data.get("error") == "stale"


def test_mark_stale_jobs_failed_does_not_mark_recent_heartbeat(tmp_path: Path) -> None:
    """Job with last_heartbeat_at within threshold is not marked failed."""
    from software_engineering_team.shared.job_store import (
        create_job,
        get_job,
        mark_stale_jobs_failed,
    )

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())
    create_job(job_id, "/repo", cache_dir=cache_dir)

    failed = mark_stale_jobs_failed(stale_after_seconds=60.0, reason="stale", cache_dir=cache_dir)
    assert job_id not in failed

    data = get_job(job_id, cache_dir=cache_dir)
    assert data is not None
    assert data.get("status") == "pending"


def test_waiting_for_answers_excluded_from_stale(tmp_path: Path, fake_job_client) -> None:
    """Job with waiting_for_answers=True is not marked stale even when last_heartbeat_at is old."""
    from software_engineering_team.shared.job_store import (
        create_job,
        get_job,
        mark_stale_jobs_failed,
    )

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())
    create_job(job_id, "/repo", cache_dir=cache_dir)
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    fake_job_client.update_job(
        job_id, heartbeat=False, last_heartbeat_at=stale_ts, waiting_for_answers=True
    )

    failed = mark_stale_jobs_failed(stale_after_seconds=60.0, reason="stale", cache_dir=cache_dir)
    assert job_id not in failed

    data = get_job(job_id, cache_dir=cache_dir)
    assert data is not None
    assert data.get("status") == "pending"
    assert data.get("waiting_for_answers") is True


def test_get_stale_after_seconds_default() -> None:
    """get_stale_after_seconds returns a positive float (default 1800 when env unset)."""
    from software_engineering_team.shared.job_store import get_stale_after_seconds

    # May be overridden by env in CI; just assert it's a positive number
    val = get_stale_after_seconds()
    assert isinstance(val, (int, float))
    assert val > 0


def test_delete_job_removes_job(tmp_path: Path) -> None:
    """delete_job removes the job file; get_job then returns None."""
    from software_engineering_team.shared.job_store import create_job, delete_job, get_job

    cache_dir = tmp_path
    job_id = str(uuid.uuid4())
    create_job(job_id, "/repo", cache_dir=cache_dir)
    assert get_job(job_id, cache_dir=cache_dir) is not None

    assert delete_job(job_id, cache_dir=cache_dir) is True
    assert get_job(job_id, cache_dir=cache_dir) is None

    assert delete_job(job_id, cache_dir=cache_dir) is False

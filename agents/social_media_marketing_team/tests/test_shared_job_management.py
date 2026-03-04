from datetime import datetime, timedelta, timezone

from shared_job_management import CentralJobManager, JOB_STATUS_FAILED, JOB_STATUS_PENDING


def test_stale_active_job_marked_failed(tmp_path) -> None:
    manager = CentralJobManager(team="team_a", cache_dir=tmp_path / "cache")
    manager.create_job("j1", status=JOB_STATUS_PENDING)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    manager.update_job("j1", last_heartbeat_at=stale, heartbeat=False)

    failed = manager.mark_stale_active_jobs_failed(
        stale_after_seconds=60,
        reason="stale",
    )

    assert failed == ["j1"]
    job = manager.get_job("j1")
    assert job is not None
    assert job["status"] == JOB_STATUS_FAILED
    assert job["error"] == "stale"


def test_waiting_jobs_excluded_from_stale_failure(tmp_path) -> None:
    manager = CentralJobManager(team="team_b", cache_dir=tmp_path / "cache")
    manager.create_job("j2", status=JOB_STATUS_PENDING, waiting_for_answers=True)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    manager.update_job("j2", last_heartbeat_at=stale, heartbeat=False)

    failed = manager.mark_stale_active_jobs_failed(
        stale_after_seconds=60,
        reason="stale",
    )

    assert failed == []
    assert manager.get_job("j2")["status"] == JOB_STATUS_PENDING

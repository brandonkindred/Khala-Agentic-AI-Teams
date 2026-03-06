from datetime import datetime, timedelta, timezone

from shared_job_management import CentralJobManager, JOB_STATUS_FAILED, JOB_STATUS_PENDING


def test_apply_to_job_merges_nested_key(tmp_path) -> None:
    """apply_to_job atomically reads, mutates via callback, writes; get_job sees the merge."""
    manager = CentralJobManager(team="team_a", cache_dir=tmp_path / "cache")
    manager.create_job("j0", status=JOB_STATUS_PENDING)

    def merge_task_state(data):
        task_states = data.setdefault("task_states", {})
        task_states["t1"] = {"status": "done", "assignee": "backend"}

    manager.apply_to_job("j0", merge_task_state)

    job = manager.get_job("j0")
    assert job is not None
    assert job.get("task_states", {}).get("t1") == {"status": "done", "assignee": "backend"}


def test_apply_to_job_no_op_when_job_missing(tmp_path) -> None:
    """apply_to_job is no-op when job does not exist (does not raise)."""
    manager = CentralJobManager(team="team_a", cache_dir=tmp_path / "cache")
    manager.apply_to_job("nonexistent", lambda data: data.update({"x": 1}))
    assert manager.get_job("nonexistent") is None


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

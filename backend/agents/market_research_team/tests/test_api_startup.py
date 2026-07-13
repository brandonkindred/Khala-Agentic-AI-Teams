"""Coverage for ``api.main._startup``'s two independent best-effort steps:
starting the Temporal worker backstop and sweeping orphaned transcript
directories. Neither is exercised by the request-response tests in
``test_api.py``/``test_api_lifecycle.py``.
"""

from __future__ import annotations

from uuid import uuid4

from market_research_team.api import main as mr_main
from market_research_team.shared import job_store as js
from market_research_team.shared import transcript_store as ts


def test_startup_sweep_uses_job_store_status_for_is_active(monkeypatch, fake_job_client) -> None:
    monkeypatch.setattr(
        "market_research_team.temporal.worker.start_market_research_temporal_worker_thread",
        lambda: None,
    )

    running_job = str(uuid4())
    completed_job = str(uuid4())
    fake_job_client.create_job(running_job, status=js.JOB_STATUS_RUNNING)
    fake_job_client.create_job(completed_job, status=js.JOB_STATUS_COMPLETED)

    seen: dict[str, bool] = {}

    def _fake_sweep(is_active) -> int:
        seen["running"] = is_active(running_job)
        seen["completed"] = is_active(completed_job)
        seen["missing"] = is_active("job-does-not-exist")
        return 2

    monkeypatch.setattr(ts, "sweep_orphaned", _fake_sweep)

    mr_main._startup()

    assert seen == {"running": True, "completed": False, "missing": False}


def test_startup_never_raises_when_worker_start_fails(monkeypatch, fake_job_client) -> None:
    def _boom() -> None:
        raise RuntimeError("temporal not reachable")

    monkeypatch.setattr(
        "market_research_team.temporal.worker.start_market_research_temporal_worker_thread",
        _boom,
    )
    monkeypatch.setattr(ts, "sweep_orphaned", lambda is_active: 0)

    mr_main._startup()  # must not raise


def test_startup_never_raises_when_sweep_fails(monkeypatch, fake_job_client) -> None:
    monkeypatch.setattr(
        "market_research_team.temporal.worker.start_market_research_temporal_worker_thread",
        lambda: None,
    )

    def _boom(is_active) -> int:
        raise RuntimeError("job store down")

    monkeypatch.setattr(ts, "sweep_orphaned", _boom)

    mr_main._startup()  # must not raise

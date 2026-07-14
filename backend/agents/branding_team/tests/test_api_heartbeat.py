"""Per-job heartbeat coverage for ``branding_team/api/main.py``.

A branding pipeline can run for several minutes with no intermediate job-service
update, so ``_run_branding_core`` drives a background heartbeat for the duration of
``orchestrator.run(...)``. Without it, the stale-job monitor would mark a valid
long-running run as failed. These tests run in the default suite (no live Postgres /
job service needed).
"""

from __future__ import annotations

import contextlib
import threading

from branding_team.api import main as api_main
from branding_team.shared import job_store


class _RecordingJobManager:
    def __init__(self) -> None:
        self.heartbeats: list[str] = []
        self.beat_once = threading.Event()

    def heartbeat(self, job_id: str) -> None:
        self.heartbeats.append(job_id)
        self.beat_once.set()


def _call_run_core() -> None:
    api_main._run_branding_core(
        job_id="job-hb",
        mission=object(),
        human_review=object(),
        brand_checks=[],
        client_id=None,
        brand_id=None,
        include_market_research=False,
        include_design_assets=False,
        target_phase=None,
    )


def test_run_branding_core_heartbeats_during_run(monkeypatch) -> None:
    manager = _RecordingJobManager()
    monkeypatch.setattr(api_main, "_job_manager", manager)
    monkeypatch.setattr(api_main, "_job_heartbeat_interval_s", lambda: 0.01)
    monkeypatch.setattr(job_store, "is_job_cancelled", lambda job_id: False)
    monkeypatch.setattr(job_store, "update_job", lambda *a, **kw: None)

    class _Result:
        def model_dump(self):
            return {}

    class _Orchestrator:
        def run(self, **kwargs):
            # Block until the background beater has fired at least once so the
            # assertion below is deterministic rather than timing-dependent.
            assert manager.beat_once.wait(timeout=5.0), "expected a heartbeat during the run"
            return _Result()

    monkeypatch.setattr(api_main, "orchestrator", _Orchestrator())

    _call_run_core()

    assert manager.heartbeats, "expected at least one heartbeat while the pipeline ran"
    assert all(jid == "job-hb" for jid in manager.heartbeats)


def test_job_heartbeat_is_noop_context_when_manager_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "_job_manager", None)
    ctx = api_main._job_heartbeat("job-x")
    assert isinstance(ctx, contextlib.nullcontext)
    with ctx:
        pass


def test_run_branding_core_runs_without_job_manager(monkeypatch) -> None:
    """With no job manager the run still completes (heartbeat degrades to a no-op)."""
    monkeypatch.setattr(api_main, "_job_manager", None)
    monkeypatch.setattr(job_store, "is_job_cancelled", lambda job_id: False)
    completed: dict = {}
    monkeypatch.setattr(
        job_store,
        "update_job",
        lambda job_id, **kw: completed.update(kw) if kw.get("status") else None,
    )

    class _Result:
        def model_dump(self):
            return {"ok": True}

    class _Orchestrator:
        def run(self, **kwargs):
            return _Result()

    monkeypatch.setattr(api_main, "orchestrator", _Orchestrator())

    _call_run_core()

    assert completed.get("status") == job_store.JOB_STATUS_COMPLETED

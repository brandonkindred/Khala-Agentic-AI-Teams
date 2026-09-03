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

from branding_team.api import background as background_mod
from branding_team.api import main as api_main
from branding_team.models import BrandPhase, HumanReview
from branding_team.shared import job_store
from branding_team.tests.conftest import make_mission
from branding_team.tests.test_orchestrator import _full_strategic_core


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
    monkeypatch.setattr(job_store, "update_job_if_not_cancelled", lambda *a, **kw: True)

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
    completed: dict = {}

    def _fake_update_if_not_cancelled(job_id, **kw):
        if kw.get("status"):
            completed.update(kw)
        return True

    monkeypatch.setattr(job_store, "update_job_if_not_cancelled", _fake_update_if_not_cancelled)

    class _Result:
        def model_dump(self):
            return {"ok": True}

    class _Orchestrator:
        def run(self, **kwargs):
            return _Result()

    monkeypatch.setattr(api_main, "orchestrator", _Orchestrator())

    _call_run_core()

    assert completed.get("status") == job_store.JOB_STATUS_COMPLETED


def test_run_branding_core_mid_run_cancel_stops_remaining_phases(monkeypatch) -> None:
    """A cancel detected between phases stops the thread-mode job from issuing
    any further phase, symmetric to
    test_temporal_unit.py::test_workflow_cancel_between_phases_skips_finalize.

    Exercises the real should_continue gate (BrandingTeamOrchestrator.run /
    _run_phases_with_cache, story #7837) through the real background-path
    wiring (_run_branding_core -> _job_not_cancelled, story #7849) -- only
    run_single_phase (the LLM/agent boundary) is faked, not the orchestrator
    itself.
    """
    monkeypatch.setattr(api_main, "_job_manager", None)

    class _CancelState:
        def __init__(self) -> None:
            self.is_cancelled_calls = 0
            self.cancelled = False

    state = _CancelState()

    def _fake_is_job_cancelled(job_id: str) -> bool:
        state.is_cancelled_calls += 1
        # Not cancelled for the check before strategic_core; cancelled from
        # the check before narrative_messaging on -- mirrors
        # _drive_workflow(cancel_after=1) in test_temporal_unit.py.
        if state.is_cancelled_calls > 1:
            state.cancelled = True
        return state.cancelled

    monkeypatch.setattr(background_mod, "is_job_cancelled", _fake_is_job_cancelled)

    job_store_calls: list[dict] = []

    def _fake_update_if_not_cancelled(job_id, **kw):
        if state.cancelled:
            return False
        job_store_calls.append(kw)
        return True

    monkeypatch.setattr(job_store, "update_job_if_not_cancelled", _fake_update_if_not_cancelled)

    phases_run: list[str] = []

    def _fake_run_single_phase(mission, phase, prior_outputs=None):
        phases_run.append(phase.value)
        return _full_strategic_core(), False

    monkeypatch.setattr(api_main.orchestrator, "run_single_phase", _fake_run_single_phase)

    api_main._run_branding_core(
        job_id="job-cancel-mid-run",
        mission=make_mission(),
        human_review=HumanReview(approved=True),
        brand_checks=[],
        client_id=None,
        brand_id=None,
        include_market_research=False,
        include_design_assets=False,
        target_phase=None,
    )

    assert phases_run == [BrandPhase.STRATEGIC_CORE.value]
    assert job_store_calls == [{"status": job_store.JOB_STATUS_RUNNING}]

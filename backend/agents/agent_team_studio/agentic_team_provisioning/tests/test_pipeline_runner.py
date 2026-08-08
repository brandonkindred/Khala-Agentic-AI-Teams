"""Tests for WAIT-state bounding + restart reliability in ``PipelineRunner``.

The pipeline runner takes its store by constructor injection, and the shared
``_fake_postgres`` fake does not model the ``agentic_test_pipeline_runs`` table, so
these tests inject a small in-memory ``_FakeStore`` that mirrors the real store's
compare-and-swap semantics (single winner out of ``waiting_for_input``) and
heartbeat/reaper behaviour. Timeouts and poll intervals are set tiny so the
background-thread cases finish in milliseconds.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent_team_studio.agentic_team_provisioning.models import (
    AgenticTeamAgent,
    ProcessDefinition,
    ProcessStep,
    ProcessStepAgent,
    StepType,
)
from agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner import PipelineRunner
from shared.concurrency import BackgroundHeartbeat


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class _FakeStore:
    """In-memory stand-in for ``AgenticTestStore``'s pipeline-run surface.

    Mirrors the real store's row-level compare-and-swap: the terminal transition out
    of ``waiting_for_input`` is serialized under a lock so exactly one caller wins,
    matching Postgres' single-row UPDATE semantics.
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- helpers used by tests -----------------------------------------
    def seed(self, run_id: str, **fields) -> None:
        row = {
            "run_id": run_id,
            "team_id": "t1",
            "process_id": "p1",
            "status": "running",
            "current_step_id": None,
            "initial_input": "",
            "step_results": [],
            "human_prompt": None,
            "human_input": None,
            "error": None,
            "started_at": _now(),
            "finished_at": None,
            "heartbeat_at": None,
        }
        row.update(fields)
        self._rows[run_id] = row

    # -- surface consumed by PipelineRunner ----------------------------
    def get_pipeline_run(self, run_id: str):
        row = self._rows.get(run_id)
        return dict(row) if row else None

    def update_pipeline_run(self, run_id: str, **fields) -> bool:
        with self._lock:
            row = self._rows.get(run_id)
            if not row:
                return False
            row.update(fields)
            return True

    def try_resume_pipeline_run(self, run_id: str, human_input: str, stale_seconds: int) -> bool:
        cutoff = _now() - timedelta(seconds=stale_seconds)
        with self._lock:
            row = self._rows.get(run_id)
            if not row or row["status"] != "waiting_for_input":
                return False
            hb = row.get("heartbeat_at")
            if hb is None or hb < cutoff:
                return False  # orphaned (stale heartbeat) -> not resumable
            row.update(
                status="running",
                human_prompt=None,
                human_input=human_input,
                heartbeat_at=_now(),
            )
            return True

    def try_expire_pipeline_run(self, run_id: str, error: str) -> bool:
        with self._lock:
            row = self._rows.get(run_id)
            if not row or row["status"] != "waiting_for_input":
                return False
            row.update(status="failed", error=error, finished_at=_now())
            return True

    def try_complete_pipeline_run(self, run_id: str, step_results: list) -> bool:
        with self._lock:
            row = self._rows.get(run_id)
            if not row or row["status"] != "running":
                return False
            row.update(status="completed", step_results=step_results, finished_at=_now())
            return True

    def try_cancel_pipeline_run(self, run_id: str) -> bool:
        with self._lock:
            row = self._rows.get(run_id)
            if not row or row["status"] not in ("running", "waiting_for_input"):
                return False
            row.update(status="cancelled", finished_at=_now())
            return True

    def try_fail_pipeline_run(self, run_id: str, error: str) -> bool:
        with self._lock:
            row = self._rows.get(run_id)
            if not row or row["status"] not in ("running", "waiting_for_input"):
                return False
            row.update(status="failed", error=error, finished_at=_now())
            return True

    def get_pipeline_status(self, run_id: str):
        row = self._rows.get(run_id)
        if not row:
            return None
        return {"status": row["status"], "human_input": row.get("human_input") or ""}

    def advance_pipeline_step(self, run_id: str, step_id: str) -> bool:
        with self._lock:
            row = self._rows.get(run_id)
            if not row or row["status"] != "running":
                return False
            row["current_step_id"] = step_id
            return True

    def heartbeat_pipeline_run(self, run_id: str) -> None:
        with self._lock:
            row = self._rows.get(run_id)
            if row and row["status"] in ("running", "waiting_for_input"):
                row["heartbeat_at"] = _now()

    def reap_orphaned_pipeline_runs(self, error: str, stale_seconds: int) -> int:
        cutoff = _now() - timedelta(seconds=stale_seconds)
        reaped = 0
        with self._lock:
            for row in self._rows.values():
                if row["status"] not in ("running", "waiting_for_input"):
                    continue
                hb = row.get("heartbeat_at")
                if hb is None or hb < cutoff:
                    row.update(status="failed", error=error, finished_at=_now())
                    reaped += 1
        return reaped


def _wait_process() -> ProcessDefinition:
    return ProcessDefinition(
        process_id="p1",
        steps=[
            ProcessStep(
                step_id="s1",
                name="Ask human",
                description="Need input",
                step_type=StepType.WAIT,
            )
        ],
    )


def _make_runner(
    store: _FakeStore, *, timeout_s: float = 30.0, poll_s: float = 0.02
) -> PipelineRunner:
    runner = PipelineRunner(store, start_sweeper=False)
    # Override the env-derived bounds directly so tests run in milliseconds without
    # tripping the production floor clamps. Heartbeat far more often than the staleness
    # window so a live run always looks fresh (resume-gate + reaper both key on it).
    runner._wait_timeout_s = timeout_s
    runner._wait_poll_s = poll_s
    runner._stale_s = 1
    runner._heartbeat_interval_s = 0.02
    return runner


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_config_defaults_and_clamps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIC_TEAM_PIPELINE_WAIT_TIMEOUT_S", raising=False)
    monkeypatch.setenv("AGENTIC_TEAM_PIPELINE_WAIT_POLL_S", "garbage")
    runner = PipelineRunner(_FakeStore(), start_sweeper=False)
    assert runner._wait_timeout_s == 259200  # 72h default
    assert runner._wait_poll_s == 5  # garbage -> default
    assert runner._stale_s >= 2 * runner._wait_poll_s


def test_config_clamps_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_TEAM_PIPELINE_WAIT_TIMEOUT_S", "1")  # below floor
    monkeypatch.setenv("AGENTIC_TEAM_PIPELINE_WAIT_POLL_S", "9999")  # above ceiling
    runner = PipelineRunner(_FakeStore(), start_sweeper=False)
    assert runner._wait_timeout_s == 60  # floored
    assert runner._wait_poll_s == 60  # ceiled


# ---------------------------------------------------------------------------
# WAIT resume / timeout / cancel
# ---------------------------------------------------------------------------


def test_wait_resume_same_worker() -> None:
    store = _FakeStore()
    store.seed("r1")
    runner = _make_runner(store)
    runner.start_run("r1", [], _wait_process())

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "waiting_for_input")
    assert runner.submit_human_input("r1", "the answer") is True

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "completed")
    row = store.get_pipeline_run("r1")
    assert row["step_results"][0]["status"] == "completed"
    assert row["step_results"][0]["output"] == "the answer"


def test_wait_resume_cross_worker_via_db() -> None:
    """Submit lands on another worker: no local Event, resume comes from the DB flip."""
    store = _FakeStore()
    store.seed("r1")
    runner = _make_runner(store)
    runner.start_run("r1", [], _wait_process())

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "waiting_for_input")
    # Simulate a sibling worker resuming: flip the DB directly, do NOT set the Event.
    # The waiter's heartbeat thread keeps the run fresh, so the resume CAS accepts it.
    assert store.try_resume_pipeline_run("r1", "db answer", runner._stale_s) is True

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "completed")
    assert store.get_pipeline_run("r1")["step_results"][0]["output"] == "db answer"


def test_wait_times_out_and_fails_cleanly() -> None:
    store = _FakeStore()
    store.seed("r1")
    runner = _make_runner(store, timeout_s=0.2, poll_s=0.02)
    runner.start_run("r1", [], _wait_process())

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "failed")
    row = store.get_pipeline_run("r1")
    assert row["error"].startswith("wait_timeout:")
    assert row["finished_at"] is not None
    assert row["step_results"][0]["status"] == "timed_out"


def test_cancel_while_waiting() -> None:
    store = _FakeStore()
    store.seed("r1")
    runner = _make_runner(store)
    runner.start_run("r1", [], _wait_process())

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "waiting_for_input")
    runner.cancel_run("r1")

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "cancelled")


# ---------------------------------------------------------------------------
# submit_human_input contract + TOCTOU
# ---------------------------------------------------------------------------


def test_submit_returns_false_when_not_waiting() -> None:
    store = _FakeStore()
    store.seed("r1", status="completed")
    runner = _make_runner(store)
    assert runner.submit_human_input("r1", "late") is False
    # Status must not be forced back to running.
    assert store.get_pipeline_run("r1")["status"] == "completed"


def test_resume_and_expire_are_mutually_exclusive() -> None:
    """Compare-and-swap guarantees exactly one winner out of waiting_for_input."""
    store = _FakeStore()
    store.seed("r1", status="waiting_for_input", heartbeat_at=_now())
    assert store.try_resume_pipeline_run("r1", "x", 30) is True
    # Once resumed, the expire CAS must lose.
    assert store.try_expire_pipeline_run("r1", "wait_timeout: too late") is False
    assert store.get_pipeline_run("r1")["status"] == "running"

    store.seed("r2", status="waiting_for_input", heartbeat_at=_now())
    assert store.try_expire_pipeline_run("r2", "wait_timeout: too late") is True
    # Once expired, the resume CAS must lose (no lost input into a dead run).
    assert store.try_resume_pipeline_run("r2", "x", 30) is False


def test_resume_rejects_orphaned_stale_run() -> None:
    """A waiting run whose heartbeat has gone stale (worker died on restart) is not
    resumable — submit returns False so the endpoint 409s instead of falsely
    succeeding on a run no thread will advance."""
    store = _FakeStore()
    store.seed(
        "r1",
        status="waiting_for_input",
        heartbeat_at=_now() - timedelta(seconds=120),
    )
    runner = _make_runner(store)
    assert runner.submit_human_input("r1", "answer") is False
    assert store.get_pipeline_run("r1")["status"] == "waiting_for_input"

    # A NULL-heartbeat waiting run (pre-feature / never heartbeated) is likewise refused.
    store.seed("r2", status="waiting_for_input", heartbeat_at=None)
    assert runner.submit_human_input("r2", "answer") is False
    # Submit only refuses — it does not itself reap; the sweeper handles the row.
    assert store.get_pipeline_run("r2")["status"] == "waiting_for_input"


# ---------------------------------------------------------------------------
# Reaper (restart / orphan) safety
# ---------------------------------------------------------------------------


def test_reaper_only_reaps_stale_active_runs() -> None:
    store = _FakeStore()
    # Live sibling-worker run: fresh heartbeat -> must survive.
    store.seed("live", status="waiting_for_input", heartbeat_at=_now())
    # Orphan with a stale heartbeat -> reaped.
    store.seed("stale", status="running", heartbeat_at=_now() - timedelta(seconds=3600))
    # Orphan that never heartbeated (e.g. pre-feature row) -> reaped.
    store.seed("null_hb", status="waiting_for_input", heartbeat_at=None)
    # Already terminal -> untouched.
    store.seed("done", status="completed", heartbeat_at=None)

    runner = _make_runner(store)
    reaped = runner.reap_orphaned_runs()

    assert reaped == 2
    assert store.get_pipeline_run("live")["status"] == "waiting_for_input"
    assert store.get_pipeline_run("stale")["status"] == "failed"
    assert store.get_pipeline_run("stale")["error"].startswith("orphaned:")
    assert store.get_pipeline_run("stale")["finished_at"] is not None
    assert store.get_pipeline_run("null_hb")["status"] == "failed"
    assert store.get_pipeline_run("done")["status"] == "completed"


# ---------------------------------------------------------------------------
# ACTION / DECISION step execution
# ---------------------------------------------------------------------------


def _action_process() -> ProcessDefinition:
    return ProcessDefinition(
        process_id="p1",
        steps=[
            ProcessStep(
                step_id="s1",
                name="Do work",
                description="Run the agent",
                step_type=StepType.ACTION,
                agents=[ProcessStepAgent(agent_name="worker", role="doer")],
            )
        ],
    )


def test_action_step_runs_agent_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.build_agent",
        lambda *a, **k: object(),
    )
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.call_agent",
        lambda _agent, _inp: "agent output",
    )
    store = _FakeStore()
    store.seed("r1", initial_input="seed")
    agent = AgenticTeamAgent(agent_name="worker", role="doer")
    runner = _make_runner(store)
    runner.start_run("r1", [agent], _action_process())

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "completed")
    row = store.get_pipeline_run("r1")
    assert row["step_results"][0]["output"] == "agent output"
    assert row["finished_at"] is not None


def test_action_step_without_agent_records_placeholder() -> None:
    store = _FakeStore()
    store.seed("r1")
    runner = _make_runner(store)
    # No roster agent matches the step -> placeholder output, run still completes.
    runner.start_run("r1", [], _action_process())

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "completed")
    assert "No agent assigned" in store.get_pipeline_run("r1")["step_results"][0]["output"]


def test_decision_step_without_agent_picks_first_branch() -> None:
    store = _FakeStore()
    store.seed("r1")
    process = ProcessDefinition(
        process_id="p1",
        steps=[
            ProcessStep(
                step_id="s1",
                name="Branch",
                step_type=StepType.DECISION,
                next_steps=["s2"],  # references a step not in the DAG -> loop ends after s1
            )
        ],
    )
    runner = _make_runner(store)
    runner.start_run("r1", [], process)

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "completed")
    assert store.get_pipeline_run("r1")["step_results"][0]["output"] == "Decision: s2"


def test_cancellation_before_step_stops_run() -> None:
    store = _FakeStore()
    store.seed("r1", status="cancelled")
    runner = _make_runner(store)
    # The loop's pre-step cancellation check should return immediately.
    runner.start_run("r1", [], _action_process())

    # Give the worker thread a moment; status must stay cancelled (never completed).
    time.sleep(0.1)
    assert store.get_pipeline_run("r1")["status"] == "cancelled"


def test_run_failure_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.build_agent", _boom
    )
    store = _FakeStore()
    store.seed("r1")
    agent = AgenticTeamAgent(agent_name="worker", role="doer")
    runner = _make_runner(store)
    runner.start_run("r1", [agent], _action_process())

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "failed")
    assert store.get_pipeline_run("r1")["error"] == "kaboom"


def test_long_step_is_not_false_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow synchronous step keeps heartbeating (background thread), so a concurrent
    reap with a short staleness window must NOT fail the live run."""
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.build_agent",
        lambda *a, **k: object(),
    )

    def _slow(_agent, _inp):
        time.sleep(0.3)
        return "done"

    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.call_agent", _slow
    )
    store = _FakeStore()
    # Mirror create_pipeline_run, which seeds heartbeat_at so a brand-new run is never
    # reaped in the window before the background heartbeat thread's first tick.
    store.seed("r1", heartbeat_at=_now())
    agent = AgenticTeamAgent(agent_name="worker", role="doer")
    runner = _make_runner(store)
    # Staleness far shorter than the step; heartbeat interval far shorter than staleness.
    runner._stale_s = 0.1
    runner._heartbeat_interval_s = 0.02
    runner.start_run("r1", [agent], _action_process())

    # Reap repeatedly while the 0.3s step runs; the live run must survive.
    for _ in range(6):
        assert runner.reap_orphaned_runs() == 0
        assert store.get_pipeline_run("r1")["status"] != "failed"
        time.sleep(0.05)

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "completed")


def test_out_of_band_termination_is_not_resurrected(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a run is terminated (e.g. reaped) mid-step, the executor must not clobber it
    back to completed via the final write."""
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.build_agent",
        lambda *a, **k: object(),
    )
    store = _FakeStore()
    store.seed("r1")

    def _reap_midstep(_agent, _inp):
        # Simulate a concurrent reap/expire flipping the run to a terminal state while
        # the (last) step is still executing.
        store._rows["r1"]["status"] = "failed"
        store._rows["r1"]["error"] = "orphaned: reaped"
        return "out"

    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.call_agent",
        _reap_midstep,
    )
    agent = AgenticTeamAgent(agent_name="worker", role="doer")
    runner = _make_runner(store)
    runner.start_run("r1", [agent], _action_process())

    # The final try_complete CAS must lose (status != running), so the run stays failed.
    time.sleep(0.2)
    row = store.get_pipeline_run("r1")
    assert row["status"] == "failed"
    assert row["error"] == "orphaned: reaped"


def test_cancel_does_not_clobber_completed_run() -> None:
    """cancel_run is a compare-and-swap: cancelling an already-terminal run is a no-op."""
    store = _FakeStore()
    store.seed("r1", status="completed", finished_at=_now())
    runner = _make_runner(store)
    runner.cancel_run("r1")
    assert store.get_pipeline_run("r1")["status"] == "completed"


def test_cancelled_wait_step_is_reconciled() -> None:
    """When a waiting run is cancelled, the pending WAIT step_result is marked so the
    audit panel doesn't show a step still 'waiting' under a cancelled run."""
    store = _FakeStore()
    store.seed("r1")
    runner = _make_runner(store)
    runner.start_run("r1", [], _wait_process())

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "waiting_for_input")
    runner.cancel_run("r1")

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "cancelled")
    assert _wait_for(
        lambda: store.get_pipeline_run("r1")["step_results"][0]["status"] == "cancelled"
    )


# ---------------------------------------------------------------------------
# Background sweeper (BackgroundHeartbeat-driven _sweep_once beat)
# ---------------------------------------------------------------------------


def test_sweep_once_reaps_only_stale_orphans() -> None:
    store = _FakeStore()
    store.seed("live", status="waiting_for_input", heartbeat_at=_now())
    store.seed("orphan", status="running", heartbeat_at=_now() - timedelta(seconds=3600))
    runner = _make_runner(store)
    runner._sweep_once()
    assert store.get_pipeline_run("orphan")["status"] == "failed"
    assert store.get_pipeline_run("live")["status"] == "waiting_for_input"


def test_sweep_once_propagates_reap_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """_sweep_once must NOT swallow a reap error — the BackgroundHeartbeat driver's
    on_error handles it and keeps ticking, so swallowing here would hide failures."""
    store = _FakeStore()
    runner = _make_runner(store)

    def _boom() -> int:
        raise RuntimeError("transient")

    monkeypatch.setattr(runner, "reap_orphaned_runs", _boom)
    with pytest.raises(RuntimeError):
        runner._sweep_once()


def test_sweeper_reaps_orphans_periodically() -> None:
    """End-to-end: the BackgroundHeartbeat driving _sweep_once reaps on its interval."""
    store = _FakeStore()
    store.seed("orphan", status="waiting_for_input", heartbeat_at=None)
    runner = _make_runner(store)
    sweeper = BackgroundHeartbeat(
        runner._sweep_once, 0.02, stop_event=runner._sweeper_stop, on_error=lambda _e: None
    )
    sweeper.start()
    try:
        assert _wait_for(lambda: store.get_pipeline_run("orphan")["status"] == "failed")
    finally:
        sweeper.stop()


def test_constructor_start_sweeper_true_starts_thread_and_reaps_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The start_sweeper=True constructor path (gated on is_postgres_enabled) actually
    starts the orphan-sweeper daemon thread, and that thread reaps orphans on its own
    cadence — not just when _sweep_once is called directly or driven manually."""
    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner.is_postgres_enabled",
        lambda: True,
    )
    # Shrink the sweeper's interval (== _stale_s) before construction — it's baked
    # into the BackgroundHeartbeat built inside __init__, so post-construction
    # attribute overrides (the _make_runner pattern) can't reach it here.
    monkeypatch.setenv("AGENTIC_TEAM_PIPELINE_WAIT_POLL_S", "1")
    monkeypatch.setenv("AGENTIC_TEAM_PIPELINE_STALE_S", "3")

    store = _FakeStore()
    store.seed("orphan", status="waiting_for_input", heartbeat_at=None)

    runner = PipelineRunner(store, start_sweeper=True)
    try:
        assert any(t.name == "pipeline-orphan-sweeper" for t in threading.enumerate())
        assert _wait_for(
            lambda: store.get_pipeline_run("orphan")["status"] == "failed", timeout=10.0
        )
    finally:
        runner._sweeper_stop.set()
        assert _wait_for(
            lambda: not any(t.name == "pipeline-orphan-sweeper" for t in threading.enumerate())
        )

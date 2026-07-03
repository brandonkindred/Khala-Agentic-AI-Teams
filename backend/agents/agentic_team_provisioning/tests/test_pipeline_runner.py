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

from agentic_team_provisioning.models import (
    AgenticTeamAgent,
    ProcessDefinition,
    ProcessStep,
    ProcessStepAgent,
    StepType,
)
from agentic_team_provisioning.runtime.pipeline_runner import PipelineRunner


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

    def try_resume_pipeline_run(self, run_id: str, human_input: str) -> bool:
        with self._lock:
            row = self._rows.get(run_id)
            if not row or row["status"] != "waiting_for_input":
                return False
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

    def consume_pipeline_human_input(self, run_id: str) -> str:
        row = self._rows.get(run_id)
        if not row:
            return ""
        return row.get("human_input") or ""

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
    # tripping the production floor clamps.
    runner._wait_timeout_s = timeout_s
    runner._wait_poll_s = poll_s
    runner._stale_s = 1
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
    assert store.try_resume_pipeline_run("r1", "db answer") is True

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
    store.seed("r1", status="waiting_for_input")
    assert store.try_resume_pipeline_run("r1", "x") is True
    # Once resumed, the expire CAS must lose.
    assert store.try_expire_pipeline_run("r1", "wait_timeout: too late") is False
    assert store.get_pipeline_run("r1")["status"] == "running"

    store.seed("r2", status="waiting_for_input")
    assert store.try_expire_pipeline_run("r2", "wait_timeout: too late") is True
    # Once expired, the resume CAS must lose (no lost input into a dead run).
    assert store.try_resume_pipeline_run("r2", "x") is False
    assert store.get_pipeline_run("r2")["status"] == "failed"


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
        "agentic_team_provisioning.runtime.pipeline_runner.build_agent",
        lambda *a, **k: object(),
    )
    monkeypatch.setattr(
        "agentic_team_provisioning.runtime.pipeline_runner.call_agent",
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

    monkeypatch.setattr("agentic_team_provisioning.runtime.pipeline_runner.build_agent", _boom)
    store = _FakeStore()
    store.seed("r1")
    agent = AgenticTeamAgent(agent_name="worker", role="doer")
    runner = _make_runner(store)
    runner.start_run("r1", [agent], _action_process())

    assert _wait_for(lambda: store.get_pipeline_run("r1")["status"] == "failed")
    assert store.get_pipeline_run("r1")["error"] == "kaboom"


# ---------------------------------------------------------------------------
# Background sweeper
# ---------------------------------------------------------------------------


def test_sweeper_reaps_orphans_periodically() -> None:
    store = _FakeStore()
    store.seed("orphan", status="waiting_for_input", heartbeat_at=None)
    runner = _make_runner(store)
    runner._stale_s = 0.02
    thread = threading.Thread(target=runner._run_sweeper, daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: store.get_pipeline_run("orphan")["status"] == "failed")
    finally:
        runner._sweeper_stop.set()
        thread.join(timeout=2)


def test_sweeper_survives_a_reap_error(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    runner = _make_runner(store)
    runner._stale_s = 0.02
    calls = {"n": 0}

    def _flaky() -> int:
        calls["n"] += 1
        raise RuntimeError("transient")

    monkeypatch.setattr(runner, "reap_orphaned_runs", _flaky)
    thread = threading.Thread(target=runner._run_sweeper, daemon=True)
    thread.start()
    try:
        # The sweeper must keep ticking despite the reap raising each time.
        assert _wait_for(lambda: calls["n"] >= 2)
        assert thread.is_alive()
    finally:
        runner._sweeper_stop.set()
        thread.join(timeout=2)

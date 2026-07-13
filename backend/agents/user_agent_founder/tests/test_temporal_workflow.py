"""Tests for ``UserAgentFounderWorkflow`` orchestration.

The workflow body is exercised directly with ``workflow.execute_activity`` and
``workflow.sleep`` monkeypatched to a recorder returning canned activity results
(no Temporal server). This pins the deterministic control flow ported from
``orchestrator._run_phase``: phase ordering, resume short-circuits
(``skip_spec``/``skip_analysis`` and existing per-phase job ids), the
poll→answer→sleep loop, the per-question-set answer-retry abort, terminal
``failed``/``cancelled``/timeout → catch-all ``mark_failed`` + re-raise, the
cancel-signal early-exit (no FAILED write), the progress query, and per-activity
retry policies.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from user_agent_founder.temporal import activities as acts
from user_agent_founder.temporal import workflows as wf


def _snap(**over):
    """Return a ``begin_run`` snapshot dict (poll intervals 0 so patched sleeps
    are instant); override any field via kwargs."""
    s = dict(
        skip_spec=False,
        skip_analysis=False,
        analysis_job_id=None,
        build_job_id=None,
        repo_path=None,
        project_name="proj",
        target_team_key="software_engineering",
        adapter_display_name="Software Engineering",
        analysis_poll_interval=0,
        build_poll_interval=0,
        max_poll_attempts=50,
        max_answer_retries=1,
    )
    s.update(over)
    return s


def _running():
    """A normalized poll verdict for a still-running phase (not terminal, no questions)."""
    return {"status": "running", "waiting": False, "poll_error": None}


def _completed(repo_path=None):
    """A normalized poll verdict for a completed phase, carrying the repo_path handoff."""
    return {"status": "completed", "waiting": False, "poll_error": None, "repo_path": repo_path}


def _waiting(qid="q1"):
    """A normalized poll verdict where the target is waiting on one pending question."""
    return {
        "status": "waiting_for_answers",
        "waiting": True,
        "poll_error": None,
        "pending_questions": [{"id": qid}],
    }


class _ActivityRecorder:
    """Fake ``workflow.execute_activity`` returning canned per-activity results."""

    def __init__(
        self,
        *,
        snap,
        analysis_polls=None,
        build_polls=None,
        answer_ok=True,
        begin_exc=None,
        mark_failed_exc=None,
    ):
        self.snap = snap
        self.polls = {"analysis": list(analysis_polls or []), "build": list(build_polls or [])}
        self.answer_ok = answer_ok
        self.begin_exc = begin_exc
        self.mark_failed_exc = mark_failed_exc
        self.calls = []  # (fn, args, retry_policy)
        self.inst = None

    async def execute_activity(self, fn, *args, **kw):
        a = kw.get("args")
        self.calls.append((fn, a, kw.get("retry_policy"), kw.get("heartbeat_timeout")))
        if fn is acts.begin_run_activity:
            if self.begin_exc is not None:
                raise self.begin_exc
            return dict(self.snap)
        if fn is acts.generate_spec_activity:
            return {"chars": 12, "skipped": False}
        if fn is acts.enter_phase_activity:
            # args = [run_id, phase, existing_job_id]; resume returns the existing id.
            return {"job_id": a[2] or f"{a[1]}-job"}
        if fn is acts.poll_phase_activity:
            phase = a[1]
            queue = self.polls[phase]
            assert queue, f"no more {phase} polls scripted"
            r = queue.pop(0)
            if r.get("_cancel"):
                self.inst._cancel_requested = True
            return r
        if fn is acts.answer_questions_activity:
            return {"ok": self.answer_ok}
        if fn is acts.finalize_run_activity:
            return {"run_id": a[0]}
        if fn is acts.mark_failed_activity:
            if self.mark_failed_exc is not None:
                raise self.mark_failed_exc
            return {"marked": True}
        raise AssertionError(f"unexpected activity: {fn}")

    def names(self):
        return [f.__name__ for (f, _a, _r, _h) in self.calls]

    def count(self, fn):
        return sum(1 for (f, _a, _r, _h) in self.calls if f is fn)

    def args_for(self, fn):
        return [a for (f, a, _r, _h) in self.calls if f is fn]

    def retry_for(self, fn):
        return [r for (f, _a, r, _h) in self.calls if f is fn]

    def heartbeat_for(self, fn):
        return [h for (f, _a, _r, h) in self.calls if f is fn]


def _prepare(monkeypatch, inst, rec):
    rec.inst = inst
    monkeypatch.setattr(wf.workflow, "execute_activity", rec.execute_activity)

    async def _wait_condition(fn, *, timeout=None, timeout_summary=None):
        # Mirrors the real semantics being faked: return immediately if the
        # condition is already true (an instant wake, e.g. a cancel that landed
        # earlier in this same simulated tick); otherwise behave as though the
        # durable timer elapsed with the condition still false.
        if fn():
            return
        raise asyncio.TimeoutError()

    monkeypatch.setattr(wf.workflow, "wait_condition", _wait_condition)
    # ``workflow.logger`` requires a live workflow event loop; swap in a plain
    # logger so the cancel/failure log lines don't raise outside the sandbox.
    monkeypatch.setattr(wf.workflow, "logger", logging.getLogger("uaf-workflow-test"))


def _run(monkeypatch, rec, *, cancel_before=False):
    inst = wf.UserAgentFounderWorkflow()
    if cancel_before:
        inst._cancel_requested = True
    _prepare(monkeypatch, inst, rec)
    return inst, asyncio.run(inst.run("r1"))


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_full_pipeline_order(monkeypatch):
    rec = _ActivityRecorder(
        snap=_snap(),
        analysis_polls=[_running(), _completed("/repo")],
        build_polls=[_running(), _completed()],
    )
    _inst, out = _run(monkeypatch, rec)

    assert out == {"run_id": "r1"}
    names = rec.names()
    assert names[0] == "begin_run_activity"
    assert names[-1] == "finalize_run_activity"
    assert rec.count(acts.generate_spec_activity) == 1
    assert rec.count(acts.enter_phase_activity) == 2  # analysis + build
    assert rec.count(acts.poll_phase_activity) == 4
    assert rec.count(acts.mark_failed_activity) == 0


def test_resume_skips_spec_and_analysis(monkeypatch):
    rec = _ActivityRecorder(
        snap=_snap(skip_spec=True, skip_analysis=True), build_polls=[_completed()]
    )
    _inst, out = _run(monkeypatch, rec)

    assert out == {"run_id": "r1"}
    assert rec.count(acts.generate_spec_activity) == 0
    # Only the build phase is entered; analysis is short-circuited entirely.
    assert rec.count(acts.enter_phase_activity) == 1
    assert rec.args_for(acts.enter_phase_activity)[0] == ["r1", "build", None]


def test_resume_existing_job_ids_enter_without_restart(monkeypatch):
    rec = _ActivityRecorder(
        snap=_snap(analysis_job_id="aj", build_job_id="bj"),
        analysis_polls=[_completed("/repo")],
        build_polls=[_completed()],
    )
    _inst, out = _run(monkeypatch, rec)

    assert out == {"run_id": "r1"}
    # Both phases are entered with their persisted job ids (enter_phase does the
    # resume status transition without re-submitting to the target).
    assert rec.count(acts.enter_phase_activity) == 2
    assert rec.args_for(acts.enter_phase_activity)[0] == ["r1", "analysis", "aj"]
    assert rec.args_for(acts.enter_phase_activity)[1] == ["r1", "build", "bj"]
    # Poll targets the persisted job ids.
    assert rec.args_for(acts.poll_phase_activity)[0] == ["r1", "analysis", "aj"]


def test_waiting_triggers_answer_then_completes(monkeypatch):
    rec = _ActivityRecorder(
        snap=_snap(),
        analysis_polls=[_waiting("q1"), _completed("/repo")],
        build_polls=[_completed()],
    )
    _inst, out = _run(monkeypatch, rec)

    assert out == {"run_id": "r1"}
    assert rec.count(acts.answer_questions_activity) == 1
    assert rec.args_for(acts.answer_questions_activity)[0] == [
        "r1",
        "analysis",
        "analysis-job",
        [{"id": "q1"}],
    ]


# ---------------------------------------------------------------------------
# Failure / abort paths → catch-all mark_failed + re-raise
# ---------------------------------------------------------------------------


def test_answer_retry_budget_exhausted_aborts(monkeypatch):
    rec = _ActivityRecorder(
        snap=_snap(max_answer_retries=1),
        analysis_polls=[_waiting(), _waiting(), _waiting()],
        answer_ok=False,
    )
    inst = wf.UserAgentFounderWorkflow()
    _prepare(monkeypatch, inst, rec)
    with pytest.raises(wf._PhaseFailed) as ei:
        asyncio.run(inst.run("r1"))
    assert "Aborting" in str(ei.value)
    assert rec.count(acts.mark_failed_activity) == 1
    # Two failed answer attempts before the third observation aborts.
    assert rec.count(acts.answer_questions_activity) == 2


def test_target_failed_marks_failed(monkeypatch):
    rec = _ActivityRecorder(
        snap=_snap(), analysis_polls=[{"status": "failed", "waiting": False, "error": "boom"}]
    )
    inst = wf.UserAgentFounderWorkflow()
    _prepare(monkeypatch, inst, rec)
    with pytest.raises(wf._PhaseFailed) as ei:
        asyncio.run(inst.run("r1"))
    assert "Product analysis failed" in str(ei.value)
    assert rec.count(acts.mark_failed_activity) == 1


def test_target_failed_with_none_error_falls_back_to_unknown(monkeypatch):
    """poll_phase_activity's dict always carries the 'error' key (None when the
    target omitted it) — r.get('error', 'unknown') would never fall back, so the
    workflow must use r.get('error') or 'unknown' to avoid showing 'None'."""
    rec = _ActivityRecorder(
        snap=_snap(), analysis_polls=[{"status": "failed", "waiting": False, "error": None}]
    )
    inst = wf.UserAgentFounderWorkflow()
    _prepare(monkeypatch, inst, rec)
    with pytest.raises(wf._PhaseFailed) as ei:
        asyncio.run(inst.run("r1"))
    assert "unknown" in str(ei.value)
    assert "None" not in str(ei.value)


def test_target_cancelled_marks_failed(monkeypatch):
    rec = _ActivityRecorder(
        snap=_snap(), analysis_polls=[{"status": "cancelled", "waiting": False}]
    )
    inst = wf.UserAgentFounderWorkflow()
    _prepare(monkeypatch, inst, rec)
    with pytest.raises(wf._PhaseFailed) as ei:
        asyncio.run(inst.run("r1"))
    assert "was cancelled" in str(ei.value)
    assert rec.count(acts.mark_failed_activity) == 1


def test_poll_timeout_marks_failed(monkeypatch):
    rec = _ActivityRecorder(snap=_snap(max_poll_attempts=1), analysis_polls=[_running()])
    inst = wf.UserAgentFounderWorkflow()
    _prepare(monkeypatch, inst, rec)
    with pytest.raises(wf._PhaseFailed) as ei:
        asyncio.run(inst.run("r1"))
    assert "timed out" in str(ei.value)
    assert rec.count(acts.mark_failed_activity) == 1


def test_begin_failure_marks_failed(monkeypatch):
    rec = _ActivityRecorder(snap=_snap(), begin_exc=RuntimeError("begin boom"))
    inst = wf.UserAgentFounderWorkflow()
    _prepare(monkeypatch, inst, rec)
    with pytest.raises(RuntimeError):
        asyncio.run(inst.run("r1"))
    assert rec.count(acts.mark_failed_activity) == 1


def test_poll_error_is_retried_not_fatal(monkeypatch):
    rec = _ActivityRecorder(
        snap=_snap(),
        analysis_polls=[
            {"poll_error": "transient", "status": "", "waiting": False},
            _completed("/repo"),
        ],
        build_polls=[_completed()],
    )
    _inst, out = _run(monkeypatch, rec)

    assert out == {"run_id": "r1"}
    # The poll-error tick is skipped and re-polled, not treated as terminal.
    assert rec.count(acts.poll_phase_activity) == 3
    assert rec.count(acts.mark_failed_activity) == 0


def test_mark_failed_own_failure_is_swallowed(monkeypatch):
    """If the FAILED write itself fails, the original error is still re-raised
    (the catch-all must never mask the pipeline error with a bookkeeping error)."""
    rec = _ActivityRecorder(
        snap=_snap(),
        analysis_polls=[{"status": "failed", "waiting": False, "error": "boom"}],
        mark_failed_exc=RuntimeError("store down"),
    )
    inst = wf.UserAgentFounderWorkflow()
    _prepare(monkeypatch, inst, rec)
    with pytest.raises(wf._PhaseFailed):
        asyncio.run(inst.run("r1"))
    assert rec.count(acts.mark_failed_activity) == 1


# ---------------------------------------------------------------------------
# Cancellation via signal
# ---------------------------------------------------------------------------


def test_cancel_before_run_short_circuits(monkeypatch):
    rec = _ActivityRecorder(
        snap=_snap(), analysis_polls=[_completed("/repo")], build_polls=[_completed()]
    )
    inst, out = _run(monkeypatch, rec, cancel_before=True)

    assert out == {"run_id": "r1", "cancelled": True}
    # begin ran, but nothing after the post-begin cancel check.
    assert rec.count(acts.generate_spec_activity) == 0
    assert rec.count(acts.finalize_run_activity) == 0
    assert rec.count(acts.mark_failed_activity) == 0
    assert inst.progress()["phase"] == "cancelled"


def test_cancel_mid_poll_loop_short_circuits(monkeypatch):
    running_then_cancel = {**_running(), "_cancel": True}
    rec = _ActivityRecorder(
        snap=_snap(), analysis_polls=[running_then_cancel], build_polls=[_completed()]
    )
    inst, out = _run(monkeypatch, rec)

    assert out == {"run_id": "r1", "cancelled": True}
    # Cancel observed during analysis; build never starts, no FAILED write.
    assert rec.count(acts.poll_phase_activity) == 1
    assert rec.count(acts.mark_failed_activity) == 0
    assert inst.progress()["cancel_requested"] is True


def test_cancel_after_analysis_completes_stops_build(monkeypatch):
    """A cancel observed exactly when the analysis poll reports 'completed' must
    stop the build phase from being entered — _run_phase's own cancel checks
    never see a cancellation delivered on its own terminal poll, since it
    returns immediately on status=='completed' with no further check."""
    completed_then_cancel = {**_completed("/repo"), "_cancel": True}
    rec = _ActivityRecorder(
        snap=_snap(), analysis_polls=[completed_then_cancel], build_polls=[_completed()]
    )
    inst, out = _run(monkeypatch, rec)

    assert out == {"run_id": "r1", "cancelled": True}
    # enter_phase ran once for analysis; build is never entered.
    assert rec.count(acts.enter_phase_activity) == 1
    assert rec.args_for(acts.enter_phase_activity)[0][1] == "analysis"
    assert rec.count(acts.finalize_run_activity) == 0
    assert rec.count(acts.mark_failed_activity) == 0


def test_cancel_after_build_completes_stops_finalize(monkeypatch):
    """A cancel observed exactly when the build poll reports 'completed' must
    stop finalize_run_activity from overwriting the terminal cancel state the
    API route already recorded."""
    completed_then_cancel = {**_completed(), "_cancel": True}
    rec = _ActivityRecorder(snap=_snap(skip_analysis=True), build_polls=[completed_then_cancel])
    inst, out = _run(monkeypatch, rec)

    assert out == {"run_id": "r1", "cancelled": True}
    assert rec.count(acts.finalize_run_activity) == 0
    assert rec.count(acts.mark_failed_activity) == 0


def test_cancel_before_answering_stops_answer_activity(monkeypatch):
    """A cancel observed right after a poll reports 'waiting' must stop the
    autonomous-answer round from running against an already-cancelled target."""
    waiting_then_cancel = {**_waiting("q1"), "_cancel": True}
    rec = _ActivityRecorder(
        snap=_snap(), analysis_polls=[waiting_then_cancel], build_polls=[_completed()]
    )
    inst, out = _run(monkeypatch, rec)

    assert out == {"run_id": "r1", "cancelled": True}
    assert rec.count(acts.answer_questions_activity) == 0


# ---------------------------------------------------------------------------
# Signal / query handlers + retry policies
# ---------------------------------------------------------------------------


def test_signal_and_query_handlers():
    inst = wf.UserAgentFounderWorkflow()
    assert inst.progress() == {"phase": "starting", "attempt": 0, "cancel_requested": False}
    inst.cancel()
    assert inst.progress()["cancel_requested"] is True


def test_activities_use_expected_retry_policies(monkeypatch):
    rec = _ActivityRecorder(
        snap=_snap(),
        analysis_polls=[_waiting(), _completed("/repo")],
        build_polls=[_completed()],
    )
    _run(monkeypatch, rec)

    assert rec.retry_for(acts.begin_run_activity) == [wf.IO_RETRY]
    assert rec.retry_for(acts.generate_spec_activity) == [wf.LLM_RETRY]
    assert rec.retry_for(acts.enter_phase_activity) == [wf.IO_RETRY, wf.IO_RETRY]
    poll_retries = rec.retry_for(acts.poll_phase_activity)
    assert poll_retries and all(r is wf.IO_RETRY for r in poll_retries)
    # Answering is not idempotent → a single attempt (no auto-retry).
    assert rec.retry_for(acts.answer_questions_activity) == [wf.ANSWER_RETRY]
    assert rec.retry_for(acts.finalize_run_activity) == [wf.IO_RETRY]


def test_spec_and_answer_activities_carry_a_heartbeat_timeout(monkeypatch):
    """Both long, LLM-calling activities (spec generation, answering) must be
    scheduled with a heartbeat_timeout so a worker crash is detected well before
    the full start_to_close ceiling — and both derive from the SAME
    activities.HEARTBEAT_TIMEOUT_S so the two ends of the contract can't drift."""
    rec = _ActivityRecorder(
        snap=_snap(),
        analysis_polls=[_waiting(), _completed("/repo")],
        build_polls=[_completed()],
    )
    _run(monkeypatch, rec)

    assert rec.heartbeat_for(acts.generate_spec_activity) == [wf._SPEC_HEARTBEAT_TIMEOUT]
    assert rec.heartbeat_for(acts.answer_questions_activity) == [wf._ANSWER_HEARTBEAT_TIMEOUT]
    assert wf._SPEC_HEARTBEAT_TIMEOUT == wf._ANSWER_HEARTBEAT_TIMEOUT
    # begin_run/enter_phase/poll_phase/finalize are cheap IO steps — no heartbeat.
    assert rec.heartbeat_for(acts.begin_run_activity) == [None]
    assert rec.heartbeat_for(acts.enter_phase_activity) == [None, None]

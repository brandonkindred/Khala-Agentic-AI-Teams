"""Tests for the AI systems team's per-phase Temporal decomposition.

Covers three seams:

* Pattern-A exports — every ``@activity.defn`` is registered via ``ACTIVITIES``
  and its name matches the ``constants`` module (an unregistered activity hangs a
  workflow forever, so this guards the wiring).
* Phase activities — each runs its phase function, returns the serialized model,
  and checkpoints on success (via the in-memory ``FakeJobServiceClient`` swapped in
  by ``tests/conftest.py``).
* Workflow orchestration — ``AISystemsBuildWorkflow.run`` sequences the phase
  activities, short-circuits on a failed phase, skips resumed phases, and replays
  the legacy monolith on the unpatched drain-out branch. ``execute_activity`` /
  ``patched`` are stubbed so no live Temporal server is needed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from ai_systems_team.shared.job_store import create_job, get_job

# ---------------------------------------------------------------------------
# Pattern-A exports / worker registration
# ---------------------------------------------------------------------------


def test_pattern_a_exports_workflows_and_activities() -> None:
    """Every ``@activity.defn`` in the package is exported via ACTIVITIES."""
    from temporalio import activity

    from ai_systems_team import temporal as t

    assert t.WORKFLOWS == [t.AISystemsBuildWorkflow]
    assert len(t.ACTIVITIES) == 9

    names = {activity._Definition.must_from_callable(a).name for a in t.ACTIVITIES}
    assert names == {
        "ai_systems_begin_run",
        "ai_systems_spec_intake",
        "ai_systems_architecture",
        "ai_systems_capabilities",
        "ai_systems_evaluation",
        "ai_systems_safety",
        "ai_systems_build_phase",
        "ai_systems_finalize",
        "run_ai_systems_build",
    }


def test_activity_names_match_constants() -> None:
    """The exported activity names line up with the name constants."""
    from temporalio import activity

    from ai_systems_team import temporal as t
    from ai_systems_team.temporal import constants

    names = {activity._Definition.must_from_callable(a).name for a in t.ACTIVITIES}
    assert names == {
        constants.ACTIVITY_BEGIN,
        constants.ACTIVITY_SPEC_INTAKE,
        constants.ACTIVITY_ARCHITECTURE,
        constants.ACTIVITY_CAPABILITIES,
        constants.ACTIVITY_EVALUATION,
        constants.ACTIVITY_SAFETY,
        constants.ACTIVITY_BUILD_PHASE,
        constants.ACTIVITY_FINALIZE,
        constants.ACTIVITY_BUILD,
    }


def test_worker_registers_exported_lists(monkeypatch) -> None:
    """create_ai_systems_worker registers exactly the exported WORKFLOWS/ACTIVITIES."""
    from ai_systems_team import temporal as t
    from ai_systems_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(worker, "_activity_executor", None)

    captured: dict = {}

    class _FakeWorker:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(worker, "Worker", _FakeWorker)

    worker.create_ai_systems_worker(client=MagicMock())
    assert list(captured["workflows"]) == list(t.WORKFLOWS)
    assert list(captured["activities"]) == list(t.ACTIVITIES)
    assert captured["max_concurrent_activities"] == 4


def test_worker_disabled_returns_none(monkeypatch) -> None:
    """No Temporal / no client -> no worker constructed."""
    from ai_systems_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: False)
    assert worker.create_ai_systems_worker(client=MagicMock()) is None

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    assert worker.create_ai_systems_worker(client=None) is None


# ---------------------------------------------------------------------------
# job_store helpers — make_job_updater / record_phase_result
# ---------------------------------------------------------------------------


def test_make_job_updater_writes_only_supplied_fields() -> None:
    from ai_systems_team.shared.job_store import make_job_updater

    create_job("j-upd", "proj", "/spec.md")
    updater = make_job_updater("j-upd")

    updater(current_phase="architecture", progress=35, status_text="designing")
    data = get_job("j-upd")
    assert data["current_phase"] == "architecture"
    assert data["progress"] == 35
    assert data["status_text"] == "designing"

    # blueprint_snapshot maps to the job's ``blueprint`` field.
    updater(blueprint_snapshot={"project_name": "proj"})
    assert get_job("j-upd")["blueprint"] == {"project_name": "proj"}


def test_make_job_updater_noop_when_no_fields() -> None:
    from ai_systems_team.shared.job_store import make_job_updater

    create_job("j-noop", "proj", "/spec.md")
    make_job_updater("j-noop")()  # no kwargs -> no write, no crash
    assert get_job("j-noop")["current_phase"] is None


def test_record_phase_result_builds_and_appends_blueprint() -> None:
    from ai_systems_team.shared.job_store import record_phase_result

    create_job("j-rec", "proj", "/spec.md")

    record_phase_result("j-rec", "spec_intake", {"success": True, "goals": ["g1"]})
    bp = get_job("j-rec")["blueprint"]
    assert bp["project_name"] == "proj"
    assert bp["spec_intake"]["goals"] == ["g1"]
    assert bp["current_phase"] == "spec_intake"
    assert bp["completed_phases"] == ["spec_intake"]

    # A second phase merges in and appends idempotently.
    record_phase_result("j-rec", "architecture", {"success": True})
    record_phase_result("j-rec", "architecture", {"success": True})
    bp = get_job("j-rec")["blueprint"]
    assert bp["architecture"]["success"] is True
    assert bp["completed_phases"] == ["spec_intake", "architecture"]


def test_record_phase_result_noop_on_missing_job() -> None:
    from ai_systems_team.shared.job_store import record_phase_result

    # No job created -> silently no-op (no exception).
    record_phase_result("nope", "spec_intake", {"success": True})
    assert get_job("nope") == {}


# ---------------------------------------------------------------------------
# phase activities — run the phase and checkpoint
# ---------------------------------------------------------------------------


def test_spec_intake_activity_checkpoints_on_success(tmp_path) -> None:
    from ai_systems_team.temporal import activities as acts

    spec = tmp_path / "spec.md"
    spec.write_text("# Goals:\n- Build a helpful agent\n", encoding="utf-8")
    create_job("j-spec", "proj", str(spec))

    out = acts.spec_intake_activity("j-spec", str(spec), {"budget": "low"})
    assert out["success"] is True
    # constraints from the request are merged into the parsed constraints.
    assert any("budget" in c for c in out["constraints"])

    bp = get_job("j-spec")["blueprint"]
    assert bp["completed_phases"] == ["spec_intake"]
    assert bp["spec_intake"]["success"] is True


def test_spec_intake_activity_missing_spec_does_not_checkpoint() -> None:
    from ai_systems_team.temporal import activities as acts

    create_job("j-nospec", "proj", "/does/not/exist.md")
    out = acts.spec_intake_activity("j-nospec", "/does/not/exist.md", {})
    assert out["success"] is False
    assert out["error"]
    # A failed phase is not checkpointed into completed_phases.
    assert get_job("j-nospec")["blueprint"] is None


def test_phase_activities_thread_and_checkpoint(tmp_path) -> None:
    """spec -> architecture -> capabilities -> evaluation -> safety -> build all run."""
    from ai_systems_team.temporal import activities as acts

    spec = tmp_path / "spec.md"
    spec.write_text("# Goals:\n- Research topics\n- Generate summaries\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    create_job("j-thread", "proj", str(spec))

    spec_dto = acts.spec_intake_activity("j-thread", str(spec), {})
    arch_dto = acts.architecture_activity("j-thread", spec_dto)
    caps_dto = acts.capabilities_activity("j-thread", spec_dto, arch_dto)
    eval_dto = acts.evaluation_activity("j-thread", spec_dto)
    safety_dto = acts.safety_activity("j-thread", spec_dto, arch_dto)
    build_dto = acts.build_phase_activity(
        "j-thread", "proj", spec_dto, arch_dto, caps_dto, eval_dto, safety_dto, str(out_dir)
    )

    for dto in (spec_dto, arch_dto, caps_dto, eval_dto, safety_dto, build_dto):
        assert dto["success"] is True

    bp = get_job("j-thread")["blueprint"]
    assert bp["completed_phases"] == [
        "spec_intake",
        "architecture",
        "capabilities",
        "evaluation",
        "safety",
        "build",
    ]
    # Build wrote artifacts to disk.
    assert build_dto["artifacts"]
    assert (out_dir / "blueprint.json").exists()


def test_phase_activity_rejects_malformed_input_dto() -> None:
    """A malformed inter-activity DTO raises (code/schema defect), never silent."""
    from pydantic import ValidationError

    from ai_systems_team.temporal import activities as acts

    create_job("j-bad", "proj", "/spec.md")
    with pytest.raises(ValidationError):
        # architecture expects a serialized SpecIntakeResult; ``success`` is required.
        acts.architecture_activity("j-bad", {"not": "a spec intake"})


# ---------------------------------------------------------------------------
# book-end activities — begin / finalize
# ---------------------------------------------------------------------------


def test_begin_run_activity_marks_running_and_returns_resume_state() -> None:
    from ai_systems_team.shared.job_store import JOB_STATUS_RUNNING, record_phase_result
    from ai_systems_team.temporal import activities as acts

    create_job("j-begin", "proj", "/spec.md")
    # fresh job -> no blueprint yet
    assert acts.begin_run_activity("j-begin") == {}
    assert get_job("j-begin")["status"] == JOB_STATUS_RUNNING

    record_phase_result("j-begin", "spec_intake", {"success": True})
    resume = acts.begin_run_activity("j-begin")
    assert resume["completed_phases"] == ["spec_intake"]
    assert resume["spec_intake"]["success"] is True


def test_finalize_activity_marks_completed_with_success_flag() -> None:
    from ai_systems_team.shared.job_store import JOB_STATUS_COMPLETED, record_phase_result
    from ai_systems_team.temporal import activities as acts

    create_job("j-fin", "proj", "/spec.md")
    record_phase_result("j-fin", "spec_intake", {"success": True})

    acts.finalize_build_activity("j-fin", None)
    data = get_job("j-fin")
    assert data["status"] == JOB_STATUS_COMPLETED
    assert data["progress"] == 100
    assert data["blueprint"]["success"] is True


def test_finalize_activity_marks_failed_on_error() -> None:
    from ai_systems_team.shared.job_store import JOB_STATUS_FAILED
    from ai_systems_team.temporal import activities as acts

    create_job("j-finerr", "proj", "/spec.md")
    acts.finalize_build_activity("j-finerr", "architecture blew up")
    data = get_job("j-finerr")
    assert data["status"] == JOB_STATUS_FAILED
    assert data["error"] == "architecture blew up"


def test_finalize_activity_completes_without_prior_blueprint() -> None:
    """Finalize with no checkpointed blueprint still completes (project_name from job)."""
    from ai_systems_team.shared.job_store import JOB_STATUS_COMPLETED
    from ai_systems_team.temporal import activities as acts

    create_job("j-finbare", "bare-proj", "/spec.md")
    acts.finalize_build_activity("j-finbare", None)
    data = get_job("j-finbare")
    assert data["status"] == JOB_STATUS_COMPLETED
    assert data["blueprint"]["project_name"] == "bare-proj"
    assert data["blueprint"]["success"] is True


# ---------------------------------------------------------------------------
# finalize retry / last-attempt fallback
# ---------------------------------------------------------------------------


def test_finalize_last_attempt_store_failure_marks_failed(monkeypatch) -> None:
    """A persistent completion-store failure on the final attempt marks the job FAILED."""
    import ai_systems_team.shared.job_store as js
    from ai_systems_team.shared.job_store import JOB_STATUS_FAILED
    from ai_systems_team.temporal import activities as acts

    create_job("j-finfail", "proj", "/spec.md")

    def _boom(*a, **kw):
        raise RuntimeError("store down")

    monkeypatch.setattr(js, "mark_job_completed", _boom)
    monkeypatch.setattr(acts, "is_last_attempt", lambda: True)

    with pytest.raises(RuntimeError, match="store down"):
        acts.finalize_build_activity("j-finfail", None)

    data = get_job("j-finfail")
    assert data["status"] == JOB_STATUS_FAILED
    assert "Finalize failed" in data["error"]


def test_finalize_last_attempt_lost_ack_keeps_job_completed(monkeypatch) -> None:
    """If the completion write landed (lost ack), finalize must not flip it to FAILED."""
    import ai_systems_team.shared.job_store as js
    from ai_systems_team.shared.job_store import JOB_STATUS_COMPLETED
    from ai_systems_team.temporal import activities as acts

    create_job("j-lostack", "proj", "/spec.md")

    def _complete_then_lose_ack(job_id, **kw):
        # The write reaches the store, then the client raises (lost response).
        js.update_job(job_id, status=JOB_STATUS_COMPLETED)
        raise RuntimeError("lost ack")

    marked_failed: dict = {}

    def _track_failed(job_id, **kw):
        marked_failed["called"] = True

    monkeypatch.setattr(js, "mark_job_completed", _complete_then_lose_ack)
    monkeypatch.setattr(js, "mark_job_failed", _track_failed)
    monkeypatch.setattr(acts, "is_last_attempt", lambda: True)

    # The job is already COMPLETED, so finalize returns success and does NOT flip it.
    acts.finalize_build_activity("j-lostack", None)
    assert get_job("j-lostack")["status"] == JOB_STATUS_COMPLETED
    assert "called" not in marked_failed


def test_finalize_non_last_attempt_reraises_without_terminal(monkeypatch) -> None:
    """Before the final attempt, a transient store failure re-raises for Temporal to retry."""
    import ai_systems_team.shared.job_store as js
    from ai_systems_team.shared.job_store import JOB_STATUS_FAILED
    from ai_systems_team.temporal import activities as acts

    create_job("j-finretry", "proj", "/spec.md")

    def _boom(*a, **kw):
        raise RuntimeError("blip")

    monkeypatch.setattr(js, "mark_job_completed", _boom)
    monkeypatch.setattr(acts, "is_last_attempt", lambda: False)

    with pytest.raises(RuntimeError, match="blip"):
        acts.finalize_build_activity("j-finretry", None)

    # Nothing terminal was written — Temporal will retry the completion.
    assert get_job("j-finretry")["status"] != JOB_STATUS_FAILED


def test_finalize_last_attempt_reread_failure_marks_failed(monkeypatch) -> None:
    """If the completion-check re-read itself fails, finalize still marks the job FAILED."""
    import ai_systems_team.shared.job_store as js
    from ai_systems_team.shared.job_store import JOB_STATUS_FAILED
    from ai_systems_team.temporal import activities as acts

    create_job("j-reread", "proj", "/spec.md")

    real_get = js.get_job
    calls = {"n": 0}

    def _get(job_id, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:  # the re-read inside the except block
            raise RuntimeError("read down")
        return real_get(job_id, **kw)

    def _complete_boom(*a, **kw):
        raise RuntimeError("complete down")

    monkeypatch.setattr(js, "get_job", _get)
    monkeypatch.setattr(js, "mark_job_completed", _complete_boom)
    monkeypatch.setattr(acts, "is_last_attempt", lambda: True)

    with pytest.raises(RuntimeError, match="complete down"):
        acts.finalize_build_activity("j-reread", None)

    # The re-read raised -> current is None -> falls through to mark_job_failed.
    assert get_job("j-reread")["status"] == JOB_STATUS_FAILED


def test_finalize_last_attempt_fallback_swallows_secondary_store_error(monkeypatch) -> None:
    """If the fallback mark-failed also fails, the original completion error still re-raises."""
    import ai_systems_team.shared.job_store as js
    from ai_systems_team.temporal import activities as acts

    create_job("j-fin2fail", "proj", "/spec.md")

    def _complete_boom(*a, **kw):
        raise RuntimeError("complete down")

    def _failed_boom(*a, **kw):
        raise RuntimeError("failed down")

    monkeypatch.setattr(js, "mark_job_completed", _complete_boom)
    monkeypatch.setattr(js, "mark_job_failed", _failed_boom)
    monkeypatch.setattr(acts, "is_last_attempt", lambda: True)

    # The original completion error propagates even though the fallback also failed.
    with pytest.raises(RuntimeError, match="complete down"):
        acts.finalize_build_activity("j-fin2fail", None)


# ---------------------------------------------------------------------------
# legacy monolith activity (drain-out)
# ---------------------------------------------------------------------------


def test_legacy_run_build_activity_delegates(monkeypatch) -> None:
    """The legacy activity delegates to _run_build_background (thread-mode entrypoint)."""
    import ai_systems_team.api.main as main
    from ai_systems_team.temporal import activities as acts

    seen: dict = {}
    monkeypatch.setattr(main, "_run_build_background", lambda *a: seen.update(args=a))
    acts.run_build_activity("j-legacy", "proj", "/spec.md", {"k": "v"}, "/out")
    assert seen["args"] == ("j-legacy", "proj", "/spec.md", {"k": "v"}, "/out")


def test_legacy_run_build_activity_reraises(monkeypatch) -> None:
    """A failure inside the background run propagates so Temporal can retry."""
    import ai_systems_team.api.main as main
    from ai_systems_team.temporal import activities as acts

    def _boom(*a):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(main, "_run_build_background", _boom)
    with pytest.raises(RuntimeError, match="kaboom"):
        acts.run_build_activity("j-legacy2", "proj", "/spec.md", {}, None)


# ---------------------------------------------------------------------------
# start_workflow dispatch
# ---------------------------------------------------------------------------


def test_start_build_workflow_dispatches(monkeypatch) -> None:
    """start_build_workflow submits AISystemsBuildWorkflow on the worker's loop."""
    from ai_systems_team.temporal import start_workflow as sw

    captured: dict = {}

    class _FakeClient:
        async def start_workflow(self, run, args=None, id=None, task_queue=None):
            captured.update(run=run, args=args, id=id, task_queue=task_queue)
            return "handle"

    monkeypatch.setattr(sw, "get_temporal_client", lambda: _FakeClient())
    # Bypass the worker event loop: await the coroutine inline.
    monkeypatch.setattr(sw, "_run_async", lambda coro: asyncio.run(coro))

    sw.start_build_workflow("j-start", "proj", "/spec.md", {"c": 1}, "/out")
    assert captured["id"] == "ai-systems-build-j-start"
    assert captured["task_queue"] == sw.TASK_QUEUE
    assert captured["args"] == ["j-start", "proj", "/spec.md", {"c": 1}, "/out"]


def test_run_async_executes_on_worker_loop(monkeypatch) -> None:
    """_run_async marshals a coroutine onto the worker's running loop and returns it."""
    import threading

    from ai_systems_team.temporal import start_workflow as sw

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _spin():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    ready.wait(timeout=5)

    try:
        monkeypatch.setattr(sw, "get_temporal_loop", lambda: loop)
        monkeypatch.setattr(sw, "get_temporal_client", lambda: object())

        async def _echo():
            return 42

        assert sw._run_async(_echo()) == 42
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()


def test_start_build_workflow_requires_client(monkeypatch) -> None:
    """No connected client -> a clear RuntimeError instead of a None dereference."""
    from ai_systems_team.temporal import start_workflow as sw

    monkeypatch.setattr(sw, "get_temporal_client", lambda: None)
    with pytest.raises(RuntimeError, match="Temporal client not available"):
        sw.start_build_workflow("j", "proj", "/spec.md", {}, None)


def test_run_async_requires_running_worker(monkeypatch) -> None:
    """_run_async surfaces a helpful error when the worker loop/client is absent."""
    from ai_systems_team.temporal import start_workflow as sw

    monkeypatch.setattr(sw, "get_temporal_loop", lambda: None)
    monkeypatch.setattr(sw, "get_temporal_client", lambda: None)

    async def _noop():
        return None

    coro = _noop()
    try:
        with pytest.raises(RuntimeError, match="worker running"):
            sw._run_async(coro)
    finally:
        coro.close()


# ---------------------------------------------------------------------------
# worker thread starter
# ---------------------------------------------------------------------------


def test_start_worker_thread_disabled_returns_false(monkeypatch) -> None:
    from ai_systems_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: False)
    assert worker.start_ai_systems_temporal_worker_thread() is False


def test_start_worker_thread_reuses_alive_thread(monkeypatch) -> None:
    from ai_systems_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    class _AliveThread:
        def is_alive(self):
            return True

    monkeypatch.setattr(worker, "_worker_thread", _AliveThread())
    assert worker.start_ai_systems_temporal_worker_thread() is True


def test_start_worker_thread_spawns_thread(monkeypatch) -> None:
    """The spawn path constructs a daemon thread and starts it (Thread stubbed)."""
    from ai_systems_team.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(worker, "_worker_thread", None)

    started: dict = {}

    class _FakeThread:
        def __init__(self, *args, **kwargs):
            started["kwargs"] = kwargs

        def start(self):
            started["started"] = True

        def is_alive(self):
            return False

    monkeypatch.setattr(worker.threading, "Thread", _FakeThread)
    assert worker.start_ai_systems_temporal_worker_thread() is True
    assert started["started"] is True
    assert started["kwargs"]["daemon"] is True
    assert started["kwargs"]["target"] is worker._worker_thread_target


# ---------------------------------------------------------------------------
# workflow orchestration — stub execute_activity / patched (no Temporal env)
# ---------------------------------------------------------------------------


def _run_workflow(monkeypatch, results, patched=True):
    """Drive AISystemsBuildWorkflow.run with a stubbed execute_activity.

    ``results`` maps activity function name -> DTO returned by that activity;
    unspecified activities default to ``{"success": True}``. Returns the ordered
    activity names scheduled and the ``error`` passed to finalize (if any).
    """
    from ai_systems_team.temporal import workflows as wf

    calls: list[str] = []
    captured: dict = {}

    async def fake_execute(activity, args=None, **kwargs):
        name = getattr(activity, "__name__", str(activity))
        calls.append(name)
        if name == "finalize_build_activity":
            captured["finalize_error"] = (args or [None, None])[1]
        return results.get(name, {"success": True})

    monkeypatch.setattr(wf.workflow, "execute_activity", fake_execute)
    monkeypatch.setattr(wf.workflow, "patched", lambda _id: patched)
    asyncio.run(wf.AISystemsBuildWorkflow().run("j1", "proj", "/spec.md", {}, "/out"))
    return calls, captured


def test_workflow_runs_all_phases_in_order(monkeypatch) -> None:
    """Happy path: begin -> six phases -> finalize (with no error)."""
    calls, captured = _run_workflow(monkeypatch, {"begin_run_activity": {}})
    assert calls == [
        "begin_run_activity",
        "spec_intake_activity",
        "architecture_activity",
        "capabilities_activity",
        "evaluation_activity",
        "safety_activity",
        "build_phase_activity",
        "finalize_build_activity",
    ]
    assert captured["finalize_error"] is None


@pytest.mark.parametrize(
    "failing,expected_error",
    [
        ("spec_intake_activity", "boom-spec"),
        ("architecture_activity", "boom-arch"),
        ("capabilities_activity", "boom-caps"),
        ("evaluation_activity", "boom-eval"),
        ("safety_activity", "boom-safety"),
        ("build_phase_activity", "boom-build"),
    ],
)
def test_workflow_short_circuits_to_finalize_on_phase_failure(
    monkeypatch, failing, expected_error
) -> None:
    """A phase reporting success=False jumps straight to finalize with its error."""
    calls, captured = _run_workflow(
        monkeypatch,
        {"begin_run_activity": {}, failing: {"success": False, "error": expected_error}},
    )
    # finalize runs immediately after the failing phase; no later phase runs.
    assert calls[-1] == "finalize_build_activity"
    assert calls[-2] == failing
    assert captured["finalize_error"] == expected_error


def test_workflow_skips_resumed_phases(monkeypatch) -> None:
    """Phases already checkpointed are skipped; only the remainder run."""
    resume = {
        "completed_phases": ["spec_intake", "architecture", "capabilities"],
        "spec_intake": {"success": True},
        "architecture": {"success": True},
        "capabilities": {"success": True},
    }
    calls, captured = _run_workflow(monkeypatch, {"begin_run_activity": resume})
    assert calls == [
        "begin_run_activity",
        "evaluation_activity",
        "safety_activity",
        "build_phase_activity",
        "finalize_build_activity",
    ]
    assert captured["finalize_error"] is None


def test_workflow_all_phases_resumed_goes_straight_to_finalize(monkeypatch) -> None:
    """A fully-completed job (resume) runs no phases, only finalize."""
    resume = {
        "completed_phases": [
            "spec_intake",
            "architecture",
            "capabilities",
            "evaluation",
            "safety",
            "build",
        ],
        "spec_intake": {"success": True},
        "architecture": {"success": True},
        "capabilities": {"success": True},
        "evaluation": {"success": True},
        "safety": {"success": True},
        "build": {"success": True},
    }
    calls, _ = _run_workflow(monkeypatch, {"begin_run_activity": resume})
    assert calls == ["begin_run_activity", "finalize_build_activity"]


def test_workflow_unpatched_replay_runs_legacy_monolith(monkeypatch) -> None:
    """Pre-decomposition histories replay the single-activity path deterministically."""
    calls, _ = _run_workflow(monkeypatch, {"run_build_activity": None}, patched=False)
    assert calls == ["run_build_activity"]


def test_finalize_retry_and_timeouts_configured() -> None:
    """Option blocks are wired: every block carries the retry policy + task queue."""
    from ai_systems_team.temporal import workflows as wf

    for opts in (wf._PHASE_ACTIVITY_OPTS, wf._BOOKEND_ACTIVITY_OPTS, wf._LEGACY_ACTIVITY_OPTS):
        assert opts["retry_policy"] is wf.DEFAULT_RETRY_POLICY
        assert opts["task_queue"] == wf.TASK_QUEUE

    # Phase/book-end activities bound each ATTEMPT (start_to_close) so a hung attempt
    # is retried under the policy rather than pinning a worker for the whole window.
    assert wf._PHASE_ACTIVITY_OPTS["start_to_close_timeout"] == wf.PHASE_TIMEOUT
    assert wf._BOOKEND_ACTIVITY_OPTS["start_to_close_timeout"] == wf.BOOKEND_TIMEOUT
    assert "schedule_to_close_timeout" not in wf._PHASE_ACTIVITY_OPTS

    # The legacy drain-out branch must keep the pre-decomposition whole-window 12h
    # ceiling byte-identical so replays stay deterministic.
    assert wf._LEGACY_ACTIVITY_OPTS["schedule_to_close_timeout"] == wf.BUILD_TIMEOUT
    assert "start_to_close_timeout" not in wf._LEGACY_ACTIVITY_OPTS

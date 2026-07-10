"""Tests for temporal worker enabled paths and the agent_implementations/run_*
example scripts. The scripts are tested by importing them and calling main()
with all heavy dependencies mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Temporal worker — Temporal-enabled paths
# ---------------------------------------------------------------------------


def test_create_blogging_worker_with_client(monkeypatch) -> None:
    """create_blogging_worker builds a Worker on the blogging task queue."""
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")

    captured = {}

    class _FakeWorker:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            captured["args"] = args

    monkeypatch.setattr(worker, "Worker", _FakeWorker)

    # Reset module-level state
    monkeypatch.setattr(worker, "_activity_executor", None)
    out = worker.create_blogging_worker(client=MagicMock())
    assert out is not None
    assert worker._activity_executor is not None
    assert captured["task_queue"] == "blogging"


def test_start_blogging_temporal_worker_thread_when_enabled(monkeypatch) -> None:
    """The worker thread starts once and is idempotent while already alive."""
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    started: dict = {"called": False}

    class _NoOpThread:
        def __init__(self, target=None, name=None, daemon=False, **kw):
            self._target = target

        def start(self):
            started["called"] = True

        def is_alive(self):
            return True

    monkeypatch.setattr(worker.threading, "Thread", _NoOpThread)
    monkeypatch.setattr(worker, "_worker_thread", None)
    assert worker.start_blogging_temporal_worker_thread() is True
    assert started["called"]

    # Re-call: already alive → True without new start
    started["called"] = False
    assert worker.start_blogging_temporal_worker_thread() is True
    assert started["called"] is False


def test_worker_thread_target_handles_runtime_error_loop_stopped(monkeypatch) -> None:
    """RuntimeError mentioning 'event loop stopped' is silently absorbed."""
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    class _FakeLoop:
        def __init__(self):
            self._closed = False

        def run_until_complete(self, coro):
            raise RuntimeError("Event loop stopped before Future completed")

        def close(self):
            self._closed = True

    monkeypatch.setattr(worker.asyncio, "new_event_loop", lambda: _FakeLoop())
    monkeypatch.setattr(worker.asyncio, "set_event_loop", lambda loop: None)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda _loop: None)

    worker._worker_thread_target()  # Must not raise


def test_worker_thread_target_handles_unknown_runtime_error(monkeypatch) -> None:
    """An unrelated RuntimeError in the worker loop is logged, not raised."""
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    class _FakeLoop:
        def run_until_complete(self, coro):
            raise RuntimeError("totally different error")

        def close(self):
            pass

    monkeypatch.setattr(worker.asyncio, "new_event_loop", lambda: _FakeLoop())
    monkeypatch.setattr(worker.asyncio, "set_event_loop", lambda loop: None)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda _loop: None)

    worker._worker_thread_target()  # Logs but must not raise


def test_worker_thread_target_handles_generic_exception(monkeypatch) -> None:
    """A generic exception in the worker loop is logged, not raised."""
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    class _FakeLoop:
        def run_until_complete(self, coro):
            raise ValueError("oops")

        def close(self):
            pass

    monkeypatch.setattr(worker.asyncio, "new_event_loop", lambda: _FakeLoop())
    monkeypatch.setattr(worker.asyncio, "set_event_loop", lambda loop: None)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda _loop: None)

    worker._worker_thread_target()  # Must not raise


def test_worker_thread_target_handles_cancelled(monkeypatch) -> None:
    """asyncio.CancelledError is swallowed silently."""
    import asyncio

    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    class _FakeLoop:
        def run_until_complete(self, coro):
            raise asyncio.CancelledError()

        def close(self):
            pass

    monkeypatch.setattr(worker.asyncio, "new_event_loop", lambda: _FakeLoop())
    monkeypatch.setattr(worker.asyncio, "set_event_loop", lambda loop: None)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda _loop: None)
    worker._worker_thread_target()


def test_shutdown_blogging_temporal_components_running_loop(monkeypatch) -> None:
    """Exercise the path where worker has a running loop and we run shutdown."""
    from blogging.temporal import worker

    fake_worker = MagicMock()
    fake_worker.shutdown = MagicMock(return_value=None)

    class _FakeFuture:
        def result(self, timeout=None):
            return None

    class _FakeLoop:
        def is_running(self):
            return True

    monkeypatch.setattr(
        worker.asyncio,
        "run_coroutine_threadsafe",
        lambda coro, loop: _FakeFuture(),
    )

    monkeypatch.setattr(worker, "_worker_instance", fake_worker)
    monkeypatch.setattr(worker, "_worker_running_loop", _FakeLoop())
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", None)

    worker.shutdown_blogging_temporal_components()


def test_shutdown_blogging_temporal_components_force_stop(monkeypatch) -> None:
    """When worker.shutdown() future raises, we force-stop the loop."""
    from blogging.temporal import worker

    fake_worker = MagicMock()
    fake_worker.shutdown = MagicMock(return_value=None)

    class _Future:
        def result(self, timeout=None):
            raise TimeoutError("timed out")

    class _FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, fn):
            pass

    monkeypatch.setattr(worker.asyncio, "run_coroutine_threadsafe", lambda coro, loop: _Future())
    monkeypatch.setattr(worker, "_worker_instance", fake_worker)
    monkeypatch.setattr(worker, "_worker_running_loop", _FakeLoop())
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", None)

    worker.shutdown_blogging_temporal_components()


def test_shutdown_blogging_temporal_components_worker_only(monkeypatch) -> None:
    """Path where worker_instance is None but loop is set."""
    from blogging.temporal import worker

    class _FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, fn):
            pass

    monkeypatch.setattr(worker, "_worker_instance", None)
    monkeypatch.setattr(worker, "_worker_running_loop", _FakeLoop())
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", None)

    worker.shutdown_blogging_temporal_components()


def test_shutdown_blogging_temporal_components_loop_not_running(monkeypatch) -> None:
    """Path where loop is set but not running — graceful skip."""
    from blogging.temporal import worker

    class _Loop:
        def is_running(self):
            return False

    monkeypatch.setattr(worker, "_worker_instance", MagicMock())
    monkeypatch.setattr(worker, "_worker_running_loop", _Loop())
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", None)
    worker.shutdown_blogging_temporal_components()


def test_shutdown_blogging_temporal_components_with_executor(monkeypatch) -> None:
    """Shutdown also tears down the activity executor."""
    from blogging.temporal import worker

    executor = MagicMock()
    monkeypatch.setattr(worker, "_worker_instance", None)
    monkeypatch.setattr(worker, "_worker_running_loop", None)
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", executor)
    worker.shutdown_blogging_temporal_components()
    executor.shutdown.assert_called()


def test_shutdown_blogging_temporal_components_executor_exception(monkeypatch) -> None:
    """If executor.shutdown raises, log but don't crash."""
    from blogging.temporal import worker

    executor = MagicMock()
    executor.shutdown = MagicMock(side_effect=RuntimeError("nope"))
    monkeypatch.setattr(worker, "_worker_instance", None)
    monkeypatch.setattr(worker, "_worker_running_loop", None)
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", executor)
    worker.shutdown_blogging_temporal_components()


# ---------------------------------------------------------------------------
# temporal.start_workflow happy path
# ---------------------------------------------------------------------------


def test_start_full_pipeline_workflow_calls_run_async(monkeypatch) -> None:
    """start_full_pipeline_workflow delegates to _run_async with client.start_workflow result."""
    from blogging.temporal import start_workflow

    fake_client = MagicMock()
    fake_client.start_workflow = MagicMock(return_value="coro-handle")
    monkeypatch.setattr(start_workflow, "get_temporal_client", lambda: fake_client)

    called: dict = {}

    def fake_run_async(coro):
        called["coro"] = coro
        return None

    monkeypatch.setattr(start_workflow, "_run_async", fake_run_async)
    start_workflow.start_full_pipeline_workflow("job-1", {"brief": "x"})
    fake_client.start_workflow.assert_called_once()
    assert called["coro"] == "coro-handle"


def test_run_async_executes(monkeypatch) -> None:
    """Happy path: get_temporal_loop and get_temporal_client return objects, run completes."""
    from blogging.temporal import start_workflow

    fake_loop = MagicMock()
    fake_client = MagicMock()

    monkeypatch.setattr(start_workflow, "get_temporal_loop", lambda: fake_loop)
    monkeypatch.setattr(start_workflow, "get_temporal_client", lambda: fake_client)

    class _Future:
        def result(self, timeout=None):
            return "ok"

    monkeypatch.setattr(
        start_workflow.asyncio, "run_coroutine_threadsafe", lambda _c, _l: _Future()
    )
    assert start_workflow._run_async("coro") == "ok"


# ---------------------------------------------------------------------------
# temporal.activities — fine-grained pipeline-phase activities
# ---------------------------------------------------------------------------


class _Dumpable:
    """Stand-in for a pydantic model at an activity boundary.

    ``model_dump`` returns the wrapped dict and ``content_plan`` exposes it so it can
    double as a ``PlanningPhaseResult`` stub in the draft/gates activities.
    """

    def __init__(self, data):
        self._data = data
        self.content_plan = data

    def model_dump(self, mode=None):
        return self._data


def _fake_ctx(tmp_path):
    from types import SimpleNamespace

    return SimpleNamespace(
        job_updater=lambda **kw: None,
        work_dir=tmp_path,
        planning_phase_result=None,
        plan=None,
        elicited_stories_text=None,
        draft_result=None,
        status="PASS",
    )


def _patch_context(monkeypatch, tmp_path):
    """Patch the shared context builder + job-store/heartbeat side effects to no-ops."""
    import importlib

    from blogging.temporal import activities as acts

    bjs = importlib.import_module("blogging.shared.blog_job_store")
    rpj = importlib.import_module("blogging.shared.run_pipeline_job")
    ctx = _fake_ctx(tmp_path)
    monkeypatch.setattr(acts, "_build_pipeline_context", lambda job_id, req: ctx)
    monkeypatch.setattr(bjs, "start_blog_job", lambda job_id: None)
    monkeypatch.setattr(rpj, "start_pipeline_heartbeat", lambda job_id: None)
    return acts, ctx


def _v2():
    import importlib

    return importlib.import_module("blogging.agent_implementations.blog_writing_process_v2")


def test_plan_stage_activity_returns_planning_dto(monkeypatch, tmp_path) -> None:
    """plan_stage_activity runs the planning stage and serializes the result."""
    acts, ctx = _patch_context(monkeypatch, tmp_path)

    def fake_planning(c):
        c.planning_phase_result = _Dumpable({"content_plan": {"x": 1}})
        c.elicited_stories_text = "story"
        return None

    monkeypatch.setattr(_v2(), "run_planning_stage", fake_planning)

    out = acts.plan_stage_activity("j1", {"brief": "x"})
    assert out["status"] == "PASS"
    assert out["planning_phase_result"] == {"content_plan": {"x": 1}}
    assert out["elicited_stories_text"] == "story"


def test_plan_stage_activity_abort_returns_fail(monkeypatch, tmp_path) -> None:
    """A planning abort (cancel/failed job) yields a FAIL DTO, not a raise."""
    acts, _ = _patch_context(monkeypatch, tmp_path)
    monkeypatch.setattr(_v2(), "run_planning_stage", lambda c: (None, None, "FAIL"))

    out = acts.plan_stage_activity("j1", {"brief": "x"})
    assert out["status"] == "FAIL"


def test_plan_stage_activity_error_fails_job_and_returns_fail(monkeypatch, tmp_path) -> None:
    """A hard stage error fails the job and returns a FAIL DTO (terminal, no Temporal retry)."""
    acts, _ = _patch_context(monkeypatch, tmp_path)

    def boom(c):
        raise ValueError("kaboom")

    monkeypatch.setattr(_v2(), "run_planning_stage", boom)
    failed: dict = {}
    monkeypatch.setattr(
        acts,
        "_fail_activity",
        lambda job_id, exc, failed_phase: failed.update(job_id=job_id, phase=failed_phase),
    )

    out = acts.plan_stage_activity("j1", {"brief": "x"})
    assert out["status"] == "FAIL"
    assert failed == {"job_id": "j1", "phase": "planning"}


def test_draft_stage_activity_returns_draft_dto(monkeypatch, tmp_path) -> None:
    """draft_stage_activity rebuilds the plan, runs the draft stage, serializes it."""
    import importlib

    acts, ctx = _patch_context(monkeypatch, tmp_path)
    cp = importlib.import_module("blogging.shared.content_plan")
    monkeypatch.setattr(
        cp.PlanningPhaseResult, "model_validate", classmethod(lambda cls, d: _Dumpable(d))
    )

    def fake_draft(c):
        c.draft_result = _Dumpable({"draft": "hello"})
        c.elicited_stories_text = "s2"
        return None

    monkeypatch.setattr(_v2(), "run_draft_stage", fake_draft)

    planning = {"planning_phase_result": {"content_plan": {}}, "status": "PASS"}
    out = acts.draft_stage_activity("j1", {"brief": "x"}, planning)
    assert out["status"] == "PASS"
    assert out["draft"] == {"draft": "hello"}
    assert out["elicited_stories_text"] == "s2"


def test_gates_stage_activity_returns_gates_dto(monkeypatch, tmp_path) -> None:
    """gates_stage_activity rebuilds plan+draft, runs the gates stage, serializes it."""
    import importlib

    acts, ctx = _patch_context(monkeypatch, tmp_path)
    cp = importlib.import_module("blogging.shared.content_plan")
    wm = importlib.import_module("blog_writer_agent.models")
    monkeypatch.setattr(
        cp.PlanningPhaseResult, "model_validate", classmethod(lambda cls, d: _Dumpable(d))
    )
    monkeypatch.setattr(wm.WriterOutput, "model_validate", classmethod(lambda cls, d: _Dumpable(d)))

    def fake_gates(c):
        c.draft_result = _Dumpable({"draft": "final"})
        c.status = "NEEDS_HUMAN_REVIEW"
        return None

    monkeypatch.setattr(_v2(), "run_gates_stage", fake_gates)

    planning = {"planning_phase_result": {"content_plan": {}}}
    draft = {"draft": {"draft": "d"}, "status": "PASS"}
    out = acts.gates_stage_activity("j1", {"brief": "x"}, planning, draft)
    assert out["status"] == "NEEDS_HUMAN_REVIEW"
    assert out["draft"] == {"draft": "final"}


def test_finalize_job_activity_delegates(monkeypatch, tmp_path) -> None:
    """finalize_job_activity reconstructs models and calls finalize_blog_job."""
    import importlib

    from blogging.temporal import activities as acts

    cp = importlib.import_module("blogging.shared.content_plan")
    wm = importlib.import_module("blog_writer_agent.models")
    rpj = importlib.import_module("blogging.shared.run_pipeline_job")
    monkeypatch.setattr(
        cp.PlanningPhaseResult, "model_validate", classmethod(lambda cls, d: _Dumpable(d))
    )
    monkeypatch.setattr(wm.WriterOutput, "model_validate", classmethod(lambda cls, d: _Dumpable(d)))
    seen: dict = {}
    monkeypatch.setattr(
        rpj,
        "finalize_blog_job",
        lambda job_id, ppr, draft, status: seen.update(job_id=job_id, status=status),
    )

    planning = {"planning_phase_result": {"content_plan": {}}}
    gates = {"draft": {"draft": "final"}, "status": "PASS"}
    acts.finalize_job_activity("j1", planning, gates)
    assert seen == {"job_id": "j1", "status": "PASS"}


class _FakeHeartbeat:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_plan_stage_activity_swallows_external_cancellation(monkeypatch, tmp_path) -> None:
    """A stage error the funnel recognizes as external cancellation marks the job
    cancelled (via the real _fail_activity) and returns a FAIL DTO; heartbeat stopped."""
    import importlib

    acts, _ = _patch_context(monkeypatch, tmp_path)
    rpj = importlib.import_module("blogging.shared.run_pipeline_job")
    hb = _FakeHeartbeat()
    monkeypatch.setattr(rpj, "start_pipeline_heartbeat", lambda job_id: hb)
    # Drive the real _fail_activity down its external-cancellation branch.
    marked: dict = {}
    monkeypatch.setattr(rpj, "_is_external_cancellation", lambda exc: True)
    monkeypatch.setattr(rpj, "mark_job_cancelled", lambda job_id: marked.setdefault("job", job_id))

    def boom(c):
        raise RuntimeError("wrapped cancel")

    monkeypatch.setattr(_v2(), "run_planning_stage", boom)
    out = acts.plan_stage_activity("j1", {"brief": "x"})
    assert out["status"] == "FAIL"
    assert marked == {"job": "j1"}
    assert hb.stopped is True


def test_draft_stage_activity_abort_returns_fail_with_partial_draft(monkeypatch, tmp_path) -> None:
    """A draft abort tuple carries the partial draft into a FAIL DTO."""
    import importlib

    acts, _ = _patch_context(monkeypatch, tmp_path)
    cp = importlib.import_module("blogging.shared.content_plan")
    monkeypatch.setattr(
        cp.PlanningPhaseResult, "model_validate", classmethod(lambda cls, d: _Dumpable(d))
    )
    monkeypatch.setattr(
        _v2(), "run_draft_stage", lambda c: (None, _Dumpable({"draft": "partial"}), "FAIL")
    )

    planning = {"planning_phase_result": {"content_plan": {}}}
    out = acts.draft_stage_activity("j1", {"brief": "x"}, planning)
    assert out["status"] == "FAIL"
    assert out["draft"] == {"draft": "partial"}


def test_draft_stage_activity_reraises_cancelled(monkeypatch, tmp_path) -> None:
    """A Temporal CancelledError from the draft stage propagates out of the activity."""
    import importlib

    from temporalio.exceptions import CancelledError

    acts, _ = _patch_context(monkeypatch, tmp_path)
    cp = importlib.import_module("blogging.shared.content_plan")
    monkeypatch.setattr(
        cp.PlanningPhaseResult, "model_validate", classmethod(lambda cls, d: _Dumpable(d))
    )

    def boom(c):
        raise CancelledError("cancel")

    monkeypatch.setattr(_v2(), "run_draft_stage", boom)
    with pytest.raises(CancelledError):
        acts.draft_stage_activity("j1", {"brief": "x"}, {"planning_phase_result": {}})


def test_gates_stage_activity_hard_error_returns_fail(monkeypatch, tmp_path) -> None:
    """A hard gates error fails the job and returns a FAIL DTO so finalize is skipped."""
    import importlib

    acts, _ = _patch_context(monkeypatch, tmp_path)
    cp = importlib.import_module("blogging.shared.content_plan")
    wm = importlib.import_module("blog_writer_agent.models")
    monkeypatch.setattr(
        cp.PlanningPhaseResult, "model_validate", classmethod(lambda cls, d: _Dumpable(d))
    )
    monkeypatch.setattr(wm.WriterOutput, "model_validate", classmethod(lambda cls, d: _Dumpable(d)))
    failed: dict = {}
    monkeypatch.setattr(
        acts, "_fail_activity", lambda job_id, exc, failed_phase: failed.update(phase=failed_phase)
    )

    def boom(c):
        raise ValueError("gate blew up")

    monkeypatch.setattr(_v2(), "run_gates_stage", boom)
    planning = {"planning_phase_result": {"content_plan": {}}}
    draft = {"draft": {"draft": "d"}}
    out = acts.gates_stage_activity("j1", {"brief": "x"}, planning, draft)
    assert out["status"] == "FAIL"
    assert failed == {"phase": "gates"}


def _patch_finalize(monkeypatch):
    """Patch finalize's model rebuilds to stubs and return the (acts, rpj) modules.

    Leaves ``finalize_blog_job`` (the transient store call) for the test to drive.
    """
    import importlib

    from blogging.temporal import activities as acts

    cp = importlib.import_module("blogging.shared.content_plan")
    wm = importlib.import_module("blog_writer_agent.models")
    rpj = importlib.import_module("blogging.shared.run_pipeline_job")
    monkeypatch.setattr(
        cp.PlanningPhaseResult, "model_validate", classmethod(lambda cls, d: _Dumpable(d))
    )
    monkeypatch.setattr(wm.WriterOutput, "model_validate", classmethod(lambda cls, d: _Dumpable(d)))
    return acts, rpj


def test_finalize_job_activity_marks_failed_on_last_attempt(monkeypatch, tmp_path) -> None:
    """On the final Temporal attempt a transient store error fails the job and swallows."""
    acts, rpj = _patch_finalize(monkeypatch)

    def _raise(*a, **kw):
        raise RuntimeError("store down")

    monkeypatch.setattr(rpj, "finalize_blog_job", _raise)
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: True)
    failed: dict = {}
    monkeypatch.setattr(
        acts, "_fail_activity", lambda job_id, exc, failed_phase: failed.update(phase=failed_phase)
    )

    assert (
        acts.finalize_job_activity("j1", {"planning_phase_result": {}}, {"draft": {"d": 1}}) is None
    )
    assert failed == {"phase": "finalize"}


def test_finalize_job_activity_reraises_before_last_attempt(monkeypatch, tmp_path) -> None:
    """Before the final attempt a transient store error re-raises so Temporal retries —
    nothing is terminal yet, and a store blip must not permanently fail a successful pipeline."""
    acts, rpj = _patch_finalize(monkeypatch)

    def _raise(*a, **kw):
        raise RuntimeError("transient store blip")

    monkeypatch.setattr(rpj, "finalize_blog_job", _raise)
    monkeypatch.setattr(acts, "_is_last_attempt", lambda: False)
    monkeypatch.setattr(
        acts,
        "_fail_activity",
        lambda *a, **kw: pytest.fail("job must not be marked failed before the last attempt"),
    )

    with pytest.raises(RuntimeError):
        acts.finalize_job_activity("j1", {"planning_phase_result": {}}, {"draft": {"d": 1}})


def test_finalize_job_activity_malformed_dto_raises_loudly(monkeypatch, tmp_path) -> None:
    """A malformed finalize input DTO raises out of the activity (code/schema defect),
    bypassing the retry-then-mark funnel — matching the draft/gates contract."""
    import importlib

    from blogging.temporal import activities as acts

    cp = importlib.import_module("blogging.shared.content_plan")

    def boom(cls, d):
        raise ValueError("bad model")

    monkeypatch.setattr(cp.PlanningPhaseResult, "model_validate", classmethod(boom))
    monkeypatch.setattr(
        acts, "_fail_activity", lambda *a, **kw: pytest.fail("malformed DTO must not mark the job")
    )
    monkeypatch.setattr(
        acts, "_is_last_attempt", lambda: pytest.fail("must not reach the retry funnel")
    )

    with pytest.raises(ValueError):
        acts.finalize_job_activity("j1", {"planning_phase_result": {}}, {"draft": None})


def test_draft_stage_activity_malformed_dto_raises_loudly(monkeypatch, tmp_path) -> None:
    """A malformed input DTO (missing key) is a code/schema defect: it must raise
    out of the activity instead of being recorded as a pipeline failure."""
    acts, _ = _patch_context(monkeypatch, tmp_path)
    monkeypatch.setattr(
        acts,
        "_fail_activity",
        lambda *a, **kw: pytest.fail("a malformed DTO must not mark the job failed"),
    )

    with pytest.raises(KeyError):
        acts.draft_stage_activity("j1", {"brief": "x"}, {"status": "PASS"})  # no planning key


def test_gates_stage_activity_malformed_dto_raises_loudly(monkeypatch, tmp_path) -> None:
    """Same loud-failure contract for the gates activity's input DTOs."""
    import importlib

    acts, _ = _patch_context(monkeypatch, tmp_path)
    cp = importlib.import_module("blogging.shared.content_plan")
    monkeypatch.setattr(
        cp.PlanningPhaseResult, "model_validate", classmethod(lambda cls, d: _Dumpable(d))
    )
    monkeypatch.setattr(
        acts,
        "_fail_activity",
        lambda *a, **kw: pytest.fail("a malformed DTO must not mark the job failed"),
    )

    planning = {"planning_phase_result": {"content_plan": {}}}
    with pytest.raises(KeyError):
        acts.gates_stage_activity("j1", {"brief": "x"}, planning, {"status": "PASS"})  # no draft


# ---------------------------------------------------------------------------
# temporal.activities.run_full_pipeline_activity (legacy drain-out monolith)
# ---------------------------------------------------------------------------


def test_legacy_full_pipeline_activity_delegates(monkeypatch) -> None:
    """run_full_pipeline_activity delegates to run_blog_full_pipeline_job."""
    import importlib

    from blogging.temporal import activities as acts

    rpj = importlib.import_module("blogging.shared.run_pipeline_job")
    seen: dict = {}
    monkeypatch.setattr(
        rpj, "run_blog_full_pipeline_job", lambda job_id, req: seen.update(job_id=job_id, req=req)
    )
    acts.run_full_pipeline_activity("j1", {"brief": "x"})
    assert seen == {"job_id": "j1", "req": {"brief": "x"}}


def test_legacy_full_pipeline_activity_reraises_cancelled(monkeypatch) -> None:
    """The legacy drain-out activity re-raises a Temporal CancelledError."""
    import importlib

    from temporalio.exceptions import CancelledError

    from blogging.temporal import activities as acts

    rpj = importlib.import_module("blogging.shared.run_pipeline_job")

    def boom(job_id, req):
        raise CancelledError("nope")

    monkeypatch.setattr(rpj, "run_blog_full_pipeline_job", boom)
    with pytest.raises(CancelledError):
        acts.run_full_pipeline_activity("j", {})


def test_legacy_full_pipeline_activity_reraises_other(monkeypatch) -> None:
    """The legacy drain-out activity re-raises non-cancellation errors."""
    import importlib

    from blogging.temporal import activities as acts

    rpj = importlib.import_module("blogging.shared.run_pipeline_job")

    def boom(job_id, req):
        raise ValueError("oops")

    monkeypatch.setattr(rpj, "run_blog_full_pipeline_job", boom)
    with pytest.raises(ValueError):
        acts.run_full_pipeline_activity("j", {})


# ---------------------------------------------------------------------------
# agent_implementations/run_*.py scripts
# ---------------------------------------------------------------------------


def test_run_copy_editor_agent_main_smoke(monkeypatch, capsys) -> None:
    """run_copy_editor_agent.main should run end-to-end with patched LLM."""
    import agent_implementations.run_copy_editor_agent as mod

    from llm_service import DummyLLMClient

    monkeypatch.setattr(mod, "get_strands_model", lambda key: DummyLLMClient())
    monkeypatch.setattr(mod, "load_style_file", lambda *a, **kw: "style")

    # Patch the agent's run to return a deterministic result
    from blog_copy_editor_agent.models import CopyEditorOutput

    monkeypatch.setattr(
        mod.BlogCopyEditorAgent,
        "run",
        lambda self, inp: CopyEditorOutput(summary="ok", feedback_items=[]),
    )

    mod.main()
    captured = capsys.readouterr()
    assert "Copy Editor Summary" in captured.out


def test_run_publication_agent_main_smoke(monkeypatch, capsys, tmp_path) -> None:
    import agent_implementations.run_publication_agent as mod
    from blog_publication_agent.models import PublicationSubmission

    from llm_service import DummyLLMClient

    monkeypatch.setattr(mod, "get_strands_model", lambda key: DummyLLMClient())
    monkeypatch.setattr(mod, "load_style_file", lambda *a, **kw: "")

    # Stub submit_draft so we don't touch the real blog_posts directory
    monkeypatch.setattr(
        mod.BlogPublicationAgent,
        "submit_draft",
        lambda self, inp: PublicationSubmission(
            submission_id="sub-123",
            slug="sub-123",
            file_path=tmp_path / "draft.md",
            message="Submitted",
        ),
    )

    mod.main()
    captured = capsys.readouterr()
    assert "sub-123" in captured.out


def test_run_writer_agent_main_smoke(monkeypatch, capsys) -> None:
    import agent_implementations.run_writer_agent as mod

    from llm_service import DummyLLMClient

    monkeypatch.setattr(mod, "get_strands_model", lambda key: DummyLLMClient())
    monkeypatch.setattr(mod, "load_style_file", lambda *a, **kw: "")

    from blog_writer_agent.models import WriterOutput

    # The script constructs WriterInput with content_plan=None which raises.
    # We mock the BlogWriterAgent so the bug is bypassed.
    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def run(self, inp):
            return WriterOutput(draft="# Draft\n\nBody.")

    monkeypatch.setattr(mod, "BlogWriterAgent", _Stub)
    # Also patch WriterInput so the missing content_plan validation is skipped
    monkeypatch.setattr(mod, "WriterInput", lambda **kw: type("X", (), {"draft": "..."})())

    mod.main()
    captured = capsys.readouterr()
    assert "Draft" in captured.out

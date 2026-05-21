"""Tests for the SOC2 compliance team's Temporal client, activities,
workflows, worker, and start-workflow helpers.

These tests do not depend on a real Temporal server: ``temporalio``'s
client is monkeypatched, the workflow ``run`` body is exercised via
direct invocation, and the worker thread entrypoint is exercised with
fakes that short-circuit network calls.
"""

from __future__ import annotations

import asyncio
import importlib
from concurrent.futures import Future
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_constants_default_task_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPORAL_TASK_QUEUE_SOC2", raising=False)
    from soc2_compliance_team.temporal import constants as cmod

    importlib.reload(cmod)
    assert cmod.TASK_QUEUE == "soc2-compliance"
    assert cmod.WORKFLOW_ID_PREFIX_AUDIT == "soc2-audit-"


# ---------------------------------------------------------------------------
# client module
# ---------------------------------------------------------------------------


def test_get_temporal_address_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from soc2_compliance_team.temporal import client as cmod

    assert cmod.get_temporal_address() is None
    assert cmod.is_temporal_enabled() is False


def test_get_temporal_address_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "  temporal:7233 ")
    from soc2_compliance_team.temporal import client as cmod

    assert cmod.get_temporal_address() == "temporal:7233"
    assert cmod.is_temporal_enabled() is True


def test_get_temporal_namespace_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    from soc2_compliance_team.temporal import client as cmod

    assert cmod.get_temporal_namespace() == "default"


def test_get_temporal_namespace_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "  soc2 ")
    from soc2_compliance_team.temporal import client as cmod

    assert cmod.get_temporal_namespace() == "soc2"


def test_set_and_get_temporal_client_and_loop() -> None:
    from soc2_compliance_team.temporal import client as cmod

    sentinel = object()
    cmod.set_temporal_client(sentinel)  # type: ignore[arg-type]
    assert cmod.get_temporal_client() is sentinel
    cmod.set_temporal_client(None)
    assert cmod.get_temporal_client() is None

    loop = asyncio.new_event_loop()
    try:
        cmod.set_temporal_loop(loop)
        assert cmod.get_temporal_loop() is loop
    finally:
        cmod.set_temporal_loop(None)
        loop.close()
    assert cmod.get_temporal_loop() is None


def test_connect_temporal_client_returns_none_when_no_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from soc2_compliance_team.temporal import client as cmod

    result = asyncio.run(cmod.connect_temporal_client())
    assert result is None


def test_connect_temporal_client_connects_when_address_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "soc2")

    sentinel = object()

    async def _fake_connect(address, namespace):  # noqa: ANN001
        assert address == "temporal:7233"
        assert namespace == "soc2"
        return sentinel

    import temporalio.client as tc

    monkeypatch.setattr(tc.Client, "connect", staticmethod(_fake_connect))

    from soc2_compliance_team.temporal import client as cmod

    result = asyncio.run(cmod.connect_temporal_client())
    assert result is sentinel


def test_connect_temporal_client_raises_on_failure(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")

    async def _bad_connect(address, namespace):  # noqa: ANN001
        raise RuntimeError("boom")

    import temporalio.client as tc

    monkeypatch.setattr(tc.Client, "connect", staticmethod(_bad_connect))

    from soc2_compliance_team.temporal import client as cmod

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError):
            asyncio.run(cmod.connect_temporal_client())
    assert any("Temporal client connection failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# start_workflow
# ---------------------------------------------------------------------------


def test_run_async_raises_when_no_loop_or_client() -> None:
    from soc2_compliance_team.temporal import client as cmod
    from soc2_compliance_team.temporal import start_workflow as swmod

    cmod.set_temporal_client(None)
    cmod.set_temporal_loop(None)

    async def _coro():
        return 1

    coro = _coro()
    try:
        with pytest.raises(RuntimeError, match="Temporal client not available"):
            swmod._run_async(coro)
    finally:
        coro.close()


def test_run_async_threads_through_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """When loop + client are set, the coroutine is submitted to the loop
    and its result is returned."""
    from soc2_compliance_team.temporal import client as cmod
    from soc2_compliance_team.temporal import start_workflow as swmod

    cmod.set_temporal_client(object())  # type: ignore[arg-type]

    fake_loop = object()
    cmod.set_temporal_loop(fake_loop)  # type: ignore[arg-type]

    called: dict[str, Any] = {}

    def _fake_threadsafe(coro, loop):
        called["coro"] = coro
        called["loop"] = loop
        f: Future = Future()
        f.set_result("done")
        return f

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _fake_threadsafe)

    async def _coro():
        return "x"

    coro = _coro()
    try:
        out = swmod._run_async(coro)
        assert out == "done"
        assert called["loop"] is fake_loop
    finally:
        cmod.set_temporal_client(None)
        cmod.set_temporal_loop(None)
        coro.close()


def test_start_audit_workflow_raises_when_no_client() -> None:
    from soc2_compliance_team.temporal import client as cmod
    from soc2_compliance_team.temporal import start_workflow as swmod

    cmod.set_temporal_client(None)
    with pytest.raises(RuntimeError, match="Temporal client not available"):
        swmod.start_audit_workflow("job-1", "/some/path")


def test_start_audit_workflow_invokes_run_async(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team.temporal import client as cmod
    from soc2_compliance_team.temporal import start_workflow as swmod

    captured: dict[str, Any] = {}

    class _FakeClient:
        def start_workflow(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

            async def _noop():
                return None

            return _noop()

    cmod.set_temporal_client(_FakeClient())  # type: ignore[arg-type]

    def _fake_run_async(coro):
        captured["coro"] = coro
        coro.close()
        return None

    monkeypatch.setattr(swmod, "_run_async", _fake_run_async)

    try:
        swmod.start_audit_workflow("abc", "/repo/path")
    finally:
        cmod.set_temporal_client(None)

    assert captured["kwargs"]["id"] == "soc2-audit-abc"
    assert captured["kwargs"]["task_queue"] == "soc2-compliance"
    assert captured["kwargs"]["args"] == ["abc", "/repo/path"]
    # First positional arg is the workflow's run method
    assert captured["args"][0].__name__ == "run"


# ---------------------------------------------------------------------------
# activities
# ---------------------------------------------------------------------------


def test_run_audit_activity_invokes_run_audit_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from soc2_compliance_team.temporal import activities as amod

    captured: dict[str, Any] = {}

    def _fake_run_audit_job(job_id, repo_path):
        captured["job_id"] = job_id
        captured["repo_path"] = repo_path

    # Patch the function inside its source module — the activity
    # imports it dynamically inside the function body.
    monkeypatch.setattr("soc2_compliance_team.api.main._run_audit_job", _fake_run_audit_job)

    amod.run_audit_activity("job-1", "/repo/x")

    assert captured["job_id"] == "job-1"
    assert captured["repo_path"] == "/repo/x"


def test_run_audit_activity_reraises_on_failure(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    from soc2_compliance_team.temporal import activities as amod

    def _fake_run_audit_job(job_id, repo_path):
        raise RuntimeError("boom")

    monkeypatch.setattr("soc2_compliance_team.api.main._run_audit_job", _fake_run_audit_job)

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError):
            amod.run_audit_activity("job-x", "/repo/y")
    assert any("SOC2 audit activity failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# workflows.run — exercise the body without a full worker
# ---------------------------------------------------------------------------


def test_workflow_run_executes_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Soc2AuditWorkflow.run`` should await execute_activity with
    the correct arguments."""
    from soc2_compliance_team.temporal import workflows as wmod

    captured: dict[str, Any] = {}

    async def _fake_execute(activity, args=None, **kwargs):  # noqa: ANN001
        captured["activity"] = activity
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(wmod.workflow, "execute_activity", _fake_execute)

    wf = wmod.Soc2AuditWorkflow()
    asyncio.run(wf.run("job-1", "/repo/path"))

    assert captured["args"] == ["job-1", "/repo/path"]
    assert captured["kwargs"]["task_queue"] == "soc2-compliance"
    assert "schedule_to_close_timeout" in captured["kwargs"]
    assert "retry_policy" in captured["kwargs"]


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


def test_create_soc2_worker_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from soc2_compliance_team.temporal import worker as wmod

    assert wmod.create_soc2_worker(client=None) is None


def test_create_soc2_worker_returns_none_when_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from soc2_compliance_team.temporal import worker as wmod

    assert wmod.create_soc2_worker(client=None) is None


def test_create_soc2_worker_builds_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from soc2_compliance_team.temporal import worker as wmod

    captured: dict[str, Any] = {}

    class _FakeWorker:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(wmod, "Worker", _FakeWorker)
    monkeypatch.setattr(wmod, "_activity_executor", None)

    out = wmod.create_soc2_worker(client=object())
    assert isinstance(out, _FakeWorker)
    assert captured["kwargs"]["task_queue"] == "soc2-compliance"
    assert captured["kwargs"]["max_concurrent_activities"] == 2
    # Workflow + activity wiring
    assert wmod.Soc2AuditWorkflow in captured["kwargs"]["workflows"]


def test_create_soc2_worker_reuses_activity_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call should not allocate a new executor."""
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from soc2_compliance_team.temporal import worker as wmod

    captured: dict[str, Any] = {}

    class _FakeWorker:
        def __init__(self, *args, **kwargs):
            captured.setdefault("executors", []).append(kwargs["activity_executor"])

    monkeypatch.setattr(wmod, "Worker", _FakeWorker)
    monkeypatch.setattr(wmod, "_activity_executor", None)

    wmod.create_soc2_worker(client=object())
    wmod.create_soc2_worker(client=object())
    assert captured["executors"][0] is captured["executors"][1]


def test_run_worker_async_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """When connect_temporal_client returns None, _run_worker_async exits."""
    from soc2_compliance_team.temporal import worker as wmod

    async def _no_client():
        return None

    monkeypatch.setattr(wmod, "connect_temporal_client", _no_client)
    asyncio.run(wmod._run_worker_async())


def test_run_worker_async_no_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """When create_soc2_worker returns None, exit cleanly."""
    from soc2_compliance_team.temporal import worker as wmod

    async def _client():
        return object()

    monkeypatch.setattr(wmod, "connect_temporal_client", _client)
    monkeypatch.setattr(wmod, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(wmod, "set_temporal_loop", lambda loop: None)
    monkeypatch.setattr(wmod, "create_soc2_worker", lambda c: None)
    asyncio.run(wmod._run_worker_async())


def test_run_worker_async_starts_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: a worker is created and its run() is awaited."""
    from soc2_compliance_team.temporal import worker as wmod

    fake_client = object()

    async def _client():
        return fake_client

    captured: dict[str, Any] = {}

    class _Worker:
        async def run(self):
            captured["ran"] = True

    monkeypatch.setattr(wmod, "connect_temporal_client", _client)
    monkeypatch.setattr(wmod, "set_temporal_client", lambda c: captured.setdefault("client", c))
    monkeypatch.setattr(wmod, "set_temporal_loop", lambda loop: captured.setdefault("loop", loop))
    monkeypatch.setattr(wmod, "create_soc2_worker", lambda c: _Worker())

    asyncio.run(wmod._run_worker_async())
    assert captured["ran"] is True
    assert captured["client"] is fake_client


def test_worker_thread_target_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from soc2_compliance_team.temporal import worker as wmod

    # Should exit immediately without calling asyncio.new_event_loop
    wmod._worker_thread_target()


def test_worker_thread_target_runs_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """When temporal is enabled, _worker_thread_target opens a new loop, runs the
    worker coroutine, and resets module state."""
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from soc2_compliance_team.temporal import client as cmod
    from soc2_compliance_team.temporal import worker as wmod

    state: dict[str, Any] = {}

    async def _fake_run_worker_async():
        state["ran"] = True

    monkeypatch.setattr(wmod, "_run_worker_async", _fake_run_worker_async)
    wmod._worker_thread_target()
    assert state["ran"] is True
    # Module-level state should have been cleared in the finally block
    assert cmod.get_temporal_client() is None
    assert cmod.get_temporal_loop() is None


def test_worker_thread_target_handles_exception(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from soc2_compliance_team.temporal import worker as wmod

    async def _explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(wmod, "_run_worker_async", _explode)
    with caplog.at_level("ERROR"):
        wmod._worker_thread_target()
    assert any("Temporal worker failed" in r.message for r in caplog.records)


def test_worker_thread_target_handles_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from soc2_compliance_team.temporal import worker as wmod

    async def _cancelled():
        raise asyncio.CancelledError

    monkeypatch.setattr(wmod, "_run_worker_async", _cancelled)
    # Should swallow CancelledError silently
    wmod._worker_thread_target()


def test_start_soc2_temporal_worker_thread_disabled_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from soc2_compliance_team.temporal import worker as wmod

    assert wmod.start_soc2_temporal_worker_thread() is False


def test_start_soc2_temporal_worker_thread_creates_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start path: when enabled and no live thread exists, a new thread is
    spawned and the function returns True."""
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from soc2_compliance_team.temporal import worker as wmod

    monkeypatch.setattr(wmod, "_worker_thread", None)

    spawned: dict[str, Any] = {}

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            spawned["target"] = target
            spawned["name"] = name
            self.daemon = daemon
            self._alive = True

        def start(self):
            spawned["started"] = True

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(wmod.threading, "Thread", _FakeThread)
    assert wmod.start_soc2_temporal_worker_thread() is True
    assert spawned["started"] is True
    assert spawned["name"] == "soc2-temporal-worker"


def test_start_soc2_temporal_worker_thread_returns_true_when_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from soc2_compliance_team.temporal import worker as wmod

    class _AliveThread:
        def is_alive(self):
            return True

    monkeypatch.setattr(wmod, "_worker_thread", _AliveThread())
    assert wmod.start_soc2_temporal_worker_thread() is True


# ---------------------------------------------------------------------------
# temporal/__init__ re-exports
# ---------------------------------------------------------------------------


def test_temporal_init_reexports() -> None:
    from soc2_compliance_team import temporal as tmod

    assert callable(tmod.is_temporal_enabled)
    assert tmod.TASK_QUEUE  # non-empty string

"""Regression tests for the user_agent_founder Temporal bootstrap.

Guards the migration hazards the wiring was designed to avoid:

1. **Self-bootstrap at import time.** Importing ``agent_team_studio.user_agent_founder.temporal``
   used to call ``shared.temporal.start_team_worker(...)`` at module load. The
   worker thread connects the Temporal client asynchronously, so the first
   ``start_founder_workflow`` call lost the race and raised ``RuntimeError:
   Temporal client not available``. Boot is now the team_service entrypoint's job
   (or the API lifespan as a backstop).

2. **Workflow sandbox restrictions.** The temporalio sandbox re-imports the
   workflow module to load ``UserAgentFounderWorkflow``. The package ``__init__``
   and the ``workflows`` module (and its passthrough imports) must register
   inside the real sandbox without a ``RestrictedWorkflowAccessError``.

3. **Full activity registration.** The decomposition introduced per-step
   activities; guard that every one is exported so a rename can't silently drop
   one from the worker's registration.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package must NOT spin up a worker thread."""
    import shared.temporal

    _purge("agent_team_studio.user_agent_founder.temporal")
    with mock.patch.object(shared.temporal, "start_team_worker") as patched:
        importlib.import_module("agent_team_studio.user_agent_founder.temporal")
        importlib.import_module("agent_team_studio.user_agent_founder.temporal.activities")
        importlib.import_module("agent_team_studio.user_agent_founder.temporal.workflows")
        importlib.import_module("agent_team_studio.user_agent_founder.temporal.start_workflow")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}). This causes a race on the "
            f"first request and a temporalio sandbox os.getenv violation when "
            f"the workflow registers."
        )


def test_workflow_registers_in_temporalio_sandbox():
    """Ground truth for sandbox safety: ``UserAgentFounderWorkflow`` (which imports
    the team's activities under ``workflow.unsafe.imports_passed_through()``) must
    import and instantiate inside the real ``SandboxedWorkflowRunner`` with
    ``shared.temporal``'s passthrough restrictions, with no
    ``RestrictedWorkflowAccessError``. This exercises exactly what the worker does
    at boot — a stronger check than an ``os.getenv`` import-spy proxy.
    """
    import asyncio

    import temporalio.workflow as _wf

    from shared.temporal.worker import _build_workflow_runner

    _purge("agent_team_studio.user_agent_founder.temporal")
    from agent_team_studio.user_agent_founder.temporal import WORKFLOWS

    async def _prepare() -> None:
        runner = _build_workflow_runner()
        for wfc in WORKFLOWS:
            runner.prepare_workflow(_wf._Definition.must_from_class(wfc))

    asyncio.run(_prepare())


def test_activities_list_exposes_every_step_activity():
    """The worker registers the whole ``ACTIVITIES`` list; guard that all seven
    fine-grained activities are exported so a rename can't silently drop one."""
    from agent_team_studio.user_agent_founder.temporal import ACTIVITIES

    names = {getattr(a, "__name__", None) for a in ACTIVITIES}
    assert names == {
        "begin_run_activity",
        "generate_spec_activity",
        "enter_phase_activity",
        "poll_phase_activity",
        "answer_questions_activity",
        "finalize_run_activity",
        "mark_failed_activity",
    }


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py looks up ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE``. Keep that contract pinned so a rename
    can't silently break docker-compose.
    """
    from agent_team_studio.user_agent_founder.temporal import worker

    fn = getattr(worker, "start_user_agent_founder_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg "
        "start_user_agent_founder_temporal_worker_thread() in "
        "agent_team_studio.user_agent_founder.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """Standalone uvicorn dev path: with TEMPORAL_ADDRESS unset, the
    backstop in the lifespan must return False instead of raising."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from agent_team_studio.user_agent_founder.temporal.worker import (
        start_user_agent_founder_temporal_worker_thread,
    )

    assert start_user_agent_founder_temporal_worker_thread() is False


def test_start_workflow_waits_for_client_then_raises(monkeypatch):
    """When the worker is genuinely not running, the helper must time out
    with the original error message — not raise immediately and not wait
    forever.
    """
    from agent_team_studio.user_agent_founder.temporal import start_workflow as sw

    monkeypatch.setattr(sw, "get_temporal_client", lambda: None)
    monkeypatch.setattr(sw, "get_temporal_loop", lambda: None)
    monkeypatch.setattr(sw, "CLIENT_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(sw, "CLIENT_READY_POLL_S", 0.01)

    import pytest

    with pytest.raises(RuntimeError, match="Temporal client not available"):
        sw._wait_for_client()


def test_cancel_founder_workflow_signals_the_workflow(monkeypatch):
    """The cancel helper delivers the ``cancel`` signal to the founder workflow id,
    capping BOTH the client-ready wait and the signal RPC's own timeout so a
    /cancel can never block on either default (10s / 30s)."""
    from agent_team_studio.user_agent_founder.temporal import WORKFLOW_ID_PREFIX
    from agent_team_studio.user_agent_founder.temporal import start_workflow as sw

    captured: dict = {}

    def _fake_signal(workflow_id, signal_name, *args, **kw):
        captured.update(workflow_id=workflow_id, signal_name=signal_name, args=args, **kw)

    monkeypatch.setattr(sw, "signal_workflow_sync", _fake_signal)

    sw.cancel_founder_workflow("run-abc")

    assert captured["workflow_id"] == f"{WORKFLOW_ID_PREFIX}run-abc"
    assert captured["signal_name"] == "cancel"
    assert captured["client_ready_timeout_s"] == sw.CANCEL_CLIENT_READY_TIMEOUT_S
    assert captured["timeout_s"] == sw.CANCEL_SIGNAL_RPC_TIMEOUT_S


def test_run_async_translates_future_timeout_to_runtime_error(monkeypatch):
    """A stuck coroutine must surface as the documented RuntimeError, not the
    bare concurrent.futures.TimeoutError Future.result raises — so every
    caller (start_founder_workflow, _dispatch_founder_run) sees one failure
    type instead of an undocumented stdlib one."""
    from agent_team_studio.user_agent_founder.temporal import start_workflow as sw

    monkeypatch.setattr(sw, "_wait_for_client", lambda *a, **k: (object(), object()))

    class _StuckFuture:
        def result(self, timeout=None):
            raise TimeoutError("future timed out")

    monkeypatch.setattr(sw.asyncio, "run_coroutine_threadsafe", lambda coro, loop: _StuckFuture())

    import pytest

    with pytest.raises(RuntimeError, match=f"did not complete within {sw.START_WORKFLOW_TIMEOUT}s"):
        sw._run_async(object())


def test_start_founder_workflow_dispatches_with_prefixed_id(monkeypatch):
    """The start helper starts ``UserAgentFounderWorkflow`` with the prefixed id
    and the team task queue, on the worker's shared loop."""
    from unittest.mock import MagicMock

    from agent_team_studio.user_agent_founder.temporal import (
        TASK_QUEUE,
        WORKFLOW_ID_PREFIX,
        UserAgentFounderWorkflow,
    )
    from agent_team_studio.user_agent_founder.temporal import start_workflow as sw

    fake_client = MagicMock(name="client")
    fake_loop = object()
    monkeypatch.setattr(sw, "_wait_for_client", lambda *a, **k: (fake_client, fake_loop))

    class _Fut:
        def result(self, timeout=None):
            return None

    captured: dict = {}

    def _fake_run_coroutine_threadsafe(coro, loop):
        captured["loop"] = loop
        coro.close()  # the MagicMock coroutine stand-in; avoid a warning
        return _Fut()

    monkeypatch.setattr(sw.asyncio, "run_coroutine_threadsafe", _fake_run_coroutine_threadsafe)

    sw.start_founder_workflow("run-1")

    fake_client.start_workflow.assert_called_once()
    args, kwargs = fake_client.start_workflow.call_args
    assert args[0] is UserAgentFounderWorkflow.run
    assert args[1] == "run-1"
    assert kwargs["id"] == f"{WORKFLOW_ID_PREFIX}run-1"
    assert kwargs["task_queue"] == TASK_QUEUE
    assert captured["loop"] is fake_loop

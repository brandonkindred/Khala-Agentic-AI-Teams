"""Unit tests for the Planning V3 Temporal start-workflow client-readiness wait.

The worker connects its client/loop in a daemon thread, so the first request
after a cold start can race the connect. ``_wait_for_client`` turns that race
into a short bounded wait while the worker is connecting, but fails fast when
no worker is running at all (so a Temporal outage doesn't tie up the threadpool).
"""

import sys
import unittest.mock as mock
from pathlib import Path

import pytest

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))


def test_wait_for_client_times_out_and_raises(monkeypatch):
    """When a worker IS running but never connects, the helper polls until the
    deadline and raises — it does not raise on the first miss and does not hang."""
    from planning_v3_team.temporal import start_workflow as sw

    monkeypatch.setattr(sw, "get_temporal_client", lambda: None)
    monkeypatch.setattr(sw, "get_temporal_loop", lambda: None)
    monkeypatch.setattr(sw, "_worker_starting", lambda: True)
    monkeypatch.setattr(sw, "CLIENT_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(sw, "CLIENT_READY_POLL_S", 0.01)

    with pytest.raises(RuntimeError, match="cannot reach Temporal"):
        sw._wait_for_client()


def test_wait_for_client_fails_fast_when_worker_absent(monkeypatch):
    """When no worker thread is running, the helper raises immediately instead
    of blocking the full timeout — so a Temporal outage can't pin threadpool
    threads for 10s each on a sync endpoint."""
    from planning_v3_team.temporal import start_workflow as sw

    monkeypatch.setattr(sw, "get_temporal_client", lambda: None)
    monkeypatch.setattr(sw, "get_temporal_loop", lambda: None)
    monkeypatch.setattr(sw, "_worker_starting", lambda: False)

    slept = []
    monkeypatch.setattr(sw.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(RuntimeError, match="worker is not running"):
        sw._wait_for_client()
    assert slept == []  # never entered the poll/sleep loop


def test_wait_for_client_returns_once_connected(monkeypatch):
    """Returns the (client, loop) pair as soon as both globals are populated,
    without consulting the worker-liveness probe."""
    from planning_v3_team.temporal import start_workflow as sw

    sentinel_client = object()
    sentinel_loop = object()
    monkeypatch.setattr(sw, "get_temporal_client", lambda: sentinel_client)
    monkeypatch.setattr(sw, "get_temporal_loop", lambda: sentinel_loop)

    client, loop = sw._wait_for_client()
    assert client is sentinel_client
    assert loop is sentinel_loop


def test_wait_for_client_waits_for_loop_after_client(monkeypatch):
    """The worker sets the client just before the loop; the helper must wait
    for BOTH so callers never see a half-initialised state."""
    from planning_v3_team.temporal import start_workflow as sw

    sentinel_client = object()
    sentinel_loop = object()
    # Client is ready immediately; loop lags for the first two polls.
    loop_states = [None, None, sentinel_loop]
    monkeypatch.setattr(sw, "get_temporal_client", lambda: sentinel_client)
    monkeypatch.setattr(
        sw, "get_temporal_loop", lambda: loop_states.pop(0) if loop_states else sentinel_loop
    )
    monkeypatch.setattr(sw, "_worker_starting", lambda: True)
    monkeypatch.setattr(sw, "CLIENT_READY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(sw, "CLIENT_READY_POLL_S", 0.001)

    client, loop = sw._wait_for_client()
    assert client is sentinel_client
    assert loop is sentinel_loop


def test_start_planning_v3_workflow_dispatches(monkeypatch):
    """start_planning_v3_workflow resolves the client, builds the workflow-start
    coroutine, and runs it on the worker loop via _run_async."""
    from planning_v3_team.temporal import start_workflow as sw

    fake_client = mock.MagicMock()
    fake_loop = object()
    monkeypatch.setattr(sw, "get_temporal_client", lambda: fake_client)
    monkeypatch.setattr(sw, "get_temporal_loop", lambda: fake_loop)

    captured = {}

    class _FakeFuture:
        def result(self, timeout=None):
            captured["timeout"] = timeout
            return "started"

    def _fake_run_coroutine_threadsafe(coro, loop):
        captured["coro"] = coro
        captured["loop"] = loop
        return _FakeFuture()

    monkeypatch.setattr(sw.asyncio, "run_coroutine_threadsafe", _fake_run_coroutine_threadsafe)

    sw.start_planning_v3_workflow("job-1", "/tmp/ws", "Acme", "brief", None, False, False, False)

    # The workflow was started on the right queue/id with job_id as first arg.
    fake_client.start_workflow.assert_called_once()
    _, kwargs = fake_client.start_workflow.call_args
    assert kwargs["id"].endswith("job-1")
    assert kwargs["task_queue"] == sw.TASK_QUEUE
    assert kwargs["args"][0] == "job-1"
    # _run_async submitted that coroutine to the worker loop with the RPC timeout.
    assert captured["coro"] is fake_client.start_workflow.return_value
    assert captured["loop"] is fake_loop
    assert captured["timeout"] == sw.START_WORKFLOW_TIMEOUT

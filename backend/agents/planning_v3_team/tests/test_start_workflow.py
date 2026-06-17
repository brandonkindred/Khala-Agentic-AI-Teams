"""Unit tests for the Planning V3 Temporal start-workflow client-readiness wait.

The worker connects its client/loop in a daemon thread, so the first request
after a cold start can race the connect. ``_wait_for_client`` turns that race
into a short bounded wait instead of an immediate 500.
"""

import sys
from pathlib import Path

import pytest

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))


def test_wait_for_client_times_out_and_raises(monkeypatch):
    """When the worker never connects, the helper polls then raises the
    original 'Temporal client not available' error — it does not hang forever
    and does not raise on the first miss."""
    from planning_v3_team.temporal import start_workflow as sw

    monkeypatch.setattr(sw, "get_temporal_client", lambda: None)
    monkeypatch.setattr(sw, "get_temporal_loop", lambda: None)
    monkeypatch.setattr(sw, "CLIENT_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(sw, "CLIENT_READY_POLL_S", 0.01)

    with pytest.raises(RuntimeError, match="Temporal client not available"):
        sw._wait_for_client()


def test_wait_for_client_returns_once_connected(monkeypatch):
    """Returns the (client, loop) pair as soon as both globals are populated."""
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
    monkeypatch.setattr(sw, "CLIENT_READY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(sw, "CLIENT_READY_POLL_S", 0.001)

    client, loop = sw._wait_for_client()
    assert client is sentinel_client
    assert loop is sentinel_loop

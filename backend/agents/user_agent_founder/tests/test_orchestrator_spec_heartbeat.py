"""Regression test for #262: Phase 1 spec generation must emit heartbeats.

Before the fix, ``orchestrator.run_workflow`` called ``agent.generate_spec()``
synchronously with no heartbeat. Slow LLM providers pushed the run past the
stale-job monitor's threshold (typically 60-120s), causing the Jobs Dashboard
and the store to flip to ``failed`` even when Phases 2 & 3 later succeeded.

The fix wraps ``generate_spec()`` in a daemon thread that fires
``_heartbeat(run_id)`` every ``SPEC_HEARTBEAT_INTERVAL`` seconds. These tests
pin the behaviour.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest


def test_heartbeat_fires_during_slow_generate_spec(monkeypatch):
    """A slow generate_spec must produce multiple heartbeats while blocked."""
    from user_agent_founder import orchestrator

    heartbeat_calls: list[str] = []
    monkeypatch.setattr(orchestrator, "_heartbeat", lambda run_id: heartbeat_calls.append(run_id))
    monkeypatch.setattr(orchestrator, "SPEC_HEARTBEAT_INTERVAL", 0.1)

    agent = MagicMock()

    def slow_spec() -> str:
        time.sleep(0.55)
        return "# Spec body"

    agent.generate_spec = slow_spec

    result = orchestrator._generate_spec_with_heartbeat(agent, "run-abc-123456")

    assert result == "# Spec body"
    # With interval=0.1s and a 0.55s sleep, we expect ~5 heartbeats.
    # Assert >=2 to tolerate scheduler jitter while still proving the loop ran.
    assert len(heartbeat_calls) >= 2, (
        f"Expected multiple heartbeats during slow generate_spec; got {len(heartbeat_calls)}"
    )
    assert all(rid == "run-abc-123456" for rid in heartbeat_calls)


def test_heartbeat_thread_stops_after_generate_spec_returns(monkeypatch):
    """The heartbeat thread must terminate once generate_spec returns."""
    from user_agent_founder import orchestrator

    monkeypatch.setattr(orchestrator, "_heartbeat", lambda run_id: None)
    monkeypatch.setattr(orchestrator, "SPEC_HEARTBEAT_INTERVAL", 0.05)

    agent = MagicMock()
    agent.generate_spec = lambda: "spec"

    before_threads = {t.name for t in threading.enumerate()}
    orchestrator._generate_spec_with_heartbeat(agent, "run-xyz-987654")

    # Give the daemon a beat to exit via the stop event + join(timeout=1).
    time.sleep(0.1)
    after_threads = {t.name for t in threading.enumerate()}

    leaked = {n for n in (after_threads - before_threads) if n.startswith("founder-spec-hb-")}
    assert not leaked, f"Heartbeat thread leaked after generate_spec returned: {leaked}"


def test_heartbeat_thread_stops_when_generate_spec_raises(monkeypatch):
    """Exceptions from generate_spec must still stop the heartbeat thread."""
    from user_agent_founder import orchestrator

    monkeypatch.setattr(orchestrator, "_heartbeat", lambda run_id: None)
    monkeypatch.setattr(orchestrator, "SPEC_HEARTBEAT_INTERVAL", 0.05)

    agent = MagicMock()

    def boom() -> str:
        raise RuntimeError("LLM blew up")

    agent.generate_spec = boom

    before_threads = {t.name for t in threading.enumerate()}
    with pytest.raises(RuntimeError, match="LLM blew up"):
        orchestrator._generate_spec_with_heartbeat(agent, "run-boom-1")

    time.sleep(0.1)
    after_threads = {t.name for t in threading.enumerate()}

    leaked = {n for n in (after_threads - before_threads) if n.startswith("founder-spec-hb-")}
    assert not leaked, f"Heartbeat thread leaked after exception: {leaked}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

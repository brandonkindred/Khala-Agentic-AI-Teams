"""Scheduled background pruning for se_agent_traces (SE_TRACE_RETENTION_DAYS).

trace_store.prune_traces() deletes se_agent_traces rows older than the
configured retention window but is never invoked on its own — nothing calls
it without a scheduler. This module runs it on a BackgroundHeartbeat at an
hours-scale cadence, registered/unregistered through the same lifespan hooks
as trace_flusher (see api/lifecycle.py).

Invariants:
    - A prune failure never raises into the heartbeat thread or the caller:
      trace_store.prune_traces already swallows and logs its own failures,
      and BackgroundHeartbeat's tick loop swallows any exception from its
      beat callable as a second, independent layer — this module adds no
      further try/except that could undo either guarantee.
    - Registration does not gate on SE_TRACE_TO_POSTGRES: prune_traces only
      checks POSTGRES_HOST (via pg_cursor()), because rows written while
      tracing was enabled must still be pruned even if tracing is later
      turned off.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from shared.concurrency.heartbeat import BackgroundHeartbeat
from software_engineering_team.shared import trace_store
from software_engineering_team.shared.env_config import env_float

logger = logging.getLogger(__name__)

_heartbeat: Optional[BackgroundHeartbeat] = None
_registered = False
_register_lock = threading.Lock()


def _prune_interval_s() -> float:
    """Seconds between background prune sweeps (env ``SE_TRACE_PRUNE_INTERVAL_S``).

    Postconditions: returns a non-negative float; garbage/unset env -> default
    ``21600.0`` (6h). Retention itself is day-granularity, so a tighter
    interval buys nothing.
    """
    return env_float("SE_TRACE_PRUNE_INTERVAL_S", 21600.0, 0.0)


def _prune_tick() -> None:
    """Run one scheduled prune sweep. Never raises (see module Invariants)."""
    removed = trace_store.prune_traces()
    if removed:
        logger.info("pruned %d stale se_agent_traces row(s)", removed)


def register_trace_pruner() -> None:
    """Start the background prune heartbeat (idempotent).

    No try/except around heartbeat construction: unlike trace_flusher's
    registration, there is no external llm_service call to guard here —
    BackgroundHeartbeat's constructor/start() are pure/safe — and the caller
    (_se_startup) already wraps all telemetry registrations in one shared
    try/except.

    Postconditions:
        - A background heartbeat is running that calls prune_traces() on the
          configured interval. Safe to call more than once (a no-op after the
          first call).
    """
    global _heartbeat, _registered
    with _register_lock:
        if _registered:
            return
        _heartbeat = BackgroundHeartbeat(
            _prune_tick,
            max(_prune_interval_s(), 60.0),  # floor: never busy-loop on a 0/garbage interval
            name="se-trace-pruner",
        )
        _heartbeat.start()
        _registered = True


def unregister() -> None:
    """Stop the prune heartbeat.

    Postconditions:
        - The heartbeat thread is stopped (joined). Safe to call when never
          registered (no-op).
    """
    global _heartbeat, _registered
    with _register_lock:
        if not _registered:
            return
        hb = _heartbeat
        _heartbeat = None
        _registered = False
    if hb is not None:
        hb.stop()


def shutdown() -> None:
    """Lifecycle shutdown: stop the prune heartbeat.

    No final "drain" step is needed here, unlike trace_flusher: pruning is
    idempotent and re-runnable — a missed final tick just means slightly
    stale rows until the next scheduled sweep, never lost data.
    """
    unregister()


# ---------------------------------------------------------------------------
# Test-only accessors (no production callers) — mirrors trace_flusher's seams
# ---------------------------------------------------------------------------


def _is_registered() -> bool:
    return _registered


def _mark_registered_for_test() -> None:
    """Pretend registration already happened so unregister() has work to do."""
    global _registered
    _registered = True


def _set_heartbeat_for_test() -> None:
    """Attach a real heartbeat so unregister() exercises the stop path."""
    global _heartbeat
    _heartbeat = BackgroundHeartbeat(_prune_tick, 3600.0, name="se-trace-pruner-test")
    _heartbeat.start()


def _reset_for_test() -> None:
    """Clear all module state between tests (registration, heartbeat)."""
    global _heartbeat, _registered
    # Stop any heartbeat a prior test may have started; best-effort (no raise).
    hb = _heartbeat
    _heartbeat = None
    if hb is not None:
        try:
            hb.stop()
        except Exception:  # pragma: no cover - BackgroundHeartbeat.stop never raises; defensive only
            pass
    _registered = False


__all__ = [
    "register_trace_pruner",
    "unregister",
    "shutdown",
]

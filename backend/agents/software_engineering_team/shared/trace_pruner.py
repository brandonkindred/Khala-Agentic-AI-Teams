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
      beat callable as a second, independent layer — _prune_tick adds no
      further try/except that could undo either guarantee. A heartbeat
      construction/start failure is a separate concern, guarded inside
      register_trace_pruner itself (see its docstring).
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

    Postconditions:
        - A background heartbeat is running that calls prune_traces() on the
          configured interval, ticking once immediately on start (beat_first)
          so a restart landing more often than the interval still gets a
          sweep in. Safe to call more than once (a no-op after the first
          successful call). A heartbeat construction/start failure is logged
          and leaves state as if this call never happened, rather than
          propagating into _se_startup's shared try/except and skipping the
          registrations after it.
    """
    global _heartbeat
    with _register_lock:
        if _heartbeat is not None:
            return
        try:
            heartbeat = BackgroundHeartbeat(
                _prune_tick,
                max(_prune_interval_s(), 60.0),  # floor: never busy-loop on a 0/garbage interval
                name="se-trace-pruner",
                beat_first=True,
            )
            heartbeat.start()
        except Exception:
            logger.warning("could not start SE trace pruner heartbeat", exc_info=True)
            return
        _heartbeat = heartbeat


def unregister() -> None:
    """Stop the prune heartbeat. Safe to call when never registered (no-op)."""
    global _heartbeat
    with _register_lock:
        hb = _heartbeat
        _heartbeat = None
    if hb is not None:
        hb.stop()


# ---------------------------------------------------------------------------
# Test-only accessors (no production callers)
# ---------------------------------------------------------------------------


def _is_registered() -> bool:
    return _heartbeat is not None


def _reset_for_test() -> None:
    """Clear all module state between tests (heartbeat)."""
    global _heartbeat
    hb = _heartbeat
    _heartbeat = None
    if hb is not None:
        hb.stop()


__all__ = [
    "register_trace_pruner",
    "unregister",
]

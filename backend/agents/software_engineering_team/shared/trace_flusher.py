"""Batched, off-hot-path persistence for SE LLM-call traces.

The trace observer used to call :func:`trace_store.write_trace` synchronously on
the LLM-call thread — one blocking Postgres round-trip per call (see the
guidance at ``llm_service/telemetry.py`` that observers must not block). This
module keeps the observer on the call path but makes it do **zero DB I/O**: it
builds the 16-column row tuple (pure Python, via
:func:`trace_store._record_to_row`) and appends it to a bounded in-memory deque.

A :class:`BackgroundHeartbeat` daemon thread drains the deque on an interval and
writes the whole batch in one ``executemany`` (:func:`trace_store.write_rows`).
On clean shutdown :func:`shutdown` stops the heartbeat, unregisters the
observer, and does a final synchronous drain — all before the shared Postgres
pool is closed (see ``api/lifecycle.py`` → ``shared/app/factory.py``).

Invariants:
    - The deque never exceeds ``SE_TRACE_BUFFER_MAX`` entries; overflow drops
      the oldest row and logs at WARNING once per sustained burst (throttled so
      a DB outage or sustained burst cannot flood the log; bounded memory,
      never blocks callers).
    - When the Postgres trace sink is disabled (``SE_TRACE_TO_POSTGRES``
      explicitly set to a falsy value; enabled by default) the observer
      enqueues nothing — the drain would drop these rows anyway, so buffering
      them only adds per-call work and can fill the buffer with
      never-persisted rows.
    - A flush failure never raises into the heartbeat thread or the caller; it
      is logged at DEBUG and the rows are dropped (telemetry must not break the
      LLM call path or the flusher).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Optional

from shared.concurrency.heartbeat import BackgroundHeartbeat
from software_engineering_team.shared import trace_store
from software_engineering_team.shared.env_config import env_float, env_int

logger = logging.getLogger(__name__)


# Exact SE team aliases (not a ``startswith`` prefix, so an unrelated team that
# merely shares the prefix is never captured) — mirrors cost_tracker._cost_observer.
# Both ids occur: attribution sets ``software_engineering`` while the job store's
# JobServiceClient is constructed with ``software_engineering_team``.
_SE_TEAMS = frozenset({"software_engineering", "software_engineering_team"})

_buffer: deque = deque()
_buffer_lock = threading.Lock()
_heartbeat: Optional[BackgroundHeartbeat] = None
_registered = False
_register_lock = threading.Lock()

# Throttle the overflow warning so a sustained burst does not flood the log —
# one warning per burst is enough to surface the cap was hit.
_overflow_warned = False


def _max_buffer() -> int:
    """Max rows buffered before the oldest is dropped (env ``SE_TRACE_BUFFER_MAX``).

    Postconditions: returns a positive int (floor 1); garbage env → default 1000.
    """
    return max(1, env_int("SE_TRACE_BUFFER_MAX", 1000, floor=1))


def _flush_interval_s() -> float:
    """Seconds between background drains (env ``SE_TRACE_FLUSH_INTERVAL_S``).

    Postconditions: returns a non-negative float; garbage env → default ``2.0``
    (mirrors ``SE_COST_FLUSH_INTERVAL_S``).
    """
    return env_float("SE_TRACE_FLUSH_INTERVAL_S", 2.0, 0.0)


def _trace_observer(record: Any) -> None:
    """Enqueue one SE-attributed LLM call row; zero DB I/O on the call path.

    Only records with a ``job_id`` whose ``team`` is in :data:`_SE_TEAMS`
    contribute, so this stays a no-op for other teams sharing the process. The
    row tuple is built eagerly (snapshotting the record's fields), so a later
    mutation of the record by the caller cannot corrupt the buffered row.

    Skips the per-call ``_record_to_row`` + enqueue work entirely when the
    Postgres trace sink is disabled (``SE_TRACE_TO_POSTGRES`` explicitly set to
    a falsy value; enabled by default): the background drain would drop these
    rows anyway (``write_rows`` re-checks the flag), and on a high-throughput
    job buffering them would fill the buffer and emit drop warnings for rows
    that are never persisted.
    """
    global _overflow_warned
    if not trace_store._trace_enabled():
        return
    team = getattr(record, "team", "") or ""
    if not getattr(record, "job_id", "") or team not in _SE_TEAMS:
        return
    row = trace_store._record_to_row(record)
    cap = _max_buffer()
    with _buffer_lock:
        _buffer.append(row)
        dropped = 0
        while len(_buffer) > cap:
            _buffer.popleft()
            dropped += 1
        overflowed = dropped > 0
        # Throttle the overflow warning to once per sustained burst: warn only
        # on the first over-cap call since the buffer last dropped below cap
        # (which resets _overflow_warned). Without this, every over-cap call
        # would log a WARNING — flooding logs during a DB outage or sustained
        # burst. should_warn is decided under the lock; the log fires once,
        # outside it.
        should_warn = overflowed and not _overflow_warned
        if should_warn:
            _overflow_warned = True
        elif not overflowed:
            _overflow_warned = False
    if should_warn:
        logger.warning(
            "SE trace buffer full (cap=%d); dropping oldest %d trace row(s)", cap, dropped
        )


def _drain() -> int:
    """Flush all buffered rows in one batch; return how many were written.

    Failures are swallowed and logged (never raise) — a flush error must not kill
    the heartbeat thread. The batch is snapshots under the lock and written
    outside it so a slow ``executemany`` does not block the call-path observer.
    """
    with _buffer_lock:
        if not _buffer:
            return 0
        batch = list(_buffer)
        _buffer.clear()
    try:
        return trace_store.write_rows(batch)
    except Exception:
        logger.debug("failed to flush %d SE trace row(s)", len(batch), exc_info=True)
        return 0


def drain() -> int:
    """Synchronous one-shot drain of the buffer (used by shutdown).

    Postconditions: returns the number of rows written; 0 if the buffer was
    empty or the write failed. Never raises.
    """
    return _drain()


def register_trace_flusher() -> None:
    """Register the observer + start the background drain heartbeat (idempotent).

    The observer and the batched write are both no-ops when
    ``SE_TRACE_TO_POSTGRES`` is explicitly disabled (or Postgres is
    unconfigured), so registering unconditionally at startup is safe and
    cheap. Safe to call from app startup more than once.
    """
    global _heartbeat, _registered
    with _register_lock:
        if _registered:
            return
        try:
            _register_call_observer(_trace_observer)
        except Exception:
            logger.warning("could not register SE trace flusher observer", exc_info=True)
            return
        _heartbeat = BackgroundHeartbeat(
            _drain,
            max(_flush_interval_s(), 0.1),  # a 0 interval would busy-loop; floor at 0.1s
            name="se-trace-flusher",
        )
        _heartbeat.start()
        _registered = True


def unregister() -> None:
    """Stop the heartbeat and remove the observer from :mod:`llm_service`.

    Postconditions: the drain thread is stopped (joined) and the observer is
    unregistered so no further rows are enqueued. Safe to call when never
    registered (no-op). Does NOT flush — call :func:`drain` afterward for the
    final batch, or :func:`shutdown` for both in the right order.
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
    try:
        _unregister_call_observer(_trace_observer)
    except Exception:
        logger.debug("could not unregister SE trace flusher observer", exc_info=True)


def shutdown() -> None:
    """Lifecycle shutdown: stop enqueuing, then flush the remaining buffer.

    Order matters: :func:`unregister` first (stop the heartbeat + remove the
    observer so no new rows arrive), then :func:`drain` for the final synchronous
    flush. Called from ``_se_shutdown`` before the shared Postgres pool closes,
    so the final drain can still use the pool.
    """
    unregister()
    drain()


# Thin wrappers so tests can monkeypatch the llm_service registration without
# importing the real module (which pulls the LLM client stack into the test).
def _register_call_observer(observer: Any) -> None:
    from llm_service import register_call_observer

    register_call_observer(observer)


def _unregister_call_observer(observer: Any) -> None:
    from llm_service import unregister_call_observer

    unregister_call_observer(observer)


# ---------------------------------------------------------------------------
# Test-only accessors (no production callers)
# ---------------------------------------------------------------------------


def _buffer_size() -> int:
    with _buffer_lock:
        return len(_buffer)


def _snapshot_buffer() -> list:
    with _buffer_lock:
        return list(_buffer)


def _is_registered() -> bool:
    return _registered


def _mark_registered_for_test() -> None:
    """Pretend registration already happened so unregister() has work to do."""
    global _registered
    _registered = True


def _set_heartbeat_for_test() -> None:
    """Attach a real heartbeat so unregister() exercises the stop path."""
    global _heartbeat
    _heartbeat = BackgroundHeartbeat(_drain, 60.0, name="se-trace-flusher-test")
    _heartbeat.start()


def _reset_for_test() -> None:
    """Clear all module state between tests (buffer, registration, heartbeat)."""
    global _heartbeat, _registered, _overflow_warned
    # Stop any heartbeat a prior test may have started; best-effort (no raise).
    hb = _heartbeat
    _heartbeat = None
    if hb is not None:
        try:
            hb.stop()
        except (
            Exception
        ):  # pragma: no cover - BackgroundHeartbeat.stop never raises; defensive only
            pass
    with _buffer_lock:
        _buffer.clear()
    _registered = False
    _overflow_warned = False


__all__ = [
    "register_trace_flusher",
    "unregister",
    "drain",
    "shutdown",
]

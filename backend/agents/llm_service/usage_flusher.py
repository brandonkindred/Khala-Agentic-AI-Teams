"""Batched, off-hot-path persistence for platform-wide LLM token usage.

Observers must not block the LLM call path (see ``llm_service/telemetry.py``).
This module keeps the observer on the call path but makes it do **zero DB I/O**:
it builds the 8-column row tuple (pure Python, via
:func:`usage_store.record_to_row`) and appends it to a bounded in-memory deque.

A :class:`BackgroundHeartbeat` daemon thread drains the deque on an interval and
writes the whole batch in one ``executemany`` (:func:`usage_store.write_rows`).
On clean shutdown :func:`shutdown` stops the heartbeat, unregisters the
observer, and does a final synchronous drain — all before the shared Postgres
pool is closed (see ``unified_api/main.py`` lifespan).

Unlike the SE-only :mod:`software_engineering_team.shared.trace_flusher`, this
flusher enqueues **every** team when Postgres is enabled (no opt-in flag, no
``job_id`` / team filter).

Invariants:
    - The deque never exceeds ``LLM_USAGE_BUFFER_MAX`` entries; overflow drops
      the oldest row and logs at WARNING once per sustained burst (throttled so
      a DB outage or sustained burst cannot flood the log; bounded memory,
      never blocks callers).
    - When Postgres is unset the observer enqueues nothing — the drain would
      drop these rows anyway, so buffering them only adds per-call work.
    - A flush failure never raises into the heartbeat thread or the caller; it
      is logged at DEBUG and the rows are dropped (telemetry must not break the
      LLM call path or the flusher).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Optional

from llm_service import usage_store
from shared.concurrency.heartbeat import BackgroundHeartbeat
from shared.env_config import env_float, env_int
from shared.postgres import is_postgres_enabled

logger = logging.getLogger(__name__)

_buffer: deque = deque()
_buffer_lock = threading.Lock()
_heartbeat: Optional[BackgroundHeartbeat] = None
_registered = False
_register_lock = threading.Lock()

# Throttle the overflow warning so a sustained burst does not flood the log —
# one warning per burst is enough to surface the cap was hit.
_overflow_warned = False


def _max_buffer() -> int:
    """Max rows buffered before the oldest is dropped (env ``LLM_USAGE_BUFFER_MAX``).

    Postconditions: returns a positive int (floor 1); garbage env → default 1000.
    """
    return max(1, env_int("LLM_USAGE_BUFFER_MAX", 1000, floor=1))


def _flush_interval_s() -> float:
    """Seconds between background drains (env ``LLM_USAGE_FLUSH_INTERVAL_S``).

    Postconditions: returns a non-negative float; garbage env → default ``2.0``.
    """
    return env_float("LLM_USAGE_FLUSH_INTERVAL_S", 2.0, floor=0.0)


def _usage_observer(record: Any) -> None:
    """Enqueue one LLM call usage row; zero DB I/O on the call path.

    Enqueues every record when :func:`is_postgres_enabled` is true (all teams).
    The row tuple is built eagerly (snapshotting the record's fields), so a later
    mutation of the record by the caller cannot corrupt the buffered row.

    Skips enqueue work entirely when Postgres is unset: the background drain
    would drop these rows anyway (``write_rows`` re-checks the flag).
    """
    global _overflow_warned
    if not is_postgres_enabled():
        return
    row = usage_store.record_to_row(record)
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
            "LLM usage buffer full (cap=%d); dropping oldest %d usage row(s)", cap, dropped
        )


def _drain() -> int:
    """Flush all buffered rows in one batch; return how many were written.

    Failures are swallowed and logged (never raise) — a flush error must not kill
    the heartbeat thread. The batch is snapshotted under the lock and written
    outside it so a slow ``executemany`` does not block the call-path observer.
    """
    with _buffer_lock:
        if not _buffer:
            return 0
        batch = list(_buffer)
        _buffer.clear()
    try:
        return usage_store.write_rows(batch)
    except Exception:
        logger.debug("failed to flush %d LLM usage row(s)", len(batch), exc_info=True)
        return 0


def drain() -> int:
    """Synchronous one-shot drain of the buffer (used by shutdown).

    Postconditions: returns the number of rows written; 0 if the buffer was
    empty or the write failed. Never raises.
    """
    return _drain()


def register_usage_flusher() -> None:
    """Register the observer + start the background drain heartbeat (idempotent).

    The observer and the batched write are both no-ops unless Postgres is
    enabled, so registering unconditionally at startup is safe and cheap.
    Safe to call from app startup more than once.

    Postconditions: on first successful call, ``_usage_observer`` is registered
        and a ``BackgroundHeartbeat`` named ``llm-usage-flusher`` is running;
        subsequent calls are no-ops.
    """
    global _heartbeat, _registered
    with _register_lock:
        if _registered:
            return
        try:
            _register_call_observer(_usage_observer)
        except Exception:
            logger.warning("could not register LLM usage flusher observer", exc_info=True)
            return
        _heartbeat = BackgroundHeartbeat(
            _drain,
            max(_flush_interval_s(), 0.1),  # a 0 interval would busy-loop; floor at 0.1s
            name="llm-usage-flusher",
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
        _unregister_call_observer(_usage_observer)
    except Exception:
        logger.debug("could not unregister LLM usage flusher observer", exc_info=True)


def shutdown() -> None:
    """Lifecycle shutdown: stop enqueuing, then flush the remaining buffer.

    Order matters: :func:`unregister` first (stop the heartbeat + remove the
    observer so no new rows arrive), then :func:`drain` for the final synchronous
    flush. Called from Unified API lifespan before the shared Postgres pool closes,
    so the final drain can still use the pool.

    Postconditions: observer unregistered, heartbeat stopped, buffer drained
        (best-effort). Never raises.
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
    _heartbeat = BackgroundHeartbeat(_drain, 60.0, name="llm-usage-flusher-test")
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
    "register_usage_flusher",
    "unregister",
    "drain",
    "shutdown",
]

"""Durable per-call transcript recording for the code review pipeline.

Every LLM call the review pipeline makes (chunk review, false-positive
verification, the merged architecture/side-effect pass, narrative synthesis,
spec-compliance synthesis) can optionally append a ``(stage, target, prompt,
response)`` entry to that review's durable transcript, so a user can inspect
the reviewer's complete "thinking process" once a review job has finished
(see ``review_history_store.append_review_transcript_entries`` for the
storage layer, and the ``code_review_transcripts`` table it writes to).

Design: rather than threading a live recorder object through the coordinator's
deeply recursive, cached, and (for the Temporal path) cross-process call
chain, each call site records directly against whatever ``job_id`` is bound on
:func:`llm_service.current_attribution` for the current thread/task —
``CodeReviewAgent.run`` binds it once, via ``llm_attribution(job_id=...)``,
for the whole in-process review; ``shared.concurrency.parallel_map`` (the map
phase's, the bisection halves', and the tail passes' fan-out mechanism)
propagates that context into every worker thread by default
(``propagate_context=True``), so every actual LLM call made anywhere in the
run sees the same ``job_id`` without any function signature changing. A call
made with no attribution bound (``job_id == ""`` — a caller that never wired
one, e.g. most existing tests and non-job-tracked callers like
``acceptance_verifier_agent``) is a no-op: nothing to attribute the entry to,
and no ``code_review_runs`` row exists for it to attach to anyway. Scope:
``llm_attribution`` is a contextvar, so it does not cross the Temporal
activity boundary — only the in-process coordinator path records a
transcript today (the GitHub PR review flow, the sole caller with a
``code_review_runs``-backed UI, always runs in-process; see
``CodeReviewAgent.run``'s docstring).

Persistence is off the hot path, mirroring
``software_engineering_team.shared.trace_flusher``'s established pattern for
SE LLM-call telemetry: :func:`record_transcript_entry` does zero Postgres I/O
— it only builds the entry dict and appends it to a bounded in-memory buffer.
A :class:`~shared.concurrency.heartbeat.BackgroundHeartbeat` daemon thread
drains the buffer on an interval, batching every buffered entry for the same
job into one ``code_review_transcripts`` write. This is a dedicated buffer
rather than a ``llm_service.telemetry`` call-observer: telemetry's
``LLMCallRecord`` deliberately caps captured prompt/response text at a few
KB (``LLM_CAPTURE_PROMPTS``) since it is shared by every team's LLM calls, and
a transcript entry carries the full, unbounded prompt/response for one
code-review call — a different size class that does not belong in that
shared, cross-team ring buffer.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from llm_service import current_attribution
from shared.concurrency.heartbeat import BackgroundHeartbeat
from shared.postgres import is_postgres_enabled
from software_engineering_team.shared.env_config import env_float, env_int

logger = logging.getLogger(__name__)

# Bounded in-memory buffer of (job_id, entry) pairs awaiting a batched write;
# see the module docstring's "Persistence is off the hot path" section.
_buffer: "deque[tuple[str, dict[str, Any]]]" = deque()
_buffer_lock = threading.Lock()
_heartbeat: Optional[BackgroundHeartbeat] = None
_registered = False
_register_lock = threading.Lock()

# Throttle the overflow warning so a sustained burst does not flood the log —
# mirrors trace_flusher._overflow_warned.
_overflow_warned = False


def _max_buffer() -> int:
    """Max buffered entries before the oldest is dropped (env-configurable).

    Postconditions: returns a positive int (floor 1); garbage env -> default 2000.
    """
    return max(1, env_int("CODE_REVIEW_TRANSCRIPT_BUFFER_MAX", 2000, floor=1))


def _flush_interval_s() -> float:
    """Seconds between background drains (env-configurable).

    Postconditions: returns a non-negative float; garbage env -> default 2.0
    (mirrors ``SE_TRACE_FLUSH_INTERVAL_S``).
    """
    return env_float("CODE_REVIEW_TRANSCRIPT_FLUSH_INTERVAL_S", 2.0, 0.0)


def model_label(model: Any) -> str:
    """Best-effort human-readable identifier for a resolved model/client.

    Mirrors ``mapping._review_model_fingerprint``'s attribute-probing tail
    (duplicated rather than imported: that function also computes the map-phase
    cache fingerprint, a different concern this module has no business coupling
    to), but this copy is purely cosmetic — its output is never hashed into a
    cache key, so a heuristic mismatch is a display nit, not a correctness bug.

    Postconditions:
        - Returns the first non-empty ``model_id``/``model_name``/``model``
          string attribute found on ``model`` (or, for a ``dict``-shaped
          ``.config``, the same three keys within it), else the type name.
          Never raises.
    """
    for attr in ("model_id", "model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    config = getattr(model, "config", None)
    if isinstance(config, dict):
        for key in ("model_id", "model_name", "model"):
            candidate = config.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return type(model).__name__


def record_transcript_entry(
    stage: str,
    target: str,
    prompt: str,
    response: str,
    *,
    model: str = "",
    duration_ms: float = 0.0,
) -> None:
    """Buffer one completed LLM call for the current job's durable transcript.

    Preconditions:
        - ``stage`` is a short, stable identifier for the pipeline step that made
          this call (e.g. ``"chunk_review"``, ``"false_positive_filter"``,
          ``"architecture_side_effect"``, ``"synthesis"``, ``"spec_compliance"``).
        - ``target`` names what the call covered (a chunk's file label, a
          verification group's file path, or ``""`` for a once-per-submission
          pass); ``prompt``/``response`` are the full text sent to and received
          from the model — never truncated here.
        - ``duration_ms`` is the caller's own measured wall-clock time for the
          call (``0.0`` when not measured); used only to backdate this entry's
          ``started_at`` so entries the reader sorts by that field approximate
          real call order even though concurrent chunk reviews complete out of
          start order.

    Postconditions:
        - Does zero Postgres I/O — see the module docstring's "off the hot
          path" section. When no ``job_id`` is bound on the current
          attribution context (no ``llm_attribution(job_id=...)`` block is
          active — most tests and every caller that never wired one), or when
          Postgres is unavailable, this is a no-op: nothing to persist the
          entry against, or nowhere to persist it.
        - Otherwise appends the entry to the in-memory buffer for a later
          batched flush (see :func:`drain`); the buffer is bounded
          (``CODE_REVIEW_TRANSCRIPT_BUFFER_MAX``, default 2000) — past that,
          the oldest buffered entry is dropped so a stalled or disabled flush
          can never grow this process's memory without bound. Never raises.
    """
    job_id = current_attribution().job_id
    if not job_id or not is_postgres_enabled():
        return
    started_at = datetime.now(timezone.utc) - timedelta(milliseconds=max(duration_ms, 0.0))
    entry = {
        "stage": stage,
        "target": target,
        "model": model,
        "prompt": prompt,
        "response": response,
        "started_at": started_at.isoformat(),
        "duration_ms": int(duration_ms),
    }
    _enqueue(job_id, entry)


def _enqueue(job_id: str, entry: dict[str, Any]) -> None:
    """Append one ``(job_id, entry)`` pair to the buffer, dropping oldest on overflow."""
    global _overflow_warned
    cap = _max_buffer()
    with _buffer_lock:
        _buffer.append((job_id, entry))
        dropped = 0
        while len(_buffer) > cap:
            _buffer.popleft()
            dropped += 1
        overflowed = dropped > 0
        should_warn = overflowed and not _overflow_warned
        if should_warn:
            _overflow_warned = True
        elif not overflowed:
            _overflow_warned = False
    if should_warn:
        logger.warning(
            "code review transcript buffer full (cap=%d); dropping oldest %d entry(ies)",
            cap,
            dropped,
        )


def _drain() -> int:
    """Flush all buffered entries, batched per job_id; return how many were written.

    Failures are swallowed and logged (never raise) — a flush error must not
    kill the heartbeat thread. The batch is snapshotted under the lock and
    written outside it so a slow write does not block the call-path recorder.
    """
    with _buffer_lock:
        if not _buffer:
            return 0
        batch = list(_buffer)
        _buffer.clear()
    by_job: dict[str, list[dict[str, Any]]] = {}
    for job_id, entry in batch:
        by_job.setdefault(job_id, []).append(entry)
    try:
        from software_engineering_team.review_history_store import (
            append_review_transcript_entries,
        )

        for job_id, entries in by_job.items():
            append_review_transcript_entries(job_id, entries)
    except Exception:  # noqa: BLE001 - the flusher must never die on a write failure
        logger.warning(
            "CodeReview transcript: failed to flush %d entry(ies)", len(batch), exc_info=True
        )
        return 0
    return len(batch)


def drain() -> int:
    """Synchronous one-shot drain of the buffer (used by shutdown and tests).

    Postconditions: returns the number of entries written; 0 if the buffer
    was empty or the write failed. Never raises.
    """
    return _drain()


def register_transcript_flusher() -> None:
    """Start the background drain heartbeat (idempotent).

    Safe to call from app startup more than once; ``record_transcript_entry``
    already works (buffering only) before this is called, so a missed or
    delayed registration degrades to "entries buffer until the next
    registration/drain," never to data loss beyond the buffer's bound.
    """
    global _heartbeat, _registered
    with _register_lock:
        if _registered:
            return
        _heartbeat = BackgroundHeartbeat(
            _drain,
            max(_flush_interval_s(), 0.1),  # a 0 interval would busy-loop; floor at 0.1s
            name="code-review-transcript-flusher",
        )
        _heartbeat.start()
        _registered = True


def unregister() -> None:
    """Stop the heartbeat.

    Postconditions: the drain thread is stopped (joined). Safe to call when
    never registered (no-op). Does NOT flush — call :func:`drain` afterward
    for the final batch, or :func:`shutdown` for both in the right order.
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
    """Lifecycle shutdown: stop the heartbeat, then flush the remaining buffer.

    Order matters: :func:`unregister` first (stop the heartbeat so no drain
    races the final one), then :func:`drain` for the final synchronous flush.
    Called before the shared Postgres pool closes, mirroring
    ``trace_flusher.shutdown``.
    """
    unregister()
    drain()


# ---------------------------------------------------------------------------
# Test-only accessors (no production callers)
# ---------------------------------------------------------------------------


def _buffer_size() -> int:
    with _buffer_lock:
        return len(_buffer)


def _reset_for_test() -> None:
    """Clear all module state between tests (buffer, registration, heartbeat)."""
    global _heartbeat, _registered, _overflow_warned
    hb = _heartbeat
    _heartbeat = None
    if hb is not None:
        try:
            hb.stop()
        except Exception:  # pragma: no cover - BackgroundHeartbeat.stop never raises
            pass
    with _buffer_lock:
        _buffer.clear()
    _registered = False
    _overflow_warned = False


__all__ = [
    "model_label",
    "record_transcript_entry",
    "drain",
    "register_transcript_flusher",
    "unregister",
    "shutdown",
]

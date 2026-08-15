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

import json
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from llm_service import current_attribution
from llm_service.interface import observer_turn_started_monotonic
from shared.concurrency.heartbeat import BackgroundHeartbeat
from shared.postgres import is_postgres_enabled
from software_engineering_team.shared.env_config import env_float, env_int

logger = logging.getLogger(__name__)

# Bounded in-memory buffer of (job_id, entry) pairs awaiting a batched write;
# see the module docstring's "Persistence is off the hot path" section.
_buffer: "deque[tuple[str, dict[str, Any]]]" = deque()
_buffer_lock = threading.Lock()
# Serializes the entire drain (snapshot + persist + requeue). ``_buffer_lock``
# only covers enqueue/snapshot so a slow Postgres write does not block
# ``record_transcript_entry``. Without this, a heartbeat can snapshot+clear
# the buffer and then a terminal ``drain()`` (``CodeReviewAgent.run``'s
# ``finally``) sees an empty buffer and returns while that write is still in
# flight — the UI's one-shot fetch can miss the batch. Lock order is always
# ``_drain_exec_lock`` then ``_buffer_lock``; never the reverse.
_drain_exec_lock = threading.Lock()
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


def _should_prefix_system_prompt(prompt: str, system_prompt: str) -> bool:
    """True when ``system_prompt`` should be prepended onto ``prompt``.

    Preconditions:
        ``prompt`` is the user/messages text for this turn. ``system_prompt``
        may be empty.
    Postconditions:
        Returns False when ``system_prompt`` is blank, when ``prompt`` already
        starts with a ``[system]`` section, or when ``prompt`` is a JSON list
        of chat messages that already includes a ``role=system`` item.
        Otherwise True.
    """
    if not system_prompt:
        return False
    stripped = prompt.lstrip()
    if stripped.startswith("[system]"):
        return False
    try:
        data = json.loads(prompt)
    except (json.JSONDecodeError, TypeError, ValueError):
        return True
    if isinstance(data, list):
        return not any(isinstance(item, dict) and item.get("role") == "system" for item in data)
    return True


def reasoning_turns_from_agent_messages(
    agent: object,
    fallback_prompt: str,
    started: float,
) -> list[tuple[str, str, float]]:
    """Split a Strands conversation into one transcript turn per assistant message.

    Preconditions:
        ``agent.messages`` is the completed reasoning conversation (may be
        empty or malformed). ``started`` is monotonic time from before the
        Agent run.
    Postconditions:
        Returns ``(prompt, response, started)`` triples in conversation order:
        each assistant message is a response whose prompt is the JSON of prior
        messages (or ``fallback_prompt`` when the prefix is empty). Returns
        an empty list when there are no assistant messages.
    """
    try:
        messages = list(getattr(agent, "messages", []) or [])
    except Exception:  # noqa: BLE001 - transcript fallback must never break the caller
        return []
    turns: list[tuple[str, str, float]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        prefix = messages[:index]
        try:
            prompt = json.dumps(prefix, default=str) if prefix else fallback_prompt
            response = json.dumps(message, default=str)
        except Exception:  # noqa: BLE001
            continue
        turns.append((prompt, response, started))
    return turns


def record_reasoning_transcript_turns(
    stage: str,
    target: str,
    *,
    turns: list[tuple[str, str, float]],
    agent: object | None,
    fallback_prompt: str,
    started: float,
    reasoning_done_at: float,
    system_prompt: str,
    model: str,
    recorder: Callable[..., None] | None = None,
) -> None:
    """Buffer one transcript entry per reasoning-pass model invocation.

    Preconditions:
        ``turns`` is the inner HTTP turns drained after the Agent run, or
        empty when the provider recorded none. ``agent`` may be None when
        reasoning never constructed an Agent.
    Postconditions:
        Records ``turns`` when non-empty; otherwise splits ``agent.messages``
        into per-assistant-message entries; otherwise dumps the full
        conversation as one entry when the agent exists. ``recorder``, when
        given, is used instead of :func:`record_transcript_entry` so callers
        that tests monkeypatch keep intercepting writes. Never raises.
    """
    write = recorder if recorder is not None else record_transcript_entry
    if not turns and agent is not None:
        turns = reasoning_turns_from_agent_messages(agent, fallback_prompt, started)
    if turns:
        for index, (turn_prompt, turn_response, turn_started) in enumerate(turns):
            ended = turns[index + 1][2] if index + 1 < len(turns) else reasoning_done_at
            write(
                stage,
                target,
                turn_prompt,
                turn_response,
                system_prompt=system_prompt,
                model=model,
                duration_ms=max(0.0, (ended - turn_started) * 1000),
                started_monotonic=turn_started,
            )
        return
    if agent is None:
        return
    try:
        transcript_response = json.dumps(getattr(agent, "messages", []), default=str)
    except Exception:  # noqa: BLE001 - transcript recording must never break the caller
        transcript_response = ""
    write(
        stage,
        target,
        fallback_prompt,
        transcript_response,
        system_prompt=system_prompt,
        model=model,
        duration_ms=(reasoning_done_at - started) * 1000,
        started_monotonic=started,
    )


def sequential_turn_durations_ms(started_times: list[float], last_ended: float) -> list[float]:
    """Wall-clock duration of each turn, ending at the next start.

    Preconditions:
        ``started_times`` is monotonic start times in call order. ``last_ended``
        is when the last turn finished.
    Postconditions:
        Returns one non-negative duration in milliseconds per start. Turn
        ``i`` ends at ``started_times[i + 1]`` except the last, which ends at
        ``last_ended``.
    """
    durations: list[float] = []
    for index, started in enumerate(started_times):
        ended = started_times[index + 1] if index + 1 < len(started_times) else last_ended
        durations.append(max(0.0, (ended - started) * 1000))
    return durations


def resolve_format_turn_started(
    existing_starts: list[float],
    format_pass_started: float | None,
    now: float,
) -> float:
    """Start time for one formatting observer callback.

    Preconditions:
        ``existing_starts`` are prior formatting turns in this pass. ``now``
        is ``time.monotonic()`` at the callback.
    Postconditions:
        Prefers the provider-stamped observer start. Otherwise the first
        turn uses ``format_pass_started`` (request start) and later turns
        use ``now`` so chained durations do not share one stamp.
    """
    observer = observer_turn_started_monotonic()
    if observer is not None:
        return observer
    if not existing_starts and format_pass_started is not None:
        return format_pass_started
    return now


def record_formatting_transcript_turns(
    stage: str,
    target: str,
    *,
    turns: list[tuple[str, str, float]],
    last_ended: float,
    system_prompt: str,
    model: str,
    recorder: Callable[..., None] | None = None,
) -> None:
    """Buffer one transcript entry per formatting LLM turn.

    Preconditions:
        ``turns`` is ``(prompt, response, started)`` in callback order.
        ``last_ended`` is monotonic time after the last formatting call.
    Postconditions:
        Each turn's ``duration_ms`` ends at the next turn's start, except
        the last which ends at ``last_ended``. No-op when ``turns`` is empty.
        Never raises.
    """
    if not turns:
        return
    write = recorder if recorder is not None else record_transcript_entry
    durations = sequential_turn_durations_ms([started for _, _, started in turns], last_ended)
    for (prompt, response, started), duration_ms in zip(turns, durations, strict=True):
        write(
            stage,
            target,
            prompt,
            response,
            system_prompt=system_prompt,
            model=model,
            duration_ms=duration_ms,
            started_monotonic=started,
        )


def record_transcript_entry(
    stage: str,
    target: str,
    prompt: str,
    response: str,
    *,
    system_prompt: str = "",
    model: str = "",
    duration_ms: float = 0.0,
    started_monotonic: float | None = None,
) -> None:
    """Buffer one completed LLM call for the current job's durable transcript.

    Preconditions:
        - ``stage`` is a short, stable identifier for the pipeline step that made
          this call (e.g. ``"chunk_review"``, ``"false_positive_filter"``,
          ``"architecture_side_effect"``, ``"synthesis"``, ``"spec_compliance"``).
        - ``target`` names what the call covered (a chunk's file label, a
          verification group's file path, or ``""`` for a once-per-submission
          pass); ``prompt``/``response`` are the full text sent to and received
          from the model — never truncated here. ``system_prompt``, when
          non-blank, is the system prompt the caller sent alongside ``prompt``
          — omitting it would leave the recorded entry missing the instruction
          layer that actually governed the model's behavior for that call.
        - ``duration_ms`` is the caller's own measured wall-clock time for the
          call (``0.0`` when not measured). ``started_monotonic``, when given,
          is ``time.monotonic()`` from when that LLM request began and is the
          source of ``started_at`` (converted to wall clock at record time).
          When omitted, ``started_at`` is backdated from ``duration_ms``
          alone, which is accurate only if this entry is buffered immediately
          when the call ends.

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
          can never grow this process's memory without bound. The recorded
          ``prompt`` field is ``prompt`` prefixed with a ``[system]``/``[user]``
          section split when ``system_prompt`` is non-blank *and* ``prompt``
          does not already embed that system message (a serialized
          ``messages`` JSON array from an Ollama continuation already includes
          ``role: system``; prepending again would duplicate it). Else
          ``prompt`` is stored unchanged. Each entry carries a unique
          ``entry_id`` so a retried flush can be appended idempotently.
          Never raises: any failure in buffering is logged and swallowed so
          transcript recording cannot break a review.
    """
    try:
        job_id = current_attribution().job_id
        if not job_id or not is_postgres_enabled():
            return
        full_prompt = (
            f"[system]\n{system_prompt}\n\n[user]\n{prompt}"
            if _should_prefix_system_prompt(prompt, system_prompt)
            else prompt
        )
        if started_monotonic is not None:
            elapsed_ms = max(0.0, (time.monotonic() - started_monotonic) * 1000)
            started_at = datetime.now(timezone.utc) - timedelta(milliseconds=elapsed_ms)
        else:
            started_at = datetime.now(timezone.utc) - timedelta(milliseconds=max(duration_ms, 0.0))
        entry = {
            "entry_id": str(uuid.uuid4()),
            "stage": stage,
            "target": target,
            "model": model,
            "prompt": full_prompt,
            "response": response,
            "started_at": started_at.isoformat(),
            "duration_ms": int(duration_ms),
        }
        _enqueue(job_id, entry)
    except Exception:  # noqa: BLE001 - transcript recording must never break the caller
        logger.warning("code review transcript: failed to buffer an entry", exc_info=True)


def _note_overflow(dropped: int) -> bool:
    """Update the overflow-warning throttle. Caller must hold ``_buffer_lock``.

    Preconditions:
        - ``dropped`` is the number of entries just evicted (>= 0).
        - ``_buffer_lock`` is held so ``_overflow_warned`` is not raced.
          Raises ``RuntimeError`` when it is not (not an ``assert``, so
          ``python -O`` cannot strip the check).
    Postconditions:
        - Returns True iff the caller should emit one overflow warning.
          A burst of overflows logs once; a later non-overflowing enqueue
          resets the throttle so the next burst can warn again.
    """
    if not _buffer_lock.locked():
        raise RuntimeError("_note_overflow requires _buffer_lock")
    global _overflow_warned
    overflowed = dropped > 0
    should_warn = overflowed and not _overflow_warned
    if should_warn:
        _overflow_warned = True
    elif not overflowed:
        _overflow_warned = False
    return should_warn


def _enqueue(job_id: str, entry: dict[str, Any]) -> None:
    """Append one ``(job_id, entry)`` pair to the buffer, dropping oldest on overflow."""
    cap = _max_buffer()
    with _buffer_lock:
        _buffer.append((job_id, entry))
        dropped = 0
        while len(_buffer) > cap:
            _buffer.popleft()
            dropped += 1
        should_warn = _note_overflow(dropped)
    if should_warn:
        logger.warning(
            "code review transcript buffer full (cap=%d); dropping oldest %d entry(ies)",
            cap,
            dropped,
        )


def _drain() -> int:
    """Flush all buffered entries, batched per job_id; return how many were written.

    A per-job write failure is requeued (see :func:`_requeue`) rather than
    discarded — a transient Postgres blip must not permanently drop those
    calls from the user-facing transcript — and never kills the heartbeat
    thread. The batch is snapshotted under ``_buffer_lock`` and written
    outside that lock so a slow write does not block the call-path recorder.
    The whole drain (snapshot through persist/requeue) is serialized on
    ``_drain_exec_lock`` so a terminal ``drain()`` cannot return while a
    heartbeat write of a just-cleared batch is still in flight.

    The store import runs *before* the buffer is cleared so an import failure
    cannot drop in-flight entries. Never raises.
    """
    try:
        from software_engineering_team.review_history_store import (
            append_review_transcript_entries,
        )
    except Exception:  # noqa: BLE001 - drain must never raise
        logger.warning("code review transcript: drain import failed", exc_info=True)
        return 0

    with _drain_exec_lock:
        with _buffer_lock:
            if not _buffer:
                return 0
            batch = list(_buffer)
            _buffer.clear()
        by_job: dict[str, list[dict[str, Any]]] = {}
        for job_id, entry in batch:
            by_job.setdefault(job_id, []).append(entry)

        written = 0
        failed: list[tuple[str, dict[str, Any]]] = []
        for job_id, entries in by_job.items():
            try:
                ok = append_review_transcript_entries(job_id, entries)
            except Exception:  # noqa: BLE001 - the flusher must never die on a write failure
                logger.warning(
                    "CodeReview transcript: failed to flush %d entry(ies) for job %s",
                    len(entries),
                    job_id,
                    exc_info=True,
                )
                failed.extend((job_id, entry) for entry in entries)
                continue
            if ok:
                written += len(entries)
            else:
                logger.warning(
                    "CodeReview transcript: failed to flush %d entry(ies) for job %s",
                    len(entries),
                    job_id,
                )
                failed.extend((job_id, entry) for entry in entries)

        if failed:
            _requeue(failed)
        return written


def _requeue(entries: list[tuple[str, dict[str, Any]]]) -> None:
    """Push entries that failed to flush back onto the buffer for a later retry.

    Applies the same bounded-buffer overflow policy as :func:`_enqueue`: if
    requeuing pushes the buffer past its cap, the oldest entries (which may
    include some just requeued) are dropped rather than growing unboundedly —
    a sustained outage degrades to "drop the oldest," never unbounded memory
    growth, matching :func:`_enqueue`'s contract.
    """
    cap = _max_buffer()
    with _buffer_lock:
        # extendleft reverses input order one item at a time, so reverse first
        # to land the requeued entries back at the front in their original order.
        _buffer.extendleft(reversed(entries))
        dropped = 0
        while len(_buffer) > cap:
            _buffer.popleft()
            dropped += 1
        should_warn = _note_overflow(dropped)
    if should_warn:
        logger.warning(
            "code review transcript buffer full (cap=%d) after requeuing failed writes; "
            "dropping oldest %d entry(ies)",
            cap,
            dropped,
        )


def drain() -> int:
    """Synchronous one-shot drain of the buffer (used by shutdown and tests).

    Postconditions: returns the number of entries written; 0 if the buffer
    was empty or the write failed. Overlapping callers serialize: this
    does not return while another drain's persist of a snapshotted batch
    is still in flight. Never raises: unexpected failures are logged and
    reported as zero writes so a ``finally`` flush cannot mask a review error.
    """
    try:
        return _drain()
    except Exception:  # noqa: BLE001 - public drain contract is never-raises
        logger.warning("code review transcript: drain failed", exc_info=True)
        return 0


def unflushed_entries(job_id: str) -> list[dict[str, Any]]:
    """Return buffered, not-yet-durable transcript entries for ``job_id``.

    The terminal transcript GET is a one-shot read. If the run's final
    ``drain()`` requeued because Postgres blipped, those entries still sit in
    this process's buffer while the heartbeat retries. Serving them here means
    the dialog is not empty during that window, without delaying the review's
    terminal status on persistence.

    Preconditions:
        ``job_id`` is a review job id (empty is allowed and returns []).

    Postconditions:
        Returns a new list of entry dicts currently buffered for ``job_id``,
        in buffer order. Waits for an in-flight drain so the snapshot-cleared
        window is not observed. Never raises.
    """
    if not job_id:
        return []
    try:
        with _drain_exec_lock:
            return _snapshot_unflushed(job_id)
    except Exception:  # noqa: BLE001 - a GET must not fail because the buffer is busy
        logger.warning("code review transcript: unflushed_entries failed", exc_info=True)
        return []


def _snapshot_unflushed(job_id: str) -> list[dict[str, Any]]:
    """Copy buffered entries for ``job_id``. Caller holds ``_drain_exec_lock``."""
    with _buffer_lock:
        return [entry for jid, entry in _buffer if jid == job_id]


def merge_unflushed(
    job_id: str,
    durable: list[dict[str, Any]] | Callable[[], list[dict[str, Any]] | None],
) -> list[dict[str, Any]]:
    """Append this job's buffered entries onto a durable transcript list.

    Preconditions:
        ``durable`` is the list returned by ``get_review_transcript`` (or []),
        or a zero-arg callable that loads that list. A callable is invoked
        while holding ``_drain_exec_lock`` after the buffer snapshot so an
        in-flight persist has committed before the durable query runs, and a
        drain cannot clear the buffer between the two reads.

    Postconditions:
        Returns the durable list unchanged when the buffer has nothing for
        ``job_id``. Otherwise returns a new list of durable + buffered
        entries that are not already in the durable list (matched by
        ``entry_id``), sorted by ``started_at`` (same order as the durable
        GET). Never raises.
    """
    extra: list[dict[str, Any]] = []
    loaded: list[dict[str, Any]] = []
    try:
        with _drain_exec_lock:
            extra = _snapshot_unflushed(job_id) if job_id else []
            loaded = durable() if callable(durable) else durable
            loaded = loaded or []
    except Exception:  # noqa: BLE001 - a GET must not fail because the buffer is busy
        logger.warning("code review transcript: merge_unflushed failed", exc_info=True)
        try:
            loaded = durable() if callable(durable) else list(durable or [])
            loaded = loaded or []
        except Exception:  # noqa: BLE001
            loaded = []
    if not extra:
        return loaded
    try:
        from software_engineering_team.review_history_store import (
            unpersisted_transcript_entries,
        )

        extra = unpersisted_transcript_entries(loaded, extra)
    except Exception:  # noqa: BLE001 - merge must still return durable + extra
        logger.warning("code review transcript: merge_unflushed dedupe failed", exc_info=True)
    if not extra:
        return loaded
    return sorted(list(loaded) + extra, key=lambda e: e.get("started_at") or "")


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
            max(_flush_interval_s(), 0.1),  # a zero interval would busy-loop; floor at 0.1s
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
    with _drain_exec_lock:
        with _buffer_lock:
            _buffer.clear()
    _registered = False
    _overflow_warned = False


__all__ = [
    "model_label",
    "record_transcript_entry",
    "record_reasoning_transcript_turns",
    "record_formatting_transcript_turns",
    "reasoning_turns_from_agent_messages",
    "resolve_format_turn_started",
    "sequential_turn_durations_ms",
    "drain",
    "unflushed_entries",
    "merge_unflushed",
    "register_transcript_flusher",
    "unregister",
    "shutdown",
]

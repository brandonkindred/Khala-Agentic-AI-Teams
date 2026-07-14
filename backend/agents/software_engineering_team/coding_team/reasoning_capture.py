"""Streamed-reasoning ("thinking") capture for the coding-team swarm.

Extracted from ``coding_team/orchestrator.py`` (issue: decompose the orchestrator
god-file into named collaborators) — pure structural move, no behavior change.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# How often the orchestrator flushes captured agent "thinking" (reasoning tokens)
# to the job record so the UI poll can surface it. Defaults to 2s; garbage/non-positive
# falls back to the default.
_ENV_THINKING_FLUSH_INTERVAL_S = "AGENT_THINKING_FLUSH_INTERVAL_S"
_DEFAULT_THINKING_FLUSH_INTERVAL_S = 2.0
# Keep only the most recent tail of reasoning so the field (and DB write) stays bounded.
_THINKING_MAX_CHARS = 8000


def _thinking_flush_interval_s() -> float:
    """Resolve the thinking-flush interval (seconds) from env, defensively.

    Preconditions: none.
    Postconditions: returns a finite, positive float; missing/garbage/non-positive/
        non-finite (``inf``/``nan``) yields ``_DEFAULT_THINKING_FLUSH_INTERVAL_S``.
        Never raises. A non-finite interval would make the heartbeat's
        ``Event.wait(interval)`` block forever, defeating periodic flushing.
    """
    raw = os.environ.get(_ENV_THINKING_FLUSH_INTERVAL_S)
    if not raw:
        return _DEFAULT_THINKING_FLUSH_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_THINKING_FLUSH_INTERVAL_S
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_THINKING_FLUSH_INTERVAL_S
    return value


class _ThinkingBuffer:
    """Thread-safe, capped accumulator for streamed reasoning ("thinking") tokens.

    The LLM client's ``on_reasoning`` hook (called from a streaming worker thread)
    appends tokens; a heartbeat thread periodically reads ``pending`` and, only
    after a SUCCESSFUL write, calls ``commit`` to mark that tail flushed. Splitting
    read from commit means a failed write is NOT recorded as flushed, so the next
    beat retries it rather than silently dropping it. Only the most recent
    ``_THINKING_MAX_CHARS`` characters are retained so memory and the persisted
    field stay bounded.

    Invariants: all access is guarded by an internal lock; ``pending`` returns the
    current tail only while it differs from the last committed tail.
    """

    def __init__(self, max_chars: int = _THINKING_MAX_CHARS) -> None:
        self._lock = threading.Lock()
        # Floor to >=1: a non-positive cap would make the tail slice ``text[-0:]``
        # return the WHOLE string, defeating the bound and growing unbounded.
        self._max_chars = max(1, max_chars)
        self._text = ""
        self._flushed = ""

    def append(self, token: str) -> None:
        """Append a reasoning delta. Amortized O(len(token)): the buffer is only
        re-sliced to the cap once it grows past ``2 * max_chars``, so a long stream
        does not pay an O(max_chars) copy on every token."""
        with self._lock:
            self._text += token
            if len(self._text) > self._max_chars * 2:
                self._text = self._text[-self._max_chars :]

    def pending(self) -> Optional[str]:
        """Return the current (capped) tail if it differs from the last committed
        flush, else ``None``. Does NOT mark it flushed — call ``commit`` after a
        successful write."""
        with self._lock:
            tail = self._text[-self._max_chars :]
            return tail if tail != self._flushed else None

    def commit(self, text: str) -> None:
        """Record ``text`` as the last successfully-flushed tail."""
        with self._lock:
            self._flushed = text


def _flush_thinking(buffer: _ThinkingBuffer, update_fn: Callable[..., None]) -> None:
    """Write the buffer's latest thinking tail to the job record, if it changed.

    Preconditions: ``update_fn`` accepts a ``thinking=`` keyword.
    Postconditions: calls ``update_fn(thinking=<tail>)`` exactly when the tail
        changed since the last *successful* flush AND is non-blank, and only marks
        the tail flushed (``commit``) when that write did not raise — so a failed
        write is retried on the next beat instead of being silently dropped. A write
        failure is swallowed (surfacing thinking must never break the job); never
        raises. Blank tails are skipped so the first ``beat_first`` tick on an empty
        buffer — and every tick on a path where no reasoning is captured — does not
        write an empty ``thinking`` field (which the UI would render as a blank panel).
    """
    text = buffer.pending()
    if not text or not text.strip():
        return
    try:
        update_fn(thinking=text)
    except Exception:  # noqa: BLE001 — surfacing thinking must never break the job
        logger.debug("failed to flush thinking to job record", exc_info=True)
        return  # do NOT commit — retry this tail on the next beat
    buffer.commit(text)


def _make_reasoning_llm_getter(record_reasoning: Callable[[str], None]) -> Callable[[str], Any]:
    """Build the default per-job model getter that streams reasoning into ``record_reasoning``.

    Each role gets a hook-bearing client that is UNCACHED in the global factory cache
    (so the per-job callback never leaks into the shared singleton), but memoized
    PER JOB by key: repeated ``getter(key)`` calls (e.g. one per task revision through
    the quality gates) reuse the same client, so its model-context (``/api/show``)
    lookup happens once per role rather than on every call.

    Preconditions: ``record_reasoning`` is callable.
    Postconditions: returns a getter ``key -> strands model`` whose underlying client
        invokes ``record_reasoning`` for each reasoning delta; the same ``key`` yields
        the same model (and underlying client) for the life of this getter — so both
        the ``/api/show`` context lookup and the wrapper allocation happen once per
        role. Today it is only called from the swarm thread, but the memo is
        lock-guarded so the one-build-per-key invariant holds even if a future caller
        invokes it concurrently.
    """
    models: Dict[str, Any] = {}
    lock = threading.Lock()

    def _getter(key: str) -> Any:
        resolved_key = key or "coding_team"
        with lock:
            model = models.get(resolved_key)
            if model is None:
                factory = __import__("llm_service.factory", fromlist=["get_client"])
                sp = __import__("llm_service.strands_provider", fromlist=["get_strands_model"])
                client = factory.get_client(resolved_key, on_reasoning=record_reasoning)
                model = sp.get_strands_model(resolved_key, client=client)
                models[resolved_key] = model
            return model

    return _getter

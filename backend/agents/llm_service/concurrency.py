"""Process-global LLM concurrency gate shared by every provider client.

A single ``BoundedSemaphore`` caps how many LLM network calls run at once
across the whole process, regardless of which provider the failover list
resolves to. Both the Ollama and the Claude clients acquire this one gate
around their network call, so the cap is truly global: a review that fans out
many concurrent chunk/verification calls can never exceed ``LLM_MAX_CONCURRENCY``
in-flight requests and trip a provider's concurrent-request rate limit.

This is a leaf module: it imports only stdlib + ``llm_service.config`` (itself
stdlib-only), so it cannot create an import cycle with either client. The gate
must be acquired around the network call ONLY — never around the multi-minute
429 backoff sleep (``backoff.py``), which runs after the gate is released, so a
waiting call never holds a slot.

Invariants:
    - Exactly one logical LLM request acquires the gate exactly once, inside
      exactly one leaf client; acquisitions are never nested, so the bound can
      never deadlock itself.
"""

from __future__ import annotations

import os
import threading

from . import config as llm_config

_DEFAULT_MAX_CONCURRENCY = 4

_llm_semaphore: "threading.BoundedSemaphore | None" = None
_semaphore_lock = threading.Lock()


def _resolve_limit() -> int:
    """Resolve the concurrency limit from ``LLM_MAX_CONCURRENCY``.

    Postconditions:
        - Returns ``max(1, int(LLM_MAX_CONCURRENCY))``; a missing, empty, or
          non-integer value falls back to ``_DEFAULT_MAX_CONCURRENCY`` (4), and a
          zero/negative value is floored to 1 (a gate of 0 would deadlock every
          call). Never raises.
    """
    raw = os.environ.get(llm_config.ENV_LLM_MAX_CONCURRENCY) or str(_DEFAULT_MAX_CONCURRENCY)
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_MAX_CONCURRENCY


def get_llm_semaphore() -> threading.BoundedSemaphore:
    """Return the process-global LLM concurrency semaphore, creating it once.

    Postconditions:
        - Returns the same ``BoundedSemaphore`` on every call for the life of the
          process (the limit is read once, at first use, exactly like the prior
          Ollama-only gate). Thread-safe (double-checked under a module lock).
    """
    global _llm_semaphore
    if _llm_semaphore is None:
        with _semaphore_lock:
            if _llm_semaphore is None:
                _llm_semaphore = threading.BoundedSemaphore(_resolve_limit())
    return _llm_semaphore


def reset_llm_semaphore() -> None:
    """Drop the cached semaphore so the next ``get_llm_semaphore`` re-reads the limit.

    The limit is frozen at first use by design, so this exists only for tests
    that set ``LLM_MAX_CONCURRENCY`` and need a fresh gate.

    Postconditions:
        - The next ``get_llm_semaphore()`` builds a new semaphore sized from the
          current environment. Thread-safe (holds the module lock).
    """
    global _llm_semaphore
    with _semaphore_lock:
        _llm_semaphore = None

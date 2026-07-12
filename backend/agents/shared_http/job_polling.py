"""Shared HTTP job-submit/poll helpers, built on shared_http.get_pooled_client().

Several teams' adapters (planning_team's product_analysis, ai_systems, and
market_research modules) each independently reimplemented the same shape: a
POST-and-get-job_id function, a GET-status function, and a hand-rolled polling
loop — each opening a fresh ``httpx.Client`` per call instead of reusing the
process-wide pooled client already used by ``job_service_client.py``. This
module is the single home for those primitives.

Invariants:
    - post_json/get_json never raise on transport/HTTP/parse failure; they log
      a WARNING tagged with ``log_context`` and return None. Callers decide
      what "could not complete request" means for their operation.
    - poll_until_terminal never busy-waits past ``total_timeout`` and never
      raises; a ``status_fn()`` failure (returns None) or a timeout both yield
      a dict shaped like a terminal-failure status (``{status_key: "failed",
      "error": ...}``), the same shape a real terminal status would have.
    - Both functions delegate all connection reuse to
      shared_http.get_pooled_client(); neither opens/closes an httpx.Client.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, FrozenSet, Optional

import httpx

from shared_http import get_pooled_client

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_TERMINAL_STATUSES", "post_json", "get_json", "poll_until_terminal"]

DEFAULT_TERMINAL_STATUSES: FrozenSet[str] = frozenset({"completed", "failed", "cancelled"})

# JSONDecodeError is a ValueError subclass, so this tuple also covers a
# response body that isn't valid JSON.
_REQUEST_ERRORS = (httpx.HTTPError, ValueError)


def post_json(
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 30.0,
    log_context: str = "request",
) -> Optional[Dict[str, Any]]:
    """POST a JSON payload and return the parsed JSON response body.

    Preconditions:
        - ``url`` is a non-empty absolute URL.
        - ``timeout`` is a positive, finite number of seconds (enforced by
          ``get_pooled_client``).
    Postconditions:
        - On a 2xx response with a JSON body, returns the parsed value.
        - On any httpx transport error, non-2xx status, or JSON parse
          failure, logs a WARNING prefixed with ``log_context`` and returns
          None. Never raises.
        - Reuses the process-wide pooled client; never opens/closes a client.
    """
    assert url, "url must be non-empty"
    try:
        client = get_pooled_client(timeout)
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
    except _REQUEST_ERRORS as e:
        logger.warning("%s failed: %s", log_context, e)
        return None


def get_json(
    url: str,
    *,
    timeout: float = 30.0,
    log_context: str = "request",
) -> Optional[Dict[str, Any]]:
    """GET a resource and return the parsed JSON response body.

    Same contract as :func:`post_json` (no request payload).
    """
    assert url, "url must be non-empty"
    try:
        client = get_pooled_client(timeout)
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()
    except _REQUEST_ERRORS as e:
        logger.warning("%s failed: %s", log_context, e)
        return None


def poll_until_terminal(
    status_fn: Callable[[], Optional[Dict[str, Any]]],
    *,
    terminal_statuses: FrozenSet[str] = DEFAULT_TERMINAL_STATUSES,
    status_key: str = "status",
    poll_interval: float = 5.0,
    total_timeout: float = 3600.0,
    on_poll: Optional[Callable[[Dict[str, Any]], None]] = None,
    log_context: str = "job",
) -> Dict[str, Any]:
    """Poll ``status_fn()`` until it reports a terminal status or time runs out.

    Preconditions:
        - ``status_fn`` takes no arguments and returns either a status dict
          (expected to carry ``status_key``) or None if status could not be
          retrieved.
        - ``poll_interval`` and ``total_timeout`` are positive, finite
          seconds.
        - ``on_poll``, if given, is called with each *non-terminal* status
          dict, once per poll, strictly before that iteration's sleep.
    Postconditions:
        - Returns the first status dict for which
          ``status.get(status_key)`` is in ``terminal_statuses``, unmodified.
        - If ``status_fn()`` returns None on any call, immediately returns
          ``{status_key: "failed", "error": "Failed to get status"}`` — no
          further polling, no sleep.
        - If no terminal status is observed within ``total_timeout`` seconds,
          returns ``{status_key: "failed", "error": f"Timed out waiting for
          {log_context}"}``.
        - Never raises; never sleeps past a terminal/None short-circuit.
    """
    assert poll_interval > 0, f"poll_interval must be positive, got {poll_interval!r}"
    assert total_timeout > 0, f"total_timeout must be positive, got {total_timeout!r}"
    start = time.monotonic()
    while (time.monotonic() - start) < total_timeout:
        status = status_fn()
        if status is None:
            return {status_key: "failed", "error": "Failed to get status"}
        if status.get(status_key) in terminal_statuses:
            return status
        if on_poll is not None:
            on_poll(status)
        time.sleep(poll_interval)
    return {status_key: "failed", "error": f"Timed out waiting for {log_context}"}

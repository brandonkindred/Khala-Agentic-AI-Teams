"""Shared HTTP job-submit/poll helpers, built on shared.http.get_pooled_client().

Several teams' adapters (planning_team's product_analysis, ai_systems, and
market_research modules) each independently reimplemented the same shape: a
POST-and-get-job_id function, a GET-status function, and a hand-rolled polling
loop — each opening a fresh ``httpx.Client`` per call instead of reusing the
process-wide pooled client already used by ``job_service_client.py``. This
module is the single home for those primitives.

Invariants:
    - post_json/get_json/async_post_json/async_get_json enforce their
      preconditions via ``assert`` (raising AssertionError on violation — a
      caller bug, per this repo's Design-by-Contract convention). Once
      preconditions hold, they never raise on transport/HTTP/parse failure; they
      log a WARNING tagged with ``log_context`` and return None instead.
      Callers decide what "could not complete request" means for their
      operation.
    - poll_until_terminal never busy-waits past ``total_timeout`` and never
      raises; a ``status_fn()`` failure (returns None or raises) or a timeout
      both yield a dict shaped like a terminal-failure status (``{status_key:
      "failed", "error": ...}``), the same shape a real terminal status would
      have.
    - Sync helpers delegate connection reuse to
      shared.http.get_pooled_client(); async helpers delegate to
      shared.http.get_pooled_async_client(). Neither opens/closes an httpx
      client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Optional

import httpx

from shared.http import get_pooled_async_client, get_pooled_client

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TERMINAL_STATUSES",
    "post_json",
    "get_json",
    "poll_until_terminal",
    "async_post_json",
    "async_get_json",
    "async_poll_until_terminal",
]

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
          None. Never raises for these failure modes (a precondition
          violation, per the ``Preconditions`` above, still raises
          AssertionError).
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


async def async_post_json(
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 30.0,
    log_context: str = "request",
) -> Optional[Dict[str, Any]]:
    """Async POST of a JSON payload; same contract as :func:`post_json`.

    Preconditions:
        - ``url`` is a non-empty absolute URL.
        - ``timeout`` is a positive, finite number of seconds (enforced by
          ``get_pooled_async_client``).
    Postconditions:
        - On a 2xx response with a JSON body, returns the parsed value.
        - On any httpx transport error, non-2xx status, or JSON parse
          failure, logs a WARNING prefixed with ``log_context`` and returns
          None. Never raises for these failure modes (precondition
          violations still raise AssertionError).
        - Reuses the process-wide pooled async client; never opens/closes a
          client.
    """
    assert url, "url must be non-empty"
    try:
        client = get_pooled_async_client(timeout)
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
    except _REQUEST_ERRORS as e:
        logger.warning("%s failed: %s", log_context, e)
        return None


async def async_get_json(
    url: str,
    *,
    timeout: float = 30.0,
    log_context: str = "request",
) -> Optional[Dict[str, Any]]:
    """Async GET; same contract as :func:`get_json` / :func:`async_post_json`."""
    assert url, "url must be non-empty"
    try:
        client = get_pooled_async_client(timeout)
        resp = await client.get(url)
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
        - If ``status_fn()`` returns None, or raises any exception, on any
          call, immediately returns ``{status_key: "failed", "error":
          "Failed to get status"}`` — no further polling, no sleep.
        - If no terminal status is observed within ``total_timeout`` seconds,
          returns ``{status_key: "failed", "error": f"Timed out waiting for
          {log_context}"}``.
        - Never raises; never sleeps past a terminal/None/exception
          short-circuit.
    """
    assert poll_interval > 0, f"poll_interval must be positive, got {poll_interval!r}"
    assert total_timeout > 0, f"total_timeout must be positive, got {total_timeout!r}"
    start = time.monotonic()
    while (time.monotonic() - start) < total_timeout:
        try:
            status = status_fn()
        except Exception as e:
            logger.warning("%s status_fn raised: %s", log_context, e)
            return {status_key: "failed", "error": "Failed to get status"}
        if status is None:
            return {status_key: "failed", "error": "Failed to get status"}
        if status.get(status_key) in terminal_statuses:
            return status
        if on_poll is not None:
            on_poll(status)
        time.sleep(poll_interval)
    return {status_key: "failed", "error": f"Timed out waiting for {log_context}"}


async def async_poll_until_terminal(
    status_fn: Callable[[], Awaitable[Optional[Dict[str, Any]]]],
    *,
    terminal_statuses: FrozenSet[str] = DEFAULT_TERMINAL_STATUSES,
    status_key: str = "status",
    poll_interval: float = 5.0,
    total_timeout: float = 3600.0,
    on_poll: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    log_context: str = "job",
) -> Dict[str, Any]:
    """Async poll of ``status_fn()`` until terminal status or timeout.

    Preconditions:
        - ``status_fn`` is an async callable taking no arguments and returning
          either a status dict (expected to carry ``status_key``) or None if
          status could not be retrieved.
        - ``poll_interval`` and ``total_timeout`` are positive, finite
          seconds.
        - ``on_poll``, if given, is an async callable invoked with each
          *non-terminal* status dict, once per poll, strictly before that
          iteration's sleep.
    Postconditions:
        - Returns the first status dict for which
          ``status.get(status_key)`` is in ``terminal_statuses``, unmodified.
        - If ``status_fn()`` returns None, or raises any exception other than
          an overall-budget ``asyncio.TimeoutError``, on any call, immediately
          returns ``{status_key: "failed", "error": "Failed to get status"}``
          — no further polling, no sleep. The same failure dict is returned if
          ``on_poll`` raises a non-timeout exception.
        - If no terminal status is observed within ``total_timeout`` seconds
          of wall time — including time spent awaiting ``status_fn``,
          ``on_poll``, and the inter-poll sleep — returns
          ``{status_key: "failed", "error": f"Timed out waiting for
          {log_context}"}``. Each await is bounded by the remaining budget via
          ``asyncio.wait_for``.
        - Never raises; never sleeps past a terminal/None/exception
          short-circuit.
        - Uses ``asyncio.sleep`` between non-terminal polls.
    """
    assert poll_interval > 0, f"poll_interval must be positive, got {poll_interval!r}"
    assert total_timeout > 0, f"total_timeout must be positive, got {total_timeout!r}"
    start = time.monotonic()
    while True:
        remaining = total_timeout - (time.monotonic() - start)
        if remaining <= 0:
            return {status_key: "failed", "error": f"Timed out waiting for {log_context}"}
        try:
            status = await asyncio.wait_for(status_fn(), timeout=remaining)
        except asyncio.TimeoutError:
            return {status_key: "failed", "error": f"Timed out waiting for {log_context}"}
        except Exception as e:
            logger.warning("%s status_fn raised: %s", log_context, e)
            return {status_key: "failed", "error": "Failed to get status"}
        if status is None:
            return {status_key: "failed", "error": "Failed to get status"}
        if status.get(status_key) in terminal_statuses:
            return status
        if on_poll is not None:
            remaining = total_timeout - (time.monotonic() - start)
            if remaining <= 0:
                return {
                    status_key: "failed",
                    "error": f"Timed out waiting for {log_context}",
                }
            try:
                await asyncio.wait_for(on_poll(status), timeout=remaining)
            except asyncio.TimeoutError:
                return {
                    status_key: "failed",
                    "error": f"Timed out waiting for {log_context}",
                }
            except Exception as e:
                logger.warning("%s on_poll raised: %s", log_context, e)
                return {status_key: "failed", "error": "Failed to get status"}
        remaining = total_timeout - (time.monotonic() - start)
        if remaining <= 0:
            return {status_key: "failed", "error": f"Timed out waiting for {log_context}"}
        try:
            await asyncio.wait_for(asyncio.sleep(poll_interval), timeout=remaining)
        except asyncio.TimeoutError:
            return {status_key: "failed", "error": f"Timed out waiting for {log_context}"}

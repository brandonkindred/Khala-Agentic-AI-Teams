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
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Optional, Tuple

import httpx

from shared.http import get_pooled_async_client, get_pooled_client

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TERMINAL_STATUSES",
    "post_json",
    "get_json",
    "get_json_with_status",
    "poll_until_terminal",
    "async_post_json",
    "async_get_json",
    "async_poll_until_terminal",
]

DEFAULT_TERMINAL_STATUSES: FrozenSet[str] = frozenset({"completed", "failed", "cancelled"})

# JSONDecodeError is a ValueError subclass, so this tuple also covers a
# response body that isn't valid JSON.
_REQUEST_ERRORS = (httpx.HTTPError, ValueError)

# httpx signals "this client was closed underneath you" with a plain
# RuntimeError rather than an HTTPError, which would otherwise escape the
# never-raises contract when a pooled client is torn down mid-request. Match on
# the message rather than adding bare RuntimeError to _REQUEST_ERRORS: that
# would also swallow genuine programming errors in these helpers as a benign
# "request failed" None.
_CLOSED_CLIENT_MARKER = "client has been closed"


def _is_closed_client_error(exc: BaseException) -> bool:
    """True when ``exc`` is httpx's "client has been closed" RuntimeError.

    Preconditions:
        - ``exc`` is the exception being considered for the request-failure path.
    Postconditions:
        - Returns True only for a ``RuntimeError`` whose message identifies a
          closed client; every other ``RuntimeError`` (a real bug) returns False
          so it propagates.
    """
    return isinstance(exc, RuntimeError) and _CLOSED_CLIENT_MARKER in str(exc)


# Error string for a status-retrieval failure (status_fn returned None or raised).
_STATUS_FAILURE = "Failed to get status"
# Distinct error string for a progress-callback failure, so a broken on_poll is
# never misreported as the job's status being unreadable.
_ON_POLL_FAILURE = "Progress callback failed"

# Short grace for cancelled callback cleanup after the polling budget expires.
# Deliberately small so a CancelledError-suppressing callback cannot overrun
# the advertised wall-time deadline by more than this amount.
_BUDGET_CANCEL_GRACE_S = 0.05


class _BudgetExpired(Exception):
    """Internal: the overall ``total_timeout`` budget expired while awaiting."""


def _discard_task_result(task: "asyncio.Task[Any]") -> None:
    """Consume a detached task's result/exception so it is never orphaned.

    Registered as a done-callback on a timed-out poll callback that suppressed
    cancellation past the grace window. Retrieving the outcome here prevents an
    "exception was never retrieved" warning and drops any late result.

    Preconditions:
        - ``task`` is done when this callback fires (asyncio guarantee).
    Postconditions:
        - Never raises; a late exception is logged at DEBUG, a late value is
          dropped.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("Discarded exception from timed-out poll callback: %r", exc)


async def _await_within_budget(awaitable: Awaitable[Any], remaining: float) -> Any:
    """Await ``awaitable`` within ``remaining`` seconds without ``wait_for`` overrun.

    Unlike ``asyncio.wait_for``, once the budget expires this cancels the task
    and returns control after a short grace — it does not wait for slow
    cancellation cleanup, and it ignores a late success after the deadline.

    Preconditions:
        - ``awaitable`` is a coroutine or other awaitable to run once.
        - ``remaining`` is the seconds left in the overall poll budget.
    Postconditions:
        - Returns the awaitable's result if it finishes within ``remaining``.
        - Raises :class:`_BudgetExpired` if the budget expires (including when
          ``remaining <= 0`` before starting, or if the task is observed
          cancelled). A late terminal result after expiry is discarded.
        - Propagates exceptions raised by the awaitable itself (including
          ``asyncio.TimeoutError`` from the callback) unchanged — those are
          not budget expiry.
        - Leaves no undisposed task: a callback that suppresses cancellation
          past the grace window is detached with :func:`_discard_task_result`
          so the poller neither blocks on it nor leaks it as an unretrieved
          task. (Such a callback may still run its own side effects to
          completion — cooperative cancellation cannot be forced.)
        - If the *caller* is cancelled while waiting (e.g. the request handler
          driving the poll is cancelled), the in-flight task is cancelled and
          detached before ``CancelledError`` propagates, so cancelling a poll
          never strands a running ``status_fn``/``on_poll``.
    """
    if remaining <= 0:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise _BudgetExpired()
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=remaining)
        if task in done:
            if task.cancelled():
                raise _BudgetExpired()
            return task.result()
        # Budget expired: cancel and allow a short grace for cooperative cleanup.
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=_BUDGET_CANCEL_GRACE_S)
    except asyncio.CancelledError:
        # The caller was cancelled, not the budget. Do not leave the callback
        # running detached and unretrieved.
        _detach_task(task)
        raise
    if task not in done:
        # Callback is suppressing cancellation; do not block the poller. Detach
        # with a done-callback that consumes its eventual result/exception.
        task.add_done_callback(_discard_task_result)
    else:
        # Finished inside the grace window. If it finished by *raising* (rather
        # than by cancelling), nobody is going to await it, so consume the
        # exception here or asyncio logs "Task exception was never retrieved".
        _discard_task_result(task)
    raise _BudgetExpired()


def _detach_task(task: "asyncio.Task[Any]") -> None:
    """Cancel ``task`` (if still running) and consume its eventual outcome.

    Postconditions:
        - The task is cancelled and, unless already done, carries
          :func:`_discard_task_result` so its result/exception is retrieved.
        - Never raises.
    """
    if task.done():
        _discard_task_result(task)
        return
    task.cancel()
    task.add_done_callback(_discard_task_result)


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
    except RuntimeError as e:
        if not _is_closed_client_error(e):
            raise
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
    except RuntimeError as e:
        if not _is_closed_client_error(e):
            raise
        logger.warning("%s failed: %s", log_context, e)
        return None


def get_json_with_status(
    url: str,
    *,
    timeout: float = 30.0,
    log_context: str = "request",
) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """GET a resource without raising on a non-2xx status.

    Shares :func:`get_json`'s transport-failure semantics but skips
    ``raise_for_status``, so a caller that needs to branch on a specific
    status code (e.g. 404 vs. 5xx) doesn't lose that information the way
    ``get_json``'s swallow-to-None contract does.

    Preconditions:
        - ``url`` is a non-empty absolute URL.
    Postconditions:
        - On a transport-level failure (httpx error, or the pooled client
          having been closed underneath the caller), logs a WARNING tagged
          with ``log_context`` and returns ``(None, None)``.
        - Otherwise returns ``(resp.status_code, body)`` where ``body`` is
          the parsed JSON if the response has a valid JSON body, else None
          -- regardless of status code. Never raises for these failure
          modes (a precondition violation still raises AssertionError).
        - Reuses the process-wide pooled client; never opens/closes a
          client.
    """
    assert url, "url must be non-empty"
    try:
        client = get_pooled_client(timeout)
        resp = client.get(url)
    except httpx.HTTPError as e:
        logger.warning("%s failed: %s", log_context, e)
        return None, None
    except RuntimeError as e:
        if not _is_closed_client_error(e):
            raise
        logger.warning("%s failed: %s", log_context, e)
        return None, None
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body


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
    except RuntimeError as e:
        if not _is_closed_client_error(e):
            raise
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
    except RuntimeError as e:
        if not _is_closed_client_error(e):
            raise
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
        - If ``status_fn()`` returns None or raises, immediately returns
          ``{status_key: "failed", "error": "Failed to get status"}`` — no
          further polling, no sleep.
        - If ``on_poll`` raises, immediately returns ``{status_key: "failed",
          "error": "Progress callback failed"}``. The distinct message keeps a
          broken progress sink from being misread as an unreadable job status.
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
            return {status_key: "failed", "error": _STATUS_FAILURE}
        if status is None:
            return {status_key: "failed", "error": _STATUS_FAILURE}
        if status.get(status_key) in terminal_statuses:
            return status
        if on_poll is not None:
            try:
                on_poll(status)
            except Exception as e:
                logger.warning("%s on_poll raised: %s", log_context, e)
                return {status_key: "failed", "error": _ON_POLL_FAILURE}
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
        - If ``status_fn()`` returns None, or raises any exception (including
          a callback-raised ``asyncio.TimeoutError``), on any call, immediately
          returns ``{status_key: "failed", "error": "Failed to get status"}``
          — no further polling, no sleep. If ``on_poll`` raises (including
          callback ``TimeoutError``) the poll stops with the distinct
          ``{status_key: "failed", "error": "Progress callback failed"}`` so a
          broken progress sink is not misreported as an unreadable status.
        - If no terminal status is observed within ``total_timeout`` seconds
          of wall time — including time spent awaiting ``status_fn``,
          ``on_poll``, and the inter-poll sleep — returns
          ``{status_key: "failed", "error": f"Timed out waiting for
          {log_context}"}``. Each await is bounded by the remaining budget via
          :func:`_await_within_budget` (``asyncio.wait`` + cancel + short
          grace), so a slow-cancelling callback cannot report success after
          the deadline and budget expiry is distinct from callback
          ``TimeoutError``.
        - Never raises; never sleeps past a terminal/None/exception
          short-circuit.
        - Uses ``asyncio.sleep`` between non-terminal polls.
    """
    assert poll_interval > 0, f"poll_interval must be positive, got {poll_interval!r}"
    assert total_timeout > 0, f"total_timeout must be positive, got {total_timeout!r}"
    start = time.monotonic()
    timeout_result = {status_key: "failed", "error": f"Timed out waiting for {log_context}"}
    while True:
        remaining = total_timeout - (time.monotonic() - start)
        if remaining <= 0:
            return timeout_result
        try:
            status = await _await_within_budget(status_fn(), remaining)
        except _BudgetExpired:
            return timeout_result
        except Exception as e:
            logger.warning("%s status_fn raised: %s", log_context, e)
            return {status_key: "failed", "error": _STATUS_FAILURE}
        if status is None:
            return {status_key: "failed", "error": _STATUS_FAILURE}
        if status.get(status_key) in terminal_statuses:
            return status
        if on_poll is not None:
            remaining = total_timeout - (time.monotonic() - start)
            if remaining <= 0:
                return timeout_result
            try:
                await _await_within_budget(on_poll(status), remaining)
            except _BudgetExpired:
                return timeout_result
            except Exception as e:
                logger.warning("%s on_poll raised: %s", log_context, e)
                return {status_key: "failed", "error": _ON_POLL_FAILURE}
        remaining = total_timeout - (time.monotonic() - start)
        if remaining <= 0:
            return timeout_result
        try:
            await _await_within_budget(asyncio.sleep(poll_interval), remaining)
        except _BudgetExpired:
            return timeout_result

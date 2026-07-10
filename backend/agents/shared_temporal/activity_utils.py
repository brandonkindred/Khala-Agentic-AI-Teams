"""Shared helpers for Temporal activity bodies.

These wrap the ``temporalio.activity`` context APIs so activity implementations can
consult retry/cancellation state without each team re-deriving it (the same
``is_last_attempt`` / cancellation plumbing was previously hand-rolled per team).

Invariants:
    - Every helper is safe to call outside an activity context (direct/thread mode);
      it degrades to a documented default rather than raising.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from temporalio import activity
from temporalio.exceptions import CancelledError

logger = logging.getLogger(__name__)


def is_cancelled() -> bool:
    """True when the current activity has been cancelled.

    Preconditions:
        - None (safe to call outside an activity context).
    Postconditions:
        - Returns ``activity.is_cancelled()`` inside an activity context, else
          ``False`` (direct/thread use has no cancellation to observe).
    """
    try:
        return activity.is_cancelled()
    except RuntimeError:
        return False


def is_last_attempt() -> bool:
    """True when this is the final Temporal retry attempt (or no activity context).

    Reads ``maximum_attempts`` from the retry policy the activity was scheduled with
    (``activity.info().retry_policy``) rather than a compile-time constant, so the
    check never drifts from the workflow's policy.

    Preconditions:
        - Called from within an activity body (or directly / thread mode).
    Postconditions:
        - Returns True when the current attempt is the last Temporal will make, or
          when called outside an activity context (the caller then marks the job
          terminal). Returns False when the policy allows unlimited retries
          (``maximum_attempts <= 0``) -- there is no last attempt to gate on.
    """
    try:
        info = activity.info()
    except RuntimeError:
        return True
    policy = info.retry_policy
    max_attempts = policy.maximum_attempts if policy is not None else 0
    if max_attempts <= 0:
        return False
    return info.attempt >= max_attempts


def raise_if_cancelled(
    exc: BaseException,
    message: str,
    on_cancelled: Optional[Callable[[], None]] = None,
) -> None:
    """Propagate an in-flight cancellation as a Temporal ``CancelledError``.

    Sync activities don't heartbeat, so a worker-delivered cancellation can surface
    either as a raised ``CancelledError`` or -- after some other error in the same
    body -- as ``activity.is_cancelled()`` returning True. When either holds, this
    runs the optional ``on_cancelled`` side effect (e.g. marking the job cancelled)
    and raises so Temporal records the activity as cancelled; otherwise it returns
    and the caller handles ``exc`` as an ordinary failure. Centralizing this keeps
    the "cancellation is not a failure" contract identical across every stage.

    Preconditions:
        - Call from within an ``except`` handler for ``exc``.
    Postconditions:
        - Re-raises ``exc`` unchanged when it is already a ``CancelledError``; raises
          a fresh ``CancelledError(message)`` chained from ``exc`` when the activity
          is otherwise cancelled; returns ``None`` (no raise) when not cancelled.
    """
    if isinstance(exc, CancelledError):
        if on_cancelled is not None:
            on_cancelled()
        raise exc
    if is_cancelled():
        if on_cancelled is not None:
            on_cancelled()
        raise CancelledError(message) from exc

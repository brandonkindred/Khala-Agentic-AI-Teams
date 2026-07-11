"""Activity-body helpers shared across every team's Temporal activities.

Lightweight (only depends on ``temporalio.activity``) so it is cheap to import
from an activities module. Keep helpers here that run *inside* an activity and
need the runtime ``activity.info()`` context.
"""

from __future__ import annotations

from temporalio import activity


def is_last_attempt() -> bool:
    """True when the current activity attempt is the final Temporal retry.

    Reads ``maximum_attempts`` from the retry policy the activity was actually
    scheduled with (``activity.info().retry_policy``) rather than a compile-time
    constant, so the check never drifts from the workflow's policy and stays
    correct for in-flight histories scheduled under an older policy.

    Preconditions:
        - Called from within an activity body (or directly / in a unit test).
    Postconditions:
        - Returns True when the current attempt is the last one Temporal will make,
          or when called outside an activity context (direct/thread use — the caller
          then treats it as terminal).
        - Returns False when the scheduled policy allows unlimited retries
          (``maximum_attempts <= 0``) or a retry policy is absent — there is no
          "last attempt" to gate on, so the caller keeps deferring to Temporal.
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

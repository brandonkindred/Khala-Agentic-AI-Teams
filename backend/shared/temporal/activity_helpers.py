"""Shared helpers for the "report progress -> run work -> fail-on-final-attempt"
funnel that Temporal activity bodies commonly implement.

These generalize a pattern that used to be hand-rolled per team (planning_team's
``_json_safe``/``_merge_context``/``_fail``/``_is_final_attempt``/``_guarded``):
JSON-normalize a context dict crossing the activity boundary, and run a phase's
work under a guard that marks the owning job FAILED only on the last Temporal
retry attempt. The job-store calls (``mark_job_failed``/``update_job``) are taken
as injected callables rather than imported here, since every team has its own
job-store module -- this keeps the helpers reusable without coupling them to any
one team's storage layer.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from temporalio import activity


def json_safe(value: Any) -> Any:
    """Return ``value`` with any pydantic model rendered to a JSON-native dict.

    Preconditions:
        - ``value`` is a phase ``context`` value: a scalar, list, dict, or a
          pydantic model (anything with a ``model_dump`` method).
    Postconditions:
        - Models become ``model_dump(mode="json")`` dicts; lists and dict values
          are converted element-wise (recursively, so a model nested inside a dict
          is normalized too); everything else is returned unchanged. The result is
          JSON-serializable so it can cross the Temporal activity boundary under
          the default data converter.
    """
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def merge_context(context: Dict[str, Any], context_update: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a phase's ``context_update`` into ``context``, JSON-normalizing values.

    Preconditions:
        - ``context`` is the JSON-native context dict received by the activity.
        - ``context_update`` is the first element of a phase function's
          ``(context_update, artifacts)`` return.
    Postconditions:
        - Returns a new dict = ``context`` overlaid with ``context_update``, with
          every overlaid value passed through :func:`json_safe` so the returned
          context stays JSON-serializable (no pydantic objects survive).
    """
    merged = dict(context)
    for key, value in context_update.items():
        merged[key] = json_safe(value)
    return merged


def is_final_attempt(max_attempts: int) -> bool:
    """Return True when the current activity attempt is the last one allowed.

    Preconditions:
        - ``max_attempts`` is the ``maximum_attempts`` of the phase's RetryPolicy
          (>= 1). It MUST match the policy the workflow assigns this activity, or
          the FAILED marking fires on the wrong attempt.
    Postconditions:
        - Inside a Temporal worker, returns ``activity.info().attempt >= max_attempts``
          -- i.e. Temporal will not retry after this attempt.
        - Outside a worker (direct call in unit tests), returns True so a failure
          still surfaces the FAILED marking rather than being silently swallowed.
    """
    if not activity.in_activity():
        return True
    return activity.info().attempt >= max_attempts


def fail_job(
    job_id: str,
    exc: BaseException,
    *,
    mark_job_failed: Callable[..., Any],
) -> None:
    """Mark a job FAILED after an activity error (before the caller re-raises).

    Preconditions:
        - ``job_id`` refers to an existing job record; ``exc`` is the caught error.
        - ``mark_job_failed`` is the owning team's job-store function, callable as
          ``mark_job_failed(job_id, error=str(exc))``.
    Postconditions:
        - The job-store row for ``job_id`` is marked FAILED with ``str(exc)``. The
          caller re-raises so the failure also surfaces as a failed Temporal
          activity/workflow rather than a silently-"completed" one.
    """
    activity.logger.exception("Activity failed for job %s", job_id)
    mark_job_failed(job_id, error=str(exc))


def guarded(
    job_id: str,
    phase: str,
    progress: int,
    status_text: str,
    work: Callable[[], Any],
    *,
    max_attempts: int,
    update_job: Callable[..., Any],
    mark_job_failed: Callable[..., Any],
    status: Optional[str] = None,
) -> Any:
    """Report phase progress, run ``work``, and mark the job FAILED on final failure.

    Preconditions:
        - ``job_id`` refers to an existing job; ``progress`` is 0..100; ``work`` is
          a zero-arg callable performing the phase's work and returning its result.
        - ``max_attempts`` matches the phase's Temporal RetryPolicy.
        - ``update_job`` is the owning team's job-store function, callable as
          ``update_job(job_id, **fields)``; ``mark_job_failed`` is passed through
          to :func:`fail_job`.
    Postconditions:
        - Updates ``current_phase``/``progress``/``status_text``, and writes
          ``status`` only when supplied.
        - The progress write is inside the guard, so a failing progress write still
          marks the job FAILED rather than leaving it stuck non-terminal.
        - On error, the job is marked FAILED only on the *final* Temporal attempt
          (:func:`is_final_attempt`); a retry that later succeeds therefore never
          leaves a transient FAILED status or a stale ``error`` behind. The
          exception is always re-raised so Temporal's RetryPolicy governs
          re-attempts.
    """
    fields: Dict[str, Any] = {
        "current_phase": phase,
        "progress": progress,
        "status_text": status_text,
    }
    if status is not None:
        fields["status"] = status
    try:
        update_job(job_id, **fields)
        return work()
    except Exception as exc:
        if is_final_attempt(max_attempts):
            fail_job(job_id, exc, mark_job_failed=mark_job_failed)
        raise

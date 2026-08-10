"""Temporal Pattern-A export module for GitHub issue grooming.

``run_issue_grooming_activity`` wraps ``IssueGroomingRunner`` (Phase A heuristic
Fibonacci scoring, then Phase B sub-issue splitting -- see
``software_engineering_team.github_source.issue_grooming_runner``) with a
background heartbeat and job-store failure marking. ``IssueGroomingWorkflow``
schedules that activity and propagates its terminal result/failure unchanged.
Both are registered (as ``WORKFLOWS``/``ACTIVITIES``) onto the coding-team
Temporal worker (``software_engineering_team.temporal.coding_team_worker``).

Like ``coding_team_workflow.py``, this module MUST NOT start a worker or read
``TEMPORAL_ADDRESS`` at import time: it defines ``IssueGroomingWorkflow``, so
the temporalio sandbox re-imports it during workflow registration, and a
top-level ``os.getenv``/``start_team_worker`` call would trip the sandbox or
race the first dispatch. For the same reason every non-trivial import used by
``run_issue_grooming_activity`` (job store, GitHub client, the runner,
``BackgroundHeartbeat``) is deferred into the function bodies below rather
than imported at module scope, mirroring ``coding_team_workflow.py``'s own
activities.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable, Optional

from pydantic import BaseModel
from temporalio import activity, workflow

logger = logging.getLogger(__name__)

# Single LLM scoring/decomposition pass -- much shorter-lived than the coding
# pipeline's 4h timeout.
_GROOMING_ACTIVITY_TIMEOUT = timedelta(minutes=10)


class IssueGroomingRunRequest(BaseModel):
    """Typed request payload for a GitHub issue grooming Temporal run.

    Preconditions:
        - ``job_id`` is a non-empty str naming the coding-team job row this
          run reports progress against.
        - ``owner``/``repo`` identify an accessible GitHub repository.
        - ``issue_number`` is a positive int naming an open issue in that repo.
    Postconditions:
        - Instances are immutable data; they cross the activity/workflow
          boundary as JSON-native dicts via ``model_dump(mode="json")`` and
          are rebuilt on the other side with ``model_validate``.
    """

    job_id: str
    owner: str
    repo: str
    issue_number: int


class IssueGroomingRunResult(BaseModel):
    """Typed result payload for a GitHub issue grooming Temporal run.

    Postconditions:
        - ``status`` reports the terminal outcome (``"completed"``/``"cancelled"``,
          as constructed by ``run_issue_grooming_activity``); ``phase``/``grooming``
          are populated once a Phase A and/or Phase B pass has run.
          ``IssueGroomingWorkflow.run`` never constructs this model itself --
          it returns the activity's already-serialized result dict unchanged.
    """

    status: str
    phase: Optional[str] = None
    grooming: Optional[dict[str, Any]] = None


def _grooming_heartbeat_interval_s() -> float:
    """Interval (seconds) between background heartbeats for the grooming activity.

    Must stay comfortably below the activity's ``heartbeat_timeout``. Override via
    ``GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S``; blank/unparseable/non-finite
    falls back to 30s, a parseable value below 0.1 clamps up to 0.1 (per
    ``shared.env.parse_float``'s documented garbage-to-default /
    out-of-range-to-floor contract). Independent of the coding-team activity's
    own ``CODING_TEAM_HEARTBEAT_INTERVAL_S`` -- separate activities, separate
    knobs.
    """
    from shared.env import parse_float

    return parse_float("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", 30.0, minimum=0.1)


def _grooming_update_callback(job_id: str) -> Callable[..., None]:
    """Forward ``IssueGroomingRunner`` progress writes to ``update_job(job_id, **kw)``.

    Liveness is owned by the background ``BackgroundHeartbeat`` in
    ``run_issue_grooming_activity``, not here -- this callback does not heartbeat
    (mirrors ``_coding_update_callback``'s single-liveness-owner invariant).

    Preconditions:
        - ``job_id`` identifies an existing job.
    Postconditions:
        - The returned callable forwards all kwargs to ``update_job(job_id, **kwargs)``.
    """
    from software_engineering_team.job_store import update_job

    def _update(**kw: Any) -> None:
        update_job(job_id, **kw)

    return _update


@activity.defn(name="issue_grooming_run")
def run_issue_grooming_activity(request: dict[str, Any]) -> dict[str, Any]:
    """Run GitHub issue grooming (Phase A, then conditionally Phase B) for one issue.

    Preconditions:
        - ``request`` is a dict conforming to ``IssueGroomingRunRequest``.
        - ``GITHUB_TOKEN`` is set in the environment -- the only token source this
          activity resolves; ``IssueGroomingRunRequest`` carries no token field,
          so there is no per-job encrypted-token path here (that is dispatch-layer
          territory, out of scope for this activity).
    Postconditions:
        - Validates ``request`` before touching the job store or GitHub (a
          malformed request has no reliable ``job_id`` to mark failed against).
        - On success, returns an ``IssueGroomingRunResult`` dict.
          ``IssueGroomingRunner`` owns the job's terminal status on every clean
          exit path (``COMPLETED`` or ``CANCELLED`` -- see its own docstring),
          so this activity does not re-write it; it only reads the final status
          back to report an accurate result.
        - On any exception (missing token, a GitHub API failure, a runner bug),
          marks the job ``FAILED`` with the error message (passed through
          ``scrub_token_from_text`` first, matching
          ``mark_coding_team_job_failed_activity``'s scrubbed-error contract --
          a git-remote-URL-embedded token echoed into an exception message must
          never reach the job store or Temporal history unredacted) and
          re-raises.
    """
    req = IssueGroomingRunRequest.model_validate(request)
    from software_engineering_team.job_store import get_job, update_job
    from software_engineering_team.models import JobStatus

    try:
        import os

        from shared.concurrency import BackgroundHeartbeat
        from software_engineering_team.github_source.client import GitHubClient
        from software_engineering_team.github_source.issue_grooming_runner import (
            IssueGroomingRunner,
        )

        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN not configured")

        # Single liveness mechanism: a background beater emits activity.heartbeat()
        # on a fixed interval for the whole run (mirrors execute_coding_team_activity).
        # copy_context=True carries the Temporal activity handle into the beater
        # thread; the update callback passed to IssueGroomingRunner must not itself
        # heartbeat.
        with BackgroundHeartbeat(
            activity.heartbeat,
            _grooming_heartbeat_interval_s(),
            name="issue-grooming-heartbeat",
            copy_context=True,
            join_timeout=5.0,
        ):
            with GitHubClient(token=token) as client:
                runner = IssueGroomingRunner(
                    client,
                    update_job_fn=_grooming_update_callback(req.job_id),
                    get_job_fn=lambda jid: get_job(jid),
                )
                grooming = runner.run(req.job_id, req.owner, req.repo, req.issue_number)

        job = get_job(req.job_id)
        cancelled = bool(job and job.get("status") == JobStatus.CANCELLED.value)
        return IssueGroomingRunResult(
            status="cancelled" if cancelled else "completed",
            phase="cancelled" if cancelled else "done",
            grooming=grooming,
        ).model_dump()
    except Exception as e:
        from software_engineering_team.github_source import scrub_token_from_text

        logger.exception("run_issue_grooming_activity failed for job %s", req.job_id)
        update_job(req.job_id, error=scrub_token_from_text(str(e)), status=JobStatus.FAILED.value)
        raise


@workflow.defn(name="IssueGroomingWorkflow")
class IssueGroomingWorkflow:
    """Durable orchestrator for a GitHub issue grooming Phase A->B run.

    Invariants:
        - Declaring the workflow class here (rather than only once its body
          is implemented) lets ``WORKFLOWS`` register it with the
          coding-team worker without a later rewrite of worker registration.
    """

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        """Schedule the grooming activity and propagate its outcome.

        Preconditions:
            - ``request`` is a dict conforming to ``IssueGroomingRunRequest``.
        Postconditions:
            - Validates ``request`` first, raising ``ValidationError``
              synchronously before scheduling anything when it does not
              conform to ``IssueGroomingRunRequest`` -- no activity is
              scheduled for a malformed request.
            - Otherwise schedules ``run_issue_grooming_activity`` with a
              ``_GROOMING_ACTIVITY_TIMEOUT`` start-to-close timeout and no
              custom retry policy (Temporal's default retry applies -- this
              is a plain compute/LLM call with no documented idempotency
              hazard, unlike the short side-effect activities elsewhere in
              this package that use a bounded policy).
            - On success, returns the activity's result dict unchanged --
              this method never re-wraps it into ``IssueGroomingRunResult``.
            - On activity failure, does not catch or re-wrap the exception:
              it propagates uncaught, so Temporal marks this workflow itself
              terminally failed. This method makes no job-store writes and
              posts no GitHub notices -- both remain the activity's (and a
              follow-up change's) responsibility, not this workflow's.
        """
        IssueGroomingRunRequest.model_validate(request)
        return await workflow.execute_activity(
            run_issue_grooming_activity,
            request,
            start_to_close_timeout=_GROOMING_ACTIVITY_TIMEOUT,
        )


WORKFLOWS = [IssueGroomingWorkflow]
ACTIVITIES = [run_issue_grooming_activity]

# NB: no worker self-boot at import time -- see module docstring. Boot lives
# in ``software_engineering_team.temporal.coding_team_worker``, which merges
# these lists onto the coding-team worker alongside ``CodingTeamWorkflow``'s
# own ``WORKFLOWS``/``ACTIVITIES``.

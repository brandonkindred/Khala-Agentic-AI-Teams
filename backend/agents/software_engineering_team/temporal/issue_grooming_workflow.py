"""Temporal Pattern-A export module for GitHub issue grooming.

``run_issue_grooming_activity`` wraps ``IssueGroomingRunner`` (Phase A heuristic
Fibonacci scoring, then Phase B sub-issue splitting -- see
``software_engineering_team.github_source.issue_grooming_runner``) with a
background heartbeat and job-store failure marking; ``IssueGroomingRunner``
itself owns the job's terminal status on every clean exit path (completed or
cooperatively cancelled). ``IssueGroomingWorkflow`` schedules that activity
and, on a Temporal-native cancellation or any other activity failure the
runner never got to handle itself (e.g. the activity process crashing before
its own exception handler runs), best-effort terminalizes the coding-team job
row as a defense-in-depth safety net before propagating the outcome
unchanged. Both are registered (as ``WORKFLOWS``/``ACTIVITIES``) onto the
coding-team Temporal worker (``software_engineering_team.temporal.coding_team_worker``).

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

import asyncio
import logging
from datetime import timedelta
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict
from temporalio import activity, workflow
from temporalio.exceptions import is_cancelled_exception

from software_engineering_team.temporal.coding_team_workflow import (
    mark_coding_team_job_cancelled_activity,
    mark_coding_team_job_failed_activity,
)

logger = logging.getLogger(__name__)

# Single LLM scoring/decomposition pass -- much shorter-lived than the coding
# pipeline's 4h timeout.
_GROOMING_ACTIVITY_TIMEOUT = timedelta(minutes=10)
# Cheap, idempotent job-store write -- short timeout, matches the analogous
# mark-failed/mark-cancelled calls in coding_team_workflow.py.
_TERMINALIZE_ACTIVITY_TIMEOUT = timedelta(minutes=1)


def _log_terminalize_failure(message: str) -> None:
    """Best-effort log from workflow code that may run outside Temporal's runtime."""
    try:
        workflow.logger.exception(message)
    except Exception:
        logging.getLogger(__name__).exception(message)


class IssueGroomingRunRequest(BaseModel):
    """Typed request payload for a GitHub issue grooming Temporal run.

    Preconditions:
        - ``job_id`` is a non-empty str naming the coding-team job row this
          run reports progress against.
        - ``owner``/``repo`` identify an accessible GitHub repository.
        - ``issue_number`` is a positive int naming an open issue in that repo.
    Postconditions:
        - Instances are immutable data (``model_config`` is frozen); they cross
          the activity/workflow boundary as JSON-native dicts via
          ``model_dump(mode="json")`` and are rebuilt on the other side with
          ``model_validate``.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    owner: str
    repo: str
    issue_number: int


class IssueGroomingRunResult(BaseModel):
    """Typed result payload for a GitHub issue grooming Temporal run.

    Postconditions:
        - ``status`` reports the terminal outcome (``"completed"``/``"cancelled"``,
          as constructed by ``run_issue_grooming_activity``).
        - ``phase`` is a terminal marker, always set on a clean exit: ``"done"``
          when the job's final status is not ``CANCELLED``, ``"cancelled"``
          otherwise -- it does not indicate which of Phase A/B actually ran.
          On exception this model is never constructed at all: the activity
          re-raises after marking the job ``FAILED``, so ``phase`` carries no
          meaning for a failed run.
        - ``grooming`` carries the runner's output dict when available; it may
          be ``None`` or missing ``sub_issues`` when cancellation cut the run
          short before or during Phase B (see ``IssueGroomingRunner``).
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
        - ``request`` is a dict conforming to ``IssueGroomingRunRequest`` and
          carries no ``token`` field -- the token is resolved activity-side from
          the job's ``github_token_encrypted`` (set by ``post_groom_github_issues``)
          or the ``GITHUB_TOKEN`` env fallback, via the same
          ``_require_activity_github_token`` helper the other coding-team GitHub
          activities use (``coding_team_github_activities.py``).
    Postconditions:
        - Validates ``request`` before touching the job store or GitHub (a
          malformed request has no reliable ``job_id`` to mark failed against).
        - On success, returns an ``IssueGroomingRunResult`` dict.
          ``IssueGroomingRunner`` owns the job's terminal status on every clean
          exit path (``COMPLETED`` or ``CANCELLED`` -- see its own docstring),
          so this activity does not re-write it; it only reads the final status
          back to report an accurate result.
        - On any exception (missing token, a GitHub API failure, a runner bug),
          scrubs the error via ``scrub_token_from_text`` once and reuses that
          scrubbed message for both the log line and the job-store write --
          matching ``mark_coding_team_job_failed_activity``'s scrubbed-error
          contract, since a git-remote-URL-embedded token echoed into an
          exception message must never reach logs, the job store, or Temporal
          history unredacted. Also clears ``status_text`` (mirroring
          ``mark_coding_team_job_failed_activity``'s terminal-state cleanup so
          a stale in-progress message doesn't linger on a failed job), and
          re-raises.
    """
    req = IssueGroomingRunRequest.model_validate(request)
    from software_engineering_team.job_store import get_job, update_job
    from software_engineering_team.models import JobStatus

    try:
        from shared.concurrency import BackgroundHeartbeat
        from software_engineering_team.github_source.client import GitHubClient
        from software_engineering_team.github_source.issue_grooming_runner import (
            IssueGroomingRunner,
        )
        from software_engineering_team.temporal.coding_team_github_activities import (
            _require_activity_github_token,
        )

        token = _require_activity_github_token(request)

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

        scrubbed = scrub_token_from_text(str(e))
        # Log the scrubbed message, not the raw exception/traceback: an unredacted
        # token embedded in a git-remote URL or API error must never reach log
        # aggregation, mirroring the job-store write below.
        logger.error("run_issue_grooming_activity failed for job %s: %s", req.job_id, scrubbed)
        update_job(
            req.job_id,
            error=scrubbed,
            status=JobStatus.FAILED.value,
            status_text=None,
        )
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
              scheduled for a malformed request, and no job-store write is
              attempted (a malformed request never named a real job to
              terminalize).
            - Otherwise schedules ``run_issue_grooming_activity`` with a
              ``_GROOMING_ACTIVITY_TIMEOUT`` start-to-close timeout and no
              custom retry policy (Temporal's default retry applies -- this
              is a plain compute/LLM call with no documented idempotency
              hazard, unlike the short side-effect activities elsewhere in
              this package that use a bounded policy).
            - On success, returns the activity's result dict unchanged --
              this method never re-wraps it into ``IssueGroomingRunResult``.
            - On a Temporal cancellation, best-effort schedules
              ``mark_coding_team_job_cancelled_activity`` -- matching
              ``coding_team_orchestrator``'s cooperative-cancel convention
              (``status=cancelled``, ``status_text="Cancelled by user"``, no
              ``error`` write) -- then always re-raises the cancellation
              unchanged, so Temporal's own workflow outcome still reports
              cancelled. A bare ``asyncio.CancelledError`` (delivered when
              this workflow itself is cancelled, e.g. directly via Temporal)
              needs its own ``except`` clause: unlike
              ``temporalio.exceptions.CancelledError``/``ActivityError``, it
              subclasses ``BaseException`` rather than ``Exception`` since
              Python 3.8, so ``except Exception`` alone would silently miss
              it. ``is_cancelled_exception`` (checked inside the ``Exception``
              branch) additionally catches an ``ActivityError``/
              ``ChildWorkflowError`` whose ``cause`` is a cancellation.
            - On any other activity failure, best-effort schedules
              ``mark_coding_team_job_failed_activity`` -- matching
              ``coding_team_workflow.CodingTeamWorkflow``'s GitHub-notice
              fallback convention (``status=failed``, a scrubbed ``error``,
              ``status_text``/``current_activity`` cleared) -- then always
              re-raises the original exception unchanged.
            - A failure of the mark-cancelled/mark-failed terminalize call
              itself is logged only and never masks or replaces the original
              cancellation/exception being propagated -- mirroring
              ``CodingTeamWorkflow._best_effort_terminalize_then_reraise``.
              This method posts no GitHub notices -- that remains the
              activity's (and a follow-up change's) responsibility, not this
              workflow's.
        """
        IssueGroomingRunRequest.model_validate(request)
        job_id = request["job_id"]
        try:
            return await workflow.execute_activity(
                run_issue_grooming_activity,
                request,
                start_to_close_timeout=_GROOMING_ACTIVITY_TIMEOUT,
            )
        except asyncio.CancelledError:
            await self._best_effort_terminalize(
                mark_coding_team_job_cancelled_activity, {"job_id": job_id}
            )
            raise
        except Exception as exc:
            if is_cancelled_exception(exc):
                await self._best_effort_terminalize(
                    mark_coding_team_job_cancelled_activity, {"job_id": job_id}
                )
            else:
                message = str(exc) or f"{type(exc).__name__}: issue grooming run failed"
                await self._best_effort_terminalize(
                    mark_coding_team_job_failed_activity,
                    {"job_id": job_id, "error": message},
                )
            raise

    async def _best_effort_terminalize(self, activity_fn: Any, args: dict[str, Any]) -> None:
        """Run a mark-failed/mark-cancelled activity, logging (not raising) on failure.

        Preconditions:
            - Intended to be called from the ``except`` branch of :meth:`run`;
              callers are responsible for re-raising their original
              exception, because this helper swallows all terminalize
              failures and never raises one of its own to signal a problem.
        Postconditions:
            - Schedules ``activity_fn`` with ``args`` and
              ``_TERMINALIZE_ACTIVITY_TIMEOUT``; both ``Exception`` and
              ``asyncio.CancelledError`` raised by that call are caught and
              logged, never propagated -- a terminalize failure (including
              the terminalize activity itself being cancelled, e.g. because
              the whole workflow is concurrently tearing down) must not
              replace or mask the original error/cancellation the caller is
              about to re-raise. ``asyncio.CancelledError`` needs its own
              branch in the ``except`` tuple: it subclasses ``BaseException``
              rather than ``Exception`` since Python 3.8, so ``except
              Exception`` alone would let it escape and overwrite ``run``'s
              original outcome -- the same gotcha ``run`` itself guards
              against on the outer activity dispatch.
        """
        try:
            await workflow.execute_activity(
                activity_fn,
                args,
                start_to_close_timeout=_TERMINALIZE_ACTIVITY_TIMEOUT,
            )
        except (Exception, asyncio.CancelledError):
            _log_terminalize_failure(
                f"{activity_fn.__name__} failed while terminalizing issue grooming job"
            )


WORKFLOWS = [IssueGroomingWorkflow]
ACTIVITIES = [run_issue_grooming_activity]
# NB: mark_coding_team_job_cancelled_activity/mark_coding_team_job_failed_activity are
# deliberately NOT listed here even though IssueGroomingWorkflow.run schedules them --
# they are already registered via coding_team_workflow.ACTIVITIES, and the worker
# merges ACTIVITIES + GROOMING_ACTIVITIES (coding_team_worker.py); listing the same
# activity object twice would register it twice on the same worker.

# NB: no worker self-boot at import time -- see module docstring. Boot lives
# in ``software_engineering_team.temporal.coding_team_worker``, which merges
# these lists onto the coding-team worker alongside ``CodingTeamWorkflow``'s
# own ``WORKFLOWS``/``ACTIVITIES``.

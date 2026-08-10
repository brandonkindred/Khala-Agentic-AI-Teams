"""Temporal Pattern-A export module for GitHub issue grooming.

GitHub issue grooming (Phase A score/template, then Phase B sub-issue split)
currently runs in thread mode only. This module makes it importable as a
Temporal package: typed request/result payloads, an ``IssueGroomingWorkflow``
that schedules ``run_issue_grooming_activity`` and, on a terminal failure or
cancellation, best-effort terminalizes the coding-team job row before
propagating the outcome unchanged -- exported as ``WORKFLOWS`` and
``ACTIVITIES`` and registered onto the coding-team Temporal worker
(``software_engineering_team.temporal.coding_team_worker``). The activity body
itself remains a stub pending a follow-up change that wraps the real Phase
A->B grooming runner and job-store progress writes -- so running this
workflow today still terminates in ``NotImplementedError``, which is the
expected terminal-failure path, not a bug.

Like ``coding_team_workflow.py``, this module MUST NOT start a worker or read
``TEMPORAL_ADDRESS`` at import time: it defines ``IssueGroomingWorkflow``, so
the temporalio sandbox re-imports it during workflow registration, and a
top-level ``os.getenv``/``start_team_worker`` call would trip the sandbox or
race the first dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Optional

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.exceptions import is_cancelled_exception

from software_engineering_team.temporal.coding_team_workflow import (
    mark_coding_team_job_cancelled_activity,
    mark_coding_team_job_failed_activity,
)

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
        - ``status`` reports the terminal outcome once real activity/workflow
          bodies exist; ``phase``/``grooming`` are populated only after a
          Phase A and/or Phase B pass has run. The stub bodies in this module
          never construct this model -- they raise before producing one.
    """

    status: str
    phase: Optional[str] = None
    grooming: Optional[dict[str, Any]] = None


@activity.defn(name="issue_grooming_run")
def run_issue_grooming_activity(request: dict[str, Any]) -> dict[str, Any]:
    """Stub activity for the GitHub issue grooming Phase A->B run.

    Preconditions:
        - ``request`` is a dict conforming to ``IssueGroomingRunRequest``.
    Postconditions:
        - None yet: always raises ``NotImplementedError``. The real body
          (wrapping the Phase A->B grooming runner and writing progress to
          the job store) is implemented in a follow-up change.
    """
    IssueGroomingRunRequest.model_validate(request)
    raise NotImplementedError("issue grooming activity body is implemented in a follow-up change")


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
                message = str(exc) or "issue grooming run failed"
                await self._best_effort_terminalize(
                    mark_coding_team_job_failed_activity,
                    {"job_id": job_id, "error": message},
                )
            raise

    async def _best_effort_terminalize(self, activity_fn: Any, args: dict[str, Any]) -> None:
        """Run a mark-failed/mark-cancelled activity, logging (not raising) on failure.

        Preconditions:
            - Called only from the ``except`` branch of :meth:`run`, so the
              caller always re-raises its own exception right after this
              returns, regardless of outcome here.
        Postconditions:
            - Schedules ``activity_fn`` with ``args`` and
              ``_TERMINALIZE_ACTIVITY_TIMEOUT``; any exception it raises is
              caught and logged, never propagated -- a terminalize failure
              must not replace or mask the original error/cancellation the
              caller is about to re-raise.
        """
        try:
            await workflow.execute_activity(
                activity_fn,
                args,
                start_to_close_timeout=_TERMINALIZE_ACTIVITY_TIMEOUT,
            )
        except Exception:
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

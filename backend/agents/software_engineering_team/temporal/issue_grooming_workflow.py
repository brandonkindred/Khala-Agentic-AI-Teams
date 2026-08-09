"""Temporal Pattern-A export scaffold for GitHub issue grooming.

GitHub issue grooming (Phase A score/template, then Phase B sub-issue split)
currently runs in thread mode only. This module is the scaffold that makes it
importable as a Temporal package: typed request/result payloads plus stub
``@activity.defn``/``@workflow.defn`` registrations, exported as ``WORKFLOWS``
and ``ACTIVITIES`` for the coding-team Temporal worker
(``software_engineering_team.temporal.coding_team_worker``) to pick up. Real
Phase A->B execution, workflow orchestration, and worker registration land in
follow-up changes -- the stub bodies below only validate shape and raise.

Like ``coding_team_workflow.py``, this module MUST NOT start a worker or read
``TEMPORAL_ADDRESS`` at import time: it defines ``IssueGroomingWorkflow``, so
the temporalio sandbox re-imports it during workflow registration, and a
top-level ``os.getenv``/``start_team_worker`` call would trip the sandbox or
race the first dispatch.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel
from temporalio import activity, workflow


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
    """Stub durable workflow for GitHub issue grooming.

    Invariants:
        - Declaring the workflow class here (rather than only once its body
          is implemented) lets ``WORKFLOWS`` register it with the
          coding-team worker without a later rewrite of worker registration.
    """

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        """Stub run method.

        Preconditions:
            - ``request`` is a dict conforming to ``IssueGroomingRunRequest``.
        Postconditions:
            - None yet: always raises ``NotImplementedError``. The real
              Phase A->B orchestration (scheduling ``run_issue_grooming_activity``
              and propagating terminal success/failure) is implemented in a
              follow-up change.
        """
        IssueGroomingRunRequest.model_validate(request)
        raise NotImplementedError(
            "issue grooming workflow orchestration is implemented in a follow-up change"
        )


WORKFLOWS = [IssueGroomingWorkflow]
ACTIVITIES = [run_issue_grooming_activity]

# NB: no worker self-boot at import time -- see module docstring. Boot will
# live in ``software_engineering_team.temporal.coding_team_worker`` once
# these lists are registered onto the coding-team worker (follow-up change),
# the same place ``CodingTeamWorkflow``'s own boot lives.

"""Start the issue grooming Temporal workflow from synchronous API code.

Thin wrapper over ``shared.temporal.start_workflow_sync`` (the shared sync->async
bridge, which polls for the worker's Temporal client to become ready before
dispatching) -- mirrors ``coding_team_start_workflow.start_coding_team_workflow``.
"""

from __future__ import annotations

import logging

from shared.temporal import start_workflow_sync
from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE
from software_engineering_team.temporal.issue_grooming_workflow import IssueGroomingWorkflow

logger = logging.getLogger(__name__)

WORKFLOW_ID_PREFIX = "issue-grooming-"


def start_issue_grooming_workflow(job_id: str, owner: str, repo: str, issue_number: int) -> None:
    """Start ``IssueGroomingWorkflow`` for a GitHub issue grooming job.

    Preconditions:
        - ``job_id`` is a non-empty str whose job row already exists (the API
          called ``create_job`` before dispatching).
        - ``owner``/``repo`` identify a GitHub repository; ``issue_number`` is a
          positive int naming an issue in that repo.
    Postconditions:
        - A workflow with id ``issue-grooming-<job_id>`` is started on the coding
          team task queue (fire-and-forget; the caller polls
          ``GET /status/{job_id}``). Raises ``RuntimeError`` if the worker's
          Temporal client never becomes available within the wait window.
    """
    assert job_id, "start_issue_grooming_workflow requires a non-empty job_id"
    payload = {
        "job_id": job_id,
        "owner": owner,
        "repo": repo,
        "issue_number": issue_number,
    }
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        IssueGroomingWorkflow.run,
        payload,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started IssueGroomingWorkflow id=%s", workflow_id)

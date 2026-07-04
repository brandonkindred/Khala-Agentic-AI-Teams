"""Start the coding team Temporal workflow from synchronous API code.

Thin wrapper over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge, which polls for the worker's Temporal client to become ready before
dispatching). We deliberately do NOT use ``shared_temporal.run_team_job`` here:
it creates its own job row and sets ``status=running`` itself, which would
collide with the API's ``create_job`` and the activity-owned status bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from coding_team.temporal import CodingTeamWorkflow
from coding_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def start_coding_team_workflow(
    job_id: str,
    repo_path: str,
    plan_input: Optional[Dict[str, Any]],
) -> None:
    """Start ``CodingTeamWorkflow`` for a coding-team job.

    Preconditions:
        - ``job_id`` is a non-empty str whose job row already exists (the API
          called ``create_job`` before dispatching).
        - ``repo_path`` is a non-empty str; ``plan_input`` is a JSON-serializable
          plan dict (a run with no plan has nothing to execute).
    Postconditions:
        - A workflow with id ``coding_team-<job_id>`` is started on the coding
          team task queue (fire-and-forget; the caller polls
          ``GET /status/{job_id}``). The activity reuses ``job_id`` so the polled
          id matches the orchestrator's writes. Raises ``RuntimeError`` if the
          worker's Temporal client never becomes available within the wait
          window.
    """
    assert job_id, "start_coding_team_workflow requires a non-empty job_id"
    assert repo_path, "start_coding_team_workflow requires a non-empty repo_path"
    payload: Dict[str, Any] = {
        "job_id": job_id,
        "repo_path": repo_path,
        "plan_input": plan_input,
    }
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        CodingTeamWorkflow.run,
        payload,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started CodingTeamWorkflow id=%s", workflow_id)

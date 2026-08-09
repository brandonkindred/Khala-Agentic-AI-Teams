"""Start the coding team Temporal workflow from synchronous API code.

Thin wrapper over ``shared.temporal.start_workflow_sync`` (the shared sync→async
bridge, which polls for the worker's Temporal client to become ready before
dispatching). We deliberately do NOT use ``shared.temporal.run_team_job`` here:
it creates its own job row and sets ``status=running`` itself, which would
collide with the API's ``create_job`` and the activity-owned status bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from shared.temporal import start_workflow_sync
from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from software_engineering_team.temporal.coding_team_workflow import CodingTeamWorkflow

logger = logging.getLogger(__name__)


def start_coding_team_workflow(
    job_id: str,
    repo_path: str,
    plan_input: Optional[Dict[str, Any]],
    github: Optional[Dict[str, Any]] = None,
) -> None:
    """Start ``CodingTeamWorkflow`` for a coding-team job.

    Preconditions:
        - ``job_id`` is a non-empty str whose job row already exists (the API
          called ``create_job`` before dispatching).
        - ``repo_path`` is a non-empty str; ``plan_input`` is a JSON-serializable
          plan dict (a run with no plan has nothing to execute).
        - ``github``, when provided, is a dict of GitHub-issue run metadata for
          the workflow (owner/repo/issue/base/integration_branch/...). It must
          not contain a plaintext token — activities resolve tokens activity-side.
    Postconditions:
        - A workflow with id ``coding_team-<job_id>`` is started on the coding
          team task queue (fire-and-forget; the caller polls
          ``GET /status/{job_id}``). When ``github`` is a non-empty dict it is
          included on the payload under ``"github"``; otherwise that key is
          omitted. Raises ``RuntimeError`` if the worker's Temporal client never
          becomes available within the wait window.
    """
    assert job_id, "start_coding_team_workflow requires a non-empty job_id"
    assert repo_path, "start_coding_team_workflow requires a non-empty repo_path"
    payload: Dict[str, Any] = {
        "job_id": job_id,
        "repo_path": repo_path,
        "plan_input": plan_input,
    }
    if github:
        assert "token" not in github, "github workflow payload must not include a token"
        payload["github"] = github
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        CodingTeamWorkflow.run,
        payload,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started CodingTeamWorkflow id=%s", workflow_id)

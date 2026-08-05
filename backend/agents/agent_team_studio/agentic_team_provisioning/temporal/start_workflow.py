"""Start the agentic pipeline Temporal workflow from synchronous API code.

Thin wrapper over ``shared.temporal.start_workflow_sync`` (the shared sync→async
bridge). We deliberately do NOT use ``shared.temporal.run_team_job`` here: it creates
its own ``JobServiceClient`` row and status bookkeeping, which would collide with the
team's own ``AgenticTestStore`` run row (``agentic_test_pipeline_runs``) and the
activity-owned RUNNING/COMPLETED/FAILED bookkeeping.

The WAIT timeout is resolved *here* (in the API process, outside the temporalio
sandbox) and passed to the workflow as an argument, so the workflow module never reads
env at import time.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_team_studio.agentic_team_provisioning.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    AgenticPipelineWorkflow,
)
from agent_team_studio.agentic_team_provisioning.wait_timeout import resolve_wait_timeout_s
from shared.temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def start_agentic_pipeline_workflow(
    run_id: str,
    team_agents_json: list[dict[str, Any]],
    process_json: dict[str, Any],
    initial_input: str | None,
) -> None:
    """Start ``AgenticPipelineWorkflow`` for the given pipeline run.

    Preconditions:
        - ``run_id`` is a run already created in the store (status ``running``,
          ``temporal_owned=True``).
        - ``team_agents_json`` / ``process_json`` are serialized ``AgenticTeamAgent`` /
          ``ProcessDefinition`` (``model_dump(mode="json")``).

    Postconditions:
        - A workflow with id ``agentic-pipeline-<run_id>`` is started on the team's
          task queue (raises ``RuntimeError`` if the worker client never becomes
          available within the wait window).
    """
    wait_timeout_s = resolve_wait_timeout_s()
    workflow_id = f"{WORKFLOW_ID_PREFIX}{run_id}"
    start_workflow_sync(
        AgenticPipelineWorkflow.run,
        run_id,
        team_agents_json,
        process_json,
        initial_input,
        wait_timeout_s,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started AgenticPipelineWorkflow id=%s", workflow_id)

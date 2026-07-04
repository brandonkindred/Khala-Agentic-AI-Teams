"""Start the road-trip Temporal workflow from synchronous API code.

Thin wrapper over ``shared_temporal.start_workflow_sync`` (the shared sync→async
bridge). We deliberately do NOT use ``shared_temporal.run_team_job`` here: it
creates its own job row (under the ``road_trip_planning`` team slug) and sets
``status=running`` itself, which would collide with the API's ``create_job``
(namespaced under ``road_trip_planning_team``) and the activity-owned
RUNNING/COMPLETED bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any

from road_trip_planning_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from road_trip_planning_team.temporal.workflows import RoadTripWorkflow
from shared_temporal import start_workflow_sync

logger = logging.getLogger(__name__)


def start_road_trip_workflow(job_id: str, request: dict[str, Any]) -> None:
    """Start ``RoadTripWorkflow`` for the given job.

    Preconditions:
        - ``job_id`` is a job already created in the job store.
        - ``request`` is the serialized ``PlanTripRequest`` (``body.model_dump()``).

    Postconditions:
        - A workflow with id ``road-trip-planning-<job_id>`` is started on the
          road-trip task queue (raises ``RuntimeError`` if the worker client
          never becomes available within the wait window).
    """
    workflow_id = f"{WORKFLOW_ID_PREFIX}{job_id}"
    start_workflow_sync(
        RoadTripWorkflow.run,
        job_id,
        request,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started RoadTripWorkflow id=%s", workflow_id)

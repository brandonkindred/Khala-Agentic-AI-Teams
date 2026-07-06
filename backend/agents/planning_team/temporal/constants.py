"""Temporal task queue and workflow IDs for the Planning team."""

import os

#: Task queue the Planning worker polls and the API dispatches workflows to;
#: overridable via ``TEMPORAL_TASK_QUEUE_PLANNING``.
TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_PLANNING", "planning").strip()

#: Prefix prepended to a job_id to form the Temporal workflow id, so workflow
#: ids are namespaced per team and never collide across teams sharing a
#: Temporal server.
WORKFLOW_ID_PREFIX = "planning-"

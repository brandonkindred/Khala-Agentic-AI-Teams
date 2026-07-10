"""Temporal task queue and workflow IDs for the Planning team."""

import os

#: Task queue the Planning worker polls and the API dispatches workflows to;
#: overridable via ``TEMPORAL_TASK_QUEUE_PLANNING``.
TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_PLANNING", "planning").strip()

#: Prefix prepended to a job_id to form the Temporal workflow id, so workflow
#: ids are namespaced per team and never collide across teams sharing a
#: Temporal server.
WORKFLOW_ID_PREFIX = "planning-"

#: Max Temporal attempts for the two retry classes, shared between the workflow's
#: RetryPolicies (workflows.py) and the activities' final-attempt check
#: (activities.py). Kept in one place so the two never drift: an activity only
#: marks the job FAILED on its *final* attempt, and "final" must match the number
#: of attempts the workflow actually grants that phase.
RETRYABLE_MAX_ATTEMPTS = 3
SINGLE_ATTEMPT = 1

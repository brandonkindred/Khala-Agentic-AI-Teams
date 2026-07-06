"""Temporal workflow + activity wrapping the job matching scan pipeline.

The workflow class and activity live in :mod:`workflows` (sandbox-safe — no
top-level non-deterministic calls). Worker startup lives in :mod:`worker` and is
invoked by the team_service entrypoint at boot (``TEAM_TEMPORAL_WORKER_MODULE`` /
``TEAM_TEMPORAL_WORKER_FUNC``), with the API lifespan backstop
(``_start_temporal_worker_backstop`` in ``api/main.py``) covering standalone
runs. The team is also registered in ``shared_temporal.teams_registry`` for any
in-process ``start_all_team_workers`` host (none in the current topology — the
unified API is a pure proxy). This package ``__init__`` only re-exports — it
never touches ``os.getenv`` or starts a worker, so importing it (including the
temporalio sandbox re-import) has no side effects.
"""

from __future__ import annotations

from job_matching_team.temporal.workflows import (
    JobMatchingWorkflow,
    run_scan_activity,
)

WORKFLOWS = [JobMatchingWorkflow]
ACTIVITIES = [run_scan_activity]
# Matches the registry's f"{team}-queue" so a registry-started worker and the
# start_workflow dispatch agree on the same task queue.
TASK_QUEUE = "job_matching-queue"
WORKFLOW_ID_PREFIX = "job-matching-"

__all__ = [
    "ACTIVITIES",
    "JobMatchingWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "run_scan_activity",
]

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
    build_queries_activity,
    fail_scan_activity,
    finalize_scan_activity,
    prepare_scan_activity,
    rank_activity,
    run_scan_activity,
    scan_activity,
)

WORKFLOWS = [JobMatchingWorkflow]
# The scan pipeline is decomposed into per-phase activities that the workflow
# schedules in order. ``run_scan_activity`` (the pre-decomposition monolith) is
# kept registered LAST purely for in-flight-history drain-out — the current
# workflow never schedules it.
ACTIVITIES = [
    prepare_scan_activity,
    build_queries_activity,
    scan_activity,
    rank_activity,
    finalize_scan_activity,
    fail_scan_activity,
    run_scan_activity,
]
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
    "build_queries_activity",
    "fail_scan_activity",
    "finalize_scan_activity",
    "prepare_scan_activity",
    "rank_activity",
    "run_scan_activity",
    "scan_activity",
]

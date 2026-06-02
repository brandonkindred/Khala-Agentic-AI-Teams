"""Temporal workflow/activity registration for the job matching team.

Follows Pattern A: export ``WORKFLOWS``/``ACTIVITIES`` and start a worker on
import when Temporal is enabled. A no-op otherwise.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="job_matching_run_scan")
def run_scan_activity(request: dict[str, Any]) -> dict[str, Any]:
    from job_matching_team.models import JobMatchRequest
    from job_matching_team.orchestrator import JobMatchingOrchestrator

    req = JobMatchRequest(**request)
    result = JobMatchingOrchestrator().run(req)
    return result.model_dump(mode="json")


@workflow.defn(name="JobMatchingWorkflow")
class JobMatchingWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_scan_activity,
            request,
            start_to_close_timeout=timedelta(minutes=30),
        )


WORKFLOWS = [JobMatchingWorkflow]
ACTIVITIES = [run_scan_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():  # pragma: no cover - requires a live Temporal server
    start_team_worker("job_matching", WORKFLOWS, ACTIVITIES, task_queue="job-matching-queue")

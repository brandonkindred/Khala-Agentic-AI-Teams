"""Temporal workflow + activity wrapping the accessibility audit orchestrator."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="accessibility_audit_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    from accessibility_audit_team.api.main import CreateAuditRequest
    from accessibility_audit_team.orchestrator import AccessibilityAuditOrchestrator

    req = CreateAuditRequest(**request)
    result = AccessibilityAuditOrchestrator().run_audit(req)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result if isinstance(result, dict) else {"result": result}


@workflow.defn(name="AccessibilityAuditWorkflow")
class AccessibilityAuditWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(hours=2),
        )


WORKFLOWS = [AccessibilityAuditWorkflow]
ACTIVITIES = [run_pipeline_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker(
        "accessibility_audit", WORKFLOWS, ACTIVITIES, task_queue="accessibility_audit-queue"
    )

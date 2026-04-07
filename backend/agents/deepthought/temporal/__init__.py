"""Temporal workflow + activity wrapping the deepthought orchestrator."""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


@activity.defn(name="deepthought_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    from deepthought.api.main import DeepthoughtRequest
    from deepthought.orchestrator import DeepthoughtOrchestrator

    req = DeepthoughtRequest(**request)
    orch = DeepthoughtOrchestrator()
    result = orch.process_message(req)
    if inspect.iscoroutine(result):
        result = asyncio.new_event_loop().run_until_complete(result)
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return result if isinstance(result, dict) else {"result": result}


@workflow.defn(name="DeepthoughtWorkflow")
class DeepthoughtWorkflow:
    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(hours=1),
        )


WORKFLOWS = [DeepthoughtWorkflow]
ACTIVITIES = [run_pipeline_activity]

from shared_temporal import is_temporal_enabled, start_team_worker  # noqa: E402

if is_temporal_enabled():
    start_team_worker("deepthought", WORKFLOWS, ACTIVITIES, task_queue="deepthought-queue")

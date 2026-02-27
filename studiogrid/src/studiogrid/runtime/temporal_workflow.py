from __future__ import annotations

from datetime import timedelta

try:
    from temporalio import workflow
except Exception:  # pragma: no cover
    class _WorkflowShim:
        def defn(self, cls=None, **kwargs):
            return cls

        def run(self, fn):
            return fn

        def signal(self, fn):
            return fn

    workflow = _WorkflowShim()


TASK_QUEUE = "studiogrid"


@workflow.defn
class StudioGridWorkflow:
    @workflow.run
    async def run(self, project_name: str, intake_payload: dict) -> dict:
        del timedelta
        del project_name
        del intake_payload
        return {"status": "stub"}

    @workflow.signal
    async def decision_resolved(self, decision_id: str) -> None:
        del decision_id

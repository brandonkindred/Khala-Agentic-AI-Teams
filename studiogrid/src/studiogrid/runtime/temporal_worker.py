from __future__ import annotations

import asyncio
import os

from studiogrid.runtime import temporal_activities as acts
from studiogrid.runtime.temporal_workflow import StudioGridWorkflow

try:
    from temporalio.client import Client
    from temporalio.worker import Worker
except Exception:  # pragma: no cover
    Client = None
    Worker = None


def env(key: str, default: str | None = None) -> str:
    value = os.getenv(key, default)
    if value is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return value


async def main() -> None:
    if Client is None or Worker is None:
        raise RuntimeError("temporalio is required to run worker")
    client = await Client.connect(env("STUDIOGRID_TEMPORAL_SERVER", "localhost:7233"), namespace=env("STUDIOGRID_TEMPORAL_NAMESPACE", "default"))
    worker = Worker(
        client,
        task_queue=env("STUDIOGRID_TEMPORAL_TASK_QUEUE", "studiogrid"),
        workflows=[StudioGridWorkflow],
        activities=[
            acts.create_project_and_run,
            acts.set_phase,
            acts.run_phase,
            acts.create_approval_decision,
            acts.get_decision,
            acts.set_waiting,
            acts.set_running,
            acts.assemble_handoff,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from studiogrid.runtime import temporal_activities as acts
from studiogrid.runtime.temporal_workflow import StudioGridWorkflow


def env(key: str, default: str | None = None) -> str:
    value = os.getenv(key, default)
    if value is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return value


async def main() -> None:
    client = await Client.connect(
        env("STUDIOGRID_TEMPORAL_SERVER", "localhost:7233"),
        namespace=env("STUDIOGRID_TEMPORAL_NAMESPACE", "default"),
    )
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
            acts.run_revision_loop,
            acts.fail_run,
            acts.assemble_handoff,
            acts.set_done,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

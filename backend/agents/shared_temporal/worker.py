"""Shared Temporal worker startup.

Every team used to hand-roll this: create a ThreadPoolExecutor, connect the
client, build a ``Worker``, run it in a daemon thread. ``start_team_worker``
replaces all that boilerplate — a team just passes its workflows/activities
list.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Optional

from shared_temporal.client import (
    connect_temporal_client,
    get_default_task_queue,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)

logger = logging.getLogger(__name__)

_worker_threads: dict[str, threading.Thread] = {}
_activity_executors: dict[str, ThreadPoolExecutor] = {}


def _build_workflow_runner() -> Any:
    """Build a SandboxedWorkflowRunner that passes through pydantic.

    Without this, pydantic schema generation for models that reference
    ``datetime.datetime`` (e.g. ``Optional[datetime]`` fields) fails
    inside the Temporal workflow sandbox: pydantic-core compares types
    by identity and the sandboxed reimport of pydantic ends up with a
    different ``datetime.datetime`` reference than pydantic-core's
    compiled one, raising ``PydanticSchemaGenerationError``. Marking
    ``pydantic``/``pydantic_core`` as pass-through loads them via the
    real importer, so the datetime identity check succeeds.
    """
    from temporalio.worker.workflow_sandbox import (
        SandboxedWorkflowRunner,
        SandboxRestrictions,
    )

    restrictions = SandboxRestrictions.default.with_passthrough_modules(
        "pydantic",
        "pydantic_core",
    )
    return SandboxedWorkflowRunner(restrictions=restrictions)


async def _run_worker_async(
    team: str,
    task_queue: str,
    workflows: Iterable[Any],
    activities: Iterable[Any],
    max_concurrent_activities: int,
) -> None:
    from temporalio.worker import Worker

    client = await connect_temporal_client()
    if client is None:
        return
    # First team to connect owns the shared client/loop slots.
    set_temporal_client(client)
    set_temporal_loop(asyncio.get_running_loop())

    executor = _activity_executors.setdefault(
        team,
        ThreadPoolExecutor(
            max_workers=max_concurrent_activities,
            thread_name_prefix=f"{team}-temporal-activity",
        ),
    )
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=list(workflows),
        activities=list(activities),
        activity_executor=executor,
        max_concurrent_activities=max_concurrent_activities,
        workflow_runner=_build_workflow_runner(),
    )
    logger.info("Temporal worker starting: team=%s task_queue=%s", team, task_queue)
    await worker.run()


def start_team_worker(
    team: str,
    workflows: Iterable[Any],
    activities: Iterable[Any],
    task_queue: Optional[str] = None,
    max_concurrent_activities: int = 4,
) -> bool:
    """Start a Temporal worker for a team in a daemon thread.

    Returns True if a worker thread is running (or already running),
    False when Temporal is disabled.
    """
    if not is_temporal_enabled():
        logger.info("Temporal disabled; skipping worker for team=%s", team)
        return False
    existing = _worker_threads.get(team)
    if existing is not None and existing.is_alive():
        return True

    queue = task_queue or get_default_task_queue()

    def _target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                _run_worker_async(team, queue, workflows, activities, max_concurrent_activities)
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Temporal worker failed for team=%s: %s", team, e)
        finally:
            loop.close()

    thread = threading.Thread(target=_target, name=f"{team}-temporal-worker", daemon=True)
    thread.start()
    _worker_threads[team] = thread
    logger.info("Temporal worker thread started for team=%s queue=%s", team, queue)
    return True

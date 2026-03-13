"""Temporal worker for the Nutrition & Meal Planning team."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from temporalio.worker import Worker

from nutrition_meal_planning_team.temporal.activities import run_meal_plan_activity
from nutrition_meal_planning_team.temporal.client import (
    connect_temporal_client,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)
from nutrition_meal_planning_team.temporal.constants import TASK_QUEUE
from nutrition_meal_planning_team.temporal.workflows import NutritionMealPlanWorkflow

logger = logging.getLogger(__name__)

_worker_thread: Optional[threading.Thread] = None
_activity_executor: Optional[ThreadPoolExecutor] = None


def create_nutrition_worker(client: Optional[object] = None) -> Optional[Worker]:
    if not is_temporal_enabled():
        return None
    if client is None:
        return None
    global _activity_executor
    if _activity_executor is None:
        _activity_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nutrition-temporal-activity")
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[NutritionMealPlanWorkflow],
        activities=[run_meal_plan_activity],
        activity_executor=_activity_executor,
        max_concurrent_activities=2,
    )
    logger.info("Nutrition Meal Planning Temporal worker created for task queue %s", TASK_QUEUE)
    return worker


async def _run_worker_async() -> None:
    client = await connect_temporal_client()
    if client is None:
        return
    set_temporal_client(client)
    set_temporal_loop(asyncio.get_running_loop())
    worker = create_nutrition_worker(client)
    if worker is None:
        return
    logger.info("Nutrition Meal Planning Temporal worker starting")
    await worker.run()


def _worker_thread_target() -> None:
    global _worker_thread
    if not is_temporal_enabled():
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_worker_async())
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.exception("Nutrition Meal Planning Temporal worker failed: %s", e)
    finally:
        set_temporal_client(None)
        set_temporal_loop(None)
        loop.close()


def start_nutrition_temporal_worker_thread() -> bool:
    global _worker_thread
    if not is_temporal_enabled():
        return False
    if _worker_thread is not None and _worker_thread.is_alive():
        return True
    _worker_thread = threading.Thread(
        target=_worker_thread_target,
        name="nutrition-temporal-worker",
        daemon=True,
    )
    _worker_thread.start()
    logger.info("Nutrition Meal Planning Temporal worker thread started")
    return True

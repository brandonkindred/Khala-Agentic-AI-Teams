"""Temporal worker(s) for the Agent Provisioning team.

``start_agent_provisioning_temporal_worker_thread`` is the explicit no-arg
boot the generic team_service entrypoint invokes via
``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC`` (matching
``planning_team`` and ``personal_assistant_team``). Importing
``agent_team_studio.agent_provisioning_team.temporal`` does NOT start a worker — only this helper
(and ``shared.temporal.teams_registry.start_all_team_workers``) does.
This module also retains ``create_agent_provisioning_worker`` for tests and
diagnostics that want to build a ``Worker`` instance directly.

``start_agent_provisioning_sandbox_temporal_worker_thread`` is a second,
independent worker for sandbox lifecycle workflows/activities only — called
explicitly from ``unified_api/main.py``'s own lifespan. See its docstring and
``SANDBOX_TASK_QUEUE`` in ``temporal/constants.py`` for why sandbox work is
pinned to its own queue/worker rather than sharing this team's general one.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from agent_team_studio.agent_provisioning_team.temporal.constants import TASK_QUEUE
from shared.temporal.client import is_temporal_enabled

logger = logging.getLogger(__name__)

_activity_executor: Optional[ThreadPoolExecutor] = None
_activity_executor_lock = threading.Lock()


def start_agent_provisioning_temporal_worker_thread() -> bool:
    """Start the Agent Provisioning Temporal worker (no-op when disabled).

    Preconditions:
        - None. Safe to call multiple times — ``start_team_worker`` is
          idempotent per team, so this is a no-op if a prior call (or
          ``start_all_team_workers``) already started the worker.
    Postconditions:
        - Returns True if a worker thread is running (or already running) for
          this team, False when Temporal is disabled. Serves only
          ``WORKFLOWS``/``ACTIVITIES`` (provisioning/deprovision) — safe to run
          in any process, including the standalone team-service container,
          since none of it depends on process-local state. Sandbox
          workflows/activities are intentionally excluded; see
          :func:`start_agent_provisioning_sandbox_temporal_worker_thread`.
    """
    from agent_team_studio.agent_provisioning_team.temporal import ACTIVITIES, WORKFLOWS
    from shared.temporal import start_team_worker

    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "agent_provisioning",
        WORKFLOWS,
        ACTIVITIES,
        task_queue=TASK_QUEUE,
    )


def start_agent_provisioning_sandbox_temporal_worker_thread() -> bool:
    """Start the sandbox-only Temporal worker, on its own task queue.

    Preconditions:
        - Must be called only from the process that owns the sandbox
          ``Lifecycle`` singleton these activities mutate — the unified API
          process (``unified_api/main.py``'s own lifespan calls this
          explicitly; it is never started by package import or by the
          standalone agent-provisioning-service team container). Calling it
          from any other process would reintroduce the state-divergence hazard
          ``SANDBOX_TASK_QUEUE``'s docstring (in ``temporal/constants.py``)
          describes.
    Postconditions:
        - Returns True if a worker thread is running (or already running) for
          the ``"agent_provisioning_sandbox"`` team key on
          ``SANDBOX_TASK_QUEUE``, False when Temporal is disabled. Uses a
          distinct team key from :func:`start_agent_provisioning_temporal_worker_thread`
          so the two worker threads are tracked independently and neither
          call can no-op against the other's registration.
    """
    from agent_team_studio.agent_provisioning_team.temporal import (
        SANDBOX_ACTIVITIES,
        SANDBOX_WORKFLOWS,
    )
    from agent_team_studio.agent_provisioning_team.temporal.constants import SANDBOX_TASK_QUEUE
    from shared.temporal import start_team_worker

    if not is_temporal_enabled():
        return False
    return start_team_worker(
        "agent_provisioning_sandbox",
        SANDBOX_WORKFLOWS,
        SANDBOX_ACTIVITIES,
        task_queue=SANDBOX_TASK_QUEUE,
    )


def create_agent_provisioning_worker(client: Optional[Client] = None) -> Optional[Worker]:
    """Build a ``Worker`` wired to every registered workflow/activity.

    Preconditions:
        - None; returns ``None`` (rather than raising) when Temporal is
          disabled or ``client`` is not supplied, so callers can branch on a
          single value.
    Postconditions:
        - The returned ``Worker`` serves exactly ``temporal.WORKFLOWS`` /
          ``temporal.ACTIVITIES`` — the same canonical lists
          :func:`start_agent_provisioning_temporal_worker_thread` registers —
          so this function can never silently drift from what ``__init__.py``
          exports.
    """
    if not is_temporal_enabled():
        return None
    if client is None:
        return None
    from agent_team_studio.agent_provisioning_team.temporal import ACTIVITIES, WORKFLOWS

    global _activity_executor
    with _activity_executor_lock:
        if _activity_executor is None:
            _activity_executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="agent-provisioning-temporal-activity"
            )
    # Pass pydantic through the workflow sandbox so models with
    # datetime fields (DeliverResult.finalized_at, etc.) don't trip
    # pydantic-core's identity-based type check. See the longer
    # explanation in shared/temporal/worker.py:_build_workflow_runner.
    sandbox_restrictions = SandboxRestrictions.default.with_passthrough_modules(
        "pydantic",
        "pydantic_core",
    )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        activity_executor=_activity_executor,
        max_concurrent_activities=8,
        workflow_runner=SandboxedWorkflowRunner(restrictions=sandbox_restrictions),
    )
    logger.info("Agent Provisioning Temporal worker created for task queue %s", TASK_QUEUE)
    return worker

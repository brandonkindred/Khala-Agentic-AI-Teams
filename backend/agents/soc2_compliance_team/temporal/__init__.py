"""Temporal workflow + activities for the SOC2 compliance team.

The workflow class and the per-stage activities live in :mod:`workflows` /
:mod:`activities` (sandbox-safe — no import-time side effects, no ``os.getenv``).
Worker startup lives in :mod:`worker` and is invoked by the team_service
entrypoint at boot (``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``),
with the API ``on_startup`` as a standalone-dev backstop, so the Temporal client
is connected before the API serves its first request. This package ``__init__``
must stay free of import-time side effects — the temporalio sandbox replays it
during workflow registration.
"""

from __future__ import annotations

import os

from soc2_compliance_team.temporal.activities import (
    audit_criterion_activity,
    load_repo_activity,
    mark_failed_activity,
    write_report_activity,
)
from soc2_compliance_team.temporal.workflows import Soc2AuditWorkflow

WORKFLOWS = [Soc2AuditWorkflow]
ACTIVITIES = [
    load_repo_activity,
    audit_criterion_activity,
    write_report_activity,
    mark_failed_activity,
]
TASK_QUEUE = "soc2_compliance-queue"
WORKFLOW_ID_PREFIX = "soc2-audit-"

# The workflow fans out all 5 TSC criteria (``soc2_audit_criterion``)
# concurrently via ``asyncio.gather``. ``start_team_worker``'s default
# ``max_concurrent_activities=4`` would leave one criterion queued behind the
# other four for up to their full 30-minute start-to-close budget before it
# even starts running — pushing it close to (or past) its 1-hour
# schedule-to-close ceiling. 8 slots comfortably covers one job's 5-way
# fan-out plus headroom for a concurrent job's load/report/mark-failed step.
# Defined here (not just in ``temporal/worker.py``) so both the dedicated
# ``start_soc2_temporal_worker_thread`` boot hook and the generic
# ``shared_temporal.teams_registry.start_all_team_workers`` host apply the
# same concurrency, instead of the registry silently falling back to the
# shared default and reintroducing the queuing problem this constant exists
# to avoid.
MAX_CONCURRENT_ACTIVITIES = 8


def resolve_task_queue() -> str:
    """The SOC2 task queue name, honoring an optional operator override.

    Defined here (not called here) so it stays a plain function definition at
    import time — safe under the temporalio sandbox, which only forbids
    *calling* restricted functions like ``os.getenv`` during workflow replay,
    not defining a function that would call one if invoked. Callers
    (``temporal/worker.py``, ``temporal/start_workflow.py``) call this lazily
    at worker-boot / dispatch time, never from workflow code.

    Postconditions:
        - Returns ``TEMPORAL_TASK_QUEUE_SOC2`` (stripped) if set and non-empty,
          else the default ``TASK_QUEUE``.
    """
    override = os.getenv("TEMPORAL_TASK_QUEUE_SOC2", "").strip()
    return override or TASK_QUEUE


__all__ = [
    "ACTIVITIES",
    "MAX_CONCURRENT_ACTIVITIES",
    "Soc2AuditWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "audit_criterion_activity",
    "load_repo_activity",
    "mark_failed_activity",
    "resolve_task_queue",
    "write_report_activity",
]

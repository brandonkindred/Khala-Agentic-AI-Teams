"""Temporal workflows + activities for the accessibility audit team (Pattern A).

Importing this package is intentionally side-effect free: it must NOT start a
worker or call ``os.getenv``/``is_temporal_enabled`` at module top level. The
temporalio workflow sandbox re-imports this module to register the workflows and
aborts on restricted calls, and a self-bootstrapping worker races the first
dispatch. Worker boot lives in ``temporal.worker`` (invoked by the team_service
entrypoint); dispatch lives in ``temporal.start_workflow``.

This module is a thin re-export hub: the workflow classes live in
:mod:`.workflows`, the per-phase activities in :mod:`.activities`, and the
task-queue / activity-name literals in :mod:`.constants`. ``WORKFLOWS`` /
``ACTIVITIES`` are the exact lists the worker registers — keep every
``@activity.defn`` in ``ACTIVITIES`` or the workflow hangs on an unregistered
activity (guarded by ``tests/test_temporal_bootstrap.py``).
"""

from __future__ import annotations

from temporalio import workflow  # re-exported for back-compat with existing tests/callers

from accessibility_audit_team.temporal.activities import (
    discovery_activity,
    finalize_activity,
    intake_activity,
    report_packaging_activity,
    retest_activity,
    run_pipeline_activity,
    verification_activity,
)
from accessibility_audit_team.temporal.constants import TASK_QUEUE
from accessibility_audit_team.temporal.workflows import (
    _AUDIT_RETRY_POLICY,
    AccessibilityAuditWorkflow,
    AccessibilityRetestWorkflow,
)

WORKFLOWS = [AccessibilityAuditWorkflow, AccessibilityRetestWorkflow]
ACTIVITIES = [
    intake_activity,
    discovery_activity,
    verification_activity,
    report_packaging_activity,
    finalize_activity,
    retest_activity,
    run_pipeline_activity,  # legacy whole-pipeline activity, last, for drain-out
]

__all__ = [
    "ACTIVITIES",
    "WORKFLOWS",
    "TASK_QUEUE",
    "_AUDIT_RETRY_POLICY",
    "AccessibilityAuditWorkflow",
    "AccessibilityRetestWorkflow",
    "workflow",
    "intake_activity",
    "discovery_activity",
    "verification_activity",
    "report_packaging_activity",
    "finalize_activity",
    "retest_activity",
    "run_pipeline_activity",
]

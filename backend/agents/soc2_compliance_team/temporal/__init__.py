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

__all__ = [
    "ACTIVITIES",
    "Soc2AuditWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "audit_criterion_activity",
    "load_repo_activity",
    "mark_failed_activity",
    "write_report_activity",
]

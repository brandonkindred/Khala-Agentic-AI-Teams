"""Temporal task queue, workflow ids, and activity names for the accessibility team.

Import-time-safe: this module holds only literals so importing it (and therefore
the ``accessibility_audit_team.temporal`` package that re-exports it) performs no
``os.getenv`` at module load. The temporalio workflow sandbox re-imports the
package during workflow registration and a top-level ``os.getenv`` trips it — so,
unlike some other teams, ``TASK_QUEUE`` is a hard-coded literal rather than an
env read (see ``tests/test_temporal_bootstrap.py``). ``timedelta``/``RetryPolicy``
objects live in ``workflows.py`` to keep this module dependency-free.
"""

from __future__ import annotations

#: Task queue the accessibility_audit worker polls and the API dispatches to.
TASK_QUEUE = "accessibility_audit-queue"

#: Prefixes prepended to a job_id to form the per-flow Temporal workflow ids, so
#: ids are namespaced and never collide across teams sharing a Temporal server.
WORKFLOW_ID_PREFIX = "accessibility_audit-"
RETEST_WORKFLOW_ID_PREFIX = "accessibility_audit-retest-"

# Per-phase ``@activity.defn`` names. Stable identifiers recorded in workflow
# history — renaming one strands in-flight executions, so treat them as a contract.
ACTIVITY_INTAKE = "accessibility_audit_intake"
ACTIVITY_DISCOVERY = "accessibility_audit_discovery"
ACTIVITY_VERIFICATION = "accessibility_audit_verification"
ACTIVITY_REPORT_PACKAGING = "accessibility_audit_report_packaging"
ACTIVITY_FINALIZE = "accessibility_audit_finalize"
ACTIVITY_RETEST = "accessibility_audit_retest"
#: Legacy whole-pipeline activity, retained for history drain-out.
ACTIVITY_RUN_PIPELINE = "accessibility_audit_run_pipeline"

#: Marker gating the per-phase sequence in ``AccessibilityAuditWorkflow.run``. New
#: executions record it and take the per-phase path; a history recorded before the
#: decomposition has no marker and replays the single legacy activity.
PER_PHASE_PATCH = "accessibility-audit-per-phase-activities"

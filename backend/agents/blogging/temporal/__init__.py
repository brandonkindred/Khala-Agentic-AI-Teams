"""Temporal workflow, activities, and worker wiring for the blogging team.

Exports Pattern-A-style ``WORKFLOWS``/``ACTIVITIES`` lists (a single source of truth
consumed by ``temporal/worker.py`` and available to guard tests). Importing this
package only binds the workflow class and activity functions into those lists — it
has no other side effects (worker startup happens from the service entrypoint), and
the activity/workflow modules keep their heavy imports inside function bodies so the
temporalio sandbox can re-import them safely.
"""

from blogging.temporal.activities import (
    draft_stage_activity,
    finalize_job_activity,
    gates_stage_activity,
    plan_stage_activity,
    run_full_pipeline_activity,
)
from blogging.temporal.client import is_temporal_enabled
from blogging.temporal.constants import TASK_QUEUE
from blogging.temporal.workflows import BlogFullPipelineWorkflow

WORKFLOWS = [BlogFullPipelineWorkflow]
ACTIVITIES = [
    plan_stage_activity,
    draft_stage_activity,
    gates_stage_activity,
    finalize_job_activity,
    # Legacy monolith, registered for drain-out of pre-decomposition histories.
    run_full_pipeline_activity,
]

__all__ = [
    "ACTIVITIES",
    "BlogFullPipelineWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "draft_stage_activity",
    "finalize_job_activity",
    "gates_stage_activity",
    "is_temporal_enabled",
    "plan_stage_activity",
    "run_full_pipeline_activity",
]

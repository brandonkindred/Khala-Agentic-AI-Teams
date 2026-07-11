"""Temporal workflow, activities, and worker wiring for the AI systems team.

Exports Pattern-A-style ``WORKFLOWS``/``ACTIVITIES`` lists (one source of truth
consumed by ``temporal/worker.py`` and available to guard tests). Importing this
package only binds the workflow class and activity functions into those lists — it
has no other side effects (worker startup happens from the service entrypoint), and
the activity/workflow modules keep their heavy imports inside function bodies so the
temporalio sandbox can re-import them safely.
"""

from ai_systems_team.temporal.activities import (
    architecture_activity,
    begin_run_activity,
    build_phase_activity,
    capabilities_activity,
    evaluation_activity,
    finalize_build_activity,
    run_build_activity,
    safety_activity,
    spec_intake_activity,
)
from ai_systems_team.temporal.client import is_temporal_enabled
from ai_systems_team.temporal.constants import TASK_QUEUE
from ai_systems_team.temporal.workflows import AISystemsBuildWorkflow

WORKFLOWS = [AISystemsBuildWorkflow]
ACTIVITIES = [
    begin_run_activity,
    spec_intake_activity,
    architecture_activity,
    capabilities_activity,
    evaluation_activity,
    safety_activity,
    build_phase_activity,
    finalize_build_activity,
    # Legacy monolith, registered for drain-out of pre-decomposition histories.
    run_build_activity,
]

__all__ = [
    "ACTIVITIES",
    "AISystemsBuildWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "architecture_activity",
    "begin_run_activity",
    "build_phase_activity",
    "capabilities_activity",
    "evaluation_activity",
    "finalize_build_activity",
    "is_temporal_enabled",
    "run_build_activity",
    "safety_activity",
    "spec_intake_activity",
]

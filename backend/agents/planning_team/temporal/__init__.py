"""Temporal workflow + per-phase activities for the Planning team.

Exports the ``WORKFLOWS``/``ACTIVITIES`` contract (the shared "Pattern A" shape
used by the newer teams): the worker bootstrap in :mod:`.worker` registers these
with ``shared_temporal.start_team_worker``, and the sync dispatcher in
:mod:`.start_workflow` starts ``PlanningWorkflow`` via ``start_workflow_sync``.

This package ``__init__`` performs no worker boot (no import-time side effects);
startup is the ``team_service`` entrypoint's job (with the API lifespan as a
standalone-dev backstop). ``TASK_QUEUE``/``WORKFLOW_ID_PREFIX`` live in
:mod:`.constants` and are imported by :mod:`.workflows` under
``imports_passed_through``, so their ``os.getenv`` never runs inside the
temporalio workflow sandbox.
"""

from planning_team.temporal.activities import (
    discovery_activity,
    document_production_activity,
    finalize_planning_activity,
    intake_activity,
    market_research_activity,
    requirements_activity,
    run_planning_activity,
    sub_agent_provisioning_activity,
    synthesis_activity,
)
from planning_team.temporal.client import is_temporal_enabled
from planning_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from planning_team.temporal.workflows import PlanningWorkflow

WORKFLOWS = [PlanningWorkflow]
ACTIVITIES = [
    intake_activity,
    discovery_activity,
    requirements_activity,
    market_research_activity,
    synthesis_activity,
    document_production_activity,
    sub_agent_provisioning_activity,
    finalize_planning_activity,
    # Legacy single-activity path, registered so pre-migration PlanningWorkflow
    # histories can still execute during a rollout (see PlanningWorkflow.run).
    run_planning_activity,
]

__all__ = [
    "ACTIVITIES",
    "PlanningWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "discovery_activity",
    "document_production_activity",
    "finalize_planning_activity",
    "intake_activity",
    "is_temporal_enabled",
    "market_research_activity",
    "requirements_activity",
    "run_planning_activity",
    "sub_agent_provisioning_activity",
    "synthesis_activity",
]

"""Temporal export contract for the personal assistant team (Pattern A).

Pattern A: a team exports a pure, side-effect-free ``WORKFLOWS``/``ACTIVITIES``
data contract from ``<team>/temporal/__init__.py``, and a separate explicit
call (``shared.temporal.start_team_worker``, invoked from this team's own
``worker.py`` boot hook or from the registry's bulk ``start_all_team_workers``)
is what actually starts the worker — importing this package alone never does.
See ``backend/shared/temporal/README.md``'s "See also" section for the
contrast with Pattern B (``shared.postgres``'s explicit lifespan-call
registration, used instead for synchronous blocking I/O like DDL).

Exposes ``WORKFLOWS`` / ``ACTIVITIES`` / ``TASK_QUEUE`` so the shared worker
registry (``shared.temporal.teams_registry``) and the team's worker bootstrap
can register the workflow + activities on the team task queue.

This module must stay free of import-time side effects (no worker boot, no
``os.getenv``) — the temporalio sandbox replays it during workflow
registration. Activities are imported before the workflow so the workflow's
passed-through ``import ... activities`` resolves against an already-loaded
submodule.
"""

from __future__ import annotations

from personal_assistant_team.temporal.activities import (
    check_profile_updates_activity,
    classify_intent_activity,
    fail_job_activity,
    finalize_success_activity,
    generate_response_activity,
    handle_calendar_activity,
    handle_deals_activity,
    handle_documentation_activity,
    handle_email_activity,
    handle_general_activity,
    handle_profile_activity,
    handle_reservations_activity,
    handle_tasks_activity,
    run_assistant_activity,
)
from personal_assistant_team.temporal.constants import (
    MAX_CONCURRENT_ACTIVITIES,
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX_ASSISTANT,
)
from personal_assistant_team.temporal.workflows import PaAssistantWorkflow

WORKFLOWS = [PaAssistantWorkflow]

ACTIVITIES = [
    classify_intent_activity,
    handle_email_activity,
    handle_calendar_activity,
    handle_tasks_activity,
    handle_deals_activity,
    handle_reservations_activity,
    handle_documentation_activity,
    handle_profile_activity,
    handle_general_activity,
    check_profile_updates_activity,
    generate_response_activity,
    finalize_success_activity,
    fail_job_activity,
    # Legacy single activity, kept registered so pre-decomposition workflow
    # executions can replay/drain (gated by workflow.patched in the workflow).
    run_assistant_activity,
]

WORKFLOW_ID_PREFIX = WORKFLOW_ID_PREFIX_ASSISTANT

__all__ = [
    "ACTIVITIES",
    "MAX_CONCURRENT_ACTIVITIES",
    "PaAssistantWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "WORKFLOW_ID_PREFIX_ASSISTANT",
]

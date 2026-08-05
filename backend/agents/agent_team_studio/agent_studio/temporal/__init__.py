"""Temporal workflows + activities for the Agent Studio team.

Agent Studio is Temporal-only: each of its four operations
(start-conversation, send-message, clone-from-registry, save-agent) runs as a
``@workflow.defn`` workflow that executes a single ``@activity.defn`` activity, and
the activity delegates to the existing :class:`~agent_team_studio.agent_studio.service.AgentStudioService`
(no duplicated business logic). There is no non-Temporal fallback.

The workflow class and activities live in :mod:`workflows` (sandbox-safe — no
top-level non-deterministic calls). Worker startup lives in :mod:`worker` and is
invoked by the unified-API lifespan (Agent Studio is an in-process team, so its
worker runs inside the unified-API process). This package ``__init__`` must stay
free of import-time side effects (no worker boot, no ``os.getenv``) — the temporalio
sandbox replays it during workflow registration, and a top-level ``os.getenv`` would
trip it. That is why ``TASK_QUEUE`` below is a plain literal, not read from the env.
"""

from __future__ import annotations

from agent_team_studio.agent_studio.temporal.workflows import (
    CloneFromRegistryWorkflow,
    SaveAgentWorkflow,
    SendMessageWorkflow,
    StartConversationWorkflow,
    clone_from_registry_activity,
    save_agent_activity,
    send_message_activity,
    start_conversation_activity,
)

WORKFLOWS = [
    StartConversationWorkflow,
    SendMessageWorkflow,
    CloneFromRegistryWorkflow,
    SaveAgentWorkflow,
]
ACTIVITIES = [
    start_conversation_activity,
    send_message_activity,
    clone_from_registry_activity,
    save_agent_activity,
]

TASK_QUEUE = "agent-studio-queue"
WORKFLOW_ID_PREFIX_START = "agent-studio-start-"
WORKFLOW_ID_PREFIX_MSG = "agent-studio-msg-"
WORKFLOW_ID_PREFIX_CLONE = "agent-studio-clone-"
WORKFLOW_ID_PREFIX_SAVE = "agent-studio-save-"

__all__ = [
    "ACTIVITIES",
    "CloneFromRegistryWorkflow",
    "SaveAgentWorkflow",
    "SendMessageWorkflow",
    "StartConversationWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX_CLONE",
    "WORKFLOW_ID_PREFIX_MSG",
    "WORKFLOW_ID_PREFIX_SAVE",
    "WORKFLOW_ID_PREFIX_START",
    "clone_from_registry_activity",
    "save_agent_activity",
    "send_message_activity",
    "start_conversation_activity",
]

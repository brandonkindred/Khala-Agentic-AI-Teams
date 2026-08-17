"""Temporal package for the Agent Studio team.

Authoring CRUD (start conversation, send message, clone, save) runs in-process via
:mod:`agent_platform.studio.temporal.dispatch` and does **not** use Temporal.
The former 1-activity authoring workflows have been removed, so this package
exports empty ``WORKFLOWS`` / ``ACTIVITIES`` lists. The unified-API lifespan may
still call :func:`agent_platform.studio.temporal.worker.start_agent_studio_temporal_worker_thread`;
that entrypoint no-ops when there is nothing to register.

This package ``__init__`` must stay free of import-time side effects (no worker
boot, no ``os.getenv``). ``TASK_QUEUE`` is a plain literal for that reason.
"""

from __future__ import annotations

WORKFLOWS: list[object] = []
ACTIVITIES: list[object] = []

TASK_QUEUE = "agent-studio-queue"

__all__ = [
    "ACTIVITIES",
    "TASK_QUEUE",
    "WORKFLOWS",
]

"""
Agent Sandbox — warm-reuse lifecycle for per-team Docker sandboxes used by the
Agent Console Runner.

Talks to ``docker compose -f docker/sandbox.compose.yml`` to spin up team
services in isolation from production, keep them warm across invocations,
and tear them down after ``SANDBOX_IDLE_TEARDOWN_MINUTES`` of inactivity
(default: 15).

Usage from the unified API::

    from agent_sandbox import get_manager

    mgr = get_manager()
    handle = await mgr.ensure_warm("blogging")
    # handle.url is http://localhost:8200
"""

from .manager import SandboxManager, get_manager
from .models import SandboxHandle, SandboxState, SandboxStatus

__all__ = [
    "SandboxHandle",
    "SandboxManager",
    "SandboxState",
    "SandboxStatus",
    "get_manager",
]

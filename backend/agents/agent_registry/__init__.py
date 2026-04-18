"""
Agent Registry — discovery substrate for the Agent Console.

Loads declarative per-agent manifests from
``backend/agents/<team>/agent_console/manifests/*.yaml`` and exposes them as
structured metadata. Read-only; no Postgres, no Temporal, no LLM.

Used by ``backend/unified_api/routes/agents.py`` to serve ``/api/agents``.
Later phases will consume the ``invoke`` and ``sandbox`` blocks to run
agents in isolation via warm provisioned sandboxes.
"""

from .loader import AgentRegistry, get_registry
from .models import (
    AgentDetail,
    AgentManifest,
    AgentSummary,
    InvokeSpec,
    IOSchema,
    SandboxSpec,
    SourceInfo,
    TeamGroup,
)

__all__ = [
    "AgentDetail",
    "AgentManifest",
    "AgentRegistry",
    "AgentSummary",
    "InvokeSpec",
    "IOSchema",
    "SandboxSpec",
    "SourceInfo",
    "TeamGroup",
    "get_registry",
]

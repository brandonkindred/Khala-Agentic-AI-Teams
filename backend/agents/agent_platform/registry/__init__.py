"""
Agent Registry — discovery substrate for the Agent Console.

Loads declarative per-agent manifests from
``backend/agents/<team>/agent_console/manifests/*.yaml`` and exposes them as
structured metadata. Read-only; no Postgres, no Temporal, no LLM.

Used by ``backend/unified_api/routes/agents.py`` to serve ``/api/agents``.
Later phases consume the ``invoke``, ``sandbox``, and ``cognition`` blocks to
run agents in isolation and give them durable memory + rules.
"""

from .loader import AgentRegistry, get_registry
from .models import (
    AgentDetail,
    AgentManifest,
    AgentStateSpec,
    AgentSummary,
    CognitionKnowledgeGraphSpec,
    CognitionMemorySpec,
    CognitionSpec,
    InvokeSpec,
    IOSchema,
    SandboxSpec,
    SourceInfo,
    TeamGroup,
)

__all__ = [
    "AgentDetail",
    "AgentManifest",
    "AgentStateSpec",
    "AgentRegistry",
    "AgentSummary",
    "CognitionKnowledgeGraphSpec",
    "CognitionMemorySpec",
    "CognitionSpec",
    "InvokeSpec",
    "IOSchema",
    "SandboxSpec",
    "SourceInfo",
    "TeamGroup",
    "get_registry",
]

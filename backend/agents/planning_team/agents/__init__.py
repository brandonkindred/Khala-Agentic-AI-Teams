"""Anatomy-conformant persona-agent layer for the Planning team.

Each subpackage (``intake``, ``discovery``, ``requirements``, ``synthesis``,
``document_production``, ``sub_agent_provisioning``) holds the real phase logic
behind a stateless agent class with typed Input/Output models, mirroring
``AGENT_ANATOMY.md`` (§1 typed I/O, §2 coordinator, §3 tools, §5 prompt split,
§6 code-level guardrails).

The ``planning_team.phases.*`` modules are thin backward-compatible adapters
over these agents; ``planning_team.orchestrator`` remains the §2 coordinator.
"""

from __future__ import annotations

from planning_team.agents.discovery import DiscoveryAgent
from planning_team.agents.document_production import DocumentProductionAgent
from planning_team.agents.intake import IntakeAgent
from planning_team.agents.requirements import RequirementsAgent
from planning_team.agents.sub_agent_provisioning import SubAgentProvisioningAgent
from planning_team.agents.synthesis import SynthesisAgent

__all__ = [
    "IntakeAgent",
    "DiscoveryAgent",
    "RequirementsAgent",
    "SynthesisAgent",
    "DocumentProductionAgent",
    "SubAgentProvisioningAgent",
]

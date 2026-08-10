"""Single source for the generated-agent runtime-binding contract.

Every manifest that points at the shared generated-agent invoke shim —
whether built for a provisioned team's roster
(``agent_team_studio.agentic_team_provisioning.manifest_generation``) or for
an agent saved from the Agent Studio UI
(``agent_team_studio.agent_studio.registration``) — must bind to the same
entrypoint, invoke schemas, and anatomy doc.

This module lives directly under ``agent_team_studio`` — a neutral, sibling-
level home owned by neither authoring surface — so importing it never creates
a dependency of one surface's package on the other's.
"""

from __future__ import annotations

GENERATED_AGENT_ENTRYPOINT = (
    "agent_team_studio.agentic_team_provisioning.runtime.agent_builder:invoke_generated_agent"
)
GENERATED_AGENT_INPUT_REF = (
    "agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeInput"
)
GENERATED_AGENT_OUTPUT_REF = (
    "agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeOutput"
)
GENERATED_AGENT_ANATOMY_REF = (
    "backend/agents/agent_team_studio/agent_provisioning_team/AGENT_ANATOMY.md"
)

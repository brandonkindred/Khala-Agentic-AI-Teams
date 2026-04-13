"""Agentic team provisioning graph.

Topology::

    conversation_design → roster_validation → provision → deploy

The conversation designer interacts with users to define team structure.
Roster validation ensures the team composition is viable. Provision
creates the team infrastructure. Deploy activates the team.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def build_provisioning_graph() -> Graph:
    """Build the agentic team provisioning graph."""
    return build_sequential(
        stages=[
            ("conversation_design", build_agent(
                name="team_designer",
                system_prompt=(
                    "You are a team design specialist. Through conversation, help the user "
                    "define their agentic team: team purpose, agent roles, skills needed, "
                    "process workflows, and communication patterns. "
                    "Return JSON with: team_definition including agents, processes, triggers."
                ),
                description="Designs team structure through conversation",
            )),
            ("roster_validation", build_agent(
                name="roster_validator",
                system_prompt=(
                    "You are a team composition validator. Verify the team design:\n"
                    "1. All required roles are filled\n"
                    "2. Skills cover the team's objectives\n"
                    "3. Process workflows are complete (no dead ends)\n"
                    "4. Communication patterns are viable\n\n"
                    "Return JSON with: valid (bool), issues array, suggestions."
                ),
                description="Validates team composition and workflows",
            )),
            ("provision", build_agent(
                name="team_provisioner",
                system_prompt=(
                    "You are a team infrastructure provisioner. Create the team:\n"
                    "1. Set up storage and database\n"
                    "2. Configure agent instances\n"
                    "3. Register process definitions\n"
                    "4. Set up event triggers\n\n"
                    "Return JSON with: team_id, provisioning_status, created_resources."
                ),
                description="Provisions team infrastructure",
            )),
            ("deploy", build_agent(
                name="team_deployer",
                system_prompt=(
                    "You are a team deployment specialist. Activate the provisioned team:\n"
                    "1. Start agent processes\n"
                    "2. Verify health checks\n"
                    "3. Run smoke tests\n"
                    "4. Enable event listeners\n\n"
                    "Return JSON with: deployed (bool), health_status, endpoints."
                ),
                description="Deploys and activates the team",
            )),
        ],
        graph_id="agentic_team_provisioning",
        execution_timeout=600.0,
        node_timeout=180.0,
    )

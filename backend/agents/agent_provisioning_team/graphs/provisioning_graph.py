"""Agent provisioning sequential graph.

Six-phase pipeline: setup → credential_generation → account_provisioning
→ access_audit → documentation → deliver.

Phases are procedural (tool-based provisioning, not LLM agents), so this
graph defines the structure for consistent orchestration patterns.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def build_provisioning_graph() -> Graph:
    """Build the agent provisioning sequential graph."""
    return build_sequential(
        stages=[
            ("setup", build_agent(
                name="provisioning_setup",
                system_prompt="You coordinate agent environment setup: Docker, networking, storage.",
                description="Sets up agent environment",
            )),
            ("credential_generation", build_agent(
                name="credential_generator",
                system_prompt="You generate and securely store agent credentials and API keys.",
                description="Generates agent credentials",
            )),
            ("account_provisioning", build_agent(
                name="account_provisioner",
                system_prompt="You provision service accounts and register the agent with required platforms.",
                description="Provisions service accounts",
            )),
            ("access_audit", build_agent(
                name="access_auditor",
                system_prompt="You verify all provisioned access meets security requirements and access tier constraints.",
                description="Audits provisioned access",
            )),
            ("documentation", build_agent(
                name="documentation_writer",
                system_prompt="You generate provisioning documentation: manifest, access summary, recovery procedures.",
                description="Generates provisioning docs",
            )),
            ("deliver", build_agent(
                name="delivery_agent",
                system_prompt="You finalize provisioning: notify stakeholders, activate agent, update registry.",
                description="Finalizes and delivers provisioned agent",
            )),
        ],
        graph_id="agent_provisioning",
        execution_timeout=600.0,
        node_timeout=120.0,
    )

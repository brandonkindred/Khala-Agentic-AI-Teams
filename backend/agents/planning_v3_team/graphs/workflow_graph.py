"""Planning V3 workflow graph — sequential 6-phase pipeline.

Topology::

    intake → discovery → requirements → synthesis → document_production → sub_agent_provisioning

The discovery and requirements phases use LLM agents; other phases use
adapters to external services (PRA, Planning V2, Market Research, AI Systems).
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def build_workflow_graph() -> Graph:
    """Build the Planning V3 sequential workflow graph.

    Returns
    -------
    Graph
        Six-phase sequential pipeline.
    """
    return build_sequential(
        stages=[
            ("intake", build_agent(
                name="planning_intake",
                system_prompt=(
                    "You are a project intake specialist. Analyze the repository and client brief "
                    "to extract project context, client needs, and initial requirements. "
                    "Return structured JSON with project_name, client_context, and initial_requirements."
                ),
                description="Extracts project context from repo and brief",
            )),
            ("discovery", build_agent(
                name="planning_discovery",
                system_prompt=(
                    "You are a product discovery specialist. Analyze the project context from intake "
                    "and refine understanding of user needs, market context, and product opportunities. "
                    "Return structured JSON with discovery_findings, user_needs, and market_context."
                ),
                description="Refines understanding through discovery analysis",
            )),
            ("requirements", build_agent(
                name="planning_requirements",
                system_prompt=(
                    "You are a requirements engineer. Analyze discovery findings and produce "
                    "detailed functional and non-functional requirements. "
                    "Return structured JSON with functional_requirements, non_functional_requirements, and priorities."
                ),
                description="Produces detailed requirements from discovery",
            )),
            ("synthesis", build_agent(
                name="planning_synthesis",
                system_prompt=(
                    "You are a product strategy synthesizer. Combine all upstream analysis into "
                    "a coherent product strategy with clear scope, priorities, and trade-offs. "
                    "Return structured JSON with strategy_summary, scope, and trade_offs."
                ),
                description="Synthesizes findings into product strategy",
            )),
            ("document_production", build_agent(
                name="planning_document_production",
                system_prompt=(
                    "You are a technical document producer. Create a comprehensive handoff package "
                    "from the synthesized strategy including PRD, architecture guidance, and "
                    "implementation recommendations. Return structured JSON with handoff_package."
                ),
                description="Produces handoff package and PRD",
            )),
            ("sub_agent_provisioning", build_agent(
                name="planning_sub_agent_provisioning",
                system_prompt=(
                    "You are an agent provisioning specialist. Based on the handoff package, "
                    "determine if sub-agent capabilities are needed and provision them. "
                    "Return structured JSON with provisioning_result and sub_agent_blueprint."
                ),
                description="Provisions sub-agents for capability gaps",
            )),
        ],
        graph_id="planning_v3_workflow",
        execution_timeout=900.0,
        node_timeout=180.0,
    )

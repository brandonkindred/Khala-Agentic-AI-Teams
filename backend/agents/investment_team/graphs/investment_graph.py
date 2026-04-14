"""Investment team workflow graph with conditional policy gates.

Topology::

    research → proposal_check ──┬──▶ promotion_decision (approve)
                                └──▶ revision (reject → back to research)

The policy guardian acts as a veto gate with conditional routing.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from shared_graph import build_agent


def build_investment_graph() -> Graph:
    """Build the investment workflow graph with policy gates."""
    builder = GraphBuilder()
    builder.set_graph_id("investment_workflow")
    builder.set_execution_timeout(600.0)
    builder.set_node_timeout(180.0)

    research = builder.add_node(
        build_agent(
            name="investment_researcher",
            system_prompt=(
                "You are an investment research analyst. Analyze market conditions, "
                "asset classes, and risk factors to produce a research brief for "
                "portfolio design. Return structured JSON with findings and recommendations."
            ),
            description="Conducts investment research and analysis",
        ),
        node_id="research",
    )
    builder.set_entry_point("research")

    portfolio_design = builder.add_node(
        build_agent(
            name="portfolio_designer",
            system_prompt=(
                "You are a portfolio construction specialist. Based on the research brief "
                "and IPS constraints, design a portfolio proposal with asset allocation, "
                "risk budget, and rebalancing rules. Return structured JSON."
            ),
            description="Designs portfolio proposals from research",
        ),
        node_id="portfolio_design",
    )
    builder.add_edge(research, portfolio_design)

    policy_check = builder.add_node(
        build_agent(
            name="policy_guardian",
            system_prompt=(
                "You are the investment policy guardian. Review the portfolio proposal against "
                "the Investment Policy Statement (IPS). Identify any violations. "
                "Return JSON with violations array and approved boolean."
            ),
            description="Validates proposals against IPS policy",
        ),
        node_id="policy_check",
    )
    builder.add_edge(portfolio_design, policy_check)

    promotion = builder.add_node(
        build_agent(
            name="promotion_gate",
            system_prompt=(
                "You are the investment promotion gate. Make the final decision on whether "
                "to approve the portfolio for execution based on policy review results. "
                "Return JSON with decision (approve/reject/revise) and rationale."
            ),
            description="Makes final promotion decision",
        ),
        node_id="promotion",
    )
    builder.add_edge(policy_check, promotion)

    return builder.build()

"""Sales pipeline graph — 7-stage sequential with qualification gate.

Topology::

    prospector → outreach → qualifier ──┬──▶ discovery → proposal → negotiator
                                        └──▶ nurturer (nurture path)

The qualification stage gates prospects: "advanced" prospects continue to
discovery, while "nurture" prospects are routed to the nurture agent.
Learning insights from the sales coach are injected into agent system
prompts at graph build time (graph is built per-invocation).

Dynamic fan-out: when multiple prospects exist, the graph fans out per-
prospect within relevant stages. The graph is built at invocation time
with the known prospect count.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from shared_graph import build_agent


def build_pipeline_graph(
    *,
    learning_insights: str = "",
    include_nurture: bool = True,
) -> Graph:
    """Build the sales pipeline graph.

    Parameters
    ----------
    learning_insights:
        Accumulated learning engine insights to inject into agent prompts.
    include_nurture:
        Whether to include the nurture path from qualification.
    """
    insights_ctx = f"\n\nLearning insights from prior campaigns:\n{learning_insights}" if learning_insights else ""

    builder = GraphBuilder()
    builder.set_graph_id("sales_pipeline")
    builder.set_execution_timeout(900.0)
    builder.set_node_timeout(180.0)

    prospector = builder.add_node(
        build_agent(
            name="prospector",
            system_prompt=(
                "You are a sales prospecting specialist. Identify and research potential "
                "prospects from the provided market context. Produce a qualified prospect "
                "list with company details, decision makers, and engagement signals. "
                f"Return structured JSON with prospects array.{insights_ctx}"
            ),
            description="Identifies and researches sales prospects",
        ),
        node_id="prospector",
    )
    builder.set_entry_point("prospector")

    outreach = builder.add_node(
        build_agent(
            name="outreach_specialist",
            system_prompt=(
                "You are a sales outreach specialist. Craft personalized outreach sequences "
                "for each prospect based on their profile, industry, and engagement signals. "
                f"Return structured JSON with outreach plans per prospect.{insights_ctx}"
            ),
            description="Creates personalized outreach sequences",
        ),
        node_id="outreach",
    )
    builder.add_edge(prospector, outreach)

    qualifier = builder.add_node(
        build_agent(
            name="lead_qualifier",
            system_prompt=(
                "You are a lead qualification specialist using BANT/MEDDIC frameworks. "
                "Evaluate each prospect's budget, authority, need, and timeline. "
                "Classify each as 'advanced' (ready for discovery), 'nurture' (needs warming), "
                f"or 'disqualify'. Return structured JSON with classifications.{insights_ctx}"
            ),
            description="Qualifies leads using BANT/MEDDIC",
        ),
        node_id="qualifier",
    )
    builder.add_edge(outreach, qualifier)

    discovery = builder.add_node(
        build_agent(
            name="discovery_specialist",
            system_prompt=(
                "You are a sales discovery specialist. For qualified prospects, conduct deep "
                "needs analysis, map decision processes, and identify key pain points. "
                f"Return structured JSON with discovery findings per prospect.{insights_ctx}"
            ),
            description="Conducts deep needs analysis for qualified prospects",
        ),
        node_id="discovery",
    )
    builder.add_edge(qualifier, discovery)

    proposal = builder.add_node(
        build_agent(
            name="proposal_specialist",
            system_prompt=(
                "You are a proposal specialist. Create tailored proposals for each prospect "
                "based on discovery findings, addressing their specific pain points with "
                f"solution recommendations. Return structured JSON with proposals.{insights_ctx}"
            ),
            description="Creates tailored proposals from discovery",
        ),
        node_id="proposal",
    )
    builder.add_edge(discovery, proposal)

    negotiator = builder.add_node(
        build_agent(
            name="closer",
            system_prompt=(
                "You are a sales negotiation specialist. Develop negotiation strategies and "
                "closing plans for each prospect based on their proposal reactions. "
                f"Return structured JSON with negotiation plans.{insights_ctx}"
            ),
            description="Develops closing strategies",
        ),
        node_id="negotiator",
    )
    builder.add_edge(proposal, negotiator)

    if include_nurture:
        nurturer = builder.add_node(
            build_agent(
                name="nurturer",
                system_prompt=(
                    "You are a lead nurturing specialist. For prospects classified as 'nurture', "
                    "design long-term engagement sequences to build relationships and advance "
                    f"them toward qualification. Return structured JSON.{insights_ctx}"
                ),
                description="Nurtures unqualified leads",
            ),
            node_id="nurturer",
        )
        builder.add_edge(qualifier, nurturer)

    return builder.build()

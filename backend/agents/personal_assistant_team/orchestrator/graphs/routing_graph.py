"""Personal assistant intent routing graph.

Topology::

    intent_classifier ──┬──▶ email_specialist
                        ├──▶ calendar_specialist
                        ├──▶ task_specialist
                        ├──▶ deal_finder
                        ├──▶ reservation_specialist
                        ├──▶ doc_generator
                        └──▶ general_assistant

The intent classifier determines which specialist to route to. For
multi-intent requests, multiple specialists run in parallel via fan-out,
then a response synthesizer merges their outputs.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from shared_graph import build_agent


def build_routing_graph() -> Graph:
    """Build the personal assistant intent routing graph.

    All specialists are connected from the classifier. In practice,
    the graph's conditional routing will activate only the relevant
    specialist(s) based on the classified intent.
    """
    builder = GraphBuilder()
    builder.set_graph_id("personal_assistant_routing")
    builder.set_execution_timeout(300.0)
    builder.set_node_timeout(120.0)

    classifier = builder.add_node(
        build_agent(
            name="intent_classifier",
            system_prompt=(
                "You are an intent classification specialist for a personal assistant. "
                "Analyze the user's message and classify it into one or more intents: "
                "email, calendar, tasks, deals, reservations, documentation, profile, general. "
                "Return JSON with intents array and confidence scores."
            ),
            description="Classifies user intent from message",
        ),
        node_id="classifier",
    )
    builder.set_entry_point("classifier")

    specialists = {
        "email": "You handle email-related tasks: compose, search, summarize, and manage emails.",
        "calendar": "You handle calendar tasks: create events, check availability, schedule meetings.",
        "tasks": "You manage task lists: create, update, prioritize, and track tasks.",
        "deals": "You find deals and shopping offers: search prices, compare products, find coupons.",
        "reservations": "You handle reservations: restaurants, hotels, flights, and activities.",
        "documentation": "You generate documents: reports, summaries, templates, and formatted content.",
        "general": "You handle general assistant queries: Q&A, research, advice, and conversation.",
    }

    for name, prompt in specialists.items():
        node = builder.add_node(
            build_agent(
                name=f"{name}_specialist",
                system_prompt=f"{prompt} Provide a helpful, actionable response.",
                description=f"Handles {name} requests",
            ),
            node_id=name,
        )
        builder.add_edge(classifier, node)

    return builder.build()

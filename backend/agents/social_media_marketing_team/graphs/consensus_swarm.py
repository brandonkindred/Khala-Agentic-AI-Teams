"""Social media marketing consensus swarm.

Collaboration agents reason about campaign quality and decide whether
to refine — matching the Swarm heuristic of reasoning-based handoffs.

Agents:
    campaign_strategist (entry) ←→ creative_director ←→ audience_analyst

The swarm replaces the hand-rolled _reach_consensus() while loop
(MIN 2, MAX 10 rounds, 0.75 consensus threshold).
"""

from __future__ import annotations

from strands.multiagent.swarm import Swarm

from shared_graph import build_agent


def build_consensus_swarm() -> Swarm:
    """Build the consensus collaboration swarm."""
    strategist = build_agent(
        name="campaign_strategist",
        system_prompt=(
            "You are a campaign strategist leading a collaborative team. Propose campaign "
            "concepts, evaluate team feedback, and refine until the team reaches consensus. "
            "Score each concept on: brand_alignment, audience_fit, creative_strength, "
            "measurability. Target consensus score >= 0.75. "
            "Hand off to creative_director for creative evaluation, or to audience_analyst "
            "for audience fit assessment. When consensus is reached, produce the final concept."
        ),
        description="Leads campaign strategy consensus",
    )

    creative = build_agent(
        name="creative_director",
        system_prompt=(
            "You are a creative director evaluating campaign concepts for creative strength, "
            "brand alignment, and visual potential. Score the concept and suggest improvements. "
            "Hand back to campaign_strategist with your assessment and score. "
            "If the concept has strong creative potential, hand to audience_analyst for fit check."
        ),
        description="Evaluates creative quality of concepts",
    )

    audience = build_agent(
        name="audience_analyst",
        system_prompt=(
            "You are an audience analyst evaluating campaign concepts for target audience "
            "resonance, engagement probability, and conversion potential. Score the concept. "
            "Hand back to campaign_strategist with your assessment. "
            "Flag concepts below 0.7 engagement probability threshold for revision."
        ),
        description="Assesses audience fit and engagement probability",
    )

    return Swarm(
        nodes=[strategist, creative, audience],
        entry_point=strategist,
        max_handoffs=10,
        execution_timeout=300.0,
    )

"""Strategy Lab ideation swarm for collaborative strategy refinement.

The ideation cycle uses a Swarm where agents reason about whether the
strategy needs further iteration:

    ideation_agent ←→ refinement_agent ←→ analysis_agent

The swarm allows agents to hand back upstream when quality gates
identify issues, enabling reasoning-based refinement cycles.

Note: The actual refinement loop in the Strategy Lab orchestrator uses
deterministic quality gates that MUST NOT be skippable. This swarm is
for the LLM-driven creative collaboration between ideation and
analysis agents, not for replacing the mandatory validation pipeline.
"""

from __future__ import annotations

from strands.multiagent.swarm import Swarm

from shared_graph import build_agent


def build_ideation_swarm() -> Swarm:
    """Build the Strategy Lab ideation swarm.

    Returns
    -------
    Swarm
        Collaborative swarm for strategy ideation and refinement.
    """
    ideation = build_agent(
        name="strategy_ideator",
        system_prompt=(
            "You are a quantitative strategy ideation specialist. Generate novel trading "
            "strategies with clear hypotheses, signal definitions, and entry/exit rules. "
            "Consider prior strategy performance and convergence directives. "
            "When the refinement agent suggests improvements, incorporate them. "
            "Return structured JSON with strategy specification and Python backtest code."
        ),
        description="Generates novel trading strategies",
    )

    refinement = build_agent(
        name="strategy_refiner",
        system_prompt=(
            "You are a strategy refinement specialist. Review strategy specifications and "
            "backtest code for issues. Suggest targeted fixes for validation failures, "
            "execution errors, or anomalous results. Hand back to ideation if the strategy "
            "needs fundamental redesign, or to analysis if the strategy is ready for evaluation. "
            "Return structured JSON with refinement updates and new code."
        ),
        description="Refines strategies based on failure feedback",
    )

    analysis = build_agent(
        name="strategy_analyst",
        system_prompt=(
            "You are a post-backtest analysis specialist. Evaluate strategy performance "
            "metrics (return, Sharpe, drawdown, win rate) and generate a clear narrative. "
            "Identify strengths, weaknesses, and potential improvements. "
            "Return a narrative analysis string."
        ),
        description="Analyzes backtest results and generates narrative",
    )

    return Swarm(
        nodes=[ideation, refinement, analysis],
        entry_point=ideation,
        max_handoffs=10,
        execution_timeout=300.0,
    )

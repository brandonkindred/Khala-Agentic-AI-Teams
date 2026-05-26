"""Strategy Lab design swarm for collaborative strategy refinement.

The design cycle uses a Swarm where agents reason about whether the
strategy needs further iteration:

    design_agent ←→ design_review_agent ←→ analysis_agent

The swarm allows agents to hand back upstream when quality gates
identify issues, enabling reasoning-based refinement cycles.

Note: The actual refinement loop in the Strategy Lab orchestrator uses
deterministic quality gates that MUST NOT be skippable. This swarm is
for the LLM-driven creative collaboration between design and review
agents, not for replacing the mandatory validation pipeline.

The filename remains ``ideation_swarm.py`` for backward-compat; the
internal node names mirror the orchestrator's split-design pipeline.
"""

from __future__ import annotations

from strands.multiagent.swarm import Swarm

from shared_graph import build_agent


def build_ideation_swarm() -> Swarm:
    """Build the Strategy Lab design swarm.

    Returns
    -------
    Swarm
        Collaborative swarm for strategy design and review.
    """
    design = build_agent(
        name="strategy_designer",
        system_prompt=(
            "You are a quantitative strategy design specialist. Author novel trading "
            "strategy specifications with clear hypotheses, signal definitions, and "
            "structured entry/exit rules. Consider prior strategy performance and "
            "convergence directives. When the reviewer suggests revisions, incorporate "
            "them. Return structured JSON with the strategy specification only — no "
            "Python code."
        ),
        description="Authors novel trading strategy specifications (spec only, no code)",
    )

    review = build_agent(
        name="strategy_reviewer",
        system_prompt=(
            "You are a strategy review specialist. Inspect candidate strategy "
            "specifications for thesis coherence, signal alignment, risk-control "
            "completeness, and universe ↔ thesis fit. Do not propose code. Do not "
            "rewrite the spec. Emit a structured JSON critique the designer can act "
            "on, with ``ready`` true only when the spec is implementable as-is."
        ),
        description="Reviews strategy specs and emits actionable critiques",
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
        nodes=[design, review, analysis],
        entry_point=design,
        max_handoffs=10,
        execution_timeout=300.0,
    )

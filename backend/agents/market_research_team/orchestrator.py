"""Orchestrator for market research and concept viability analysis.

Uses a Strands Graph for parallel agent execution:
- Split mode: UX research → [psychology, consistency] → viability synthesis
- Unified mode: UX research → psychology → viability synthesis
- Scripts agent runs independently in parallel
"""

from __future__ import annotations

import logging
from typing import List

from shared_graph import extract_node_text, invoke_graph_sync

from .agents import (
    TranscriptIngestionAgent,
    _ensure_list,
    _parse_json,
    _safe_float,
)
from .graphs.research_graph import build_research_graph
from .models import (
    HumanReview,
    InterviewInsight,
    MarketSignal,
    ResearchMission,
    TeamOutput,
    TeamTopology,
    ViabilityRecommendation,
    WorkflowStatus,
)
from .prompts import CONSISTENCY_SYSTEM_PROMPT  # noqa: F401 — re-exported for backward compat

logger = logging.getLogger(__name__)

_DEFAULT_SIGNALS_FALLBACK = [
    MarketSignal(
        signal="User pain urgency",
        confidence=0.5,
        evidence=["No direct pain statements yet; run discovery interviews."],
    ),
    MarketSignal(
        signal="Adoption motivation clarity",
        confidence=0.5,
        evidence=["No clear desired outcomes captured yet."],
    ),
]

_DEFAULT_SCRIPTS_FALLBACK = [
    (
        "Interview script:\n"
        "1) Tell me about your current workflow.\n"
        "2) What is hardest or most frustrating today?\n"
        "3) What have you already tried and why did it fail?\n"
        "4) If this problem disappeared tomorrow, what outcome would change?"
    ),
    (
        "Transcript tagging guide:\n"
        "- Label each statement as job_to_be_done, pain_point, desired_outcome, workaround, or trigger_event.\n"
        "- Track frequency count for repeated themes across interviews."
    ),
    (
        "Decision checkpoint template:\n"
        "- What evidence improved confidence?\n"
        "- What assumptions remain unproven?\n"
        "- What experiment is approved for next sprint?"
    ),
]


def _parse_insights_from_text(text: str) -> List[InterviewInsight]:
    """Parse UX research agent output into InterviewInsight list."""
    data = _parse_json(text, {})
    if isinstance(data, dict):
        # Single insight or nested structure
        return [InterviewInsight(
            source="graph_analysis",
            user_jobs=_ensure_list(data.get("user_jobs"), []),
            pain_points=_ensure_list(data.get("pain_points"), []),
            desired_outcomes=_ensure_list(data.get("desired_outcomes"), []),
            direct_quotes=_ensure_list(data.get("direct_quotes"), []),
        )]
    if isinstance(data, list):
        insights = []
        for item in data:
            if isinstance(item, dict):
                insights.append(InterviewInsight(
                    source=item.get("source", "graph_analysis"),
                    user_jobs=_ensure_list(item.get("user_jobs"), []),
                    pain_points=_ensure_list(item.get("pain_points"), []),
                    desired_outcomes=_ensure_list(item.get("desired_outcomes"), []),
                    direct_quotes=_ensure_list(item.get("direct_quotes"), []),
                ))
        return insights
    return []


def _signal_name(raw: object) -> str:
    """Coerce a signal name, defaulting to a sensible label when None or non-string."""
    return str(raw) if isinstance(raw, str) else "Cross-interview theme consistency"


def _parse_signals_from_text(text: str) -> List[MarketSignal]:
    """Parse psychology/consistency agent output into MarketSignal list."""
    data = _parse_json(text, [])
    if isinstance(data, dict):
        return [MarketSignal(
            signal=_signal_name(data.get("signal")),
            confidence=min(1.0, max(0.0, _safe_float(data.get("confidence"), 0.5))),
            evidence=_ensure_list(data.get("evidence"), []),
        )]
    if isinstance(data, list):
        signals = []
        for item in data:
            if isinstance(item, dict):
                signals.append(MarketSignal(
                    signal=_signal_name(item.get("signal")),
                    confidence=min(1.0, max(0.0, _safe_float(item.get("confidence"), 0.5))),
                    evidence=_ensure_list(item.get("evidence"), []),
                ))
        return signals
    return []


def _parse_viability_from_text(text: str, mission: ResearchMission) -> ViabilityRecommendation:
    """Parse viability synthesis output into ViabilityRecommendation."""
    data = _parse_json(text, {})
    if not isinstance(data, dict):
        data = {}

    valid_verdicts = {"insufficient_evidence", "needs_more_validation", "promising_with_risks"}
    verdict = str(data.get("verdict", "needs_more_validation"))
    if verdict not in valid_verdicts:
        verdict = "needs_more_validation"

    return ViabilityRecommendation(
        verdict=verdict,
        confidence=min(1.0, max(0.0, _safe_float(data.get("confidence"), 0.5))),
        rationale=_ensure_list(data.get("rationale"), [f"Mission concept: {mission.product_concept}."]),
        suggested_next_experiments=_ensure_list(
            data.get("suggested_next_experiments"),
            ["Run a concierge MVP with 3-5 target users for one core workflow."],
        ),
    )


def _parse_scripts_from_text(text: str) -> List[str]:
    """Parse scripts agent output into a list of strings."""
    data = _parse_json(text, _DEFAULT_SCRIPTS_FALLBACK)
    if isinstance(data, list) and all(isinstance(s, str) for s in data) and len(data) >= 1:
        return data
    return list(_DEFAULT_SCRIPTS_FALLBACK)


class MarketResearchOrchestrator:
    """Coordinates market research workflow via a Strands Graph."""

    def __init__(self) -> None:
        self.ingestion = TranscriptIngestionAgent()

    def run(self, mission: ResearchMission, human_review: HumanReview) -> TeamOutput:
        """Run the market research workflow."""
        # Load transcripts outside the graph (pure data I/O)
        loaded = self.ingestion.load_transcripts(mission)

        is_split = mission.topology == TeamTopology.SPLIT

        # Build task with mission context and transcript data
        transcript_text = ""
        for source, text in loaded:
            transcript_text += f"\n--- {source} ---\n{text}\n"

        task = (
            f"Analyze the following market research data.\n\n"
            f"Product concept: {mission.product_concept}\n"
            f"Target users: {mission.target_users}\n"
            f"Business goal: {mission.business_goal}\n"
            f"Number of interviews: {len(loaded)}\n\n"
            f"Transcripts:{transcript_text if transcript_text else ' (none provided)'}"
        )

        # Build and invoke the graph
        graph = build_research_graph(include_consistency=is_split)
        result = invoke_graph_sync(graph, task)

        # Extract results from graph nodes
        ux_text = extract_node_text(result, "ux_research")
        insights = _parse_insights_from_text(ux_text) if ux_text else []

        psych_text = extract_node_text(result, "psychology")
        market_signals = _parse_signals_from_text(psych_text) if psych_text else []

        if is_split:
            if not loaded:
                # No transcripts → deterministic fallback (no LLM call needed)
                market_signals.append(MarketSignal(
                    signal="Cross-interview theme consistency",
                    confidence=0.55,
                    evidence=[
                        "Insufficient transcript volume for consistency scoring; collect 5+ interviews."
                    ],
                ))
            else:
                consistency_text = extract_node_text(result, "consistency")
                consistency_signals = _parse_signals_from_text(consistency_text) if consistency_text else []
                market_signals.extend(consistency_signals)

        # Ensure minimum signals
        while len(market_signals) < 2:
            market_signals.append(_DEFAULT_SIGNALS_FALLBACK[len(market_signals)])

        viability_text = extract_node_text(result, "viability_synthesis")
        recommendation = _parse_viability_from_text(viability_text, mission) if viability_text else ViabilityRecommendation(
            verdict="insufficient_evidence",
            confidence=0.3,
            rationale=["Graph execution produced no viability output."],
            suggested_next_experiments=["Re-run analysis with transcript data."],
        )

        scripts_text = extract_node_text(result, "scripts")
        scripts = _parse_scripts_from_text(scripts_text) if scripts_text else list(_DEFAULT_SCRIPTS_FALLBACK)

        if not human_review.approved:
            return TeamOutput(
                status=WorkflowStatus.NEEDS_HUMAN_DECISION,
                topology=mission.topology,
                mission_summary=(
                    "AI completed heavy-lifting analysis. Awaiting human strategic decision "
                    "before execution of experiments."
                ),
                insights=insights,
                market_signals=market_signals,
                recommendation=recommendation,
                proposed_research_scripts=scripts,
                human_feedback=human_review.feedback
                or "Please review findings and approve next experiment.",
            )

        return TeamOutput(
            status=WorkflowStatus.READY_FOR_EXECUTION,
            topology=mission.topology,
            mission_summary=(
                "Human approved strategic direction. Team prepared prioritized experiments "
                "and scripts for next sprint."
            ),
            insights=insights,
            market_signals=market_signals,
            recommendation=recommendation,
            proposed_research_scripts=scripts,
            human_feedback=human_review.feedback or "Approved.",
        )

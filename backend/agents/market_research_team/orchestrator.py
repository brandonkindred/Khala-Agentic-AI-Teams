"""Orchestrator for market research and concept viability analysis."""

from __future__ import annotations

from collections import Counter

from .agents import (
    MarketViabilityAgent,
    ResearchScriptAgent,
    TranscriptIngestionAgent,
    UserPsychologyAgent,
    UXResearchAgent,
)
from .models import (
    HumanReview,
    MarketSignal,
    ResearchMission,
    TeamOutput,
    TeamTopology,
    WorkflowStatus,
)


class MarketResearchOrchestrator:
    """Coordinates either a unified or split research workflow."""

    def __init__(self) -> None:
        self.ingestion = TranscriptIngestionAgent()
        self.ux_research = UXResearchAgent()
        self.psychology = UserPsychologyAgent()
        self.viability = MarketViabilityAgent()
        self.script_builder = ResearchScriptAgent()

    @staticmethod
    def _cross_interview_consistency_signal() -> MarketSignal:
        return MarketSignal(
            signal="Cross-interview theme consistency",
            confidence=0.55,
            evidence=["Insufficient transcript volume for consistency scoring; collect 5+ interviews."],
        )

    def _run_split_signals(self, mission: ResearchMission) -> tuple[list, list[MarketSignal]]:
        loaded = self.ingestion.load_transcripts(mission)
        insights = [self.ux_research.analyze(source, text) for source, text in loaded]

        base_signals = self.psychology.derive_signals(insights)
        if not insights:
            return insights, [*base_signals, self._cross_interview_consistency_signal()]

        theme_counter: Counter[str] = Counter()
        for insight in insights:
            theme_counter.update(insight.pain_points)

        repeated = [theme for theme, count in theme_counter.items() if count > 1]
        confidence = min(1.0, 0.55 + (len(repeated) * 0.1))
        evidence = repeated[:4] or ["No repeated pains found yet; gather more interviews for consistency checks."]

        consistency = MarketSignal(
            signal="Cross-interview theme consistency",
            confidence=confidence,
            evidence=evidence,
        )
        return insights, [*base_signals, consistency]

    def _run_unified_signals(self, mission: ResearchMission) -> tuple[list, list[MarketSignal]]:
        loaded = self.ingestion.load_transcripts(mission)
        insights = [self.ux_research.analyze(source, text) for source, text in loaded]
        return insights, self.psychology.derive_signals(insights)

    def run(self, mission: ResearchMission, human_review: HumanReview) -> TeamOutput:
        if mission.topology == TeamTopology.SPLIT:
            # Split-mode: explicit phase handoff (Discovery -> Viability), includes consistency scoring.
            insights, market_signals = self._run_split_signals(mission)
        else:
            # Unified-mode: one-pass synthesis for speed.
            insights, market_signals = self._run_unified_signals(mission)

        recommendation = self.viability.recommend(mission, market_signals, len(insights))
        scripts = self.script_builder.build_scripts(mission)

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
                human_feedback=human_review.feedback or "Please review findings and approve next experiment.",
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

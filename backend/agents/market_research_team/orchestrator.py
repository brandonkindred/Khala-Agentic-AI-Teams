"""Orchestrator for market research and concept viability analysis."""

from __future__ import annotations

import json
import logging
from typing import List

from .agents import (
    MarketViabilityAgent,
    ResearchScriptAgent,
    TranscriptIngestionAgent,
    UserPsychologyAgent,
    UXResearchAgent,
    _build_strands_agent,
    _call_agent,
    _parse_json,
)
from .models import (
    HumanReview,
    InterviewInsight,
    MarketSignal,
    ResearchMission,
    TeamOutput,
    TeamTopology,
    WorkflowStatus,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Consistency analysis system prompt
# ---------------------------------------------------------------------------

_CONSISTENCY_SYSTEM_PROMPT = """\
You are a Cross-Interview Consistency Analyst. Your job is to identify recurring themes \
across multiple user interviews and assess how consistent the evidence is.

## Your Methodology
- Compare pain points, user jobs, and desired outcomes across all interviews.
- Identify themes that appear in 2+ interviews — these are the strongest signals.
- Assess whether different interviewees describe the same underlying problem in different words \
(semantic similarity, not just exact matches).
- Higher consistency = higher confidence that the problem is real and widespread.

## Confidence Calibration
- 5+ interviews with 3+ repeated themes: confidence 0.8-0.95
- 3-4 interviews with 2+ repeated themes: confidence 0.6-0.8
- 1-2 interviews or few repeated themes: confidence 0.4-0.6
- Contradictory signals across interviews: confidence 0.2-0.4

## Output Format
Return ONLY a valid JSON object (no markdown, no commentary) with these exact keys:
- "signal": always "Cross-interview theme consistency"
- "confidence": float 0.0-1.0
- "evidence": array of strings — the repeated themes or patterns found across interviews
"""


class MarketResearchOrchestrator:
    """Coordinates either a unified or split research workflow."""

    def __init__(self) -> None:
        self.ingestion = TranscriptIngestionAgent()
        self.ux_research = UXResearchAgent()
        self.psychology = UserPsychologyAgent()
        self.viability = MarketViabilityAgent()
        self.script_builder = ResearchScriptAgent()
        self._consistency_agent = _build_strands_agent(_CONSISTENCY_SYSTEM_PROMPT)

    def _cross_interview_consistency_signal(self, insights: List[InterviewInsight]) -> MarketSignal:
        if not insights:
            return MarketSignal(
                signal="Cross-interview theme consistency",
                confidence=0.55,
                evidence=[
                    "Insufficient transcript volume for consistency scoring; collect 5+ interviews."
                ],
            )

        insights_json = json.dumps([i.model_dump() for i in insights], indent=2)
        prompt = (
            f"Analyze the following {len(insights)} interview insights for cross-interview "
            f"theme consistency.\n\n{insights_json}\n\n"
            f"Identify recurring themes and rate the consistency confidence. "
            f"Return ONLY valid JSON."
        )
        raw = _call_agent(self._consistency_agent, prompt)
        data = _parse_json(raw, {})

        if not isinstance(data, dict):
            data = {}

        return MarketSignal(
            signal=data.get("signal", "Cross-interview theme consistency"),
            confidence=min(1.0, max(0.0, float(data.get("confidence", 0.55)))),
            evidence=data.get(
                "evidence",
                ["No repeated pains found yet; gather more interviews for consistency checks."],
            ),
        )

    def _run_split_signals(self, mission: ResearchMission) -> tuple[list, list[MarketSignal]]:
        loaded = self.ingestion.load_transcripts(mission)
        insights = [self.ux_research.analyze(source, text) for source, text in loaded]

        base_signals = self.psychology.derive_signals(insights)
        consistency = self._cross_interview_consistency_signal(insights)
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

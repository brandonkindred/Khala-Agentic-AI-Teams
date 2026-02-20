"""Specialist agent implementations for market research workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from .models import InterviewInsight, MarketSignal, ResearchMission, ViabilityRecommendation


@dataclass
class TranscriptIngestionAgent:
    """Loads transcript text from a mission payload or folder path."""

    def load_transcripts(self, mission: ResearchMission) -> List[tuple[str, str]]:
        loaded: List[tuple[str, str]] = []

        for index, text in enumerate(mission.transcripts, start=1):
            if text.strip():
                loaded.append((f"inline_transcript_{index}", text.strip()))

        if mission.transcript_folder_path:
            folder = Path(mission.transcript_folder_path).expanduser().resolve()
            if folder.is_dir():
                for file_path in sorted(folder.glob("*.txt")):
                    text = file_path.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        loaded.append((file_path.name, text))

        return loaded


@dataclass
class UXResearchAgent:
    """Extracts user jobs and outcomes from transcripts."""

    role: str = "UX Research Lead"

    def analyze(self, source: str, transcript: str) -> InterviewInsight:
        lines = [line.strip(" -•\t") for line in transcript.splitlines() if line.strip()]
        user_jobs = [line for line in lines if "job" in line.lower() or "trying to" in line.lower()][:4]
        pain_points = [line for line in lines if any(k in line.lower() for k in ("pain", "friction", "issue", "problem"))][:4]
        desired_outcomes = [line for line in lines if any(k in line.lower() for k in ("want", "need", "goal", "outcome"))][:4]
        direct_quotes = [line for line in lines if '"' in line][:3]

        if not user_jobs:
            user_jobs = ["Identify the core user job-to-be-done through follow-up interviews."]
        if not pain_points:
            pain_points = ["Validate top workflow frictions from observed user behavior."]
        if not desired_outcomes:
            desired_outcomes = ["Confirm measurable success criteria users care about."]

        return InterviewInsight(
            source=source,
            user_jobs=user_jobs,
            pain_points=pain_points,
            desired_outcomes=desired_outcomes,
            direct_quotes=direct_quotes,
        )


@dataclass
class UserPsychologyAgent:
    """Derives adoption and behavior-change signals from insights."""

    role: str = "User Psychology Researcher"

    def derive_signals(self, insights: List[InterviewInsight]) -> List[MarketSignal]:
        all_pains = [p for insight in insights for p in insight.pain_points]
        all_outcomes = [o for insight in insights for o in insight.desired_outcomes]

        urgency = 0.55 + min(0.35, len(all_pains) * 0.04)
        willingness = 0.5 + min(0.35, len(all_outcomes) * 0.035)

        return [
            MarketSignal(
                signal="User pain urgency",
                confidence=min(1.0, urgency),
                evidence=all_pains[:4] or ["No direct pain statements yet; run discovery interviews."],
            ),
            MarketSignal(
                signal="Adoption motivation clarity",
                confidence=min(1.0, willingness),
                evidence=all_outcomes[:4] or ["No clear desired outcomes captured yet."],
            ),
        ]


@dataclass
class MarketViabilityAgent:
    """Generates a viability recommendation and next experiments."""

    role: str = "Business Viability Strategist"

    def recommend(self, mission: ResearchMission, signals: List[MarketSignal], insight_count: int) -> ViabilityRecommendation:
        avg_confidence = sum(signal.confidence for signal in signals) / len(signals) if signals else 0.45

        if insight_count == 0:
            return ViabilityRecommendation(
                verdict="insufficient_evidence",
                confidence=0.3,
                rationale=[
                    "No interview transcript evidence was provided.",
                    "The team should start with 5-8 exploratory interviews in the target segment.",
                ],
                suggested_next_experiments=[
                    "Create interview screener and recruit target users.",
                    "Run 5 problem interviews and tag repeated pains.",
                    "Draft a fake-door landing page and measure sign-up intent.",
                ],
            )

        verdict = "promising_with_risks" if avg_confidence >= 0.65 else "needs_more_validation"
        return ViabilityRecommendation(
            verdict=verdict,
            confidence=round(avg_confidence, 2),
            rationale=[
                f"Mission concept: {mission.product_concept}.",
                f"Target users: {mission.target_users}.",
                f"Signals indicate {'moderate-to-strong' if avg_confidence >= 0.65 else 'early'} demand potential.",
            ],
            suggested_next_experiments=[
                "Run a concierge MVP with 3-5 target users for one core workflow.",
                "Test value proposition copy with a short ad + landing page experiment.",
                "Quantify willingness to pay via pricing sensitivity interviews.",
            ],
        )


@dataclass
class ResearchScriptAgent:
    """Produces interview and data collection scripts for the user."""

    role: str = "Research Operations Specialist"

    def build_scripts(self, mission: ResearchMission) -> List[str]:
        return [
            (
                "Interview script:\n"
                f"1) Tell me about how you currently handle {mission.business_goal}.\n"
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

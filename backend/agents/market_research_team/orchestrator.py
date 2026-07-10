"""Orchestrator for market research and concept viability analysis.

Coordinates role-separated specialist agents (defined in ``agents.py``) through
a **per-stage seam** that is shared by BOTH runtime paths:

- the thread path — :meth:`MarketResearchOrchestrator.run`, which fans the UX
  stage out one call per transcript with ``shared_concurrency.parallel_map``,
  then runs psychology/consistency/viability/scripts;
- the Temporal path — ``temporal/activities.py``, one durable
  ``@activity.defn`` per stage, calling the exact same methods.

Topology (``TeamTopology``):
- Split mode:  UX → [psychology, consistency] → viability;  scripts in parallel.
- Unified mode: UX → psychology → viability;                scripts in parallel.

Every stage method is a pure function of its inputs (no job-store or Temporal
coupling) so the two paths can never derive different results from the same
mission.
"""

from __future__ import annotations

import logging
from functools import cached_property
from typing import List, Tuple

from shared_concurrency import parallel_map

from .agents import (
    ConsistencyAgent,
    MarketViabilityAgent,
    ResearchScriptAgent,
    TranscriptIngestionAgent,
    UserPsychologyAgent,
    UXResearchAgent,
)
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

logger = logging.getLogger(__name__)

# Thread-path UX fan-out width. The Temporal path bounds fan-out by the worker's
# ``max_concurrent_activities`` instead, so this only governs local/thread runs.
_UX_FANOUT_WORKERS = 4

# Padding used by ``assemble`` to guarantee ``TeamOutput`` always carries at
# least two market signals (a display-only floor — these are never fed to the
# viability stage, which sees only the real derived signals).
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


class MarketResearchOrchestrator:
    """Coordinates the market-research workflow via a shared per-stage seam.

    Invariants:
        - Each ``*_one``/stage method is a pure function of its arguments and the
          held specialist agents; none touch the job store or Temporal. This is
          what lets the thread path and the Temporal activities share them
          verbatim.
    """

    def __init__(self) -> None:
        # ``TranscriptIngestionAgent``/``UXResearchAgent`` have cheap
        # constructors (no strands agent is built until ``UXResearchAgent`` runs,
        # which builds a fresh one per call). The four specialist agents below
        # each build a strands agent in their constructor, so they are lazily
        # cached: a fresh orchestrator is built per Temporal activity, and an
        # activity only touches the one stage it runs — so the others are never
        # constructed.
        self.ingestion = TranscriptIngestionAgent()
        self.ux = UXResearchAgent()

    @cached_property
    def psychology_agent(self) -> UserPsychologyAgent:
        return UserPsychologyAgent()

    @cached_property
    def consistency_agent(self) -> ConsistencyAgent:
        return ConsistencyAgent()

    @cached_property
    def viability_agent(self) -> MarketViabilityAgent:
        return MarketViabilityAgent()

    @cached_property
    def scripts_agent(self) -> ResearchScriptAgent:
        return ResearchScriptAgent()

    # ------------------------------------------------------------------
    # Per-stage seam (shared by the thread path and the Temporal activities)
    # ------------------------------------------------------------------

    def ingest(self, mission: ResearchMission) -> List[Tuple[str, str]]:
        """Load transcript text for ``mission`` (pure I/O, no LLM).

        Postconditions:
            - Returns ``[(source, text), ...]`` — inline transcripts first, then
              ``*.txt`` files under ``transcript_folder_path`` (may be empty).
        """
        return self.ingestion.load_transcripts(mission)

    def ux_one(self, source: str, transcript: str) -> InterviewInsight:
        """Extract one interview's ``InterviewInsight`` (one LLM call).

        Preconditions:
            - ``transcript`` is a single interview's text; ``source`` labels it.
        Postconditions:
            - Returns an ``InterviewInsight`` tagged with ``source``; parsing
              failures fall back to the agent's default fields, never raise.
        """
        return self.ux.analyze(source, transcript)

    def psychology(self, insights: List[InterviewInsight]) -> List[MarketSignal]:
        """Derive adoption/behavior ``MarketSignal``s from ``insights``.

        Postconditions:
            - Returns at least two signals (the agent pads with defaults).
        """
        return self.psychology_agent.derive_signals(insights)

    def consistency(self, insights: List[InterviewInsight]) -> List[MarketSignal]:
        """Score cross-interview theme consistency (split mode only).

        Postconditions:
            - Empty ``insights`` (no transcripts) → a single deterministic
              "Cross-interview theme consistency" fallback signal (no LLM call),
              preserving the previous graph-path behavior.
            - Otherwise → the consistency agent's single-signal assessment.
        """
        if not insights:
            return [self._consistency_empty_signal()]
        return self.consistency_agent.analyze(insights)

    def viability(
        self, mission: ResearchMission, signals: List[MarketSignal], insight_count: int
    ) -> ViabilityRecommendation:
        """Produce the viability verdict from the real derived ``signals``.

        Postconditions:
            - ``insight_count == 0`` → a deterministic ``insufficient_evidence``
              recommendation (no LLM call); otherwise an LLM-backed verdict.
        """
        return self.viability_agent.recommend(mission, signals, insight_count)

    def scripts(self, mission: ResearchMission) -> List[str]:
        """Generate the research scripts/templates for ``mission`` (one LLM call).

        Postconditions:
            - Returns a non-empty ``list[str]`` (agent defaults on parse failure).
        """
        return self.scripts_agent.build_scripts(mission)

    @staticmethod
    def _consistency_empty_signal() -> MarketSignal:
        """The deterministic split-mode fallback when no transcripts exist."""
        return MarketSignal(
            signal="Cross-interview theme consistency",
            confidence=0.55,
            evidence=[
                "Insufficient transcript volume for consistency scoring; collect 5+ interviews."
            ],
        )

    def assemble(
        self,
        mission: ResearchMission,
        human_review: HumanReview,
        insights: List[InterviewInsight],
        signals: List[MarketSignal],
        recommendation: ViabilityRecommendation,
        scripts: List[str],
    ) -> TeamOutput:
        """Assemble the final ``TeamOutput`` (pure — no LLM, no I/O).

        Holds the two policy branches the workflow must not: the min-2
        market-signals display floor, and the ``human_review.approved`` gate
        (NEEDS_HUMAN_DECISION vs READY_FOR_EXECUTION).

        Preconditions:
            - ``signals`` are the real derived signals (already includes the
              consistency signal in split mode); ``recommendation`` was computed
              from them.
        Postconditions:
            - Returns a ``TeamOutput`` carrying ``insights``/``scripts`` verbatim
              and at least two ``market_signals``.
        """
        market_signals = list(signals)
        while len(market_signals) < 2:
            # Copy the shared default so distinct outputs never alias (and mutate)
            # the same module-level MarketSignal instance.
            market_signals.append(_DEFAULT_SIGNALS_FALLBACK[len(market_signals)].model_copy())

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

    # ------------------------------------------------------------------
    # Thread-path driver (Temporal mode reproduces this DAG across activities)
    # ------------------------------------------------------------------

    def run(self, mission: ResearchMission, human_review: HumanReview) -> TeamOutput:
        """Run the full market-research workflow in-process (thread mode).

        Reproduces the same DAG the Temporal workflow orchestrates: ingest →
        UX fan-out (one call per transcript) + scripts → psychology (+consistency
        in split mode) → viability → assemble.

        Preconditions:
            - ``mission`` is a validated ``ResearchMission``.
        Postconditions:
            - Returns the assembled ``TeamOutput``. A single transcript's UX
              failure raises (whole-run failure), matching the previous
              behavior; the Temporal path instead retries/drops per transcript.
        """
        loaded = self.ingest(mission)

        insights: List[InterviewInsight] = (
            parallel_map(
                loaded,
                lambda item: self.ux_one(item[0], item[1]),
                max_workers=_UX_FANOUT_WORKERS,
            )
            if loaded
            else []
        )

        scripts = self.scripts(mission)

        signals = self.psychology(insights)
        if mission.topology == TeamTopology.SPLIT:
            signals = signals + self.consistency(insights)

        recommendation = self.viability(mission, signals, len(insights))

        return self.assemble(mission, human_review, insights, signals, recommendation, scripts)

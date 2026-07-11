"""Typed payloads threaded across the deepthought Temporal activity boundaries.

Each ``@activity.defn`` takes/returns JSON (dicts) so the payloads are built with
``model_dump(mode="json")`` on the workflow side and rebuilt with
``model_validate`` inside the activity. Keeping them as Pydantic models (rather
than bare dicts) documents the contract and gives validation on the way in.

Only the light state an activity actually needs travels through Temporal — a
node's spec, a *bounded* pre-rendered knowledge summary (the workflow renders it
so it never ships the whole growing knowledge base per node), and the *compact*
child summaries (deliberation/synthesis only read ``agent_name``/
``focus_question``/``confidence``/``answer``), never the full recursive subtrees.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from deepthought.models import AgentResult, AgentSpec


class AnalysePayload(BaseModel):
    """Input to ``analyse_activity`` (wraps ``DeepthoughtAgent._analyse``)."""

    spec: AgentSpec
    parent_question: str = ""
    original_query: str
    conversation_history: list[dict[str, Any]] = Field(default_factory=list)
    decomposition_strategy: str
    knowledge_summary: str = ""
    max_depth: int


class ForceDirectAnswerPayload(BaseModel):
    """Input to ``force_direct_answer_activity``."""

    spec: AgentSpec
    parent_question: str = ""
    original_query: str
    knowledge_summary: str = ""


class ChildSummary(BaseModel):
    """Compact projection of a child ``AgentResult`` for deliberation/synthesis."""

    agent_id: str = ""
    agent_name: str
    depth: int = 0
    focus_question: str
    answer: str
    confidence: float = 0.0

    def to_agent_result(self) -> AgentResult:
        """Rebuild the lightweight ``AgentResult`` the reasoning methods consume.

        Postconditions:
            - Returns a childless, non-decomposed ``AgentResult`` carrying the
              four fields ``_results_to_dicts`` / the synthesis fallback read.
        """
        return AgentResult(
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            depth=self.depth,
            focus_question=self.focus_question,
            answer=self.answer,
            confidence=self.confidence,
            child_results=[],
            was_decomposed=False,
        )


class DeliberatePayload(BaseModel):
    """Input to ``deliberate_activity`` (wraps ``DeepthoughtAgent._deliberate``)."""

    spec: AgentSpec
    original_query: str
    children: list[ChildSummary] = Field(default_factory=list)


class SynthesisePayload(BaseModel):
    """Input to ``synthesise_activity`` (wraps ``DeepthoughtAgent._synthesise``)."""

    spec: AgentSpec
    original_query: str
    deliberation_notes: str = ""
    children: list[ChildSummary] = Field(default_factory=list)


def child_summaries(results: list[AgentResult]) -> list[ChildSummary]:
    """Project child results to their compact summaries (pure)."""
    return [
        ChildSummary(
            agent_id=r.agent_id,
            agent_name=r.agent_name,
            depth=r.depth,
            focus_question=r.focus_question,
            answer=r.answer,
            confidence=r.confidence,
        )
        for r in results
    ]

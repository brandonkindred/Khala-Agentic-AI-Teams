"""
Planning phase: high-level plan, milestones, user stories.

Roles: System Design, Architecture, User Story creation, DevOps, UI Design.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from shared.llm import LLMClient

from ..models import PlanningPhaseResult, SpecReviewResult
from ..prompts import PLANNING_PROMPT

logger = logging.getLogger(__name__)


def _parse_planning_response(raw: Any) -> PlanningPhaseResult:
    """Parse LLM JSON response into PlanningPhaseResult."""
    if not isinstance(raw, dict):
        return PlanningPhaseResult(summary="Planning complete (no structured output).")
    milestones = raw.get("milestones")
    user_stories = raw.get("user_stories")
    return PlanningPhaseResult(
        milestones=list(milestones) if isinstance(milestones, list) else [],
        user_stories=list(user_stories) if isinstance(user_stories, list) else [],
        high_level_plan=str(raw.get("high_level_plan", "") or ""),
        summary=str(raw.get("summary", "") or "Planning complete."),
    )


def run_planning(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    spec_review_result: Optional[SpecReviewResult] = None,
    inspiration_content: Optional[str] = None,
) -> PlanningPhaseResult:
    """
    Run Planning phase (5 roles: System Design, Architecture, User Story, DevOps, UI Design).
    """
    review_summary = (spec_review_result.summary if spec_review_result else "") or "None"
    prompt = PLANNING_PROMPT.format(
        spec_content=(spec_content or "")[:8000],
        review_summary=review_summary[:1000],
    )
    try:
        raw = llm.complete_json(prompt)
        result = _parse_planning_response(raw)
        logger.info("Planning: %d milestones, %d user stories", len(result.milestones), len(result.user_stories))
        return result
    except Exception as e:
        logger.warning("Planning LLM call failed, using fallback: %s", e)
        return PlanningPhaseResult(
            milestones=[],
            user_stories=[],
            high_level_plan="",
            summary="Planning completed (fallback).",
        )

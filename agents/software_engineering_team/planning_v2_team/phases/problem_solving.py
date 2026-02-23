"""
Problem-solving phase: identify root causes and fix inconsistencies.

Roles: System Design, Architecture (High-Level).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from shared.llm import LLMClient

from ..models import (
    ImplementationPhaseResult,
    PlanningPhaseResult,
    ProblemSolvingPhaseResult,
    ReviewPhaseResult,
    SpecReviewResult,
)
from ..prompts import PROBLEM_SOLVING_PROMPT

logger = logging.getLogger(__name__)


def run_problem_solving(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    spec_review_result: Optional[SpecReviewResult] = None,
    planning_result: Optional[PlanningPhaseResult] = None,
    implementation_result: Optional[ImplementationPhaseResult] = None,
    review_result: Optional[ReviewPhaseResult] = None,
) -> ProblemSolvingPhaseResult:
    """
    Run Problem-solving phase (roles: System Design, Architecture).
    """
    issues_str = ""
    if review_result and review_result.issues:
        issues_str = "; ".join(review_result.issues[:10])
    if not issues_str:
        return ProblemSolvingPhaseResult(fixes_applied=[], resolved=True, summary="No issues to fix.")
    prompt = PROBLEM_SOLVING_PROMPT.format(issues=issues_str[:2000])
    try:
        raw = llm.complete_json(prompt)
        if not isinstance(raw, dict):
            return ProblemSolvingPhaseResult(fixes_applied=[], resolved=True, summary="Problem-solving complete (fallback).")
        fixes = raw.get("fixes_applied")
        return ProblemSolvingPhaseResult(
            fixes_applied=list(fixes) if isinstance(fixes, list) else [],
            resolved=bool(raw.get("resolved", True)),
            summary=str(raw.get("summary", "") or "Problem-solving complete."),
        )
    except Exception as e:
        logger.warning("Problem-solving LLM call failed: %s", e)
        return ProblemSolvingPhaseResult(fixes_applied=[], resolved=True, summary="Problem-solving (fallback).")

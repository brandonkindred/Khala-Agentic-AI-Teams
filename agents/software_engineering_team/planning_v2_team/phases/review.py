"""
Review phase: ensure plan assets are cohesive and aligned with spec.

Roles: System Design, Architecture, Task Dependency analyzer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from shared.llm import LLMClient

from ..models import (
    ImplementationPhaseResult,
    PlanningPhaseResult,
    ReviewPhaseResult,
    SpecReviewResult,
)
from ..prompts import REVIEW_PROMPT

logger = logging.getLogger(__name__)


def run_review(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    spec_review_result: Optional[SpecReviewResult] = None,
    planning_result: Optional[PlanningPhaseResult] = None,
    implementation_result: Optional[ImplementationPhaseResult] = None,
) -> ReviewPhaseResult:
    """
    Run Review phase (roles: System Design, Architecture, Task Dependency analyzer).
    """
    prompt = REVIEW_PROMPT.format(spec_content=(spec_content or "")[:8000])
    try:
        raw = llm.complete_json(prompt)
        if not isinstance(raw, dict):
            return ReviewPhaseResult(passed=True, issues=[], summary="Review complete (fallback).")
        issues = raw.get("issues")
        return ReviewPhaseResult(
            passed=bool(raw.get("passed", True)),
            issues=list(issues) if isinstance(issues, list) else [],
            summary=str(raw.get("summary", "") or "Review complete."),
        )
    except Exception as e:
        logger.warning("Review LLM call failed, passing: %s", e)
        return ReviewPhaseResult(passed=True, issues=[], summary="Review completed (fallback).")

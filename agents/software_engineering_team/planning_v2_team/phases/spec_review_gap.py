"""
Spec Review and Gap analysis phase: System Design + Architecture roles.

Identifies critical gaps, open questions, and requirements from the spec.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from shared.llm import LLMClient

from ..models import SpecReviewResult
from ..prompts import SPEC_REVIEW_PROMPT

logger = logging.getLogger(__name__)


def _parse_spec_review_response(raw: Any) -> SpecReviewResult:
    """Parse LLM JSON response into SpecReviewResult."""
    if not isinstance(raw, dict):
        return SpecReviewResult(summary="Spec review completed (no structured output).")
    gaps = raw.get("gaps")
    open_questions = raw.get("open_questions")
    return SpecReviewResult(
        gaps=list(gaps) if isinstance(gaps, list) else [],
        open_questions=list(open_questions) if isinstance(open_questions, list) else [],
        system_design_notes=str(raw.get("system_design_notes", "") or ""),
        architecture_notes=str(raw.get("architecture_notes", "") or ""),
        summary=str(raw.get("summary", "") or "Spec review complete."),
    )


def run_spec_review_gap(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    inspiration_content: Optional[str] = None,
) -> SpecReviewResult:
    """
    Run Spec Review and Gap analysis (roles: System Design, Architecture High-Level).
    """
    prompt = SPEC_REVIEW_PROMPT.format(
        spec_content=(spec_content or "")[:12000],
    )
    try:
        raw = llm.complete_json(prompt)
        result = _parse_spec_review_response(raw)
        logger.info("Spec review: %d gaps, %d open questions", len(result.gaps), len(result.open_questions))
        return result
    except Exception as e:
        logger.warning("Spec review LLM call failed, using fallback: %s", e)
        return SpecReviewResult(
            gaps=[],
            open_questions=[],
            system_design_notes="",
            architecture_notes="",
            summary="Spec review completed (fallback).",
        )

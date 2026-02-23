"""
Implementation phase: create or update planning assets.

Roles: all 8 (System Design, Architecture, User Story, DevOps, UI, UX, Task Classification, Task Dependency).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from shared.llm import LLMClient

from ..models import ImplementationPhaseResult, PlanningPhaseResult, SpecReviewResult

logger = logging.getLogger(__name__)


def run_implementation(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    spec_review_result: Optional[SpecReviewResult] = None,
    planning_result: Optional[PlanningPhaseResult] = None,
    inspiration_content: Optional[str] = None,
) -> ImplementationPhaseResult:
    """
    Run Implementation phase (all 8 roles); create/update planning assets under repo_path.
    """
    assets_created: list[str] = []
    try:
        repo_path.mkdir(parents=True, exist_ok=True)
        plan_dir = repo_path / "planning_v2"
        plan_dir.mkdir(parents=True, exist_ok=True)

        parts = ["# Planning (v2) artifacts\n"]
        if spec_review_result:
            parts.append("## Spec review\n")
            parts.append(spec_review_result.summary or "")
            parts.append("\n\n")
        if planning_result:
            parts.append("## High-level plan\n")
            parts.append(planning_result.high_level_plan or planning_result.summary or "")
            parts.append("\n\n## Milestones\n")
            for m in planning_result.milestones:
                parts.append(f"- {m}\n")
            parts.append("\n## User stories\n")
            for u in planning_result.user_stories:
                parts.append(f"- {u}\n")

        out_file = plan_dir / "planning_artifacts.md"
        out_file.write_text("".join(parts), encoding="utf-8")
        assets_created.append(str(out_file.relative_to(repo_path)))
        logger.info("Implementation: wrote %s", out_file)
    except Exception as e:
        logger.warning("Implementation write failed: %s", e)

    return ImplementationPhaseResult(
        assets_created=assets_created,
        assets_updated=[],
        summary="Implementation complete." if assets_created else "No assets written.",
    )

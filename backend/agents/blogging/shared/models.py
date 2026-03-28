"""
Shared models for the blogging pipeline.

Provides phase enums and common data structures used across the blogging agents.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict


class BlogPhase(str, Enum):
    """Lifecycle phases of the blogging pipeline workflow.

    Each phase has an associated progress range for UI display.
    """

    RESEARCH = "research"
    PLANNING = "planning"
    DRAFT_INITIAL = "draft_initial"
    DRAFT_REVIEW = "draft_review"
    COPY_EDIT_LOOP = "copy_edit"
    FACT_CHECK = "fact_check"
    COMPLIANCE = "compliance"
    REWRITE_LOOP = "rewrite"
    FINALIZE = "finalize"


# Progress ranges for each phase (min, max percentage)
PHASE_PROGRESS_RANGES: Dict[BlogPhase, tuple[int, int]] = {
    BlogPhase.RESEARCH: (0, 12),
    BlogPhase.PLANNING: (12, 24),
    BlogPhase.DRAFT_INITIAL: (24, 38),
    BlogPhase.DRAFT_REVIEW: (38, 50),
    BlogPhase.COPY_EDIT_LOOP: (50, 65),
    BlogPhase.FACT_CHECK: (65, 73),
    BlogPhase.COMPLIANCE: (73, 82),
    BlogPhase.REWRITE_LOOP: (82, 95),
    BlogPhase.FINALIZE: (95, 100),
}


def get_phase_progress(phase: BlogPhase, sub_progress: float = 0.0) -> int:
    """Calculate overall progress percentage based on phase and sub-progress within phase.

    Args:
        phase: Current pipeline phase
        sub_progress: Progress within the current phase (0.0 to 1.0)

    Returns:
        Overall progress percentage (0-100)
    """
    min_prog, max_prog = PHASE_PROGRESS_RANGES[phase]
    return min(100, int(min_prog + (max_prog - min_prog) * sub_progress))


# Phase order for tracking completed phases
PHASE_ORDER = [
    BlogPhase.RESEARCH,
    BlogPhase.PLANNING,
    BlogPhase.DRAFT_INITIAL,
    BlogPhase.DRAFT_REVIEW,
    BlogPhase.COPY_EDIT_LOOP,
    BlogPhase.FACT_CHECK,
    BlogPhase.COMPLIANCE,
    BlogPhase.REWRITE_LOOP,
    BlogPhase.FINALIZE,
]


def get_completed_phases(current_phase: BlogPhase) -> list[str]:
    """Return list of phase values that have been completed before the current phase."""
    completed = []
    for phase in PHASE_ORDER:
        if phase == current_phase:
            break
        completed.append(phase.value)
    return completed

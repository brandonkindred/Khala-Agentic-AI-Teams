"""
Planning-V2 team orchestrator: 6-phase state machine.

Product Planning Tool Agents: Spec Review and Gap analysis → Planning → Implementation
→ Review (with Problem-solving retry) → Deliver.

No code from planning_team or project_planning_agent is imported or reused.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from shared.llm import LLMClient

from .models import (
    DeliverPhaseResult,
    ImplementationPhaseResult,
    Phase,
    PlanningPhaseResult,
    PlanningRole,
    PlanningV2WorkflowResult,
    ProblemSolvingPhaseResult,
    ReviewPhaseResult,
    SpecReviewResult,
)
from .phases.spec_review_gap import run_spec_review_gap
from .phases.planning import run_planning
from .phases.implementation import run_implementation
from .phases.review import run_review
from .phases.problem_solving import run_problem_solving
from .phases.deliver import run_deliver

logger = logging.getLogger(__name__)

# Phase order for completed_phases and progress (snake_case for API/UI)
PHASE_ORDER: List[str] = [
    Phase.SPEC_REVIEW_GAP.value,
    Phase.PLANNING.value,
    Phase.IMPLEMENTATION.value,
    Phase.REVIEW.value,
    Phase.PROBLEM_SOLVING.value,
    Phase.DELIVER.value,
]

# Role–phase mapping (which roles participate in each phase)
PHASE_ROLES: dict[Phase, List[PlanningRole]] = {
    Phase.SPEC_REVIEW_GAP: [PlanningRole.SYSTEM_DESIGN, PlanningRole.ARCHITECTURE_HIGH_LEVEL],
    Phase.PLANNING: [
        PlanningRole.SYSTEM_DESIGN,
        PlanningRole.ARCHITECTURE_HIGH_LEVEL,
        PlanningRole.USER_STORY_CREATION,
        PlanningRole.DEVOPS,
        PlanningRole.UI_DESIGN,
    ],
    Phase.IMPLEMENTATION: list(PlanningRole),
    Phase.REVIEW: [
        PlanningRole.SYSTEM_DESIGN,
        PlanningRole.ARCHITECTURE_HIGH_LEVEL,
        PlanningRole.TASK_DEPENDENCY_ANALYZER,
    ],
    Phase.PROBLEM_SOLVING: [PlanningRole.SYSTEM_DESIGN, PlanningRole.ARCHITECTURE_HIGH_LEVEL],
    Phase.DELIVER: [PlanningRole.SYSTEM_DESIGN, PlanningRole.ARCHITECTURE_HIGH_LEVEL],
}

MAX_REVIEW_ITERATIONS = 5


def _active_roles_for_phase(phase: Phase) -> List[str]:
    """Return list of role names (snake_case) for the current phase."""
    return [r.value for r in PHASE_ROLES.get(phase, [])]


class PlanningV2TeamLead:
    """
    Orchestrates the planning-v2 6-phase lifecycle.

    Invariants:
        - self.llm is always a valid LLMClient.
        - run_workflow never imports from planning_team or project_planning_agent.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run_workflow(
        self,
        *,
        spec_content: str,
        repo_path: Path,
        inspiration_content: Optional[str] = None,
        job_updater: Optional[Callable[..., None]] = None,
    ) -> PlanningV2WorkflowResult:
        """
        Execute the full 6-phase planning-v2 lifecycle.

        Phases:
            1. Spec Review and Gap analysis
            2. Planning
            3. Implementation
            4. Review (on failure: Problem-solving, then retry Implementation/Review)
            5. Deliver
        """
        start_time = time.monotonic()
        result = PlanningV2WorkflowResult()

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        logger.info("Planning-v2 WORKFLOW START")

        spec_review_result: Optional[SpecReviewResult] = None
        planning_result: Optional[PlanningPhaseResult] = None
        implementation_result: Optional[ImplementationPhaseResult] = None
        review_result: Optional[ReviewPhaseResult] = None
        problem_solving_result: Optional[ProblemSolvingPhaseResult] = None
        deliver_result: Optional[DeliverPhaseResult] = None

        # ── Phase 1: Spec Review and Gap analysis ───────────────────────
        result.current_phase = Phase.SPEC_REVIEW_GAP
        _update_job(
            current_phase=Phase.SPEC_REVIEW_GAP.value,
            progress=5,
            active_roles=_active_roles_for_phase(Phase.SPEC_REVIEW_GAP),
        )
        try:
            spec_review_result = run_spec_review_gap(
                llm=self.llm,
                spec_content=spec_content,
                repo_path=repo_path,
                inspiration_content=inspiration_content,
            )
            result.spec_review_result = spec_review_result
        except Exception as exc:
            result.failure_reason = f"Spec review failed: {exc}"
            logger.error("Planning-v2: %s", result.failure_reason)
            return result
        _update_job(current_phase=Phase.SPEC_REVIEW_GAP.value, progress=15)

        # ── Phase 2: Planning ────────────────────────────────────────────
        result.current_phase = Phase.PLANNING
        _update_job(
            current_phase=Phase.PLANNING.value,
            progress=20,
            active_roles=_active_roles_for_phase(Phase.PLANNING),
        )
        try:
            planning_result = run_planning(
                llm=self.llm,
                spec_content=spec_content,
                repo_path=repo_path,
                spec_review_result=spec_review_result,
                inspiration_content=inspiration_content,
            )
            result.planning_result = planning_result
        except Exception as exc:
            result.failure_reason = f"Planning failed: {exc}"
            logger.error("Planning-v2: %s", result.failure_reason)
            return result
        _update_job(current_phase=Phase.PLANNING.value, progress=35)

        # ── Phases 3–4: Implementation → Review (with Problem-solving retry) ─
        for iteration in range(1, MAX_REVIEW_ITERATIONS + 1):
            # Phase 3: Implementation
            result.current_phase = Phase.IMPLEMENTATION
            _update_job(
                current_phase=Phase.IMPLEMENTATION.value,
                progress=40 + (iteration - 1) * 10,
                active_roles=_active_roles_for_phase(Phase.IMPLEMENTATION),
            )
            try:
                implementation_result = run_implementation(
                    llm=self.llm,
                    spec_content=spec_content,
                    repo_path=repo_path,
                    spec_review_result=spec_review_result,
                    planning_result=planning_result,
                    inspiration_content=inspiration_content,
                )
                result.implementation_result = implementation_result
            except Exception as exc:
                result.failure_reason = f"Implementation failed (iter {iteration}): {exc}"
                logger.error("Planning-v2: %s", result.failure_reason)
                return result

            # Phase 4: Review
            result.current_phase = Phase.REVIEW
            _update_job(
                current_phase=Phase.REVIEW.value,
                progress=55 + (iteration - 1) * 10,
                active_roles=_active_roles_for_phase(Phase.REVIEW),
            )
            try:
                review_result = run_review(
                    llm=self.llm,
                    spec_content=spec_content,
                    repo_path=repo_path,
                    spec_review_result=spec_review_result,
                    planning_result=planning_result,
                    implementation_result=implementation_result,
                )
                result.review_result = review_result
            except Exception as exc:
                logger.warning("Planning-v2: Review failed (non-blocking): %s", exc)
                break

            if review_result.passed:
                logger.info("Planning-v2: Review passed on iteration %d", iteration)
                break

            # Phase: Problem-solving
            result.current_phase = Phase.PROBLEM_SOLVING
            _update_job(
                current_phase=Phase.PROBLEM_SOLVING.value,
                progress=70,
                active_roles=_active_roles_for_phase(Phase.PROBLEM_SOLVING),
            )
            try:
                problem_solving_result = run_problem_solving(
                    llm=self.llm,
                    spec_content=spec_content,
                    repo_path=repo_path,
                    spec_review_result=spec_review_result,
                    planning_result=planning_result,
                    implementation_result=implementation_result,
                    review_result=review_result,
                )
                result.problem_solving_result = problem_solving_result
            except Exception as exc:
                logger.warning("Planning-v2: Problem-solving failed (non-blocking): %s", exc)
                break

        # ── Phase 5: Deliver ─────────────────────────────────────────────
        result.current_phase = Phase.DELIVER
        _update_job(
            current_phase=Phase.DELIVER.value,
            progress=90,
            active_roles=_active_roles_for_phase(Phase.DELIVER),
        )
        try:
            deliver_result = run_deliver(
                llm=self.llm,
                spec_content=spec_content,
                repo_path=repo_path,
                implementation_result=implementation_result,
            )
            result.deliver_result = deliver_result
            result.success = True
            result.summary = deliver_result.summary or "Planning-v2 workflow completed."
        except Exception as exc:
            result.failure_reason = f"Deliver failed: {exc}"
            logger.error("Planning-v2: %s", result.failure_reason)
            return result

        elapsed = time.monotonic() - start_time
        _update_job(current_phase=Phase.DELIVER.value, progress=100)
        logger.info("Planning-v2 WORKFLOW completed in %.1fs", elapsed)
        return result

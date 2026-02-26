"""
Planning-V2 team orchestrator: 3-layer architecture with 6-phase state machine.

Layer 1: PlanningV2ProductLead (top) - handles spec intake, inspiration, feedback
Layer 2: PlanningV2PlanningAgent (middle) - orchestrates 8 tool agents across phases
Layer 3: Tool Agents (bottom) - 8 specialized agents participating in 6 phases

Phases: Spec Review → Planning → Implementation → Review → Problem-solving → Deliver

Open Questions: After the planning phase, if open questions were identified during spec review,
the workflow pauses and waits for user answers before continuing to implementation.

No code from planning_team or project_planning_agent is imported or reused.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from shared.llm import LLMClient
from shared.models import PlanningHierarchy

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
    ToolAgentKind,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
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

# Tool agent participation per phase (from the matrix)
PHASE_TOOL_AGENTS: Dict[Phase, List[ToolAgentKind]] = {
    Phase.SPEC_REVIEW_GAP: [
        ToolAgentKind.SYSTEM_DESIGN,
        ToolAgentKind.ARCHITECTURE,
    ],
    Phase.PLANNING: [
        ToolAgentKind.SYSTEM_DESIGN,
        ToolAgentKind.ARCHITECTURE,
        ToolAgentKind.USER_STORY,
        ToolAgentKind.DEVOPS,
        ToolAgentKind.UI_DESIGN,
    ],
    Phase.IMPLEMENTATION: [
        ToolAgentKind.SYSTEM_DESIGN,
        ToolAgentKind.ARCHITECTURE,
        ToolAgentKind.USER_STORY,
        ToolAgentKind.DEVOPS,
        ToolAgentKind.UI_DESIGN,
        ToolAgentKind.UX_DESIGN,
        ToolAgentKind.TASK_CLASSIFICATION,
    ],
    Phase.REVIEW: [
        ToolAgentKind.SYSTEM_DESIGN,
        ToolAgentKind.ARCHITECTURE,
        ToolAgentKind.USER_STORY,
        ToolAgentKind.TASK_DEPENDENCY,
    ],
    Phase.PROBLEM_SOLVING: [
        ToolAgentKind.SYSTEM_DESIGN,
        ToolAgentKind.ARCHITECTURE,
        ToolAgentKind.USER_STORY,
    ],
    Phase.DELIVER: [
        ToolAgentKind.SYSTEM_DESIGN,
        ToolAgentKind.ARCHITECTURE,
        ToolAgentKind.USER_STORY,
    ],
}

MAX_REVIEW_ITERATIONS = 5

# Open questions wait configuration
OPEN_QUESTIONS_POLL_INTERVAL = 5  # seconds
OPEN_QUESTIONS_TIMEOUT = 3600  # 1 hour


def _convert_open_questions_to_pending(
    open_questions: List[str],
    source: str = "spec_review",
) -> List[Dict[str, Any]]:
    """Convert open questions from spec review to PendingQuestion format."""
    pending = []
    for i, question in enumerate(open_questions):
        pending.append({
            "id": f"oq-{source}-{i}",
            "question_text": question,
            "context": "This question was identified during the spec review phase.",
            "options": [
                {"id": "answer", "label": "Provide answer in 'Other' field"},
            ],
            "required": True,
            "source": source,
        })
    return pending


def _wait_for_answers(job_id: str, timeout: int = OPEN_QUESTIONS_TIMEOUT) -> bool:
    """
    Block until user answers are submitted or timeout.
    
    Returns True if answers were received, False if timeout.
    """
    from shared.job_store import is_waiting_for_answers, get_job
    
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if not is_waiting_for_answers(job_id):
            return True
        # Check if job was cancelled or failed
        job_data = get_job(job_id)
        if job_data and job_data.get("status") in ("failed", "cancelled"):
            return False
        time.sleep(OPEN_QUESTIONS_POLL_INTERVAL)
    return False


def _active_roles_for_phase(phase: Phase) -> List[str]:
    """Return list of role names (snake_case) for the current phase."""
    return [r.value for r in PHASE_ROLES.get(phase, [])]


def _build_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build all 8 tool agent instances."""
    from .tool_agents.system_design import SystemDesignToolAgent
    from .tool_agents.architecture import ArchitectureToolAgent
    from .tool_agents.user_story import UserStoryToolAgent
    from .tool_agents.devops import DevOpsToolAgent
    from .tool_agents.ui_design import UIDesignToolAgent
    from .tool_agents.ux_design import UXDesignToolAgent
    from .tool_agents.task_classification import TaskClassificationToolAgent
    from .tool_agents.task_dependency import TaskDependencyToolAgent

    return {
        ToolAgentKind.SYSTEM_DESIGN: SystemDesignToolAgent(llm),
        ToolAgentKind.ARCHITECTURE: ArchitectureToolAgent(llm),
        ToolAgentKind.USER_STORY: UserStoryToolAgent(llm),
        ToolAgentKind.DEVOPS: DevOpsToolAgent(llm),
        ToolAgentKind.UI_DESIGN: UIDesignToolAgent(llm),
        ToolAgentKind.UX_DESIGN: UXDesignToolAgent(llm),
        ToolAgentKind.TASK_CLASSIFICATION: TaskClassificationToolAgent(llm),
        ToolAgentKind.TASK_DEPENDENCY: TaskDependencyToolAgent(llm),
    }


class PlanningV2PlanningAgent:
    """
    Layer 2: Planning Agent that orchestrates the 8 tool agents across 6 phases.
    
    Called by PlanningV2ProductLead after initial spec intake.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self.tool_agents = _build_tool_agents(llm_client)

    def run_workflow(
        self,
        *,
        spec_content: str,
        repo_path: Path,
        inspiration_content: Optional[str] = None,
        job_updater: Optional[Callable[..., None]] = None,
        job_id: Optional[str] = None,
    ) -> PlanningV2WorkflowResult:
        """Execute the 6-phase planning workflow using tool agents.
        
        Args:
            spec_content: The specification content to plan from.
            repo_path: Path to the repository for storing artifacts.
            inspiration_content: Optional inspiration/moodboard content.
            job_updater: Callback to update job status in the store.
            job_id: Job ID for open questions support. If provided and open questions
                    are found after planning, the workflow will pause and wait for
                    user answers before continuing to implementation.
        """
        start_time = time.monotonic()
        result = PlanningV2WorkflowResult()
        
        current_files: Dict[str, str] = {}
        hierarchy: Optional[PlanningHierarchy] = None
        metadata: Dict[str, Any] = {}

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        logger.info("Planning-v2 Planning Agent WORKFLOW START")

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
                tool_agents=self.tool_agents,
            )
            result.spec_review_result = spec_review_result
        except Exception as exc:
            result.failure_reason = f"Spec review failed: {exc}"
            logger.error("Planning-v2: %s", result.failure_reason)
            return result
        _update_job(current_phase=Phase.SPEC_REVIEW_GAP.value, progress=15)

        # ── Open Questions Check: Wait for user answers if needed ────────────
        if job_id and spec_review_result and spec_review_result.open_questions:
            from shared.job_store import add_pending_questions, get_submitted_answers
            
            open_questions = spec_review_result.open_questions
            logger.info(
                "Planning-v2: Found %d open questions, waiting for user answers",
                len(open_questions),
            )
            
            pending_questions = _convert_open_questions_to_pending(open_questions)
            add_pending_questions(job_id, pending_questions)
            _update_job(
                current_phase=Phase.SPEC_REVIEW_GAP.value,
                progress=16,
                waiting_for_answers=True,
            )
            
            if not _wait_for_answers(job_id):
                result.failure_reason = "Timeout waiting for user answers to open questions"
                logger.error("Planning-v2: %s", result.failure_reason)
                return result
            
            # Retrieve submitted answers and store them in the result for context
            answers = get_submitted_answers(job_id)
            if answers:
                logger.info("Planning-v2: Received %d answers, continuing workflow", len(answers))
                result.user_answers = answers
            
            _update_job(
                current_phase=Phase.SPEC_REVIEW_GAP.value,
                progress=18,
                waiting_for_answers=False,
            )

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
                tool_agents=self.tool_agents,
            )
            result.planning_result = planning_result
            if planning_result.hierarchy:
                hierarchy = planning_result.hierarchy
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
                    tool_agents=self.tool_agents,
                    hierarchy=hierarchy,
                )
                result.implementation_result = implementation_result
                current_files.update({
                    a: "" for a in implementation_result.assets_created
                })
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
                    tool_agents=self.tool_agents,
                    hierarchy=hierarchy,
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
                    tool_agents=self.tool_agents,
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
                tool_agents=self.tool_agents,
                hierarchy=hierarchy,
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
        logger.info("Planning-v2 Planning Agent WORKFLOW completed in %.1fs", elapsed)
        return result


class PlanningV2ProductLead:
    """
    Layer 1: Product Lead that handles spec intake, inspiration, and feedback.
    
    Top layer of the 3-layer architecture. Delegates to PlanningV2PlanningAgent.
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
        job_id: Optional[str] = None,
    ) -> PlanningV2WorkflowResult:
        """
        Execute the full planning-v2 workflow.
        
        The Product Lead handles initial spec intake and then delegates to
        the Planning Agent for the 6-phase workflow.
        
        Args:
            spec_content: The specification content to plan from.
            repo_path: Path to the repository for storing artifacts.
            inspiration_content: Optional inspiration/moodboard content.
            job_updater: Callback to update job status in the store.
            job_id: Job ID for open questions support. If provided and open questions
                    are found after planning, the workflow will pause and wait for
                    user answers before continuing to implementation.
        """
        logger.info("Planning-v2 Product Lead: starting workflow")
        
        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass
        
        _update_job(current_phase="intake", progress=2)
        
        planning_agent = PlanningV2PlanningAgent(self.llm)
        result = planning_agent.run_workflow(
            spec_content=spec_content,
            repo_path=repo_path,
            inspiration_content=inspiration_content,
            job_updater=job_updater,
            job_id=job_id,
        )
        
        logger.info("Planning-v2 Product Lead: workflow %s", "succeeded" if result.success else "failed")
        return result


# Backward compatibility: alias the original class name
class PlanningV2TeamLead(PlanningV2ProductLead):
    """
    Backward-compatible alias for PlanningV2ProductLead.
    
    Use PlanningV2ProductLead or PlanningV2PlanningAgent directly for new code.
    """
    pass

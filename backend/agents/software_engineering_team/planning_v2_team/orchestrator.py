"""
Planning-V2 team orchestrator: 3-layer architecture with 4-phase state machine.

Layer 1: PlanningV2ProductLead (top) - handles spec intake, inspiration, feedback
Layer 2: PlanningV2PlanningAgent (middle) - orchestrates 8 tool agents across phases
Layer 3: Tool Agents (bottom) - 8 specialized agents participating in phases

Phases: Planning → Implementation → Review → Deliver

When Review finds issues, they are passed back to Implementation for fixing.
When tool agents raise clarification questions (e.g., deployment target), they
are surfaced to the user via the Open Questions UI.

This team expects to receive a pre-validated, complete specification. Use the
Product Requirements Analysis agent or similar upstream process to validate
specs before passing them to Planning V2. The team will not expand or
clarify the specification.

No code from planning_team or project_planning_agent is imported or reused.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from llm_service import LLMClient
from software_engineering_team.shared.job_store import add_pending_questions, is_waiting_for_answers
from software_engineering_team.shared.models import PlanningHierarchy

try:
    from unified_api.slack_notifier import notify_open_questions as slack_notify_open_questions
except ImportError:
    slack_notify_open_questions = None

from .models import (
    PLAN_PLANNING_TEAM_DIR,
    DeliverPhaseResult,
    ImplementationPhaseResult,
    Phase,
    PlanningPhaseResult,
    PlanningRole,
    PlanningV2WorkflowResult,
    ReviewPhaseResult,
    ToolAgentKind,
)
from .phases.deliver import run_deliver
from .phases.implementation import run_implementation
from .phases.planning import run_planning
from .phases.problem_solving import (
    format_issues_breakdown_and_synopsis,
    group_issues_by_agent,
)
from .phases.review import run_review

logger = logging.getLogger(__name__)

CLARIFICATION_POLL_INTERVAL = 5.0
MAX_CLARIFICATION_WAIT_SECONDS = 3600

# Phase order for completed_phases and progress (snake_case for API/UI)
PHASE_ORDER: List[str] = [
    Phase.PLANNING.value,
    Phase.IMPLEMENTATION.value,
    Phase.REVIEW.value,
    Phase.DELIVER.value,
]

# Role–phase mapping (which roles participate in each phase)
PHASE_ROLES: dict[Phase, List[PlanningRole]] = {
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
    Phase.DELIVER: [PlanningRole.SYSTEM_DESIGN, PlanningRole.ARCHITECTURE_HIGH_LEVEL],
}

# Tool agent participation per phase (from the matrix)
PHASE_TOOL_AGENTS: Dict[Phase, List[ToolAgentKind]] = {
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
    Phase.DELIVER: [
        ToolAgentKind.SYSTEM_DESIGN,
        ToolAgentKind.ARCHITECTURE,
        ToolAgentKind.USER_STORY,
    ],
}

MAX_REVIEW_ITERATIONS = 100


def _active_roles_for_phase(phase: Phase) -> List[str]:
    """Return list of role names (snake_case) for the current phase."""
    return [r.value for r in PHASE_ROLES.get(phase, [])]


def _convert_clarifications_to_pending_questions(
    clarification_questions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert tool agent clarification questions to pending question format.

    Tool agents produce questions in the format:
        {"source": "devops", "question_text": "What is the deployment target?"}

    This converts them to the job_store pending question format with sensible defaults.
    """
    pending: List[Dict[str, Any]] = []
    for q in clarification_questions:
        source = q.get("source", "unknown")
        question_text = q.get("question_text", "")
        if not question_text:
            continue

        question_id = f"clarify-{source}-{uuid.uuid4().hex[:8]}"

        options = _generate_options_for_question(question_text, source)

        pending.append(
            {
                "id": question_id,
                "question_text": question_text,
                "context": f"This question was raised by the {source.upper()} agent during planning.",
                "options": options,
                "allow_multiple": False,
                "required": True,
                "source": f"planning_v2_{source}",
                "category": "clarification",
                "priority": "medium",
            }
        )
    return pending


def _generate_options_for_question(question_text: str, source: str) -> List[Dict[str, Any]]:
    """Generate sensible default options based on the question content."""
    question_lower = question_text.lower()

    if source == "devops":
        if "deployment" in question_lower or "deploy" in question_lower:
            return [
                {
                    "id": "aws",
                    "label": "AWS (EC2/ECS/Lambda)",
                    "is_default": False,
                    "rationale": "Popular cloud provider",
                    "confidence": 0.0,
                },
                {
                    "id": "gcp",
                    "label": "Google Cloud Platform",
                    "is_default": False,
                    "rationale": "Popular cloud provider",
                    "confidence": 0.0,
                },
                {
                    "id": "azure",
                    "label": "Microsoft Azure",
                    "is_default": False,
                    "rationale": "Popular cloud provider",
                    "confidence": 0.0,
                },
                {
                    "id": "heroku",
                    "label": "Heroku",
                    "is_default": False,
                    "rationale": "Simple PaaS option",
                    "confidence": 0.0,
                },
                {
                    "id": "digitalocean",
                    "label": "DigitalOcean",
                    "is_default": False,
                    "rationale": "Developer-friendly hosting",
                    "confidence": 0.0,
                },
                {
                    "id": "vercel",
                    "label": "Vercel",
                    "is_default": False,
                    "rationale": "Good for frontend/JAMstack",
                    "confidence": 0.0,
                },
                {
                    "id": "docker",
                    "label": "Docker/Kubernetes (self-hosted)",
                    "is_default": False,
                    "rationale": "Container-based deployment",
                    "confidence": 0.0,
                },
                {
                    "id": "other",
                    "label": "Other (specify in text field)",
                    "is_default": False,
                    "rationale": "",
                    "confidence": 0.0,
                },
            ]
        if "ci" in question_lower or "continuous" in question_lower:
            return [
                {
                    "id": "github_actions",
                    "label": "GitHub Actions",
                    "is_default": False,
                    "rationale": "Integrated with GitHub",
                    "confidence": 0.0,
                },
                {
                    "id": "gitlab_ci",
                    "label": "GitLab CI/CD",
                    "is_default": False,
                    "rationale": "Integrated with GitLab",
                    "confidence": 0.0,
                },
                {
                    "id": "jenkins",
                    "label": "Jenkins",
                    "is_default": False,
                    "rationale": "Traditional CI/CD server",
                    "confidence": 0.0,
                },
                {
                    "id": "circleci",
                    "label": "CircleCI",
                    "is_default": False,
                    "rationale": "Cloud CI service",
                    "confidence": 0.0,
                },
                {
                    "id": "other",
                    "label": "Other (specify in text field)",
                    "is_default": False,
                    "rationale": "",
                    "confidence": 0.0,
                },
            ]

    return [
        {
            "id": "other",
            "label": "Provide answer in text field",
            "is_default": True,
            "rationale": "",
            "confidence": 0.0,
        }
    ]


def _build_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build all 8 tool agent instances."""
    from .tool_agents.architecture import ArchitectureToolAgent
    from .tool_agents.devops import DevOpsToolAgent
    from .tool_agents.system_design import SystemDesignToolAgent
    from .tool_agents.task_classification import TaskClassificationToolAgent
    from .tool_agents.task_dependency import TaskDependencyToolAgent
    from .tool_agents.ui_design import UIDesignToolAgent
    from .tool_agents.user_story import UserStoryToolAgent
    from .tool_agents.ux_design import UXDesignToolAgent

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
    Layer 2: Planning Agent that orchestrates the 8 tool agents across 4 phases.

    Phases: Planning -> Implementation -> Review -> Deliver

    When Review finds issues, they are passed back to Implementation for fixing.
    Implementation agents decide how to handle fixes (batch, one-by-one, or all at once).

    Called by PlanningV2ProductLead after initial spec intake.

    This agent expects to receive a pre-validated, complete specification.
    No spec review or expansion is performed.
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
        prd_content: Optional[str] = None,
    ) -> PlanningV2WorkflowResult:
        """Execute the planning workflow using tool agents.

        The planning team expects (1) a spec and (2) a product requirements document
        (PRD). Both are written under plan/planning_team at init for downstream phases.
        Spec comes from spec_content or from disk (get_latest_spec_path); PRD from
        prd_content if provided, else copied from plan/product_analysis or plan/.

        Args:
            spec_content: The pre-validated specification content to plan from.
                         This should be a complete spec that requires no expansion.
            repo_path: Path to the repository for storing artifacts.
            inspiration_content: Optional inspiration/moodboard content.
            job_updater: Callback to update job status in the store.
            job_id: Job ID for tracking progress.
            prd_content: Optional PRD content; when provided, written to plan/planning_team
                         and disk copy is skipped.
        """
        start_time = time.monotonic()
        result = PlanningV2WorkflowResult()

        current_files: Dict[str, str] = {}
        hierarchy: Optional[PlanningHierarchy] = None

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        logger.info("Planning-v2 Planning Agent WORKFLOW START")

        # Initialize plan/planning_team and copy validated spec (from PRA) or latest spec + PRD into it
        planning_team_dir = repo_path / PLAN_PLANNING_TEAM_DIR
        planning_team_dir.mkdir(parents=True, exist_ok=True)
        validated_spec_src = repo_path / "plan" / "product_analysis" / "validated_spec.md"
        if validated_spec_src.exists():
            shutil.copy2(validated_spec_src, planning_team_dir / "updated_spec.md")
            logger.info(
                "Planning-v2: copied validated_spec from plan/product_analysis to plan/planning_team/updated_spec.md"
            )
        else:
            try:
                from spec_parser import get_latest_spec_path

                src = get_latest_spec_path(repo_path)
                shutil.copy2(src, planning_team_dir / "updated_spec.md")
                logger.info(
                    "Planning-v2: copied latest spec to plan/planning_team/updated_spec.md (from %s)",
                    src,
                )
            except FileNotFoundError as e:
                logger.warning("Planning-v2: no spec file to copy into plan/planning_team: %s", e)
        # PRD: use in-memory prd_content if provided, else copy from disk (product_analysis first, then plan root)
        if prd_content is not None and prd_content.strip():
            (planning_team_dir / "product_requirements_document.md").write_text(
                prd_content, encoding="utf-8"
            )
            logger.info("Planning-v2: wrote PRD from in-memory content to plan/planning_team/")
        else:
            prd_src = repo_path / "plan" / "product_analysis" / "product_requirements_document.md"
            if prd_src.exists():
                shutil.copy2(prd_src, planning_team_dir / "product_requirements_document.md")
                logger.info(
                    "Planning-v2: copied PRD from plan/product_analysis to plan/planning_team/"
                )
            else:
                prd_src = repo_path / "plan" / "product_requirements_document.md"
                if prd_src.exists():
                    shutil.copy2(prd_src, planning_team_dir / "product_requirements_document.md")
                    logger.info("Planning-v2: copied PRD from plan/ to plan/planning_team/")
        logger.info("Planning-v2: plan/planning_team initialized")

        planning_result: Optional[PlanningPhaseResult] = None
        implementation_result: Optional[ImplementationPhaseResult] = None
        review_result: Optional[ReviewPhaseResult] = None
        deliver_result: Optional[DeliverPhaseResult] = None

        # ── Phase 1: Planning ──────────────────────────────────────────────
        logger.info("Planning-v2: Next step -> Starting Phase 1: Planning")
        result.current_phase = Phase.PLANNING
        _update_job(
            current_phase=Phase.PLANNING.value,
            progress=20,
            active_roles=_active_roles_for_phase(Phase.PLANNING),
            status_text="Generating system design, architecture, user stories, DevOps plan, and UI design.",
        )
        try:
            planning_result = run_planning(
                llm=self.llm,
                spec_content=spec_content,
                repo_path=repo_path,
                spec_review_result=None,
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

        # ── Handle Clarification Questions from Tool Agents ─────────────────
        if planning_result.clarification_questions and job_id:
            logger.info(
                "Planning-v2: Tool agents raised %d clarification questions",
                len(planning_result.clarification_questions),
            )
            pending_questions = _convert_clarifications_to_pending_questions(
                planning_result.clarification_questions
            )
            if pending_questions:
                add_pending_questions(job_id, pending_questions)
                if slack_notify_open_questions:
                    base = os.getenv("UI_BASE_URL", "http://localhost:4200").rstrip("/")
                    status_url = f"{base}/software-engineering/planning-v2?job={job_id}"
                    slack_notify_open_questions(
                        job_id, pending_questions, source="planning-v2", status_url=status_url
                    )
                _update_job(
                    waiting_for_answers=True,
                    status_text=f"Waiting for answers to {len(pending_questions)} question(s)",
                )
                logger.info(
                    "Planning-v2: Sent %d clarification questions to Open Questions UI",
                    len(pending_questions),
                )

                wait_start = time.time()
                while time.time() - wait_start < MAX_CLARIFICATION_WAIT_SECONDS:
                    if not is_waiting_for_answers(job_id):
                        logger.info("Planning-v2: Received answers to clarification questions")
                        break
                    time.sleep(CLARIFICATION_POLL_INTERVAL)
                else:
                    logger.warning(
                        "Planning-v2: Timeout waiting for clarification answers after %ds",
                        MAX_CLARIFICATION_WAIT_SECONDS,
                    )

        # ── Phases 2–3: Implementation → Review (loop until review passes) ─
        review_result = None
        for iteration in range(1, MAX_REVIEW_ITERATIONS + 1):
            # Phase 2: Implementation
            issue_count = len(review_result.issues) if review_result and review_result.issues else 0
            if issue_count > 0:
                logger.info(
                    "Planning-v2: Next step -> Starting Phase 2: Implementation (iteration %d/%d, fixing %d review issues)",
                    iteration,
                    MAX_REVIEW_ITERATIONS,
                    issue_count,
                )
                grouped = group_issues_by_agent(review_result.issues)
                counts_segment, synopsis_segment = format_issues_breakdown_and_synopsis(grouped)
                status_text = (
                    f"Fixing {issue_count} issues: {counts_segment} (iteration {iteration})"
                )
                if synopsis_segment:
                    status_text = f"{status_text}. {synopsis_segment}"
            else:
                logger.info(
                    "Planning-v2: Next step -> Starting Phase 2: Implementation (iteration %d/%d)",
                    iteration,
                    MAX_REVIEW_ITERATIONS,
                )
                status_text = (
                    f"Writing system design, architecture, user stories, DevOps, UI design, "
                    f"UX design, and task classification (iteration {iteration})."
                )

            result.current_phase = Phase.IMPLEMENTATION
            _update_job(
                current_phase=Phase.IMPLEMENTATION.value,
                progress=40 + (iteration - 1) * 10,
                active_roles=_active_roles_for_phase(Phase.IMPLEMENTATION),
                status_text=status_text,
            )
            try:
                implementation_result = run_implementation(
                    llm=self.llm,
                    spec_content=spec_content,
                    repo_path=repo_path,
                    spec_review_result=None,
                    planning_result=planning_result,
                    inspiration_content=inspiration_content,
                    tool_agents=self.tool_agents,
                    hierarchy=hierarchy,
                    review_result=review_result,
                )
                result.implementation_result = implementation_result
                current_files.update({a: "" for a in implementation_result.assets_created})
            except Exception as exc:
                result.failure_reason = f"Implementation failed (iter {iteration}): {exc}"
                logger.error("Planning-v2: %s", result.failure_reason)
                return result

            # Phase 3: Review
            logger.info("Planning-v2: Next step -> Starting Phase 3: Review")
            result.current_phase = Phase.REVIEW
            _update_job(
                current_phase=Phase.REVIEW.value,
                progress=55 + (iteration - 1) * 10,
                active_roles=_active_roles_for_phase(Phase.REVIEW),
                status_text="Reviewing system design, architecture, user stories, and task dependencies for consistency and completeness.",
            )
            try:
                review_result = run_review(
                    llm=self.llm,
                    spec_content=spec_content,
                    repo_path=repo_path,
                    spec_review_result=None,
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

            # Review did not pass - log issues and loop back to Implementation
            issue_count = len(review_result.issues) if review_result.issues else 0
            logger.info(
                "Planning-v2: Review did not pass (%d issues). Next step -> Returning to Implementation phase",
                issue_count,
            )

        # ── Phase 4: Deliver ─────────────────────────────────────────────
        logger.info("Planning-v2: Next step -> Starting Deliver phase")
        result.current_phase = Phase.DELIVER
        _update_job(
            current_phase=Phase.DELIVER.value,
            progress=90,
            active_roles=_active_roles_for_phase(Phase.DELIVER),
            status_text="Finalizing plan deliverables and documentation",
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
            result.final_spec_content = deliver_result.final_spec_content
        except Exception as exc:
            result.failure_reason = f"Deliver failed: {exc}"
            logger.error("Planning-v2: %s", result.failure_reason)
            return result

        elapsed = time.monotonic() - start_time
        _update_job(
            current_phase=Phase.DELIVER.value,
            progress=100,
            status_text="Planning complete - ready for execution",
        )
        logger.info("Planning-v2 Planning Agent WORKFLOW completed in %.1fs", elapsed)
        return result


class PlanningV2ProductLead:
    """
    Layer 1: Product Lead that handles spec intake, inspiration, and feedback.

    Top layer of the 3-layer architecture. Delegates to PlanningV2PlanningAgent.

    This team expects to receive a pre-validated, complete specification. Use the
    Product Requirements Analysis agent or similar upstream process to validate
    specs before passing them to Planning V2. The team will not expand or
    clarify the specification.

    Optionally runs Product Requirements Analysis first to get a validated spec.
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
        use_product_analysis: bool = False,
        validated_spec_content: Optional[str] = None,
        prd_content: Optional[str] = None,
    ) -> PlanningV2WorkflowResult:
        """
        Execute the full planning-v2 workflow.

        The Product Lead handles initial spec intake and then delegates to
        the Planning Agent for the planning workflow.

        The planning team expects (1) a spec (via spec_content/validated_spec_content
        or from disk) and (2) a product requirements document (PRD). Both are
        written under plan/planning_team at init. PRD can be passed as prd_content
        or is copied from plan/product_analysis or plan/ when present.

        Important: This team expects a pre-validated, complete specification.
        No spec review or expansion is performed.

        Args:
            spec_content: The pre-validated specification content to plan from.
                         This should be a complete spec that requires no expansion.
            repo_path: Path to the repository for storing artifacts.
            inspiration_content: Optional inspiration/moodboard content.
            job_updater: Callback to update job status in the store.
            job_id: Job ID for tracking progress.
            use_product_analysis: If True, run Product Requirements Analysis first
                                  to generate a validated spec before planning.
            validated_spec_content: Pre-validated spec content from Product Analysis.
                                    If provided, skips running Product Analysis even
                                    if use_product_analysis is True.
            prd_content: Optional PRD content; when provided, passed to Planning Agent
                         and written to plan/planning_team (skips disk copy).
        """
        logger.info("Planning-v2 Product Lead: starting workflow")

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        _update_job(current_phase="intake", progress=2, status_text="Ingesting specification")

        # Use validated spec if provided, or run Product Analysis if requested
        final_spec = spec_content

        if validated_spec_content:
            logger.info("Planning-v2: Using pre-validated spec content")
            final_spec = validated_spec_content
        elif use_product_analysis:
            logger.info("Planning-v2: Running Product Requirements Analysis first")
            _update_job(current_phase="product_analysis", progress=3)

            try:
                from product_requirements_analysis_agent import ProductRequirementsAnalysisAgent
                from spec_parser import gather_context_files

                # Gather context files for PRA agent
                context_files = gather_context_files(repo_path)
                if context_files:
                    logger.info(
                        "Planning-v2: Gathered %d context files for PRA", len(context_files)
                    )

                analysis_agent = ProductRequirementsAnalysisAgent(self.llm)
                analysis_result = analysis_agent.run_workflow(
                    spec_content=spec_content,
                    repo_path=repo_path,
                    job_id=job_id,
                    job_updater=job_updater,
                    context_files=context_files,
                )

                if analysis_result.success and analysis_result.final_spec_content:
                    final_spec = analysis_result.final_spec_content
                    logger.info("Planning-v2: Product Analysis complete - using validated spec")
                else:
                    logger.warning(
                        "Planning-v2: Product Analysis did not produce validated spec, "
                        "proceeding with original spec"
                    )
            except ImportError:
                logger.warning(
                    "Planning-v2: Product Requirements Analysis Agent not available, "
                    "skipping pre-analysis"
                )
            except Exception as exc:
                logger.warning(
                    "Planning-v2: Product Analysis failed (%s), proceeding with original spec",
                    exc,
                )

        # When no validated_spec_content was provided, try loading latest spec from repo (validated_spec, updated_spec, etc.)
        if not validated_spec_content:
            try:
                from spec_parser import get_latest_spec_content

                disk_spec = get_latest_spec_content(repo_path)
                if disk_spec.strip():
                    logger.info(
                        "Planning-v2: Using latest spec from repo (validated_spec.md, updated_spec.md, or fallback)"
                    )
                    final_spec = disk_spec
            except FileNotFoundError:
                pass  # Keep final_spec from spec_content or PRA result
            except Exception as exc:
                logger.warning("Planning-v2: Could not read latest spec from repo: %s", exc)

        planning_agent = PlanningV2PlanningAgent(self.llm)
        result = planning_agent.run_workflow(
            spec_content=final_spec,
            repo_path=repo_path,
            inspiration_content=inspiration_content,
            job_updater=job_updater,
            job_id=job_id,
            prd_content=prd_content,
        )

        logger.info(
            "Planning-v2 Product Lead: workflow %s", "succeeded" if result.success else "failed"
        )
        return result


# Backward compatibility: alias the original class name
class PlanningV2TeamLead(PlanningV2ProductLead):
    """
    Backward-compatible alias for PlanningV2ProductLead.

    Use PlanningV2ProductLead or PlanningV2PlanningAgent directly for new code.
    """

    pass

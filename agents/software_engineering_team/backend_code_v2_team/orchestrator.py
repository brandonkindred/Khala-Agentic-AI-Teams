"""
Backend-Code-V2 team orchestrator: 5-phase state machine.

Entry point used by the main orchestrator.
No code from ``backend_agent`` is imported or reused.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from software_engineering_team.shared.llm import LLMClient
from software_engineering_team.shared.models import SystemArchitecture, Task, TaskUpdate

from .models import (
    BackendCodeV2WorkflowResult,
    MicrotaskReviewConfig,
    MicrotaskReviewFailedError,
    MicrotaskStatus,
    Phase,
    ToolAgentKind,
    ToolAgentInput,
    ToolAgentOutput,
)
from .phases.planning import run_planning, plan_fixes_for_unresolved_issues
from .phases.execution import run_execution, run_execution_with_review_gates, ReviewDependencies
from .phases.review import run_review
from .phases.problem_solving import run_problem_solving
from .phases.deliver import run_deliver
from .phases.setup import run_setup

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERATIONS = 100


def _build_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build team-owned tool agent instances (for plan/execute/review/problem_solve/deliver)."""
    from .tool_agents.data_engineering import DataEngineeringToolAgent
    from .tool_agents.api_openapi import ApiOpenApiToolAgent
    from .tool_agents.auth import AuthToolAgent
    from .tool_agents.cicd import CicdAdapterAgent
    from .tool_agents.containerization import ContainerizationAdapterAgent
    from .tool_agents.git_branch_management import GitBranchManagementToolAgent
    from .tool_agents.build_specialist import BuildSpecialistAdapterAgent
    from .tool_agents.testing_qa import TestingQAToolAgent
    from .tool_agents.security import SecurityToolAgent
    from .tool_agents.documentation import DocumentationToolAgent

    return {
        ToolAgentKind.DATA_ENGINEERING: DataEngineeringToolAgent(llm),
        ToolAgentKind.API_OPENAPI: ApiOpenApiToolAgent(llm),
        ToolAgentKind.AUTH: AuthToolAgent(llm),
        ToolAgentKind.CICD: CicdAdapterAgent(),
        ToolAgentKind.CONTAINERIZATION: ContainerizationAdapterAgent(),
        ToolAgentKind.GIT_BRANCH_MANAGEMENT: GitBranchManagementToolAgent(),
        ToolAgentKind.BUILD_SPECIALIST: BuildSpecialistAdapterAgent(llm),
        ToolAgentKind.TESTING_QA: TestingQAToolAgent(llm),
        ToolAgentKind.SECURITY: SecurityToolAgent(llm),
        ToolAgentKind.DOCUMENTATION: DocumentationToolAgent(llm),
    }


class BackendDevelopmentAgent:
    """
    Backend Development Agent: runs the 5-phase cycle (Planning → Execution →
    Review → Problem-solving → Deliver). Used by BackendCodeV2TeamLead after Setup.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def _build_tool_runners(self, tool_agents: Dict[ToolAgentKind, Any]) -> Dict[ToolAgentKind, Callable[[ToolAgentInput], ToolAgentOutput]]:
        """Build run callables from tool agent instances (for Execution phase)."""
        runners = {}
        for k, ag in tool_agents.items():
            if hasattr(ag, "run"):
                runners[k] = ag.run
            elif hasattr(ag, "execute"):
                runners[k] = ag.execute
        return runners

    @staticmethod
    def _read_repo_code(repo_path: Path, max_chars: int = 30_000) -> str:
        """Read Python/Java source files from repo into a single string."""
        extensions = {".py", ".java", ".kt", ".yaml", ".yml", ".json", ".toml", ".cfg", ".txt"}
        parts: List[str] = []
        total = 0
        try:
            for f in sorted(repo_path.rglob("*")):
                if not f.is_file() or f.suffix not in extensions:
                    continue
                if any(skip in f.parts for skip in ("node_modules", ".git", "__pycache__", "venv", ".venv")):
                    continue
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(f.relative_to(repo_path))
                chunk = f"--- {rel} ---\n{content}\n"
                if total + len(chunk) > max_chars:
                    break
                parts.append(chunk)
                total += len(chunk)
        except Exception:
            pass
        return "\n".join(parts) if parts else "# No code files found"

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture] = None,
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        tech_lead: Any = None,
        completed_tasks: Optional[List[Task]] = None,
        remaining_tasks: Optional[List[Task]] = None,
        all_tasks: Optional[Dict[str, Task]] = None,
        execution_queue: Optional[List[str]] = None,
        append_task_fn: Optional[Callable[[Task], None]] = None,
        build_fix_specialist: Any = None,
        git_operations_tool_agent: Any = None,
        acceptance_verifier_agent: Any = None,
        dbc_agent: Any = None,
        problem_solver_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Optional[MicrotaskReviewConfig] = None,
    ) -> BackendCodeV2WorkflowResult:
        """
        Execute the full 5-phase backend-code-v2 lifecycle with per-microtask review gates.

        Each microtask must pass full review (code quality, QA, security, build, lint)
        before the next microtask can begin.
        """
        task_id = task.id
        start_time = time.monotonic()
        result = BackendCodeV2WorkflowResult(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        logger.info("[%s] WORKFLOW START: Backend Development Agent (per-microtask review gates)", task_id)

        existing_code = self._read_repo_code(repo_path)
        tool_agents = _build_tool_agents(self.llm)
        tool_runners = self._build_tool_runners(tool_agents)

        # ── Phase 1: Planning ──────────────────────────────────────────
        logger.info("[%s] Next step -> Starting Phase 1: Planning", task_id)
        result.current_phase = Phase.PLANNING
        _update_job(current_phase="planning", progress=5, status_text="Analyzing task and creating implementation plan")

        try:
            planning_result = run_planning(
                llm=self.llm,
                task=task,
                repo_path=repo_path,
                architecture=architecture,
                existing_code=existing_code,
                tool_agents=tool_agents,
            )
            result.planning_result = planning_result
        except Exception as exc:
            result.failure_reason = f"Planning failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result

        total_microtasks = len(planning_result.microtasks)
        _update_job(
            current_phase="planning",
            progress=10,
            microtasks_total=total_microtasks,
            microtasks_completed=0,
            status_text=f"Plan created with {total_microtasks} microtasks",
        )

        # ── Create feature branch (Git agent) before first execution ───
        feature_branch_name: Optional[str] = None
        git_agent = tool_agents.get(ToolAgentKind.GIT_BRANCH_MANAGEMENT)
        if git_agent is not None and hasattr(git_agent, "create_feature_branch"):
            try:
                ok, branch_name = git_agent.create_feature_branch(
                    repo_path, task_id, task.title or ""
                )
                if ok and branch_name:
                    feature_branch_name = branch_name
                    logger.info("[%s] Created feature branch: %s", task_id, feature_branch_name)
                else:
                    logger.warning("[%s] Git agent create_feature_branch failed, deliver will create branch", task_id)
            except Exception as exc:
                logger.warning("[%s] Git agent create_feature_branch raised: %s", task_id, exc)

        # ── Phase 2: Execution with per-microtask review gates ─────────
        logger.info("[%s] Next step -> Starting Phase 2: Execution", task_id)
        result.current_phase = Phase.EXECUTION
        _update_job(current_phase="execution", current_microtask="", progress=15, status_text="Starting code implementation")

        def _progress_cb(current_index: int, done: int, total: int, title: str, microtask_phase: str = "coding", phase_detail: str = "") -> None:
            phase_labels = {
                "coding": "Writing code",
                "code_review": "Code Review",
                "qa_testing": "QA Testing",
                "security_testing": "Security Testing",
                "documentation": "Documentation",
                "review": "Reviewing code",
                "problem_solving": "Fixing issues",
                "completed": "Completed",
            }
            phase_label = phase_labels.get(microtask_phase, microtask_phase.replace("_", " ").title())
            status = f"{phase_label}: {title} ({current_index}/{total})"
            _update_job(
                current_phase="execution",
                current_microtask=title,
                current_microtask_phase=microtask_phase,
                phase_detail=phase_detail,
                current_microtask_index=current_index,
                microtasks_completed=done,
                microtasks_total=total,
                progress=min(15 + int(done / max(total, 1) * 60), 75),
                status_text=status,
            )

        review_deps = ReviewDependencies(
            build_verifier=build_verifier,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            linting_tool_agent=linting_tool_agent,
            tool_agents=tool_agents,
        )

        config = review_config or MicrotaskReviewConfig()

        try:
            exec_result = run_execution_with_review_gates(
                llm=self.llm,
                task=task,
                planning_result=planning_result,
                repo_path=repo_path,
                architecture=architecture,
                existing_code=existing_code,
                tool_runners=tool_runners,
                progress_callback=_progress_cb,
                review_config=config,
                review_deps=review_deps,
            )
            result.execution_result = exec_result
        except MicrotaskReviewFailedError as err:
            result.failure_reason = f"Microtask {err.microtask.id} failed review: {err.review_result.summary}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result
        except Exception as exc:
            result.failure_reason = f"Execution failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result

        current_files = exec_result.files
        if not current_files:
            result.failure_reason = "Execution produced no files."
            return result

        completed_count = sum(1 for mt in exec_result.microtasks if mt.status == MicrotaskStatus.COMPLETED)
        failed_count = sum(1 for mt in exec_result.microtasks if mt.status == MicrotaskStatus.REVIEW_FAILED)
        result.iterations_used = completed_count

        if feature_branch_name and git_agent is not None and hasattr(git_agent, "commit_current_changes"):
            try:
                git_agent.commit_current_changes(repo_path, f"feat: {completed_count} microtasks completed")
            except Exception as exc:
                logger.warning("[%s] Git agent commit_current_changes raised: %s", task_id, exc)

        result.final_files = current_files

        # ── Phase: Documentation ────────────────────────────────────────
        logger.info("[%s] Next step -> Starting Phase: Documentation", task_id)
        result.current_phase = Phase.DOCUMENTATION
        _update_job(current_phase="documentation", progress=80, status_text="Generating documentation and API specs")

        from .phases.documentation import run_documentation_phase

        try:
            doc_result = run_documentation_phase(
                llm=self.llm,
                task=task,
                repo_path=repo_path,
                execution_result=exec_result,
                planning_result=planning_result,
                tool_agents=tool_agents,
            )
            result.documentation_result = doc_result
            if doc_result.files:
                current_files.update(doc_result.files)
                result.final_files = current_files
            logger.info("[%s] Documentation phase complete: %s", task_id, doc_result.summary)
        except Exception as exc:
            logger.warning(
                "[%s] Documentation phase failed: %s. Next step -> Continuing to Deliver phase",
                task_id, exc,
            )

        # ── Phase: Deliver ───────────────────────────────────────────
        logger.info("[%s] Next step -> Starting Phase: Deliver", task_id)
        result.current_phase = Phase.DELIVER
        _update_job(current_phase="deliver", progress=90, status_text="Committing changes and preparing delivery")

        try:
            deliver_result = run_deliver(
                task_id=task_id,
                repo_path=repo_path,
                files=current_files,
                summary=exec_result.summary,
                task_title=task.title or "",
                tool_agents=tool_agents,
                task_description=task.description or "",
                feature_branch_name=feature_branch_name,
            )
            result.deliver_result = deliver_result
            result.success = deliver_result.merged and failed_count == 0
            result.summary = f"{exec_result.summary} {deliver_result.summary}"
            if failed_count > 0:
                result.needs_followup = True
                result.summary += f" ({failed_count} microtask(s) failed review)"
        except Exception as exc:
            result.failure_reason = f"Deliver failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result

        elapsed = time.monotonic() - start_time
        final_status = "Backend task complete" if result.success else "Backend task completed with issues"
        _update_job(current_phase="deliver", progress=100 if result.success else 95, status_text=final_status)
        logger.info(
            "[%s] WORKFLOW %s in %.1fs (%d microtasks completed, %d failed review)",
            task_id,
            "SUCCEEDED" if result.success else "PARTIAL",
            elapsed,
            completed_count,
            failed_count,
        )
        return result


class BackendCodeV2TeamLead:
    """
    Backend Tech Lead Agent: runs Setup (git init, README, development branch)
    then delegates the 5-phase cycle to BackendDevelopmentAgent.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture] = None,
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        tech_lead: Any = None,
        completed_tasks: Optional[List[Task]] = None,
        remaining_tasks: Optional[List[Task]] = None,
        all_tasks: Optional[Dict[str, Task]] = None,
        execution_queue: Optional[List[str]] = None,
        append_task_fn: Optional[Callable[[Task], None]] = None,
        build_fix_specialist: Any = None,
        git_operations_tool_agent: Any = None,
        acceptance_verifier_agent: Any = None,
        dbc_agent: Any = None,
        problem_solver_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Optional[MicrotaskReviewConfig] = None,
    ) -> BackendCodeV2WorkflowResult:
        """
        Run Setup phase, then delegate to BackendDevelopmentAgent for the 5-phase cycle.
        """
        task_id = task.id
        result = BackendCodeV2WorkflowResult(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        # ── Setup phase (Backend Tech Lead) ─────────────────────────────
        result.current_phase = Phase.SETUP
        _update_job(current_phase="setup", progress=2, status_text="Setting up repository and development environment")
        try:
            setup_result = run_setup(repo_path=repo_path, task_title=task.title or "")
            result.setup_result = setup_result
        except Exception as exc:
            result.failure_reason = f"Setup failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result
        _update_job(current_phase="setup", progress=5, status_text="Repository setup complete")

        # ── Delegate to Backend Development Agent ──────────────────────
        dev_agent = BackendDevelopmentAgent(self.llm)
        inner = dev_agent.run_workflow(
            repo_path=repo_path,
            task=task,
            architecture=architecture,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            build_verifier=build_verifier,
            doc_agent=doc_agent,
            linting_tool_agent=linting_tool_agent,
            tech_lead=tech_lead,
            completed_tasks=completed_tasks,
            remaining_tasks=remaining_tasks,
            all_tasks=all_tasks,
            execution_queue=execution_queue,
            append_task_fn=append_task_fn,
            build_fix_specialist=build_fix_specialist,
            git_operations_tool_agent=git_operations_tool_agent,
            acceptance_verifier_agent=acceptance_verifier_agent,
            dbc_agent=dbc_agent,
            problem_solver_agent=problem_solver_agent,
            job_updater=job_updater,
            review_config=review_config,
        )
        result.success = inner.success
        result.current_phase = inner.current_phase
        result.iterations_used = inner.iterations_used
        result.planning_result = inner.planning_result
        result.execution_result = inner.execution_result
        result.review_result = inner.review_result
        result.problem_solving_result = inner.problem_solving_result
        result.deliver_result = inner.deliver_result
        result.final_files = inner.final_files
        result.summary = inner.summary
        result.failure_reason = inner.failure_reason
        result.needs_followup = inner.needs_followup
        return result

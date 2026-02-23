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

from shared.llm import LLMClient
from shared.models import SystemArchitecture, Task, TaskUpdate

from .models import (
    BackendCodeV2WorkflowResult,
    Phase,
    ToolAgentKind,
    ToolAgentInput,
    ToolAgentOutput,
)
from .phases.planning import run_planning
from .phases.execution import run_execution
from .phases.review import run_review
from .phases.problem_solving import run_problem_solving
from .phases.deliver import run_deliver
from .phases.setup import run_setup

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERATIONS = 5


def _build_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build team-owned tool agent instances (for plan/execute/review/problem_solve/deliver)."""
    from .tool_agents.data_engineering import DataEngineeringToolAgent
    from .tool_agents.api_openapi import ApiOpenApiToolAgent
    from .tool_agents.auth import AuthToolAgent
    from .tool_agents.cicd import CicdAdapterAgent
    from .tool_agents.containerization import ContainerizationAdapterAgent
    from .tool_agents.git_branch_management import GitBranchManagementToolAgent
    from .tool_agents.build_specialist import BuildSpecialistAdapterAgent

    return {
        ToolAgentKind.DATA_ENGINEERING: DataEngineeringToolAgent(llm),
        ToolAgentKind.API_OPENAPI: ApiOpenApiToolAgent(llm),
        ToolAgentKind.AUTH: AuthToolAgent(llm),
        ToolAgentKind.CICD: CicdAdapterAgent(),
        ToolAgentKind.CONTAINERIZATION: ContainerizationAdapterAgent(),
        ToolAgentKind.GIT_BRANCH_MANAGEMENT: GitBranchManagementToolAgent(),
        ToolAgentKind.BUILD_SPECIALIST: BuildSpecialistAdapterAgent(),
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
        spec_content: str = "",
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
    ) -> BackendCodeV2WorkflowResult:
        """
        Execute the full 5-phase backend-code-v2 lifecycle.

        Phases:
            1. Planning — decompose task into microtasks
            2. Execution — implement microtasks via tool agents / LLM
            3. Review — code review, build, lint, QA, security
            4. Problem-solving — root-cause and fix loop (on review failure)
            5. Deliver — commit and merge to development

        Steps 2-4 repeat for up to ``MAX_REVIEW_ITERATIONS``.
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

        logger.info("[%s] WORKFLOW START: Backend Development Agent (5-phase)", task_id)

        existing_code = self._read_repo_code(repo_path)
        tool_agents = _build_tool_agents(self.llm)
        tool_runners = self._build_tool_runners(tool_agents)

        # ── Phase 1: Planning ──────────────────────────────────────────
        result.current_phase = Phase.PLANNING
        _update_job(current_phase="planning", progress=5)

        try:
            planning_result = run_planning(
                llm=self.llm,
                task=task,
                repo_path=repo_path,
                spec_content=spec_content,
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

        # ── Phases 2-4: Execution → Review → Problem-solving loop ─────
        current_files: Dict[str, str] = {}
        for iteration in range(1, MAX_REVIEW_ITERATIONS + 1):
            logger.info("[%s] ── Iteration %d/%d ──", task_id, iteration, MAX_REVIEW_ITERATIONS)

            # Phase 2: Execution
            result.current_phase = Phase.EXECUTION
            _update_job(current_phase="execution", current_microtask="", progress=10 + iteration * 10)

            def _progress_cb(done: int, total: int, title: str) -> None:
                _update_job(
                    current_phase="execution",
                    current_microtask=title,
                    microtasks_completed=done,
                    microtasks_total=total,
                    progress=min(10 + iteration * 10 + int(done / max(total, 1) * 20), 80),
                )

            try:
                exec_result = run_execution(
                    llm=self.llm,
                    task=task,
                    planning_result=planning_result,
                    repo_path=repo_path,
                    spec_content=spec_content,
                    architecture=architecture,
                    existing_code=existing_code,
                    tool_runners=tool_runners,
                    progress_callback=_progress_cb,
                )
                result.execution_result = exec_result
                current_files.update(exec_result.files)
            except Exception as exc:
                result.failure_reason = f"Execution failed (iter {iteration}): {exc}"
                logger.error("[%s] %s", task_id, result.failure_reason)
                result.iterations_used = iteration
                return result

            if not current_files:
                result.failure_reason = "Execution produced no files."
                result.iterations_used = iteration
                return result

            # Write files to repo for build/review
            from shared.repo_writer import write_agent_output

            class _Payload:
                def __init__(self, files: Dict[str, str]) -> None:
                    self.files = files
                    self.summary = ""
                    self.suggested_commit_message = f"wip: iteration {iteration}"
                    self.gitignore_entries: list[str] = []

            write_agent_output(repo_path, _Payload(current_files), subdir="")

            if feature_branch_name and git_agent is not None and hasattr(git_agent, "commit_current_changes"):
                try:
                    git_agent.commit_current_changes(repo_path, f"wip: iteration {iteration}")
                except Exception as exc:
                    logger.warning("[%s] Git agent commit_current_changes raised: %s", task_id, exc)

            # Phase 3: Review
            result.current_phase = Phase.REVIEW
            _update_job(current_phase="review", progress=min(50 + iteration * 10, 85))

            try:
                review_result = run_review(
                    llm=self.llm,
                    task=task,
                    execution_result=exec_result,
                    repo_path=repo_path,
                    build_verifier=build_verifier,
                    qa_agent=qa_agent,
                    security_agent=security_agent,
                    code_review_agent=code_review_agent,
                    linting_tool_agent=linting_tool_agent,
                    tool_agents=tool_agents,
                )
                result.review_result = review_result
            except Exception as exc:
                logger.warning("[%s] Review failed (non-blocking): %s", task_id, exc)
                break  # proceed to deliver anyway

            if review_result.passed:
                logger.info("[%s] Review passed on iteration %d", task_id, iteration)
                break

            # Phase 4: Problem-solving
            result.current_phase = Phase.PROBLEM_SOLVING
            _update_job(current_phase="problem_solving", progress=min(60 + iteration * 10, 85))

            try:
                ps_result = run_problem_solving(
                    llm=self.llm,
                    task=task,
                    review_result=review_result,
                    current_files=current_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    tool_agents=tool_agents,
                )
                result.problem_solving_result = ps_result
                current_files = ps_result.files
            except Exception as exc:
                logger.warning("[%s] Problem-solving failed (non-blocking): %s", task_id, exc)
                break

            result.iterations_used = iteration

        result.iterations_used = max(result.iterations_used, 1)
        result.final_files = current_files

        # ── Phase 5: Deliver ───────────────────────────────────────────
        result.current_phase = Phase.DELIVER
        _update_job(current_phase="deliver", progress=90)

        try:
            deliver_result = run_deliver(
                task_id=task_id,
                repo_path=repo_path,
                files=current_files,
                summary=result.execution_result.summary if result.execution_result else "",
                task_title=task.title or "",
                tool_agents=tool_agents,
                task_description=task.description or "",
                feature_branch_name=feature_branch_name,
            )
            result.deliver_result = deliver_result
            result.success = deliver_result.merged
            result.summary = deliver_result.summary
        except Exception as exc:
            result.failure_reason = f"Deliver failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result

        elapsed = time.monotonic() - start_time
        _update_job(current_phase="deliver", progress=100 if result.success else 95)
        logger.info(
            "[%s] WORKFLOW %s in %.1fs (%d iterations)",
            task_id,
            "SUCCEEDED" if result.success else "FAILED",
            elapsed,
            result.iterations_used,
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
        spec_content: str = "",
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
        _update_job(current_phase="setup", progress=2)
        try:
            setup_result = run_setup(repo_path=repo_path, task_title=task.title or "")
            result.setup_result = setup_result
        except Exception as exc:
            result.failure_reason = f"Setup failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return result
        _update_job(current_phase="setup", progress=5)

        # ── Delegate to Backend Development Agent ──────────────────────
        dev_agent = BackendDevelopmentAgent(self.llm)
        inner = dev_agent.run_workflow(
            repo_path=repo_path,
            task=task,
            spec_content=spec_content,
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

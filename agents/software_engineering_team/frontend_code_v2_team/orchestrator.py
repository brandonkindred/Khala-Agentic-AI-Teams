"""
Frontend-Code-V2 team orchestrator: Setup + 5-phase state machine.

Entry point used by the main orchestrator and by the frontend-code-v2 API.
No code from frontend_team or feature_agent is imported or reused.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.llm import LLMClient
from shared.models import SystemArchitecture, Task

from .models import (
    FrontendCodeV2WorkflowResult,
    Phase,
    ToolAgentKind,
    ToolAgentInput,
    ToolAgentOutput,
)
from .phases.planning import run_planning, plan_fixes_for_unresolved_issues
from .phases.execution import run_execution
from .phases.review import run_review
from .phases.problem_solving import run_problem_solving
from .phases.deliver import run_deliver
from .phases.setup import run_setup

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERATIONS = 5


def _build_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build team-owned tool agent instances (stubs and Git/Build when implemented)."""
    from .tool_agents.state_management import StateManagementToolAgent
    from .tool_agents.auth import AuthToolAgent
    from .tool_agents.api_openapi import ApiOpenApiToolAgent
    from .tool_agents.cicd import CicdAdapterAgent
    from .tool_agents.containerization import ContainerizationAdapterAgent
    from .tool_agents.documentation import DocumentationToolAgent
    from .tool_agents.testing_qa import TestingQAToolAgent
    from .tool_agents.security import SecurityToolAgent
    from .tool_agents.git_branch_management import GitBranchManagementToolAgent
    from .tool_agents.ui_design import UiDesignToolAgent
    from .tool_agents.branding_theme import BrandingThemeToolAgent
    from .tool_agents.ux_usability import UxUsabilityToolAgent
    from .tool_agents.accessibility import AccessibilityToolAgent
    from .tool_agents.build_specialist import BuildSpecialistAdapterAgent
    from .tool_agents.linter import LinterToolAgent

    return {
        ToolAgentKind.STATE_MANAGEMENT: StateManagementToolAgent(),
        ToolAgentKind.AUTH: AuthToolAgent(),
        ToolAgentKind.API_OPENAPI: ApiOpenApiToolAgent(),
        ToolAgentKind.CICD: CicdAdapterAgent(),
        ToolAgentKind.CONTAINERIZATION: ContainerizationAdapterAgent(),
        ToolAgentKind.DOCUMENTATION: DocumentationToolAgent(),
        ToolAgentKind.TESTING_QA: TestingQAToolAgent(llm),
        ToolAgentKind.SECURITY: SecurityToolAgent(llm),
        ToolAgentKind.GIT_BRANCH_MANAGEMENT: GitBranchManagementToolAgent(),
        ToolAgentKind.UI_DESIGN: UiDesignToolAgent(),
        ToolAgentKind.BRANDING_THEME: BrandingThemeToolAgent(),
        ToolAgentKind.UX_USABILITY: UxUsabilityToolAgent(),
        ToolAgentKind.ACCESSIBILITY: AccessibilityToolAgent(),
        ToolAgentKind.BUILD_SPECIALIST: BuildSpecialistAdapterAgent(llm),
        ToolAgentKind.LINTER: LinterToolAgent(),
    }


class FrontendDevelopmentAgent:
    """
    Frontend Development Agent: runs the 5-phase cycle (Planning → Execution →
    Review → Problem-solving → Deliver). Used by FrontendCodeV2TeamLead after Setup.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def _build_tool_runners(self, tool_agents: Dict[ToolAgentKind, Any]) -> Dict[ToolAgentKind, Callable[[ToolAgentInput], ToolAgentOutput]]:
        runners = {}
        for k, ag in tool_agents.items():
            if hasattr(ag, "run"):
                runners[k] = ag.run
            elif hasattr(ag, "execute"):
                runners[k] = ag.execute
        return runners

    @staticmethod
    def _read_repo_code(repo_path: Path, max_chars: int = 30_000) -> str:
        """Read frontend source files from repo into a single string."""
        extensions = {".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".scss", ".json", ".yaml", ".yml"}
        parts: List[str] = []
        total = 0
        try:
            for f in sorted(repo_path.rglob("*")):
                if not f.is_file() or f.suffix not in extensions:
                    continue
                if any(skip in f.parts for skip in ("node_modules", ".git", "dist", "build", ".angular")):
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
        job_updater: Optional[Callable[..., None]] = None,
    ) -> FrontendCodeV2WorkflowResult:
        """Execute the full 5-phase frontend lifecycle. Steps 2-4 repeat up to MAX_REVIEW_ITERATIONS."""
        task_id = task.id
        start_time = time.monotonic()
        result = FrontendCodeV2WorkflowResult(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        logger.info("[%s] WORKFLOW START: Frontend Development Agent (5-phase)", task_id)

        existing_code = self._read_repo_code(repo_path)
        tool_agents = _build_tool_agents(self.llm)
        tool_runners = self._build_tool_runners(tool_agents)

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
        _update_job(current_phase="planning", progress=10, microtasks_total=total_microtasks, microtasks_completed=0)

        feature_branch_name: Optional[str] = None
        git_agent = tool_agents.get(ToolAgentKind.GIT_BRANCH_MANAGEMENT)
        if git_agent is not None and hasattr(git_agent, "create_feature_branch"):
            try:
                ok, branch_name = git_agent.create_feature_branch(repo_path, task_id, task.title or "")
                if ok and branch_name:
                    feature_branch_name = branch_name
            except Exception as exc:
                logger.warning("[%s] Git agent create_feature_branch raised: %s", task_id, exc)

        current_files: Dict[str, str] = {}
        for iteration in range(1, MAX_REVIEW_ITERATIONS + 1):
            logger.info("[%s] ── Iteration %d/%d ──", task_id, iteration, MAX_REVIEW_ITERATIONS)

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

            from shared.repo_writer import write_agent_output
            class _Payload:
                def __init__(self, files: Dict[str, str]) -> None:
                    self.files = files
                    self.summary = ""
                    self.suggested_commit_message = f"wip: iteration {iteration}"
                    self.gitignore_entries = []
            write_agent_output(repo_path, _Payload(current_files), subdir="")

            if feature_branch_name and git_agent is not None and hasattr(git_agent, "commit_current_changes"):
                try:
                    git_agent.commit_current_changes(repo_path, f"wip: iteration {iteration}")
                except Exception as exc:
                    logger.warning("[%s] Git agent commit_current_changes raised: %s", task_id, exc)

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
                break

            if review_result.passed:
                logger.info("[%s] Review passed on iteration %d", task_id, iteration)
                break

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
                if getattr(ps_result, "unresolved_issues", None):
                    fix_microtasks = plan_fixes_for_unresolved_issues(
                        llm=self.llm,
                        task=task,
                        unresolved_issues=ps_result.unresolved_issues,
                        current_files=current_files,
                        language=planning_result.language,
                    )
                    if fix_microtasks:
                        fix_ids = [mt.id for mt in fix_microtasks]
                        planning_result.microtasks.extend(fix_microtasks)
                        try:
                            fix_exec_result = run_execution(
                                llm=self.llm,
                                task=task,
                                planning_result=planning_result,
                                repo_path=repo_path,
                                spec_content=spec_content,
                                architecture=architecture,
                                existing_code=existing_code,
                                tool_runners=tool_runners,
                                only_microtask_ids=fix_ids,
                            )
                            current_files.update(fix_exec_result.files)
                            write_agent_output(repo_path, _Payload(current_files), subdir="")
                            if feature_branch_name and git_agent and hasattr(git_agent, "commit_current_changes"):
                                try:
                                    git_agent.commit_current_changes(repo_path, f"fix: iteration {iteration}")
                                except Exception:
                                    pass
                            review_result = run_review(
                                llm=self.llm,
                                task=task,
                                execution_result=fix_exec_result,
                                repo_path=repo_path,
                                build_verifier=build_verifier,
                                qa_agent=qa_agent,
                                security_agent=security_agent,
                                code_review_agent=code_review_agent,
                                linting_tool_agent=linting_tool_agent,
                                tool_agents=tool_agents,
                            )
                            result.review_result = review_result
                            if review_result.passed:
                                break
                        except Exception as fix_exc:
                            logger.warning("[%s] Fix microtasks failed: %s", task_id, fix_exc)
            except Exception as exc:
                logger.warning("[%s] Problem-solving failed (non-blocking): %s", task_id, exc)
                break

            result.iterations_used = iteration

        result.iterations_used = max(result.iterations_used, 1)
        result.final_files = current_files

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

        _update_job(current_phase="deliver", progress=100 if result.success else 95)
        logger.info("[%s] WORKFLOW %s in %.1fs (%d iterations)", task_id, "SUCCEEDED" if result.success else "FAILED", time.monotonic() - start_time, result.iterations_used)
        return result


class FrontendCodeV2TeamLead:
    """
    Frontend Tech Lead Agent: runs Setup (git init, README, development branch)
    then delegates the 5-phase cycle to FrontendDevelopmentAgent.
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
        job_updater: Optional[Callable[..., None]] = None,
    ) -> FrontendCodeV2WorkflowResult:
        """Run Setup phase, then delegate to FrontendDevelopmentAgent for the 5-phase cycle."""
        task_id = task.id
        result = FrontendCodeV2WorkflowResult(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

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

        dev_agent = FrontendDevelopmentAgent(self.llm)
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

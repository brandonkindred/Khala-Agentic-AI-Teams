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

from llm_service import LLMClient
from software_engineering_team.shared.models import SystemArchitecture, Task

from .models import (
    FrontendCodeV2WorkflowResult,
    MicrotaskReviewConfig,
    MicrotaskReviewFailedError,
    MicrotaskStatus,
    Phase,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
)
from .phases.deliver import run_deliver
from .phases.execution import ReviewDependencies, run_execution_with_review_gates
from .phases.planning import run_planning
from .phases.setup import run_setup

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERATIONS = 15


def _build_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build team-owned tool agent instances with LLM support where applicable."""
    from .tool_agents.accessibility import AccessibilityToolAgent
    from .tool_agents.api_openapi import ApiOpenApiToolAgent
    from .tool_agents.architecture import ArchitectureToolAgent
    from .tool_agents.auth import AuthToolAgent
    from .tool_agents.branding_theme import BrandingThemeToolAgent
    from .tool_agents.build_specialist import BuildSpecialistAdapterAgent
    from .tool_agents.cicd import CicdAdapterAgent
    from .tool_agents.containerization import ContainerizationAdapterAgent
    from .tool_agents.documentation import DocumentationToolAgent
    from .tool_agents.git_branch_management import GitBranchManagementToolAgent
    from .tool_agents.linter import LinterToolAgent
    from .tool_agents.performance import PerformanceToolAgent
    from .tool_agents.security import SecurityToolAgent
    from .tool_agents.state_management import StateManagementToolAgent
    from .tool_agents.testing_qa import TestingQAToolAgent
    from .tool_agents.ui_design import UiDesignToolAgent
    from .tool_agents.ux_usability import UxUsabilityToolAgent

    return {
        ToolAgentKind.STATE_MANAGEMENT: StateManagementToolAgent(),
        ToolAgentKind.AUTH: AuthToolAgent(),
        ToolAgentKind.API_OPENAPI: ApiOpenApiToolAgent(),
        ToolAgentKind.CICD: CicdAdapterAgent(llm),
        ToolAgentKind.CONTAINERIZATION: ContainerizationAdapterAgent(),
        ToolAgentKind.DOCUMENTATION: DocumentationToolAgent(llm),
        ToolAgentKind.TESTING_QA: TestingQAToolAgent(llm),
        ToolAgentKind.SECURITY: SecurityToolAgent(llm),
        ToolAgentKind.GIT_BRANCH_MANAGEMENT: GitBranchManagementToolAgent(),
        ToolAgentKind.UI_DESIGN: UiDesignToolAgent(llm),
        ToolAgentKind.BRANDING_THEME: BrandingThemeToolAgent(llm),
        ToolAgentKind.UX_USABILITY: UxUsabilityToolAgent(llm),
        ToolAgentKind.ACCESSIBILITY: AccessibilityToolAgent(llm),
        ToolAgentKind.PERFORMANCE: PerformanceToolAgent(llm),
        ToolAgentKind.ARCHITECTURE: ArchitectureToolAgent(llm),
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

    def _build_tool_runners(
        self, tool_agents: Dict[ToolAgentKind, Any]
    ) -> Dict[ToolAgentKind, Callable[[ToolAgentInput], ToolAgentOutput]]:
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
        extensions = {
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".html",
            ".css",
            ".scss",
            ".json",
            ".yaml",
            ".yml",
        }
        parts: List[str] = []
        total = 0
        try:
            for f in sorted(repo_path.rglob("*")):
                if not f.is_file() or f.suffix not in extensions:
                    continue
                if any(
                    skip in f.parts
                    for skip in ("node_modules", ".git", "dist", "build", ".angular")
                ):
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
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Optional[MicrotaskReviewConfig] = None,
    ) -> FrontendCodeV2WorkflowResult:
        """
        Execute the full 5-phase frontend lifecycle with per-microtask review gates.

        Each microtask must pass full review (code quality, QA, security, build, lint)
        before the next microtask can begin.
        """
        task_id = task.id
        start_time = time.monotonic()
        result = FrontendCodeV2WorkflowResult(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        logger.info(
            "[%s] WORKFLOW START: Frontend Development Agent (per-microtask review gates)", task_id
        )

        # ── Pre-flight: verify linting & testing are configured ───────
        _has_lint = bool(
            list(repo_path.glob("eslint.config.*"))
            or list(repo_path.glob(".eslintrc*"))
            or (repo_path / "angular.json").exists()
        )
        pkg_json = repo_path / "package.json"
        _has_test = False
        if bool(
            list(repo_path.glob("vitest.config.*"))
            or list(repo_path.glob("jest.config.*"))
            or (repo_path / "karma.conf.js").exists()
        ):
            _has_test = True
        elif pkg_json.exists():
            try:
                import json

                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                test_script = pkg.get("scripts", {}).get("test", "")
                if test_script and "no test" not in test_script and "exit 1" not in test_script:
                    _has_test = True
            except Exception:
                pass

        if not _has_lint or not _has_test:
            missing = []
            if not _has_lint:
                missing.append("linting")
            if not _has_test:
                missing.append("testing")
            logger.error(
                "[%s] Pre-flight check failed: %s not configured at %s",
                task_id,
                " and ".join(missing),
                repo_path,
            )
            result.failure_reason = (
                f"Pre-flight check failed: {' and '.join(missing)} not configured. "
                "The build process requires linting and testing to be set up before coding tasks begin."
            )
            return result
        logger.info("[%s] Pre-flight check passed: linting and testing configured", task_id)

        existing_code = self._read_repo_code(repo_path)
        tool_agents = _build_tool_agents(self.llm)
        tool_runners = self._build_tool_runners(tool_agents)

        logger.info("[%s] Next step -> Starting Phase: Planning", task_id)
        result.current_phase = Phase.PLANNING
        _update_job(
            current_phase="planning",
            progress=5,
            status_text="Analyzing task and creating implementation plan...",
        )

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
            status_text=f"Plan created with {total_microtasks} microtask(s)",
        )

        feature_branch_name: Optional[str] = None
        git_agent = tool_agents.get(ToolAgentKind.GIT_BRANCH_MANAGEMENT)
        if git_agent is not None and hasattr(git_agent, "create_feature_branch"):
            try:
                ok, branch_name = git_agent.create_feature_branch(
                    repo_path, task_id, task.title or ""
                )
                if ok and branch_name:
                    feature_branch_name = branch_name
            except Exception as exc:
                logger.warning("[%s] Git agent create_feature_branch raised: %s", task_id, exc)

        logger.info("[%s] Next step -> Starting Phase: Execution", task_id)
        result.current_phase = Phase.EXECUTION
        _update_job(
            current_phase="execution",
            current_microtask="",
            progress=15,
            status_text="Starting code implementation...",
        )

        def _progress_cb(
            current_index: int,
            done: int,
            total: int,
            title: str,
            microtask_phase: str = "coding",
            phase_detail: str = "",
        ) -> None:
            phase_labels = {
                "coding": "Writing code",
                "code_review": "Code review",
                "qa_testing": "QA testing",
                "security_testing": "Security testing",
                "documentation": "Documentation",
                "review": "Reviewing",
                "problem_solving": "Fixing issues",
                "completed": "Completed",
            }
            phase_label = phase_labels.get(
                microtask_phase, microtask_phase.replace("_", " ").title()
            )
            status = f"{phase_label}: {title} ({current_index}/{total})"
            if phase_detail:
                status = f"{status} — {phase_detail}"
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
            result.failure_reason = (
                f"Microtask {err.microtask.id} failed review: {err.review_result.summary}"
            )
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

        completed_count = sum(
            1 for mt in exec_result.microtasks if mt.status == MicrotaskStatus.COMPLETED
        )
        failed_count = sum(
            1 for mt in exec_result.microtasks if mt.status == MicrotaskStatus.REVIEW_FAILED
        )
        result.iterations_used = completed_count

        if (
            feature_branch_name
            and git_agent is not None
            and hasattr(git_agent, "commit_current_changes")
        ):
            try:
                git_agent.commit_current_changes(
                    repo_path, f"feat: {completed_count} microtasks completed"
                )
            except Exception as exc:
                logger.warning("[%s] Git agent commit_current_changes raised: %s", task_id, exc)

        result.final_files = current_files

        # ── Phase: Documentation ────────────────────────────────────────
        logger.info("[%s] Next step -> Starting Phase: Documentation", task_id)
        result.current_phase = Phase.DOCUMENTATION
        _update_job(
            current_phase="documentation",
            progress=80,
            status_text="Generating documentation and API docs...",
        )

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
                task_id,
                exc,
            )

        # ── Phase: Deliver ───────────────────────────────────────────
        logger.info("[%s] Next step -> Starting Phase: Deliver", task_id)
        result.current_phase = Phase.DELIVER
        _update_job(
            current_phase="deliver",
            progress=90,
            status_text="Committing changes and preparing delivery...",
        )

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

        final_status = (
            "Frontend task complete" if result.success else "Frontend task completed with issues"
        )
        _update_job(
            current_phase="deliver",
            progress=100 if result.success else 95,
            status_text=final_status,
        )
        elapsed = time.monotonic() - start_time
        logger.info(
            "[%s] WORKFLOW %s in %.1fs (%d microtasks completed, %d failed review)",
            task_id,
            "SUCCEEDED" if result.success else "PARTIAL",
            elapsed,
            completed_count,
            failed_count,
        )
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
        architecture: Optional[SystemArchitecture] = None,
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Optional[MicrotaskReviewConfig] = None,
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
        _update_job(current_phase="setup", progress=3)

        # ── Verify linting and testing are configured ─────────────────
        if not getattr(setup_result, "linting_configured", False):
            logger.warning(
                "[%s] Linting not configured after setup — coding cannot proceed without linting",
                task_id,
            )
            result.failure_reason = (
                "Setup completed but linting is not configured. "
                "Linting must be set up before any coding tasks can begin."
            )
            return result

        if not getattr(setup_result, "testing_configured", False):
            logger.warning(
                "[%s] Testing not configured after setup — coding cannot proceed without testing",
                task_id,
            )
            result.failure_reason = (
                "Setup completed but testing is not configured. "
                "Testing must be set up before any coding tasks can begin."
            )
            return result

        logger.info("[%s] Linting and testing verified — proceeding to coding phase", task_id)
        _update_job(
            current_phase="setup",
            progress=5,
            status_text="Linting and testing verified; ready for development",
        )

        dev_agent = FrontendDevelopmentAgent(self.llm)
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

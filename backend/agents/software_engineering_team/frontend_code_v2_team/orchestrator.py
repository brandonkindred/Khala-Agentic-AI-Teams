"""
Frontend-Code-V2 team orchestrator: 5-phase state machine (Setup → Planning →
Execution → Documentation → Deliver) for frontend code-generation tasks.

Entry point used by the main orchestrator and by the frontend-code-v2 API.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from llm_service import LLMClient
from shared.repo_context import read_repo_code_budgeted
from software_engineering_team.shared.git_utils import checkout_branch
from software_engineering_team.shared.models import SystemArchitecture, Task
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.team_lead_base import (
    BaseTeamLead,
    copy_development_result_fields,
)
from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

from .models import (
    FrontendCodeV2WorkflowResult,
    MicrotaskReviewConfig,
    MicrotaskReviewFailedError,
    MicrotaskStatus,
    Phase,
    ToolAgentKind,
)
from .phases.deliver import run_deliver
from .phases.execution import ReviewDependencies, run_execution_with_review_gates
from .phases.planning import run_planning
from .phases.setup import configure_quality_tooling, run_setup

logger = logging.getLogger(__name__)

# Frontend repo-briefing filter contract: the extensions read into the development
# agent's context and the directories pruned from the walk. Single-sourced here so
# the fresh-walk ``_read_repo_code`` and the incremental ``RepoContextCache`` the
# team lead threads in cannot drift apart (the cache's byte-identical invariant
# depends on them matching).
_FRONTEND_REPO_EXTENSIONS = frozenset(
    {".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".scss", ".json", ".yaml", ".yml"}
)
_FRONTEND_REPO_EXCLUDE_DIRS = frozenset({"node_modules", ".git", "dist", "build", ".angular"})
# Character budget for the repo briefing (whole files only; the next chunk that
# would exceed it stops the briefing).
_FRONTEND_REPO_BRIEFING_MAX_CHARS = 30_000


def _build_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build team-owned tool agent instances with LLM support where applicable.

    The tool-agent imports are deferred to call time on purpose: each adapter
    module pulls in heavy strands/llm_service machinery and several import the
    orchestrator's own package, so hoisting them to module top would both make
    this module expensive to import and reintroduce a circular import. Keeping
    them lazy bounds the import cost to actual runs, so they are not hoisted to
    module scope.

    Preconditions: ``llm`` is a configured ``LLMClient`` (not ``None``) — agents
      that need an LLM (documentation, testing, security, UI/UX, accessibility,
      performance, architecture, build) are constructed with it.
    Postconditions: returns a ``Dict[ToolAgentKind, Any]`` mapping every
      ``ToolAgentKind`` this team uses to a constructed agent instance; does
      not raise on the happy path.
    """
    from software_engineering_team.shared.tool_agent_git_branch import (
        GitBranchManagementToolAgent,
    )

    from .tool_agents.accessibility import AccessibilityToolAgent
    from .tool_agents.api_openapi import ApiOpenApiToolAgent
    from .tool_agents.architecture import ArchitectureToolAgent
    from .tool_agents.auth import AuthToolAgent
    from .tool_agents.branding_theme import BrandingThemeToolAgent
    from .tool_agents.build_specialist import BuildSpecialistAdapterAgent
    from .tool_agents.documentation import DocumentationToolAgent
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


class FrontendDevelopmentAgent(BaseV2DevelopmentAgent):
    """
    Frontend Development Agent: runs the 5-phase lifecycle (Pre-flight → Planning →
    Execution → Documentation → Deliver) with per-microtask review gates embedded
    in the Execution phase. Used by FrontendCodeV2TeamLead after it runs Setup.

    Inherits ``__init__`` / ``_build_tool_runners`` / ``_read_existing_code`` from
    :class:`BaseV2DevelopmentAgent`; supplies the frontend tooling detection,
    repo-briefing sets, and the integration-only ``run_workflow``.
    """

    @staticmethod
    def _read_repo_code(repo_path: Path, max_chars: int = _FRONTEND_REPO_BRIEFING_MAX_CHARS) -> str:
        """Read frontend source files from repo into a single string.

        Delegates to the shared budgeted scanner so every per-domain reader shares
        one implementation; the frontend extension/exclude sets are the contract.
        """
        return read_repo_code_budgeted(
            repo_path,
            extensions=_FRONTEND_REPO_EXTENSIONS,
            exclude_dirs=_FRONTEND_REPO_EXCLUDE_DIRS,
            max_chars=max_chars,
        )

    @staticmethod
    def _detect_tooling(repo_path: Path) -> Tuple[bool, bool]:
        """Return ``(has_lint, has_test)`` for the configured frontend tooling.

        Detects ESLint/Angular configs as lint, and Vitest/Jest/Karma or a real
        ``npm test`` script as testing. Best-effort: an unparseable ``package.json``
        just means no test script was found.

        Preconditions: ``repo_path`` is a directory.
        Postconditions: returns two booleans. Raises ``AssertionError`` if the
          precondition is violated (a non-directory ``repo_path`` is a caller
          bug, not a runtime failure mode this method recovers from).
        """
        assert repo_path.is_dir(), "repo_path must be a directory"
        has_lint = (
            next(repo_path.glob("eslint.config.*"), None) is not None
            or next(repo_path.glob(".eslintrc*"), None) is not None
            or (repo_path / "angular.json").exists()
        )
        has_test = False
        if (
            next(repo_path.glob("vitest.config.*"), None) is not None
            or next(repo_path.glob("jest.config.*"), None) is not None
            or (repo_path / "karma.conf.js").exists()
        ):
            has_test = True
        else:
            pkg_json = repo_path / "package.json"
            if pkg_json.exists():
                try:
                    pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                    test_script = pkg.get("scripts", {}).get("test", "")
                    if test_script and "no test" not in test_script and "exit 1" not in test_script:
                        has_test = True
                except Exception as exc:
                    # A malformed package.json means no test script was found;
                    # log at DEBUG so a real config problem is observable during
                    # debugging without failing the best-effort pre-flight gate.
                    logger.debug("[%s] failed to parse package.json: %s", repo_path, exc)
        return has_lint, has_test

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture] = None,
        spec_content: str = "",
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Optional[MicrotaskReviewConfig] = None,
        merge_to_development: bool = True,
        repo_context_cache: Optional[RepoContextCache] = None,
    ) -> FrontendCodeV2WorkflowResult:
        """
        Execute the full 5-phase frontend lifecycle with per-microtask review gates.

        Each microtask must pass full review (code quality, QA, security, build, lint)
        before the next microtask can begin.

        merge_to_development defaults to True. When False, the deliver phase commits
        a feature branch and leaves it ready for external Tech Lead review instead of
        merging it into the development branch.
        """
        self._repo_context_cache = repo_context_cache
        task_id = task.id
        start_time = time.monotonic()
        result = FrontendCodeV2WorkflowResult(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception as exc:
                    # A job-update failure must not crash the workflow, but log it
                    # at DEBUG so a persistently broken updater callback stays
                    # observable during debugging instead of vanishing silently.
                    logger.debug("[%s] job_updater failed: %s", task_id, exc)

        logger.info(
            "[%s] WORKFLOW START: Frontend Development Agent (per-microtask review gates)", task_id
        )

        # ── Check out the review feature branch, then ensure tooling ───
        # Setup commits lint/test scaffolding to ``development``, but a handoff
        # feature branch created before setup does not inherit it. Configure the
        # tooling on the branch we will actually edit so the pre-flight check and
        # later quality gates see the config (idempotent when already present).
        feature_branch_name = (task.feature_branch_name or "").strip() or None
        preflight_failure = self._run_preflight(
            task_id=task_id,
            repo_path=repo_path,
            feature_branch_name=feature_branch_name,
            detect_tooling=self._detect_tooling,
            checkout_branch=checkout_branch,
            configure_quality_tooling=configure_quality_tooling,
            update_job=_update_job,
            logger=logger,
        )
        if preflight_failure is not None:
            result.failure_reason = preflight_failure
            return result

        existing_code = self._read_existing_code(repo_path)
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

        git_agent = tool_agents.get(ToolAgentKind.GIT_BRANCH_MANAGEMENT)
        create_feature_branch_fn = (
            getattr(git_agent, "create_feature_branch", None) if git_agent is not None else None
        )
        if not feature_branch_name and callable(create_feature_branch_fn):
            try:
                ok, branch_name = create_feature_branch_fn(repo_path, task_id, task.title or "")
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

        progress_callback = self._build_progress_callback(_update_job, review_label="Reviewing")

        review_deps = ReviewDependencies(
            build_verifier=build_verifier,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            linting_tool_agent=linting_tool_agent,
            tool_agents=tool_agents,
        )

        config = review_config or MicrotaskReviewConfig()

        try:  # pragma: no cover  # integration-only: runs review-gated execution loop against live LLM + npm/ng
            exec_result = run_execution_with_review_gates(
                llm=self.llm,
                task=task,
                planning_result=planning_result,
                repo_path=repo_path,
                architecture=architecture,
                spec_content=spec_content,
                existing_code=existing_code,
                tool_runners=tool_runners,
                progress_callback=progress_callback,
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
                merge_to_development=merge_to_development,
            )
            result.deliver_result = deliver_result
            delivered = (
                deliver_result.merged if merge_to_development else deliver_result.branch_ready
            )
            result.success = delivered and failed_count == 0
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


class FrontendCodeV2TeamLead(BaseTeamLead):
    """
    Frontend Tech Lead Agent: runs setup, verifies the repository, then executes
    the FrontendDevelopmentAgent 5-phase workflow.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        super().__init__(
            llm_client,
            extensions=_FRONTEND_REPO_EXTENSIONS,
            exclude_dirs=_FRONTEND_REPO_EXCLUDE_DIRS,
            max_chars=_FRONTEND_REPO_BRIEFING_MAX_CHARS,
        )

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task: Task,
        architecture: Optional[SystemArchitecture] = None,
        spec_content: str = "",
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        doc_agent: Any = None,
        linting_tool_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Optional[MicrotaskReviewConfig] = None,
        merge_to_development: bool = True,
    ) -> FrontendCodeV2WorkflowResult:
        """Run setup, verify lint/test readiness, then execute the frontend 5-phase workflow.

        merge_to_development defaults to True. When False, delivery prepares a
        feature branch for external review instead of merging it.
        """
        task_id = task.id
        result = FrontendCodeV2WorkflowResult(task_id=task_id)

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception as exc:
                    logger.debug("[%s] job_updater failed: %s", task_id, exc)

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
            spec_content=spec_content,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            build_verifier=build_verifier,
            doc_agent=doc_agent,
            linting_tool_agent=linting_tool_agent,
            job_updater=job_updater,
            review_config=review_config,
            merge_to_development=merge_to_development,
            repo_context_cache=self._repo_context_cache_for(repo_path),
        )
        copy_development_result_fields(result, inner)
        return result

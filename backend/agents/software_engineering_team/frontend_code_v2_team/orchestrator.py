"""
Frontend-Code-V2 team orchestrator: 5-phase state machine (Setup → Planning →
Execution → Documentation → Deliver) for frontend code-generation tasks.

Entry point used by the main orchestrator and by the frontend-code-v2 API.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from llm_service import LLMClient
from shared.repo_context import read_repo_code_budgeted
from software_engineering_team.shared.git_utils import checkout_branch
from software_engineering_team.shared.models import SystemArchitecture, Task
from software_engineering_team.shared.phases.deliver import make_run_deliver
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.team_lead_base import BaseTeamLead
from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

from . import models as _models
from .models import (
    FrontendCodeV2WorkflowResult,
    MicrotaskReviewConfig,
    MicrotaskReviewFailedError,
    ToolAgentKind,
)
from .phases.execution import ReviewDependencies, run_execution_with_review_gates
from .phases.planning import run_planning
from .phases.setup import configure_quality_tooling, run_setup
from .prompts import DELIVER_COMMIT_MSG_TEMPLATE

logger = logging.getLogger(__name__)

run_deliver = make_run_deliver(
    models=_models,
    commit_msg_template=DELIVER_COMMIT_MSG_TEMPLATE,
    logger=logger,
)

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

    return BaseV2DevelopmentAgent._assemble_tool_agents(
        (ToolAgentKind.STATE_MANAGEMENT, StateManagementToolAgent()),
        (ToolAgentKind.AUTH, AuthToolAgent()),
        (ToolAgentKind.API_OPENAPI, ApiOpenApiToolAgent()),
        (ToolAgentKind.DOCUMENTATION, DocumentationToolAgent(llm)),
        (ToolAgentKind.TESTING_QA, TestingQAToolAgent(llm)),
        (ToolAgentKind.SECURITY, SecurityToolAgent(llm)),
        (ToolAgentKind.GIT_BRANCH_MANAGEMENT, GitBranchManagementToolAgent()),
        (ToolAgentKind.UI_DESIGN, UiDesignToolAgent(llm)),
        (ToolAgentKind.BRANDING_THEME, BrandingThemeToolAgent(llm)),
        (ToolAgentKind.UX_USABILITY, UxUsabilityToolAgent(llm)),
        (ToolAgentKind.ACCESSIBILITY, AccessibilityToolAgent(llm)),
        (ToolAgentKind.PERFORMANCE, PerformanceToolAgent(llm)),
        (ToolAgentKind.ARCHITECTURE, ArchitectureToolAgent(llm)),
        (ToolAgentKind.BUILD_SPECIALIST, BuildSpecialistAdapterAgent(llm)),
        (ToolAgentKind.LINTER, LinterToolAgent()),
    )


class FrontendDevelopmentAgent(BaseV2DevelopmentAgent):
    """
    Frontend Development Agent: runs the 5-phase lifecycle (Pre-flight → Planning →
    Execution → Documentation → Deliver) with per-microtask review gates embedded
    in the Execution phase. Used by FrontendCodeV2TeamLead after it runs Setup.

    Inherits ``__init__`` / ``_build_tool_runners`` / ``_read_existing_code`` /
    ``_run_preflight`` / ``_run_planning_and_branch_setup`` /
    ``_record_execution_bookkeeping`` / ``_run_documentation_phase`` /
    ``_run_deliver_and_finalize`` / ``_run_development_workflow`` from
    :class:`BaseV2DevelopmentAgent`; supplies the frontend tooling detection,
    repo-briefing sets, and a thin ``run_workflow`` that forwards this
    module's own tool-agent builder, planning/execution/deliver functions, and
    review classes into ``_run_development_workflow``.
    """

    _TEAM_LABEL = "Frontend"
    _DELIVER_IN_PROGRESS_STATUS = "Committing changes and preparing delivery..."

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
        from .phases.documentation import run_documentation_phase

        return self._run_development_workflow(
            repo_path=repo_path,
            task=task,
            architecture=architecture,
            spec_content=spec_content,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            build_verifier=build_verifier,
            linting_tool_agent=linting_tool_agent,
            job_updater=job_updater,
            review_config=review_config,
            merge_to_development=merge_to_development,
            repo_context_cache=repo_context_cache,
            result_cls=FrontendCodeV2WorkflowResult,
            team_label=self._TEAM_LABEL,
            deliver_in_progress_status=self._DELIVER_IN_PROGRESS_STATUS,
            logger=logger,
            checkout_branch=checkout_branch,
            configure_quality_tooling=configure_quality_tooling,
            detect_tooling=self._detect_tooling,
            emit_branch_ready_progress=False,
            build_tool_agents=_build_tool_agents,
            git_branch_management_kind=ToolAgentKind.GIT_BRANCH_MANAGEMENT,
            run_planning=run_planning,
            review_label="Reviewing",
            execution_status_text="Starting code implementation...",
            review_deps_cls=ReviewDependencies,
            review_config_cls=MicrotaskReviewConfig,
            review_failed_exc_cls=MicrotaskReviewFailedError,
            run_execution_with_review_gates=run_execution_with_review_gates,
            documentation_status_text="Generating documentation and API docs...",
            run_documentation_phase=run_documentation_phase,
            run_deliver=run_deliver,
        )


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
        return self._run_setup_and_delegate(
            repo_path=repo_path,
            task=task,
            result_cls=FrontendCodeV2WorkflowResult,
            run_setup_fn=run_setup,
            development_agent_cls=FrontendDevelopmentAgent,
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
        )

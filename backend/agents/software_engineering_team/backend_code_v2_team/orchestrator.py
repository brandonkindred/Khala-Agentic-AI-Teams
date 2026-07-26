"""
Backend-Code-V2 team orchestrator: 5-phase state machine.

Entry point used by the main orchestrator.
No code from ``backend_agent`` is imported or reused.
"""

from __future__ import annotations

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
from software_engineering_team.shared.text_utils import has_section_header, toml_has_section
from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

from . import models as _models
from .models import (
    BackendCodeV2WorkflowResult,
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

# Backend repo-briefing filter contract: the extensions read into the development
# agent's context and the directories pruned from the walk. Single-sourced here so
# the fresh-walk ``_read_repo_code`` and the incremental ``RepoContextCache`` the
# team lead threads in cannot drift apart (the cache's byte-identical invariant
# depends on them matching).
_BACKEND_REPO_EXTENSIONS = frozenset(
    {".py", ".java", ".kt", ".yaml", ".yml", ".json", ".toml", ".cfg", ".txt"}
)
_BACKEND_REPO_EXCLUDE_DIRS = frozenset({"node_modules", ".git", "__pycache__", "venv", ".venv"})
# Character budget for the repo briefing (whole files only; the next chunk that
# would exceed it stops the briefing).
_BACKEND_REPO_BRIEFING_MAX_CHARS = 30_000


def _build_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build team-owned tool agent instances (for plan/execute/review/problem_solve/deliver).

    The tool-agent imports are deferred to call time on purpose: each adapter
    pulls in heavy strands/llm_service machinery, and constructing them here at
    module import would make the orchestrator expensive to import (and would
    eagerly build agents even on paths that never run a workflow). Keeping them
    lazy bounds the import cost to actual runs, so they are not hoisted to the
    top of the module.
    """
    from software_engineering_team.shared.tool_agent_git_branch import (
        GitBranchManagementToolAgent,
    )

    from .tool_agents.api_openapi import ApiOpenApiToolAgent
    from .tool_agents.auth import AuthToolAgent
    from .tool_agents.build_specialist import BuildSpecialistAdapterAgent
    from .tool_agents.data_engineering import DataEngineeringToolAgent
    from .tool_agents.documentation import DocumentationToolAgent
    from .tool_agents.security import SecurityToolAgent
    from .tool_agents.testing_qa import TestingQAToolAgent

    return BaseV2DevelopmentAgent._assemble_tool_agents(
        (ToolAgentKind.DATA_ENGINEERING, DataEngineeringToolAgent(llm)),
        (ToolAgentKind.API_OPENAPI, ApiOpenApiToolAgent(llm)),
        (ToolAgentKind.AUTH, AuthToolAgent(llm)),
        (ToolAgentKind.GIT_BRANCH_MANAGEMENT, GitBranchManagementToolAgent()),
        (ToolAgentKind.BUILD_SPECIALIST, BuildSpecialistAdapterAgent(llm)),
        (ToolAgentKind.TESTING_QA, TestingQAToolAgent(llm)),
        (ToolAgentKind.SECURITY, SecurityToolAgent(llm)),
        (ToolAgentKind.DOCUMENTATION, DocumentationToolAgent(llm)),
    )


class BackendDevelopmentAgent(BaseV2DevelopmentAgent):
    """
    Backend Development Agent: runs the 5-phase lifecycle (Pre-flight → Planning →
    Execution → Documentation → Deliver) with per-microtask review gates embedded
    in the Execution phase. Used by BackendCodeV2TeamLead after it runs Setup.

    Inherits ``__init__`` / ``_build_tool_runners`` / ``_read_existing_code`` /
    ``_run_preflight`` / ``_run_planning_and_branch_setup`` /
    ``_record_execution_bookkeeping`` / ``_run_documentation_phase`` /
    ``_run_deliver_and_finalize`` / ``_run_development_workflow`` from
    :class:`BaseV2DevelopmentAgent`; supplies the backend tooling detection,
    repo-briefing sets, and a thin ``run_workflow`` that forwards this
    module's own tool-agent builder, planning/execution/deliver functions, and
    review classes into ``_run_development_workflow``.
    """

    _TEAM_LABEL = "Backend"
    _DELIVER_IN_PROGRESS_STATUS = "Committing changes and preparing delivery"

    @staticmethod
    def _read_repo_code(repo_path: Path, max_chars: int = _BACKEND_REPO_BRIEFING_MAX_CHARS) -> str:
        """Read Python/Java source files from repo into a single string.

        Delegates to the shared budgeted scanner so every per-domain reader shares
        one implementation; the backend extension/exclude sets are the contract.
        """
        return read_repo_code_budgeted(
            repo_path,
            extensions=_BACKEND_REPO_EXTENSIONS,
            exclude_dirs=_BACKEND_REPO_EXCLUDE_DIRS,
            max_chars=max_chars,
        )

    @staticmethod
    def _detect_tooling(repo_path: Path) -> Tuple[bool, bool]:
        """Return ``(has_lint, has_test)`` for the configured backend tooling.

        Detects ruff/flake8 (or a ``[tool.ruff]`` block in ``pyproject.toml``) as
        lint, and a ``tests`` dir with a pytest config (``pytest.ini`` or a
        ``[tool.pytest`` block in ``pyproject.toml``) as testing. Reads
        ``pyproject.toml`` once and reuses it for both probes. Lint also
        recognises a ``[flake8]`` section in ``setup.cfg`` — a common flake8
        config location that the file-name-only ``.flake8`` probe would miss.

        The ``[tool.ruff]`` / ``[tool.pytest`` pyproject checks use the shared
        ``toml_has_section`` helper: a real TOML parse (stdlib ``tomllib`` on
        Python 3.11+, the ``tomli`` backport if installed) that asks whether the
        table actually exists, so a section header appearing inside a
        multi-line string value can no longer produce a false positive; on
        Python 3.10 without ``tomli`` (or on unparseable TOML) it falls back to
        the line-anchored ``has_section_header`` text scan. The ``[flake8]``
        ``setup.cfg`` probe stays on ``has_section_header`` (INI has no
        multi-line strings, so the text scan is exact there). No hard dependency
        is added: 3.11+ stdlib covers the real runtime, and 3.10 keeps the prior
        best-effort text probe. The pre-flight only decides whether to fail the
        task early for missing tooling, so a residual false positive errs toward
        proceeding (a real build/lint gate still enforces correctness).

        Preconditions: ``repo_path`` is a directory.
        Postconditions: returns two booleans. Raises ``AssertionError`` if the
          precondition is violated (a non-directory ``repo_path`` is a caller
          bug, not a runtime failure mode this method recovers from).
        """
        assert repo_path.is_dir(), "repo_path must be a directory"
        pyproject_path = repo_path / "pyproject.toml"
        pyproject_text = (
            pyproject_path.read_text(encoding="utf-8", errors="replace")
            if pyproject_path.exists()
            else ""
        )
        setup_cfg_path = repo_path / "setup.cfg"
        setup_cfg_text = (
            setup_cfg_path.read_text(encoding="utf-8", errors="replace")
            if setup_cfg_path.exists()
            else ""
        )
        has_lint = (
            (repo_path / "ruff.toml").exists()
            or (repo_path / ".flake8").exists()
            or toml_has_section(pyproject_text, "[tool.ruff]")
            or has_section_header(setup_cfg_text, "[flake8]")
        )
        has_test = (repo_path / "tests").is_dir() and (
            (repo_path / "pytest.ini").exists() or toml_has_section(pyproject_text, "[tool.pytest")
        )
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
    ) -> BackendCodeV2WorkflowResult:
        """
        Execute the full 5-phase backend-code-v2 lifecycle with per-microtask review gates.

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
            result_cls=BackendCodeV2WorkflowResult,
            team_label=self._TEAM_LABEL,
            deliver_in_progress_status=self._DELIVER_IN_PROGRESS_STATUS,
            logger=logger,
            checkout_branch=checkout_branch,
            configure_quality_tooling=configure_quality_tooling,
            detect_tooling=self._detect_tooling,
            emit_branch_ready_progress=True,
            build_tool_agents=_build_tool_agents,
            git_branch_management_kind=ToolAgentKind.GIT_BRANCH_MANAGEMENT,
            run_planning=run_planning,
            review_label="Reviewing code",
            execution_status_text="Starting code implementation",
            review_deps_cls=ReviewDependencies,
            review_config_cls=MicrotaskReviewConfig,
            review_failed_exc_cls=MicrotaskReviewFailedError,
            run_execution_with_review_gates=run_execution_with_review_gates,
            documentation_status_text="Generating documentation and API specs",
            run_documentation_phase=run_documentation_phase,
            run_deliver=run_deliver,
        )


class BackendCodeV2TeamLead(BaseTeamLead):
    """
    Backend Tech Lead Agent: runs setup, verifies the repository, then executes
    the BackendDevelopmentAgent 5-phase workflow.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        super().__init__(
            llm_client,
            extensions=_BACKEND_REPO_EXTENSIONS,
            exclude_dirs=_BACKEND_REPO_EXCLUDE_DIRS,
            max_chars=_BACKEND_REPO_BRIEFING_MAX_CHARS,
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
    ) -> BackendCodeV2WorkflowResult:
        """Run setup, verify lint/test readiness, then execute the backend 5-phase workflow.

        merge_to_development defaults to True. When False, delivery prepares a
        feature branch for external review instead of merging it.
        """
        return self._run_setup_and_delegate(
            repo_path=repo_path,
            task=task,
            result_cls=BackendCodeV2WorkflowResult,
            run_setup_fn=run_setup,
            development_agent_cls=BackendDevelopmentAgent,
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

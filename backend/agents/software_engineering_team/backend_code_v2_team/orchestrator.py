"""
Backend-Code-V2 team orchestrator: 5-phase state machine.

Entry point used by the main orchestrator.
No code from ``backend_agent`` is imported or reused.

Re-expressed (Story 3b, Step 3) as a thin config instance over
:class:`~software_engineering_team.shared.v2_orchestrator.ConfigDrivenV2DevelopmentAgent`:
``BackendDevelopmentAgent`` subclasses the config-driven base and supplies only
a :class:`~software_engineering_team.shared.v2_team_config.V2TeamConfig` — the
language default, tool-agent registry, and conventions map all flow from that
config rather than from hand-written class attributes or separate lookups.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from llm_service import LLMClient
from shared.dev_models.models import SystemArchitecture, Task
from shared.git.git_utils import checkout_branch
from software_engineering_team.shared.phases.deliver import make_run_deliver
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.team_lead_base import BaseTeamLead
from software_engineering_team.shared.v2_orchestrator import ConfigDrivenV2DevelopmentAgent

from . import models as _models
from .models import (
    BackendCodeV2WorkflowResult,
    MicrotaskReviewConfig,
    ToolAgentKind,
)
from .phases._profile import BACKEND_CONFIG, PROFILE
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


def _build_tool_agents_impl(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
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

    return ConfigDrivenV2DevelopmentAgent._assemble_tool_agents(
        (ToolAgentKind.DATA_ENGINEERING, DataEngineeringToolAgent(llm)),
        (ToolAgentKind.API_OPENAPI, ApiOpenApiToolAgent(llm)),
        (ToolAgentKind.AUTH, AuthToolAgent(llm)),
        (ToolAgentKind.GIT_BRANCH_MANAGEMENT, GitBranchManagementToolAgent()),
        (ToolAgentKind.BUILD_SPECIALIST, BuildSpecialistAdapterAgent(llm)),
        (ToolAgentKind.TESTING_QA, TestingQAToolAgent(llm)),
        (ToolAgentKind.SECURITY, SecurityToolAgent(llm)),
        (ToolAgentKind.DOCUMENTATION, DocumentationToolAgent(llm)),
    )


class BackendDevelopmentAgent(ConfigDrivenV2DevelopmentAgent):
    """
    Backend Development Agent: runs the 5-phase lifecycle (Pre-flight -> Planning ->
    Execution -> Documentation -> Deliver) with per-microtask review gates embedded
    in the Execution phase. Used by BackendCodeV2TeamLead after it runs Setup.

    Subclasses :class:`ConfigDrivenV2DevelopmentAgent` and supplies only a
    :class:`V2TeamConfig` instance (``BACKEND_CONFIG``) — the language default,
    tool-agent registry, conventions map, and (empty) extra review clause all
    resolve through the config rather than hand-written class attributes.

    The ``PROFILE`` class attribute is retained for backward compatibility with
    callers that read ``BackendDevelopmentAgent.PROFILE`` (e.g. parity tests,
    the team-lead's ``_run_setup_and_delegate`` pre-flight); it is *not* used
    by the config-driven base (which reads ``self.config.stack_profile``
    instead).
    """

    _TEAM_LABEL = "Backend"
    _DELIVER_IN_PROGRESS_STATUS = "Committing changes and preparing delivery"
    PROFILE = PROFILE

    def __init__(self, llm_client: LLMClient) -> None:
        """Construct with the module-level ``BACKEND_CONFIG``.

        Accepts only ``llm_client`` so the signature stays compatible with
        ``BaseTeamLead._run_setup_and_delegate``'s
        ``development_agent_cls(self.llm)`` contract.
        """
        super().__init__(llm_client, BACKEND_CONFIG)

    def _build_tool_agents(self, llm: LLMClient) -> Dict[ToolAgentKind, Any]:
        """Build backend tool agents — delegates to the module-level builder."""
        return _build_tool_agents_impl(llm)

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
            build_tool_agents=self._build_and_validate_tool_agents,
            git_branch_management_kind=ToolAgentKind.GIT_BRANCH_MANAGEMENT,
            run_planning=run_planning,
            review_label="Reviewing code",
            execution_status_text="Starting code implementation",
            review_deps_cls=ReviewDependencies,
            review_config_cls=MicrotaskReviewConfig,
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
            extensions=PROFILE.repo_extensions,
            exclude_dirs=PROFILE.repo_exclude_dirs,
            max_chars=PROFILE.repo_max_chars,
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

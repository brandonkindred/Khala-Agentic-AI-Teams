"""
Codegen team orchestrator: config-driven 7-phase state machine (Setup ->
Planning -> Execution -> Review -> Problem Solving -> Documentation -> Deliver)
for backend and frontend code-generation tasks, selected at construction time
by a ``stack`` parameter.

Entry point used by the coding-team engine's ``CodeEngineProvider`` seam
(``coding_engine_provider.py``), the SE team's standalone Temporal activities
and HTTP routes (``temporal/activities.py``, ``api/routes/code_v2.py``), and
``build_fix.py``.

Merges what were previously two near-identical team packages
(``backend_code_v2_team``, ``frontend_code_v2_team``) into one: both were
already thin config instances over
:class:`~software_engineering_team.shared.v2_orchestrator.ConfigDrivenV2DevelopmentAgent`
and :class:`~software_engineering_team.shared.team_lead_base.BaseTeamLead`,
diverging only in which :class:`~software_engineering_team.shared.v2_team_config.V2TeamConfig`,
tool-agent roster, prompt module, and handful of ``run_workflow``-level
functions/strings they wired in. ``CodegenDevelopmentAgent`` and
``CodegenTeamLead`` now take that divergence as a runtime ``stack`` parameter
(``"backend"`` or ``"frontend"``) instead of being two separate classes; the
remaining per-stack wiring is captured as data in :class:`StackWiring`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional, Tuple

from llm_service import LLMClient
from shared.dev_models.models import SystemArchitecture, Task
from shared.git.git_utils import checkout_branch
from software_engineering_team.shared.phases.deliver import make_run_deliver
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.team_lead_base import BaseTeamLead, warn_doc_agent_deprecated
from software_engineering_team.shared.v2_orchestrator import ConfigDrivenV2DevelopmentAgent
from software_engineering_team.shared.v2_team_config import V2TeamConfig

from . import models as _models
from .models import CodegenWorkflowResult, MicrotaskReviewConfig, ToolAgentKind
from .stacks.backend import profile as _backend_profile
from .stacks.backend import prompts as _backend_prompts
from .stacks.frontend import profile as _frontend_profile
from .stacks.frontend import prompts as _frontend_prompts

logger = logging.getLogger(__name__)

Stack = Literal["backend", "frontend"]

STACK_CONFIGS: Dict[Stack, V2TeamConfig] = {
    "backend": _backend_profile.BACKEND_CONFIG,
    "frontend": _frontend_profile.FRONTEND_CONFIG,
}

_backend_run_deliver = make_run_deliver(
    models=_models,
    commit_msg_template=_backend_prompts.DELIVER_COMMIT_MSG_TEMPLATE,
    logger=logger,
)
_frontend_run_deliver = make_run_deliver(
    models=_models,
    commit_msg_template=_frontend_prompts.DELIVER_COMMIT_MSG_TEMPLATE,
    logger=logger,
)


def _build_backend_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build the backend stack's tool agent instances.

    The tool-agent imports are deferred to call time on purpose: each adapter
    pulls in heavy strands/llm_service machinery, and constructing them here at
    module import would make the orchestrator expensive to import (and would
    eagerly build agents even on paths that never run a workflow).

    Preconditions: ``llm`` is a configured ``LLMClient`` (not ``None``) —
      every backend tool agent is constructed with it.
    """
    if llm is None:
        raise ValueError("llm must be a configured LLMClient (not None)")

    from software_engineering_team.shared.tool_agent_git_branch import (
        GitBranchManagementToolAgent,
    )

    from .tool_agents.backend.api_openapi import ApiOpenApiToolAgent
    from .tool_agents.backend.auth import AuthToolAgent
    from .tool_agents.backend.build_specialist import BuildSpecialistAdapterAgent
    from .tool_agents.backend.data_engineering import DataEngineeringToolAgent
    from .tool_agents.backend.documentation import DocumentationToolAgent
    from .tool_agents.backend.security import SecurityToolAgent
    from .tool_agents.backend.testing_qa import TestingQAToolAgent

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


def _build_frontend_tool_agents(llm: LLMClient) -> Dict[ToolAgentKind, Any]:
    """Build the frontend stack's tool agent instances.

    The tool-agent imports are deferred to call time for the same reason as
    :func:`_build_backend_tool_agents`.

    Preconditions: ``llm`` is a configured ``LLMClient`` (not ``None``) —
      agents that need an LLM (documentation, testing, security, UI/UX,
      accessibility, performance, architecture, build, auth, api_openapi,
      state_management, linter) are constructed with it.
    """
    if llm is None:
        raise ValueError("llm must be a configured LLMClient (not None)")

    from software_engineering_team.shared.tool_agent_git_branch import (
        GitBranchManagementToolAgent,
    )

    from .tool_agents.frontend.accessibility import AccessibilityToolAgent
    from .tool_agents.frontend.api_openapi import ApiOpenApiToolAgent
    from .tool_agents.frontend.architecture import ArchitectureToolAgent
    from .tool_agents.frontend.auth import AuthToolAgent
    from .tool_agents.frontend.branding_theme import BrandingThemeToolAgent
    from .tool_agents.frontend.build_specialist import BuildSpecialistAdapterAgent
    from .tool_agents.frontend.documentation import DocumentationToolAgent
    from .tool_agents.frontend.linter import LinterToolAgent
    from .tool_agents.frontend.performance import PerformanceToolAgent
    from .tool_agents.frontend.security import SecurityToolAgent
    from .tool_agents.frontend.state_management import StateManagementToolAgent
    from .tool_agents.frontend.testing_qa import TestingQAToolAgent
    from .tool_agents.frontend.ui_design import UiDesignToolAgent
    from .tool_agents.frontend.ux_usability import UxUsabilityToolAgent

    return ConfigDrivenV2DevelopmentAgent._assemble_tool_agents(
        (ToolAgentKind.STATE_MANAGEMENT, StateManagementToolAgent(llm)),
        (ToolAgentKind.AUTH, AuthToolAgent(llm)),
        (ToolAgentKind.API_OPENAPI, ApiOpenApiToolAgent(llm)),
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
        (ToolAgentKind.LINTER, LinterToolAgent(llm)),
    )


@dataclass(frozen=True)
class StackWiring:
    """The ``run_workflow``-level divergence between the backend and frontend stacks.

    Everything a code-v2 team's config (:class:`V2TeamConfig`/``StackProfile``)
    does not already capture: a couple of status/label strings, the review
    dependencies class, and the one real behavioral difference
    (``emit_branch_ready_progress``).

    Deliberately does NOT hold the per-phase callables (``run_setup``,
    ``configure_quality_tooling``, ``run_planning``,
    ``run_execution_with_review_gates``, ``run_documentation_phase``,
    ``run_deliver``): a frozen dataclass field captures a function *value* once
    at module-import time, which would defeat ``monkeypatch.setattr`` on the
    owning stack profile module (patching the module attribute afterwards
    would not change what is already stored here). Those six are instead bare
    per-stack module-level names below -- five (``_backend_run_setup`` /
    ``_frontend_run_setup`` etc.) aliased directly from the stack profile
    modules, plus ``_backend_run_deliver``/``_frontend_run_deliver``, built via
    ``make_run_deliver`` rather than aliased from a profile (``run_deliver``
    isn't part of either ``StackProfile``) but still a bare module-level name
    subject to the same lookup -- resolved via ``globals()`` at
    ``run_workflow`` call time -- mirroring how each former per-team
    orchestrator module referenced its own bare ``run_setup`` etc. global
    directly, just with an explicit stack prefix now that one module serves
    both stacks. Tests must monkeypatch the bare per-stack name on *this*
    module (e.g. ``monkeypatch.setattr(orch, "_backend_run_setup", fake)``):
    the profile-aliased names bind the profile module's function *value* once
    at import time, so patching the owning profile module's attribute
    afterward (e.g. ``monkeypatch.setattr(backend_profile,
    "run_documentation_phase", fake)``) does *not* change what
    ``_backend_run_documentation_phase`` already points to here -- only
    ``globals()`` lookups against this module's own names stay live.
    """

    team_label: str
    deliver_in_progress_status: str
    build_tool_agents: Callable[[LLMClient], Dict[ToolAgentKind, Any]]
    emit_branch_ready_progress: bool
    review_label: str
    execution_status_text: str
    review_deps_cls: Callable[..., Any]
    documentation_status_text: str


STACK_WIRING: Dict[Stack, StackWiring] = {
    "backend": StackWiring(
        team_label="Backend",
        deliver_in_progress_status="Committing changes and preparing delivery",
        build_tool_agents=_build_backend_tool_agents,
        emit_branch_ready_progress=True,
        review_label="Reviewing code",
        execution_status_text="Starting code implementation",
        review_deps_cls=_backend_profile.ReviewDependencies,
        documentation_status_text="Generating documentation and API specs",
    ),
    "frontend": StackWiring(
        team_label="Frontend",
        deliver_in_progress_status="Committing changes and preparing delivery...",
        build_tool_agents=_build_frontend_tool_agents,
        emit_branch_ready_progress=False,
        review_label="Reviewing",
        execution_status_text="Starting code implementation...",
        review_deps_cls=_frontend_profile.ReviewDependencies,
        documentation_status_text="Generating documentation and API docs...",
    ),
}

# Bare per-stack module-level names for the six phase callables that must
# stay individually monkeypatchable per stack (see StackWiring's docstring
# above) -- five aliased directly from the stack profiles, plus run_deliver
# (built via make_run_deliver at module level above, not aliased from a
# profile).
# ``run_workflow`` looks each of these up via ``globals()[f"_{stack}_<name>"]``
# at call time rather than reading a precomputed StackWiring field, so a
# test's ``monkeypatch.setattr`` on the bare name here is honored on the next
# call -- patching the owning profile module's own attribute afterward is NOT
# honored, since the profile-aliased names bind the function value once at
# import time.
_backend_run_setup = _backend_profile.run_setup
_frontend_run_setup = _frontend_profile.run_setup
_backend_configure_quality_tooling = _backend_profile.configure_quality_tooling
_frontend_configure_quality_tooling = _frontend_profile.configure_quality_tooling
_backend_run_planning = _backend_profile.run_planning
_frontend_run_planning = _frontend_profile.run_planning
_backend_run_execution_with_review_gates = _backend_profile.run_execution_with_review_gates
_frontend_run_execution_with_review_gates = _frontend_profile.run_execution_with_review_gates
_backend_run_documentation_phase = _backend_profile.run_documentation_phase
_frontend_run_documentation_phase = _frontend_profile.run_documentation_phase


def _validate_stack(stack: str) -> None:
    """Validate ``stack`` is a known key of :data:`STACK_CONFIGS`/:data:`STACK_WIRING`.

    Raises ``ValueError`` (not ``assert``) since ``stack`` is external input
    reaching this boundary from callers outside static-type enforcement (e.g.
    ``team_kind`` strings routed through ``coding_engine_provider.py``), and
    ``assert`` is stripped under ``python -O``/``PYTHONOPTIMIZE=1``.
    """
    if stack not in STACK_CONFIGS:
        raise ValueError(f"stack must be one of {sorted(STACK_CONFIGS)}, got {stack!r}")


class CodegenDevelopmentAgent(ConfigDrivenV2DevelopmentAgent):
    """
    Codegen Development Agent: runs everything after CodegenTeamLead's own Setup
    phase in the module docstring's 7-phase list -- Planning, Execution (with the
    Review and Problem Solving phases embedded as per-microtask gates rather than
    separate top-level calls), Documentation, and Deliver -- for either the
    backend or frontend stack. Before Planning, it also runs its own Pre-flight
    check (verifying lint/test tooling is configured on the feature branch); this
    is a check internal to this agent, distinct from CodegenTeamLead's Setup
    phase, not a named member of the ``Phase`` enum.

    Subclasses :class:`ConfigDrivenV2DevelopmentAgent` and supplies the
    ``stack``'s :class:`V2TeamConfig` instance — the language default,
    tool-agent registry, conventions map, and extra review clause all resolve
    through the config rather than hand-written class attributes. The
    remaining per-stack divergence (which phase functions to call, status
    text) resolves through :data:`STACK_WIRING`.
    """

    def __init__(self, llm_client: LLMClient, stack: Stack) -> None:
        """Construct with the ``V2TeamConfig`` for ``stack``.

        Preconditions: ``stack`` is ``"backend"`` or ``"frontend"``.
        Postconditions: ``self.stack`` is ``stack``; ``self.config`` is
          ``STACK_CONFIGS[stack]`` (see ``ConfigDrivenV2DevelopmentAgent.__init__``).
        """
        _validate_stack(stack)
        self.stack: Stack = stack
        super().__init__(llm_client, STACK_CONFIGS[stack])

    def _build_tool_agents(self, llm: LLMClient) -> Dict[ToolAgentKind, Any]:
        """Build this instance's stack's tool agents — delegates to :data:`STACK_WIRING`."""
        return STACK_WIRING[self.stack].build_tool_agents(llm)

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
    ) -> CodegenWorkflowResult:
        """
        Execute the full 5-phase lifecycle for this instance's stack with
        per-microtask review gates.

        Each microtask must pass full review (code quality, QA, security, build, lint)
        before the next microtask can begin.

        Args:
            repo_path: Path to the checked-out repo the workflow writes into.
            task: The task being implemented (id/title/description/assignee).
            architecture: Optional system architecture context passed to planning.
            spec_content: Optional spec text passed to planning/execution prompts.
            qa_agent: Optional pre-built QA review tool agent to reuse instead of
                the stack's own; falls back to the stack's tool-agent roster when
                ``None``.
            security_agent: Same as ``qa_agent`` but for the security review pass.
            code_review_agent: Optional pre-built production code-review agent
                (e.g. from ``shared.production_review_agents``) reused for the
                Code Review gate instead of an LLM one-shot review.
            build_verifier: Optional callable ``(repo_path, agent_type, task_id) ->
                (success, message)`` reused as the build gate instead of the
                stack's default build tooling.
            doc_agent: Deprecated and ignored — see
                :func:`software_engineering_team.shared.team_lead_base.warn_doc_agent_deprecated`.
            linting_tool_agent: Optional pre-built linting tool agent reused for
                the lint gate instead of the stack's default.
            job_updater: Optional callback invoked with progress/status updates
                as the workflow advances through phases.
            review_config: Optional ``MicrotaskReviewConfig`` overriding the
                default per-gate retry caps; defaults to ``MicrotaskReviewConfig()``.
            merge_to_development: Defaults to True. When False, the deliver phase
                commits a feature branch and leaves it ready for external Tech
                Lead review instead of merging it into the development branch.
            repo_context_cache: Optional shared repo-context cache reused across
                calls to avoid re-scanning the repo for each microtask.

        Returns:
            A :class:`CodegenWorkflowResult` capturing every phase's outcome.
        """
        warn_doc_agent_deprecated(doc_agent)

        stack = self.stack
        wiring = STACK_WIRING[stack]
        g = globals()

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
            result_cls=CodegenWorkflowResult,
            team_label=wiring.team_label,
            deliver_in_progress_status=wiring.deliver_in_progress_status,
            logger=logger,
            checkout_branch=checkout_branch,
            configure_quality_tooling=g[f"_{stack}_configure_quality_tooling"],
            detect_tooling=self._detect_tooling,
            emit_branch_ready_progress=wiring.emit_branch_ready_progress,
            build_tool_agents=self._build_and_validate_tool_agents,
            git_branch_management_kind=ToolAgentKind.GIT_BRANCH_MANAGEMENT,
            run_planning=g[f"_{stack}_run_planning"],
            review_label=wiring.review_label,
            execution_status_text=wiring.execution_status_text,
            review_deps_cls=wiring.review_deps_cls,
            review_config_cls=MicrotaskReviewConfig,
            run_execution_with_review_gates=g[f"_{stack}_run_execution_with_review_gates"],
            documentation_status_text=wiring.documentation_status_text,
            run_documentation_phase=g[f"_{stack}_run_documentation_phase"],
            run_deliver=g[f"_{stack}_run_deliver"],
        )


class CodegenTeamLead(BaseTeamLead):
    """
    Codegen Tech Lead Agent: runs setup, verifies the repository, then executes
    the CodegenDevelopmentAgent 5-phase workflow for the ``stack`` given at
    construction time (``"backend"`` or ``"frontend"``).
    """

    def __init__(self, llm_client: LLMClient, stack: Stack) -> None:
        """Construct for ``stack``.

        Preconditions: ``stack`` is ``"backend"`` or ``"frontend"``.
        Postconditions: ``self.stack`` is ``stack``; the repo-briefing
          extensions/exclude-dirs/char-budget passed to
          ``BaseTeamLead.__init__`` come from ``stack``'s ``StackProfile``.
        """
        _validate_stack(stack)
        self.stack: Stack = stack
        profile = STACK_CONFIGS[stack].stack_profile
        super().__init__(
            llm_client,
            extensions=profile.repo_extensions,
            exclude_dirs=profile.repo_exclude_dirs,
            max_chars=profile.repo_max_chars,
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
    ) -> CodegenWorkflowResult:
        """Run setup, verify lint/test readiness, then execute this instance's
        stack's 5-phase workflow.

        Args:
            repo_path: Path to the checked-out repo the workflow writes into.
            task: The task being implemented (id/title/description/assignee).
            architecture: Optional system architecture context passed to planning.
            spec_content: Optional spec text passed to planning/execution prompts.
            qa_agent: Optional pre-built QA review tool agent to reuse instead of
                the stack's own.
            security_agent: Same as ``qa_agent`` but for the security review pass.
            code_review_agent: Optional pre-built production code-review agent
                reused for the Code Review gate instead of an LLM one-shot review.
            build_verifier: Optional callable ``(repo_path, agent_type, task_id) ->
                (success, message)`` reused as the build gate instead of the
                stack's default build tooling.
            doc_agent: Deprecated and ignored — see
                :func:`software_engineering_team.shared.team_lead_base.warn_doc_agent_deprecated`.
                The warning fires here, unconditionally, before setup runs (so it
                still fires even if setup or the lint/test readiness gate fails);
                ``doc_agent`` is not forwarded past this point.
            linting_tool_agent: Optional pre-built linting tool agent reused for
                the lint gate instead of the stack's default.
            job_updater: Optional callback invoked with progress/status updates
                as the workflow advances through phases.
            review_config: Optional ``MicrotaskReviewConfig`` overriding the
                default per-gate retry caps.
            merge_to_development: Defaults to True. When False, delivery prepares
                a feature branch for external Tech Lead review instead of
                merging it into the development branch.

        Returns:
            A :class:`CodegenWorkflowResult` capturing every phase's outcome.
        """
        warn_doc_agent_deprecated(doc_agent)

        stack = self.stack

        def _development_agent_cls(llm: LLMClient) -> CodegenDevelopmentAgent:
            return CodegenDevelopmentAgent(llm, stack)

        return self._run_setup_and_delegate(
            repo_path=repo_path,
            task=task,
            result_cls=CodegenWorkflowResult,
            run_setup_fn=globals()[f"_{stack}_run_setup"],
            development_agent_cls=_development_agent_cls,
            architecture=architecture,
            spec_content=spec_content,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            build_verifier=build_verifier,
            doc_agent=None,
            linting_tool_agent=linting_tool_agent,
            job_updater=job_updater,
            review_config=review_config,
            merge_to_development=merge_to_development,
        )

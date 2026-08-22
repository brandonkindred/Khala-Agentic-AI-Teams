"""
Parity tests for the config-driven backend and frontend v2 orchestrators.

Proves that ``BackendDevelopmentAgent`` and ``FrontendDevelopmentAgent`` — now
re-expressed as thin ``ConfigDrivenV2DevelopmentAgent`` subclasses over
``V2TeamConfig`` — behave identically to their pre-change implementations for
representative runs. The config/property axes (default_language,
conventions_for, tool_agent_kinds, extra_review_clause,
build_task_requirements) against each team's *real* values are already
proven by ``test_v2_team_config.py`` (``TestBackendParity``/
``TestFrontendParity``) and ``test_v2_config_orchestrator.py``
(``TestBackendConfigParity``/``TestFrontendConfigParity``) -- this module
doesn't re-assert them. It instead exercises what's unique to the real
production classes:

- That each agent is wired to its module-level config singleton
  (``BACKEND_CONFIG``/``FRONTEND_CONFIG``), not a copy
- The real tool-agent registry actually constructs (via
  ``_build_and_validate_tool_agents``)
- The frontend accessibility-verification note's content and exact
  append structure (nothing else checks these)
- Full pipeline run via ``run_workflow`` verifying config values flow to
  deliver -- ``run_workflow`` is a thin public wrapper that immediately
  delegates to ``_run_development_workflow``
- Cross-team divergence: the two teams' real configs differ only on the
  declared axes
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from shared.dev_models.models import Task, TaskStatus, TaskType
from software_engineering_team.shared.v2_orchestrator import ConfigDrivenV2DevelopmentAgent
from software_engineering_team.shared.v2_team_config import V2TeamConfig

# ---------------------------------------------------------------------------
# Backend parity tests
# ---------------------------------------------------------------------------


class TestBackendOrchestratorParity:
    """Prove ``BackendDevelopmentAgent`` (now a ``ConfigDrivenV2DevelopmentAgent``
    subclass) behaves identically to the pre-change hand-wired implementation.

    Focuses on what's unique to the *real* production class: that it is
    wired to the module-level ``BACKEND_CONFIG`` singleton, its tool-agent
    registry actually constructs, and a full mocked ``run_workflow`` drives
    config-resolved values into deliver. The config/property axes
    (default_language, conventions_for, tool_agent_kinds, extra_review_clause,
    build_task_requirements) are already proven against backend's real
    values by ``test_v2_team_config.py::TestBackendParity`` and
    ``test_v2_config_orchestrator.py::TestBackendConfigParity`` -- not
    re-asserted here.
    """

    def _make_agent(self):
        from software_engineering_team.backend_code_v2_team.orchestrator import (
            BackendDevelopmentAgent,
        )

        return BackendDevelopmentAgent(MagicMock())

    def test_is_config_driven_subclass(self):
        """BackendDevelopmentAgent subclasses ConfigDrivenV2DevelopmentAgent."""
        agent = self._make_agent()
        assert isinstance(agent, ConfigDrivenV2DevelopmentAgent)

    def test_config_is_the_module_level_backend_config(self):
        """The agent's config is the canonical BACKEND_CONFIG instance.

        ``BACKEND_CONFIG`` has no public alias (unlike ``PROFILE``, which
        ``BackendDevelopmentAgent`` re-exposes for exactly this reason), so
        this is a deliberate, singular reach into the team's private
        ``phases._profile`` module -- matching the pattern established in
        ``test_v2_config_orchestrator.py``.
        """
        from software_engineering_team.backend_code_v2_team.phases._profile import BACKEND_CONFIG

        agent = self._make_agent()
        assert agent.config is BACKEND_CONFIG

    def test_build_task_requirements_empty_base_returns_empty(self):
        """Empty base + empty clause == empty."""
        agent = self._make_agent()
        assert agent.build_task_requirements("") == ""

    def test_stack_profile_is_the_real_profile_object(self):
        """The config's stack_profile is the team's canonical PROFILE, not a copy."""
        from software_engineering_team.backend_code_v2_team.orchestrator import (
            BackendDevelopmentAgent,
        )

        agent = self._make_agent()
        assert agent._stack_profile() is BackendDevelopmentAgent.PROFILE

    def test_build_verify_label_from_profile(self):
        """build_verify_label resolves through the config's stack_profile."""
        agent = self._make_agent()
        assert agent._stack_profile().build_verify_label == "backend_code_v2"

    def test_detect_tooling_delegates_to_profile(self, tmp_path: Path):
        """_detect_tooling uses the profile's callable (detects ruff + pytest)."""
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n[tool.pytest.ini_options]\n")
        (tmp_path / "tests").mkdir()
        agent = self._make_agent()
        has_lint, has_test = agent._detect_tooling(tmp_path)
        assert has_lint is True
        assert has_test is True

    def test_detect_tooling_no_tooling(self, tmp_path: Path):
        """A bare directory has neither lint nor test."""
        agent = self._make_agent()
        has_lint, has_test = agent._detect_tooling(tmp_path)
        assert has_lint is False
        assert has_test is False

    def test_build_and_validate_tool_agents_succeeds(self):
        """The real tool-agent builder passes validation against the config registry."""
        agent = self._make_agent()
        agents = agent._build_and_validate_tool_agents(MagicMock())
        # Keys are ToolAgentKind enum members — their string values must match
        built_kinds = frozenset(k.value for k in agents)
        assert built_kinds == agent.tool_agent_kinds

    def test_run_workflow_full_pipeline_parity(self, tmp_path: Path, monkeypatch):
        """A representative mocked run through BackendDevelopmentAgent.run_workflow
        succeeds, verifying the config-driven base resolves and passes the correct
        build_verify_label and lint_agent_type to the deliver phase. ``run_workflow``
        is a thin public wrapper that immediately delegates to
        ``_run_development_workflow``, so exercising it here also exercises that
        seam end-to-end."""
        from software_engineering_team.backend_code_v2_team import orchestrator as orch
        from software_engineering_team.backend_code_v2_team.models import (
            DeliverResult,
            DocumentationPhaseResult,
            ExecutionResult,
            Microtask,
            MicrotaskStatus,
            PlanningResult,
            ToolAgentKind,
        )

        # Set up repo with lint + test tooling
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n[tool.pytest.ini_options]\n")
        (tmp_path / "tests").mkdir()

        class _GitAgent:
            def create_feature_branch(self, *_a, **_kw):
                return True, "feature/api"

            def commit_current_changes(self, *_a, **_kw):
                return True, "committed"

        captured_deliver_kwargs: dict[str, Any] = {}

        def _run_deliver(**kwargs):
            captured_deliver_kwargs.update(kwargs)
            return DeliverResult(
                branch_name=kwargs["feature_branch_name"],
                branch_ready=True, merged=True,
                summary="delivered",
            )

        monkeypatch.setattr(orch, "checkout_branch", lambda *_a, **_kw: (True, "checked out"))
        monkeypatch.setattr(
            orch.BackendDevelopmentAgent,
            "_build_and_validate_tool_agents",
            lambda _self, _llm: {ToolAgentKind.GIT_BRANCH_MANAGEMENT: _GitAgent()},
        )
        monkeypatch.setattr(
            orch.BackendDevelopmentAgent, "_read_repo_code", lambda _self, _p: "repo code"
        )
        monkeypatch.setattr(
            orch,
            "run_planning",
            lambda **_kw: PlanningResult(
                microtasks=[Microtask(id="mt-1")], summary="planned"
            ),
        )
        monkeypatch.setattr(
            orch,
            "run_execution_with_review_gates",
            lambda **_kw: ExecutionResult(
                files={"app.py": "print('ok')\n"},
                microtasks=[Microtask(id="mt-1", status=MicrotaskStatus.COMPLETED)],
                summary="implemented",
            ),
        )

        from software_engineering_team.backend_code_v2_team.phases import (
            documentation as doc_phase,
        )

        monkeypatch.setattr(
            doc_phase,
            "run_documentation_phase",
            lambda **_kw: DocumentationPhaseResult(summary="docs"),
        )
        monkeypatch.setattr(orch, "run_deliver", _run_deliver)

        task = Task(
            id="t-be",
            type=TaskType.BACKEND,
            assignee="backend-code-v2",
            status=TaskStatus.PENDING,
            title="Build API",
            description="Implement API endpoints",
        )

        result = self._make_agent().run_workflow(
            repo_path=tmp_path,
            task=task,
        )

        assert result.success is True
        # Deliver phase received the config-resolved build_verify_label
        assert captured_deliver_kwargs["build_verify_label"] == "backend_code_v2"
        # Deliver phase received the config-resolved lint_agent_type (profile name)
        assert captured_deliver_kwargs["lint_agent_type"] == "backend"


# ---------------------------------------------------------------------------
# Frontend parity tests
# ---------------------------------------------------------------------------


class TestFrontendOrchestratorParity:
    """Prove ``FrontendDevelopmentAgent`` (now a ``ConfigDrivenV2DevelopmentAgent``
    subclass) behaves identically to the pre-change hand-wired implementation.

    Focuses on what's unique to the *real* production class: that it is
    wired to the module-level ``FRONTEND_CONFIG`` singleton, its tool-agent
    registry actually constructs, and a full mocked ``run_workflow`` drives
    config-resolved values into deliver. The config/property axes
    (default_language, conventions_for, tool_agent_kinds) are already
    proven against frontend's real values by
    ``test_v2_team_config.py::TestFrontendParity`` and
    ``test_v2_config_orchestrator.py::TestFrontendConfigParity`` -- not
    re-asserted here. The accessibility-clause *content* and its exact
    append structure are kept below since neither other file checks them.
    """

    def _make_agent(self):
        from software_engineering_team.frontend_code_v2_team.orchestrator import (
            FrontendDevelopmentAgent,
        )

        return FrontendDevelopmentAgent(MagicMock())

    def test_is_config_driven_subclass(self):
        """FrontendDevelopmentAgent subclasses ConfigDrivenV2DevelopmentAgent."""
        agent = self._make_agent()
        assert isinstance(agent, ConfigDrivenV2DevelopmentAgent)

    def test_config_is_the_module_level_frontend_config(self):
        """The agent's config is the canonical FRONTEND_CONFIG instance.

        ``FRONTEND_CONFIG`` has no public alias (unlike ``PROFILE``, which
        ``FrontendDevelopmentAgent`` re-exposes for exactly this reason), so
        this is a deliberate, singular reach into the team's private
        ``phases._profile`` module -- matching the pattern established in
        ``test_v2_config_orchestrator.py``.
        """
        from software_engineering_team.frontend_code_v2_team.phases._profile import FRONTEND_CONFIG

        agent = self._make_agent()
        assert agent.config is FRONTEND_CONFIG

    def test_conventions_map_has_exactly_one_key(self):
        """Frontend's conventions map contains only _default — no language-specific entries."""
        agent = self._make_agent()
        assert set(agent.config.stack_profile.conventions_by_language.keys()) == {"_default"}

    def test_extra_review_clause_content(self):
        """The accessibility note mentions key concerns."""
        agent = self._make_agent()
        clause = agent.extra_review_clause
        assert "accessibility" in clause.lower()
        assert "aria" in clause.lower()
        assert "keyboard" in clause

    def test_build_task_requirements_appends_accessibility_clause(self):
        """With a non-empty base, build_task_requirements appends the clause after a blank line."""
        agent = self._make_agent()
        result = agent.build_task_requirements("Review the code.")
        assert result == "Review the code.\n\n" + agent.extra_review_clause

    def test_build_task_requirements_empty_base_returns_clause_verbatim(self):
        """With empty base, build_task_requirements returns the clause verbatim."""
        agent = self._make_agent()
        result = agent.build_task_requirements("")
        assert result == agent.extra_review_clause

    def test_stack_profile_is_the_real_profile_object(self):
        """The config's stack_profile is the team's canonical PROFILE, not a copy."""
        from software_engineering_team.frontend_code_v2_team.orchestrator import (
            FrontendDevelopmentAgent,
        )

        agent = self._make_agent()
        assert agent._stack_profile() is FrontendDevelopmentAgent.PROFILE

    def test_build_verify_label_from_profile(self):
        """build_verify_label resolves through the config's stack_profile."""
        agent = self._make_agent()
        assert agent._stack_profile().build_verify_label == "frontend_code_v2"

    def test_detect_tooling_delegates_to_profile(self, tmp_path: Path):
        """_detect_tooling uses the profile's callable (detects eslint + vitest)."""
        (tmp_path / "eslint.config.js").write_text("export default [];\n")
        (tmp_path / "vitest.config.ts").write_text("export default {};\n")
        agent = self._make_agent()
        has_lint, has_test = agent._detect_tooling(tmp_path)
        assert has_lint is True
        assert has_test is True

    def test_detect_tooling_no_tooling(self, tmp_path: Path):
        """A bare directory has neither lint nor test."""
        agent = self._make_agent()
        has_lint, has_test = agent._detect_tooling(tmp_path)
        assert has_lint is False
        assert has_test is False

    def test_build_and_validate_tool_agents_succeeds(self):
        """The real tool-agent builder passes validation against the config registry."""
        agent = self._make_agent()
        agents = agent._build_and_validate_tool_agents(MagicMock())
        # Keys are ToolAgentKind enum members — their string values must match
        built_kinds = frozenset(k.value for k in agents)
        assert built_kinds == agent.tool_agent_kinds

    def test_run_workflow_full_pipeline_parity(self, tmp_path: Path, monkeypatch):
        """A representative mocked run through FrontendDevelopmentAgent.run_workflow
        succeeds, verifying the config-driven base resolves and passes the correct
        build_verify_label and lint_agent_type to the deliver phase. ``run_workflow``
        is a thin public wrapper that immediately delegates to
        ``_run_development_workflow``, so exercising it here also exercises that
        seam end-to-end."""
        from software_engineering_team.frontend_code_v2_team import orchestrator as orch
        from software_engineering_team.frontend_code_v2_team.models import (
            DeliverResult,
            DocumentationPhaseResult,
            ExecutionResult,
            Microtask,
            MicrotaskStatus,
            PlanningResult,
            ToolAgentKind,
        )

        # Set up repo with lint + test tooling
        (tmp_path / "eslint.config.js").write_text("export default [];\n")
        (tmp_path / "vitest.config.ts").write_text("export default {};\n")

        class _GitAgent:
            def create_feature_branch(self, *_a, **_kw):
                return True, "feature/ui"

            def commit_current_changes(self, *_a, **_kw):
                return True, "committed"

        captured_deliver_kwargs: dict[str, Any] = {}

        def _run_deliver(**kwargs):
            captured_deliver_kwargs.update(kwargs)
            return DeliverResult(
                branch_name=kwargs["feature_branch_name"],
                branch_ready=True, merged=True,
                summary="delivered",
            )

        monkeypatch.setattr(orch, "checkout_branch", lambda *_a, **_kw: (True, "checked out"))
        monkeypatch.setattr(
            orch.FrontendDevelopmentAgent,
            "_build_and_validate_tool_agents",
            lambda _self, _llm: {ToolAgentKind.GIT_BRANCH_MANAGEMENT: _GitAgent()},
        )
        monkeypatch.setattr(
            orch.FrontendDevelopmentAgent, "_read_repo_code", lambda _self, _p: "repo code"
        )
        monkeypatch.setattr(
            orch,
            "run_planning",
            lambda **_kw: PlanningResult(
                microtasks=[Microtask(id="mt-1")], summary="planned"
            ),
        )
        monkeypatch.setattr(
            orch,
            "run_execution_with_review_gates",
            lambda **_kw: ExecutionResult(
                files={"src/app.component.ts": "export class AppComponent {}\n"},
                microtasks=[Microtask(id="mt-1", status=MicrotaskStatus.COMPLETED)],
                summary="implemented",
            ),
        )

        from software_engineering_team.frontend_code_v2_team.phases import (
            documentation as doc_phase,
        )

        monkeypatch.setattr(
            doc_phase,
            "run_documentation_phase",
            lambda **_kw: DocumentationPhaseResult(summary="docs"),
        )
        monkeypatch.setattr(orch, "run_deliver", _run_deliver)

        task = Task(
            id="t-fe",
            type=TaskType.FRONTEND,
            assignee="frontend-code-v2",
            status=TaskStatus.PENDING,
            title="Build UI",
            description="Implement login component",
        )

        result = self._make_agent().run_workflow(
            repo_path=tmp_path,
            task=task,
        )

        assert result.success is True
        # Deliver phase received the config-resolved build_verify_label
        assert captured_deliver_kwargs["build_verify_label"] == "frontend_code_v2"
        # Deliver phase received the config-resolved lint_agent_type (profile name)
        assert captured_deliver_kwargs["lint_agent_type"] == "frontend"


# ---------------------------------------------------------------------------
# Cross-team parity: prove the two teams diverge only on the declared axes
# ---------------------------------------------------------------------------


class TestCrossTeamConfigDivergence:
    """Assert backend and frontend configs diverge only on the documented axes
    (language default, tool-agent registry, conventions map, extra review clause)
    and agree on structural properties.

    Needs the whole ``V2TeamConfig`` object (not just ``stack_profile``,
    which ``PROFILE`` covers), so it reaches into each team's private
    ``phases._profile`` module for ``BACKEND_CONFIG``/``FRONTEND_CONFIG`` --
    neither has a public alias, the same deliberate exception documented on
    ``TestBackendOrchestratorParity``/``TestFrontendOrchestratorParity`` above.
    """

    def _backend_config(self) -> V2TeamConfig:
        from software_engineering_team.backend_code_v2_team.phases._profile import BACKEND_CONFIG

        return BACKEND_CONFIG

    def _frontend_config(self) -> V2TeamConfig:
        from software_engineering_team.frontend_code_v2_team.phases._profile import FRONTEND_CONFIG

        return FRONTEND_CONFIG

    def test_both_configs_are_frozen(self):
        """Both configs are immutable (frozen dataclass)."""
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            self._backend_config().extra_review_clause = "changed"
        with pytest.raises(dataclasses.FrozenInstanceError):
            self._frontend_config().extra_review_clause = "changed"

    def test_different_default_languages(self):
        """Backend defaults to python; frontend defaults to typescript."""
        be = self._backend_config()
        fe = self._frontend_config()
        assert be.stack_profile.default_language == "python"
        assert fe.stack_profile.default_language == "typescript"
        assert be.stack_profile.default_language != fe.stack_profile.default_language

    def test_different_tool_agent_registries(self):
        """Backend and frontend have different tool-agent kind sets."""
        be = self._backend_config()
        fe = self._frontend_config()
        assert be.tool_agent_kinds != fe.tool_agent_kinds
        # Both share some common kinds
        common = be.tool_agent_kinds & fe.tool_agent_kinds
        assert "security" in common
        assert "testing_qa" in common
        assert "git_branch_management" in common

    def test_frontend_has_accessibility_kind_backend_does_not(self):
        """Only frontend registers the accessibility tool-agent kind."""
        be = self._backend_config()
        fe = self._frontend_config()
        assert "accessibility" in fe.tool_agent_kinds
        assert "accessibility" not in be.tool_agent_kinds

    def test_backend_has_data_engineering_kind_frontend_does_not(self):
        """Only backend registers the data_engineering tool-agent kind."""
        be = self._backend_config()
        fe = self._frontend_config()
        assert "data_engineering" in be.tool_agent_kinds
        assert "data_engineering" not in fe.tool_agent_kinds

    def test_different_conventions_maps(self):
        """Backend has java + _default; frontend has only _default."""
        be = self._backend_config()
        fe = self._frontend_config()
        assert "java" in be.stack_profile.conventions_by_language
        assert "java" not in fe.stack_profile.conventions_by_language
        # Both have _default
        assert "_default" in be.stack_profile.conventions_by_language
        assert "_default" in fe.stack_profile.conventions_by_language

    def test_different_extra_review_clauses(self):
        """Backend has no extra clause; frontend has the accessibility note."""
        be = self._backend_config()
        fe = self._frontend_config()
        assert be.extra_review_clause == ""
        assert fe.extra_review_clause != ""
        assert "accessibility" in fe.extra_review_clause.lower()

    def test_both_profiles_have_non_empty_build_verify_labels(self):
        """Both teams have distinct, non-empty build_verify_label values."""
        be = self._backend_config()
        fe = self._frontend_config()
        assert be.stack_profile.build_verify_label != ""
        assert fe.stack_profile.build_verify_label != ""
        assert be.stack_profile.build_verify_label != fe.stack_profile.build_verify_label

    def test_both_profiles_have_non_empty_names(self):
        """Both teams have distinct, non-empty profile names (lint_agent_type)."""
        be = self._backend_config()
        fe = self._frontend_config()
        assert be.stack_profile.name == "backend"
        assert fe.stack_profile.name == "frontend"
